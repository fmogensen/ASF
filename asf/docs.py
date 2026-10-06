"""asf.docs — ``asf docs check``: the one meaning of "the docs build" in this repository.

No HTML, no site, no theme (§1.2). The build is: the pages in ``conv.doc_pages``
(:data:`DEFAULT_DOC_PAGES` when unset) exist, and every relative link and every ``#fragment`` on
each of them resolves against the page that carries it. Stdlib only, read-only, and opens
nothing outside the repo root.

``--at <sha>`` runs the same two checks against the tree of a commit instead of the worktree —
every read through ``git show <sha>:<path>``, every existence check through
``git cat-file -e <sha>:<path>`` (the shape :func:`asf.customer_content.tree_hits` uses): no
checkout, and a tree that cannot be read is not a clean one. :func:`link_complaints` is the one
resolver both forms call, with their reader and existence check as arguments, so neither form can
drift from the other's rules.
"""
import fnmatch
import glob
import json
import os
import re
import subprocess

from asf.conventions import DEFAULT_DOC_PAGES

_LINK = re.compile(r'\[[^\]]*\]\(([^)]+)\)')
_INLINE_CODE = re.compile(r'`[^`\n]+`')
_SCHEME = re.compile(r'^[a-zA-Z][a-zA-Z0-9+.-]*:')
_HEADING = re.compile(r'^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$', re.M)
_ANCHOR_DROP = re.compile(r'[^\w\- ]')
_MAGIC = re.compile(r'[*?\[]')


def _skip_target(target):
    """A link target the check does not resolve: an in-page anchor, an absolute or
    home-relative path, a shell variable, or a URL scheme — the same rule
    ``asf.views.readme._skip_target`` states for the README's own links."""
    return not target or target.startswith(('#', '~', '/', '$')) or bool(_SCHEME.match(target))


def anchors(text):
    """The GitHub-style slug of every ATX heading in ``text``, in document order: lower-case,
    drop everything that is not a word character, a hyphen or a space, then one hyphen per
    space. Runs are **not** collapsed — ``` `/asf:status` — FACTORY STATUS ``` slugs to
    ``asfstatus--factory-status``, the double hyphen where the em dash and its spaces were."""
    out = []
    for m in _HEADING.finditer(text or ''):
        slug = _ANCHOR_DROP.sub('', m.group(1).lower())
        out.append(slug.replace(' ', '-'))
    return out


def _in_ranges(pos, ranges):
    return any(a <= pos < b for a, b in ranges)


def _targets(text):
    """``[(line, target)]`` for every link in ``text`` whose target is not inline code and not
    skipped (PD6): a link inside a backtick span is prose about a link, not one."""
    code_ranges = [m.span() for m in _INLINE_CODE.finditer(text)]
    out = []
    for m in _LINK.finditer(text):
        if _in_ranges(m.start(), code_ranges):
            continue
        target = m.group(1).strip()
        if _skip_target(target):
            continue
        out.append((text.count('\n', 0, m.start()) + 1, target))
    return out


def _doc_pages(conv):
    pages = conv.doc_pages if conv is not None and conv.doc_pages else None
    return list(pages) if pages else list(DEFAULT_DOC_PAGES)


def pages(conv, root):
    """The page set ``conv.doc_pages`` (or :data:`DEFAULT_DOC_PAGES`) names, as paths relative to
    ``root``: a literal entry is kept even when it does not exist (so check 1 can say so), a
    glob entry is expanded against what is actually on disk (silent when it matches nothing)."""
    out, seen = [], set()
    for pat in _doc_pages(conv):
        found = sorted(os.path.relpath(p, root) for p in glob.glob(os.path.join(root, pat))) \
            if _MAGIC.search(pat) else [pat]
        for p in found:
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def _tree_pages(doc_pages, files):
    out, seen = [], set()
    for pat in doc_pages:
        if _MAGIC.search(pat):
            pat_parts = pat.split('/')
            found = sorted(f for f in files if len(f.split('/')) == len(pat_parts)
                           and all(fnmatch.fnmatchcase(a, b)
                                   for a, b in zip(f.split('/'), pat_parts)))
        else:
            found = [pat]
        for p in found:
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def link_complaints(pages, read, exists):
    """The one resolver: ``pages`` is the page set, ``read(path)`` returns a page's text or
    ``None``, ``exists(path)`` says whether a path is there. A target resolves relative to
    ``os.path.dirname`` of the page that carries it; a fragment resolves against ``anchors()``
    of the target page, or of the carrying page when the target is the page itself. Each
    complaint is ``(path, line, what)`` with the line of the carrying page."""
    complaints = []
    texts, page_anchors = {}, {}
    for page in pages:
        if not exists(page):
            complaints.append((page, 0, 'page does not exist'))
            continue
        text = read(page)
        if text is None:
            complaints.append((page, 0, 'page could not be read'))
            continue
        texts[page] = text
        page_anchors[page] = set(anchors(text))
    for page, text in texts.items():
        page_dir = os.path.dirname(page)
        for line, target in _targets(text):
            path_part, _, frag = target.partition('#')
            resolved = os.path.normpath(os.path.join(page_dir, path_part)) if path_part else page
            if not exists(resolved):
                complaints.append((page, line, f'{target}: no such file'))
                continue
            if frag:
                target_anchors = page_anchors.get(resolved)
                if target_anchors is None:
                    target_text = read(resolved)
                    target_anchors = set(anchors(target_text)) if target_text is not None else set()
                if frag not in target_anchors:
                    complaints.append((page, line, f'{target}: no such anchor'))
    return complaints


