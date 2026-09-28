"""asf.security.ports — the nightly probe of every box's exposed ports, and the address it never
writes down.

A box's address never becomes a line, an artifact or a commit (D11): the probe's targets reach
the runner as one repository secret, ``$ASF_PORT_TARGETS``, and every line this module prints or
returns names the box label alone. The probe itself runs on a runner (:func:`dispatch`, dispatched
by the daily step) because a connect from the tick host proves nothing about what the internet
reaches (D12) — the tick host sits on the same network as the boxes. A box the newest probe did
not cover is a violation at the same severity as an open port (D13): unprobed is indistinguishable
from closed, and silence there is not safety.

Four functions and the daily step's one new part:

- :func:`targets` — the union of ``ci.pool``'s boxes (which carry no address of any kind, P16)
  and ``security.ports.boxes``, each resolved against ``$ASF_PORT_TARGETS``.
- :func:`probe` — one TCP connect per (box, port); ``connect`` is injected in the tests.
- :func:`dispatch` — ``gh workflow run`` of the configured workflow; the daily step's part.
- :func:`latest` — the newest successful run's artifact, or ``(None, why)``.
- :func:`violations` — the three ``R-0010`` line shapes a stale, missing or exposing probe owes.
"""
import datetime
import json
import os
import shutil
import socket
import subprocess
import tempfile

from asf.conventions import Conventions
from asf.security import gh_env

#: the repository secret the job hands the probe: ``{box: address}`` as JSON.
TARGETS_ENV = 'ASF_PORT_TARGETS'
GH_TIMEOUT_S = 30


def _security_ports(product):
    conv = getattr(product, 'conventions', None)
    return (conv or Conventions()).security_ports()


def _configured_addresses():
    """``{box: address}`` off ``$ASF_PORT_TARGETS``; ``{}`` when unset or unreadable."""
    raw = os.environ.get(TARGETS_ENV)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def targets(product):
    """``[(box, address)]`` — the union of the ``ci.pool`` boxes and ``security.ports.boxes``, in
    that order, each resolved against ``$ASF_PORT_TARGETS``. A box with no address comes back with
    an empty one: unprobed, never discovered, never guessed, never ranged."""
    from asf import ci_pool
    boxes = []
    seen = set()
    for entry in ci_pool.load_pool(product):
        if entry.box and entry.box not in seen:
            seen.add(entry.box)
            boxes.append(entry.box)
    for box in _security_ports(product)['boxes']:
        if box and box not in seen:
            seen.add(box)
            boxes.append(box)
    addresses = _configured_addresses()
    return [(box, str(addresses.get(box) or '')) for box in boxes]


def _connect(address, port, timeout):
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe(targets, ports, connect=None, timeout=2.0):
    """``{'ts', 'from', 'results': [{'box', 'port', 'open'}]}`` — one TCP connect per (box, port)
    of ``targets``. The address is used and never returned: a result names the box label, so the
    artifact carries no address either. A box with no address yields no result rows — which is
    what makes it *unprobed* downstream."""
    connect = connect or _connect
    results = []
    for box, address in targets:
        if not address:
            continue
        for port in ports:
            results.append({'box': box, 'port': port, 'open': bool(connect(address, port, timeout))})
    return {'ts': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'from': os.environ.get('RUNNER_NAME') or socket.gethostname(),
            'results': results}


def dispatch(product, run=None):
    """``gh workflow run <security.ports.workflow>`` on the trunk. A product with no boxes
    configured prints ``no boxes configured`` and returns 0 (this product configures none, so
    this is a clean no-op here, P21)."""
    run = run or subprocess.run
    cfg = _security_ports(product)
    if not cfg['workflow']:
        print('no boxes configured')
        return 0
    args = ['gh', 'workflow', 'run', cfg['workflow'], '-R', product.repo_slug,
            '--ref', product.main]
    try:
        p = run(args, capture_output=True, text=True, timeout=GH_TIMEOUT_S, env=gh_env(product))
    except (OSError, subprocess.SubprocessError) as e:
        print(f'security-ports dispatch: gh workflow run failed — {e}')
        return 1
    if p.returncode != 0:
        lines = (p.stderr or p.stdout or '').strip().splitlines()
        print(f"security-ports dispatch: gh workflow run failed — "
              f"{lines[-1] if lines else 'exit ' + str(p.returncode)}")
        return 1
    print(f'dispatched {cfg["workflow"]}')
    return 0


