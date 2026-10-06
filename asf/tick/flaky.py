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
#: How far back the window may ever reach — the floor under ``window_from`` (D4).
LOOKBACK_DAYS = 7
#: The steady-state window: longer than any single run, so a run created before the last pass and
#: completed after it is still listed (D5, D6).
SINCE_OVERLAP_HOURS = 24
#: A listing this long has hit the API's result cap: runs older than it are unreachable (D3).
TRUNCATED_ROWS = 1000
#: Runs whose logs one pass reads at most, oldest first — the rest wait for the next pass.
MAX_RUNS_PER_PASS = 30
#: The run links a card body lists (newest first); every run id stays in ``links.runs``.
BODY_RUNS = 10
#: A flaky test's card title and signature start with this (config ``flaky.title_prefix``). Changing
#: it starts new signatures: a card filed under the old prefix is no longer matched.
SIG_PREFIX = 'flaky e2e: '
TITLE_MAX = 120
GH_TIMEOUT_S = 120


def sig_prefix():
    """Config ``flaky.title_prefix``, else :data:`SIG_PREFIX`."""
    from asf import config_keys
    return config_keys.value('flaky.title_prefix', SIG_PREFIX)

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
    sig = sig_prefix() + key
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

def _runner_class(labels, ignore=()):
    """The class a job's labels name; ``ignore``: labels that are no class (``ci.reserve``'s
    PR-only label, :func:`asf.ci_pool.reserve_labels`)."""
    from asf import ci_pool
    names = [ci_pool._norm(l if isinstance(l, str) else (l or {}).get('name', ''))
             for l in labels or ()]
    names = [n for n in names if n not in ignore]
    for n in names:
        if n.startswith(ci_pool.CLASS_PREFIX):
            return n[len(ci_pool.CLASS_PREFIX):]
    rest = [n for n in names if n and n not in ci_pool.DEFAULT_LABELS]
    return rest[0] if rest else (names[0] if names else '')


#: `GitHubRuns.workflow_id`'s answer when the workflow listing did not read (Unknown)
UNREAD = object()
#: `GitHubRuns.workflow_id` has not yet resolved this instance's selector (`None` is itself a
#: valid, cacheable outcome — the selector matched nothing — so it cannot double as "unresolved").
_UNRESOLVED = object()


