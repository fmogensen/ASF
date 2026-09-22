"""asf.groom.digest — the digest (F-0085 §2.7, D9): ``groom/<date>-digest.md``, regenerated
every gated ``cmd_groom`` run from the cards' ``## History`` lines dated ``<date>`` plus the
day's remaining open questions. Nothing here is state of its own — re-running the same date
renders the same text (T4), since every input is either the record itself or the day's own
files.
"""
import os
import re

ANSWER_LINE_RE = re.compile(
    r'^- \[(?P<box>[ x])\]\s+(?P<id>[A-Z]-\d{4})\s+(?P<rest>.*?)\s*→\s*answer:\s*(?P<answer>.*)$')
HISTORY_LINE_RE = re.compile(
    r'^- (?P<date>\d{4}-\d{2}-\d{2}) groom: (?P<field>\S+) → (?P<value>.+) '
    r'\((?P<kind>controller|adjudicator), (?P<name>[^)]+)\)$')
BARRED_KEY_RE = re.compile(r'barred:\s*approvals\.(\w+)\)')
SPOKEN_FOR_LABEL_RE = re.compile(r'spoken for:\s*([^)]+)\)')


def _why(rest):
    return rest.rsplit(' — ', 1)[1] if ' — ' in rest else rest


def _why_map(text):
    out = {}
    for line in text.splitlines():
        m = ANSWER_LINE_RE.match(line)
        if m:
            out[m.group('id')] = _why(m.group('rest'))
    return out


def _classify_groom_lines(text):
    """``(suppressed, barred, open)`` off the day's rendered groom file — the three shapes of a
    line ``run_policy_pass``/``suppress`` leave for a question no rule has finished answering
    into a card: ``[x] … (spoken for: …)``, ``____ (barred: approvals.<key>)``, and a still-bare
    ``____``. Each entry is ``(item_id, why)`` plus the label/key where one applies."""
    suppressed, barred, open_ = [], [], []
    for line in text.splitlines():
        m = ANSWER_LINE_RE.match(line)
        if not m:
            continue
        iid, why, answer = m.group('id'), _why(m.group('rest')), m.group('answer')
        if answer == '____':
            open_.append((iid, why))
        elif answer.startswith('____ (barred:'):
            km = BARRED_KEY_RE.search(answer)
            barred.append((iid, why, km.group(1) if km else ''))
        elif answer.startswith('(spoken for:'):
            lm = SPOKEN_FOR_LABEL_RE.search(answer)
            suppressed.append((iid, why, lm.group(1) if lm else ''))
    return suppressed, barred, open_


def _dated_history(rec, date):
    out = []
    for line in (rec.get('body') or '').splitlines():
        m = HISTORY_LINE_RE.match(line.strip())
        if m and m.group('date') == date:
            out.append(m)
    return out


def _rule_and_adjudicator_lines(canonical, date, rule_why, adjudicator_why):
    rule_lines, adjudicator_lines = [], []
    for iid in sorted(canonical):
        for m in _dated_history(canonical[iid], date):
            field, value, name = m.group('field'), m.group('value'), m.group('name')
            head = f"- {iid} {field} → {value}"
            if m.group('kind') == 'controller':
                why = rule_why.get(iid)
                rule_lines.append(f"{head} — {name}: {why}" if why else f"{head} — {name}")
            else:
                why = adjudicator_why.get(iid)
                adjudicator_lines.append(f"{head} — {why}" if why else head)
    return rule_lines, adjudicator_lines


def render_digest(root, date, canonical, groom_text, answers_done_texts, attempts, cap):
    """§2.7: the four sections, derived fresh from ``canonical``'s ``## History`` and the day's
    own files — never a state this module keeps itself (D9). ``answers_done_texts`` is every
    ``<date>.answers``/``.answers.done`` text applied into today's History (usually zero or
    one); each of its own ``NEEDS OPERATOR:`` lines (the brief's fourth answer class) passes
    through to **For you** verbatim. ``attempts``/``cap`` are the groom day's own adjudicate
    attempts and ``groom.adjudicate_attempts`` (PD6): under the cap, a still-open question is
    listed under **Spoken for** as queued for the adjudicate session; at or past it, it moves to
    **For you** as a ``NEEDS OPERATOR`` line instead."""
    from asf import cli

    answers_done_texts = list(answers_done_texts or ())
    adjudicator_why = {}
    for t in answers_done_texts:
        adjudicator_why.update(_why_map(t))
    rule_lines, adjudicator_lines = _rule_and_adjudicator_lines(
        canonical, date, _why_map(groom_text), adjudicator_why)

    suppressed, barred, open_ = _classify_groom_lines(groom_text)
    spoken_for_lines = [f"- {iid} {why} — (spoken for: {label})" for iid, why, label in suppressed]
    for_you_lines = [f"NEEDS OPERATOR: {iid} — {why}; approvals.{key} is not auto."
                     for iid, why, key in barred]
    if attempts < cap:
        spoken_for_lines += [f"- {iid} {why} — (spoken for: GROOM → ADJUDICATE)"
                             for iid, why in open_]
    else:
        for_you_lines += [f"NEEDS OPERATOR: {iid} — {why}" for iid, why in open_]
    for t in answers_done_texts:
        for line in t.splitlines():
            s = line.strip()
            if s.startswith('NEEDS OPERATOR:'):
                for_you_lines.append(s)

    summary = (f"{len(rule_lines)} answered by rule · {len(adjudicator_lines)} ruled by the "
              f"adjudicator · {len(spoken_for_lines)} spoken for · {len(for_you_lines)} for you")

    blocks = [f"# Groom digest {date}", summary]
    for title, lines in (('Answered by rule', rule_lines),
                         (f'Ruled by the adjudicator (groom-{date})', adjudicator_lines),
                         ('Spoken for', spoken_for_lines),
                         ('For you', for_you_lines)):
        blocks.append(f"## {title}\n" + ('\n'.join(lines) if lines else '(none)'))
    blocks.append(cli.stamp('groom', repo=root))
    return '\n\n'.join(blocks) + '\n'


def write_digest(root, date, canonical, groom_text, answers_done_texts, attempts, cap):
    text = render_digest(root, date, canonical, groom_text, answers_done_texts, attempts, cap)
    groom_dir = os.path.join(root, 'groom')
    os.makedirs(groom_dir, exist_ok=True)
    path = os.path.join(groom_dir, f"{date}-digest.md")
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    return path
