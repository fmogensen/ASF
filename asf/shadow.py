"""asf.shadow — the one decision ledger every code decider writes while it runs beside a session.

A decider that replaces a model session (or an older code path) runs first beside it:
``conventions.flags.<flag>: shadow`` computes the code's answer, the incumbent still decides, and
**every** decision — an agreement as much as a disagreement — goes to
``state/<product>/shadow-<decider>.jsonl`` (:func:`decide`)::

    {"ts": "…Z", "type": "decision", "decider": "facts", "case": "trunkclose|T-0001|…",
     "stratum": "trunkclose", "code": …, "incumbent": …, "verdict": "same" | "diff" | "pending",
     "code_says": "landed", "item": "T-0001"}

``pending`` is a decision whose other half is not known yet; a later :func:`decide` on the same
``case`` settles it. A decision can also be judged against its **outcome** — what happened after
(a close reopened within ``shadow.outcome_days``, say): :func:`outcome` writes a
``{"type": "outcome", "case": …, "verdict": "same" | "diff"}`` line, and an outcome verdict beats
the incumbent's, because agreement with a noisy incumbent is not correctness.

**The count** (:func:`tally`) is over DISTINCT cases, never lines: a question asked again on the
same evidence is one case, so ``N`` is the number of independent trials, and an identical repeat
of a case's last decision is not written again. With ``k`` wrong of ``N``, the report carries the
one-sided Clopper-Pearson upper bound on the error rate at ``shadow.confidence`` (95%: about
``3/N`` at ``k = 0``, the rule of three) — :func:`upper_bound`. Counts are per ``stratum`` too:
a pass on one question type never vouches for another, and the ledger is per product.

**The cutover** is the operator's. ``asf deciders`` shows each decider's mode (``off`` /
``shadow`` / ``on``), its N, diffs, outcome-settled cases, the bound and the ``N`` it needs
(``shadow.min_n.<decider>``). A decider that closes, removes or pushes (:attr:`Decider.acts`) is
**manual-flip only**: nothing in ASF ever turns it on (:func:`auto_cutover` is False), the flag
moves by hand and only after the evidence. Writing the ledger never raises into a decider — a
line that cannot be written is one stderr line.
"""
import dataclasses
import datetime
import json
import math
import sys

from asf.state import store

PREFIX, SUFFIX = 'shadow-', '.jsonl'
OFF, SHADOW, ON = 'off', 'shadow', 'on'
MODES = (OFF, SHADOW, ON)
ON_WORDS = ('on', 'true', 'yes', '1')
SAME, DIFF, PENDING = 'same', 'diff', 'pending'
DECISION, OUTCOME = 'decision', 'outcome'
#: The side effects that make a decider manual-flip only.
IRREVERSIBLE = ('closes', 'removes', 'pushes', 'merges')
#: ``config.yaml shadow:`` defaults.
DEFAULTS = {'confidence': 0.95, 'outcome_days': 7, 'min_n': 300}


@dataclasses.dataclass(frozen=True)
class Decider:
    """A code decider with a shadow: its flag, how the flag's words map onto off/shadow/on, what
    it does when on (``acts``), and the distinct cases it needs before a flip is argued."""
    name: str
    flag: str
    acts: str = ''
    min_n: int = DEFAULTS['min_n']
    words: tuple = ()  # extra (word, mode) pairs of a flag with its own vocabulary
    what: str = ''


#: Every decider that runs in shadow. ``min_n`` is the default; ``shadow.min_n.<name>`` overrides.
DECIDERS = {
    'facts': Decider('facts', 'facts', acts='closes', min_n=300,
                     words=(('old', OFF), ('new', ON)),
                     what='the landing fact beside the closing deciders'),
    'mechanical': Decider('mechanical', 'mechanical', acts='pushes', min_n=40,
                          what='code repair of conflict/copies/naming/unpushed/hook-refused'),
    'roots': Decider('roots', 'roots', acts='removes', min_n=100,
                     what='unverified-landing answers and Resolved resets'),
}


def auto_cutover(decider):
    """Never for a decider that closes, removes, merges or pushes: a human flips it."""
    d = DECIDERS.get(decider) if isinstance(decider, str) else decider
    return d is not None and d.acts not in IRREVERSIBLE


# ---- settings ----------------------------------------------------------------------------------

