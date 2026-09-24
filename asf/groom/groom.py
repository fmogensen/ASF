"""asf.groom.groom — inbox -> cards, then write groom/<date>.md (``asf groom``)."""
import datetime
import os
import re

from asf import env
from asf.record import frontmatter
from asf.record.core import canonicalize, compute_derived, is_open, jaccard, load_items, tokenize
from asf.record.index import do_index
from asf.record.ingest import append_history_lines
from asf.tick import stale
from asf.tick.stale import format_age, parse_iso
from asf.groom import policy
from asf.groom import inbox
from asf.groom.digest import write_digest
from asf.groom import inbox as inbox_mod
from asf.groom.inbox import process_inbox
from asf.capacity import DEFAULT_SESSIONS as DEFAULT_CAPACITY
from asf.conventions import DEFAULT_AREA_DEPTH, DEFAULT_BATCH_MAX_GLOBS
from asf.evidence import closing
from asf.groom import shape
from asf.groom.shape import SHAPE_LINE_RE
from asf.views import index_reader
from asf.workers import lifecycle, pool

ANSWER_LINE_RE = re.compile(r'^- \[[ xX]\]\s+(?P<id>[A-Z]-\d{4})\b.*→\s*answer:\s*(?P<answer>.*)$')
INBOX_ANSWER_RE = re.compile(r'^- \[[ xX]\]\s+inbox:(?P<name>\S+)\s.*→\s*answer:\s*(?P<answer>.*)$')
ANSWER_YES = re.compile(r'^yes$', re.IGNORECASE)
#: ``no``/``close``, optionally ``: <reason>`` — a rule that closes a card names why
#: (``no: superseded by F-0001``), and the reason becomes the card's ``removed:`` text.
ANSWER_NO = re.compile(r'^(no|close)(?::\s*(?P<why>\S.*))?$', re.IGNORECASE)
ANSWER_LANDED = re.compile(r'^landed\s+([0-9a-f]{7,40})$', re.IGNORECASE)
ANSWER_OPEN = re.compile(r'^open$', re.IGNORECASE)
ANSWER_RANK = re.compile(r'^rank\s+(\d+)$', re.IGNORECASE)
ANSWER_PARENT = re.compile(r'^parent\s+(\S+)$', re.IGNORECASE)
ANSWER_SEVERITY = re.compile(r'^(S[123])$', re.IGNORECASE)
ANSWER_UNBLOCK = re.compile(r'^unblock\s+([A-Z]-\d{4})$', re.IGNORECASE)
#: A shape proposal's line (F-0086 D6): the verb after the id says what `yes` does.
PROPOSAL_RE = re.compile(
    r'^- \[[ xX]\]\s+(?P<id>[A-Z]-\d{4})\s+(?P<verb>merge|batch|split)\s+(?P<rest>.*?)\s+—')
_ID_RE = re.compile(r'^[A-Z]-\d{4}$')
CONTROLLER_PREFIX = re.compile(r'^controller:\s*', re.IGNORECASE)
ADJUDICATOR_PREFIX = re.compile(r'^adjudicator:\s*', re.IGNORECASE)

#: PD3 — the applier's side of the policy names §2.2 defines. ``policy.POLICIES`` (a later Task)
#: must name exactly these; kept here, not in ``asf.groom.policy``, because that module is
#: imported by ``asf.feeder.rows`` and must not import this one back.
POLICY_NAMES = ('unblock_on_closed', 'close_duplicate_task', 'close_exact_duplicate',
                'close_superseded',
                'decide_on_approved_doc', 'decide_or_close_ci_red', 'decide_recurring_bug',
                'decide_by_approval', 'close_on_starvation')


def _previous_groom_file(root, date):
    d = os.path.join(root, 'groom')
    if not os.path.isdir(d):
        return None
    dates = [m.group(1) for name in os.listdir(d)
             for m in [re.match(r'^(\d{4}-\d{2}-\d{2})\.md$', name)] if m and m.group(1) < date]
    if not dates:
        return None
    return os.path.join(d, f"{sorted(dates)[-1]}.md")


def _parse_answer(answer):
    """(field, value) for one groom answer word, or (None, None) when it's blank or unrecognized."""
    a = answer.strip()
    if not a or a == '____':
        return None, None
    if ANSWER_YES.match(a):
        return 'decided', True
    m = ANSWER_NO.match(a)
    if m:
        # the caller dates the reason text; a bare `no` is only the date
        return 'removed', (m.group('why').strip() if m.group('why') else None)
    if ANSWER_OPEN.match(a):
        return 'reconciled', None  # caller fills in the date
    m = ANSWER_LANDED.match(a)
    if m:
        return 'landed', m.group(1).lower()
    m = ANSWER_RANK.match(a)
    if m:
        return 'rank', int(m.group(1))
    m = ANSWER_PARENT.match(a)
    if m:
        return 'parent', m.group(1)
    m = ANSWER_SEVERITY.match(a)
    if m:
        return 'severity', m.group(1).upper()
    m = ANSWER_UNBLOCK.match(a)
    if m:
        return 'unblock', m.group(1)
    return None, None


