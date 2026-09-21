"""match.py — attribute a factory event (a CI run, a session, a PR) to backlog items.

Reads `index.json` (never the md files) and never invents an id: every id returned
exists in the index. The rules run in order and the first one that finds anything wins:

  1. an explicit id token `\\b[EFSTBDR]-\\d{4}\\b` in the PR title/body, the branch name or the
     task name (tokens of Decisions/Rules only count when nothing else matches — a PR that merely
     cites [[D-0292]] belongs to the work, not to the ruling)
  2. `links.prs` contains the PR number
  3. `links.branches` contains the branch
  4. `legacy_id` equals the spec slug in the branch/task name (`worker/spec-free-plan` → `free-plan`;
     `worker/free-plan-t3` → the Task with legacy_id `FREE-1/T3` if present, else the Feature).
     The branch's directory part (through its last `/`) is dropped, then any of the product's
     `conventions.branch_prefixes` (pass `prefixes=`, e.g. `branch_prefixes(product)`) — a
     prefix with no `/` in it, like a legacy one, only goes when it is passed
     A Feature is also found by its slugified title, because legacy ids (`FREE-1`) are not slugs
  5. a review file name on the branch (`.sdd-input/reviews/spec-free-plan-r2.md`)

A batch run carries the items of every PR in the batch: pass `prs=[…]` (and `pr_info` for their
titles/bodies when known) and the result is the union of each PR matched on its own.

    ids, why = match_event(items, branch="worker/free-plan-t3", pr=623)
"""
import json
import os
import re

ID_TOKEN = re.compile(r'\b[EFSTBDR]-\d{4}\b')
WORK_TYPES = {'epic', 'feature', 'story', 'task', 'bug'}
ROLE_PREFIX = re.compile(
    r'^((fix|review|prereview|rebase|remerge|bouncefix|bounce|adjudicate|diag|rr\d*|spec|plan|'
    r'preflight|probe|relaunch|revise|hotfix|code|job)-)+')
ROUND_SUFFIX = re.compile(r'(-r\d+[a-z]?)+$')
TASK_SUFFIX = re.compile(r'^(.+)-(t\d+[a-z]?)$')
REVIEW_FILE = (
    re.compile(r'(?:^|/)(?:spec|plan)-([^/]+)-r\d+\.md$'),
    re.compile(r'(?:^|/)([^/]+)-(?:spec-|plan-)?review-r\d+\.md$'),
)


def load_index(root):
    """The `items` map of `<root>/index.json`; {} when there is none."""
    path = os.path.join(root, 'index.json')
    if not os.path.isfile(path):
        return {}
    with open(path, encoding='utf-8') as f:
        return json.load(f).get('items', {})


def slugify(text):
    return re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')


def _norm_branch(b):
    b = (b or '').strip()
    for p in ('refs/heads/', 'origin/'):
        if b.startswith(p):
            b = b[len(p):]
    return b


def branch_prefixes(product):
    """Every branch prefix the product's conventions name (code/fix/spec/plan and legacy ones),
    longest first — the `prefixes=` argument of `match_event`."""
    conv = (getattr(product, 'conventions', None) or {}).get('branch_prefixes') or {}
    out = []
    for k, v in conv.items():
        out.extend(v if isinstance(v, list) else [v])
    return tuple(sorted({str(p).lower() for p in out if p}, key=len, reverse=True))


def _strip_name(name, prefixes=()):
    """A branch or task name → the bare spec/task slug: prefixes, role words and -rN tails off."""
    n = _norm_branch(name).lower()
    for p in prefixes:
        if n.startswith(p):
            n = n[len(p):]
            break
    n = n.rsplit('/', 1)[-1]
    n = ROLE_PREFIX.sub('', n)
    return ROUND_SUFFIX.sub('', n)


def _links(item, key):
    v = (item.get('links') or {}).get(key) or []
    return v if isinstance(v, list) else [v]


