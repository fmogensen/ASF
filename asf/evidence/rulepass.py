"""asf.evidence.rulepass — the disputes code rules before any adjudicate session is spawned.

An adjudicate session is the most expensive row the feeder mints and the only one with no round
after it. Most of them rule on a cosmetic review demand (squash the commits, a wording
preference) or on a point an existing decision or a standing ruling already answers — neither is
judgement. This module is the one table of those disputes, applied by the wave
(:func:`asf.tick.step_wave.screen`) *before* it spawns anything:

* ``cosmetic``  → every open finding is a demand about commit shape, wording or formatting, and
  nothing in :data:`NEVER_WAIVE` matches. Overruled; nothing on the branch changes.
* ``precedent`` → every open finding is covered by a standing ruling on this item
  (:func:`asf.evidence.rulings.covers`) or names a decision the register confirms
  (:func:`asf.evidence.precedent.entries`). Overruled, with the citation.

A dispute is waived only when *every* open finding is (:func:`rule`) — one unwaived finding and
the whole dispute goes to the session unchanged. The ruling is one sentence per finding naming
that finding's own file, because that is what the landed reader covers and no more (C4): a
genuinely new C item in a later round still blocks.

The only write is the ruling, filed on the card exactly as
:func:`asf.tick.step_health.file_rulings` writes a session's — so the lane's merge gate
(:func:`asf.evidence.rulings.reraised_only`), every later brief
(:func:`asf.evidence.rulings.brief_section`) and this module's own idempotence
(:func:`waived`) read it back with no change to any of them.

Default off: ``conventions.flags.rule_pass`` (:func:`enabled`), the ``mechanical`` shape.
"""
import dataclasses
import os
import re
import types

from asf.evidence import precedent as precedent_mod
from asf.evidence import review
from asf.evidence import rulings
from asf.feeder import rows as feeder_rows
from asf.harvest import mechanical
from asf.record import decisions
from asf.tick import shadow
from asf.workers import lifecycle, pool

#: ``conventions.flags.<FLAG>`` turns the table on; anything but an "on" word is off.
FLAG = 'rule_pass'
#: The job every later reader sees on the card's ``## History`` line (P4).
JOB = 'rule-pass'
#: The event name written on the tick when a dispute is ruled.
EVENT = 'rule_pass'
#: What "on" means — the one list :func:`asf.harvest.mechanical.enabled` already reads.
ON_WORDS = mechanical.ON_WORDS


def enabled(product):
    """``conventions.flags.rule_pass`` is on (``on``/``true``/``yes``/``1``); default off."""
    if product is None:
        return False
    flag = getattr(product, 'flag', None)
    if flag is None:
        conv = getattr(product, 'conventions', None)
        flag = getattr(conv, 'flag', None)
    value = flag(FLAG, 'off') if flag is not None else 'off'
    return value is True or str(value).strip().lower() in ON_WORDS


@dataclasses.dataclass(frozen=True)
class Dispute:
    """What is disputed, for the two adjudicate rows that have a finding (P1)."""
    item: str          # the card the ruling is filed on
    branch: str
    finding: tuple     # lifecycle.finding_of's key — the C list's files (P11)
    entries: tuple     # [(label, text)] from rulings.c_entries
    where: str         # 'correction' | 'review-round', for the line and the event


#: A finding about how the commits are arranged, not about the work in them.
COMMIT_SHAPE = re.compile(
    r'\bsquash\b|\bfixup\b|\bamend\b|\breword\b|\bone commit\b|commit\s+(?:message|subject)'
    r'|split the commit', re.I)
#: A finding about prose: a word, not a behaviour.
WORDING = re.compile(
    r'\btypo\b|\bspelling\b|\bgrammar\b|\bwording\b|\bphrasing\b|\brephrase\b'
    r'|capitalisation|capitalization|\bpunctuation\b|\bthe word\b|should\s+say', re.I)
