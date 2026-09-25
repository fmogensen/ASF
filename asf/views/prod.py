"""asf.views.prod — the ``PROD`` table (``asf prod``): what's live, and what just shipped.

A reduced port of the operator's tick closing block's ENV line + "ON PROD" section — the parts
of that block that are pure deploy state (:mod:`asf.env`'s ``Product.deploy_sha``/
``customer_paths`` conventions carry exactly the discovery rules the original script had
hardcoded). The rest of that block (ON DEV, NEXT, WORKING NOW, SPAWNING, QUOTA, CI POOL) is
``asf next``, ``asf sessions`` and ``asf status``. A product with ``deploy_sha: none`` gets one
line: ``no deploy configured``.
"""
import json
import re
import subprocess

STRIP_PREFIX = re.compile(r'^[a-z]+(\([^)]*\))?[!]?:\s*')


def _sh(cmd, cwd=None, timeout=60):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return out.stdout.strip() if out.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        return ''


def _gh_json(args, repo_slug, timeout=60):
    out = _sh(['gh'] + args, timeout=timeout)
    try:
        return json.loads(out) if out else []
    except json.JSONDecodeError:
        return []


def deploy_configured(product):
    """False for ``deploy_sha: none`` (or no ``deploy_sha`` at all): nothing is deployed."""
    d = product.deploy_sha
    return isinstance(d, dict) and bool(d)


def _deploy_sha(product, kind):
    """(sha, iso_time) for a ``deploy_sha.<kind>`` rule from the product config, or (None, None)."""
    if not deploy_configured(product):
        return None, None
    cfg = product.deploy_sha.get(kind) or {}
    source = cfg.get('source')
    if source == 'github-deployments':
        workflow = cfg.get('workflow')
        args = ['run', 'list', '-R', product.repo_slug, '--workflow', workflow, '--limit', '15',
                '--json', 'headSha,conclusion,updatedAt,headBranch']
        if cfg.get('branch'):
            args += ['--branch', cfg['branch']]
        runs = _gh_json(args, product.repo_slug)
        for r in runs:
            if r.get('conclusion') == 'success':
                return r.get('headSha'), r.get('updatedAt')
        return None, None
    if source == 'vercel':
        out = _sh(['vercel', 'ls', cfg.get('project', ''), '--prod', '--scope', cfg.get('scope', ''),
                   '--json'])
        try:
            parsed = json.loads(out) if out else []
        except json.JSONDecodeError:
            parsed = []
        deployments = parsed.get('deployments', []) if isinstance(parsed, dict) else parsed
        for d in deployments:
            if isinstance(d, dict) and d.get('state') == 'READY':
                return (d.get('meta') or {}).get('githubCommitSha'), d.get('createdAt')
        return None, None
    return None, None


def _local(ts):
    import datetime as dt
    if not ts:
        return '?'
    try:
        if isinstance(ts, (int, float)):
            t = dt.datetime.fromtimestamp(ts / 1000, tz=dt.timezone.utc)
        else:
            t = dt.datetime.fromisoformat(ts.replace('Z', '+00:00'))
        return t.astimezone().strftime('%H:%M')
    except (ValueError, OSError):
        return '?'


def _strip_prefix(title):
    return STRIP_PREFIX.sub('', title).strip()


def _glob_re(glob):
    """A path glob as a regex: ``**`` crosses ``/``, ``*`` and ``?`` do not; a trailing ``/`` is a directory."""
    if glob.endswith('/'):
        glob += '**'
    out, i = [], 0
    while i < len(glob):
        if glob.startswith('**/', i):
            out.append('(?:.*/)?')
            i += 3
        elif glob.startswith('**', i):
            out.append('.*')
            i += 2
        elif glob[i] == '*':
            out.append('[^/]*')
            i += 1
        elif glob[i] == '?':
            out.append('[^/]')
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile(''.join(out) + r'\Z')


def _is_customer(paths, non_customer_hint, changed_files, within=None):
    """True when a non-test file of ``changed_files`` is customer-visible: it matches
    ``paths`` (the product's ``customer_paths``) and, given ``within`` (a deploy target's
    ``paths``), one of those too. With no ``customer_paths``, ``within`` alone decides."""
    cust = [_glob_re(p) for p in paths]
    scope = [_glob_re(p) for p in within or []]
    if not cust and not scope:
        return False
    hit = [f for f in changed_files if (not cust or any(r.match(f) for r in cust))
           and (not scope or any(r.match(f) for r in scope))]
    if not hit:
        return False
    non_test = [f for f in hit if not re.search(r'\.(test|spec)\.(ts|tsx|mjs)$|/e2e/|/__tests__/|\.md$', f)]
    return bool(non_test)


#: a trunk commit that lands a PR: GitHub's merge / squash subjects, or any merge commit whose
#: subject names ``#N`` (a merge queue's own subject)
PR_SUBJECTS = (re.compile(r'Merge pull request #(\d+)'), re.compile(r'\(#(\d+)\)\s*$'))
MERGE_PR = re.compile(r'#(\d+)\b')


def _landings(repo_dir, rng):
    """[(sha, parents, subject, pr)] — the trunk's own commits in ``rng`` (first parent only) that
    land a PR, newest first."""
    log = _sh(['git', 'log', '--first-parent', '--format=%H\t%P\t%s', rng], cwd=repo_dir)
    out = []
    for line in log.splitlines():
        parts = line.split('\t', 2)
        if len(parts) != 3:
            continue
        sha, parents, subject = parts[0], parts[1].split(), parts[2]
        m = next((m for m in (r.search(subject) for r in PR_SUBJECTS) if m), None)
        if not m and len(parents) > 1:
            m = MERGE_PR.search(subject)
        if m:
            out.append((sha, parents, subject, int(m.group(1))))
    return out


