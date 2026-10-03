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
"""
import socket
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