#: A finding about layout.
FORMATTING = re.compile(
    r'blank lines?|trailing whitespace|line length|\bcolumns\b|\bindentation\b'
    r'|import order|alphabeti[sz]e', re.I)
COSMETIC = (('commit-shape', COMMIT_SHAPE), ('wording', WORDING), ('formatting', FORMATTING))
#: One sentence per cosmetic class, each ending in a full stop (PD9).
CLASS_WHY = {
    'commit-shape': 'A commit-shape demand is about how the commits are arranged, not about the '
                     'work in them.',
    'wording': 'A wording demand is about a word, not a behaviour.',
    'formatting': 'A formatting demand is about layout, not a behaviour.',
}

#: What no rule waives, whatever else matches. The first four are the adjudicate brief's own
#: ("FOUR THINGS ARE NEVER YOURS", P14); the rest are the words that make a finding a claim about
#: behaviour rather than about appearance.
LICENCE = re.compile(r'licen[cs]e|copyright|\bnotice\b|attribution', re.I)
MONEY = re.compile(r'\bcost\b|\bspend\b|\bbudget\b|\bbilling\b|\bprice\b|\bquota\b|\busd\b', re.I)
SECURITY = re.compile(
    r'security|credential|secret|\btoken\b|password|\bauth\b|injection|redact|CVE', re.I)
CUSTOMER = re.compile(
    r'customer|user-visible|\bUI\b|API contract|breaking change|migration|deprecat', re.I)
CORRECTNESS = re.compile(
    r'\bbug\b|defect|\bwrong\b|incorrect|\bcrash\b|\braise\b|exception|regression|\brace\b'
    r'|\bleak\b|does not|never returns', re.I)
COVERAGE = re.compile(r'no test|untested|does not cover|not pinned', re.I)
NEVER_WAIVE = (LICENCE, MONEY, SECURITY, CUSTOMER, CORRECTNESS, COVERAGE)


def classify(text):
    """``'commit-shape'`` | ``'wording'`` | ``'formatting'`` for a finding about appearance,
    else ``''``. :data:`NEVER_WAIVE` beats every cosmetic match (C6, P14): the two errors are
    not symmetric — a false negative costs one adjudicate session, today's cost, and a false
    positive files a binding ruling over a real defect."""
    t = text or ''
    if any(rx.search(t) for rx in NEVER_WAIVE):
        return ''
    return next((name for name, rx in COSMETIC if rx.search(t)), '')


@dataclasses.dataclass(frozen=True)
class Ruling:
    """What the table ruled: the rule names that fired, in finding order (``rules``); the
    paragraph — one sentence per finding (C4, ``text``); the citations for the ``[precedent:
    …]`` suffix (``cites``); the key this ruling waives (C7, ``finding``); the operator line
    (``why``); and ``'correction' | 'review-round'`` (``where``), for the event (PD4)."""
    rules: tuple
    text: str
    cites: tuple
    finding: tuple
    why: str
    where: str


def standing(product, item):
    """The standing rulings of ``item``, read off the card under the record clone and under
    ``product.backlog_dir``, concatenated (PD6): a failed push or a dirty operator checkout
    leaves one empty, never both, and the pass stays idempotent either way. Neither read copies
    :func:`asf.evidence.rulings._card_file` or changes :mod:`asf.evidence.rulings` (Out)."""
    record = types.SimpleNamespace(backlog_dir=shadow.record_dir(product))
    backlog = types.SimpleNamespace(backlog_dir=getattr(product, 'backlog_dir', None))
    return rulings.standing(record, item) + rulings.standing(backlog, item)


def cosmetic(dispute, label, text):
    """``('cosmetic', <sentence>, '')`` when :func:`classify` names a class, else ``None``. The
    sentence names the finding's own file (C4, P5) when it names one, and is unscoped when the
    finding is unscoped too."""
    cls = classify(text)
    if not cls:
        return None
    m = review.C_PATH_RE.search(text or '')
    over = f' over `{m.group("p")}`' if m else ''
    return ('cosmetic',
            f'{label} is overruled as cosmetic ({cls}){over}: {CLASS_WHY[cls]}', '')


