"""asf.tick.flaky — one counted Bug per flaky e2e test (part of ``asf file-bugs``).

A product whose CI retries its end-to-end tests passes a run a flaky test only survived on a
retry. The run is green; the flake is still there, printed in the reporter's summary. This pass
reads the logs of the completed runs of ``conventions.ci_workflow`` (trunk and PRs) it has not
read yet, finds each flaky test in the summary blocks (:func:`parse_flaky`), keeps a count per
test in ``state/<p>/flaky.json``, and files one Bug per test — once. A later flake of the same
test updates that card's count line; it never files a second card.

The summary block (Playwright's ``list`` reporter) is::

      2 flaky
        [chromium] › e2e/login.spec.ts:12:5 › login › remembers the user
        [firefox] › e2e/cart.spec.ts:40:3 › adds an item

A log may hold several blocks (a job that loops over spec files prints one per loop).

A test flaky in :data:`S1_RUNS` runs within :data:`S1_WINDOW_DAYS` days, or on the trunk
:data:`S1_TRUNK_RUNS` times, is S1: it is no longer an occasional nuisance but a stall waiting
to happen.
"""
import datetime
import json
import os
import re
import subprocess

from asf.record import frontmatter
from asf.record.core import today
from asf.record.ids import mint_id, write_new_item
from asf.tick.stale import parse_iso

STATE_NAME = 'flaky.json'
S1_RUNS = 3
S1_WINDOW_DAYS = 7
S1_TRUNK_RUNS = 2
#: How far back the pass lists runs; a run already read (``seen``) is never read twice.
LOOKBACK_DAYS = 7
#: Runs whose logs one pass reads at most, oldest first — the rest wait for the next pass.
MAX_RUNS_PER_PASS = 30
#: The run links a card body lists (newest first); every run id stays in ``links.runs``.
BODY_RUNS = 10
SIG_PREFIX = 'flaky e2e: '
TITLE_MAX = 120
GH_TIMEOUT_S = 120

_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07')
#: ``gh run view --log`` prefixes ``<job>\t<step>\t``; a job log prefixes an ISO timestamp.
_GH_PREFIX_RE = re.compile(r'^[^\t]*\t[^\t]*\t')
_TS_PREFIX_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ?')
_FLAKY_HEAD_RE = re.compile(r'^\s*(\d+) flaky\s*$')
_TEST_RE = re.compile(r'^\s*(?:\[(?P<project>[^\]]*)\]\s*[›>]\s*)?'
                      r'(?P<file>[^\s:][^:]*?):(?P<line>\d+):(?P<col>\d+)\s*[›>]\s*(?P<title>.+?)\s*$')


def _clean(line):
    line = _GH_PREFIX_RE.sub('', line.rstrip('\r\n'), count=1)
    line = _TS_PREFIX_RE.sub('', line, count=1)
    return _ANSI_RE.sub('', line).rstrip('\r')


def parse_flaky(text):
    """The flaky tests a CI log names: ``[{project, file, line, col, title}]``, one per test line
    in every ``N flaky`` summary block, in log order. A block ends at a blank or non-test line.
    ANSI colour codes and the CI host's line prefixes are stripped. Pure."""
    out = []
    in_block = False
    for raw in (text or '').splitlines():
        line = _clean(raw)
        if _FLAKY_HEAD_RE.match(line):
            in_block = True
            continue
        if not in_block:
            continue
        m = _TEST_RE.match(line) if line.strip() else None
        if not m:
            in_block = False
            continue
        out.append({'project': m.group('project') or '', 'file': m.group('file').strip(),
                    'line': int(m.group('line')), 'col': int(m.group('col')),
                    'title': m.group('title')})
    return out


def test_key(t):
    """The identity of a flaky test: ``<file>:<line> › <title>`` (the project is not part of it —
    the same test flaky in two browsers is one Bug)."""
    return f"{t['file']}:{t['line']} › {t['title']}"