def settings(cfg=None):
    """``config.yaml shadow:`` over :data:`DEFAULTS`: ``confidence``, ``outcome_days`` and
    ``min_n`` (a number for every decider, or a map ``{<decider>: n}``)."""
    if cfg is None:
        from asf import env
        try:
            cfg = env.load_config()
        except Exception:  # noqa: BLE001 — no config is the defaults
            cfg = {}
    block = (cfg or {}).get('shadow')
    block = block if isinstance(block, dict) else {}
    out = dict(DEFAULTS)
    out['min_n'] = {}
    try:
        c = float(block.get('confidence', DEFAULTS['confidence']))
        out['confidence'] = c if 0 < c < 1 else DEFAULTS['confidence']
    except (TypeError, ValueError):
        pass
    try:
        out['outcome_days'] = max(0, int(block.get('outcome_days', DEFAULTS['outcome_days'])))
    except (TypeError, ValueError):
        pass
    raw = block.get('min_n')
    for name, d in DECIDERS.items():
        v = raw.get(name) if isinstance(raw, dict) else raw
        try:
            out['min_n'][name] = int(v) if v is not None and not isinstance(v, bool) else d.min_n
        except (TypeError, ValueError):
            out['min_n'][name] = d.min_n
    return out


# ---- the mode ----------------------------------------------------------------------------------

def _flag_reader(product):
    if product is None:
        return None
    flag = getattr(product, 'flag', None)
    if flag is None:
        flag = getattr(getattr(product, 'conventions', None), 'flag', None)
    return flag if callable(flag) else None


def mode(product, decider, default=OFF):
    """The decider's ``conventions.flags.<flag>`` as ``off`` / ``shadow`` / ``on``: an "on" word
    (``on``/``true``/``yes``/``1``, True, or the decider's own word for on) is ``on``, ``shadow``
    is ``shadow``; unset, ``off``, False or a typo is ``default``."""
    d = DECIDERS.get(decider) if isinstance(decider, str) else decider
    flag = d.flag if d is not None else str(decider)
    read = _flag_reader(product)
    value = read(flag, None) if read is not None else None
    if value is True:
        return ON
    if value is False or value is None:
        return default if value is None else OFF
    text = str(value).strip().lower()
    own = dict(d.words) if d is not None else {}
    if text in own:
        return own[text]
    if text in ON_WORDS:
        return ON
    if text in (SHADOW, OFF):
        return text
    return default


# ---- writing -----------------------------------------------------------------------------------

def name(decider):
    return f'{PREFIX}{decider}{SUFFIX}'


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _pname(product):
    return getattr(product, 'name', product)


def _canon(value):
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        return repr(value)


#: ``{(product, decider): {case: signature of its last decision}}`` — the repeat filter, seeded
#: from the ledger once per process.
_LAST = {}


def _last(pname, decider):
    key = (pname, decider)
    got = _LAST.get(key)
    if got is None:
        got = {}
        for r in records(pname, decider):
            if r.get('type', DECISION) == DECISION:
                got[r.get('case')] = _sig(r.get('code'), r.get('incumbent'), r.get('verdict'))
        _LAST[key] = got
    return got


def _sig(code, incumbent, verdict):
    return _canon([code, incumbent, verdict])


def _append(pname, decider, rec):
    try:
        store.append(pname, name(decider), rec)
        return True
    except Exception as e:  # noqa: BLE001 — the ledger never takes a decider down
        print(f'SHADOW: {decider} not logged ({type(e).__name__}: {e})', file=sys.stderr,
              flush=True)
        return False


def decide(product, decider, case, *, code=None, incumbent=None, verdict=PENDING, stratum='',
           **extra):
    """Record one decision of ``decider`` on ``case`` — agreement or not. ``case`` names the
    question and the evidence it was asked on, so a repeat is the same case; a decision identical
    to the case's last one is not written again. Returns the record (None when skipped). Never
    raises."""
    try:
        pname = _pname(product)
        case = str(case)
        sig = _sig(code, incumbent, verdict)
        last = _last(pname, decider)
        if last.get(case) == sig:
            return None
        rec = {'ts': _now(), 'type': DECISION, 'decider': decider, 'case': case,
               'stratum': str(stratum or ''), 'code': code, 'incumbent': incumbent,
               'verdict': verdict}
        rec.update(extra)
        if _append(pname, decider, rec):
            last[case] = sig
        return rec
    except Exception as e:  # noqa: BLE001
        print(f'SHADOW: {decider} not logged ({type(e).__name__}: {e})', file=sys.stderr,
              flush=True)
        return None


