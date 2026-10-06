"""asf.connectors.quota — the ``quota`` connectors: an account's usage for the quota guard.

* ``none`` (default) — nothing to read: every account reads 0/0, so every account is available.
* ``command`` — a shell command, ``{account}`` substituted, whose last non-empty stdout line is
  the usage as JSON (``{"five_h_pct": n, "seven_d_pct": n, …}``); any failure is None (the
  account is then not under the guard). Configured as ``connectors.quota: {command: "<cmd>"}``,
  or by the older ``worker_pool.quota_command: "<cmd>"``, which is still read.
* ``fake`` — a table (tests).

The implementations are :mod:`asf.workers.quota`'s sources; this module only builds them.
"""
from asf.workers import quota as quota_mod


def command_line(cfg):
    """The configured command: ``connectors.quota.command``, else ``worker_pool.quota_command``."""
    spec = ((cfg or {}).get('connectors') or {}).get('quota')
    if isinstance(spec, dict) and spec.get('command'):
        return str(spec['command']), spec
    return ((cfg or {}).get('worker_pool') or {}).get('quota_command'), {}


def none(cfg=None):
    return quota_mod.NoQuotaSource()


def command(cfg=None):
    cmd, spec = command_line(cfg)
    if not cmd:
        return quota_mod.NoQuotaSource()
    if spec.get('timeout_s'):
        return quota_mod.CommandQuotaSource(cmd, timeout=float(spec['timeout_s']))
    return quota_mod.CommandQuotaSource(cmd)


def fake(cfg=None):
    return quota_mod.FakeQuotaSource()
