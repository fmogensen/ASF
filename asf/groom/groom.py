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
from asf.groom.digest import write_digest
from asf.groom.inbox import process_inbox
from asf.views import index_reader
from asf.workers import lifecycle, pool

ANSWER_LINE_RE = re.compile(r'^- \[[ xX]\]\s+(?P<id>[A-Z]-\d{4})\b.*→\s*answer:\s*(?P<answer>.*)$')
ANSWER_YES = re.compile(r'^yes$', re.IGNORECASE)
ANSWER_NO = re.compile(r'^(no|close)$', re.IGNORECASE)
ANSWER_RANK = re.compile(r'^rank\s+(\d+)$', re.IGNORECASE)
ANSWER_PARENT = re.compile(r'^parent\s+(\S+)$', re.IGNORECASE)
ANSWER_SEVERITY = re.compile(r'^(S[123])$', re.IGNORECASE)
ANSWER_UNBLOCK = re.compile(r'^unblock\s+([A-Z]-\d{4})$', re.IGNORECASE)
CONTROLLER_PREFIX = re.compile(r'^controller:\s*', re.IGNORECASE)
ADJUDICATOR_PREFIX = re.compile(r'^adjudicator:\s*', re.IGNORECASE)

#: PD3 — the applier's side of the policy names §2.2 defines. ``policy.POLICIES`` (a later Task)
#: must name exactly these; kept here, not in ``asf.groom.policy``, because that module is
#: imported by ``asf.feeder.rows`` and must not import this one back.
POLICY_NAMES = ('unblock_on_closed', 'close_exact_duplicate', 'decide_recurring_bug',
                'close_on_starvation')


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
    if ANSWER_NO.match(a):
        return 'removed', None  # caller fills in the reason text
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


def apply_groom_answers(root, canonical, prev_path, date, adjudicator_job=None, event=None,
                        sections=None):
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
        m = ANSWER_LINE_RE.match(line)
        if not m:
            continue
        iid, raw_answer = m.group('id'), m.group('answer').strip()
        rec = canonical.get(iid)
        if rec is None:
            continue

        who = '(operator)'
        by = 'operator'
        cm = CONTROLLER_PREFIX.match(raw_answer)
        am = ADJUDICATOR_PREFIX.match(raw_answer) if cm is None else None
        if cm:
            raw_answer = raw_answer[cm.end():].strip()
            pm = re.match(r'^(\S+)\s+(.*)$', raw_answer)
            if pm and pm.group(1) in POLICY_NAMES:
                who = f'(controller, {pm.group(1)})'
                by = f'rule:{pm.group(1)}'
                raw_answer = pm.group(2).strip()
            else:
                who = '(controller, starvation policy)'
                by = 'rule:starvation policy'
        elif am:
            raw_answer = raw_answer[am.end():].strip()
            who = f'(adjudicator, {adjudicator_job})'
            by = f'adjudicator:{adjudicator_job}'

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
            value = f"groom {date}"

        if typed.get(field) == value:
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
        lines.append(_card_line(iid, typed.get('title', ''), 'from inbox, awaiting a decision'))
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
                score = jaccard(tokenize(canonical[a]['meta'].get('title', '')),
                                tokenize(canonical[b]['meta'].get('title', '')))
                if score > 0.6 and (a, b) not in seen_pairs:
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
    ('Blocked on a Closed item', 'blocked_closed'),
    ('Near-duplicate titles', 'dupes'),
    ('Undecided > 14 days', 'undecided14'),
    ('Auto-filed Bugs not yet decided', 'auto_bugs'),
]


def build_groom_sections(canonical, derived, date):
    now = datetime.datetime.now(datetime.timezone.utc)
    origin_ids = inbox_origin_ids(canonical)
    return {
        'inbox': groom_inbox_section(canonical, origin_ids),
        'undecided3': groom_undecided_section(canonical, now, 3, exclude=origin_ids),
        'no_stories': groom_features_without_stories(canonical, derived),
        'no_tasks': groom_stories_without_tasks(canonical, derived),
        'blocked_closed': groom_blocked_on_closed(canonical),
        'dupes': groom_near_duplicates(canonical),
        'undecided14': groom_undecided_section(canonical, now, 14, exclude=origin_ids),
        'auto_bugs': groom_auto_bugs_section(canonical),
    }


