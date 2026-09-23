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


def _is_customer(paths, non_customer_hint, changed_files):
    regexes = [_glob_re(p) for p in paths]
    hit = [f for f in changed_files if any(r.match(f) for r in regexes)]
    if not hit:
        return False
    non_test = [f for f in hit if not re.search(r'\.(test|spec)\.(ts|tsx|mjs)$|/e2e/|/__tests__/|\.md$', f)]
    return bool(non_test)


def render(root, product):
    repo_dir = product.repo_dir
    main_sha = _sh(['git', 'rev-parse', '--short=9', f'origin/{product.main}'], cwd=repo_dir) or '?'
    if not deploy_configured(product):
        return (f"**PROD** no deploy configured (`deploy_sha: none`) · **{product.main}** "
                f"`{main_sha}`\n")
    prod_sha, prod_ts = _deploy_sha(product, 'prod')
    dev_sha, dev_ts = _deploy_sha(product, 'dev')
    site_sha, site_ts = _deploy_sha(product, 'site')

    def short(sha):
        return (sha or '?')[:9]

    out = [f"**Prod app** `{short(prod_sha)}` {_local(prod_ts)} · "
           f"**Dev app** `{short(dev_sha)}` {_local(dev_ts)} · "
           f"**Site** `{short(site_sha)}` {_local(site_ts)} · **{product.main}** `{main_sha}`"]
    out.append("")
    out.append("**ON PROD — check these**")
    out.append("")
    out.append("| What | Since | PR |")
    out.append("|---|---|---|")

    paths = product.customer_paths or []
    rows = []
    if prod_sha:
        log = _sh(['git', 'log', '--format=%H %s', f'{prod_sha}~80..{prod_sha}'], cwd=repo_dir)
        for line in log.splitlines():
            sha, _, subject = line.partition(' ')
            m = re.search(r'Merge pull request #(\d+)', subject) or re.search(r'\(#(\d+)\)\s*$', subject)
            if not m:
                continue
            n = int(m.group(1))
            files = _sh(['git', 'show', '--stat', '--format=', sha], cwd=repo_dir).splitlines()
            changed = [f.split('|')[0].strip() for f in files if '|' in f]
            if not _is_customer(paths, [], changed):
                continue
            rows.append((_strip_prefix(subject)[:70], n))
    if not rows:
        out.append("| (no unticked customer-visible change on prod) | | |")
    else:
        for what, n in rows[:5]:
            out.append(f"| {what} | {_local(prod_ts)} | #{n} |")
    return "\n".join(out) + "\n"


def cmd_prod(args, root):
    from asf import env
    product = env.load_product(getattr(args, 'product', None))
    print(render(root, product), end='')
    return 0
