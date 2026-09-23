"""asf.tick.file_bugs — file/bump Bugs from ci, refusals and rule violations (``asf file-bugs``).

The learning loop: three sources file or bump a Bug, keyed on the typed `signature` field so
"same signature = same Bug" needs no id lookup table of its own:
  - metrics/ci: a `failed_step` seen >= 2 times in the last 24h
  - metrics/ticks: a file refused >= 2 times in the last 24h
  - `asf rules check --json`: every current violation
A signature already carrying today's date in its typed `last_filed` is left alone — this is
what makes a second same-day run a no-op instead of double-counting a still-open problem.
"""
import datetime
import glob
import json
import os
import subprocess
import sys

from asf.conventions import Conventions
from asf.record import frontmatter
from asf.record.core import canonicalize, load_items, today
from asf.record.index import do_index
from asf.record.ids import mint_id, write_new_item
from asf.record.ingest import append_history_lines
from asf.tick.migrate import truncate
from asf.tick.stale import parse_iso

CI_REFUSAL_WINDOW_H = 24

#: The conventions a caller with no Product reads: the trunk is `main`, no batch lane, no
#: default Bug Epic. The Epic a filed Bug is parented under is `conventions.default_bug_epic`;
#: a product that configures none leaves it unset, `_file_or_bump_bug` omits `parent`, and
#: `asf check` flags it for a human to fix once — the same "ask rather than guess" rule the
#: groom's inbox intake follows. ``$ASF_DEFAULT_BUG_EPIC`` still overrides, for one run.
DEFAULTS = Conventions()


def _jsonl_lines(pattern):
    for path in sorted(glob.glob(pattern)):
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def ci_signatures(root, now, conv=None):
    """CI failures seen twice in the window. A failure on the trunk or on a merge-batch branch
    (`conventions.branch_prefixes`) is one severity worse: it blocks everyone, not one branch."""
    conv = conv or DEFAULTS
    cutoff = now - datetime.timedelta(hours=CI_REFUSAL_WINDOW_H)
    raw = {}
    for run in _jsonl_lines(os.path.join(root, 'metrics', 'ci', '*.jsonl')):
        ts = parse_iso(run.get('ts'))
        if ts is None or ts < cutoff:
            continue
        branch = run.get('branch') or ''
        main_or_batch = conv.is_trunk(branch) or conv.branch_kind(branch) == 'batch'
        for job in run.get('jobs') or []:
            failed_step = job.get('failed_step')
            if not failed_step:
                continue
            job_name = job.get('name', '')
            sig = f"{job_name}: {failed_step}"
            d = raw.setdefault(sig, {'count': 0, 'runs': set(), 'evidence': [], 'main_or_batch': False})
            d['count'] += 1
            d['runs'].add(run.get('run'))
            d['main_or_batch'] = d['main_or_batch'] or main_or_batch
            d['evidence'].append(
                f"run {run.get('run')} on {branch} (sha {str(run.get('sha') or '')[:9]}) "
                f"{run.get('ts')}: {job_name} — {failed_step}")

    out = {}
    for sig, d in raw.items():
        if d['count'] < 2:
            continue
        out[sig] = {
            'title': truncate(f"CI red: {sig}", 120),
            'severity': 'S2' if d['main_or_batch'] else 'S3',
            'evidence': d['evidence'],
            'runs': sorted(r for r in d['runs'] if r is not None),
        }
    return out


def refusal_signatures(root, now):
    cutoff = now - datetime.timedelta(hours=CI_REFUSAL_WINDOW_H)
    totals = {}
    for tick in _jsonl_lines(os.path.join(root, 'metrics', 'ticks', '*.jsonl')):
        ts = parse_iso(tick.get('ts'))
        if ts is None or ts < cutoff:
            continue
        for file, n in (tick.get('refused_files') or {}).items():
            d = totals.setdefault(file, {'count': 0, 'evidence': []})
            d['count'] += n
            d['evidence'].append(f"tick {tick.get('tick')} ({tick.get('ts')}): refused {n}×")

    out = {}
    for file, d in totals.items():
        if d['count'] < 2:
            continue
        sig = f"refusal: {file}"
        out[sig] = {'title': truncate(f"Refused twice: {file}", 120), 'severity': 'S3',
                    'evidence': d['evidence'], 'runs': []}
    return out


