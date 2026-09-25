"""asf.tick.file_bugs — file/bump Bugs from ci, refusals, rule violations and the record's own
standing errors (``asf file-bugs``).

The learning loop: four sources file or bump a Bug, keyed on the typed `signature` field so
"same signature = same Bug" needs no id lookup table of its own:
  - metrics/ci: a `failed_step` seen >= 2 times in the last 24h
  - metrics/ticks: a file refused >= 2 times in the last 24h
  - `asf rules check --json`: every current violation — never a `broken` check (timed out or
    crashed): that is a check failure, printed as ``rule check timed out: R-nnnn`` and, once it
    persists, surfaced once as a factory-side ``NEEDS OPERATOR`` line (``report_check_failures``)
  - `asf check` over the record: one Bug per error CLASS (``record_error_signatures``)
  - the CI logs of ``conventions.ci_workflow``: one counted Bug per flaky e2e test
    (:mod:`asf.tick.flaky`, keyed and counted in ``state/<p>/flaky.json``)
A signature already carrying today's date in its typed `last_filed` is left alone — this is
what makes a second same-day run a no-op instead of double-counting a still-open problem.
"""
import datetime
import glob
import json
import os
import re
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
#: A CI failure's Bug title opens with this, then its ``<job>: <failed step>`` signature — the
#: groom's ``decide_or_close_ci_red`` reads the job back off it.
CI_RED_TITLE = 'CI red: '

#: The conventions a caller with no Product reads: the trunk is `main`, no batch lane, no
#: default Bug Epic. The Epic a filed Bug is parented under is `conventions.default_bug_epic`;
#: a product that configures none — or names a removed, closed or missing Epic (``usable_bug_epic``)
#: — leaves it unset, `_file_or_bump_bug` omits `parent`, and
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
    """CI failures seen twice in the window — or once, when it is the trunk's latest run of that
    job: the trunk is red *now*, and the lane holds every PR red on the same check until it is
    green (:meth:`asf.harvest.lane.GitHubHost.check_gate`), so the Bug for it cannot wait for a
    second failure. A failure on the trunk or on a merge-batch branch
    (`conventions.branch_prefixes`) is one severity worse: it blocks everyone, not one branch."""
    conv = conv or DEFAULTS
    cutoff = now - datetime.timedelta(hours=CI_REFUSAL_WINDOW_H)
    raw = {}
    trunk_latest = {}  # job name -> (ts, failed step or None) of the trunk's newest run of it
    for run in _jsonl_lines(os.path.join(root, 'metrics', 'ci', '*.jsonl')):
        ts = parse_iso(run.get('ts'))
        if ts is None or ts < cutoff:
            continue
        branch = run.get('branch') or ''
        main_or_batch = conv.is_trunk(branch) or conv.branch_kind(branch) == 'batch'
        for job in run.get('jobs') or []:
            failed_step = job.get('failed_step')
            job_name = job.get('name', '')
            if conv.is_trunk(branch) and (job_name not in trunk_latest
                                          or ts >= trunk_latest[job_name][0]):
                trunk_latest[job_name] = (ts, failed_step or None)
            if not failed_step:
                continue
            sig = f"{job_name}: {failed_step}"
            d = raw.setdefault(sig, {'count': 0, 'runs': set(), 'evidence': [], 'main_or_batch': False})
            d['count'] += 1
            d['runs'].add(run.get('run'))
            d['main_or_batch'] = d['main_or_batch'] or main_or_batch
            d['evidence'].append(
                f"run {run.get('run')} on {branch} (sha {str(run.get('sha') or '')[:9]}) "
                f"{run.get('ts')}: {job_name} — {failed_step}")

    out = {}
    red_now = {f"{name}: {step}" for name, (_ts, step) in trunk_latest.items() if step}
    for sig, d in raw.items():
        if d['count'] < 2 and sig not in red_now:
            continue
        out[sig] = {
            'title': truncate(f"{CI_RED_TITLE}{sig}", 120),
            'severity': 'S2' if d['main_or_batch'] else 'S3',
            'evidence': d['evidence'],
            'runs': sorted(r for r in d['runs'] if r is not None),
            'acceptance': [f"`{sig}` fails in no CI run for {CI_REFUSAL_WINDOW_H}h"],
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
                    'evidence': d['evidence'], 'runs': [],
                    'acceptance': [f"no tick refuses `{file}` for {CI_REFUSAL_WINDOW_H}h"]}
    return out


