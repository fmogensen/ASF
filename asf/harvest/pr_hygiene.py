#!/usr/bin/env python3
"""pr_hygiene.py — PR hygiene by code.

Reads a product's open PRs and sorts the ones GitHub calls `dirty` (conflicting with main) into two lanes:

  CONFLICT → REBASE  #<n> <branch> (<item>) approved, dirty since <age>   → launch rebase-<slug> (Sonnet)
      an APPROVED PR (the branch's newest review file under the product's `conventions.reviews_dir`,
      else reviewDecision) that has been
      dirty for more than one tick. The review stands: a Sonnet merges main into the branch, the PR's own CI reruns.
  STALE → CLOSE      #<n> <branch> (<item>) unreviewed, dirty <days>d   → close
      a PR with no review at all that has been dirty for more than 7 days. `--close` closes it with a comment naming its
      Task and the reason, and takes the PR and the branch out of the Task's `links.prs` / `links.branches`
      so the feeder re-queues the Task. A PR that no Task matches is never closed (`STALE → NO TASK`, nobody to re-queue it).

Approved PRs are never closed; nobody's branch is pushed to or deleted here.

`mergeable_state` carries no timestamp, so "dirty since" is kept in `~/.ASF/state/<product>/pr-hygiene.json`: the first run that saw the
PR dirty at its current head sha records `first_seen` (now) and `since` (the PR's `updated_at`, the last activity — a PR
nobody has touched for 8 days that we see dirty for the first time is 8 days dirty for our purposes). A new push resets
both, and a PR that turns clean drops out. The rebase lane waits for `first_seen` to be older than one tick (PRH_TICK_S,
default 300 s) — a PR that is dirty for one look is a queue race, not work.

    pr_hygiene.py            print the rows (read-only; PR reads cached 3 minutes under the state dir)
    pr_hygiene.py --close    close the STALE → CLOSE PRs (the tick's one line)
    pr_hygiene.py --lanes    the PR numbers in either lane, one per line (the rule check skips them)
    pr_hygiene.py --product <name>   which product's PRs/checkout to read (default: see asf.env)
"""
import datetime
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import time

from asf import env
from asf.conventions import Conventions
from asf.record import core as backlog
from asf.record import frontmatter
from asf.record import match

CACHE_TTL = 180
STALE_DAYS = 7
TICK_S = int(os.environ.get('PRH_TICK_S', '300'))
VERDICT = re.compile(r'APPROVED|CHANGES REQUESTED|BOUNCE|REVISE', re.I)
VERDICT_LINE = re.compile(r'^[\s*#>-]*verdict\b', re.I)
#: A review file of round `n` inside the product's reviews_dir: `<anything>-review-r<n>[a-z].md`.
REVIEW_ROUND = re.compile(r'-review-r(\d+)[a-z]?\.md$')
DEFAULTS = Conventions()


def cache_dir(product=None):
    """Where the PR reads and the dirty-since state are kept: ``$PRH_CACHE_DIR`` when the
    operator points it somewhere, else the product's own state directory under ``~/.ASF``."""
    return os.environ.get('PRH_CACHE_DIR') or env.state_dir(product)


def state_path(product=None):
    return os.path.join(cache_dir(product), 'pr-hygiene.json')


def run(cmd):
    env = dict(os.environ, PATH='/opt/homebrew/bin:' + os.environ.get('PATH', ''))
    p = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}… failed: {p.stderr.strip()[:200]}")
    return p.stdout


def cached(key, fn, now=None, product=None):
    """`fn()` (JSON-able) cached 3 minutes in the state directory under `key`."""
    now = now or time.time()
    path = os.path.join(cache_dir(product), 'pr-hygiene-' + hashlib.md5(key.encode()).hexdigest()[:12] + '.json')
    try:
        if now - os.stat(path).st_mtime < CACHE_TTL:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
    except (OSError, ValueError):
        pass
    val = fn()
    tmp = f'{path}.{os.getpid()}'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(val, f)
    os.replace(tmp, path)
    return val


def ts(iso):
    return datetime.datetime.strptime(iso, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc).timestamp()


def fetch_open_prs(product=None):
    """(pulls list JSON, {n: mergeable_state JSON}, {n: reviewDecision})."""
    product = product or env.load_product()
    repo = product.repo_slug
    pulls = cached('pulls', lambda: json.loads(run(['gh', 'api', f'repos/{repo}/pulls?state=open&per_page=100'])),
                   product=product)
    decisions = cached('decisions', lambda: {str(d['number']): d.get('reviewDecision') or '' for d in json.loads(run(
        ['gh', 'pr', 'list', '-R', repo, '--state', 'open', '--limit', '100', '--json', 'number,reviewDecision']))},
                       product=product)
    details = {p['number']: cached(f"pull-{p['number']}", lambda n=p['number']: json.loads(
        run(['gh', 'api', f'repos/{repo}/pulls/{n}'])), product=product) for p in pulls}
    return pulls, details, decisions


