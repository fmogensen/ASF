"""asf.tick.network — one answer per process to "is the forge reachable from this host".

A tick that runs while the host has no route out (DNS down, cable unplugged, VPN dropped) used to
try its network steps anyway and count each one as failed; an upgrade attempted offline read as a
failed upgrade in the release-readiness measurement. :func:`reachable` asks once — a DNS lookup,
then a TCP connect to :443 — and memoises the answer for :data:`MEMO_S` seconds, so every step of
one tick reads the same verdict.

The verdict is three-valued: ``ok`` True (reachable), False (offline — DNS failed or the connect
was refused or unroutable) or None (Unknown — the connect timed out: a slow link and a dead one
look the same from here). A caller that needs the network treats anything but True as "not now".

:func:`is_offline_text` reads the same verdict out of a git/network error's text; it is the one
list of offline markers (``asf.tick.tick`` keeps its ``_reason`` alias over it).

The host probe (W1-PR3a, log-only) is the second half: :func:`probe_record` answers, once a
minute, *which layer* fails — DNS, the TCP connect, the default route, the interface, Tailscale or
a VPN service — :func:`watchdog` appends it to ``state/network.jsonl`` and keeps the latest in
``state/network.json``, and :func:`offline_ticks` reads the log back as the idle metric. Nothing
here recovers anything: the layer that fails is the evidence the recovery is chosen from.
"""
import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass

HOST = 'github.com'
PORT = 443
TIMEOUT_S = 3
MEMO_S = 30

#: Substrings a git/network failure prints when the host has no route out at all — DNS down,
#: cable unplugged, VPN dropped (B-0124).
OFFLINE_MARKERS = (
    'could not resolve host',
    'temporary failure in name resolution',
    'name or service not known',
    'nodename nor servname provided',
    'network is unreachable',
    'no route to host',
    'connection timed out',
)


@dataclass(frozen=True)
class Reachability:
    as_of: float
    ok: object  # True | False | None (Unknown)
    reason: str = ''

    @property
    def offline(self):
        return self.ok is not True

    def label(self):
        return 'reachable' if self.ok is True else ('offline' if self.ok is False else 'unknown')


_MEMO = {}


def is_offline_text(text):
    """True when an error's text carries one of :data:`OFFLINE_MARKERS`."""
    low = (text or '').lower()
    return any(m in low for m in OFFLINE_MARKERS)