_ASF_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def rule_check_results(root):
    """``asf rules check --json`` over ``root``: ``{'violations': [...], 'broken': [...]}``, or
    None when the run itself could not be read."""
    env_vars = dict(os.environ)
    env_vars['BACKLOG_ROOT'] = root
    env_vars['PYTHONPATH'] = _ASF_REPO_ROOT + os.pathsep + env_vars.get('PYTHONPATH', '')
    try:
        proc = subprocess.run(
            [sys.executable, '-m', 'asf.rules.rules', 'check', '--json'],
            cwd=root, env=env_vars, capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode not in (0, 1):
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def rule_violation_signatures(root, data=None):
    """One signature per violated rule. Only ``violations`` count: a check that timed out or
    crashed (``broken``) said nothing about the product and files no Bug here."""
    if data is None:
        data = rule_check_results(root)
    if not data:
        return {}

    # One Bug per RULE, not per place: a Bug per violating location buries the few real problems
    # under noise. The signature is the rule id; every violating place is an evidence line, and
    # `places` carries how many there are this run.
    rules_idx = {}
    try:
        with open(os.path.join(root, 'index.json'), encoding='utf-8') as f:
            idx = json.load(f).get('items', {})
        rules_idx = {k: v for k, v in idx.items() if v.get('type') == 'rule'}
    except (OSError, json.JSONDecodeError):
        pass
    out = {}
    for v in data.get('violations') or []:
        rule, line = v.get('rule', ''), v.get('line', '')
        card = rules_idx.get(rule) or {}
        sig = f"{rule}: rule violated"
        check = card.get('check')
        acceptance = (f"`asf rules check` reports no violation of {rule}: its check "
                      f"`bash {check}` exits 0" if check else
                      f"`asf rules check` reports no violation of {rule}")
        d = out.setdefault(sig, {'title': truncate(f"{rule} violated: {card.get('title') or 'see the rule card'}", 120),
                                 'severity': 'S2', 'evidence': [], 'runs': [], 'places': 0,
                                 'acceptance': [acceptance]})
        d['evidence'].append(line)
        d['places'] += 1
    return out


#: A standing record error's signature: ``record error: <class>``.
RECORD_ERROR_SIG = 'record error: {klass}'
#: How many places a record-error Bug names before it says "and N more" — a Bug is a page, and a
#: record with 300 cards missing a field would otherwise write its whole card list into one.
RECORD_ERROR_PLACES = 10
#: What makes two errors the same class: the message with its specifics — quoted text and item
#: ids — taken out. The placeholder is never itself a bare `D<n>`, or the Bug's own title would
#: be an instance of the error it reports. An opening `'` may not follow a word character: a
#: possessive (`T-0001's`) would otherwise pair with the *next* specific's opening quote and
#: leave that specific in the class, splitting one defect into one class per specific (B-0132).
_QUOTED_RE = re.compile(r"(?<![A-Za-z0-9])'[^']*'|\"[^\"]*\"")
_ITEM_ID_RE = re.compile(r'\b[A-Z]-[0-9]{4}\b')


def error_class(message):
    """The class of a record error: its message with the specifics taken out, so ``bare decision
    reference 'D1'`` in eight cards is one class, not eight."""
    return ' '.join(_ITEM_ID_RE.sub('…', _QUOTED_RE.sub('…', message)).split())


def record_error_signatures(root, findings=None, canonical=None):
    """One signature per error CLASS `asf check` reports over the record, every card carrying it
    an evidence line (B-0132). The record pre-commit judges only what a commit stages, so an
    error in a card nobody touched no longer refuses anything — this is what keeps that standing
    debt owned by a card of its own instead of printed as a warning for ever. The layout checks
    are left out: a missing stream folder is `asf init`'s job, not a card's defect.

    A card this tool filed itself (one carrying a typed ``signature``) is not the record's debt
    and is skipped: a Bug filed with no parent because the product configures no usable
    ``default_bug_epic`` is already reported once (``usable_bug_epic``), and filing a Bug about
    it would make every run file a Bug about the Bug the last run filed."""
    if findings is None:
        from asf.record.check import record_findings
        findings, _warnings, _index_wrong = record_findings(root, layout=False)
    if canonical is None:
        by_id, _errors = load_items(root)
        canonical, _dupes = canonicalize(by_id)
    filed_here = {rec['relpath'] for rec in canonical.values()
                  if frontmatter.split_machine(rec['meta'])[0].get('signature')}
    out = {}
    for path, line, message in sorted(findings):
        if path in filed_here:
            continue
        klass = error_class(message)
        sig = RECORD_ERROR_SIG.format(klass=klass)
        d = out.setdefault(sig, {
            'title': truncate(f"Record error: {klass}", 120), 'severity': 'S3',
            'evidence': [], 'runs': [], 'places': 0,
            'acceptance': [f"`asf check` reports no `{klass}` error in the record"]})
        d['places'] += 1
        if len(d['evidence']) < RECORD_ERROR_PLACES:
            # the message is quoted: an evidence line that carried the defect bare (a bare
            # decision reference) would make this Bug an instance of its own error class
            d['evidence'].append(f"`{path}:{line}: {message}`")
    for d in out.values():
        if d['places'] > len(d['evidence']):
            d['evidence'].append(f"and {d['places'] - len(d['evidence'])} more")
    return out


#: A rule check that failed this many runs in a row is surfaced once as a factory-side problem.
CHECK_FAILURE_RUNS_TO_SURFACE = 3
LEDGER_NAME = 'rule-check-failures.json'


def _read_ledger(path):
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_ledger(path, data):
    if not path:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def report_check_failures(failures, ledger, now_iso, out=print):
    """A rule check that timed out or crashed is a *check failure*, never a product Bug: one
    ``rule check timed out: R-nnnn`` line per run, and once the same rule's check has failed
    ``CHECK_FAILURE_RUNS_TO_SURFACE`` runs in a row, one ``NEEDS OPERATOR`` line — the factory's
    problem (the check, or its timeout) — printed once until the check runs clean again.
    Mutates and returns ``ledger['checks']``."""
    from asf.rules.rules import failure_line
    prev = ledger.get('checks') or {}
    checks = {}
    for b in failures:
        rid = b.get('rule', '')
        entry = dict(prev.get(rid) or {'since': now_iso, 'runs': 0})
        entry['runs'] = int(entry.get('runs') or 0) + 1
        entry['kind'] = b.get('kind', 'failed')
        entry['line'] = b.get('line', '')
        entry['last'] = now_iso
        checks[rid] = entry
        out(failure_line(b))
        if entry['runs'] >= CHECK_FAILURE_RUNS_TO_SURFACE and not entry.get('surfaced'):
            entry['surfaced'] = now_iso
            out(f"NEEDS OPERATOR: rule check {entry['kind']}: {rid} — {entry['runs']} runs in a row "
                f"since {entry['since']} ({entry['line']}); a factory-side problem with the check, "
                f"not a product Bug")
    ledger['checks'] = checks
    return checks


def usable_bug_epic(canonical, epic_id):
    """``(epic_id, None)`` when the Epic exists and is open, else ``(None, why)``: a filed Bug
    is never parented under a removed, closed or missing Epic."""
    if not epic_id:
        return None, None
    rec = canonical.get(epic_id)
    if rec is None:
        return None, f"default_bug_epic {epic_id} is not in the record"
    typed, machine = frontmatter.split_machine(rec['meta'])
    if typed.get('removed'):
        return None, f"default_bug_epic {epic_id} is removed"
    if machine.get('state') == 'Closed':
        return None, f"default_bug_epic {epic_id} is closed"
    return epic_id, None


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
        'signature': sig, 'count': 1, 'last_filed': date,
        # B-0089: an S1/S2 is decided by its severity — BUG → FIX must not wait for the daily groom
        'decided': info['severity'] in ('S1', 'S2'),
        'parent': default_bug_epic,
    }
    if info.get('places'):
        typed['places'] = info['places']
    if info['runs']:
        typed['links'] = {'runs': sorted(info['runs'])}
    # B-0101: open with what is wrong, then the evidence — never a bare list of lines
    body = info['title'] + '\n\nEvidence:\n' + '\n'.join(f"- {l}" for l in info['evidence'])
    new_id = mint_id(root, canonical, 'bug')
    write_new_item(root, canonical, 'bug', new_id, typed, body, date, 'file-bugs',
                    acceptance=info.get('acceptance') or [f"`{sig}` is not seen again"],
                    shape=('signature', 'bug'))
    return 'filed'