def normalize(pulls, details, decisions, conv=None):
    """The GitHub JSON → one flat dict per PR: only non-draft PRs into the trunk, whatever their mergeable_state."""
    conv = conv or DEFAULTS
    out = []
    for p in pulls:
        n = p['number']
        if p.get('draft') or (p.get('base') or {}).get('ref', conv.main) != conv.main:
            continue
        out.append({'n': n, 'branch': p['head']['ref'], 'sha': p['head']['sha'], 'title': p.get('title') or '',
                    'body': p.get('body') or '', 'updated': ts(p['updated_at']),
                    'dirty': (details.get(n) or {}).get('mergeable_state') == 'dirty',
                    'decision': (decisions or {}).get(str(n)) or ''})
    return sorted(out, key=lambda x: x['n'])


def parse_verdict(text):
    """A review file's OWN verdict line, else the first verdict word anywhere (next-work.sh reads it the same way)."""
    for line in text.splitlines():
        if VERDICT_LINE.search(line):
            m = VERDICT.search(line)
            if m:
                return m.group(0).upper()
    m = VERDICT.search(text)
    return m.group(0).upper() if m else ''


def branch_verdict(branch, git=None, product=None, conv=None):
    """The verdict of the branch's newest review file, read-only from origin/<branch>; '' = no review file or no
    such ref. Reviews live under the product's `conventions.reviews_dir`."""
    if git is None:
        product = product or env.load_product()
        git = lambda *a: run(['git', '-C', product.repo_dir, *a])
    conv = conv or (product.conventions if product is not None else DEFAULTS)
    try:
        names = git('ls-tree', '-r', '--name-only', f'origin/{branch}', '--', conv.reviews_dir).splitlines()
        rounds = sorted((int(m.group(1)), f) for f in names for m in [REVIEW_ROUND.search(f)] if m)
        return parse_verdict(git('show', f'origin/{branch}:{rounds[-1][1]}')) if rounds else ''
    except RuntimeError:
        return ''


def review_state(pr, verdict):
    """approved | reviewed (a review exists, not approved) | unreviewed."""
    if verdict:
        return 'approved' if verdict == 'APPROVED' else 'reviewed'
    if pr['decision'] == 'APPROVED':
        return 'approved'
    return 'reviewed' if pr['decision'] == 'CHANGES_REQUESTED' else 'unreviewed'


def load_state(product=None):
    try:
        with open(state_path(product), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state, product=None):
    path = state_path(product)
    tmp = f'{path}.{os.getpid()}'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f)
    os.replace(tmp, path)


def age(sec):
    sec = max(0, int(sec))
    return f'{sec // 86400}d' if sec >= 86400 else f'{sec // 3600}h' if sec >= 3600 else f'{sec // 60}m'


def find_items(items, pr):
    """(ids matched by :mod:`asf.record.match`, the Tasks among them)."""
    ids, _why = match.match_event(items, branch=pr['branch'], pr=pr['n'], title=pr['title'], body=pr['body'])
    return ids, [i for i in ids if items[i].get('type') == 'task']


def classify(prs, items, verdict_of, state, now):
    """(rows, new_state). A row: {kind: REBASE|CLOSE|NO TASK, pr, ids, tasks, since, ready}; `ready` is False for an approved PR
    first seen dirty within the last tick (it is in the lane — R-0032 skips it — but draws no row yet). `verdict_of(branch)`
    reads a review."""
    rows, new_state = [], {}
    for pr in prs:
        if not pr['dirty']:
            continue
        rec = state.get(str(pr['n']))
        if not rec or rec.get('sha') != pr['sha']:
            rec = {'sha': pr['sha'], 'first_seen': now, 'since': min(now, pr['updated'])}
        new_state[str(pr['n'])] = rec
        rs = review_state(pr, verdict_of(pr['branch']))
        ids, tasks = find_items(items, pr)
        if rs == 'approved':
            rows.append({'kind': 'REBASE', 'pr': pr, 'ids': ids, 'since': now - rec['since'],
                         'ready': now - rec['first_seen'] > TICK_S})
        elif rs == 'unreviewed' and now - rec['since'] > STALE_DAYS * 86400:
            rows.append({'kind': 'CLOSE' if tasks else 'NO TASK', 'pr': pr, 'ids': ids, 'tasks': tasks,
                         'since': now - rec['since'], 'ready': True})
    return rows, new_state


def item_label(row):
    return ','.join(row['ids'][:2]) or '—'


