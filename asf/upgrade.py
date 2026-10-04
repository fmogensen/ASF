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

from asf import __version__, env, schema
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
    until = at + PENDING_TTL_S
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
    if age is None or age > PENDING_TTL_S or age < -60:
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
    if age > PENDING_TTL_S or age < -60:  # a clock step leaves a future `at`; pending() clears it
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
    go on — the owner drains and retries the upgrade at its start."""
    data = pending(product_name, now, installed, out=out, run=run)
    if data is None or data.get('owner') == product_name:
        return False
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
    return m.group(1) if m else os.environ.get('ASF_REPO_URL', DEFAULT_REPO_URL)


def remote_head(url, run=subprocess.run, branch='main'):
    text = _out(run, ['git', 'ls-remote', url, f'refs/heads/{branch}'])
    return (text or '').split('\t')[0].strip() or None


#: a resolved commit is exactly this — 40 hex characters — never looked up again (``--to``'s sha
#: form costs no network round trip)
HEX40 = re.compile(r'[0-9a-fA-F]{40}$')


def resolve_ref(url, ref, run=subprocess.run):
    """``ref`` as a commit, for the CI guard, ``write_pending`` and the marker (PD12): unchanged
    when it already is a 40-hex sha, else the commit ``git ls-remote <url> <ref>`` names — the
    dereferenced commit on a ``^{}`` line for an annotated tag, else the line's own sha. ``None``
    when ``ref`` matches nothing on ``url``."""
    if not ref or HEX40.match(ref):
        return ref
    text = _out(run, ['git', 'ls-remote', url, ref])
    lines = [ln for ln in (text or '').splitlines() if ln.strip()]
    deref = next((ln for ln in lines if ln.endswith('^{}')), None)
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


def foreign_homes(pids, run=subprocess.run):
    """The pids whose environment names an ASF home other than :data:`asf.env.ASF_HOME` —
    ``ASF_HOME``, else ``HOME``/.ASF, exactly as :mod:`asf.env` resolves it. Read from
    ``ps eww`` (the command followed by its environment); a process whose environment cannot be
    read is not foreign — an unknown process still holds the floor."""
    text = _out(run, ['ps', 'eww', '-o', 'pid=,command=', '-p', ','.join(str(p) for p in pids)],
                timeout=10) or ''
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


def describe(pids, run=subprocess.run):
    """``pid (age) command`` for each pid, for the lines that say what an upgrade waits on."""
    text = _out(run, ['ps', '-o', 'pid=,etime=,command=', '-p', ','.join(str(p) for p in pids)],
                timeout=10) or ''
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
    waited = 0
    while others and waited < wait_s:
        step = min(DRAIN_POLL_S, wait_s - waited)
        sleep(step)
        waited += step
        others = other_ticks(run)
    if not others:
        out(f'upgrade: the floor drained after {int(waited)}s')
    return others


def ci_red(url, ref, run=subprocess.run):
    """True when a finished remote CI run on ``ref`` failed; False when green, pending, or not
    knowable (no ``gh``, not a GitHub url) — the check is skipped, never guessed."""
    m = re.search(r'github\.com[/:]([^/]+/[^/]+?)(\.git)?/?$', url)
    if not m:
        return False
    text = _out(run, ['gh', 'run', 'list', '--repo', m.group(1), '--commit', ref,
                      '--limit', '20', '--json', 'conclusion'], timeout=30)
    try:
        runs = json.loads(text) if text else []
    except ValueError:
        return False
    return any((r or {}).get('conclusion') in ('failure', 'timed_out') for r in runs)


def refuses(url, ref, run=subprocess.run):
    """Why :func:`_install` would refuse ``ref`` — one phrase — else ``None``. The pending marker
    parks every other product's ticks, so it is written only for a target the upgrade will
    actually install (B-0141: a red head parked the whole factory for the marker's whole life)."""
    if not ref:
        return f"main's head is unreadable from {url}"
    if ci_red(url, ref, run):
        return f'remote CI is red at {ref[:7]}'
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
    listed = _out(run, ['launchctl', 'list'])
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


def install(ref=None, run=subprocess.run, out=print, owner=None, wait_s=0, sleep=time.sleep):
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
    records it and :func:`asf.cli.version_string` reports the release."""
    others = other_ticks(run)
    marked = []
    pin = ref
    if ref and not HEX40.match(ref):
        url = repo_url(run)
        resolved = resolve_ref(url, ref, run)
        if resolved is None:
            out(f'upgrade: refused — {ref} does not resolve to a commit on {url}')
            return 2
        ref = resolved
    targets = marked_products(owner) if others and (owner or wait_s) else []
    if targets:
        url = repo_url(run)
        if not owner and not ref:
            ref = remote_head(url, run)  # the operator's marker names what it waits for
        refusal = refuses(url, ref, run)
        if refusal:
            # the install will refuse this target, so no marker may park the other products for
            # it — and one this caller holds for a head that has moved goes now, not at the TTL
            for name in targets:
                wait = read_pending(name)
                if wait is not None and (wait.get('owner') == owner or wait.get('sha') == ref):
                    clear_pending(name)
            out(f'upgrade: no pending mark — {refusal}; the other ticks run')
        elif ref:
            # an operator's wait never replaces a marker a tick already holds
            marked = [n for n in targets if (owner or read_pending(n) is None)
                      and write_pending(ref, owner, n) is not None]
            if marked:
                out(f'upgrade: pending {ref[:7]} — other ticks wait until it installs')
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
    rc = _install(ref, run, out, pin=pin) if pin != ref else _install(ref, run, out)
    if rc == 0:
        record_last(ref)
    if owner:
        _clear(n for n in marked_products(owner)
               if (read_pending(n) or {}).get('owner') == owner)
    _clear(marked)
    return rc


def _clear(names):
    for name in names:
        clear_pending(name)


def _install(ref, run, out, pin=None):
    """Reinstall at ``ref``. ``pin`` is what ``pipx`` receives on the command line when it
    differs from ``ref`` — the tag :func:`install` resolved ``ref`` from; ``None`` (the default)
    means ``pipx`` receives ``ref`` itself, as every caller but :func:`install`'s ``--to`` does."""
    url = repo_url(run)
    ref = ref or remote_head(url, run)
    if not ref:
        out(f'NEEDS OPERATOR: cannot read main\'s head from {url}')
        return 2
    if ci_red(url, ref, run):
        out(f'upgrade: skipped — remote CI is red at {ref[:7]}; the next green head installs')
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
    if not got or not (got.startswith(ref) or ref.startswith(got)):
        out(f'upgrade: FAILED — the install is at {(got or "unknown")[:7]}, not {ref[:7]}')
        return 1
    out(f'upgrade: installed {ref[:7]}')
    for line in reload_clocks(products(), run):
        out(line)
    return 0


# ---- the per-product move: ``asf upgrade --product <p> --to <sha>`` ---------------------------
#
# A pinned product (``state/<p>/install.json``, :mod:`asf.installs`) runs its own venv,
# ``asf-factory-<p>-<sha7>``. A move installs the target venv beside the running one (or finds it
# already on disk — then it is a local switch, no network), requires positive CI evidence on the
# exact sha, and then quiesces the product before it re-points anything: record every clock's
# pause (no bootout yet — that kills a running tick), wait until no tick, ``ci queue`` pass or
# background harvest of the product runs, its ``harvest.lock`` is free and no merge-queue batch
# is in flight — else resume and refuse after ``--wait-s`` — only then boot the clocks out,
# record the pin with the venv it replaces as ``previous``, render the clocks and the hooks from
# it, smoke, re-render a stale host clock, resume. ``--rollback`` moves back to ``previous`` the
# same way, offline.

#: ``asf upgrade --product`` waits this long for the product's floor to drain before refusing
DEFAULT_MOVE_WAIT_S = 900
#: the product's processes a move waits out: its ticks, its ``ci queue`` passes (the queue clock
#: pushes and cancels runs) and its background harvest — by interpreter argv, as
#: :data:`TICK_PATTERN`, but also the suffixed ``asf-<p>-<sha7>`` and dispatcher entry points
MOVE_PATTERN = r'(-m asf\.cli|/asf\S*) (tick|ci queue)( |$)|-m asf\.tick\.step_harvest( |$)'
#: the move waits (CI unknown, the floor still busy): nothing changed, try again later
MOVE_DEFERRED = 3
#: a check run conclusion that is a red verdict (anything else not ``success`` is Unknown)
RED_CONCLUSIONS = ('failure', 'timed_out', 'cancelled', 'startup_failure')


def product_processes(product_name, run=subprocess.run, me=None):
    """Pids of ``product_name``'s ticks, ``ci queue`` passes and background harvests under this
    ASF home, other than this process (and its parent)."""
    me = me if me is not None else {os.getpid(), os.getppid()}
    text = _out(run, ['pgrep', '-f', MOVE_PATTERN], timeout=10) or ''
    pids = [int(x) for x in text.split() if x.isdigit() and int(x) not in me]
    if not pids:
        return []
    listing = _out(run, ['ps', '-ww', '-o', 'pid=,command=', '-p',
                         ','.join(str(p) for p in pids)], timeout=10) or ''
    mine = re.compile(rf'--product[ =]{re.escape(product_name)}( |$)')
    ours = []
    for ln in listing.splitlines():
        parts = ln.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and mine.search(parts[1]):
            ours.append(int(parts[0]))
    foreign = foreign_homes(ours, run) if ours else set()
    return [p for p in ours if p not in foreign]


def floor_busy(product_name, run=subprocess.run):
    """What keeps ``product_name``'s floor from being quiet — one line each — else ``[]``."""
    state = os.path.join(env.ASF_HOME, 'state', product_name)
    pids = product_processes(product_name, run)
    busy = [f'process {line}' for line in describe(pids, run)] if pids else []
    if os.path.exists(os.path.join(state, 'harvest.lock')):
        from asf.harvest import harvest
        if harvest.try_lock_held(state):
            busy.append('harvest.lock is held (a gate is running)')
    if os.path.exists(os.path.join(state, 'merge-queue.json')):
        from asf import merge_queue
        refs = [b.get('ref') for b in merge_queue.load(state)['batches']]
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
    text = _out(run, ['gh', 'api', f'repos/{slug}/commits/{sha}/check-runs?per_page=100'],
                timeout=60)
    try:
        runs = json.loads(text)['check_runs'] if text else None
    except (ValueError, KeyError, TypeError):
        runs = None
    if not isinstance(runs, list):
        return 'unknown', 'gh could not read the check runs'
    latest = {}
    for r in runs:
        if isinstance(r, dict) and r.get('name') in names:
            if r['name'] not in latest or (r.get('id') or 0) > (latest[r['name']].get('id') or 0):
                latest[r['name']] = r
    statuses = {}
    if any(n not in latest for n in names):
        stext = _out(run, ['gh', 'api', f'repos/{slug}/commits/{sha}/status'], timeout=60)
        try:
            statuses = {s.get('context'): s.get('state')
                        for s in json.loads(stext).get('statuses') or ()} if stext else {}
        except (ValueError, AttributeError, TypeError):
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
        return scheduler.pause(product_name, clocks, reason='upgrade', by=by, bootout=False)

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
        out(f'upgrade: rollback of {product_name} to {(sha or "?")[:7]} ({venv}) — '
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
        out(f'upgrade: {product_name} already runs {sha[:7]} ({venv}) — nothing to move')
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
    clocks = [c for c in ops.clock_names(product_name) if c not in ops.paused(product_name)]
    steps += [f'pause clocks {", ".join(clocks) or "(none)"} (the record: no new tick starts)',
              f'drain: no tick / ci queue / harvest of {product_name}, harvest.lock free, '
              f'no merge-queue batch (up to {int(wait_s)}s; else resume, nothing moved)',
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
        out(f'upgrade: dry run — the move of {product_name} to {sha[:7]}:')
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
    # 1. no new tick starts: the move's marker parks the product's ticks, ci queue passes and
    # harvests (:func:`waiting`, :func:`moving`), and the pause record keeps every render from
    # loading a clock. Nothing is booted out yet: ``launchctl bootout`` kills a running job, and
    # the drain below must see the tick end on its own (2026-10-04 canary: the pause booted the
    # clocks out first, and the drain never saw the tick it had just killed mid-wave)
    _mark(product_name, sha)
    try:
        for line in ops.pause(product_name, clocks, by) or ():
            out(line)
        # 2. the drain: every running process of the product ends, or the move gives up
        busy = floor_busy(product_name, run)
        waited = 0
        if busy:
            out(f'upgrade: waiting up to {int(wait_s)}s for the floor of {product_name}:')
            for line in busy:
                out(f'  {line}')
        while busy and waited < wait_s:
            step = min(DRAIN_POLL_S, wait_s - waited)
            sleep(step)
            waited += step
            busy = floor_busy(product_name, run)
        if busy:
            out(f'upgrade: refused — the floor of {product_name} is still busy after '
                f'{int(waited)}s; nothing moved:')
            for line in busy:
                out(f'  {line}')
            return MOVE_DEFERRED
        # 3. only now, with nothing of the product running, the clocks leave launchd
        for line in ops.bootout(product_name, clocks) or ():
            out(line)
        installs.write(product_name, sha, venv, previous=previous, by=by)
        out(f'upgrade: {product_name} pinned to {sha[:7]} ({venv})')
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
        out(f'upgrade: moved {product_name} to {sha[:7]}'
            + (f' (previous {(previous.get("sha") or "?")[:7]} stays on disk for --rollback)'
               if previous else ''))
        return 0
    finally:
        for line in ops.resume(product_name, clocks) or ():
            out(line)
        clear_pending(product_name)


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
        return (name, '?', str(schema.SCHEMA_VERSION), f'fix the config: {e}')
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
    return (name, record, str(schema.SCHEMA_VERSION), action)


def render(rows):
    head = ('product', 'record schema', 'package', 'action')
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
    print(f'upgrade: package {__version__}, schema {schema.SCHEMA_VERSION}')
    print(render([row(n) for n in products()]), end='')
    return 0


def cmd_move(args, product, run=subprocess.run):
    """``asf upgrade --product <p> (--to <sha> | --rollback) [--dry-run] [--wait-s N]
    [--force-ci] [--prune]``."""
    rollback = bool(getattr(args, 'rollback', False))
    ref = getattr(args, 'ref', None)
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
                   help="the commit or tag to install (default: main's head)")
    p.add_argument('--ref', dest='ref', metavar='REF',
                   help='same as --to — the accepted older spelling')
    p.add_argument('--wait', type=int, nargs='?', const=DEFAULT_MANUAL_WAIT_S, default=None,
                   metavar='SECONDS',
                   help='when another asf tick or harvest runs: park new ticks and wait up to '
                        f'SECONDS (default {DEFAULT_MANUAL_WAIT_S}) for them to end, then install')
    p.set_defaults(run=cmd_upgrade)
    return p