def precedent(product, items, dispute, label, text):
    """``('precedent', <sentence>, <cite>)`` or ``None``: :func:`asf.evidence.rulings.covers`
    over the item's :func:`standing` rulings first — a job back means ``cite`` is that job and
    the stamp of the ruling it came from; else the finding's own text names a ``D-nnnn`` or a
    path under :data:`asf.record.decisions.DOCS_DIR` that :func:`asf.evidence.precedent.entries`
    carries, confirmed with a status outside :data:`asf.evidence.precedent.STALE_STATUS`,
    compared lower-cased (PD15) — then ``cite`` is ``brief_section``'s own row grammar. No
    lexical or semantic guess, ever (Out)."""
    rulings_list = standing(product, dispute.item)
    job = rulings.covers(rulings_list, label, text)
    if job:
        at = next((r['at'] for r in reversed(rulings_list) if r['job'] == job), '')
        cite = f'{job} {at}'.strip()
        return ('precedent', f'{label} is already settled by a standing ruling ({cite}).', cite)
    rows = precedent_mod.entries(product, items)
    ids = decisions.ID_RE.findall(text or '')
    if ids:
        row = next((r for r in rows if r['id'] == ids[0]), None)
    else:
        row = next((r for r in rows if r['path'] and r['path'] in (text or '')), None)
    if not row:
        return None
    status = (row.get('status') or '').strip().lower()
    if not status or status in precedent_mod.STALE_STATUS:
        return None
    cite = f"{row['id']} — {row['title']} · {row['path'] or 'the record'}"
    return ('precedent', f'{label} is already settled: {cite}.', cite)


def rule(product, dispute, items=None):
    """A :class:`Ruling` when **every** open finding of ``dispute`` is waived, else None —
    :func:`asf.evidence.rulings.reraised_only`'s own rule (C5): one unwaived finding and the
    whole dispute goes to the session, unchanged, and nothing is written."""
    names, sentences, cites = [], [], []
    for label, text in dispute.entries:
        hit = cosmetic(dispute, label, text) or precedent(product, items, dispute, label, text)
        if not hit:
            return None
        name, sentence, cite = hit
        names.append(name)
        sentences.append(sentence)
        if cite:
            cites.append(cite)
    why = f'rule pass: {", ".join(dict.fromkeys(names))} — ruled, no session'
    return Ruling(rules=tuple(names), text=' '.join(sentences), cites=tuple(cites),
                  finding=tuple(dispute.finding), why=why, where=dispute.where)


def history_line(ruling, now=None):
    """The ``## History`` line, in ``file_rulings``' own shape with two bracketed suffixes:

        - 2026-10-08 10:30 adjudicate (rule-pass): <ruling.text> [rule: cosmetic; finding: asf/flake.py] [precedent: …]

    ``JOB = 'rule-pass'`` is the job every later reader sees — ``rulings.HISTORY_RULING_RE``
    binds it as ``[^)\\s]+`` and ``covers`` answers with it (P4), so a review the gate reads says
    in one word that a rule, not a session, settled it."""
    stamp = (now or pool.now_iso())[:16].replace('T', ' ')
    cite = ', '.join(ruling.cites) or 'none'
    return (f'- {stamp} adjudicate ({JOB}): {" ".join(ruling.text.split())} '
            f'[rule: {", ".join(ruling.rules)}; finding: {" ".join(ruling.finding)}] '
            f'[precedent: {cite}]')


#: A rule-pass line's own ``finding: …`` clause, inside the ``[rule: …; finding: …]`` bracket.
_FINDING_SUFFIX_RE = re.compile(r'finding:\s*(?P<finding>[^\]]*)\]')


