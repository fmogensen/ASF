"""asf.groom.sticky — G1: a groom question answered by a person stays out of the file for
``flags.groom.reask_days`` unless the card's own facts changed since (F-0101 §4). An adjudicator
or operator answer that nothing in the card has moved since is the same question again — the
73 identical T-0042 sessions the evaluation found were partly this, a card asked, answered, and
asked again the very next day because nothing told the groom it had already been settled.

Three flags live here, each read off ``flags.groom.*`` (:meth:`asf.env.Product.flag`) and
nowhere else:

* ``structural`` — ``report`` (default) | ``ask``: whether a Feature-without-Stories or
  Story-without-Tasks line is a question (the old behaviour) or a report line nobody is asked
  (:func:`asf.groom.groom.cmd_groom`).
* ``rank_owner`` — ``code`` (default) | ``adjudicator``: whether a ``rank <n>`` groom answer is
  applied, or left to the feeder's own order.
* ``reask_days`` — the sticky window itself, below.

``off`` turns a flag's rule off; a value its grammar does not recognise takes the default — the
same convention :mod:`asf.workers.loops` reads its caps by.
"""
import datetime
import hashlib
import json
import os
import re

from asf.record.core import ID_DIGITS

#: Fields the groom applier itself writes (:func:`asf.groom.groom.apply_groom_answers`) — a
#: card's digest excludes them, since changing only one of these is groom answering its own
#: question, never a new fact to ask about again.
GROOM_WRITTEN = ('decided', 'removed', 'reconciled', 'rank', 'parent', 'severity', 'blockedBy',
                 'budget_sessions', 'budget_usd', 'landed', 'reshape')

#: ``flags.groom.reask_days`` unset: days a sticky answer holds its question out of the file.
DEFAULT_REASK_DAYS = 7

#: The groom sections asking one question — "decide this card" — grouped as one sticky key, so
#: answering it under any one of them answers it under every other (a card the same day both
#: fresh in ``inbox`` and old enough for ``undecided3`` is one question, not two).
_DECIDE_GROUP = 'decide'
_GROUPS = {'inbox': _DECIDE_GROUP, 'undecided_new': _DECIDE_GROUP, 'undecided3': _DECIDE_GROUP,
          'undecided14': _DECIDE_GROUP}

_OFF = ('off', 'false', 'no', 'none', '0')
_HISTORY_RE = re.compile(r'^## History\b', re.M)
LEDGER_NAME = 'groom-answered.json'


def section_group(section):
    """The sticky ledger's key for a groom section: ``decide`` for the sections that all ask
    "decide this card" (:data:`_GROUPS`), else the section itself — a ``no_stories`` answer says
    nothing about whether the card is also over budget."""
    return _GROUPS.get(section, section or '')


def digest(meta, body):
    """A short digest of the card's own facts: its typed fields (:func:`asf.record.frontmatter.
    split_machine`) minus what groom itself writes (:data:`GROOM_WRITTEN`), and its body minus
    ``## History`` onward — so a card groom answered yesterday, which only gained a History
    line since, digests the same today."""
    from asf.record import frontmatter
    typed, _machine = frontmatter.split_machine(meta or {})
    facts = {k: v for k, v in typed.items() if k not in GROOM_WRITTEN}
    without_history = _HISTORY_RE.split(body or '', maxsplit=1)[0]
    raw = json.dumps(facts, sort_keys=True, default=str) + '\n' + without_history.strip()
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def _flag(product, name, default):
    value = product.flag(f'groom.{name}') if product is not None else None
    return value if value is not None else default


def reask_days(product):
    """``flags.groom.reask_days`` (default :data:`DEFAULT_REASK_DAYS`); ``off`` disables the
    sticky rule; anything that is not a positive integer takes the default."""
    value = _flag(product, 'reask_days', DEFAULT_REASK_DAYS)
    if value is False or str(value).strip().lower() in _OFF:
        return None
    try:
        n = int(str(value).strip())
    except ValueError:
        return DEFAULT_REASK_DAYS
    return n if n > 0 else DEFAULT_REASK_DAYS


def rank_owner(product):
    """``flags.groom.rank_owner``: ``code`` (default) | ``adjudicator``."""
    v = str(_flag(product, 'rank_owner', 'code') or 'code').strip().lower()
    return v if v == 'adjudicator' else 'code'


def structural(product):
    """``flags.groom.structural``: ``report`` (default) | ``ask``."""
    v = str(_flag(product, 'structural', 'report') or 'report').strip().lower()
    return v if v == 'ask' else 'report'


