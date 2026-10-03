"""F-0166 ripple check (scratch, not committed): narrowed scan_text, widened hook trigger."""
import io
import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from asf.record import core, check  # noqa: E402

DERIVED_LINE_RE = re.compile(r'^- \[([A-Z]-\d{4})\]\(\.\./[a-z]+/\1\.md\)( |$)')
DERIVED_HEADINGS = ('## Children', '## Backlinks')

# ---- 1. scan_text narrowed to the index-owned bullets only ------------------
orig_scan_text = core.scan_text


def scan_text(rec):
    typed, machine = core.frontmatter.split_machine(rec['meta'])
    parts = []
    core._flatten_strings(typed, parts)
    core._flatten_strings(machine, parts)
    preamble, sections = core.parse_sections(rec['body'])
    body_text = preamble
    for heading, content in sections:
        if heading.strip() in DERIVED_HEADINGS:
            content = '\n'.join(l for l in content.split('\n')
                                if not DERIVED_LINE_RE.match(l))
        body_text += heading + content
    parts.append(body_text)
    return '\n'.join(parts)


def _rec(iid, body='', parent=None, **meta):
    m = {'id': iid, 'type': 'task', 'title': iid}
    if parent:
        m['parent'] = parent
    m.update(meta)
    return {'meta': m, 'body': body, 'folder': 'tasks'}


FIXTURE = [
    _rec('F-0001', 'the feature'),
    _rec('T-0001', 'after F-0001; see T-0002.', parent='F-0001'),
    _rec('T-0002', 'xT-0001 T-00012 T-0001a F-0001-2 (T-0003)'),
    _rec('T-0003', '## Backlinks\n- T-0002\n', notes='mentions T-0002 in meta'),
    _rec('T-0004', 'F-0001_x and "T-0003" and t-0002'),
    _rec('odd-id', 'names T-0004'),
    _rec('T-0005', 'the odd-id card, and odd-idx is not it'),
]
canon = {r['meta']['id']: r for r in FIXTURE}
before = core.compute_derived(canon)
core.scan_text = scan_text
after = core.compute_derived(canon)
core.scan_text = orig_scan_text
print('=== tests/test_record_mentions.py fixture, before vs after the narrowing')
for iid in canon:
    if before[iid] != after[iid]:
        print(f'    CHANGED {iid}: {before[iid]} -> {after[iid]}')
print('    (no CHANGED line above = the existing assertions still hold)')

# a title in a derived bullet that itself names a card must stay excluded
canon2 = {
    'F-0001': _rec('F-0001', ''),
    'F-0002': _rec('F-0002', '## Children\n\n## Backlinks\n'
                             '- [T-0009](../tasks/T-0009.md) Rework F-0001 parsing\n'),
    'T-0009': _rec('T-0009', 'nothing'),
}
core.scan_text = scan_text
d2 = core.compute_derived(canon2)
core.scan_text = orig_scan_text
print(f"    a derived bullet whose title names F-0001 -> F-0001 backlinks "
      f"{d2['F-0001']['backlinks']} (must be [])")

# a hand note parked in Backlinks must now create a backlink
canon3 = {
    'F-0001': _rec('F-0001', ''),
    'F-0002': _rec('F-0002', '## Backlinks\n\nNote: superseded by F-0001.\n'),
}
d3_before = core.compute_derived(canon3)
core.scan_text = scan_text
d3_after = core.compute_derived(canon3)
core.scan_text = orig_scan_text
print(f"    a hand note in Backlinks naming F-0001 -> before {d3_before['F-0001']['backlinks']}, "
      f"after {d3_after['F-0001']['backlinks']}")

# does the sample record change at all under the narrowing?
tmp = tempfile.mkdtemp(prefix='f0166-sample-')
rec_dir = os.path.join(tmp, 'rec')
shutil.copytree(os.path.join(ROOT, 'sample', 'backlog'), rec_dir)
by_id, _e = core.load_items(rec_dir)
c, _d = core.canonicalize(by_id)
s_before = core.compute_derived(c)
core.scan_text = scan_text
s_after = core.compute_derived(c)
core.scan_text = orig_scan_text
print(f'    sample/backlog derived state unchanged by the narrowing: {s_before == s_after}')
shutil.rmtree(tmp)