def outcome(product, decider, case, verdict, *, why='', **extra):
    """Record what happened to ``case`` after its decision: ``same`` (the code's answer held) or
    ``diff`` (it would have been wrong). Never raises."""
    rec = {'ts': _now(), 'type': OUTCOME, 'decider': decider, 'case': str(case),
           'verdict': verdict, 'why': why}
    rec.update(extra)
    _append(_pname(product), decider, rec)
    return rec


# ---- reading -----------------------------------------------------------------------------------

def records(product, decider, since=None):
    """Every record of ``decider`` (oldest first); at or after ``since`` (ISO ``…Z``) when given.
    An unreadable ledger reads as none."""
    try:
        got = store.read(_pname(product), name(decider), default=[]).data or []
    except Exception:  # noqa: BLE001 — a reader of the ledger never fails its caller
        return []
    out = [r for r in got if isinstance(r, dict)]
    return [r for r in out if str(r.get('ts') or '') >= since] if since else out


def cases(product, decider, since=None):
    """``{case: {'decision': newest decision, 'outcome': newest outcome | None}}``."""
    out = {}
    for r in records(product, decider, since):
        c = r.get('case')
        if c is None:
            continue
        slot = out.setdefault(c, {'decision': None, 'outcome': None})
        if r.get('type', DECISION) == OUTCOME:
            if r.get('verdict') in (SAME, DIFF):
                slot['outcome'] = r
        else:
            slot['decision'] = r
    return {c: s for c, s in out.items() if s['decision'] is not None}


def verdict_of(slot):
    """``(verdict, against)``: the outcome's verdict when there is one, else the incumbent's."""
    if slot.get('outcome') is not None:
        return slot['outcome'].get('verdict'), OUTCOME
    v = (slot.get('decision') or {}).get('verdict')
    return v, 'incumbent'


def _binom_cdf(k, n, p):
    if p <= 0:
        return 1.0
    if p >= 1:
        return 1.0 if k >= n else 0.0
    lp, lq = math.log(p), math.log1p(-p)
    total = 0.0
    for i in range(0, k + 1):
        total += math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
                          + i * lp + (n - i) * lq)
    return min(1.0, total)


def upper_bound(k, n, confidence=DEFAULTS['confidence']):
    """The one-sided Clopper-Pearson upper bound on the error rate after ``k`` errors in ``n``
    independent cases (1.0 when ``n`` is 0): the largest ``p`` with ``P(X <= k) >= 1 - conf``.
    At ``k = 0`` it is ``1 - (1 - conf) ** (1 / n)`` — about ``3 / n`` at 95%."""
    if n <= 0:
        return 1.0
    if k >= n:
        return 1.0
    alpha = 1 - confidence
    if k == 0:
        return 1 - alpha ** (1 / n)
    lo, hi = k / n, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if _binom_cdf(k, n, mid) > alpha:
            lo = mid
        else:
            hi = mid
    return hi


def _count(slots, confidence):
    n = diff = by_outcome = pending = 0
    for s in slots:
        v, against = verdict_of(s)
        if v not in (SAME, DIFF):
            pending += 1
            continue
        n += 1
        diff += v == DIFF
        by_outcome += against == OUTCOME
    return {'n': n, 'diff': diff, 'outcome': by_outcome, 'pending': pending,
            'bound': round(upper_bound(diff, n, confidence), 4)}


def tally(product, decider, since=None, cfg=None):
    """The decider's evidence over distinct cases: ``{'n', 'diff', 'outcome', 'pending', 'bound',
    'min_n', 'ready', 'cutover', 'strata': {stratum: {n, diff, outcome, pending, bound}}}``.
    ``ready`` is ``n >= min_n`` with no diff; ``cutover`` is ``manual`` for a decider that closes,
    removes or pushes, else ``eligible`` once ready."""
    s = settings(cfg)
    got = cases(product, decider, since)
    out = _count(got.values(), s['confidence'])
    strata = {}
    for slot in got.values():
        strata.setdefault(slot['decision'].get('stratum') or '', []).append(slot)
    out['strata'] = {k: _count(v, s['confidence']) for k, v in sorted(strata.items())}
    out['min_n'] = s['min_n'].get(decider, DEFAULTS['min_n'])
    out['ready'] = out['n'] >= out['min_n'] and out['diff'] == 0
    out['cutover'] = 'manual' if not auto_cutover(decider) else (
        'eligible' if out['ready'] else 'not yet')
    return out