#: An open ``_card_line`` (:func:`asf.groom.groom._card_line`): the checkbox and the trailing
#: ``→ answer: ____`` slot, both dropped by :func:`to_report_line`.
_CARD_LINE_RE = re.compile(rf'^- \[ \]\s+(?P<id>[A-Z]-{ID_DIGITS})\s+(?P<rest>.*?)\s*→\s*answer:\s*____$')


def to_report_line(line):
    """``- <id> <title> — <why> (report)`` for an open question line — no checkbox, no answer
    slot — so :data:`asf.groom.policy.OPEN_QUESTION_RE` never matches it and no adjudicator or
    operator is ever asked. A line that is not an open question (already answered, barred, or
    not this shape) comes back unchanged."""
    m = _CARD_LINE_RE.match(line)
    return f"- {m.group('id')} {m.group('rest')} (report)" if m else line


def ledger_path(state_dir):
    return os.path.join(state_dir, LEDGER_NAME)


def load_ledger(state_dir):
    """``{<id>|<group>: {'digest': ..., 'at': ...}}``, or ``{}`` when the file is missing or
    unreadable — a corrupt ledger asks every question again rather than refusing to groom."""
    path = ledger_path(state_dir)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_ledger(state_dir, data):
    os.makedirs(state_dir, exist_ok=True)
    path = ledger_path(state_dir)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, sort_keys=True, indent=1)
        f.write('\n')
    os.replace(tmp, path)


def record_answers(state_dir, answered, canonical, now=None):
    """Writes the ledger with each ``(iid, section, by)`` of ``answered`` whose ``by`` is not a
    ``rule:`` (a controller policy's own answer is not a person's, and is never sticky) — one
    entry per ``<id>|<group>``, the card's current digest and now, keyed by
    :func:`section_group`. A card no longer in ``canonical`` writes nothing (gone before its
    ledger line would be read). ``now``: a :class:`datetime.datetime` (default: the clock) —
    a caller replaying history gives the moment being replayed, never the real wall clock."""
    entries = [(iid, section) for iid, section, by in answered or ()
              if not str(by or '').startswith('rule:')]
    if not entries:
        return
    data = load_ledger(state_dir)
    now = (now if now is not None else datetime.datetime.now(datetime.timezone.utc)).isoformat()
    for iid, section in entries:
        rec = canonical.get(iid)
        if rec is None:
            continue
        key = f'{iid}|{section_group(section)}'
        data[key] = {'digest': digest(rec['meta'], rec.get('body') or ''), 'at': now}
    _write_ledger(state_dir, data)


def _age_days(at, now):
    try:
        when = datetime.datetime.strptime(str(at)[:19], '%Y-%m-%dT%H:%M:%S').replace(
            tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None
    return (now - when).total_seconds() / 86400


def is_sticky(state_dir, iid, section, canonical, days, now=None):
    """True when ``iid``'s line in ``section`` should stay out of the file: the ledger's
    ``<id>|<group>`` entry is younger than ``days`` and its digest still matches the card's
    current one. ``days`` of ``None`` (``flags.groom.reask_days: off``) always answers False."""
    if days is None:
        return False
    data = load_ledger(state_dir)
    entry = data.get(f'{iid}|{section_group(section)}')
    if not isinstance(entry, dict):
        return False
    now = now if now is not None else datetime.datetime.now(datetime.timezone.utc)
    age = _age_days(entry.get('at'), now)
    if age is None or age > days:
        return False
    rec = canonical.get(iid)
    if rec is None:
        return False
    return digest(rec['meta'], rec.get('body') or '') == entry.get('digest')


#: An open groom line's leading id — the same token :mod:`asf.groom.groom`'s ``_LINE_ID_RE``
#: reads, kept here too so this module needs no import of that one (it is imported by it).
_LINE_ID_RE = re.compile(rf'^- \[[ xX]\]\s+([A-Z]-{ID_DIGITS})\b')


def filter_sticky(sections, state_dir, canonical, days, now=None):
    """``(sections, count)``: ``sections`` with every open line whose card is
    :func:`is_sticky` in that section dropped; ``count`` how many were. A line that is not an
    open ``- [ ] <id> …`` line (already answered, barred, or a conflicts/proposal line) is never
    touched — sticky only ever hides a question nobody has reason to ask again."""
    if days is None:
        return sections, 0
    out, count = {}, 0
    for key, lines in sections.items():
        kept = []
        for line in lines:
            m = _LINE_ID_RE.match(line)
            if m and line.rstrip().endswith('____') \
                    and is_sticky(state_dir, m.group(1), key, canonical, days, now):
                count += 1
                continue
            kept.append(line)
        out[key] = kept
    return out, count
