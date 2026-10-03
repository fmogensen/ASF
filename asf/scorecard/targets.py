"""asf.scorecard.targets — ``asf scorecard --check <targets.yaml>``: each program target read
against the program row (:mod:`asf.scorecard.program`), and the ones it misses.

A targets file::

    wave: A
    targets:
      - key: cardless_heavy_reviews     # a program-row key; a dotted path reaches inside
        op: '=='                        # ==, <=, <, >=, >, or measured (the key is read at all)
        value: 0
        why: model routing (W2-PR1)

``measured`` holds when the row carries the key — a wave whose numbers are a baseline, not a
threshold, still proves its keys exist. Any other op against a key the row could not read
(``None``) is a miss: an unread number never meets a target.
"""
import operator

OPS = {'==': operator.eq, '<=': operator.le, '<': operator.lt, '>=': operator.ge,
       '>': operator.gt, 'measured': None}
_MISSING = object()


class TargetsError(ValueError):
    pass


def parse(data):
    """The targets file's mapping → ``[{key, op, value, why}]``; :class:`TargetsError` on a
    malformed entry."""
    if not isinstance(data, dict) or not isinstance(data.get('targets'), list):
        raise TargetsError('a targets file is a mapping with a `targets:` list')
    out = []
    for n, t in enumerate(data['targets'], 1):
        if not isinstance(t, dict) or not isinstance(t.get('key'), str) or not t['key']:
            raise TargetsError(f'target {n}: needs a `key:`')
        op = str(t.get('op') or 'measured')
        if op not in OPS:
            raise TargetsError(f"target {n} ({t['key']}): op {op!r} is not one of {', '.join(OPS)}")
        if op != 'measured' and not isinstance(t.get('value'), (int, float)):
            raise TargetsError(f"target {n} ({t['key']}): op {op} needs a numeric `value:`")
        out.append({'key': t['key'], 'op': op, 'value': t.get('value'), 'why': t.get('why') or ''})
    return out


def lookup(row, key):
    cur = row
    for part in key.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def check(row, targets):
    """``[(target, value, ok)]`` per target, in file order."""
    out = []
    for t in targets:
        v = lookup(row, t['key'])
        if t['op'] == 'measured':
            ok = v is not _MISSING
        else:
            ok = (isinstance(v, (int, float)) and not isinstance(v, bool)
                  and OPS[t['op']](v, t['value']))
        out.append((t, None if v is _MISSING else v, ok))
    return out


def render(results, product, start, end):
    lines = [f'scorecard --check {product} {start} → {end}']
    for t, v, ok in results:
        want = 'measured' if t['op'] == 'measured' else f"{t['op']} {t['value']:g}"
        shown = 'not read' if v is None else (format(v, 'g') if isinstance(v, (int, float))
                                              and not isinstance(v, bool) else str(v)[:80])
        lines.append(f"{'ok  ' if ok else 'MISS'} {t['key']} = {shown} (want {want})"
                     + (f" — {t['why']}" if t['why'] else ''))
    missed = sum(1 for _t, _v, ok in results if not ok)
    lines.append(f'{len(results) - missed}/{len(results)} targets met' if results else 'no targets')
    return '\n'.join(lines) + '\n'