def _fmt_history_value(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


def _attribution(raw_answer, adjudicator_job):
    """``(answer word, who, by)`` — the answer with its ``controller:``/``adjudicator:`` prefix
    taken off, and whom the History line and the event name."""
    who, by = '(operator)', 'operator'
    cm = CONTROLLER_PREFIX.match(raw_answer)
    am = ADJUDICATOR_PREFIX.match(raw_answer) if cm is None else None
    if cm:
        raw_answer = raw_answer[cm.end():].strip()
        pm = re.match(r'^(\S+)\s+(.*)$', raw_answer)
        if pm and pm.group(1) in POLICY_NAMES:
            who, by = f'(controller, {pm.group(1)})', f'rule:{pm.group(1)}'
            raw_answer = pm.group(2).strip()
        else:
            who, by = '(controller, starvation policy)', 'rule:starvation policy'
    elif am:
        raw_answer = raw_answer[am.end():].strip()
        who, by = f'(adjudicator, {adjudicator_job})', f'adjudicator:{adjudicator_job}'
    return raw_answer, who, by


def _write_card(rec, fields, hist):
    """Write typed `fields` on a card, append one History line, and mirror both into `rec`."""
    frontmatter.write_typed(rec['path'], fields)
    with open(rec['path'], encoding='utf-8') as f:
        text = f.read()
    meta2, body2 = frontmatter.parse(text, path=rec['relpath'])
    new_body = append_history_lines(body2, [hist])
    if new_body != body2:
        with open(rec['path'], 'w', encoding='utf-8') as f:
            f.write(frontmatter.render(meta2, new_body))
    rec['meta'].update(fields)


def _why_not_ready(canonical, tid):
    rec = canonical.get(tid)
    if rec is None:
        return 'missing'
    if not is_open(rec):
        return 'removed' if rec['meta'].get('removed') else 'Closed'
    state = frontmatter.split_machine(rec['meta'])[1].get('state', 'New')
    if state != 'New':
        return state
    if rec['meta'].get('blocked'):
        return 'blocked'
    if rec['meta'].get('reshape'):
        return 'held for reshape'
    return 'not ready'


def _union(*lists):
    out = []
    for lst in lists:
        for v in lst or []:
            if v not in out:
                out.append(v)
    return out


def merge_tasks(canonical, verb, ids, date, who, derived=None, emit=None):
    """D8: merge `ids[1:]` into `ids[0]`, atomically. Returns the number of cards written."""
    survivor, others = ids[0], list(ids[1:])
    target = f"merged into {survivor} (groom {date})"
    srec = canonical.get(survivor)
    if srec is not None and all(o in (srec['meta'].get('merged') or []) and o in canonical
                                and canonical[o]['meta'].get('removed') == target for o in others):
        return 0  # already applied — keeps --apply idempotent
    ready = {r.id for r in shape.ready_tasks(canonical, derived)}
    for tid in ids:
        if tid not in ready:
            print(f"groom: {verb} {'+'.join(ids)} skipped — {tid} is {_why_not_ready(canonical, tid)}")
            return 0
    recs = [canonical[t] for t in ids]
    fields = {'writes': _union(*[r['meta'].get('writes') for r in recs]),
              'merged': _union(srec['meta'].get('merged'), others)}
    stories = _union(*[r['meta'].get('stories') for r in recs])
    if stories:
        fields['stories'] = stories
    _write_card(srec, fields, f"- {date} groom: merged {', '.join(others)} into {survivor} {who}")
    if emit:
        emit(survivor, 'merged', ', '.join(others))
    for tid in others:
        _write_card(canonical[tid], {'removed': target},
                    f"- {date} groom: removed → merged into {survivor} {who}")
        if emit:
            emit(tid, 'removed', target)
    return len(ids)


def apply_proposal(root, canonical, verb, ids, areas, answer, date, who, derived=None, emit=None):
    """One proposal line's answer: `yes` reshapes (merge/batch/split), `no`/`close` declines and
    suppresses, anything else changes nothing. Returns the number of cards written."""
    a = answer.strip()
    if ANSWER_YES.match(a):
        if verb in ('merge', 'batch'):
            return merge_tasks(canonical, verb, ids, date, who, derived=derived, emit=emit)
        rec = canonical.get(ids[0])
        if rec is None or rec['meta'].get('reshape'):
            return 0  # already applied
        ready = {r.id for r in shape.ready_tasks(canonical, derived)}
        if ids[0] not in ready:
            print(f"groom: split {ids[0]} skipped — {ids[0]} is {_why_not_ready(canonical, ids[0])}")
            return 0
        names = ' | '.join(areas)
        value = f"split {names} (groom {date})"
        _write_card(rec, {'reshape': value}, f"- {date} groom: reshape → split {names} {who}")
        if emit:
            emit(ids[0], 'reshape', value)
        return 1
    if ANSWER_NO.match(a):
        key = shape.proposal_key(verb, ids, areas)
        n = 0
        for tid in ids:
            rec = canonical.get(tid)
            declined = list(rec['meta'].get('reshape_declined') or []) if rec else []
            if rec is None or key in declined:
                continue
            _write_card(rec, {'reshape_declined': declined + [key]},
                        f"- {date} groom: {verb} declined {who}")
            if emit:
                emit(tid, 'reshape_declined', key)
            n += 1
        return n
    return 0


def _parse_proposal(m):
    """`(verb, ids, areas)` for a PROPOSAL_RE match, or None when its `rest` is not a proposal's
    (an ordinary card whose title happens to start with the verb)."""
    verb, rest = m.group('verb'), m.group('rest')
    if verb == 'split':
        names = tuple(rest.split(' | '))
        return (verb, (m.group('id'),), names) if len(names) >= 2 else None
    ids = tuple(rest.split('+'))
    if len(ids) >= 2 and all(_ID_RE.match(i) for i in ids) and ids[0] == m.group('id'):
        return verb, ids, ()
    return None


def apply_groom_answers(root, canonical, prev_path, date, adjudicator_job=None, event=None,
                        sections=None, intake_dir=None, derived=None):
    """Read `prev_path`'s answered lines, write each as a typed field, append one History line
    each. A no-op line (blank/`____`, or a field already at the target value) changes nothing —
    this is what makes re-running `--apply` for the same date idempotent.

    `controller: <word>` attributes `(controller, starvation policy)`; `controller: <policy>
    <word>`, where `<policy>` is one of `POLICY_NAMES` (PD2, PD3), attributes `(controller,
    <policy>)` instead. `adjudicator: <word>` attributes `(adjudicator, <adjudicator_job>)`.

    `event` (§4), when given, gets one `groom_answer` call per applied answer: `item`, `section`
    (`sections.get(item, '')` — the `{item: section key}` map :func:`_line_sections` builds off
    the file the question was asked in), `field`, `value`, `by` (`rule:<policy>` /
    `adjudicator:<job>` / `operator`)."""
    with open(prev_path, encoding='utf-8') as f:
        lines = f.read().split('\n')

    applied = 0
    for line in lines:
        im = INBOX_ANSWER_RE.match(line)
        if im:  # an inbox card intake asked about: the answer edits the card, intake re-reads it
            word, who, by = _attribution(im.group('answer').strip(), adjudicator_job)
            if inbox_mod.apply_answer(root, im.group('name'), word, date, who.strip('()'),
                                      intake_dir=intake_dir):
                applied += 1
                if event:
                    event('groom_answer', item=f"inbox:{im.group('name')}", section='inbox_questions',
                          field='inbox', value=word, by=by)
            continue
        m = ANSWER_LINE_RE.match(line)
        if not m:
            continue
        iid, raw_answer = m.group('id'), m.group('answer').strip()
        rec = canonical.get(iid)
        if rec is None:
            continue

        raw_answer, who, by = _attribution(raw_answer, adjudicator_job)

        pm = PROPOSAL_RE.match(line)
        proposal = _parse_proposal(pm) if pm else None
        if proposal:
            def emit(item, fld, val, _sec=(sections or {}).get(iid, ''), _by=by):
                if event:
                    event('groom_answer', item=item, section=_sec, field=fld, value=val, by=_by)
            applied += apply_proposal(root, canonical, *proposal, raw_answer, date, who,
                                      derived=derived, emit=emit)
            continue

        field, value = _parse_answer(raw_answer)
        if field is None:
            continue

        typed, _machine = frontmatter.split_machine(rec['meta'])

        if field == 'unblock':
            blocked_by = typed.get('blockedBy') or []
            if value not in blocked_by:
                continue  # not there — no-op, keeps --apply idempotent
            remainder = [b for b in blocked_by if b != value]
            frontmatter.write_typed(rec['path'], {'blockedBy': remainder or None})
            with open(rec['path'], encoding='utf-8') as f:
                text = f.read()
            meta2, body2 = frontmatter.parse(text, path=rec['relpath'])
            hist_value = ', '.join(remainder) if remainder else '(none)'
            hist = f"- {date} groom: blockedBy → {hist_value} {who}"
            new_body = append_history_lines(body2, [hist])
            if new_body != body2:
                with open(rec['path'], 'w', encoding='utf-8') as f:
                    f.write(frontmatter.render(meta2, new_body))
            if remainder:
                rec['meta']['blockedBy'] = remainder
            else:
                rec['meta'].pop('blockedBy', None)
            applied += 1
            if event:
                event('groom_answer', item=iid, section=(sections or {}).get(iid, ''),
                     field=field, value=hist_value, by=by)
            continue

        if field == 'removed':
            value = f"{value} (groom {date})" if value else f"groom {date}"
        elif field == 'reconciled':
            value = date

        if str(typed.get(field)) == str(value) and typed.get(field) is not None:
            continue  # already applied

        frontmatter.write_typed(rec['path'], {field: value})
        with open(rec['path'], encoding='utf-8') as f:
            text = f.read()
        meta2, body2 = frontmatter.parse(text, path=rec['relpath'])
        hist = f"- {date} groom: {field} → {_fmt_history_value(value)} {who}"
        new_body = append_history_lines(body2, [hist])
        if new_body != body2:
            with open(rec['path'], 'w', encoding='utf-8') as f:
                f.write(frontmatter.render(meta2, new_body))
        rec['meta'][field] = value
        applied += 1
        if event:
            event('groom_answer', item=iid, section=(sections or {}).get(iid, ''),
                 field=field, value=_fmt_history_value(value), by=by)
    return applied


def _card_line(iid, title, why):
    return f"- [ ] {iid} {title} — {why} → answer: ____"


def _open_items(canonical, exclude_types=()):
    return {iid: rec for iid, rec in canonical.items()
            if is_open(rec) and rec['meta'].get('type') not in exclude_types}


def inbox_origin_ids(canonical):
    """Every item `process_inbox` ever minted (its History says so), open or not. A card stays
    in this set for its whole life, so "Inbox cards to decide" keeps naming it every day it sits
    undecided instead of only on the day it was created."""
    return {iid for iid, rec in canonical.items() if 'created (inbox)' in (rec.get('body') or '')}


def groom_inbox_section(canonical, origin_ids):
    lines = []
    for iid in sorted(origin_ids):
        rec = canonical.get(iid)
        if rec is None:
            continue
        typed, _machine = frontmatter.split_machine(rec['meta'])
        if typed.get('decided') is True or typed.get('removed'):
            continue
        why = 'from inbox, awaiting a decision'
        m = SHAPE_LINE_RE.search(rec.get('body') or '')
        if m:
            why = f"from inbox as {m.group(3)} ({m.group(2)}), awaiting a decision"
        lines.append(_card_line(iid, typed.get('title', ''), why))
    return lines


def groom_undecided_section(canonical, now, days, exclude=()):
    lines = []
    threshold = days * 86400
    for iid, rec in sorted(_open_items(canonical, ('decision', 'rule')).items()):
        if iid in exclude:
            continue
        typed, machine = frontmatter.split_machine(rec['meta'])
        if typed.get('decided') is True:
            continue
        since = parse_iso(machine.get('stage_since'))
        if since is None:
            continue
        age = (now - since).total_seconds()
        if age > threshold:
            lines.append(_card_line(iid, typed.get('title', ''), f"undecided {format_age(age)}"))
    return lines


def groom_features_without_stories(canonical, derived):
    lines = []
    for iid, rec in sorted(_open_items(canonical).items()):
        if rec['meta'].get('type') != 'feature':
            continue
        stories = [cid for cid in derived[iid]['children'] if canonical[cid]['meta'].get('type') == 'story']
        if not stories:
            lines.append(_card_line(iid, rec['meta'].get('title', ''), 'no Stories'))
    return lines


_AFTER_PLAN_APPROVED = ('plan-approved', 'landed', 'on-prod')


def groom_stories_without_tasks(canonical, derived):
    task_story_ids = set()
    for rec in canonical.values():
        if rec['meta'].get('type') == 'task' and not rec['meta'].get('removed'):
            for s in rec['meta'].get('stories') or []:
                task_story_ids.add(s)
    lines = []
    for fid, frec in sorted(canonical.items()):
        if frec['meta'].get('type') != 'feature':
            continue
        _typed, machine = frontmatter.split_machine(frec['meta'])
        stage = machine.get('stage') or ''
        if stage not in _AFTER_PLAN_APPROVED and not stage.startswith('building'):
            continue
        for sid in derived[fid]['children']:
            srec = canonical[sid]
            if srec['meta'].get('type') != 'story' or not is_open(srec):
                continue
            if sid not in task_story_ids:
                lines.append(_card_line(sid, srec['meta'].get('title', ''),
                                        f"{fid} is {stage}, no Task lists it"))
    return lines


def groom_predates_section(canonical, since):
    """One line per item `ingest` marked as older than the id convention, in `created` order.
    Unset `since` asks nothing (§1.4)."""
    if not since:
        return []
    marked = [(closing.created_of(rec['meta']), iid, rec) for iid, rec in canonical.items()
              if is_open(rec) and closing.predates_marked(rec['meta'])]
    return [_card_line(iid, rec['meta'].get('title', ''),
                       f"created {created}, before the id convention ({str(since)[:10]}); "
                       f"no commit, PR or branch names it")
            for created, iid, rec in sorted(marked)]


def groom_blocked_on_closed(canonical):
    lines = []
    for iid, rec in sorted(_open_items(canonical).items()):
        for b in rec['meta'].get('blockedBy') or []:
            if isinstance(b, str) and b in canonical:
                _t, bm = frontmatter.split_machine(canonical[b]['meta'])
                if bm.get('state') == 'Closed':
                    lines.append(_card_line(iid, rec['meta'].get('title', ''),
                                            f"blockedBy {b}, which is Closed"))
    return lines


def groom_near_duplicates(canonical):
    """One line per near-duplicate title pair, naming the younger card. The threshold is raised
    for templated titles (:func:`asf.groom.policy.near_duplicate_threshold`). A Task pair is
    decided by rule, never asked: only the younger of two Tasks with one parent and one
    footprint gets a line (``close_duplicate_task`` closes it); any other Task pair's flag is
    dropped."""
    lines = []
    by_type = {}
    for iid, rec in canonical.items():
        if is_open(rec):
            by_type.setdefault(rec['meta'].get('type'), []).append(iid)
    seen_pairs = set()
    for type_, ids in by_type.items():
        ids = sorted(ids)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                ta, tb = canonical[a]['meta'].get('title', ''), canonical[b]['meta'].get('title', '')
                score = jaccard(tokenize(ta), tokenize(tb))
                if score <= policy.near_duplicate_threshold(ta, tb):
                    continue
                if type_ == 'task' and policy.task_duplicate_of(b, canonical[b], canonical) != a:
                    continue  # decided by rule: not the same parent and footprint — no flag
                if (a, b) not in seen_pairs:
                    seen_pairs.add((a, b))
                    lines.append(_card_line(b, canonical[b]['meta'].get('title', ''),
                                            f"near-duplicate of {a} (overlap {score:.2f})"))
    return sorted(lines)


def groom_auto_bugs_section(canonical):
    lines = []
    for iid, rec in sorted(canonical.items()):
        typed, _machine = frontmatter.split_machine(rec['meta'])
        if typed.get('type') != 'bug' or not typed.get('signature'):
            continue
        if typed.get('decided') is True or typed.get('removed'):
            continue
        lines.append(_card_line(iid, typed.get('title', ''),
                                f"auto-filed, count {typed.get('count', 1)}"))
    return lines


GROOM_SECTIONS = [
    ('Inbox cards to decide', 'inbox'),
    ('Undecided > 3 days', 'undecided3'),
    ('Features without Stories', 'no_stories'),
    ('Stories without Tasks after plan-approved', 'no_tasks'),
    ('Landed before the id convention', 'predates'),
    ('Merges proposed', 'merge'),
    ('Batches proposed', 'batch'),
    ('Splits proposed', 'split'),
    ('Split parts to confirm', 'split_parts'),
    ('Blocked on a Closed item', 'blocked_closed'),
    ('Near-duplicate titles', 'dupes'),
    ('Undecided > 14 days', 'undecided14'),
    ('Auto-filed Bugs not yet decided', 'auto_bugs'),
]


def _proposal_line(p):
    rest = ' | '.join(p.areas) if p.verb == 'split' else '+'.join(p.ids)
    return f"- [ ] {p.ids[0]} {p.verb} {rest} — {p.why} → answer: ____"


def groom_shape_sections(canonical, derived, capacity, area_depth, batch_max_globs):
    """The four F-0086 sections: merges, batches, splits, and split parts awaiting a `yes`."""
    # a Task the duplicate rule closes is no merge partner: it is going, not merging
    ready = [t for t in shape.ready_tasks(canonical, derived)
             if policy.task_duplicate_of(t.id, canonical[t.id], canonical) is None]
    declined = shape.declined_keys(canonical)
    merges = shape.merge_proposals(ready, declined)
    batches = shape.batch_proposals(ready, merges, capacity, batch_max_globs, declined)
    splits = shape.split_proposals(ready, shape.active_tasks(canonical), area_depth, declined)
    parts = []
    for tid, rec in sorted(canonical.items()):
        meta = rec['meta']
        if (meta.get('type') == 'task' and meta.get('split_from') and is_open(rec)
                and meta.get('decided') is not True):
            parts.append(_card_line(tid, meta.get('title', ''),
                                    f"split from {meta['split_from']}, launches after yes"))
    return {'merge': [_proposal_line(p) for p in merges],
            'batch': [_proposal_line(p) for p in batches],
            'split': [_proposal_line(p) for p in splits],
            'split_parts': parts}


def build_groom_sections(canonical, derived, date, capacity=DEFAULT_CAPACITY,
                         area_depth=DEFAULT_AREA_DEPTH, batch_max_globs=DEFAULT_BATCH_MAX_GLOBS,
                         since=None):
    now = datetime.datetime.now(datetime.timezone.utc)
    origin_ids = inbox_origin_ids(canonical)
    return {
        **groom_shape_sections(canonical, derived, capacity, area_depth, batch_max_globs),
        'inbox': groom_inbox_section(canonical, origin_ids),
        'undecided3': groom_undecided_section(canonical, now, 3, exclude=origin_ids),
        'no_stories': groom_features_without_stories(canonical, derived),
        'no_tasks': groom_stories_without_tasks(canonical, derived),
        'predates': groom_predates_section(canonical, since),
        'blocked_closed': groom_blocked_on_closed(canonical),
        'dupes': groom_near_duplicates(canonical),
        'undecided14': groom_undecided_section(canonical, now, 14, exclude=origin_ids),
        'auto_bugs': groom_auto_bugs_section(canonical),
    }


#: Rendered only when it has lines: the inbox cards intake asked a question of (their lines
#: carry an ``inbox:<file>`` token, not an id — :func:`asf.groom.inbox.question_lines`).
INBOX_QUESTIONS = ('Inbox cards with a question', 'inbox_questions')

_SECTION_BY_TITLE = {title: key for title, key in GROOM_SECTIONS + [INBOX_QUESTIONS]}
_HEADER_RE = re.compile(r'^## (.+)$')
_LINE_ID_RE = re.compile(r'^- \[[ xX]\]\s+([A-Z]-\d{4})\b')


def _line_sections(text):
    """``{item_id: section key}`` for a rendered groom file's bullet lines, tracking which ``##
    <title>`` header each falls under. Used to attribute a ``groom_answer`` event's ``section``
    to the file the question was actually asked in, whether that is today's groom file, a
    previous one being ``--apply``-ed, or the groom day an answers file answers."""
    out = {}
    current = None
    for line in text.splitlines():
        h = _HEADER_RE.match(line)
        if h:
            current = _SECTION_BY_TITLE.get(h.group(1))
            continue
        m = _LINE_ID_RE.match(line)
        if m and current:
            out[m.group(1)] = current
    return out


#: The sections some decision policy reads — a card's one decision answer is rendered on each of
#: its open lines in these.
_DECISION_KEYS = frozenset(key for name, keys, _fn in policy.POLICIES
                           if name not in policy.PER_LINE_POLICIES for key in keys)


def decide_card(item_id, keys, canonical, derived, product, ctx):
    """``(policy name, Answer)`` of the first decision policy, in ``policy.POLICIES`` order,
    that reads one of ``keys`` (the sections the card has an open line in), is on for the
    product, and answers the card; ``None`` when none does."""
    item = canonical[item_id]
    for name, sections, fn in policy.POLICIES:
        if name in policy.PER_LINE_POLICIES or not keys.intersection(sections):
            continue
        if not policy.policy_on(product, name):
            continue
        ans = fn(item_id, item, canonical, derived, ctx)
        if ans is not None:
            return name, ans
    return None


def run_policy_pass(sections, canonical, derived, product, ctx):
    """§2.2 steps 3-4's rendering half: every remaining ``→ answer: ____`` line either gets
    barred (§2.8, PD4) or tried against the policies — never both. A barred line is rewritten
    ``____ (barred: approvals.<key>)``; an answered line, ``controller: <policy> <word>``.

    ``unblock_on_closed`` answers its own lines one by one. Every other policy is a decision:
    a card gets one (:func:`decide_card`, over all the sections it has an open line in), and
    that one answer goes on each of its open lines in a decision section, so no card is closed
    on one line and decided on another. Returns ``(sections, barred_count)``; the actual field
    writes come from applying the rendered file through :func:`apply_groom_answers` (D3), so
    this function only rewrites text."""
    probe = policy.Answer('yes', 'decided', True, '')
    open_keys = {}
    for key, lines in sections.items():
        for line in lines:
            m = policy.OPEN_QUESTION_RE.match(line)
            if m and m.group('id') in canonical:
                open_keys.setdefault(m.group('id'), set()).add(key)
    decisions = {}
    out = {}
    barred_count = 0
    for key, lines in sections.items():
        new_lines = []
        for line in lines:
            m = policy.OPEN_QUESTION_RE.match(line)
            item = canonical.get(m.group('id')) if m else None
            if m is None or item is None:
                new_lines.append(line)
                continue
            iid = m.group('id')
            bar = policy.barred(probe, item, product)
            if bar:
                new_lines.append(re.sub(r'____$', f'____ (barred: approvals.{bar})', line))
                barred_count += 1
                continue
            answer = None
            for name, keys, fn in policy.POLICIES:
                if (name in policy.PER_LINE_POLICIES and key in keys
                        and policy.policy_on(product, name)):
                    ans = fn(iid, item, canonical, derived, ctx)
                    answer = (name, ans) if ans is not None else None
                    break
            else:
                if key in _DECISION_KEYS:
                    if iid not in decisions:
                        decisions[iid] = decide_card(iid, open_keys.get(iid, set()), canonical,
                                                     derived, product, ctx)
                    answer = decisions[iid]
            if answer is None:
                new_lines.append(line)
                continue
            name, ans = answer
            new_lines.append(re.sub(r'____$', f'controller: {name} {ans.word}', line))
        out[key] = new_lines
    return out, barred_count


def trunk_ci_runs(root, product):
    """The trunk's finished CI runs off the record's ``metrics/ci`` stream — the runs the
    metrics import gathers from the CI provider and the Bug filer reads — oldest first, as
    ``policy.Ctx.ci_runs`` wants them: ``{'ts', 'sha', 'jobs': {name: conclusion}}``. A run on
    any branch but ``main`` is left out; a job listed twice in one run keeps its last
    conclusion."""
    from asf.tick.file_bugs import DEFAULTS, _jsonl_lines
    conv = product.conventions if product is not None else DEFAULTS
    runs = []
    for run in _jsonl_lines(os.path.join(root, 'metrics', 'ci', '*.jsonl')):
        ts = parse_iso(run.get('ts'))
        if ts is None or not conv.is_trunk(run.get('branch') or ''):
            continue
        jobs = {j.get('name'): j.get('conclusion') for j in run.get('jobs') or []
                if isinstance(j, dict) and j.get('name')}
        runs.append({'ts': ts, 'sha': run.get('sha') or '', 'jobs': jobs})
    runs.sort(key=lambda r: r['ts'])
    return tuple(runs)


def _ledger_items(product):
    """Every item id the session ledger has ever launched a job for — what
    ``close_on_starvation`` must never answer out from under a session (§2.2)."""
    if product is None:
        return frozenset()
    return frozenset(lifecycle.attempts(pool.sessions_path(product)).keys())


def render_groom_file(date, sections):
    out = [f"# Groom {date}\n"]
    for title, key in GROOM_SECTIONS:
        lines = sections.get(key) or []
        out.append(f"## {title}\n")
        if lines:
            out.append('\n'.join(lines) + '\n')
        else:
            out.append("(none)\n")
        out.append('')
    title, key = INBOX_QUESTIONS
    if sections.get(key):
        out.append(f"## {title}\n")
        out.append('\n'.join(sections[key]) + '\n')
    return '\n'.join(out).rstrip('\n') + '\n'


ANSWERS_FILE_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})\.answers$')

