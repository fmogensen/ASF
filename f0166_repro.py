"""F-0166 reproduction (scratch, not committed): does `asf index` preserve hand-added lines?"""
import difflib
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from asf.record.index import refresh  # noqa: E402
from asf.record.core import parse_sections  # noqa: E402


def trial(name, mutate, card='features/F-0001.md'):
    tmp = tempfile.mkdtemp(prefix='f0166-')
    rec = os.path.join(tmp, 'rec')
    shutil.copytree(os.path.join(ROOT, 'sample', 'backlog'), rec)
    p = os.path.join(rec, card)
    with open(p, encoding='utf-8') as f:
        before = mutate(f.read())
    with open(p, 'w', encoding='utf-8') as f:
        f.write(before)
    written = refresh(rec)
    with open(p, encoding='utf-8') as f:
        after = f.read()
    print(f'--- {name}: {"PRESERVED" if before == after else "DROPPED"} '
          f'(cards written={[w for w in written if w != "index.json"]})')
    if before != after:
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(),
                                         'before', 'after', lineterm='', n=2):
            print('   ' + line)
    shutil.rmtree(tmp)


trial('bare note appended at end of card (Backlinks is last)',
      lambda t: t + '\nNote 2026-09-26: a hand-appended note.\n')
trial('History line appended bare at end of card',
      lambda t: t + '- 2026-09-26: appended by hand\n')
trial('note under its own new ## heading at end of card',
      lambda t: t + '\n## Notes\nhand note under its own heading\n')
trial('note inside ## Children (a middle section)',
      lambda t: t.replace('## Children\n', '## Children\n<!-- hand note inside Children -->\n'))
trial('note between the Backlinks heading and its list',
      lambda t: t.replace('## Backlinks\n', '## Backlinks\nNote: read me\n'))

body = '## Description\nx\n\n## Backlinks\n- [T-0001](../tasks/T-0001.md) t\n\nNote: hand\n'
print('--- parse_sections over a card whose note follows the Backlinks list:')
pre, secs = parse_sections(body)
print(f'    preamble={pre!r}')
for h, c in secs:
    print(f'    {h!r} -> {c!r}')