def _probe(host, timeout_s):
    """``(ok, reason)``: DNS first, then a TCP connect to :data:`PORT`."""
    try:
        infos = socket.getaddrinfo(host, PORT, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        return False, f'DNS: {e.strerror or e}'
    except OSError as e:
        return False, f'DNS: {e}'
    last = 'no address'
    for family, kind, proto, _canon, addr in infos:
        try:
            with socket.socket(family, kind, proto) as s:
                s.settimeout(timeout_s)
                s.connect(addr)
                return True, ''
        except socket.timeout:
            return None, f'connect to {host}:{PORT} timed out after {timeout_s:g}s'
        except OSError as e:
            last = f'connect: {e.strerror or e}'
    return False, last


def reachable(host=HOST, timeout_s=TIMEOUT_S, probe=None, now=None):
    """The memoised :class:`Reachability` of ``host``. ``probe(host, timeout_s) -> (ok, reason)``
    replaces the socket probe (tests); a probe that raises reads as Unknown."""
    t = time.monotonic() if now is None else now
    hit = _MEMO.get(host)
    if hit is not None and t - hit[0] <= MEMO_S:
        return hit[1]
    try:
        ok, reason = (probe or _probe)(host, timeout_s)
    except Exception as e:  # noqa: BLE001 — a probe never stops a tick
        ok, reason = None, f'probe failed: {str(e).strip() or type(e).__name__}'
    r = Reachability(time.time(), ok, reason or '')
    _MEMO[host] = (t, r)
    return r


def forget():
    """Drop the memo (tests; a long-lived process that wants a fresh answer)."""
    _MEMO.clear()


# ---- the host probe clock (log-only) ------------------------------------------------------

#: ``asf.host.net-probe`` runs the probe this often (the clock's StartInterval)
PROBE_EVERY_S = 60
#: hosts probed when ``config.yaml network.hosts`` names none
DEFAULT_HOSTS = ('github.com', 'api.github.com')
LOG_FILE = 'network.jsonl'
LATEST_FILE = 'network.json'
KEEP_S = 7 * 86400
#: a record older than this is not "the last minute": the doctor reads the clock as stopped
STALE_S = 5 * PROBE_EVERY_S
CMD_TIMEOUT_S = 5


def enabled(cfg):
    """``network.probe`` in ``config.yaml`` — ``on`` (or true); anything else is off."""
    v = ((cfg or {}).get('network') or {}).get('probe')
    return v is True or str(v).strip().lower() == 'on'


def hosts(cfg):
    named = ((cfg or {}).get('network') or {}).get('hosts')
    if isinstance(named, str):
        named = [named]
    return tuple(h for h in (named or DEFAULT_HOSTS) if isinstance(h, str) and h)


def config_problems(cfg):
    """``[(dotted key, problem)]`` for a malformed ``network:`` block of ``config.yaml``."""
    block = (cfg or {}).get('network')
    if block is None:
        return []
    if not isinstance(block, dict):
        return [('network', f'must be a mapping, not {block!r}')]
    problems = []
    for key in ('probe', 'recover', 'watchdog'):
        v = block.get(key)
        if v is not None and not (isinstance(v, bool) or str(v).lower() in ('on', 'off')):
            problems.append((f'network.{key}', f'must be on or off, not {v!r}'))
    h = block.get('hosts')
    if h is not None and not (isinstance(h, list) and all(isinstance(x, str) and x for x in h)):
        problems.append(('network.hosts', 'must be a list of host names'))
    return problems


def run_command(argv, timeout=CMD_TIMEOUT_S):
    """Stdout of ``argv``, or None when the tool is absent, fails or hangs."""
    if shutil.which(argv[0]) is None:
        return None
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def _dns(host, resolve):
    t = time.monotonic()
    try:
        addrs = sorted({i[4][0] for i in resolve(host, PORT, type=socket.SOCK_STREAM)})
    except OSError as e:
        return {'ok': False, 'ms': round((time.monotonic() - t) * 1000),
                'reason': getattr(e, 'strerror', None) or str(e)}
    return {'ok': True, 'ms': round((time.monotonic() - t) * 1000), 'addrs': addrs[:4]}


def parse_route(text):
    """``{interface, gateway}`` out of ``route -n get default`` (empty when there is no route)."""
    out = {}
    for line in (text or '').splitlines():
        k, _, v = line.strip().partition(':')
        if k in ('interface', 'gateway') and v.strip():
            out[k] = v.strip()
    return out


def parse_resolvers(text, limit=6):
    """The distinct ``nameserver[n] : addr`` of ``scutil --dns``, in order, capped."""
    seen = []
    for line in (text or '').splitlines():
        k, _, v = line.strip().partition(':')
        if k.startswith('nameserver[') and v.strip() and v.strip() not in seen:
            seen.append(v.strip())
    return seen[:limit]


def parse_vpn(text):
    """``[{name, state}]`` out of ``scutil --nc list`` (``* (Connected) ... "Name" ...``)."""
    out = []
    for line in (text or '').splitlines():
        if '(' not in line or '"' not in line:
            continue
        state = line.split('(', 1)[1].split(')', 1)[0]
        name = line.split('"')[1]
        out.append({'name': name, 'state': state})
    return out


def parse_tailscale(text):
    """``{state, online}`` out of ``tailscale status --json`` — None when it does not parse."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    me = data.get('Self') if isinstance(data.get('Self'), dict) else {}
    return {'state': data.get('BackendState') or '', 'online': me.get('Online')}


def failing_layer(rec):
    """The first layer that failed, in the order a packet needs them: ``interface`` (no default
    route interface), ``route`` (no gateway), ``vpn`` (a service not Connected while DNS fails),
    ``tailscale`` (not Running while DNS fails), ``dns`` (resolution failed with a route),
    ``tcp`` (resolved, connect failed). ``None`` when every probed host is reachable."""
    dns, tcp = rec.get('dns') or {}, rec.get('tcp') or {}
    if all(v.get('ok') for v in tcp.values()) and tcp:
        return None
    route = rec.get('route') or {}
    if not route.get('interface'):
        return 'interface'
    if not route.get('gateway'):
        return 'route'
    dns_down = any(not v.get('ok') for v in dns.values())
    if dns_down:
        for v in rec.get('vpn') or []:
            if str(v.get('state', '')).lower() not in ('connected', 'disconnected'):
                return 'vpn'
        ts = rec.get('tailscale')
        if ts and ts.get('state') not in ('Running', '', None):
            return 'tailscale'
        return 'dns'
    return 'tcp'


def probe_record(now, probe=None, run=None, host_list=None, resolve=None):
    """One probe of the host's way out, as a JSON-ready dict. ``probe(host, timeout_s) ->
    (ok, reason)`` is the TCP leg (:func:`_probe` — DNS inside it is read separately here, so a
    fake replaces both); ``run(argv) -> text|None`` runs the system tools; ``resolve`` is
    ``getaddrinfo``. Every injected leg is optional, none can raise out of here."""
    probe = probe or _probe
    run = run or run_command
    resolve = resolve or socket.getaddrinfo
    host_list = tuple(host_list or DEFAULT_HOSTS)
    rec = {'at': float(now), 'hosts': list(host_list), 'dns': {}, 'tcp': {}}
    for h in host_list:
        rec['dns'][h] = _dns(h, resolve)
        if not rec['dns'][h]['ok']:
            rec['tcp'][h] = {'ok': False, 'reason': 'not tried: DNS failed'}
            continue
        t = time.monotonic()
        try:
            ok, reason = probe(h, TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 — a probe never stops the clock
            ok, reason = None, f'probe failed: {str(e).strip() or type(e).__name__}'
        rec['tcp'][h] = {'ok': ok is True, 'ms': round((time.monotonic() - t) * 1000),
                         'reason': reason or ''}
        if ok is None:
            rec['tcp'][h]['unknown'] = True
    rec['route'] = parse_route(run(['route', '-n', 'get', 'default']))
    rec['resolvers'] = parse_resolvers(run(['scutil', '--dns']))
    rec['vpn'] = parse_vpn(run(['scutil', '--nc', 'list']))
    ts = run(['tailscale', 'status', '--json'])
    rec['tailscale'] = parse_tailscale(ts) if ts else None
    rec['ok'] = bool(rec['tcp']) and all(v['ok'] for v in rec['tcp'].values())
    rec['failing'] = failing_layer(rec)
    return rec


def state_root(home):
    return os.path.join(home, 'state')


def log_line(rec):
    """The one human line a probe leaves in the clock's log."""
    if rec.get('ok'):
        return 'net-probe: reachable ' + ' '.join(f"{h}={v.get('ms', 0)}ms"
                                                    for h, v in rec['tcp'].items())
    why = '; '.join(f"{h}: {(rec['dns'].get(h) or {}).get('reason') or v.get('reason') or 'failed'}"
                    for h, v in rec['tcp'].items() if not v.get('ok'))
    route = rec.get('route') or {}
    return (f"net-probe: OFFLINE failing={rec.get('failing')} iface={route.get('interface', '-')} "
            f"gw={route.get('gateway', '-')} resolvers={','.join(rec.get('resolvers') or []) or '-'} "
            f"({why})")


def _rotate(path, now):
    """Drop records older than :data:`KEEP_S` — only when the first one is, so a normal append
    reads one line, not the file."""
    try:
        with open(path, encoding='utf-8') as f:
            first = f.readline()
        if not first or now - float(json.loads(first).get('at', now)) <= KEEP_S:
            return
        with open(path, encoding='utf-8') as f:
            keep = [ln for ln in f if _at(ln) >= now - KEEP_S]
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.writelines(keep)
        os.replace(tmp, path)
    except (OSError, ValueError):
        return


def _at(line):
    try:
        return float(json.loads(line).get('at', 0))
    except (ValueError, AttributeError, TypeError):
        return 0.0


def write_record(home, rec):
    """Append ``rec`` to ``state/network.jsonl`` (``O_APPEND``, 7-day rotation) and replace
    ``state/network.json`` with it. Returns the log path."""
    root = state_root(home)
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, LOG_FILE)
    _rotate(path, rec['at'])
    line = json.dumps(rec, sort_keys=True) + '\n'
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode('utf-8'))
    finally:
        os.close(fd)
    latest = os.path.join(root, LATEST_FILE)
    tmp = latest + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(line)
    os.replace(tmp, latest)
    return path