def _changed(repo_dir, sha):
    """The files a trunk commit changed against its first parent (a merge's whole PR)."""
    out = _sh(['git', 'diff-tree', '-r', '--no-commit-id', '--name-only', '-m', '--first-parent',
               sha], cwd=repo_dir)
    return [f for f in out.splitlines() if f.strip()]


def _title(repo_dir, sha, parents, subject):
    """What a landing is called: a squash's subject; for a merge, the PR title GitHub puts in its
    body, else the first commit of the merged branch (a merge queue's subject names only #N)."""
    if len(parents) > 1:
        body = _sh(['git', 'log', '-1', '--format=%b', sha], cwd=repo_dir).strip()
        if body:
            return body.splitlines()[0]
        first = _sh(['git', 'log', '--reverse', '--no-merges', '--format=%s',
                     f'{parents[0]}..{parents[-1]}'], cwd=repo_dir).splitlines()
        if first:
            return first[0]
    return subject


def _customer_rows(repo_dir, rng, cust, within, limit):
    rows = []
    for sha, parents, subject, n in _landings(repo_dir, rng):
        if not _is_customer(cust, [], _changed(repo_dir, sha), within):
            continue
        rows.append((_strip_prefix(_title(repo_dir, sha, parents, subject))[:70], n))
        if limit and len(rows) >= limit:
            break
    return rows


#: how many merged-but-not-live PRs a target lists before it says how many more there are
NOT_LIVE_ROWS = 10


def _check_rows(product, targets):
    """The ON PROD rows, per customer-facing target: the customer-visible PRs merged to the trunk
    but not yet on it (NOT LIVE — the gap a manual or stalled target leaves), then the last ones
    it shipped. ``targets`` is [(name, deployed sha, deployed time, paths)]."""
    cust = list(product.customer_paths or [])
    rows = []
    for name, sha, ts, within in targets:
        if not sha:
            continue
        gap = _customer_rows(product.repo_dir, f'{sha}..origin/{product.main}', cust, within,
                             None)
        for what, n in gap[:NOT_LIVE_ROWS]:
            rows.append((name, what, f'NOT LIVE — merged, {name} `{sha[:9]}` lacks it', n))
        if len(gap) > NOT_LIVE_ROWS:
            rows.append((name, f'… and {len(gap) - NOT_LIVE_ROWS} more customer-visible PRs',
                         f'NOT LIVE on {name}', None))
        for what, n in _customer_rows(product.repo_dir, f'{sha}~80..{sha}', cust, within, 5):
            rows.append((name, what, f'live since {_local(ts)}' if ts else 'live', n))
    return rows


def _legacy_kinds(product):
    """``deploy_sha.<kind>`` discovery blocks beyond prod and dev (the old ``site:``) that no
    named target has replaced."""
    d = product.deploy_sha if isinstance(product.deploy_sha, dict) else {}
    targets = d.get('targets') if isinstance(d.get('targets'), dict) else {}
    return [k for k, v in d.items() if k not in ('prod', 'dev', 'targets') and k not in targets
            and isinstance(v, dict) and v.get('source')]


def render(root, product):
    repo_dir = product.repo_dir
    main_sha = _sh(['git', 'rev-parse', '--short=9', f'origin/{product.main}'], cwd=repo_dir) or '?'
    if not deploy_configured(product):
        return (f"**PROD** no deploy configured (`deploy_sha: none`) · **{product.main}** "
                f"`{main_sha}`\n")
    prod_sha, prod_ts = _deploy_sha(product, 'prod')
    dev_sha, dev_ts = _deploy_sha(product, 'dev')

    def short(sha):
        return (sha or '?')[:9]

    from asf.harvest import deploy  # what each environment waits on, or that the tick deploys it
    states = deploy.states(product) if deploy.applies(product) else []
    named = [(e, f) for e, f in states if deploy.is_target(e)]
    head = [f"**Prod app** `{short(prod_sha)}` {_local(prod_ts)}",
            f"**Dev app** `{short(dev_sha)}` {_local(dev_ts)}"]
    for kind in _legacy_kinds(product):
        sha, ts = _deploy_sha(product, kind)
        head.append(f"**{kind.capitalize()}** `{short(sha)}` {_local(ts)}")
    for e, f in named:
        rel = 'relevant ' if f['paths'] else ''
        behind = '?' if f['relevant'] is None else f['relevant']
        head.append(f"**{e.capitalize()}** `{short(f['deployed'])}` {behind} {rel}behind"
                    f" ({f['mode']})")
    out = [' · '.join(head + [f"**{product.main}** `{main_sha}`"])]
    for e, f in states:
        out += ["", f"**{deploy.view_line(product, e, f)}**"]
    out.append("")
    out.append("**ON PROD — check these**")
    out.append("")
    out.append("| Target | What | State | PR |")
    out.append("|---|---|---|---|")
    targets = [('prod', prod_sha, prod_ts, [])]
    targets += [(e, f['deployed'], None, f['paths']) for e, f in named if not f['error']]
    rows = _check_rows(product, targets)
    if not rows:
        out.append("| | (no unticked customer-visible change on prod) | | |")
    for name, what, state, n in rows:
        out.append(f"| {name} | {what} | {state} | {f'#{n}' if n else ''} |")
    return "\n".join(out) + "\n"


def cmd_prod(args, root):
    from asf import env
    product = env.load_product(getattr(args, 'product', None))
    print(render(root, product), end='')
    return 0