_ASF_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def rule_violation_signatures(root):
    env_vars = dict(os.environ)
    env_vars['BACKLOG_ROOT'] = root
    env_vars['PYTHONPATH'] = _ASF_REPO_ROOT + os.pathsep + env_vars.get('PYTHONPATH', '')
    try:
        proc = subprocess.run(
            [sys.executable, '-m', 'asf.rules.rules', 'check', '--json'],
            cwd=root, env=env_vars, capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode not in (0, 1):
        return {}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}

    # One Bug per RULE, not per place: a Bug per violating location buries the few real problems
    # under noise. The signature is the rule id; every violating place is an evidence line, and
    # `places` carries how many there are this run.
    titles = {}
    try:
        idx = json.load(open(os.path.join(root, 'index.json'), encoding='utf-8')).get('items', {})
        titles = {k: v.get('title', '') for k, v in idx.items() if v.get('type') == 'rule'}
    except (OSError, json.JSONDecodeError):
        pass
    out = {}
    for v in data.get('violations') or []:
        rule, line = v.get('rule', ''), v.get('line', '')
        sig = f"{rule}: rule violated"
        d = out.setdefault(sig, {'title': truncate(f"{rule} violated: {titles.get(rule) or 'see the rule card'}", 120),
                                 'severity': 'S2', 'evidence': [], 'runs': [], 'places': 0})
        d['evidence'].append(line)
        d['places'] += 1
    return out


def _find_bug_by_signature(canonical, sig):
    for rec in canonical.values():
        typed, _machine = frontmatter.split_machine(rec['meta'])
        if typed.get('type') == 'bug' and typed.get('signature') == sig:
            return rec
    return None


def _file_or_bump_bug(root, canonical, sig, info, date, default_bug_epic=None):
    rec = _find_bug_by_signature(canonical, sig)
    if rec is not None:
        typed, _machine = frontmatter.split_machine(rec['meta'])
        if typed.get('last_filed') == date:
            return 'skipped'
        old_count = typed.get('count') or 1
        new_count = old_count + 1
        updates = {'count': new_count, 'last_filed': date}
        if info.get('places'):
            updates['places'] = info['places']
        if info['runs']:
            merged = sorted(set((typed.get('links') or {}).get('runs') or []) | set(info['runs']))
            links = dict(typed.get('links') or {})
            links['runs'] = merged
            updates['links'] = links
        frontmatter.write_typed(rec['path'], updates)
        with open(rec['path'], encoding='utf-8') as f:
            text = f.read()
        meta2, body2 = frontmatter.parse(text, path=rec['relpath'])
        latest = info['evidence'][-1] if info['evidence'] else 'seen again'
        hist = f"- {date} file-bugs: count {old_count} → {new_count} ({latest})"
        new_body = append_history_lines(body2, [hist])
        if new_body != body2:
            with open(rec['path'], 'w', encoding='utf-8') as f:
                f.write(frontmatter.render(meta2, new_body))
        return 'bumped'

    typed = {
        'title': info['title'], 'severity': info['severity'], 'found_in': 'ci',
        'signature': sig, 'count': 1, 'last_filed': date, 'decided': False,
        'parent': default_bug_epic,
    }
    if info.get('places'):
        typed['places'] = info['places']
    if info['runs']:
        typed['links'] = {'runs': sorted(info['runs'])}
    body = '\n'.join(f"- {l}" for l in info['evidence'])
    new_id = mint_id(root, canonical, 'bug')
    write_new_item(root, canonical, 'bug', new_id, typed, body, date, 'file-bugs')
    return 'filed'


def _conventions(args):
    """The product's conventions, or the defaults when there is no product config to read
    (a test, or `asf file-bugs` run against a checkout on its own)."""
    from asf import env
    conv = getattr(args, 'conventions', None)
    if conv is not None:
        return conv
    try:
        return env.load_product(getattr(args, 'product', None)).conventions
    except (env.ConfigError, OSError):
        return DEFAULTS


def cmd_file_bugs(args, root):
    by_id, parse_errors = load_items(root)
    if parse_errors:
        for f, line, why in parse_errors:
            print(f"{f}:{line}: {why}", file=sys.stderr)
        return 1
    canonical, _dupes = canonicalize(by_id)
    now = datetime.datetime.now(datetime.timezone.utc)
    date = today()

    conv = _conventions(args)
    default_bug_epic = (getattr(args, 'default_bug_epic', None)
                        or os.environ.get('ASF_DEFAULT_BUG_EPIC')
                        or conv.default_bug_epic)

    signatures = {}
    signatures.update(ci_signatures(root, now, conv))
    signatures.update(refusal_signatures(root, now))
    signatures.update(rule_violation_signatures(root))

    level = getattr(args, 'file_bug_level', 'auto')
    if level != 'auto':
        prefix = 'NEEDS OPERATOR: ' if level == 'human-now' else ''
        for sig in sorted(signatures):
            print(f'{prefix}held file_bug on {sig} — widen approvals: file_bug in products/<p>.yaml')
        return 0

    filed = bumped = skipped = 0
    for sig in sorted(signatures):
        outcome = _file_or_bump_bug(root, canonical, sig, signatures[sig], date,
                                    default_bug_epic=default_bug_epic)
        if outcome == 'filed':
            filed += 1
        elif outcome == 'bumped':
            bumped += 1
        else:
            skipped += 1

    if filed or bumped:
        do_index(root)
    print(f"file-bugs: {filed} filed, {bumped} bumped, {skipped} unchanged")
    return 0