# ---- 2. the hook trigger widened to a card's own stale sections -------------
SECTION_STALE_RE = re.compile(r'^## (Children|Backlinks) section is stale ')


def derived_stale(findings):
    return {(p, m) for p, _l, m in findings if SECTION_STALE_RE.match(m)}


def git(repo, *a):
    return subprocess.run(['git', '-C', repo] + list(a), capture_output=True, text=True)


def hook_case(name, mutate, widened):
    tmp = tempfile.mkdtemp(prefix='f0166-hook2-')
    repo = os.path.join(tmp, 'repo')
    shutil.copytree(os.path.join(ROOT, 'sample', 'backlog'), os.path.join(repo, 'backlog'))
    git(repo, 'init', '-q')
    git(repo, 'config', 'user.email', 'a@b.c')
    git(repo, 'config', 'user.name', 'a')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-qm', 'base')
    card = os.path.join(repo, 'backlog', 'features', 'F-0001.md')
    with open(card, encoding='utf-8') as f:
        text = f.read()
    with open(card, 'w', encoding='utf-8') as f:
        f.write(mutate(text))
    git(repo, 'add', '-A')
    orig = check.cmd_check_staged
    if widened:
        check.cmd_check_staged = patched_cmd_check_staged
    out = io.StringIO()
    cwd = os.getcwd()
    os.chdir(os.path.join(repo, 'backlog'))
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = check.cmd_check_staged(os.path.join(repo, 'backlog'))
    finally:
        os.chdir(cwd)
        check.cmd_check_staged = orig
    print(f'    {name} [{"widened" if widened else "today"}]: rc={rc} '
          f'({"REFUSED" if rc else "passes"})')
    for line in out.getvalue().strip().splitlines():
        print('        ' + line)
    shutil.rmtree(tmp)


def patched_cmd_check_staged(root):
    import tempfile as _tf
    from asf.record import tree
    from asf.record.core import title_scrub
    found = check.committing_repo(root)
    repo, prefix = found
    staged = check.staged_paths(repo, prefix)
    if not staged:
        return 0
    scrub = title_scrub(root)
    with _tf.TemporaryDirectory(prefix='asf-check-') as scratch:
        head_dir = os.path.join(scratch, 'head')
        staged_dir = os.path.join(scratch, 'staged')
        tree.lay_out_head(repo, head_dir, tree.record_paths(prefix), scratch)
        tree.lay_out(repo, staged_dir, tree.record_paths(prefix))
        base = check.record_findings(os.path.join(head_dir, prefix), scrub, layout=False)
        now = check.record_findings(os.path.join(staged_dir, prefix), scrub, layout=False)
        if (now[2] - base[2]) or (derived_stale(now[0]) - derived_stale(base[0])):
            if check.stage_derived(repo, prefix, os.path.join(staged_dir, prefix), scrub):
                now = check.record_findings(os.path.join(staged_dir, prefix), scrub, layout=False)
    return check._report_staged(now, base, staged)


print('\n=== the hook, today vs with the trigger widened')
drop_backlink = lambda t: t.replace('- [T-0001](../tasks/T-0001.md) Split on whitespace\n', '')
drop_child = lambda t: t.replace(
    '- [S-0002](../stories/S-0002.md) Empty input counts zero — New\n', '')
for w in (False, True):
    hook_case('a stale ## Backlinks list, index.json correct', drop_backlink, w)
for w in (False, True):
    hook_case('a stale ## Children list, index.json correct', drop_child, w)
for w in (False, True):
    hook_case('nothing stale (a plain edit)',
              lambda t: t.replace('## Description\n', '## Description\nplain prose.\n'), w)
