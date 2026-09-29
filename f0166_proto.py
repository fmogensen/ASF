"""F-0166 prototype (scratch, not committed): a derived section that keeps the card's own lines."""
import re

# a derived bullet: the link's target is the bullet's own id, in an item folder — exactly the
# shape children_lines()/backlinks_lines() write, for any id they can write it for
ITEM_FOLDERS = ['epics', 'features', 'stories', 'tasks', 'bugs', 'decisions', 'rules']
DERIVED_LINE_RE = re.compile(
    r'^- \[([^\]]+)\]\(\.\./(?:' + '|'.join(ITEM_FOLDERS) + r')/\1\.md\)(?: |$)')


def section_content(lines, is_last, current=None):
    """The derived section's content: ``lines`` (the derived bullets) written where the section's
    existing bullets stood, every other line of ``current`` kept verbatim and in order."""
    if current is None:
        text = '\n' + ''.join(f'{l}\n' for l in lines)
        return text + '\n' if not is_last else text
    # content always opens with the newline that ended the heading line
    body = current[1:] if current.startswith('\n') else current
    own = [l for l in body.split('\n')]
    if own and own[-1] == '':
        own.pop()  # the newline ending the last line, not a line
    at = next((i for i, l in enumerate(own) if DERIVED_LINE_RE.match(l)), None)
    kept = [l for l in own if not DERIVED_LINE_RE.match(l)]
    if at is None:
        at = 0
    else:
        at -= sum(1 for l in own[:at] if DERIVED_LINE_RE.match(l))
    merged = kept[:at] + list(lines) + kept[at:]
    while merged and merged[-1] == '':
        merged.pop()  # the section's own trailing blank is the separator, re-added below
    text = '\n' + ''.join(f'{l}\n' for l in merged)
    return text + '\n' if not is_last else text


def show(name, lines, is_last, current):
    once = section_content(lines, is_last, current)
    twice = section_content(lines, is_last, once)
    ok = 'stable' if once == twice else 'NOT IDEMPOTENT'
    print(f'--- {name}: {ok}\n    in   {current!r}\n    out  {once!r}')
    if once != twice:
        print(f'    twice {twice!r}')


D = ['- [T-0002](../tasks/T-0002.md) Count lines']
show('note appended after the list, last section', D, True,
     '\n- [T-0001](../tasks/T-0001.md) Split\n\nNote 2026-09-26: a hand note.\n')
show('note before the list, last section', D, True,
     '\n- [T-0001](../tasks/T-0001.md) Split\n')
show('empty section gaining a bullet, last', D, True, '\n')
show('empty section staying empty, last', [], True, '\n')
show('empty section staying empty, middle', [], False, '\n\n')
show('middle section with a note after the list', D, False,
     '\n- [T-0001](../tasks/T-0001.md) Split\n\nnote\n\n')
show('note wedged between heading and list', D, True,
     '\nNote: read me\n- [T-0001](../tasks/T-0001.md) Split\n')
show('History line appended bare after the list', D, True,
     '\n- [T-0001](../tasks/T-0001.md) Split\n- 2026-09-26: appended by hand\n')
show('already correct, last', D, True, '\n- [T-0002](../tasks/T-0002.md) Count lines\n')
show('already correct, middle', D, False, '\n- [T-0002](../tasks/T-0002.md) Count lines\n\n')
show('a child bullet carrying its state', ['- [S-0001](../stories/S-0001.md) A — New'], True,
     '\n- [S-0001](../stories/S-0001.md) A — Done\n\nnote\n')
show('section emptied while a note stays', [], True,
     '\n- [T-0001](../tasks/T-0001.md) Split\n\nNote: keep me\n')
show('an id of another shape', ['- [odd-id](../tasks/odd-id.md) Odd'], True,
     '\n- [odd-id](../tasks/odd-id.md) Old title\n\nnote\n')
show('a hand bullet linking a path the index never writes', D, True,
     '\n- [T-0001](../tasks/T-0001.md) Split\n- [see the spec](../../docs/specs/f-0001.md) ref\n')
show('a bullet with an empty title (a card whose title is blank)',
     ['- [T-0002](../tasks/T-0002.md) '], True,
     '\n- [T-0002](../tasks/T-0002.md) \n\nnote\n')