def _fs_target(root, conv):
    page_list = pages(conv, root)

    def read(path):
        try:
            with open(os.path.join(root, path), encoding='utf-8') as f:
                return f.read()
        except OSError:
            return None

    def exists(path):
        return os.path.exists(os.path.join(root, path))

    return page_list, read, exists


def check(root, conv):
    """The worktree form: the page set exists on disk, every link and fragment resolves
    against it. Returns the complaint list."""
    page_list, read, exists = _fs_target(root, conv)
    return link_complaints(page_list, read, exists)


def _git(repo, args, timeout=30):
    try:
        p = subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def _tree_target(repo, sha, conv):
    listed = _git(repo, ['ls-tree', '-r', '--name-only', sha]) if repo and sha else None
    if listed is None:
        return None
    files = [f for f in listed.splitlines() if f]
    page_list = _tree_pages(_doc_pages(conv), files)

    def read(path):
        return _git(repo, ['show', f'{sha}:{path}'])

    def exists(path):
        return _git(repo, ['cat-file', '-e', f'{sha}:{path}']) is not None

    return page_list, read, exists


def check_at(repo, sha, conv):
    """The tree-at-a-sha form: every read through ``git show``, every existence check through
    ``git cat-file -e`` — no checkout. Returns the complaint list, or ``None`` when the tree
    cannot be read at all (a bad sha, an unreadable repo)."""
    target = _tree_target(repo, sha, conv)
    if target is None:
        return None
    page_list, read, exists = target
    return link_complaints(page_list, read, exists)


def cmd_docs(args):
    from asf import conventions as conventions_mod
    from asf import env
    product_name = getattr(args, 'product', None)
    try:
        product = env.load_product(product_name)
        repo_dir, conv = product.repo_dir, product.conventions
    except env.ConfigError:
        repo_dir, conv = None, conventions_mod.Conventions()
    repo_dir = repo_dir or os.getcwd()

    sha = getattr(args, 'at', None)
    if sha:
        target = _tree_target(repo_dir, sha, conv)
        if target is None:
            print(f'docs: {sha} could not be read')
            return 2
    else:
        target = _fs_target(repo_dir, conv)
    page_list, read, exists = target
    complaints = link_complaints(page_list, read, exists)

    n_links = 0
    for page in page_list:
        if exists(page):
            text = read(page)
            if text is not None:
                n_links += len(_targets(text))

    as_json = getattr(args, 'json', False)
    if as_json:
        print(json.dumps({'ok': not complaints,
                          'complaints': [{'path': p, 'line': l, 'what': w}
                                        for p, l, w in complaints],
                          'pages': len(page_list), 'links': n_links}))
        return 1 if complaints else 0
    for path, line, what in complaints:
        print(f'{path}:{line}: {what}')
    if complaints:
        print(f'docs: {len(complaints)} complaint(s)')
        return 1
    print(f'docs: clean — {len(page_list)} pages, {n_links} links')
    return 0


def register(subparsers):
    p = subparsers.add_parser('docs', help='the docs build: every page exists and every link '
                                           'and fragment on it resolves')
    sub = p.add_subparsers(dest='docs_command', required=True)
    c = sub.add_parser('check', help='check the page set (--product), the worktree or a tree '
                                     'at --at <sha>')
    from asf import env
    env.add_product_arg(c)
    c.add_argument('--at', metavar='SHA', help='check the tree at this sha instead of the '
                                               'worktree — no checkout')
    c.add_argument('--json', action='store_true')
    c.set_defaults(run=cmd_docs)
    return p