#: An invariant refusal's signature: one Bug per ``(invariant, path)`` (R9).
INVARIANT_SIG = 'invariant {invariant}: {path}'


def invariant_signatures(findings):
    """One signature per ``(invariant, path)`` of the record findings a staged writer was refused
    on (:func:`asf.record.stage.drain`); every finding on it is an evidence line."""
    out = {}
    for f in findings or ():
        for path in (f.paths or (f.subject,)):
            sig = INVARIANT_SIG.format(invariant=f.invariant, path=path)
            d = out.setdefault(sig, {
                'title': truncate(f"Invariant {f.invariant} refused a write to {path}", 120),
                'severity': 'S3', 'evidence': [], 'runs': [],
                'acceptance': [f"no writer is refused on {f.invariant} at `{path}` for "
                               f"{CI_REFUSAL_WINDOW_H}h"]})
            line = f"{f.subject}: {f.message}"
            if line not in d['evidence']:
                d['evidence'].append(line)
    return out


def file_invariant_bugs(root, findings, level='auto', default_bug_epic=None, out=print):
    """File (or bump, once a day) one Bug per ``(invariant, path)`` the record step refused —
    the write was put back and the rest committed; the Bug says what was refused and why.
    Returns ``{signature: outcome}``; under a ``file_bug`` level other than ``auto`` nothing is
    written and each signature is printed as held."""
    signatures = invariant_signatures(findings)
    if not signatures:
        return {}
    if level != 'auto':
        prefix = 'NEEDS OPERATOR: ' if level == 'human-now' else ''
        for sig in sorted(signatures):
            out(f'{prefix}held file_bug on {sig} — widen approvals: file_bug in products/<p>.yaml')
        return {sig: 'held' for sig in signatures}
    by_id, _errors = load_items(root)
    canonical, _dupes = canonicalize(by_id)
    epic, _why = usable_bug_epic(canonical, default_bug_epic)
    outcomes = {}
    for sig in sorted(signatures):
        outcomes[sig] = _file_or_bump_bug(root, canonical, sig, signatures[sig], today(),
                                          default_bug_epic=epic)
        if outcomes[sig] == 'filed':
            by_id, _errors = load_items(root)
            canonical, _dupes = canonicalize(by_id)
    if any(o in ('filed', 'bumped') for o in outcomes.values()):
        do_index(root)
    out(f"file-bugs: invariants — {sum(o == 'filed' for o in outcomes.values())} filed, "
        f"{sum(o == 'bumped' for o in outcomes.values())} bumped")
    return outcomes


