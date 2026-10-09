"""asf.upgrade — ``asf upgrade``: reinstall the package at the trunk's head, then the schema check
for every product, one table: product · record schema · package · action.

The install is pinned (the install script runs ``pipx install --force git+<url>@<sha>``), so
``pipx upgrade`` reinstalls the same pin and changes nothing. The upgrade reinstalls at a named
commit instead: the head the tick's drift check read, or ``main``'s head for a manual run. It is
deferred while another tick runs (a reinstall under a running tick tore it: ImportError
mid-tick), refused on a head whose remote CI is red, verified by the new install's own commit,
and followed by reloading any product clock that is on disk but not loaded.

A deferral alone never finds a gap: two products whose ticks run for minutes, launched every few
minutes, always overlap. So a tick's deferred upgrade writes the *pending* marker
(``state/<product>/upgrade-pending.json``: the sha, when, and the owning product) for every
product whose install it replaces — every unpinned one (:func:`marked_products`); a pinned
product runs its own venv and is never parked by it. Every other such tick reads its own marker
at its start and exits without a step while it is fresh (:func:`waiting`), running ticks
finish, and the owner's next tick finds the gap and installs, clearing the marker. A marker
older than :data:`PENDING_TTL_S`, or whose sha is already installed, is removed and ignored —
a stuck upgrade never stops the factory.

The marker is written only for a target the install will actually take: a head the install would
refuse — red remote CI, or no readable head — parks nobody (:func:`refuses`), and a marker this
caller holds whose target the moved head has made uninstallable is cleared there and then rather
than left to expire (B-0141). While a marker does hold the ticks, the doctor's ``SCHEDULER``
section and the status ``Cron`` row say so (:func:`held`, :func:`held_label`) instead of reading
``ok`` through a factory that is not ticking.

The floor drains rather than waits for luck. A running asf process counts — the ticks and the
detached background harvest (``python -m asf.tick.step_harvest``) alike, since a reinstall under
either tears it; a process under another ASF home, such as a test suite's ``tick --product
sample`` in a temp dir, is not the factory's and never counts — and while a marker is pending no tick spawns a new background harvest
(:func:`asf.tick.step_harvest.run`), so the processes running out end. The owner's tick polls
for them at its start for up to ``upgrade.drain_wait_s`` (:func:`drain_wait_s`, default
:data:`DEFAULT_DRAIN_WAIT_S`) before it defers; ``asf upgrade --wait`` does the same by hand and
names what it waits on. A marker that outlives :data:`PENDING_TTL_S` says so in a
``NEEDS OPERATOR`` line when it is removed, and no new marker parks the other products for as
long again (``state/upgrade-expired.json``): the parked products resume, whatever the install.

Every upgrade pauses every product's ticks, so the tick's upgrades are batched: after one
installs (``state/upgrade-last.json``), no tick starts another until ``upgrade.min_interval_min``
(``~/.ASF/config.yaml``, default :data:`DEFAULT_MIN_INTERVAL_MIN`) has passed — by then several
commits have landed and one install carries them all. A head any of whose new commits carries
an ``Urgent: yes`` trailer upgrades regardless (:func:`batch_hold`).

A pinned product (:mod:`asf.installs`) moves only by ``asf upgrade --product <p> --to <sha>``
(:func:`move`): its own venv per sha, positive CI evidence, a quiesced switch, ``--rollback``
offline. While any product is pinned, the shared reinstall above refuses.
"""
import glob
import json
import os
import re
import subprocess
import sys
import time

from asf import env, schema
from asf import version as versions
from asf.drift import DEFERRED  # noqa: F401 — the upgrade waits for the next tick (EX_TEMPFAIL)

PACKAGE_NAME = 'asf-factory'
DEFAULT_REPO_URL = 'https://github.com/fmogensen/ASF.git'
#: a tick's command line: ``python -m asf.cli tick …`` (the clocks) or ``…/bin/asf tick …`` —
#: anchored on the interpreter's argv, so a shell whose script merely mentions them does not match
TICK_PATTERN = r'(-m asf\.cli|/asf) tick( |$)|-m asf\.tick\.step_harvest( |$)'
#: a pending marker older than this is stale: removed, reported, and ignored
PENDING_TTL_S = 10 * 60
#: ``upgrade.drain_wait_s`` when the operator config leaves it out
DEFAULT_DRAIN_WAIT_S = 180
#: ``asf upgrade --wait`` with no seconds given
DEFAULT_MANUAL_WAIT_S = 600
#: how often a draining upgrade looks for the other asf processes again
DRAIN_POLL_S = 5
#: the drain's sleep — its own name, so a test replaces it without touching time.sleep
_drain_sleep = time.sleep
#: ``upgrade.min_interval_min`` when the operator config leaves it out
DEFAULT_MIN_INTERVAL_MIN = 30
#: the newest release tag is read from the remote at most this often, for every product at once
RELEASE_POLL_S = 3600
#: ``v<major>.<minor>.<patch>``, optionally ``+<commits past it>`` (asf.cli's release string)
RELEASE_RE = re.compile(r'^v(\d+)\.(\d+)\.(\d+)(?:\+(\d+))?$')


def _v(sha):
    """A commit as the operator reads it: ``0.1.121 (267264dbe)`` (the sha only as a detail)."""
    return versions.pin_label(sha) if sha else 'unknown'


def pending_path(product):
    """``state/<product>/upgrade-pending.json`` — the marker that parks *this* product's ticks.
    There is no global marker: an upgrade marks only the products whose install it replaces
    (:func:`marked_products`), so one product's move never parks a product pinned elsewhere."""
    return os.path.join(env.ASF_HOME, 'state', product, 'upgrade-pending.json')