class GitHubRuns:
    """The completed runs of one workflow and their job logs, off ``gh`` through
    :func:`asf.github.gh` (the rate-limit latch, the escape-sequence retry, the timeout). A read
    that does not answer is ``None`` — Unknown — from every method, never an empty listing or an
    empty log: the pass then leaves the window where it was and the run unread. A test passes a
    fake with the same three methods."""

    def __init__(self, product, run=None):
        self.product = product
        self.slug = getattr(product, 'repo_slug', None)
        self._run = run or subprocess.run
        self._workflow_id = _UNRESOLVED
        self.workflow_names = []

    def _gh(self, args):
        """``gh <args>``'s stdout, or None when Unknown."""
        from asf import ci_pool, connectors, gh_limit
        if self._run is subprocess.run and gh_limit.low(self.product):
            return None  # a history read, never urgent: unreadable under the reserve
        r = connectors.ci().call(args, timeout=GH_TIMEOUT_S, run=self._run,
                      env=ci_pool._gh_env(self.product))
        return r.data if r.ok else None

    def _lines(self, args):
        """The JSON line of each object ``gh --jq … @json`` printed, or None when Unknown."""
        text = self._gh(args)
        if text is None:
            return None
        out = []
        for line in text.splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    @staticmethod
    def _row(r):
        return {'id': r['id'], 'name': r.get('name'), 'branch': r.get('head_branch') or '',
                'ts': r.get('updated_at') or r.get('created_at') or '',
                'created': r.get('created_at') or '', 'url': r.get('html_url') or ''}

    def workflow_id(self, selector):
        """The numeric id of the workflow ``selector`` names — its own id, its display ``name``,
        or its file name (``conventions.ci_workflow`` may hold any of the three, P2) — or None
        with :attr:`workflow_names` set to the names the listing held, for the caller's one line
        (D2). Resolved once per instance (D3); a listing that did not read resolves nothing — it
        returns :data:`UNREAD` and is asked again."""
        if self._workflow_id is _UNRESOLVED:
            rows = self._lines(['api', f'repos/{self.slug}/actions/workflows?per_page=100',
                                '--paginate', '--jq', '.workflows[]|{id,name,path}|@json'])
            if rows is None:
                return UNREAD
            self.workflow_names = [r.get('name') or '' for r in rows]
            match = next((r for r in rows if str(r.get('id')) == str(selector)
                         or r.get('name') == selector
                         or os.path.basename(r.get('path') or '') == selector), None)
            self._workflow_id = match['id'] if match else None
        return self._workflow_id

    def runs(self, workflow, since):
        """``[{id, name, branch, ts, created, url}]``: the completed runs of ``workflow`` created
        on or after ``since`` (an ISO timestamp). One workflow's runs, asked of the workflow (D1)
        — ``repos/{slug}/actions/workflows/{id}/runs`` — so no display-name filter stands between
        the listing and the pass. When the selector resolves to nothing the listing falls back to
        every workflow's runs with the old filter, unchanged (D2). None when a listing did not
        read (Unknown)."""
        wf_id = self.workflow_id(workflow)
        if wf_id is UNREAD:
            return None
        if wf_id is None:
            print(f"file-bugs: flaky — ci_workflow {workflow!r} matches none of: "
                  f"{', '.join(self.workflow_names) or '(none)'}")
            rows = self._lines(['api', f'repos/{self.slug}/actions/runs?created=%3E%3D{since}'
                                       f'&status=completed&per_page=100', '--paginate', '--jq',
                                '.workflow_runs[]|{id,name,head_branch,created_at,updated_at,'
                                'html_url}|@json'])
            return None if rows is None else [self._row(r) for r in rows
                                              if r.get('name') == workflow]
        rows = self._lines(['api', f'repos/{self.slug}/actions/workflows/{wf_id}/runs?'
                                   f'created=%3E%3D{since}&status=completed&per_page=100',
                            '--paginate', '--jq',
                            '.workflow_runs[]|{id,name,head_branch,created_at,updated_at,'
                            'html_url}|@json'])
        return None if rows is None else [self._row(r) for r in rows]

    def jobs(self, run_id):
        """``[{id, runner_name, labels}]``, or None when the listing failed."""
        text = self._gh(['api', f'repos/{self.slug}/actions/runs/{run_id}/jobs?per_page=100',
                         '--paginate', '--jq', '.jobs[]|select(.conclusion!="skipped")|'
                         '{id,runner_name,labels}|@json'])
        if text is None:
            return None
        return [json.loads(l) for l in text.splitlines() if l.strip().startswith('{')]

    def log(self, job_id):
        """The job's log, or None when it did not read (Unknown — never "no flaky test")."""
        return self._gh(['api', f'repos/{self.slug}/actions/jobs/{job_id}/logs'])


def state_path(product):
    from asf import env
    return os.path.join(env.state_dir(product), STATE_NAME)


def window_since(state, now):
    """The pass's listing floor (D4): ``state['window_from']`` when it is inside the lookback
    window; the full ``LOOKBACK_DAYS`` floor otherwise — including when there is no
    ``window_from`` at all, which is a state file written before this card and today's exact
    behaviour."""
    floor = now - datetime.timedelta(days=LOOKBACK_DAYS)
    since = parse_iso(state.get('window_from'))
    if since is None or since < floor:
        since = floor
    return since.strftime('%Y-%m-%dT%H:%M:%SZ')


