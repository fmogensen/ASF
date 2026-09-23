"""asf.tokens — the meter: four token dimensions, never summed, read in one pass, held to one cap table.

A session's spend is four separate numbers — input, output, cache read, cache write — and the
sum of them is what hid 11.6 M input tokens inside one run of the prototype. So this module owns
the one map from a usage object's wire keys to the four dimension names, one pass over a job log
that returns the run's result *and* its per-dimension tally, the cap table (`token_caps:` in the
product file over a built-in default) and the verdict on a tally against a cap. It is the only
module that reads a ``usage`` key, and there is no fifth number anywhere in it.

A leaf: it imports the stdlib only (``asf.env`` is the one sibling it may ever read), so both
``asf.metrics`` and ``asf.workers`` may import it.
"""
import json
import time
from dataclasses import dataclass, field

DIMENSIONS = ('input', 'output', 'cache_read', 'cache_write')

#: dimension -> the key a usage object carries it under (D1).
USAGE_KEYS = {'input': 'input_tokens', 'output': 'output_tokens',
              'cache_read': 'cache_read_input_tokens',
              'cache_write': 'cache_creation_input_tokens'}

#: runaway ceilings per run, not budgets (D12). `token_caps:` narrows or widens them.
DEFAULT_CAPS = {'default': {'input': 8_000_000, 'output': 1_000_000,
                            'cache_read': 400_000_000, 'cache_write': 40_000_000}}

STOP_GRACE_S = 10

#: the `sessions` stream's job kinds (D5) — a copy of `metrics.KINDS`, because this module is a
#: leaf and may not import it; `tests.test_tokens.CapsTest` holds the two equal.
KINDS = ('spec', 'review', 'fix', 'code', 'plan', 'preflight', 'probe', 'rebase', 'relaunch', 'launch',
         'tick', 'other')

OFF = 'off'


class TokenCapError(Exception):
    """A malformed ``token_caps:`` block: the text names every bad entry (D13)."""


def _empty():
    return {d: None for d in DIMENSIONS}


def _count(v):
    """A usage value as a count: an int (never a bool) that is not negative, else None (D4)."""
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def usage_of(rec):
    """The four dimensions of one stream-json record: ``{dimension: int|None}``.

    ``rec['message']['usage']`` for an ``assistant`` line, ``rec['usage']`` for a ``result`` line,
    nothing for any other. A key that is missing, not a number or negative is ``None`` for that
    dimension — never ``0``, which is a measurement (D4).
    """
    u = {}
    if isinstance(rec, dict):
        kind = rec.get('type')
        if kind == 'assistant':
            msg = rec.get('message')
            u = msg.get('usage') if isinstance(msg, dict) else {}
        elif kind == 'result':
            u = rec.get('usage')
    if not isinstance(u, dict):
        u = {}
    return {d: _count(u.get(USAGE_KEYS[d])) for d in DIMENSIONS}


def of_result(rec):
    """``usage_of`` for a caller that already holds a result record and wants no second pass."""
    return usage_of(rec)


@dataclass
class Meter:
    result: dict = None
    by_dim: dict = field(default_factory=_empty)
    lines: int = 0
    runs: int = 0


def _fold(tally, usage):
    for d in DIMENSIONS:
        n = usage[d]
        if n is not None:
            tally[d] = n if tally[d] is None else tally[d] + n


def meter(log_path):
    """One pass over a job log: the last run's result and its tally. Never raises.

    A ``system``/``init`` line is the run boundary (as in ``runtime.read_result``, B-0028): it drops
    the result and the tally. An ``assistant`` line adds each dimension it carries; a ``result`` line
    is kept. At the end the result's own usage, when it carries any of the four, replaces the tally
    wholesale — the runtime's count wins over the sum of turns, and nothing adds the two (D2).
    """
    m = Meter()
    try:
        with open(log_path, encoding='utf-8', errors='replace') as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                m.lines += 1
                kind = rec.get('type')
                if kind == 'system' and rec.get('subtype') == 'init':
                    m.result = None
                    m.by_dim = _empty()
                    m.runs += 1
                elif kind == 'assistant':
                    _fold(m.by_dim, usage_of(rec))
                elif kind == 'result':
                    m.result = rec
    except (OSError, ValueError):
        return Meter()
    if m.result is not None:
        own = usage_of(m.result)
        if any(n is not None for n in own.values()):
            m.by_dim = own
    return m