#: A groom line's item token: an id, or ``inbox:<file>``.
_LINE_TOKEN_RE = re.compile(r'^- \[[ xX]\]\s+(\S+)')
#: An open ``inbox:<file>`` line — settled once its card has left the intake dir.
_OPEN_INBOX_LINE_RE = re.compile(r'^(- \[ \]\s+inbox:(?P<name>\S+)\s.*→\s*answer:\s*____)$')
SETTLED_SUFFIX = ' (settled: the card left the inbox)'


def merge_groom_text(existing, sections, intake_path=None):
    """Today's groom file with ``sections``' new lines added in place — the every-tick pass.
    A line is new when its section holds no line for the same item yet; nothing already in the
    file is dropped or rewritten, save one thing: an open ``inbox:<file>`` line whose card has
    left ``intake_path`` (typed, or closed) is marked settled, so no adjudicator is sent to rule
    on a card that is gone. Returns ``(text, lines added)``; merging the same sections twice
    changes nothing."""
    lines = existing.rstrip('\n').split('\n')
    if intake_path is not None:
        for i, line in enumerate(lines):
            m = _OPEN_INBOX_LINE_RE.match(line)
            if m and not os.path.isfile(os.path.join(intake_path, m.group('name'))):
                lines[i] = line + SETTLED_SUFFIX
    added = 0
    for title, key in GROOM_SECTIONS + [INBOX_QUESTIONS]:
        header = f'## {title}'
        start = next((i for i, l in enumerate(lines) if l.strip() == header), None)
        end = len(lines)
        if start is not None:
            end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith('## ')),
                       len(lines))
        have = {m.group(1) for l in (lines[start + 1:end] if start is not None else ())
                for m in [_LINE_TOKEN_RE.match(l)] if m}
        new = []
        for line in sections.get(key) or []:
            m = _LINE_TOKEN_RE.match(line)
            token = m.group(1) if m else line
            if token not in have:
                have.add(token)
                new.append(line)
        if not new:
            continue
        added += len(new)
        if start is None:
            lines += ['', header, ''] + new
            continue
        body = [i for i in range(start + 1, end) if lines[i].strip()]
        if len(body) == 1 and lines[body[0]].strip() == '(none)':
            lines[body[0]:body[0] + 1] = new
        else:
            at = (body[-1] + 1) if body else start + 1
            lines[at:at] = new
    return '\n'.join(lines) + '\n', added