def next_window(unread, now):
    """Where the next pass should start listing from (D4): one second before the oldest unread
    run's ``created_at`` — never its ``ts`` (D5), so a long run is not excluded by its own
    completion — or ``now - SINCE_OVERLAP_HOURS`` when the pass left nothing unread (D6); floored
    at ``now - LOOKBACK_DAYS`` either way. ``unread``: a run's own dict lacks ``created`` (a fake
    source that predates this card), its ``ts`` stands in — losing D5's precision for that source,
    never a ``KeyError``."""
    floor = now - datetime.timedelta(days=LOOKBACK_DAYS)
    if unread:
        oldest = min(r.get('created') or r['ts'] for r in unread)
        since = (parse_iso(oldest) or floor) - datetime.timedelta(seconds=1)
    else:
        since = now - datetime.timedelta(hours=SINCE_OVERLAP_HOURS)
    return max(since, floor).strftime('%Y-%m-%dT%H:%M:%SZ')


def collect(state, source, workflow, conv, now, out=print):
    """Read the unread completed runs of ``workflow`` into ``state``. Returns the keys touched."""
    since = window_since(state, now)
    listed = source.runs(workflow, since)
    if listed is None:
        out(f"file-bugs: flaky — the runs of {workflow} since {since} unreadable: the window "
            f"stays, read again next pass")
        return set()
    if len(listed) >= TRUNCATED_ROWS:
        out(f"file-bugs: flaky — {len(listed)} runs listed since {since}: the window is "
            f"truncated, runs older than it cannot be read")
    unseen = sorted((r for r in listed if str(r['id']) not in state['seen']),
                    key=lambda r: r['ts'])
    runs, deferred = unseen[:MAX_RUNS_PER_PASS], unseen[MAX_RUNS_PER_PASS:]
    touched = set()
    failed = []
    from asf import ci_pool
    product = getattr(source, 'product', None)
    ignore = ci_pool.reserve_labels(product) if product is not None else set()
    for r in runs:
        jobs = source.jobs(r['id'])
        if jobs is None:
            failed.append(r)
            continue  # read again next pass
        logs = [(j, source.log(j['id'])) for j in jobs]
        if any(log is None for _j, log in logs):
            failed.append(r)
            continue  # a log did not read: never "no flaky test" — read again next pass
        flaky = []
        for j, log in logs:
            klass = _runner_class(j.get('labels'), ignore)
            for t in parse_flaky(log):
                flaky.append((t, j.get('runner_name'), klass))
        run = dict(r, trunk=conv.is_trunk(r['branch']))
        touched |= record_run(state, run, flaky)
        state['seen'][str(r['id'])] = r['ts']
    cutoff = (now - datetime.timedelta(days=LOOKBACK_DAYS + 1)).strftime('%Y-%m-%dT%H:%M:%SZ')
    state['seen'] = {k: v for k, v in state['seen'].items() if v >= cutoff}
    state['last_pass'] = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    state['window_from'] = next_window(deferred + failed, now)
    if runs:
        out(f"file-bugs: flaky — read {len(runs)} run(s), {len(touched)} flaky test(s)")
    return touched


def file_flaky_bugs(root, canonical, state, now, default_bug_epic=None, reload=None):
    """Reconcile every test in ``state`` with its card: file the missing ones, update the rest.
    Returns ``{signature: outcome}``. ``reload`` re-reads the record after a new card."""
    outcomes = {}
    for key in sorted(state['tests']):
        o = file_or_update(root, canonical, key, state['tests'][key], now, default_bug_epic)
        outcomes[sig_prefix() + key] = o
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
            if _find_card(canonical, sig_prefix() + key) is None:
                out(f'{prefix}held file_bug on {sig_prefix()}{key} — widen approvals: file_bug '
                    f'in products/<p>.yaml')
        return {}
    outcomes = file_flaky_bugs(root, canonical, state, now, default_bug_epic, reload=reload)
    filed = sum(o == 'filed' for o in outcomes.values())
    updated = sum(o == 'updated' for o in outcomes.values())
    if filed or updated:
        out(f"file-bugs: flaky — {filed} filed, {updated} updated")
    return outcomes