_SECTION_BY_TITLE = {title: key for title, key in GROOM_SECTIONS}
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


#: PD3 checked against §2.2's own order — a test asserts these equal ``[n for n, _s, _f in
#: policy.POLICIES]``.
_POLICY_BY_SECTION = {section: (name, fn) for name, section, fn in policy.POLICIES}


def run_policy_pass(sections, canonical, derived, product, ctx):
    """§2.2 steps 3-4's rendering half: every remaining ``→ answer: ____`` line either gets
    barred (§2.8, PD4) or tried against its section's policy — never both. A barred line is
    rewritten ``____ (barred: approvals.<key>)``; an answered line, ``controller: <policy>
    <word>``. Returns ``(sections, barred_count)``; the actual field writes come from applying
    the rendered file through :func:`apply_groom_answers` (D3), so this function only rewrites
    text."""
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
            probe = policy.Answer('yes', 'decided', True, '')
            bar = policy.barred(probe, item, product)
            if bar:
                new_lines.append(re.sub(r'____$', f'____ (barred: approvals.{bar})', line))
                barred_count += 1
                continue
            entry = _POLICY_BY_SECTION.get(key)
            if entry is None or not policy.policy_on(product, entry[0]):
                new_lines.append(line)
                continue
            name, fn = entry
            ans = fn(m.group('id'), item, canonical, derived, ctx)
            if ans is None:
                new_lines.append(line)
                continue
            new_lines.append(re.sub(r'____$', f'controller: {name} {ans.word}', line))
        out[key] = new_lines
    return out, barred_count


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
    return '\n'.join(out).rstrip('\n') + '\n'


ANSWERS_FILE_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})\.answers$')


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

    applied = 0
    if args.apply:
        prev = _previous_groom_file(root, date)
        if prev:
            with open(prev, encoding='utf-8') as f:
                prev_sections = _line_sections(f.read())
            applied = apply_groom_answers(root, canonical, prev, date, event=event,
                                          sections=prev_sections)

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
                                       sections=adj_sections)
        os.rename(answers_file, answers_file + '.done')

    default_bug_parent = getattr(args, 'default_bug_epic', None)
    if default_bug_parent is None and product is not None:
        default_bug_parent = product.conventions.get('default_bug_epic')
    created_ids = process_inbox(root, canonical, date, default_bug_parent=default_bug_parent)

    derived = compute_derived(canonical)
    sections = build_groom_sections(canonical, derived, date)

    auto = policy.groom_auto(product)
    by_rule = 0
    spoken_for = 0
    barred_count = 0
    if auto:
        index, _generated = index_reader.load(root)
        inflight = lifecycle.inflight(pool.sessions_path(product))
        sections, spoken_for = policy.suppress(sections, index, inflight, product)

        ctx = policy.Ctx(date=date, now=datetime.datetime.now(datetime.timezone.utc),
                         duplicate_overlap=policy.duplicate_overlap(product),
                         recurring_bug_count=policy.recurring_bug_count(product),
                         undecided_close=stale.load_limits(product).get(
                             'undecided_close', policy.DEFAULT_UNDECIDED_CLOSE),
                         ledger_items=_ledger_items(product))
        sections, barred_count = run_policy_pass(sections, canonical, derived, product, ctx)

    text = render_groom_file(date, sections)
    groom_dir = os.path.join(root, 'groom')
    os.makedirs(groom_dir, exist_ok=True)
    groom_path = os.path.join(groom_dir, f"{date}.md")
    with open(groom_path, 'w', encoding='utf-8') as f:
        f.write(text)

    if auto:
        by_rule = apply_groom_answers(root, canonical, groom_path, date, event=event,
                                      sections=_line_sections(text))
        canonical, _dupes = canonicalize(load_items(root)[0])
        derived = compute_derived(canonical)
        cap = policy.adjudicate_attempts(product)
        attempts = sum(1 for rec in lifecycle.read_lines(pool.sessions_path(product))
                       if rec.get('job') == f'groom-{date}')
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