def ready(product, decider, cfg=None):
    """True when ``decider`` has ``min_n`` distinct settled cases and no diff among them."""
    return tally(product, decider, cfg=cfg)['ready']


# ---- outcome settlement ------------------------------------------------------------------------

def _stamp_ago(days, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return (now - datetime.timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')


def _reopened_after(meta, ts):
    for m in (meta or {}).get('reopened') or []:
        stamp = str(m).split(': ', 1)[0].strip()
        if stamp and stamp >= ts:
            return stamp
    return None


def settle_closes(product, decider, items, *, cfg=None, now=None):
    """Judge a closing decider against what happened: every case whose code said ``landed``, at
    least ``shadow.outcome_days`` old and with no outcome yet, gets one — ``diff`` when its item
    was reopened after the decision (the close would have been false), else ``same``.
    ``items`` is ``{id: card meta}``. A case with no item, or an item the record no longer holds,
    stays with the incumbent's verdict. Returns the outcome records written."""
    s = settings(cfg)
    due = _stamp_ago(s['outcome_days'], now)
    wrote = []
    for case, slot in cases(product, decider).items():
        d = slot['decision']
        if slot['outcome'] is not None or d.get('code_says') != 'landed' or str(d.get('ts')) > due:
            continue
        meta = items.get(d.get('item')) if d.get('item') else None
        if meta is None:
            continue
        hit = _reopened_after(meta, str(d.get('ts') or ''))
        wrote.append(outcome(product, decider, case, DIFF if hit else SAME,
                             why=f'reopened {hit}' if hit else
                             f"not reopened in {s['outcome_days']} d"))
    return wrote


def settle(product, root, cfg=None, now=None, out=print):
    """The daily part: every closing decider's due cases judged against the record at ``root``.
    Prints one line; returns 0."""
    closing = [n for n, d in DECIDERS.items() if d.acts == 'closes' and records(product, n)]
    total = 0
    if closing:
        from asf.record.core import load_items
        by_id, _errors = load_items(root)
        items = {iid: (recs[0] or {}).get('meta') or {} for iid, recs in by_id.items() if recs}
        for name_ in closing:
            total += len(settle_closes(product, name_, items, cfg=cfg, now=now))
    out(f'deciders: {total} case(s) settled against their outcome')
    return 0


# ---- the view ----------------------------------------------------------------------------------

def rows(product, cfg=None):
    """One dict per registered decider: its mode, what it does and its :func:`tally`."""
    out = []
    for n, d in DECIDERS.items():
        t = tally(product, n, cfg=cfg)
        out.append(dict(t, decider=n, flag=d.flag, mode=mode(product, d), acts=d.acts or '-',
                        what=d.what))
    return out


def render(product_name, got):
    lines = [f'**DECIDERS** — {product_name}', '',
             '| Decider | Mode | Acts | Cases (N) | vs outcome | Diffs | Pending | Error ≤ | '
             'Needs N | Cutover |',
             '|---|---|---|---|---|---|---|---|---|---|']
    for r in got:
        lines.append(f"| {r['decider']} | {r['mode']} | {r['acts']} | {r['n']} | {r['outcome']} | "
                     f"{r['diff']} | {r['pending']} | {r['bound']:.1%} | {r['min_n']} | "
                     f"{'manual flip only' if r['cutover'] == 'manual' else r['cutover']} |")
    for r in got:
        for st, t in r['strata'].items():
            if st:
                lines.append(f"  {r['decider']}/{st}: {t['diff']} diffs over {t['n']} "
                             f"(error ≤ {t['bound']:.1%})")
    return '\n'.join(lines) + '\n'


def add_parser(sub):
    p = sub.add_parser('deciders', help='the code deciders: mode, distinct cases, diffs, '
                                         'error bound, cutover')
    p.add_argument('--product')
    p.add_argument('--json', action='store_true')
    p.set_defaults(run=cmd_deciders)
    return p


def cmd_deciders(args):
    from asf import env
    product = env.load_product(getattr(args, 'product', None))
    got = rows(product)
    if getattr(args, 'json', False):
        print(json.dumps(got, indent=1, default=str))
    else:
        print(render(_pname(product), got), end='')
    return 0