def _find_legacy(items, slug):
    """Items whose legacy_id equals slug; else Features whose slugified title does."""
    hits = [i for i, it in items.items() if it.get('legacy_id')
            and slug in (str(it['legacy_id']).lower(), slugify(str(it['legacy_id'])))]
    if hits:
        return sorted(hits)
    return sorted(i for i, it in items.items() if it.get('type') == 'feature' and slugify(it.get('title')) == slug)


def _descendants(items, iid):
    out, todo = [], list(items.get(iid, {}).get('children') or [])
    while todo:
        c = todo.pop()
        if c in items and c not in out:
            out.append(c)
            todo.extend(items[c].get('children') or [])
    return out


def _by_legacy(items, names, prefixes=()):
    for name in names:
        base = _strip_name(name, prefixes)
        if not base:
            continue
        hits = _find_legacy(items, base)
        if hits:
            return hits
        m = TASK_SUFFIX.match(base)
        if not m:
            continue
        slug, tn = m.groups()
        for fid in _find_legacy(items, slug):
            want = f"{items[fid].get('legacy_id', '')}/{tn}".lower()
            scope = [fid] + _descendants(items, fid)
            tasks = [i for i in scope if items[i].get('type') == 'task'
                     and str(items[i].get('legacy_id', '')).lower() in (want,)]
            tasks = tasks or [i for i in scope if items[i].get('type') == 'task'
                              and str(items[i].get('legacy_id', '')).lower().endswith('/' + tn)]
            return sorted(tasks) if tasks else [fid]
    return []


def _by_review_file(items, files):
    for f in files or ():
        for rx in REVIEW_FILE:
            m = rx.search(f)
            if m:
                hits = _find_legacy(items, m.group(1).lower())
                if hits:
                    return hits
    return []


def _match_one(items, task=None, branch=None, pr=None, title=None, body=None, files=(), prefixes=()):
    fallback = []
    texts = [t for t in (title, body, branch, task) if t]
    tokens = {t for text in texts for t in ID_TOKEN.findall(text) if t in items}
    work = sorted(t for t in tokens if items[t].get('type') in WORK_TYPES)
    if work:
        return work, 'id token'
    fallback = sorted(tokens)
    if pr is not None:
        hits = sorted(i for i, it in items.items() if int(pr) in [int(p) for p in _links(it, 'prs')])
        if hits:
            return hits, 'links.prs'
    b = _norm_branch(branch)
    if b:
        hits = sorted(i for i, it in items.items() if b in [_norm_branch(x) for x in _links(it, 'branches')])
        if hits:
            return hits, 'links.branches'
    hits = _by_legacy(items, [n for n in (branch, task) if n], prefixes)
    if hits:
        return hits, 'legacy_id'
    hits = _by_review_file(items, files)
    if hits:
        return hits, 'review file'
    if fallback:
        return fallback, 'id token (decision/rule only)'
    return [], None


def match_event(items, task=None, branch=None, pr=None, title=None, body=None, files=(), prs=None,
                pr_info=None, prefixes=()):
    """(ids, reason). `ids` is a sorted list, empty when nothing matched; `reason` names the rule that
    matched, or says why nothing did."""
    if prs:
        found, rules = set(), set()
        for n in prs:
            info = (pr_info or {}).get(n) or (pr_info or {}).get(str(n)) or {}
            ids, why = _match_one(items, pr=n, title=info.get('title'), body=info.get('body'),
                                  branch=info.get('branch'), prefixes=prefixes)
            if ids:
                found.update(ids)
                rules.add(why)
        if found:
            return sorted(found), 'batch: ' + ', '.join(sorted(rules))
        return [], f"batch of {len(prs)} PR(s), none matched an item"
    ids, why = _match_one(items, task=task, branch=branch, pr=pr, title=title, body=body, files=files,
                          prefixes=prefixes)
    if ids:
        return ids, why
    tried = [k for k, v in (('task', task), ('branch', branch), ('pr', pr), ('title', title), ('files', files)) if v]
    return [], 'no rule matched (' + ', '.join(tried) + ')' if tried else 'nothing to match on'