def bug_title(t):
    loc = f" ({t['file']}:{t['line']})"
    title = t['title']
    room = TITLE_MAX - len('Flaky e2e: ') - len(loc)
    if len(title) > room:
        title = title[:max(room - 1, 10)].rstrip() + '…'
    return f"Flaky e2e: {title}{loc}"


# ------------------------------------------------------------------ state --

def read_state(path):
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault('tests', {})
    data.setdefault('seen', {})
    return data


def write_state(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def record_run(state, run, flaky):
    """Count one run's flaky tests into ``state``. ``run``: ``{id, ts, branch, trunk, url}``;
    ``flaky``: ``[(test, runner, runner_class)]``. A test counts once per run, however many jobs
    or projects it flaked in. Returns the keys touched."""
    touched = set()
    for t, runner, klass in flaky:
        key = test_key(t)
        e = state['tests'].setdefault(key, {
            'file': t['file'], 'line': t['line'], 'title': t['title'],
            'first_seen': run['ts'], 'last_seen': run['ts'], 'count': 0, 'runs': []})
        entry = next((r for r in e['runs'] if r['run'] == run['id']), None)
        if entry is None:
            entry = {'run': run['id'], 'ts': run['ts'], 'branch': run.get('branch') or '',
                     'trunk': bool(run.get('trunk')), 'url': run.get('url') or '', 'runners': []}
            e['runs'].append(entry)
            e['count'] += 1
        who = {'name': runner or '', 'class': klass or ''}
        if who not in entry['runners']:
            entry['runners'].append(who)
        if run['ts'] < e['first_seen']:
            e['first_seen'] = run['ts']
        if run['ts'] > e['last_seen']:
            e['last_seen'] = run['ts']
        touched.add(key)
    return touched


def severity(entry, now):
    cutoff = now - datetime.timedelta(days=S1_WINDOW_DAYS)
    recent = [r for r in entry['runs'] if (parse_iso(r['ts']) or cutoff) >= cutoff]
    trunk = [r for r in entry['runs'] if r.get('trunk')]
    return 'S1' if len(recent) >= S1_RUNS or len(trunk) >= S1_TRUNK_RUNS else 'S3'


# ------------------------------------------------------------------- card --

def _runners_line(entry):
    seen = []
    for r in entry['runs']:
        for w in r.get('runners') or ():
            label = w['name'] + (f" ({w['class']})" if w.get('class') else '')
            if label and label not in seen:
                seen.append(label)
    return 'Runners: ' + (', '.join(seen) or 'unknown')


def _count_line(entry):
    trunk = sum(1 for r in entry['runs'] if r.get('trunk'))
    return (f"Count: flaky in {entry['count']} run(s) ({trunk} on trunk), first seen "
            f"{entry['first_seen']}, last seen {entry['last_seen']}")


def _runs_line(entry):
    newest = sorted(entry['runs'], key=lambda r: r['ts'], reverse=True)[:BODY_RUNS]
    links = [f"[{r['run']}]({r['url']})" if r.get('url') else str(r['run']) for r in newest]
    more = len(entry['runs']) - len(newest)
    return 'Runs: ' + ', '.join(links) + (f" and {more} more" if more > 0 else '')


def _find_card(canonical, sig):
    for rec in canonical.values():
        typed, _machine = frontmatter.split_machine(rec['meta'])
        if typed.get('type') == 'bug' and typed.get('signature') == sig:
            return rec
    return None


def _replace_line(body, prefix, line):
    pat = re.compile(rf'^{re.escape(prefix)}.*$', re.M)
    return pat.sub(lambda _m: line, body, count=1) if pat.search(body) else body


def file_or_update(root, canonical, key, entry, now, default_bug_epic=None):
    """File the Bug for one flaky test, or bring its card's count up to date. Returns
    ``filed``, ``updated`` or ``unchanged``."""
    sig = SIG_PREFIX + key
    sev = severity(entry, now)
    last_day = today()
    run_ids = sorted({r['run'] for r in entry['runs']})
    rec = _find_card(canonical, sig)
    if rec is None:
        t = {'file': entry['file'], 'line': entry['line'], 'title': entry['title']}
        typed = {'title': bug_title(t), 'severity': sev, 'found_in': 'ci', 'signature': sig,
                 'count': entry['count'], 'last_filed': last_day, 'decided': sev == 'S1',
                 'parent': default_bug_epic, 'links': {'runs': run_ids}}
        body = '\n'.join([
            bug_title(t), '',
            'The CI retry passed this test on a second attempt: the run went green, the flake '
            'is still there.', '',
            _count_line(entry), _runners_line(entry), _runs_line(entry)])
        new_id = mint_id(root, canonical, 'bug')
        write_new_item(root, canonical, 'bug', new_id, typed, body, last_day, 'file-bugs',
                       acceptance=[f"`{key}` is flaky in no CI run for {S1_WINDOW_DAYS} days"],
                       shape=('signature', 'bug'))
        return 'filed'
    typed, _machine = frontmatter.split_machine(rec['meta'])
    updates = {}
    if typed.get('count') != entry['count']:
        updates['count'] = entry['count']
        updates['last_filed'] = last_day   # the day it was seen flaky again (P6's quiet clock)
    old_runs = (typed.get('links') or {}).get('runs') or []
    if sorted(old_runs) != run_ids:
        links = dict(typed.get('links') or {})
        links['runs'] = sorted(set(old_runs) | set(run_ids))
        updates['links'] = links
    if sev == 'S1' and typed.get('severity') != 'S1':
        updates['severity'] = 'S1'
        updates['decided'] = True
    if not updates:
        return 'unchanged'
    frontmatter.write_typed(rec['path'], updates)
    with open(rec['path'], encoding='utf-8') as f:
        meta, body = frontmatter.parse(f.read(), path=rec['relpath'])
    new_body = body
    for prefix, line in (('Count: ', _count_line(entry)), ('Runners: ', _runners_line(entry)),
                         ('Runs: ', _runs_line(entry))):
        new_body = _replace_line(new_body, prefix, line)
    if new_body != body:
        with open(rec['path'], 'w', encoding='utf-8') as f:
            f.write(frontmatter.render(meta, new_body))
    return 'updated'


# -------------------------------------------------------------- CI reader --

def _runner_class(labels):
    from asf import ci_pool
    names = [ci_pool._norm(l if isinstance(l, str) else (l or {}).get('name', ''))
             for l in labels or ()]
    for n in names:
        if n.startswith(ci_pool.CLASS_PREFIX):
            return n[len(ci_pool.CLASS_PREFIX):]
    rest = [n for n in names if n and n not in ci_pool.DEFAULT_LABELS]
    return rest[0] if rest else (names[0] if names else '')


class GitHubRuns:
    """The completed runs of one workflow and their job logs, off ``gh``. A test passes a fake
    with the same three methods."""

    def __init__(self, product, run=None):
        self.product = product
        self.slug = getattr(product, 'repo_slug', None)
        self._run = run or subprocess.run

    def _gh(self, args):
        from asf import ci_pool
        try:
            p = self._run(['gh', *args], capture_output=True, text=True, timeout=GH_TIMEOUT_S,
                          env=ci_pool._gh_env(self.product))
        except (OSError, subprocess.TimeoutExpired):
            return None
        return p.stdout if p.returncode == 0 else None

    def _lines(self, args):
        out = []
        for line in (self._gh(args) or '').splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def runs(self, workflow, since):
        """``[{id, name, branch, ts, url}]``: completed runs created on or after ``since``."""
        rows = self._lines(['api', f'repos/{self.slug}/actions/runs?created=%3E%3D{since}'
                                   f'&status=completed&per_page=100', '--paginate', '--jq',
                            '.workflow_runs[]|{id,name,head_branch,created_at,updated_at,'
                            'html_url}|@json'])
        return [{'id': r['id'], 'name': r.get('name'), 'branch': r.get('head_branch') or '',
                 'ts': r.get('updated_at') or r.get('created_at') or '',
                 'url': r.get('html_url') or ''}
                for r in rows if r.get('name') == workflow]

    def jobs(self, run_id):
        """``[{id, runner_name, labels}]``, or None when the listing failed."""
        text = self._gh(['api', f'repos/{self.slug}/actions/runs/{run_id}/jobs?per_page=100',
                         '--paginate', '--jq', '.jobs[]|select(.conclusion!="skipped")|'
                         '{id,runner_name,labels}|@json'])
        if text is None:
            return None
        return [json.loads(l) for l in text.splitlines() if l.strip().startswith('{')]

    def log(self, job_id):
        return self._gh(['api', f'repos/{self.slug}/actions/jobs/{job_id}/logs']) or ''


def state_path(product):
    from asf import env
    return os.path.join(env.state_dir(product), STATE_NAME)


def collect(state, source, workflow, conv, now, out=print):
    """Read the unread completed runs of ``workflow`` into ``state``. Returns the keys touched."""
    since = (now - datetime.timedelta(days=LOOKBACK_DAYS)).date().isoformat()
    runs = sorted((r for r in source.runs(workflow, since) if str(r['id']) not in state['seen']),
                  key=lambda r: r['ts'])[:MAX_RUNS_PER_PASS]
    touched = set()
    for r in runs:
        jobs = source.jobs(r['id'])
        if jobs is None:
            continue  # read again next pass
        flaky = []
        for j in jobs:
            klass = _runner_class(j.get('labels'))
            for t in parse_flaky(source.log(j['id'])):
                flaky.append((t, j.get('runner_name'), klass))
        run = dict(r, trunk=conv.is_trunk(r['branch']))
        touched |= record_run(state, run, flaky)
        state['seen'][str(r['id'])] = r['ts']
    cutoff = (now - datetime.timedelta(days=LOOKBACK_DAYS + 1)).strftime('%Y-%m-%dT%H:%M:%SZ')
    state['seen'] = {k: v for k, v in state['seen'].items() if v >= cutoff}
    state['last_pass'] = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    if runs:
        out(f"file-bugs: flaky — read {len(runs)} run(s), {len(touched)} flaky test(s)")
    return touched


def file_flaky_bugs(root, canonical, state, now, default_bug_epic=None, reload=None):
    """Reconcile every test in ``state`` with its card: file the missing ones, update the rest.
    Returns ``{signature: outcome}``. ``reload`` re-reads the record after a new card."""
    outcomes = {}
    for key in sorted(state['tests']):
        o = file_or_update(root, canonical, key, state['tests'][key], now, default_bug_epic)
        outcomes[SIG_PREFIX + key] = o
        if o == 'filed' and reload is not None:
            canonical = reload()
    return outcomes


def run_pass(root, canonical, product, conv, now, level='auto', default_bug_epic=None,
             source=None, path=None, reload=None, out=print):
    """The whole pass for ``asf file-bugs``: read new runs, save the counts, file or update the
    cards (or print them held, under a ``file_bug`` level other than ``auto``)."""
    workflow = conv.get('ci_workflow')
    source = source or GitHubRuns(product)
    if not workflow or not getattr(source, 'slug', True):
        return {}
    path = path or state_path(product)
    state = read_state(path)
    collect(state, source, workflow, conv, now, out=out)
    write_state(path, state)
    if level != 'auto':
        prefix = 'NEEDS OPERATOR: ' if level == 'human-now' else ''
        for key in sorted(state['tests']):
            if _find_card(canonical, SIG_PREFIX + key) is None:
                out(f'{prefix}held file_bug on {SIG_PREFIX}{key} — widen approvals: file_bug '
                    f'in products/<p>.yaml')
        return {}
    outcomes = file_flaky_bugs(root, canonical, state, now, default_bug_epic, reload=reload)
    filed = sum(o == 'filed' for o in outcomes.values())
    updated = sum(o == 'updated' for o in outcomes.values())
    if filed or updated:
        out(f"file-bugs: flaky — {filed} filed, {updated} updated")
    return outcomes