def read_pending(product):
    try:
        with open(pending_path(product), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get('sha') else None


def expired_path(product):
    return os.path.join(env.ASF_HOME, 'state', product, 'upgrade-expired.json')


def cooling(product, now=None):
    """The epoch time until which no new pending marker parks the other products (a marker
    expired: they resume for as long as they were parked), else ``None``."""
    try:
        with open(expired_path(product), encoding='utf-8') as f:
            at = json.load(f).get('at')
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(at, (int, float)):
        return None
    until = at + tunable('PENDING_TTL_S')
    return until if (now or time.time()) < until else None


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    os.replace(tmp, path)


def write_pending(sha, owner, product, now=None):
    """Mark the upgrade to ``sha`` pending in ``product``'s state, owned by product ``owner``
    (``None``: an operator's ``asf upgrade --wait`` or ``--product`` move, which the product
    waits for). An existing marker keeps its first timestamp (a newer head never extends the
    wait past :data:`PENDING_TTL_S`). Returns ``None`` and writes nothing while an expired marker
    cools down (:func:`cooling`)."""
    if cooling(product, now) is not None:
        return None
    old = read_pending(product)
    at = old.get('at') if old and isinstance(old.get('at'), (int, float)) else None
    data = {'sha': sha, 'owner': owner, 'at': at if at is not None else (now or time.time())}
    if owner is None:
        data['pid'] = os.getpid()  # an operator's wait lives only as long as its process
    _write_json(pending_path(product), data)
    return data


def clear_pending(product):
    try:
        os.remove(pending_path(product))
    except OSError:
        pass


def clear_expired(product):
    try:
        os.remove(expired_path(product))
    except OSError:
        pass


def marked_products(owner):
    """The products a shared-install upgrade marks pending: every product with no pin
    (:func:`asf.installs.read` — they all run the venv it replaces) and the owner. A pinned
    product runs its own venv, which this upgrade does not touch: it is never parked (S-M1)."""
    from asf import installs
    names = [n for n in products() if not installs.pinned(n)]
    if owner and owner not in names:
        names.append(owner)
    return names


def _installed(sha, installed, run=subprocess.run):
    """True when ``sha`` is already in the installed build: an exact (or prefix) match, or an
    ancestor of it in the factory's own repo (a newer install already carries the commit the
    marker names). No repo beside the running package, an unresolvable sha, or any git error
    falls back to the prefix result alone — a git hiccup never keeps a marker stuck pending."""
    if not (installed and sha):
        return False
    if installed.startswith(sha) or sha.startswith(installed):
        return True
    from asf import drift
    root = drift.factory_root()
    if not root:
        return False
    try:
        p = run(['git', '-C', root, 'merge-base', '--is-ancestor', sha, installed],
                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # it exists, owned by someone else
    return True


def pending(product, now=None, installed=None, out=print, run=subprocess.run):
    """The fresh pending marker in ``product``'s state, or ``None``. A stale one (older than
    :data:`PENDING_TTL_S`, its sha already installed, or an operator's wait whose process is
    gone — killed, it never cleared its mark) is removed; one that timed out uninstalled says so
    in a ``NEEDS OPERATOR`` line and starts the cool-down (:func:`cooling`). A move's marker
    (:func:`move`) lives exactly as long as its process: the running build's commit says
    nothing about the product's own venv, and the move clears it itself."""
    data = read_pending(product)
    if data is None:
        return None
    pid = data.get('pid')
    if data.get('owner') is None and isinstance(pid, int) and pid > 0 and not _alive(pid):
        clear_pending(product)
        out(f'upgrade: pending {data["sha"][:7]} dropped — its waiting process {pid} is gone')
        return None
    if data.get('move') and isinstance(pid, int) and pid > 0:
        return data
    if installed is None:
        from asf import drift
        installed = drift.installed_commit()
    at = data.get('at')
    age = (now or time.time()) - at if isinstance(at, (int, float)) else None
    if _installed(data['sha'], installed, run):
        clear_pending(product)
        clear_expired(product)  # the sha is in: whatever cool-down an earlier expiry left is moot
        return None
    if age is None or age > tunable('PENDING_TTL_S') or age < -60:
        clear_pending(product)
        _write_json(expired_path(product), {'sha': data['sha'], 'owner': data.get('owner'),
                                     'at': now or time.time()})
        out(f'NEEDS OPERATOR: upgrade to {data["sha"][:7]} pending {int((age or 0) // 60)} min '
            f'never found a gap — the parked ticks resume; the install is still due '
            f'(asf upgrade --ref {data["sha"][:7]} --wait)')
        return None
    return data


def held(product_name, now=None):
    """The pending marker while it still holds ``product_name``'s ticks, else ``None`` —
    read-only, for the views that report the wait (:func:`asf.doctor.scheduler_rows`,
    :func:`asf.views.status.cron_cell`). ``None`` for the marker's own owner: the owner's ticks
    go on by design (:func:`waiting`), so a row about the owner must never claim it is waiting.

    It drops the same markers :func:`waiting` ignores, minus the one that costs: a dead
    operator's wait, a future ``at``, an expired TTL. It does *not* check whether the sha is
    already installed — that is a ``git merge-base`` (:func:`_installed`) and does not belong in
    a view — so read it as :func:`pending` minus the writes *and* minus that one check.
    :func:`pending` is the one that expires and clears."""
    data = read_pending(product_name)
    if data is None:
        return None
    if data.get('owner') == product_name:
        return None
    pid = data.get('pid')
    if data.get('owner') is None and isinstance(pid, int) and pid > 0 and not _alive(pid):
        return None  # an operator's wait that was killed before it cleared its own mark
    if data.get('move') and isinstance(pid, int) and pid > 0:
        return data  # a move's marker lives as long as the move (:func:`pending`)
    at = data.get('at')
    if not isinstance(at, (int, float)):
        return None
    age = (now or time.time()) - at
    if age > tunable('PENDING_TTL_S') or age < -60:  # a clock step leaves a future `at`; pending() clears it
        return None
    return data


def held_label(data):
    """``waiting on upgrade to <sha> since <time> (owner <product>)`` — what a row says while a
    marker parks the ticks, so no view reads ``ok`` through a factory that is not ticking."""
    return (f'waiting on upgrade to {data["sha"][:7]} since '
            f'{time.strftime("%H:%M", time.localtime(data["at"]))} '
            f'(owner {data.get("owner") or "operator"})')


def waiting(product_name, out=print, now=None, installed=None, run=subprocess.run):
    """True when this tick must not start: an upgrade owned by another product is pending, and
    this tick's running would only keep the gap the upgrade needs from coming. The owner's ticks
    go on — the owner drains and retries the upgrade at its start. A product with a
    merge-queue batch in flight is never held (#25): the batch lands first."""
    data = pending(product_name, now, installed, out=out, run=run)
    if data is None or data.get('owner') == product_name:
        return False
    if in_flight(product_name):
        return False    # drain first (#25): the clocks land the batch in flight, then the gap
    out(f'tick: waiting — upgrade to {data["sha"][:7]} pending')
    return True


def last_path():
    return os.path.join(env.ASF_HOME, 'state', 'upgrade-last.json')


def read_last():
    """``{'sha', 'at'}`` of the last install, or ``None`` when none is recorded."""
    try:
        with open(last_path(), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get('at'), (int, float)) else None


def record_last(sha, now=None):
    _write_json(last_path(), {'sha': sha or '', 'at': now or time.time()})


def _config_number(key, default, cfg=None):
    """``upgrade.<key>`` from the operator config: a non-negative number, else ``default``."""
    if cfg is None:
        try:
            cfg = env.load_config()
        except Exception:  # noqa: BLE001 — a config problem leaves the default in force
            cfg = {}
    block = (cfg or {}).get('upgrade')
    value = block.get(key) if isinstance(block, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        value = default
    return value


def min_interval_s(cfg=None):
    """``upgrade.min_interval_min`` from the operator config, in seconds (default 30 minutes)."""
    return _config_number('min_interval_min', DEFAULT_MIN_INTERVAL_MIN, cfg) * 60


def drain_wait_s(cfg=None):
    """``upgrade.drain_wait_s``: how long the owner's tick waits at its start for the other asf
    processes to end before it defers the install (default :data:`DEFAULT_DRAIN_WAIT_S`)."""
    return _config_number('drain_wait_s', DEFAULT_DRAIN_WAIT_S, cfg)


def urgent(repo, installed, head):
    """True when a commit in ``installed..head`` carries an ``Urgent: yes`` trailer."""
    if not (repo and installed and head):
        return False
    try:
        p = subprocess.run(['git', '-C', repo, 'log', '--format=%(trailers:key=Urgent,valueonly)',
                            f'{installed}..{head}'], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0 and any(ln.strip().lower() == 'yes' for ln in p.stdout.splitlines())


def batch_hold(repo, installed, head, now=None, cfg=None):
    """When the tick's upgrade to ``head`` waits for the batching interval: the epoch time it is
    due, else ``None`` (no install recorded yet, the interval has passed, or the head is urgent)."""
    last = read_last()
    if last is None:
        return None
    due = last['at'] + min_interval_s(cfg)
    if (now or time.time()) >= due or urgent(repo, installed, head):
        return None
    return due


def _git(root, *args, run=subprocess.run):
    r = run(['git', '-C', root, *args], capture_output=True, text=True, timeout=15)
    return r.stdout.strip() if r.returncode == 0 else None


def checkout_off_main(root=None, run=subprocess.run):
    """Why the install is an editable checkout that is NOT on ``main`` at ``origin/main`` — one
    phrase naming the branch and sha — else ``None`` (a pipx install, or a checkout that is clean
    on main at origin/main). The live factory runs from such a checkout: a feature branch or a
    detached head checked out there changes what every tick executes."""
    root = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        top = _git(root, 'rev-parse', '--show-toplevel', run=run)
        if not top or os.path.realpath(top) != os.path.realpath(root):
            return None  # not an editable checkout of its own repo
        sha = _git(root, 'rev-parse', 'HEAD', run=run) or '?'
        branch = _git(root, 'symbolic-ref', '--short', '-q', 'HEAD', run=run)
        origin = _git(root, 'rev-parse', '--verify', '-q', 'origin/main', run=run)
        dirty = _git(root, 'status', '--porcelain', '--untracked-files=no', run=run)
    except (OSError, subprocess.SubprocessError):
        return None
    problems = []
    if branch != 'main':
        problems.append(f'on {branch or "a detached head"}')
    if origin and sha != origin:
        problems.append(f'HEAD {sha[:7]} is not origin/main {origin[:7]}')
    if dirty:
        problems.append('uncommitted changes')
    if not problems:
        return None
    return (f'the editable install at {root} is {"; ".join(problems)} '
            f'(branch {branch or "(detached)"}, sha {sha[:7]})')


def upgrade_command(url, ref):
    return ['pipx', 'install', '--force', f'git+{url}@{ref}']


def _budget(deadline, cap=10):
    """A subprocess's timeout: ``cap`` seconds, or what is left before ``deadline`` (a
    ``time.monotonic`` value) when that is less — never under one second."""
    if deadline is None:
        return cap
    return max(1, min(cap, deadline - time.monotonic()))


def _out(run, cmd, timeout=60):
    """stdout of ``cmd``, or ``None`` when it fails or cannot start."""
    try:
        p = run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 and isinstance(p.stdout, str) else None


def repo_url(run=subprocess.run):
    """The git url the current install came from (its pipx spec), else install.sh's default."""
    text = _out(run, ['pipx', 'list', '--json'])
    try:
        spec = json.loads(text)['venvs'][PACKAGE_NAME]['metadata']['main_package']['package_or_url']
    except (TypeError, ValueError, KeyError):
        spec = ''
    m = re.match(r'git\+(.+?)(@[^@/]+)?$', spec or '')
    return m.group(1) if m else os.environ.get('ASF_REPO_URL', tunable('DEFAULT_REPO_URL'))


def remote_head(url, run=subprocess.run, branch='main'):
    text = _out(run, ['git', 'ls-remote', url, f'refs/heads/{branch}'])
    return (text or '').split('\t')[0].strip() or None


#: a resolved commit is exactly this — 40 hex characters — never looked up again (``--to``'s sha
#: form costs no network round trip)
HEX40 = re.compile(r'[0-9a-fA-F]{40}$')


#: The suffix ``git ls-remote`` puts on the extra line that names the commit an annotated tag
#: points at. An unfiltered listing prints that line unasked; a listing given ref patterns
#: prints it only for a pattern that matches it, so it is asked for by name (F-0303).
PEEL = '^{}'


def resolve_ref(url, ref, run=subprocess.run):
    """``ref`` as a commit, for the CI guard, ``write_pending`` and the marker (PD12): unchanged
    when it already is a 40-hex sha, else the commit ``git ls-remote <url> <ref> <ref>^{}`` names
    — the dereferenced commit on the ``^{}`` line for an annotated tag, else the line's own sha.
    ``None`` when ``ref`` matches nothing on ``url``. The peel is asked for by name: a filtered
    ``ls-remote`` prints it only for a pattern that matches it (F-0303)."""
    if not ref or HEX40.match(ref):
        return ref
    patterns = [ref] if ref.endswith(PEEL) else [ref, f'{ref}{PEEL}']
    text = _out(run, ['git', 'ls-remote', url, *patterns])
    lines = [ln for ln in (text or '').splitlines() if ln.strip()]
    deref = next((ln for ln in lines if ln.endswith(PEEL)), None)
    line = deref or (lines[0] if lines else '')
    return line.split('\t')[0].strip() or None


def other_ticks(run=subprocess.run, me=None):
    """Pids of the asf tick and background harvest processes of this install's ASF home other
    than this one (and its parent). A process under another ASF home — a test suite's
    ``asf.cli tick --product sample`` in a temp dir — is not the factory's and never holds the
    floor (:func:`foreign_homes`)."""
    me = me if me is not None else {os.getpid(), os.getppid()}
    text = _out(run, ['pgrep', '-f', TICK_PATTERN], timeout=10) or ''
    pids = [int(x) for x in text.split() if x.isdigit() and int(x) not in me]
    foreign = foreign_homes(pids, run) if pids else set()
    return [p for p in pids if p not in foreign]


_ENV_HOME = re.compile(r'(?:^|\s)(ASF_HOME|HOME)=(\S+)')


def _real(path):
    return os.path.realpath(os.path.expanduser(path))


def foreign_homes(pids, run=subprocess.run, deadline=None):
    """The pids whose environment names an ASF home other than :data:`asf.env.ASF_HOME` —
    ``ASF_HOME``, else ``HOME``/.ASF, exactly as :mod:`asf.env` resolves it. Read from
    ``ps eww`` (the command followed by its environment); a process whose environment cannot be
    read is not foreign — an unknown process still holds the floor."""
    text = _out(run, ['ps', 'eww', '-o', 'pid=,command=', '-p', ','.join(str(p) for p in pids)],
                timeout=_budget(deadline)) or ''
    ours = _real(env.ASF_HOME)
    foreign = set()
    for ln in text.splitlines():
        parts = ln.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        found = {}
        for name, value in _ENV_HOME.findall(parts[1]):
            found[name] = value  # the environment follows the argv: the last one is the env's
        home = found.get('ASF_HOME') or (os.path.join(found['HOME'], '.ASF')
                                         if found.get('HOME') else None)
        if home and _real(home) != ours:
            foreign.add(int(parts[0]))
    return foreign


def describe(pids, run=subprocess.run, deadline=None):
    """``pid (age) command`` for each pid, for the lines that say what an upgrade waits on."""
    text = _out(run, ['ps', '-o', 'pid=,etime=,command=', '-p', ','.join(str(p) for p in pids)],
                timeout=_budget(deadline)) or ''
    seen = {}
    for ln in text.splitlines():
        parts = ln.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit():
            m = re.search(r'(-m \S+|/asf)( .*)?$', parts[2])
            seen[int(parts[0])] = f'{parts[0]} ({parts[1]}) {(m.group(0) if m else parts[2])[:90]}'
    return [seen.get(p, str(p)) for p in pids]


def drain(others, wait_s, run=subprocess.run, out=print, sleep=time.sleep):
    """Poll every :data:`DRAIN_POLL_S` for up to ``wait_s`` seconds until no other asf process
    runs. Returns the ones still running (empty: the gap came)."""
    if not others or not wait_s or wait_s <= 0:
        return others
    out(f'upgrade: waiting up to {int(wait_s)}s for {len(others)} asf process(es) to end:')
    for line in describe(others, run):
        out(f'  {line}')
    waited, start = 0, time.monotonic()
    while others and waited < wait_s:
        step = min(DRAIN_POLL_S, wait_s - waited)
        sleep(step)
        # the wait counts what the sleeps asked or what the clock says, whichever is more: a
        # poll's pgrep and a sleep that overslept are part of the bound
        waited = max(waited + step, time.monotonic() - start)
        others = other_ticks(run)
    if not others:
        out(f'upgrade: the floor drained after {int(waited)}s')
    return others


def _gh(run, args, timeout=60, json=True):
    """``gh <args>`` through :func:`asf.github.gh` — a rate limit is an Unknown answer here, not
    an exception: the upgrade decides nothing on it, and the latch already keeps the next call
    from spending."""
    from asf import gh_limit, github
    try:
        return github.gh(args, json=json, timeout=timeout, run=run)
    except gh_limit.RateLimited:
        return github.unknown('rate limited')


def ci_state(url, ref, run=subprocess.run):
    """``('red' | 'clear' | 'unknown', detail)`` for the finished remote CI runs on ``ref``:
    ``red`` when one failed, ``clear`` when none did (green, pending, or no run yet), ``unknown``
    when ``gh`` could not answer (a failure, a timeout, a rate limit, unparsable output) — never
    read as clear. A url that is not a GitHub repository has no CI host to ask: ``clear``, the
    check skipped by configuration."""
    m = re.search(r'github\.com[/:]([^/]+/[^/]+?)(\.git)?/?$', url)
    if not m:
        return 'clear', f'{url} is not a GitHub repository'
    r = _gh(run, ['run', 'list', '--repo', m.group(1), '--commit', ref,
                  '--limit', '20', '--json', 'conclusion'], timeout=30)
    if not r.ok:
        return 'unknown', r.reason or 'unknown'
    runs = r.data if r.data is not None else []
    if not isinstance(runs, list):
        return 'unknown', 'bad json'
    if any(isinstance(x, dict) and x.get('conclusion') in ('failure', 'timed_out') for x in runs):
        return 'red', 'a run failed'
    return 'clear', ''


def ci_red(url, ref, run=subprocess.run):
    """True when a finished remote CI run on ``ref`` failed; False when green, pending or no CI
    host to ask; None when ``gh`` could not answer — Unknown, which the upgrade defers on, never
    reads as green (:func:`ci_state`)."""
    state, _detail = ci_state(url, ref, run)
    return None if state == 'unknown' else state == 'red'


# ---- the release channel: which tag is newest, and whether it is past the install -------------
#
# A product that is not the factory's own source has no checkout to compare against — only
# ASF's release tags. The newest one is read from the remote at most once an hour, in one cache
# every product and ``asf status`` share (D4): a tick's own call never shells out on top of it.

def releases_path():
    """``<ASF_HOME>/state/releases.json`` — ``{<url>: {'tag', 'sha', 'at'}}``, one entry per
    remote, shared by every product (D4)."""
    return os.path.join(env.ASF_HOME, 'state', 'releases.json')


def _read_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def latest_release(url, now=None, run=subprocess.run):
    """``(tag, sha)`` of the greatest ``v<major>.<minor>.<patch>`` tag on ``url``, from the cache
    while its entry is younger than :data:`RELEASE_POLL_S`, else ``git ls-remote --tags <url>
    'v*'`` — the peeled ``^{}`` line is the commit an annotated tag points at. Every attempt
    stamps ``at``; a read that fails keeps the entry's previous ``tag``/``sha``. ``(None, None)``
    when nothing was ever read. Never raises."""
    now = now if now is not None else time.time()
    cache = _read_json(releases_path())
    entry = cache.get(url) if isinstance(cache.get(url), dict) else {}
    tag, sha, at = entry.get('tag'), entry.get('sha'), entry.get('at')
    if isinstance(at, (int, float)) and now - at < RELEASE_POLL_S:
        return tag, sha
    text = _out(run, ['git', 'ls-remote', '--tags', url, 'v*'])  # client-exempt: a url, no local
    # repo — asf.gitops's `cwd`-shaped client doesn't fit, exactly as its sibling remote_head/
    # resolve_ref above are already counted raw
    if text:
        plain, peeled = {}, {}
        for line in text.splitlines():
            parts = line.split('\t', 1)
            if len(parts) != 2:
                continue
            commit, ref = parts
            name = ref.split('refs/tags/', 1)[-1]
            if name.endswith('^{}'):
                peeled[name[:-3]] = commit
            else:
                plain[name] = commit
        names = [n for n in set(plain) | set(peeled) if version_tuple(n) is not None]
        if names:
            best = max(names, key=version_tuple)
            tag, sha = best, peeled.get(best) or plain.get(best)
    cache[url] = {'tag': tag, 'sha': sha, 'at': now}
    _write_json(releases_path(), cache)
    return tag, sha


def installed_release():
    """``(tag, sha)`` of the running install: the tag the install script pinned
    (:func:`asf.cli._release` — a git install's ``requested_revision``, else a checkout's
    ``git describe``, else the build stamp) and the commit it was built from
    (:func:`asf.drift.installed_commit`). ``tag`` is ``None`` when the install records no
    release at all."""
    from asf import cli, drift
    tag = cli._release(cli._checkout_root(), cli._direct_url())
    return (f'v{tag}' if tag else None), drift.installed_commit()


def version_tuple(text):
    """``(major, minor, patch, past)`` of a release string, or ``None``. ``v0.1.62`` →
    ``(0, 1, 62, 0)``; ``v0.1.62+7`` → ``(0, 1, 62, 7)`` — seven commits past the tag, and so
    *ahead* of it (D5). A plain tuple, not a string compare and not ``packaging``: ``v0.1.9``
    against ``v0.1.62`` is the string compare's classic wrong answer, and ``+N`` is ASF's own
    "commits past the tag", never a PEP 440 local version."""
    m = RELEASE_RE.match(str(text or '').strip())
    if not m:
        return None
    major, minor, patch, past = m.groups()
    return int(major), int(minor), int(patch), int(past or 0)


def newer(latest, installed):
    """True when release ``latest`` is past release ``installed``. False when either is
    unreadable — an install whose release cannot be read at all is never upgraded on a guess
    (D6): ``__version__`` is never bumped per release, so comparing against it would read as
    BEHIND forever and reinstall on every tick."""
    a, b = version_tuple(latest), version_tuple(installed)
    return a is not None and b is not None and a > b


def mid_landing(product):
    """``[(item, branch)]`` of ``product``'s items pushed and waiting on the lane
    (:func:`asf.workers.lifecycle.occupancy`'s ``landing``), sorted by item — the sessions an
    install must not land under (P8). Any exception — an unreadable registry, an import that
    cannot resolve — returns ``[]``, which **holds** the install: it never clears it, so the two
    readings of an error are opposite at the call site."""
    try:
        from asf.workers import lifecycle, pool
        out = lifecycle.occupancy(pool.sessions_path(product))
        return sorted((item, info['branch']) for item, info in out['landing'].items())
    except Exception:  # noqa: BLE001 — an unreadable ledger holds the install, never clears it
        return []


def _ref_label(ref):
    """``ref`` printed whole when it is a release tag (:data:`RELEASE_RE`), else its first seven
    characters — a tag never prints as a truncated ``v0.1.6``."""
    return ref if RELEASE_RE.match(ref or '') else (ref or '')[:7]


def refuses(url, ref, run=subprocess.run, sha=None):
    """Why :func:`_install` would refuse ``ref`` — one phrase — else ``None``. The pending marker
    parks every other product's ticks, so it is written only for a target the upgrade will
    actually install (B-0141: a red head parked the whole factory for the marker's whole life)
    — not for one whose CI could not be read either. ``sha`` is the commit ``ref`` names when
    the caller already knows it (a release tag): ``ci_state`` is asked about ``sha or ref``,
    never the tag itself (PD5), while the message still names ``ref``."""
    if not ref:
        return f"main's head is unreadable from {url}"
    state, detail = ci_state(url, sha or ref, run)
    if state == 'red':
        return f'remote CI is red at {_ref_label(ref)}'
    if state == 'unknown':
        return f'remote CI is unknown at {_ref_label(ref)} ({detail})'
    return None


_COMMIT_PY = ("import json;from importlib import metadata as m;"
              "t=m.distribution('asf-factory').read_text('direct_url.json') or '{}';"
              "print((json.loads(t).get('vcs_info') or {}).get('commit_id') or '')")


def new_install_commit(run=subprocess.run):
    """The commit the pipx venv now holds — read by that venv's interpreter, since this process
    still has the old package's metadata loaded."""
    venvs = (_out(run, ['pipx', 'environment', '--value', 'PIPX_LOCAL_VENVS']) or '').strip()
    if not venvs:
        return None
    python = os.path.join(venvs, PACKAGE_NAME, 'bin', 'python')
    return (_out(run, [python, '-c', _COMMIT_PY]) or '').strip() or None


def reload_clocks(names, run=subprocess.run, unloaded=()):
    """Report every product clock whose plist is on disk but that launchd has not loaded, and
    bootstrap only those this upgrade run itself unloaded (``unloaded``: labels). A clock the
    operator paused (``asf scheduler pause``) is named with its pause; one booted out by hand
    with no pause record is an unknown state, so it is named with the command that starts it —
    never loaded silently. Loaded jobs are never booted out — one of them may be the tick
    running this upgrade. Returns the lines to print."""
    from asf import scheduler
    try:
        cfg = env.load_config()
        if scheduler.kind(cfg) != 'launchd':
            return []
        prefix = scheduler.label_prefix(cfg)
    except env.ConfigError:
        return []
    from asf.connectors import launchd
    listed = _out(run, launchd.list_argv())
    if listed is None:
        return ['upgrade: launchctl list failed — clocks not checked']
    loaded = set(scheduler.parse_list(listed))
    unloaded = set(unloaded)
    lines = []
    for name in names:
        for path in sorted(glob.glob(os.path.join(scheduler.launch_agents_dir(), f'{prefix}.{name}.*.plist'))):
            label = os.path.basename(path)[:-len('.plist')]
            if label in loaded:
                continue
            record = scheduler.pause_record(label, cfg)
            if record is not None:
                lines.append(f'upgrade: clock {label} {scheduler.pause_text(record)} — left unloaded')
                continue
            if label not in unloaded:
                lines.append(f'upgrade: clock {label} not loaded — '
                             f'{scheduler.resume_hint(label, cfg)} to start it')
                continue
            ok, err = scheduler.bootstrap(path)
            lines.append(f'upgrade: reloaded clock {label}' if ok
                         else f'upgrade: clock {label} is not loaded and bootstrap failed ({err})')
    return lines


def install(ref=None, run=subprocess.run, out=print, owner=None, wait_s=0, sleep=time.sleep,
            sha=None):
    """Reinstall at ``ref`` (default: ``main``'s head). 0 installed and verified, DEFERRED when
    it waits for the next tick, else non-zero.

    While another asf process runs, the upgrade marks itself pending first — a tick's upgrade
    (``owner``: its product), or an operator's waiting one (``wait_s`` and no owner), which every
    product waits for — so no new tick starts and no new harvest spawns, then polls up to
    ``wait_s`` seconds for the running ones to end (:func:`drain`). A tick's upgrade still
    blocked defers and keeps its mark for its next start; any other outcome clears the mark
    this call is responsible for.

    ``ref`` may be a tag as well as a sha (``--to``/``--ref``): resolved to its commit up front
    (PD12) — that commit is what the CI guard, the pending marker and the post-install check see
    — while ``pipx`` still receives the tag as the operator gave it, so ``direct_url.json``
    records it and :func:`asf.cli.version_string` reports the release. ``sha`` is that commit
    when the caller already knows it (the release channel's own cache): the resolve over the
    network is skipped and ``sha`` is what the CI guard, the pending marker and the verification
    see instead, passed down to :func:`refuses` and :func:`_install` unchanged. With ``sha=None``
    every existing caller is byte-for-byte unchanged."""
    ref = versions.tag_of(ref) or ref       # --to 0.1.108 is the tag v0.1.108
    others = other_ticks(run)
    marked = []
    pin = ref
    if ref and not HEX40.match(ref) and sha is None:
        url = repo_url(run)
        resolved = resolve_ref(url, ref, run)
        if resolved is None:
            out(f'upgrade: refused — {ref} does not resolve to a commit on {url}')
            return 2
        ref = resolved
    targets = marked_products(owner) if others and (owner or wait_s) else []
    if targets:
        # drain first (#25): the pending mark is a full hold, so a product with a batch in
        # flight, or whose own move drains, is never marked — the hold would keep the batch from
        # landing and the move would wait on it forever. Its floor reaches the gap by draining.
        draining_now = [n for n in targets if n != owner and (in_flight(n) or draining(n))]
        if draining_now:
            out(f'upgrade: not parking {", ".join(draining_now)} — a batch in flight or a move '
                'drains there; it lands first')
        targets = [n for n in targets if n not in draining_now]
    if targets:
        url = repo_url(run)
        if not owner and not ref:
            ref = remote_head(url, run)  # the operator's marker names what it waits for
        refusal = refuses(url, ref, run, sha=sha)
        if refusal:
            # the install will refuse this target, so no marker may park the other products for
            # it — and one this caller holds for a head that has moved goes now, not at the TTL
            for name in targets:
                wait = read_pending(name)
                if wait is not None and (wait.get('owner') == owner
                                         or wait.get('sha') == (sha or ref)):
                    clear_pending(name)
            out(f'upgrade: no pending mark — {refusal}; the other ticks run')
        elif ref:
            # an operator's wait never replaces a marker a tick already holds
            marked = [n for n in targets if (owner or read_pending(n) is None)
                      and write_pending(sha or ref, owner, n) is not None]
            if marked:
                out(f'upgrade: pending {(sha or ref)[:7]} — other ticks wait until it installs')
            elif any(cooling(n) for n in targets):
                until = time.strftime('%H:%M', time.localtime(
                    max(cooling(n) or 0 for n in targets) or time.time()))
                out(f'upgrade: no pending mark until {until} — an earlier one expired; '
                    'the other ticks run')
    try:
        others = drain(others, wait_s, run, out, sleep)
    except BaseException:
        if not owner:
            _clear(marked)  # an interrupted manual wait never leaves the factory parked
        raise
    if others:
        where = 'the next tick' if owner else 'later'
        out(f'upgrade: deferred to {where} — another asf process is running '
            f'(pid {", ".join(str(p) for p in others)})')
        for line in describe(others, run):
            out(f'  {line}')
        if not owner:
            _clear(marked)
            out('upgrade: NOT installed — rerun with --wait [SECONDS] to wait for them to end')
        return DEFERRED
    sha_kw = {'sha': sha} if sha is not None else {}
    rc = (_install(ref, run, out, pin=pin, **sha_kw) if pin != ref
          else _install(ref, run, out, **sha_kw))
    if rc == 0:
        record_last(sha or ref)
    if owner:
        _clear(n for n in marked_products(owner)
               if (read_pending(n) or {}).get('owner') == owner)
    _clear(marked)
    return rc


def _clear(names):
    for name in names:
        clear_pending(name)


def _install(ref, run, out, pin=None, sha=None):
    """Reinstall at ``ref``. ``pin`` is what ``pipx`` receives on the command line when it
    differs from ``ref`` — the tag :func:`install` resolved ``ref`` from; ``None`` (the default)
    means ``pipx`` receives ``ref`` itself, as every caller but :func:`install`'s ``--to`` does.
    ``sha`` is the commit ``ref`` names when the caller already knows it (a release tag, from
    the cache): :func:`ci_state` and the post-install verification ask about ``sha or ref`` while
    ``pipx`` still installs ``ref`` — the tag, so ``requested_revision`` records it and
    :func:`installed_release` can read it next tick (PD3, PD4, PD5). With ``sha=None`` the
    target is ``ref`` and this is byte-for-byte today's path."""
    url = repo_url(run)
    ref = ref or remote_head(url, run)
    if not ref:
        out(f'NEEDS OPERATOR: cannot read main\'s head from {url}')
        return 2
    target = sha or ref
    state, detail = ci_state(url, target, run)
    if state == 'red':
        out(f'upgrade: skipped — remote CI is red at {_ref_label(ref)}; the next green head installs')
        return DEFERRED
    if state == 'unknown':
        out(f'upgrade: deferred — remote CI is unknown at {_ref_label(ref)} ({detail}); '
            'the next pass reads it again')
        return DEFERRED
    cmd = upgrade_command(url, pin or ref)
    out('upgrade: ' + ' '.join(cmd))
    try:
        rc = run(cmd).returncode
    except OSError:
        out('NEEDS OPERATOR: pipx is not on PATH — install pipx, then rerun the install script')
        return 2
    if rc != 0:
        return rc
    got = new_install_commit(run)
    if not got or not (got.startswith(target) or target.startswith(got)):
        out(f'upgrade: FAILED — the install is at {(got or "unknown")[:7]}, not {_ref_label(ref)}')
        return 1
    out(f'upgrade: installed {_v(target)}')
    for line in reload_clocks(products(), run):
        out(line)
    return 0


# ---- the per-product move: ``asf upgrade --product <p> --to <sha>`` ---------------------------
#
# A pinned product (``state/<p>/install.json``, :mod:`asf.installs`) runs its own venv,
# ``asf-factory-<p>-<sha7>``. A move installs the target venv beside the running one (or finds it
# already on disk — then it is a local switch, no network), requires positive CI evidence on the
# exact sha, and then quiesces the product before it re-points anything. Drain first: the
# drain marker (:func:`draining`) stops new merge-queue cuts (never a launch), while the clocks
# keep running — the tick's background harvest is what lands a batch in flight, and the ci
# queue admits its run — until no batch is in flight. Only then the hold: record every clock's
# pause (no bootout yet — that kills a running tick) and wait until no tick, ``ci queue`` pass or
# background harvest of the product runs and its ``harvest.lock`` is free; a batch cut by a pass
# that was already running lifts the hold again until it lands. After ``--wait-s`` the move
# refuses and resumes exactly the clocks it paused. Then boot the clocks out, record the pin
# with the venv it replaces as ``previous``, render the clocks and the hooks from it, smoke,
# re-render a stale host clock, resume. ``--rollback`` moves back to ``previous`` the same way,
# offline. A move killed outright leaves its pause records behind: they carry its pid, and the
# next move or ``asf scheduler status`` lifts them (:func:`lift_stale_pauses`).

#: ``asf upgrade --product`` waits this long for the product's floor to drain before refusing
DEFAULT_MOVE_WAIT_S = 900
#: what a drain may overrun its ``--wait-s`` by (one last poll, the refusal's resume); a drain
#: marker past its recorded deadline plus this is a hung move's: every reader drops it
DRAIN_MARGIN_S = 60
#: ``upgrade.drain_marker_max_s``: the age past which a drain marker that records no deadline
#: (an older build's) is dropped
DEFAULT_DRAIN_MARKER_MAX_S = DEFAULT_MOVE_WAIT_S + DRAIN_MARGIN_S
#: the product's processes a move waits out: its ticks, its ``ci queue`` passes (the queue clock
#: pushes and cancels runs), its wave clock's own runs (:mod:`asf.tick.wave_clock`: it spawns
#: sessions too) and its background harvest — by interpreter argv, as :data:`TICK_PATTERN`, but
#: also the suffixed ``asf-<p>-<sha7>`` and dispatcher entry points
MOVE_PATTERN = r'(-m asf\.cli|/asf\S*) (tick|ci queue|wave)( |$)|-m asf\.tick\.step_harvest( |$)'
#: the move waits (CI unknown, the floor still busy): nothing changed, try again later
MOVE_DEFERRED = 3
#: a check run conclusion that is a red verdict (anything else not ``success`` is Unknown)
RED_CONCLUSIONS = ('failure', 'timed_out', 'cancelled', 'startup_failure')


#: a pause record's ``reason`` written by a move (:meth:`MoveOps.pause`)
PAUSE_REASON = 'upgrade'


def draining_path(product):
    """``state/<product>/upgrade-draining.json`` — a move's drain marker: no new merge-queue cut
    while the batches in flight land; launches go on (a drain never holds a session). Its own
    file, not the pending marker: a clock on an older build reads the pending marker as a full
    hold, which would stop the very harvest that lands the batch."""
    return os.path.join(env.ASF_HOME, 'state', product, 'upgrade-draining.json')


def _mark_draining(product, sha, now=None, wait_s=None):
    """The drain marker; with ``wait_s`` it records its ``deadline`` (epoch seconds), past which
    (plus :data:`DRAIN_MARGIN_S`) every reader drops it (:func:`draining`)."""
    at = now or time.time()
    data = {'sha': sha, 'pid': os.getpid(), 'at': at}
    if wait_s is not None:
        data['deadline'] = at + max(0, wait_s)
    _write_json(draining_path(product), data)


def drain_marker_max_s(cfg=None):
    """``upgrade.drain_marker_max_s``: the age past which a drain marker with no ``deadline``
    is dropped (default :data:`DEFAULT_DRAIN_MARKER_MAX_S`)."""
    return _config_number('drain_marker_max_s', DEFAULT_DRAIN_MARKER_MAX_S, cfg)


def _drain_expired(data, now=None):
    """True when a drain marker outlived its move: past its ``deadline`` plus
    :data:`DRAIN_MARGIN_S`, or — an older build's, with no deadline — older than
    :func:`drain_marker_max_s` (or with no readable ``at``)."""
    now = now or time.time()
    deadline = data.get('deadline')
    if isinstance(deadline, (int, float)) and not isinstance(deadline, bool):
        return now > deadline + DRAIN_MARGIN_S
    at = data.get('at')
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return True
    return now - at > drain_marker_max_s()


def clear_draining(product):
    try:
        os.remove(draining_path(product))
    except FileNotFoundError:
        pass


def draining(product):
    """The drain marker of a move of ``product`` that is still running, else ``None`` — a
    marker whose process is gone (the move was killed) or that is past its deadline (a hung
    move, :func:`_drain_expired`) is removed."""
    try:
        with open(draining_path(product), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    pid = data.get('pid') if isinstance(data, dict) else None
    if not isinstance(pid, int) or pid <= 0 or not _alive(pid) or _drain_expired(data):
        clear_draining(product)
        return None
    return data


def _move_alive(product):
    """True while a move of ``product`` runs: its hold or drain marker names a live process."""
    data = read_pending(product)
    pid = (data or {}).get('pid')
    if data and data.get('move') and isinstance(pid, int) and pid > 0 and _alive(pid):
        return True
    return draining(product) is not None


def stale_pauses(product_name):
    """The clocks a move paused and never resumed — it was killed (SIGKILL, a closed
    terminal) before its ``finally`` ran: a move's pause whose pid is gone, or an older move's
    pause (no pid) while no move of the product runs. Sorted; an operator's pause never counts."""
    from asf import scheduler
    pauses = scheduler.read_pauses(product_name)
    alive = None
    stale = []
    for clock, rec in pauses.items():
        if rec.get('reason') != PAUSE_REASON:
            continue
        pid = rec.get('pid')
        if isinstance(pid, int) and pid > 0:
            if not _alive(pid):
                stale.append(clock)
            continue
        if alive is None:
            alive = _move_alive(product_name)
        if not alive:
            stale.append(clock)
    return sorted(stale)


def lift_stale_pauses(product_name, resume=None):
    """Resume the clocks a killed move left paused (:func:`stale_pauses`) — the lines say so."""
    clocks = stale_pauses(product_name)
    if not clocks:
        return []
    if resume is None:
        from asf import scheduler
        resume = scheduler.resume
    lines = [f'upgrade: stale pause of {", ".join(clocks)} (a move of {product_name} that '
             'was killed before it resumed them) — lifted']
    return lines + list(resume(product_name, clocks) or ())


def _dead_batch_why(product_name, batch):
    """Why merge-queue batch ``batch`` is dead — a required check concluded red at its exact
    sha (:data:`RED_CONCLUSIONS`: failure, cancelled, timed out, startup failure) — else None.

    Wider than the queue's own judge (:func:`asf.merge_queue.verdict`): its
    :data:`asf.merge_queue.NONVERDICT` gives a cancelled or timed-out check one re-run before it
    cuts the batch again — a retry that needs a tick to run it, and a drain has none left once
    the clocks it would pause are the only ones that still could (#35). It can never land as
    cut, whatever runs it again: nothing here asks for a re-run, only whether this sha already
    answered red.

    Unreadable (no product, no slug, no required names, no runs) is never dead — this narrows
    what a drain counts as in flight; it never decides what the queue's own pass does with the
    same batch."""
    sha = batch.get('sha')
    if not sha:
        return None
    try:
        product = env.load_product(product_name)
    except env.ConfigError:
        return None
    from asf.harvest import lane as lane_mod
    slug = lane_mod.repo_slug(product)
    if not slug:
        return None
    try:
        required, _why = lane_mod.GitHubHost(product).merge_required(
            env.state_dir(product_name), lane_mod.CODE, sha)
    except Exception:  # noqa: BLE001 — unreadable: never assumed dead
        return None
    if not required:
        return None
    from asf import merge_queue
    runs = merge_queue.batch_runs(slug, sha)
    if runs is None:
        return None
    from asf.harvest import deploy
    red = [name for name in required
           if any(r.get('status') == 'completed' and r.get('conclusion') in RED_CONCLUSIONS
                  for r in runs if deploy.job_key(r.get('name')) == name)]
    return f"{', '.join(red)} concluded red at {sha[:9]}" if red else None


def in_flight(product_name, out=None):
    """The refs of ``product_name``'s merge-queue batches in flight, else ``[]`` — a dead batch
    (:func:`_dead_batch_why`) never counts, even while its ref still holds on origin (#35): the
    drain (``out``, its own) logs it ``dead, ignored by the drain`` and moves on; every other
    reader (:func:`waiting`'s #25 check among them) stays silent and just gets the same, narrower
    list."""
    state = os.path.join(env.ASF_HOME, 'state', product_name)
    if not os.path.exists(os.path.join(state, 'merge-queue.json')):
        return []
    from asf import merge_queue
    live = []
    for b in merge_queue.load(state)['batches']:
        why = _dead_batch_why(product_name, b)
        if why:
            if out:
                out(f"upgrade: {b.get('ref')} — dead, ignored by the drain ({why})")
            continue
        live.append(b.get('ref'))
    return live


def product_processes(product_name, run=subprocess.run, me=None, deadline=None):
    """Pids of ``product_name``'s ticks, ``ci queue`` passes and background harvests under this
    ASF home, other than this process (and its parent)."""
    me = me if me is not None else {os.getpid(), os.getppid()}
    text = _out(run, ['pgrep', '-f', MOVE_PATTERN], timeout=_budget(deadline)) or ''
    pids = [int(x) for x in text.split() if x.isdigit() and int(x) not in me]
    if not pids:
        return []
    listing = _out(run, ['ps', '-ww', '-o', 'pid=,command=', '-p',
                         ','.join(str(p) for p in pids)], timeout=_budget(deadline)) or ''
    mine = re.compile(rf'--product[ =]{re.escape(product_name)}( |$)')
    ours = []
    for ln in listing.splitlines():
        parts = ln.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and mine.search(parts[1]):
            ours.append(int(parts[0]))
    foreign = foreign_homes(ours, run, deadline) if ours else set()
    return [p for p in ours if p not in foreign]


def floor_busy(product_name, run=subprocess.run, deadline=None):
    """What keeps ``product_name``'s floor from being quiet — one line each — else ``[]``.
    Every subprocess gets at most what is left before ``deadline`` (``time.monotonic``)."""
    state = os.path.join(env.ASF_HOME, 'state', product_name)
    pids = product_processes(product_name, run, deadline=deadline)
    busy = [f'process {line}' for line in describe(pids, run, deadline)] if pids else []
    if os.path.exists(os.path.join(state, 'harvest.lock')):
        from asf.harvest import harvest
        if harvest.try_lock_held(state):
            busy.append('harvest.lock is held (a gate is running)')
    refs = in_flight(product_name)
    if refs:
        busy.append(f'merge-queue batch in flight ({", ".join(refs)})')
    return busy


def _gh_slug(url):
    m = re.search(r'github\.com[/:]([^/]+/[^/]+?)(\.git)?/?$', url or '')
    return m.group(1) if m else None


def landing_checks_for(slug):
    """The ``conventions.landing_checks`` of the product whose ``repo_slug`` is ``slug`` (the
    factory's own repo, run as a product), else ``[]``."""
    for name in products():
        try:
            product = env.load_product(name)
        except env.ConfigError:
            continue
        if str(product.repo_slug or '').lower() != slug.lower():
            continue
        named = product.conventions.get('landing_checks') \
            if hasattr(product.conventions, 'get') else None
        return [str(named)] if isinstance(named, str) else [str(n) for n in named or ()]
    return []


def ci_verdict(url, sha, run=subprocess.run, checks=None):
    """``('green' | 'red' | 'unknown', detail)`` for the exact ``sha``: green only when every
    landing check of the repo has a ``success`` run (or status) on it. A gh failure, non-JSON,
    a missing or pending check, or no named checks at all is Unknown — never green (S-M2)."""
    slug = _gh_slug(url)
    if not slug:
        return 'unknown', f'{url} is not a GitHub repository'
    names = list(checks) if checks is not None else landing_checks_for(slug)
    if not names:
        return 'unknown', f'no product names landing_checks for {slug}'
    r = _gh(run, ['api', f'repos/{slug}/commits/{sha}/check-runs?per_page=100'])
    runs = r.data.get('check_runs') if r.ok and isinstance(r.data, dict) else None
    if not isinstance(runs, list):
        why = r.reason or 'the answer is not a check-runs object'
        return 'unknown', f'gh could not read the check runs ({why})'
    latest = {}
    for r in runs:
        if isinstance(r, dict) and r.get('name') in names:
            if r['name'] not in latest or (r.get('id') or 0) > (latest[r['name']].get('id') or 0):
                latest[r['name']] = r
    statuses = {}
    if any(n not in latest for n in names):
        sr = _gh(run, ['api', f'repos/{slug}/commits/{sha}/status'])
        try:  # unread statuses leave the missing checks Unknown below — never green
            statuses = {s.get('context'): s.get('state')
                        for s in sr.data.get('statuses') or ()} if sr.ok and sr.data else {}
        except (AttributeError, TypeError):
            statuses = {}
    red, unknown = [], []
    for name in names:
        r = latest.get(name)
        if r is not None:
            if r.get('status') != 'completed':
                unknown.append(f'{name} {r.get("status") or "pending"}')
            elif r.get('conclusion') == 'success':
                continue
            elif r.get('conclusion') in RED_CONCLUSIONS:
                red.append(f'{name} {r.get("conclusion")}')
            else:
                unknown.append(f'{name} {r.get("conclusion") or "no conclusion"}')
        elif statuses.get(name) == 'success':
            continue
        elif statuses.get(name) in ('failure', 'error'):
            red.append(f'{name} {statuses[name]}')
        else:
            unknown.append(f'{name} {statuses.get(name) or "has no run"}')
    if red:
        return 'red', ', '.join(red)
    if unknown:
        return 'unknown', ', '.join(unknown)
    return 'green', f'{", ".join(names)} success'


def resolve_target(to, rec, url, run=subprocess.run):
    """``to`` as a 40-hex commit, or ``None``. A sha this product's record already names
    (current or previous; a prefix of seven or more) resolves offline; then the factory's own
    checkout (``git rev-parse``), then the remote (``git ls-remote``)."""
    if not to:
        return None
    if HEX40.match(to):
        return to.lower()
    known = [s for s in ((rec.sha, rec.previous_sha) if rec else ()) if s]
    if re.fullmatch(r'[0-9a-fA-F]{7,39}', to):
        hit = [s for s in known if s.lower().startswith(to.lower())]
        if hit:
            return hit[0]
    from asf import drift
    root = drift.factory_root()
    if root:
        try:
            got = _git(root, 'rev-parse', '--verify', '-q', f'{to}^{{commit}}', run=run)
        except (OSError, subprocess.SubprocessError):
            got = None
        if got and HEX40.match(got):
            return got
    got = resolve_ref(url, to, run)
    return got if got and HEX40.match(got) else None


def pipx_suffix_command(url, product_name, sha):
    from asf import installs
    return ['pipx', 'install', '--force', f'--suffix={installs.suffix(product_name, sha)}',
            f'git+{url}@{sha}']


class MoveOps:
    """The side effects of a move on the clocks and the hooks — one object, so a test replaces
    them all and no launchd job or settings file is touched."""

    def clock_names(self, product_name):
        """Every clock of the product: declared in its file, or with a plist on disk."""
        from asf import scheduler
        names = []
        try:
            names = [c.name for c in scheduler.clocks(env.load_product(product_name))]
        except (env.ConfigError, scheduler.SchedulerError):
            pass
        try:
            for label in scheduler.product_labels(product_name):
                parts = scheduler.split_label(label)
                if parts and parts[1] not in names:
                    names.append(parts[1])
        except env.ConfigError:
            pass
        return names

    def paused(self, product_name):
        from asf import scheduler
        return set(scheduler.read_pauses(product_name))

    def pause(self, product_name, clocks, by):
        """The pause record only — no bootout: ``launchctl bootout`` kills a running tick, and
        the drain must see it end on its own (:meth:`bootout`, after the drain)."""
        from asf import scheduler
        return scheduler.pause(product_name, clocks, reason=PAUSE_REASON, by=by, bootout=False,
                               pid=os.getpid())

    def bootout(self, product_name, clocks):
        from asf import scheduler
        return scheduler.bootout_clocks(product_name, clocks)

    def install_host(self):
        """Re-render the host clocks when the one on disk is not what a render writes now
        (:func:`asf.scheduler.refresh_host`) — only while ``network.probe`` is on."""
        from asf import scheduler
        try:
            return scheduler.refresh_host()
        except (env.ConfigError, scheduler.SchedulerError, OSError) as e:
            return [f'upgrade: WARNING — the host clocks were not re-rendered ({e})']

    def resume(self, product_name, clocks):
        from asf import scheduler
        return scheduler.resume(product_name, clocks)

    def install_clocks(self, product_name):
        """``asf scheduler install --product <p>`` in this process (rc): the render reads the
        pin from ``install.json``; a paused clock's plist is written, not loaded."""
        import argparse
        from asf import scheduler
        return scheduler.cmd_scheduler(argparse.Namespace(
            scheduler_command='install', product=product_name, clock=None, json=False,
            label=None, reason=None, by=None))

    def install_hooks(self, product_name):
        """``asf hooks install --product <p>`` (rc, message) — as the command runs it: the
        dispatcher refreshed first, and every agent home's ``.local/bin/asf`` linked to it."""
        from asf import dispatch, hooks
        return hooks.install(env.load_product(product_name), dispatcher=dispatch.default_path())

    def verify_hooks(self, product_name):
        """:func:`asf.hooks.verify` — what is wrong with the git hooks just installed: one that
        does not exec the dispatcher does not run the pin this move wrote (F-0283)."""
        from asf import hooks
        try:
            return hooks.verify(env.load_product(product_name))
        except (env.ConfigError, OSError) as e:
            return [f'check did not run ({e})']

    def smoke(self, product_name):
        """``(ok_lines, failures)`` of :func:`asf.scheduler.smoke`: read-only commands under each
        written clock plist's exact environment and interpreter."""
        from asf import scheduler
        return scheduler.smoke(env.load_product(product_name))


def _mark(product_name, sha, now=None):
    """The move's own marker: no tick of the product starts and no background harvest spawns
    while it drains — written whatever an earlier expiry's cool-down says."""
    _write_json(pending_path(product_name), {'sha': sha, 'owner': None, 'pid': os.getpid(),
                                             'move': True, 'at': now or time.time()})


def moving(product_name):
    """The marker of a move of ``product_name`` that is still running, else ``None`` — what a
    clock that does not tick (``asf ci queue --apply``) reads so it starts no pass while the
    move drains."""
    data = held(product_name)
    return data if data and data.get('move') else None


def _actor():
    return os.environ.get('ASF_ACTOR') or os.environ.get('USER') or 'operator'


def move(product_name, to=None, rollback=False, wait_s=DEFAULT_MOVE_WAIT_S, force_ci=False,
         dry_run=False, by=None, run=subprocess.run, out=print, sleep=None, ops=None):
    """Move ``product_name`` to the venv of ``to`` (or back to its ``previous`` with
    ``rollback``). 0 moved (or already there), :data:`MOVE_DEFERRED` when it waits (CI Unknown,
    the floor still busy after ``wait_s``), 2 refused, 1 failed after the install."""
    from asf import installs
    ops = ops or MoveOps()
    sleep = sleep or _drain_sleep
    by = by or _actor()
    rec = installs.read(product_name)
    if rollback:
        if rec is None or rec.previous is None:
            out(f'upgrade: refused — {product_name} has no previous install to roll back to '
                f'({installs.record_path(product_name)})')
            return 2
        sha, venv = rec.previous_sha, rec.previous_venv
        if not installs.usable(venv):
            out(f'NEEDS OPERATOR: the previous venv {venv} is not on disk — no offline rollback; '
                f'move forward with asf upgrade --product {product_name} --to <sha>')
            return 2
        local = True
        out(f'upgrade: rollback of {product_name} to {_v(sha)} ({venv}) — '
            'a local switch, no network')
    else:
        url = repo_url(run)
        sha = resolve_target(to, rec, url, run)
        if sha is None:
            out(f'upgrade: refused — {to} does not resolve to a commit')
            return 2
        known = rec is not None and sha in (rec.sha, rec.previous_sha)
        if known and sha == rec.previous_sha and installs.usable(rec.previous_venv, sha):
            venv = rec.previous_venv
        else:
            venv = installs.venv_dir(product_name, sha, run)
        local = installs.usable(venv, sha)
        if local and known:
            out(f'upgrade: {sha[:7]} is this product\'s own earlier pin, on disk — '
                'a local switch, no CI read')
        else:
            verdict, detail = ci_verdict(url, sha, run)
            if verdict == 'red':
                out(f'upgrade: refused — CI is red at {sha[:7]} ({detail})')
                return 2
            if verdict != 'green' and not force_ci:
                out(f'upgrade deferred — CI unknown ({detail}); '
                    'rerun when it is green, or --force-ci to move without it')
                return MOVE_DEFERRED
            if verdict != 'green':
                out(f'upgrade: WARNING — --force-ci: moving {product_name} to {sha[:7]} '
                    f'with CI unknown ({detail})')
            else:
                out(f'upgrade: CI green at {sha[:7]} ({detail})')
    if rec is not None and rec.sha == sha and os.path.realpath(rec.venv) == os.path.realpath(venv):
        out(f'upgrade: {product_name} already runs {_v(sha)} ({venv}) — nothing to move')
        return 0
    if rollback:
        previous = {'sha': rec.sha, 'venv': rec.venv}
    elif rec is not None:
        previous = {'sha': rec.sha, 'venv': rec.venv}
    else:
        shared = installs.shared_venv(run)
        previous = ({'sha': installs.commit_of(shared), 'venv': shared}
                    if installs.usable(shared) else None)
    steps = []
    if not local:
        steps.append(' '.join(pipx_suffix_command(url, product_name, sha)))
    if not dry_run:
        # a killed move's pauses: lifted here, or they would read as the operator's below and
        # stay paused after this move, too
        for line in lift_stale_pauses(product_name, ops.resume):
            out(line)
    clocks = [c for c in ops.clock_names(product_name) if c not in ops.paused(product_name)]
    steps += [f'drain: no new merge-queue cut; the clocks run until no batch is in '
              f'flight (up to {int(wait_s)}s in all; else refuse, nothing paused)',
              f'pause clocks {", ".join(clocks) or "(none)"} (the record: no new tick starts)',
              f'quiet: no tick / ci queue / harvest of {product_name}, harvest.lock free '
              f'(a batch cut meanwhile: resume until it lands; past the wait resume, nothing '
              f'moved)',
              f'bootout clocks {", ".join(clocks) or "(none)"}',
              f'record {installs.record_path(product_name)}: {sha[:7]} {venv}; previous '
              f'{((previous or {}).get("sha") or "none")[:7]} {(previous or {}).get("venv") or ""}',
              f'asf scheduler install --product {product_name}',
              f'asf hooks install --product {product_name}',
              'smoke: each clock plist\'s interpreter imports asf.cli and loads the product, '
              'and runs the clock\'s own command in its no-op form (tick --manifest); '
              'gh auth status, git/claude/toolchain --version under the plist env '
              '(any failure: roll back)',
              'host clocks: asf scheduler install --host when the one on disk is stale '
              '(network.probe on)',
              f'resume clocks {", ".join(clocks) or "(none)"}']
    if dry_run:
        out(f'upgrade: dry run — the move of {product_name} to {_v(sha)}:')
        for i, step in enumerate(steps, 1):
            out(f'  {i}. {step}')
        return 0
    if not local:
        cmd = pipx_suffix_command(url, product_name, sha)
        out('upgrade: ' + ' '.join(cmd))
        try:
            rc = run(cmd).returncode
        except OSError:
            out('NEEDS OPERATOR: pipx is not on PATH')
            return 2
        if rc != 0:
            out(f'upgrade: FAILED — pipx exit {rc}; {product_name} still runs '
                f'{(rec.sha if rec else "the shared install")[:7]}')
            return 1
        got = installs.commit_of(venv)
        if not got or not (got.startswith(sha) or sha.startswith(got)):
            out(f'upgrade: FAILED — {venv} holds {(got or "unknown")[:7]}, not {sha[:7]}')
            return 1
    return _quiesced_switch(product_name, sha, venv, previous, rec, clocks, wait_s, by,
                            run, out, sleep, ops)


def _quiesced_switch(product_name, sha, venv, previous, rec, clocks, wait_s, by, run, out,
                     sleep, ops):
    from asf import installs
    paused = []     # exactly the clocks this attempt paused: what every exit resumes
    previous_handlers = _trap_signals()
    try:
        waited = _drain_and_hold(product_name, sha, clocks, wait_s, by, run, out, sleep, ops,
                                 paused)
        if waited is None:
            return MOVE_DEFERRED
        # 3. only now, with nothing of the product running, the clocks leave launchd
        for line in ops.bootout(product_name, clocks) or ():
            out(line)
        installs.write(product_name, sha, venv, previous=previous, by=by)
        out(f'upgrade: {product_name} pinned to {_v(sha)} ({venv})')
        rc = ops.install_clocks(product_name)
        if rc not in (0, None):
            if rec is None:
                os.remove(installs.record_path(product_name))
            else:
                installs.write(product_name, rec.sha, rec.venv, previous=rec.previous, by=rec.by)
            out(f'NEEDS OPERATOR: asf scheduler install --product {product_name} exited {rc} — '
                'the pin is put back; the clocks resume on what they ran')
            return 1
        hrc, msg = ops.install_hooks(product_name)
        for line in (msg or '').splitlines():
            out(line)
        if hrc:
            out(f'upgrade: WARNING — asf hooks install --product {product_name} exited {hrc}')
        verify = getattr(ops, 'verify_hooks', None)
        for line in (verify(product_name) if verify else ()):
            out(f'upgrade: WARNING — hook {line}')
        # the smoke: what the clocks will run, under their plists' exact env and interpreter —
        # before a single clock resumes on it (2026-10-03: a render that lost gh from PATH
        # stopped the landing lane, and nothing in the move noticed)
        ok_lines, failures = ops.smoke(product_name)
        for line in ok_lines:
            out(f'upgrade: smoke {line}')
        if failures:
            for line in failures:
                out(f'upgrade: smoke FAILED {line}')
            if rec is None:
                os.remove(installs.record_path(product_name))
            else:
                installs.write(product_name, rec.sha, rec.venv, previous=rec.previous, by=rec.by)
            back = ops.install_clocks(product_name)
            brc, bmsg = ops.install_hooks(product_name)
            for line in (bmsg or '').splitlines():
                out(line)
            out(f'NEEDS OPERATOR: the smoke of {product_name} at {sha[:7]} failed — rolled back to '
                f'{(rec.sha if rec else "the shared install")[:7]} (clocks rc {back}, hooks rc '
                f'{brc}); the clocks resume on what they ran')
            _host_clocks(ops, out)
            return 1
        _host_clocks(ops, out)
        out(f'upgrade: moved {product_name} to {_v(sha)}'
            + (f' (previous {_v(previous.get("sha"))} stays on disk for --rollback)'
               if previous else ''))
        return 0
    finally:
        if paused:
            for line in ops.resume(product_name, paused) or ():
                out(line)
        clear_pending(product_name)
        clear_draining(product_name)
        _restore_signals(previous_handlers)


def _trap_signals():
    """SIGTERM and SIGHUP end the move through its ``finally`` (the clocks it paused resume),
    as Ctrl-C does; SIGKILL cannot be caught — :func:`lift_stale_pauses` covers that one."""
    import signal
    import threading
    if threading.current_thread() is not threading.main_thread():
        return {}

    def bail(signum, _frame):
        raise SystemExit(128 + signum)
    saved = {}
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            saved[sig] = signal.signal(sig, bail)
        except (ValueError, OSError):
            pass
    return saved


def _restore_signals(saved):
    import signal
    for sig, handler in (saved or {}).items():
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, TypeError):
            pass


def _drain_and_hold(product_name, sha, clocks, wait_s, by, run, out, sleep, ops, paused):
    """Drain, then hold: the seconds waited once the floor is quiet with ``clocks`` paused (each
    one appended to ``paused``), else ``None`` after the refusal's lines.

    1. the drain: the drain marker stops new merge-queue cuts (never a launch); the clocks run
       on, so the batches in flight land (or drop) — a paused clock never lands one.
    2. the hold: the move's marker parks the product's ticks, ci queue passes and harvests
       (:func:`waiting`, :func:`moving`), and the pause record keeps every render from loading
       a clock. Nothing is booted out yet: ``launchctl bootout`` kills a running job, and the
       floor must be seen to go quiet on its own (2026-10-04 canary: the pause booted the clocks
       out first, and the drain never saw the tick it had just killed mid-wave).
    3. a batch cut meanwhile — by a pass already running when the hold went up, or a clock on
       an older build that does not read the drain marker — lifts the hold: back to 1."""
    # one budget for every wait below — the in-flight wait, the floor's polls, each re-cut
    # cycle's pause and resume: spent is what the sleeps asked or what the clock says, whichever
    # is more, so a slow poll or a sleep that overslept never stretches the drain (#32)
    wait_s = max(0, wait_s or 0)
    start = time.monotonic()
    deadline = start + wait_s
    asked = 0
    _mark_draining(product_name, sha, wait_s=wait_s)

    def spent():
        return max(asked, time.monotonic() - start)

    def left():
        return wait_s - spent()

    def nap():
        nonlocal asked
        step = min(DRAIN_POLL_S, left())
        if step > 0:
            sleep(step)
            asked += step

    while True:
        refs = in_flight(product_name, out=out)
        if refs:
            out(f'upgrade: draining {product_name} — no new merge-queue cut; the clocks run until '
                f'the batch(es) in flight land ({", ".join(refs)}), up to {int(left())}s')
        while refs and left() > 0:
            nap()
            refs = in_flight(product_name, out=out)
        if refs:
            out(f'upgrade: refused — the floor of {product_name} is still busy after '
                f'{int(spent())}s; nothing paused, nothing moved:')
            out(f'  merge-queue batch in flight ({", ".join(refs)})')
            return None
        _mark(product_name, sha)
        lines = ops.pause(product_name, clocks, by) or ()
        paused[:] = list(clocks)
        for line in lines:
            out(line)
        busy = floor_busy(product_name, run, deadline)
        if busy:
            out(f'upgrade: waiting up to {int(max(0, left()))}s for the floor of {product_name}:')
            for line in busy:
                out(f'  {line}')
        while busy and left() > 0:
            nap()
            busy = floor_busy(product_name, run, deadline)
            if in_flight(product_name):
                break
        if busy and in_flight(product_name) and left() > 0:
            out(f'upgrade: a batch was cut while the floor of {product_name} went quiet — '
                'the clocks resume until it lands')
            clear_pending(product_name)
            for line in ops.resume(product_name, paused) or ():
                out(line)
            paused[:] = []
            continue
        if busy:
            out(f'upgrade: refused — the floor of {product_name} is still busy after '
                f'{int(spent())}s; nothing moved:')
            for line in busy:
                out(f'  {line}')
            return None
        # the drain is over: its marker goes now, not after the switch — the move's hold and the
        # paused clocks keep the floor quiet through it
        clear_draining(product_name)
        return spent()


def _host_clocks(ops, out):
    """The host clocks follow the pin a move or rollback just wrote (#695/#701 rendered the
    probe against one product's venv)."""
    for line in ops.install_host() or ():
        out(line)


def products():
    d = os.path.join(env.ASF_HOME, 'products')
    if not os.path.isdir(d):
        return []
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith('.yaml'))


def row(name):
    """``(product, record schema, package, action)`` for one product."""
    try:
        product = env.load_product(name)
    except env.ConfigError as e:
        return (name, pin_of(name), '?', str(schema.SCHEMA_VERSION), f'fix the config: {e}')
    versions = [schema.record_version(d) for _label, d in schema.record_dirs(product)]
    versions = [v for v in versions if v is not None]
    record = ','.join(str(v) for v in sorted(set(versions))) or 'none'
    ok, _detail = schema.check(product)
    if ok:
        action = 'none'
    elif any(v > schema.SCHEMA_VERSION for v in versions):
        action = 'record is newer than this package: pipx install the newer asf'
    else:
        action = f'asf schema-migrate --product {name}'
    return (name, pin_of(name), record, str(schema.SCHEMA_VERSION), action)


def pin_of(name):
    """What the product runs, as a version: its pin (``0.1.121 (267264dbe)``), else ``shared``."""
    from asf import installs
    rec = installs.read(name)
    return rec.label if rec else 'shared'


def render(rows):
    head = ('product', 'runs', 'record schema', 'package', 'action')
    out = ['| ' + ' | '.join(head) + ' |', '| ' + ' | '.join('---' for _ in head) + ' |']
    out += ['| ' + ' | '.join(r) + ' |' for r in rows]
    return '\n'.join(out) + '\n'


def cmd_upgrade(args, run=subprocess.run):
    product = getattr(args, 'product', None)
    if isinstance(product, str) and product:
        return cmd_move(args, product, run)
    if not args.skip_pipx:
        from asf import installs
        pinned = [n for n in products() if installs.pinned(n)]
        if pinned:
            # the shared install is a pinned product's rollback target (its `previous`), and the
            # dispatcher owns the `asf` entry point once it is gone: never reinstall it blind
            print(f'upgrade: refused — pinned product(s) {", ".join(pinned)} run their own venv; '
                  f'use asf upgrade --product <p> --to <sha> (or --rollback)')
            return 2
        off = checkout_off_main(run=run)
        if off:
            print(f'upgrade: warning — {off}', file=sys.stderr)
            if not getattr(args, 'ref', None):
                try:
                    on_branch = _git(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                     'symbolic-ref', '--short', '-q', 'HEAD', run=run) == 'main'
                except (OSError, subprocess.SubprocessError):
                    on_branch = True
                if not on_branch:
                    print('upgrade: refused — the install checkout is not on main; '
                          'pass --ref <sha-or-tag> to install a named commit')
                    return 2
        rc = install(getattr(args, 'ref', None), run=run, owner=getattr(args, 'owner', None),
                     wait_s=getattr(args, 'wait', None) or 0,
                     sleep=getattr(args, 'sleep', None) or _drain_sleep)
        if rc != 0:
            return rc
    from asf.cli import version_string
    print(f'upgrade: package {version_string()}, schema {schema.SCHEMA_VERSION}')
    print(render([row(n) for n in products()]), end='')
    return 0


def cmd_move(args, product, run=subprocess.run):
    """``asf upgrade --product <p> (--to <sha> | --rollback) [--dry-run] [--wait-s N]
    [--force-ci] [--prune]``."""
    rollback = bool(getattr(args, 'rollback', False))
    ref = getattr(args, 'ref', None)
    ref = versions.tag_of(ref) or ref       # --to 0.1.108 is the tag v0.1.108
    if rollback == bool(ref):
        print('upgrade: --product takes exactly one of --to <sha> and --rollback')
        return 2
    wait_s = getattr(args, 'wait_s', None)
    rc = move(product, to=ref, rollback=rollback,
              wait_s=DEFAULT_MOVE_WAIT_S if wait_s is None else wait_s,
              force_ci=bool(getattr(args, 'force_ci', False)),
              dry_run=bool(getattr(args, 'dry_run', False)), run=run,
              sleep=getattr(args, 'sleep', None), ops=getattr(args, 'ops', None))
    if rc == 0 and getattr(args, 'prune', False) and not getattr(args, 'dry_run', False):
        from asf import installs
        installs.prune(product, run=run)
    return rc


def register(subparsers):
    p = subparsers.add_parser('upgrade', help="reinstall asf at main's head, then the schema check for every product")
    p.add_argument('--skip-pipx', action='store_true', help='only the schema table')
    p.add_argument('--product', metavar='P',
                   help="move one pinned product: its own venv asf-factory-<p>-<sha7>, recorded "
                        "in state/<p>/install.json (with --to or --rollback)")
    p.add_argument('--rollback', action='store_true',
                   help="--product: switch back to the recorded previous venv (local, offline)")
    p.add_argument('--dry-run', action='store_true',
                   help='--product: print the move, change nothing')
    p.add_argument('--wait-s', type=int, default=None, metavar='SECONDS',
                   help=f'--product: how long to wait for the floor to drain before refusing '
                        f'(default {DEFAULT_MOVE_WAIT_S})')
    p.add_argument('--force-ci', action='store_true',
                   help='--product: move although CI on the sha is not known green (loud)')
    p.add_argument('--prune', action='store_true',
                   help='--product: after the move, uninstall the oldest venvs beyond '
                        'the newest three (never the current or previous)')
    p.add_argument('--to', dest='ref', metavar='REF',
                   help="the version (0.1.108), tag or commit to install (default: main's head)")
    p.add_argument('--ref', dest='ref', metavar='REF',
                   help='same as --to — the accepted older spelling')
    p.add_argument('--wait', type=int, nargs='?', const=DEFAULT_MANUAL_WAIT_S, default=None,
                   metavar='SECONDS',
                   help='when another asf tick or harvest runs: park new ticks and wait up to '
                        f'SECONDS (default {DEFAULT_MANUAL_WAIT_S}) for them to end, then install')
    p.set_defaults(run=cmd_upgrade)
    return p


# ---- tunables ---------------------------------------------------------------

#: The config key (``~/.ASF/config.yaml``) over each constant above; the constant is its default.
TUNABLES = {
    'DEFAULT_REPO_URL': 'upgrade.repo_url',
    'PENDING_TTL_S': 'upgrade.pending_ttl_s',
}


def tunable(name):
    """The constant ``name`` of :data:`TUNABLES` with its config key over it."""
    from asf import config_keys
    return config_keys.value(TUNABLES[name], globals()[name])
