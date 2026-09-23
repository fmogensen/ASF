"""asf.groom.shape — the shape rules: derive(card, canonical, default_bug_parent) decides an
inbox card's type, or asks the one Question that would settle it. Pure over the parsed card and
the record; the only place a type is chosen."""
import re
from collections import namedtuple

from asf.record.core import PARENT_TYPES, is_open, tokenize

Shape = namedtuple('Shape', 'type rule parent')
Question = namedtuple('Question', 'text')
Card = namedtuple('Card', 'title headers description features acceptance')

SIZE = {  # the card's definitions, in one place; the History line and asf check quote them
    'epic': 'a business outcome spanning several Features',
    'feature': 'one spec and one plan, landing as one deployable thing',
    'story': 'one PR with one acceptance list',
    'task': 'one session in one worktree with a writes: footprint',
    'bug': 'a defect with a signature',
}

RULES = ('signature', 'writes', 'features-list', 'parent-feature', 'default')

SHAPE_LINE_RE = re.compile(r'created \(([^)]*)\) — shape: (\S+) → (\w+)')

DEFECT_WORDS_RE = re.compile(r'\b(broken|red|fails?|failing)\b', re.IGNORECASE)


def infer_parent_epic(canonical, tokens):
    """The open Epic sharing the most title words with `tokens`, or None."""
    best_id, best_n = None, 0
    for iid, rec in sorted(canonical.items()):
        if rec['meta'].get('type') != 'epic' or not is_open(rec):
            continue
        shared = len(tokens & tokenize(rec['meta'].get('title', '')))
        if shared > best_n:
            best_id, best_n = iid, shared
    return best_id


def _typed(card, shape):
    """D4: a `type:` line that disagrees with the reading turns a Shape into a Question."""
    t = card.headers.get('type')
    if t and t.lower() != shape.type:
        return Question(
            f"`type: {t}` is not read — the card's shape reads as {shape.type} "
            f"({shape.rule}: {SIZE[shape.type]}). Remove the line, or change the shape."
        )
    return shape


def derive(card, canonical, default_bug_parent=None):
    headers = card.headers
    signature = headers.get('signature')
    writes = headers.get('writes')
    parent = headers.get('parent')

    if signature and writes:
        return Question(
            'A card is one thing: signature: makes it a Bug, writes: makes it a Task — drop one.'
        )

    if parent and parent not in canonical:
        return Question(f"`parent: {parent}` does not exist — name an existing item or remove the line.")

    if signature:
        if parent:
            ptype = canonical[parent]['meta'].get('type')
            if ptype not in PARENT_TYPES['bug']:
                return Question(
                    f"`parent: {parent}` is a {ptype} — a Bug hangs under an Epic, a Feature or a Story."
                )
            bug_parent = parent
        elif default_bug_parent:
            bug_parent = default_bug_parent
        else:
            return Question('Which item is this Bug under? Add a `parent: <id>` line.')
        return _typed(card, Shape('bug', 'signature', bug_parent))

    if writes:
        if not parent or canonical[parent]['meta'].get('type') not in PARENT_TYPES['task']:
            return Question('A Task hangs under a Feature or a Story — add parent: <id>.')
        return _typed(card, Shape('task', 'writes', parent))

    if len(card.features) >= 2:
        if parent:
            return Question(
                'An Epic has no parent — drop the parent: line, or it is a Feature under that Epic.'
            )
        return _typed(card, Shape('epic', 'features-list', None))

    if parent:
        ptype = canonical[parent]['meta'].get('type')
        if ptype == 'feature':
            if not card.acceptance:
                return Question(
                    'A Story is one PR with one acceptance list — add an ## Acceptance checklist.'
                )
            return _typed(card, Shape('story', 'parent-feature', parent))
        if ptype == 'story':
            return Question(
                'Under a Story only a Task hangs — add a writes: line, or parent the card to the Feature.'
            )

    explicit_feature = (headers.get('type') or '').lower() == 'feature'  # the answer to it
    if (not card.acceptance and not explicit_feature
            and DEFECT_WORDS_RE.search(f"{card.title} {card.description}")):
        return Question(
            'This reads as a defect. A Bug carries a signature — add signature: <the failing '
            'test or error line>; or an ## Acceptance list if it is new work.'
        )

    if parent:
        ptype = canonical[parent]['meta'].get('type')
        if ptype != 'epic':
            return Question(f"`parent: {parent}` is a {ptype} — a Feature hangs under an Epic.")
        default_parent = parent
    else:
        default_parent = infer_parent_epic(canonical, tokenize(card.title))
        if default_parent is None:
            return Question('Which Epic is this under? No open Epic shares a title word with it.')
    return _typed(card, Shape('feature', 'default', default_parent))