def waived(product, item, finding):
    """The stamp of the rule-pass ruling that already waived this exact key, or ``''``:
    :func:`standing` filtered to ``job == JOB``, its ``[finding: …]`` suffix parsed and compared
    to ``finding`` as a set (C7)."""
    want = set(finding or ())
    matches = []
    for r in standing(product, item):
        if r['job'] != JOB:
            continue
        m = _FINDING_SUFFIX_RE.search(r['text'])
        if m and set(m.group('finding').split()) == want:
            matches.append(r['at'])
    return max(matches) if matches else ''


def dispute(product, row, items=None):
    """What is disputed about ``row`` (:class:`Dispute`), for the two rows that have a finding
    (P1): a pending correction at the round cap, or a stage review-round stalled past
    :func:`asf.feeder.rows.stalemate_round`. ``None`` for every other adjudicate row — the
    attempts-exhausted ones (Out) and any row whose review cannot be read. May raise on an
    unreadable review; :func:`apply` and :func:`asf.tick.step_wave.waivers` are the ones that
    catch it."""
    item = getattr(row, 'item_id', None)
    branch = getattr(row, 'branch', None) or ''
    if not item:
        return None
    path = pool.sessions_path(product)
    corr = lifecycle.correction_of(path, item)
    where = None
    if corr and corr.get('kind') == lifecycle.REVIEW \
            and lifecycle.repeats(corr) >= lifecycle.round_cap():
        where = 'correction'
    else:
        doc, rnd = feeder_rows.review_round((items or {}).get(item) or {})
        if doc and rnd >= feeder_rows.stalemate_round(product):
            where = 'review-round'
    if not where:
        return None
    rv = review.review_at(getattr(product, 'repo_dir', None), product.conventions,
                          f'origin/{branch}', item)
    body = (rv or {}).get('body') or ''
    entries = tuple(rulings.c_entries(body))
    if not entries:
        return None
    keys = tuple(corr.get('finding') or ()) if where == 'correction' and corr else ()
    finding = tuple(lifecycle.finding_of(lifecycle.REVIEW, body, keys=keys or review.c_items(body)))
    return Dispute(item=item, branch=branch, finding=finding, entries=entries, where=where)


def _file_ruling(root, item, ruling):
    """Files ``ruling`` on ``item``'s card under ``root``, exactly as
    :func:`asf.tick.step_health.file_rulings` writes a session's."""
    from asf.record.core import TYPES
    from asf.record import frontmatter
    from asf.record.ingest import append_history_lines
    folder = next((f for _t, (f, p) in TYPES.items() if item.startswith(p + '-')), None)
    if not folder:
        return
    path = os.path.join(root, folder, f'{item}.md')
    if not os.path.isfile(path):
        return
    with open(path, encoding='utf-8') as f:
        meta, body = frontmatter.parse(f.read(), path=path)
    new_body = append_history_lines(body, [history_line(ruling)])
    with open(path, 'w', encoding='utf-8') as f:
        f.write(frontmatter.render(meta, new_body))


def apply(product, row, items, out, root=None, dry_run=False):
    """Total: :func:`enabled` → :func:`dispute` → :func:`waived` (already ruled: stand down,
    quiet) → :func:`rule` → the card write, or, under ``dry_run`` or with no ``root``, the same
    answer with nothing written (PD5). Any exception inside is one line and a stand-down — a
    rule pass that cannot read a review never holds a row the wave would otherwise launch."""
    try:
        if not enabled(product):
            return None
        d = dispute(product, row, items)
        if not d:
            return None
        if waived(product, d.item, d.finding):
            return None
        ruling = rule(product, d, items)
        if not ruling:
            return None
        if root and not dry_run:
            _file_ruling(root, d.item, ruling)
        return ruling
    except Exception as exc:  # noqa: BLE001 — no ruling is no stand-down, never an exception
        out(f'rule pass {getattr(row, "item_id", "?")}: could not rule ({exc}) — not ruled')
        return None