def _product(args):
    from asf import env
    try:
        return env.load_product(getattr(args, 'product', None))
    except (env.ConfigError, OSError):
        return None


def _conventions(args):
    """The product's conventions, or the defaults when there is no product config to read
    (a test, or `asf file-bugs` run against a checkout on its own)."""
    conv = getattr(args, 'conventions', None)
    if conv is not None:
        return conv
    product = _product(args)
    return product.conventions if product is not None else DEFAULTS


def ledger_path(product=None, state_dir=None):
    """Where the run-over-run check-failure ledger lives: the operator's state dir for the
    product (factory-side, never the product's record). None with no product to name it."""
    if state_dir:
        return os.path.join(state_dir, LEDGER_NAME)
    if product is None:
        return None
    from asf import env
    return os.path.join(env.state_dir(product), LEDGER_NAME)


def _flaky_pass(args, root, canonical, conv, now, level, default_bug_epic):
    """The flaky-e2e pass (:mod:`asf.tick.flaky`): only for a named product whose CI is read —
    a caller that hands its own ``conventions`` (a test, a checkout on its own) has no CI host,
    unless it hands a ``flaky_source`` too. Never fails the rest of file-bugs."""
    from asf.tick import flaky
    source = getattr(args, 'flaky_source', None)
    product = None
    if source is None:
        if getattr(args, 'conventions', None) is not None:
            return {}
        product = _product(args)
        if product is None or not getattr(product, 'repo_slug', None):
            return {}
        ci = product.ci if isinstance(product.ci, dict) else {'provider': product.ci}
        if str(ci.get('provider') or '').strip().lower() == 'none':
            return {}

    def reload():
        by_id, _errors = load_items(root)
        return canonicalize(by_id)[0]
    try:
        return flaky.run_pass(root, canonical, product, conv, now, level=level,
                              default_bug_epic=default_bug_epic, source=source,
                              path=getattr(args, 'flaky_state', None), reload=reload)
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"file-bugs: flaky pass failed: {e}", file=sys.stderr)
        return {}