# ---- the cap table ----------------------------------------------------------

def _block(product):
    return product._get('token_caps') if product is not None else None


def caps(product):
    """``{kind: {dimension: int|None}}``: ``DEFAULT_CAPS`` under the product's ``token_caps:``.

    ``None`` is uncapped (``off``). Every bad entry is collected before anything is raised, so one
    ``TokenCapError`` names all of them (D13).
    """
    block = _block(product)
    if block is None:
        return {k: dict(v) for k, v in DEFAULT_CAPS.items()}
    if not isinstance(block, dict):
        raise TokenCapError('token_caps must be a map of job kind: {dimension: cap}')
    entries = {str(k): v for k, v in block.items()}
    bad = [f'unknown job kind {k}' for k in entries if k != 'default' and k not in KINDS]
    maps = {k: v for k, v in entries.items() if isinstance(v, dict)}
    bad += [f'unknown dimension {d} in token_caps.{k}'
            for k, row in maps.items() for d in row if d not in DIMENSIONS]
    bad += [f'cap {k}.{d} must be a whole number of tokens or off, got {v!r}'
            for k, row in maps.items() for d, v in row.items()
            if d in DIMENSIONS and v != OFF and (not isinstance(v, int) or isinstance(v, bool) or v < 1)]
    bad += [f'token_caps.{k} must be a map of dimension: cap' for k, v in entries.items() if k not in maps]
    if bad:
        raise TokenCapError('; '.join(bad))

    def resolve(base, row):
        out = dict(base)
        for d, v in (row or {}).items():
            out[d] = None if v == OFF else v
        return out

    default = resolve(DEFAULT_CAPS['default'], maps.get('default'))
    table = {'default': default}
    for k, row in maps.items():
        if k != 'default':
            table[k] = resolve(default, row)
    return table


def cap_for(product, kind):
    """The kind's own cap per dimension, else ``default``'s, else ``None`` (uncapped)."""
    table = caps(product)
    return dict(table.get(kind) or table.get('default') or _empty())


def over(by_dim, cap):
    """``(dimension, tokens, limit)`` for the first dimension, in ``DIMENSIONS`` order, whose tally
    is a number strictly over its cap; ``None`` when none is. A ``None`` on either side is never
    over. Dimension order is the tie-break, so the verdict is deterministic."""
    for d in DIMENSIONS:
        n, limit = by_dim.get(d), cap.get(d)
        if isinstance(n, int) and isinstance(limit, int) and n > limit:
            return d, n, limit
    return None


def cap_text(kind, dimension, tokens, limit):
    return f'token cap: {dimension} {tokens} over the {limit} cap for a {kind} job'


def cap_result(kind, dimension, tokens, limit, by_dim, at):
    """The result record the factory appends to a capped run's log (D8, D9).

    The judgement rests on the structured ``asf.cap`` object, which only the factory writes; the
    text says the same for a human. No ``duration_ms`` and no ``total_cost_usd``: nothing measured
    either, and a dimension that was never counted is left out of ``usage``, not written as 0.
    """
    return {
        'type': 'result', 'subtype': 'error', 'is_error': True,
        'result': cap_text(kind, dimension, tokens, limit) + ' — stopped by the factory',
        'usage': {USAGE_KEYS[d]: by_dim[d] for d in DIMENSIONS if by_dim.get(d) is not None},
        'asf': {'cap': {'kind': kind, 'dimension': dimension, 'tokens': tokens, 'limit': limit,
                        'at': at or time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}},
    }