def render(row, conv=None):
    conv = conv or DEFAULTS
    pr = row['pr']
    head = f"#{pr['n']} {pr['branch']} ({item_label(row)})"
    if row['kind'] == 'REBASE':
        slug = conv.strip_prefix(pr['branch']).replace('/', '-')
        return f"CONFLICT → REBASE  {head} approved, dirty since {age(row['since'])}   → launch rebase-{slug} (Sonnet)"
    days = int(row['since'] // 86400)
    if row['kind'] == 'CLOSE':
        return f"STALE → CLOSE  {head} unreviewed, dirty {days}d   → close"
    return f"STALE → NO TASK  {head} unreviewed, dirty {days}d   → not closed: no Task matches, nothing would re-queue it"


def load_index(product=None):
    product = product or env.load_product()
    return {k: v for k, v in match.load_index(product.backlog_dir).items() if not v.get('removed')}


def compute(now=None, git=None, product=None):
    now = now or time.time()
    product = product or env.load_product()
    conv = product.conventions
    prs = normalize(*fetch_open_prs(product), conv=conv)
    rows, new_state = classify(prs, load_index(product),
                               lambda b: branch_verdict(b, git, product, conv),
                               load_state(product), now)
    save_state(new_state, product)
    return rows


def _append_history_lines(body, new_lines):
    """Append lines to '## History'. A local stand-in for backlog.append_history_lines, which
    is not (yet) part of asf.record.core — built from the section helpers that are."""
    if not new_lines:
        return body
    preamble, sections = backlog.parse_sections(body)
    n = len(sections)
    out_sections = []
    for idx, (heading, content) in enumerate(sections):
        if heading.strip() == '## History':
            lines = [l for l in content.split('\n') if l.strip() != '']
            is_last = idx == n - 1
            content = backlog.section_content(lines + list(new_lines), is_last)
        out_sections.append([heading, content])
    return backlog.render_sections(preamble, out_sections)


def unlink(root, item_id, pr_number, branch, note, now):
    """Take the PR and the branch out of the item's `links` (a key left empty goes) + one History line."""
    paths = glob.glob(os.path.join(root, '*', f'{item_id}.md'))
    if not paths:
        return False
    path = paths[0]
    with open(path, encoding='utf-8') as f:
        meta, _body = frontmatter.parse(f.read(), path=path)
    links = {k: list(v) if isinstance(v, list) else v for k, v in (meta.get('links') or {}).items()}
    for key, keep in (('prs', lambda p: int(p) != pr_number), ('branches', lambda b: b not in (branch, f'origin/{branch}'))):
        if key in links:
            links[key] = [v for v in links[key] if keep(v)]
            if not links[key]:
                del links[key]
    frontmatter.write_typed(path, {'links': links or None})
    with open(path, encoding='utf-8') as f:
        meta2, body2 = frontmatter.parse(f.read(), path=path)
    stamp = time.strftime('%Y-%m-%d %H:%M', time.gmtime(now))
    new_body = _append_history_lines(body2, [f'- {stamp} pr-hygiene: {note}'])
    with open(path, 'w', encoding='utf-8') as f:
        f.write(frontmatter.render(meta2, new_body))
    return True


def close_row(row, gh=None, now=None, product=None):
    """Comment, close, then unlink the Task — in that order, so a failed close leaves the Task's links alone."""
    gh = gh or (lambda *a: run(['gh', *a]))
    now = now or time.time()
    product = product or env.load_product()
    repo = product.repo_slug
    pr, days = row['pr'], int(row['since'] // 86400)
    task = row['tasks'][0]
    gh('api', f"repos/{repo}/issues/{pr['n']}/comments", '-f', 'body=' + (
        f"Closed by PR hygiene: this PR was never reviewed and has conflicted with `main` for {days} days. "
        f"Its Task {task} is re-queued — the PR and branch links are cleared so the feeder builds it again from current "
        f"`main`. The branch `{pr['branch']}` is left in place."))
    gh('api', '-X', 'PATCH', f"repos/{repo}/pulls/{pr['n']}", '-f', 'state=closed')
    for t in row['tasks']:
        unlink(product.backlog_dir, t, pr['n'], pr['branch'],
               f"PR #{pr['n']} ({pr['branch']}) closed — unreviewed, conflicting {days}d; links.prs/branches cleared, re-queued", now)
    return task


def main(argv):
    product_name = None
    if '--product' in argv:
        i = argv.index('--product')
        product_name = argv[i + 1] if i + 1 < len(argv) else None
    try:
        product = env.load_product(product_name)
        rows = compute(product=product)
    except (RuntimeError, OSError, ValueError, KeyError, env.ConfigError) as e:
        print(f'pr_hygiene: {e}', file=sys.stderr)
        return 0    # next-work.sh calls this: no PR data means no rows, never a broken run
    if '--lanes' in argv:
        for r in rows:
            if r['kind'] != 'NO TASK':
                print(r['pr']['n'])   # `ready` or not: the PR is in its lane from the first look
        return 0
    if '--close' in argv:
        for r in rows:
            if r['kind'] != 'CLOSE':
                continue
            try:
                print(f"closed #{r['pr']['n']} {r['pr']['branch']} (Task {close_row(r, product=product)})")
            except RuntimeError as e:
                print(f"pr_hygiene: #{r['pr']['n']} not closed: {e}", file=sys.stderr)
        return 0
    for r in rows:
        if r['ready']:
            print(render(r, product.conventions))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