def cmd_file_bugs(args, root):
    by_id, parse_errors = load_items(root)
    if parse_errors:
        for f, line, why in parse_errors:
            print(f"{f}:{line}: {why}", file=sys.stderr)
        return 1
    # the rule check reads index.json: a card removed, moved or deleted since the last index must
    # not still run its check and file a Bug, so the index is rebuilt from the cards first
    do_index(root)
    by_id, _errors = load_items(root)
    canonical, _dupes = canonicalize(by_id)
    now = datetime.datetime.now(datetime.timezone.utc)
    date = today()

    conv = _conventions(args)
    default_bug_epic = (getattr(args, 'default_bug_epic', None)
                        or os.environ.get('ASF_DEFAULT_BUG_EPIC')
                        or conv.default_bug_epic)

    ledger_file = ledger_path(None if getattr(args, 'state_dir', None) else _product(args),
                              getattr(args, 'state_dir', None))
    ledger = _read_ledger(ledger_file)

    epic, why = usable_bug_epic(canonical, default_bug_epic)
    if why:
        if ledger.get('unusable_bug_epic') != why:
            print(f"file-bugs: {why} — Bugs are filed with no parent until "
                  f"conventions.default_bug_epic names an open Epic")
        ledger['unusable_bug_epic'] = why
    else:
        ledger.pop('unusable_bug_epic', None)
    default_bug_epic = epic

    rule_data = rule_check_results(root)
    signatures = {}
    signatures.update(ci_signatures(root, now, conv))
    signatures.update(refusal_signatures(root, now))
    signatures.update(rule_violation_signatures(root, rule_data))
    signatures.update(record_error_signatures(root, canonical=canonical))
    if rule_data is not None:
        report_check_failures(rule_data.get('broken') or [], ledger,
                              now.strftime('%Y-%m-%dT%H:%M:%SZ'))
    _write_ledger(ledger_file, ledger)

    level = getattr(args, 'file_bug_level', 'auto')
    flaky_out = _flaky_pass(args, root, canonical, conv, now, level, default_bug_epic)
    if level != 'auto':
        prefix = 'NEEDS OPERATOR: ' if level == 'human-now' else ''
        for sig in sorted(signatures):
            print(f'{prefix}held file_bug on {sig} — widen approvals: file_bug in products/<p>.yaml')
        return 0
    if any(o == 'filed' for o in flaky_out.values()):
        by_id, _errors = load_items(root)
        canonical, _dupes = canonicalize(by_id)

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

    if filed or bumped or any(o in ('filed', 'updated') for o in flaky_out.values()):
        do_index(root)
    print(f"file-bugs: {filed} filed, {bumped} bumped, {skipped} unchanged")
    return 0