def watchdog(home, cfg=None, now=None, probe=None, run=None, resolve=None, force=False):
    """One clock tick: probe, write, return the record — or None when ``network.probe`` is off
    (and not ``force``d). Log only: no recovery action is taken here."""
    if not force and not enabled(cfg):
        return None
    t = time.time() if now is None else now
    rec = probe_record(t, probe=probe, run=run, host_list=hosts(cfg), resolve=resolve)
    write_record(home, rec)
    return rec


def read_latest(home):
    """The last record written, or None (absent, unreadable)."""
    try:
        with open(os.path.join(state_root(home), LATEST_FILE), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_log(home, since=0.0, until=None):
    """The records with ``since <= at <= until``, oldest first; unreadable lines are skipped."""
    out = []
    try:
        with open(os.path.join(state_root(home), LOG_FILE), encoding='utf-8') as f:
            for line in f:
                at = _at(line)
                if at >= since and (until is None or at <= until):
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
    except OSError:
        return []
    return out


def offline_ticks(home, since=0.0, until=None):
    """``{probes, offline, offline_s, by_layer}`` over the window — the idle metric's reader.
    ``offline_s`` counts each offline probe as :data:`PROBE_EVERY_S`; ``by_layer`` says what
    failed first, which is the evidence W1-PR3b chooses a recovery from."""
    recs = read_log(home, since, until)
    bad = [r for r in recs if r.get('ok') is False]
    layers = {}
    for r in bad:
        k = r.get('failing') or 'unknown'
        layers[k] = layers.get(k, 0) + 1
    return {'probes': len(recs), 'offline': len(bad), 'offline_s': len(bad) * PROBE_EVERY_S,
            'by_layer': layers}


def doctor_row(home, cfg=None, now=None):
    """``(ok, detail)`` for the doctor's ``network`` row, or None while ``network.probe`` is off
    and no record exists. Red when the last record is offline (and says which layer) or when the
    clock has stopped (the record is older than :data:`STALE_S`)."""
    last = read_latest(home)
    if last is None:
        return None if not enabled(cfg) else (False, 'network.probe is on but no record yet — '
                                                     'asf.host.net-probe has not run')
    t = time.time() if now is None else now
    age = t - float(last.get('at', 0))
    if enabled(cfg) and age > STALE_S:
        return False, f'last probe {int(age // 60)} min ago — the net-probe clock has stopped'
    if last.get('ok') is False:
        return False, log_line(last).replace('net-probe: ', '')
    return True, f"reachable ({int(age)} s ago)"


def cmd_net_probe(args):
    from asf import env
    cfg = env.load_config()
    rec = watchdog(env.ASF_HOME, cfg, force=bool(getattr(args, 'once', False)))
    if rec is None:
        print('net-probe: network.probe is off (config.yaml) — nothing probed; --once probes anyway')
        return 0
    print(log_line(rec), flush=True)
    return 0


def register(sub):
    p = sub.add_parser('net-probe', help='probe how this host reaches the forge, once')
    p.add_argument('--once', action='store_true',
                   help='probe now even when network.probe is off, and print the record')
    p.set_defaults(run=lambda args: cmd_net_probe(args))
    return p
