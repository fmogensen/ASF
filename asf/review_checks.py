"""asf.review_checks — ``asf review-checks``: the mechanical pre-review, and what it refuses.

Reads one review file and derives its verdict from the check table alone (:mod:`asf.reviews`),
never from a typed word. Unlike :func:`asf.reviews.verdict`, a file with no table at all is not
the legacy ``''``: this command refuses it outright (D4), naming the fault ``no check table``.
"""
import json
import os
import sys

from asf import proves, reviews
from asf.record import match
from asf.record.core import as_list


def _card_required(root, card_id, items):
    """The normalized acceptance lines ``card_id`` adds to the required set: a Task's own
    Stories' lines (its ``stories:``), a Story's own — ``None`` when ``card_id`` names nothing
    in the record."""
    entry = items.get(card_id)
    if entry is None:
        return None
    if entry.get('type') == 'story':
        story_ids = [card_id]
    elif entry.get('type') == 'task':
        story_ids = as_list(entry.get('stories'))
    else:
        story_ids = []
    lines = []
    for sid in story_ids:
        story = items.get(sid)
        if story:
            lines.extend(proves.card_bullets(root, story))
    return [reviews.normalize(line) for line in lines]


def _fault_line(basename, fault):
    """One ``reviews.faults`` entry, prefixed ``<basename>:<line>: `` when it names a line, else
    ``<basename>: `` (a ``required check missing:`` line, which has none)."""
    if fault.startswith('line '):
        n, _, rest = fault[len('line '):].partition(': ')
        return f'{basename}:{n}: {rest}'
    return f'{basename}: {fault}'


def cmd_review_checks(args, record):
    try:
        with open(args.path, encoding='utf-8') as f:
            text = f.read()
    except OSError as e:
        print(f'{args.path}: {e}', file=sys.stderr)
        return 2

    required = list(reviews.required(args.kind))
    card = getattr(args, 'card', None)
    if card:
        items = match.load_index(record) if record else {}
        extra = _card_required(record, card, items)
        if extra is None:
            print(f'{card}: not in the record', file=sys.stderr)
            return 2
        if args.kind == 'code':
            required += extra
    required = tuple(required)

    checks, structural = reviews.parse(text)
    if not checks and not structural:
        faults, verdict = ['no check table'], reviews.BOUNCE
    else:
        faults, verdict = reviews.faults(text, required), reviews.verdict(text, required)

    if getattr(args, 'json', False):
        payload = {
            'path': args.path, 'kind': args.kind, 'verdict': verdict, 'faults': faults,
            'checks': [c._asdict() for c in checks],
        }
        print(json.dumps(payload, sort_keys=True))
    else:
        basename = os.path.basename(args.path)
        print(f'== REVIEW {basename}  kind {args.kind}, {len(checks)} checks, {len(faults)} faults')
        for fault in faults:
            print(_fault_line(basename, fault))
        print(f'verdict: {verdict}')

    return 1 if faults else 0