def cmd_groom(args, root):
    date = args.date or datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
    by_id, parse_errors = load_items(root)
    if parse_errors:
        for f, line, why in parse_errors:
            print(f"{f}:{line}: {why}")
        return 1
    canonical, _dupes = canonicalize(by_id)

    try:
        product = env.load_product(getattr(args, 'product', None))
    except env.ConfigError:
        product = None

    event = getattr(args, 'event', None)
    intake_dir = product.conventions.intake_dir if product else None

    applied = 0
    if args.apply:
        # the previous day's file, then today's own: an answer the operator wrote into today's
        # file is applied now, not a day late (applying is idempotent, so a rule answer already
        # applied changes nothing)
        today = os.path.join(root, 'groom', f'{date}.md')
        for prev in (_previous_groom_file(root, date), today if os.path.isfile(today) else None):
            if not prev:
                continue
            with open(prev, encoding='utf-8') as f:
                prev_sections = _line_sections(f.read())
            applied += apply_groom_answers(root, canonical, prev, date, event=event,
                                           sections=prev_sections, intake_dir=intake_dir)

    answers_file = getattr(args, 'answers_file', None)
    answers_text = None
    if answers_file and os.path.isfile(answers_file):
        m = ANSWERS_FILE_RE.match(os.path.basename(answers_file))
        adj_date = m.group(1) if m else date
        adj_groom_path = os.path.join(root, 'groom', f'{adj_date}.md')
        adj_sections = {}
        if os.path.isfile(adj_groom_path):
            with open(adj_groom_path, encoding='utf-8') as f:
                adj_sections = _line_sections(f.read())
        with open(answers_file, encoding='utf-8') as f:
            answers_text = f.read()
        applied += apply_groom_answers(root, canonical, answers_file, date,
                                       adjudicator_job=f'groom-{adj_date}', event=event,
                                       sections=adj_sections, intake_dir=intake_dir)
        os.rename(answers_file, answers_file + '.done')

    default_bug_parent = getattr(args, 'default_bug_epic', None)
    if default_bug_parent is None and product is not None:
        default_bug_parent = product.conventions.get('default_bug_epic')
    incremental = getattr(args, 'incremental', False)
    asked = []
    created_ids = process_inbox(root, canonical, date, default_bug_parent=default_bug_parent,
                                intake_dir=intake_dir, asked=asked)

    derived = compute_derived(canonical)
    from asf.tick.step_wave import capacity as lane_capacity  # inside: asf.groom stays out of asf.tick
    try:
        lane_slots = lane_capacity(product)
    except env.ConfigError:
        lane_slots = DEFAULT_CAPACITY
    conv = product.conventions if product is not None else None
    sections = build_groom_sections(
        canonical, derived, date, capacity=lane_slots,
        area_depth=conv.area_depth if conv else DEFAULT_AREA_DEPTH,
        batch_max_globs=conv.batch_max_globs if conv else DEFAULT_BATCH_MAX_GLOBS,
        since=conv.get('id_in_subject_since') if conv else None)
    sections[INBOX_QUESTIONS[1]] = inbox_mod.question_lines(root, intake_dir)

    auto = policy.groom_auto(product)
    by_rule = 0
    spoken_for = 0
    barred_count = 0
    if auto:
        if applied or created_ids:
            do_index(root)  # suppression reads the index: it must see what was just decided
        index, _generated = index_reader.load(root)
        inflight = lifecycle.inflight(pool.sessions_path(product))
        sections, spoken_for = policy.suppress(sections, index, inflight, product)

        ctx = policy.Ctx(date=date, now=datetime.datetime.now(datetime.timezone.utc),
                         duplicate_overlap=policy.duplicate_overlap(product),
                         recurring_bug_count=policy.recurring_bug_count(product),
                         undecided_close=stale.load_limits(product).get(
                             'undecided_close', policy.DEFAULT_UNDECIDED_CLOSE),
                         ledger_items=_ledger_items(product),
                         ci_red_days=policy.ci_red_days(product),
                         ci_runs=trunk_ci_runs(root, product),
                         approvals=dict((product.approvals if product is not None else None)
                                        or {}))
        sections, barred_count = run_policy_pass(sections, canonical, derived, product, ctx)

    text = render_groom_file(date, sections)
    groom_dir = os.path.join(root, 'groom')
    groom_path = os.path.join(groom_dir, f"{date}.md")
    if incremental:
        return _write_incremental(root, date, groom_path, text, sections, auto, created_ids,
                                  asked, intake_dir, canonical, event)
    os.makedirs(groom_dir, exist_ok=True)
    with open(groom_path, 'w', encoding='utf-8') as f:
        f.write(text)

    if auto:
        by_rule = apply_groom_answers(root, canonical, groom_path, date, event=event,
                                      sections=_line_sections(text), intake_dir=intake_dir)
        canonical, _dupes = canonicalize(load_items(root)[0])
        derived = compute_derived(canonical)
        cap = policy.adjudicate_attempts(product)
        attempts = sum(1 for rec in lifecycle.read_lines(pool.sessions_path(product))
                       if rec.get('job') == f'groom-{date}' and lifecycle.is_launch(rec))
        write_digest(root, date, canonical, text,
                     [answers_text] if answers_text is not None else [], attempts, cap)

    rc = do_index(root)
    counts = ', '.join(f"{title}: {len(sections.get(key) or [])}" for title, key in GROOM_SECTIONS)
    open_count = len(policy.open_questions(text))
    if auto:
        print(f"groom {date}: applied {applied}, inbox {len(created_ids)} card(s), "
             f"by rule {by_rule}, spoken for {spoken_for}, open {open_count} — {counts}")
    else:
        print(f"groom {date}: applied {applied}, inbox {len(created_ids)} card(s) — {counts}")
    if event:
        event('groom_open', count=open_count, barred=barred_count)
    return rc