def latest(product, run=None):
    """The newest successful probe run's artifact, parsed — ``(data, None)`` — or ``(None, why)``
    when there is no successful run or the artifact is unreadable: ``gh run list --workflow <w>
    --status success --limit 1 --json databaseId,createdAt``, then ``gh run download <id> -n
    <artifact> -D <tmp>``, the JSON read out of the temp dir and the dir removed."""
    run = run or subprocess.run
    cfg = _security_ports(product)
    if not cfg['workflow'] or not cfg['artifact']:
        return None, 'no boxes configured'
    env = gh_env(product)
    try:
        p = run(['gh', 'run', 'list', '-R', product.repo_slug, '--workflow', cfg['workflow'],
                 '--status', 'success', '--limit', '1', '--json', 'databaseId,createdAt'],
                capture_output=True, text=True, timeout=GH_TIMEOUT_S, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return None, f'gh run list: {e}'
    if p.returncode != 0:
        lines = (p.stderr or p.stdout or '').strip().splitlines()
        return None, f"gh run list: {lines[-1] if lines else 'failed'}"
    try:
        runs = json.loads(p.stdout or '[]')
    except ValueError:
        return None, 'gh run list: unreadable JSON'
    if not isinstance(runs, list) or not runs:
        return None, 'no successful run'
    run_id = runs[0].get('databaseId')
    tmp = tempfile.mkdtemp()
    try:
        try:
            p = run(['gh', 'run', 'download', str(run_id), '-R', product.repo_slug,
                     '-n', cfg['artifact'], '-D', tmp],
                    capture_output=True, text=True, timeout=GH_TIMEOUT_S, env=env)
        except (OSError, subprocess.SubprocessError) as e:
            return None, f'gh run download: {e}'
        if p.returncode != 0:
            lines = (p.stderr or p.stdout or '').strip().splitlines()
            return None, f"gh run download: {lines[-1] if lines else 'failed'}"
        names = sorted(n for n in os.listdir(tmp) if n.endswith('.json'))
        if not names:
            return None, 'artifact has no json file'
        try:
            with open(os.path.join(tmp, names[0]), encoding='utf-8') as f:
                return json.load(f), None
        except (OSError, ValueError) as e:
            return None, f'artifact unreadable: {e}'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def violations(product, now=None):
    """The three ``R-0010`` line shapes, in this order:

        R-0010 exposed port <box>:<port> open from a runner at <ts> sev=S1 sig=port-<box>-<port>
        R-0010 box <box> was not probed (no address in the probe's targets) sev=S1 sig=unprobed-<box>
        R-0010 the last port probe is <n>h old (<why>) sev=S2 sig=probe-stale

    one per open port, one per configured box the newest probe did not cover, and one when the
    newest probe is older than ``max_age_h``, absent, or unreadable, naming the reason. Nothing is
    closed and nothing is reconfigured."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cfg = _security_ports(product)
    boxes = [box for box, _ in targets(product)]
    data, why = latest(product)
    if data is None:
        return [f'R-0010 the last port probe is unreadable ({why}) sev=S2 sig=probe-stale']
    out = []
    results = data.get('results') or []
    for r in results:
        if r.get('open'):
            out.append(f"R-0010 exposed port {r['box']}:{r['port']} open from a runner at "
                       f"{data.get('ts')} sev=S1 sig=port-{r['box']}-{r['port']}")
    covered = {r.get('box') for r in results}
    for box in boxes:
        if box not in covered:
            out.append(f"R-0010 box {box} was not probed (no address in the probe's targets) "
                       f"sev=S1 sig=unprobed-{box}")
    age_h = None
    ts = data.get('ts')
    if isinstance(ts, str):
        try:
            age_h = (now - datetime.datetime.fromisoformat(ts)).total_seconds() / 3600
        except ValueError:
            age_h = None
    if age_h is None:
        out.append('R-0010 the last port probe is unreadable (no timestamp) sev=S2 sig=probe-stale')
    elif age_h > cfg['max_age_h']:
        out.append(f"R-0010 the last port probe is {age_h:.0f}h old (older than "
                   f"{cfg['max_age_h']}h) sev=S2 sig=probe-stale")
    return out