def _write_incremental(root, date, groom_path, text, sections, auto, created_ids, asked,
                       intake_dir, canonical, event):
    """The every-tick groom (``incremental``): today's file gains the lines that are new since
    the last pass (:func:`merge_groom_text`) instead of being written afresh — the questions and
    answers it already carries stay. No file yet today: it is written whole, but only when
    intake changed something (the daily writes it otherwise). The rule answers among the new
    lines are applied; the digest stays the daily's."""
    existing = None
    if os.path.isfile(groom_path):
        with open(groom_path, encoding='utf-8') as f:
            existing = f.read()
    if existing is None:
        if not (created_ids or asked):
            return 0
        merged, added = text, len(policy.open_questions(text))
    else:
        merged, added = merge_groom_text(
            existing, sections, os.path.join(root, intake_dir or inbox_mod.DEFAULT_INTAKE_DIR))
    by_rule = 0
    if merged != existing:
        os.makedirs(os.path.dirname(groom_path), exist_ok=True)
        with open(groom_path, 'w', encoding='utf-8') as f:
            f.write(merged)
        if auto:
            by_rule = apply_groom_answers(root, canonical, groom_path, date, event=event,
                                          sections=_line_sections(merged), intake_dir=intake_dir)
    if merged == existing and not created_ids and not asked:
        return 0
    rc = do_index(root)
    print(f"groom {date} (tick): inbox {len(created_ids)} card(s), asked {len(asked)}, "
          f"added {added} line(s), by rule {by_rule}, open {len(policy.open_questions(merged))}")
    return rc
