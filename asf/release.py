"""asf.release — ``asf release-readiness [--product p] [--json]``: is this product ready to be
released as a framework? A computed gate, not an opinion: eight criteria, each read from facts
and printed met or unmet with its evidence. Nothing is written.

1. **stability** — no hand hotfix in the last ``window_days``: commits on the trunk of a
   ``hand_types`` kind (``fix``, ``hotfix``, ``revert``) that carry no ``ASF-Session:`` trailer
   (so no factory session made them), plus installs by hand: every run of the install script (``install.sh``)
   (``logs/install.log``) and every break in the auto-upgrade chain the tick logs show (an
   upgrade that starts from a commit the previous upgrade did not install — something installed
   it in between, by hand).
2. **repair load** — the factory's repair sessions per landed Feature over the window
   (:mod:`asf.scorecard`) at most ``max_repair_per_feature``.
3. **main CI** — the last ``ci_runs`` finished trunk push runs are all green.
4. **install from zero** — the latest finished trunk run has a green step matching
   ``ci_steps.install_from_zero`` (the release-tag install path), and ``requires.install`` landed.
5. **upgrade safety** — at least ``min_upgrades`` auto-upgrades in the window, none failed, no
   torn tick (an ``ImportError`` after an upgrade) and no rollback; ``requires.upgrade`` landed.
6. **genericity** — the latest finished trunk run has the ``ci_steps.generic`` step and the
   ``ci_steps.second_product`` step green; ``requires.generic`` landed.
7. **first-user docs** — the trunk's README has every ``readme_sections`` heading, the latest
   release tag has a CHANGELOG section with notes; ``requires.docs`` landed. Structural only.
8. **blocking Features** — every id in ``blocking`` has landed in the record.

Every threshold is ``release: {…}`` in the product file (:data:`DEFAULTS`). The criteria read CI
from what the forge records; nothing is installed or run on the host.
"""
import datetime
import glob
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass

DEFAULTS = {
    'window_days': 7,
    'max_hand_fixes': 0,
    'max_repair_per_feature': 3.0,
    'ci_runs': 10,
    'min_upgrades': 3,
    'hand_types': ['fix', 'hotfix', 'revert'],
    'readme_sections': ['Install', 'Quick start', 'Configuration', 'Upgrade'],
    'ci_steps': {'install_from_zero': 'install.sh', 'generic': 'check generic',
                 'second_product': 'sample product'},
    'requires': {},
    'blocking': [],
}
KEYS = ('stability', 'repair', 'ci', 'install', 'upgrade', 'generic', 'docs', 'blocking')

_UPGRADE_RE = re.compile(r'tick: ran asf upgrade \((?:\S*@)?([0-9a-f]{7,40}) → (?:\S*@)?([0-9a-f]{7,40})\), '
                         r'exit (-?\d+)')
_TORN_RE = re.compile(r'^(ImportError|ModuleNotFoundError)\b')
_ROLLBACK_RE = re.compile(r'\brolled back\b|\brollback:', re.I)
UTC = datetime.timezone.utc


@dataclass
class Criterion:
    key: str
    name: str
    met: bool
    evidence: str


# ------------------------------------------------------------ settings --

def settings(product):
    block = getattr(product, 'release', None) or {}
    block = block if isinstance(block, dict) else {}
    out = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
           for k, v in DEFAULTS.items()}
    for k in ('window_days', 'max_hand_fixes', 'max_repair_per_feature', 'ci_runs', 'min_upgrades'):
        v = block.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = v
        elif isinstance(v, str):
            try:
                out[k] = float(v) if '.' in v else int(v)
            except ValueError:
                pass
    for k in ('hand_types', 'readme_sections', 'blocking'):
        v = block.get(k)
        if isinstance(v, list):
            out[k] = [str(x) for x in v]
    if isinstance(block.get('ci_steps'), dict):
        out['ci_steps'].update({k: str(v) for k, v in block['ci_steps'].items() if v})
    if isinstance(block.get('requires'), dict):
        out['requires'] = {k: ([str(x) for x in v] if isinstance(v, list) else [str(v)])
                           for k, v in block['requires'].items() if v}
    return out


# ------------------------------------------------------------ facts --

def _git(repo, *args, run=subprocess.run):
    try:
        p = run(['git', '-C', repo] + list(args), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def trunk_ref(repo, main, git=_git):
    for ref in (f'origin/{main}', main):
        if git(repo, 'rev-parse', '--verify', '-q', f'{ref}^{{commit}}'):
            return ref
    return main


def hand_commits(repo, ref, since, hand_types, git=_git):
    """The trunk's commits since ``since`` (ISO) whose subject's type is one of ``hand_types`` and
    that no factory session made: ``[{sha, date, author, subject}]``, newest first.

    A factory session's commit carries an ``ASF-Session:`` trailer, or (the sessions before that
    trailer) a ``Signed-off-by:`` with no ``Claude-Session:`` — a console session's trailer."""
    tr = '%(trailers:key={},valueonly,separator=%x2C)'
    fmt = ('%H%x1f%cI%x1f%an%x1f%s%x1f' + tr.format('ASF-Session') + '%x1f' + tr.format('Signed-off-by')
           + '%x1f' + tr.format('Claude-Session') + '%x1e')
    text = git(repo, 'log', ref, f'--since={since}', f'--format={fmt}') or ''
    types = tuple(t.lower() for t in hand_types)
    out = []
    for rec in text.split('\x1e'):
        parts = [p.strip() for p in rec.strip('\n').split('\x1f')]
        if len(parts) < 7:
            continue
        sha, date, author, subject, session, signed, console = parts[:7]
        m = re.match(r'^([A-Za-z-]+)[(:!]', subject)
        if not m or m.group(1).lower() not in types:
            continue
        if session or (signed and not console):
            continue
        out.append({'sha': sha[:7], 'date': date, 'author': author, 'subject': subject})
    return out


def commit_dates(repo, shas, git=_git):
    """``{sha: committer date}`` for the shas git knows, in UTC (``…Z``) so it compares as text."""
    out = {}
    for sha in set(shas):
        d = _utc((git(repo, 'show', '-s', '--format=%cI', sha) or '').strip())
        if d:
            out[sha] = d
    return out


def parse_tick_log(text):
    """One tick log → ordered events: ``('upgrade', from, to, rc)``, ``('torn', line)``,
    ``('rollback', line)``."""
    events = []
    for line in (text or '').splitlines():
        m = _UPGRADE_RE.search(line)
        if m:
            events.append(('upgrade', m.group(1), m.group(2), int(m.group(3))))
        elif _TORN_RE.search(line):
            events.append(('torn', line.strip()[:120]))
        elif _ROLLBACK_RE.search(line):
            events.append(('rollback', line.strip()[:120]))
    return events


def read_tick_logs(log_dir):
    out = []
    for path in sorted(glob.glob(os.path.join(log_dir, 'tick-*.log'))):
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                out.append((os.path.basename(path), parse_tick_log(f.read())))
        except OSError:
            continue
    return out


def upgrade_facts(logs, dates, since):
    """Over every tick log's events: the auto-upgrades dated in the window (an upgrade is dated by
    the commit it installed), the failed ones, the torn ticks and rollbacks after the first
    in-window upgrade, and the hand installs — breaks in the upgrade chain, dated by the commit
    the next upgrade started from."""
    ups, failed, torn, rollbacks, breaks = [], [], [], [], []
    chain = []
    for _name, events in logs:
        live = False
        for ev in events:
            if ev[0] == 'upgrade':
                _, frm, to, rc = ev
                chain.append((frm, to, rc))
                when = dates.get(to)
                if when and when >= since:
                    live = True
                    (ups if rc == 0 else failed).append({'from': frm, 'to': to, 'rc': rc, 'date': when})
            elif live and ev[0] == 'torn':
                torn.append(ev[1])
            elif live and ev[0] == 'rollback':
                rollbacks.append(ev[1])
    prev_to = None
    for frm, to, rc in chain:
        if prev_to and not (prev_to.startswith(frm) or frm.startswith(prev_to)):
            when = dates.get(frm)
            if when and when >= since:
                breaks.append({'installed': frm, 'after': prev_to, 'date': when})
        if rc == 0:
            prev_to = to
    return {'upgrades': ups, 'failed': failed, 'torn': torn, 'rollbacks': rollbacks,
            'chain_breaks': breaks}


def install_log(log_dir, since):
    """The install script's runs since ``since``: one ``<iso>\\t<product>\\t<ref>`` line each."""
    out = []
    try:
        with open(os.path.join(log_dir, 'install.log'), encoding='utf-8') as f:
            for line in f:
                parts = line.rstrip('\n').split('\t')
                if parts and parts[0] >= since:
                    out.append({'date': parts[0], 'product': parts[1] if len(parts) > 1 else '',
                                'ref': (parts[2] if len(parts) > 2 else '')[:12]})
    except OSError:
        pass
    return out


def ci_runs(slug, main, n, gh_json):
    """The last ``n`` finished push runs on the trunk, newest first; ``None`` when the forge did
    not answer."""
    runs = gh_json(['run', 'list', '--repo', slug, '--branch', main, '--event', 'push',
                    '--limit', str(max(n * 3, 20)),
                    '--json', 'databaseId,conclusion,status,headSha,createdAt,workflowName'])
    if not isinstance(runs, list):
        return None
    return [r for r in runs if r.get('status') == 'completed'][:n]


def ci_steps(slug, run_id, gh_json):
    """``[(job, step, conclusion)]`` of one run; ``None`` when the forge did not answer."""
    data = gh_json(['run', 'view', str(run_id), '--repo', slug, '--json', 'jobs'])
    if not isinstance(data, dict):
        return None
    return [(j.get('name', ''), s.get('name', ''), s.get('conclusion'))
            for j in data.get('jobs') or [] for s in j.get('steps') or []]


def step_state(steps, pattern):
    """``(found, green)`` for the steps whose name contains ``pattern`` (case-insensitive)."""
    hits = [c for _j, name, c in steps or () if pattern.lower() in name.lower()]
    return bool(hits), bool(hits) and all(c == 'success' for c in hits)


def readme_headings(text):
    return [m.group(1).strip() for m in re.finditer(r'^#{2,3}\s+(.+?)\s*#*\s*$', text or '', re.M)]


def changelog_notes(text, tag):
    """The CHANGELOG section for ``tag`` (``## v0.1.11 — …``) and whether it carries a line."""
    if not tag:
        return False, False
    lines = (text or '').splitlines()
    for i, line in enumerate(lines):
        if re.match(rf'^##\s+{re.escape(tag)}\b', line):
            body = []
            for nxt in lines[i + 1:]:
                if re.match(r'^##\s', nxt):
                    break
                body.append(nxt)
            return True, any(b.strip().startswith(('-', '*')) or (b.strip() and not b.startswith('#'))
                             for b in body)
    return False, False


def landed(items, fid):
    c = items.get(fid)
    return bool(c and c.get('landed')), (c or {}).get('stage') or ('not in the record' if c is None else '?')


# ------------------------------------------------------------ criteria --

def _requires(items, cfg, key):
    """``(all landed, evidence)`` for ``requires.<key>``."""
    ids = cfg['requires'].get(key) or []
    if not ids:
        return True, ''
    parts, ok = [], True
    for fid in ids:
        done, stage = landed(items, fid)
        ok = ok and done
        parts.append(f'{fid} {"landed" if done else stage}')
    return ok, '; ' + ', '.join(parts)


def evaluate(f, cfg):
    """The eight criteria over the gathered facts ``f`` (see :func:`gather`)."""
    items = f['items']
    out = []
    w = cfg['window_days']

    hand = f['hand_commits']
    installs = f['installs'] + f['upgrade']['chain_breaks']
    n = len(hand) + len(installs)
    last = max([c['date'] for c in hand] + [i.get('date') or '' for i in installs] or [''])
    ev = (f"{len(hand)} hand fix commit(s) and {len(installs)} hand install(s) in {w:g} d"
          + (f" (last {last[:16]})" if last else '')
          + (': ' + ', '.join(f"{c['sha']} {c['subject'][:40]}" for c in hand[:3]) if hand else '')
          + (' …' if len(hand) > 3 else '')
          + ('; installs ' + ', '.join(i.get('installed') or i.get('ref') or '?' for i in installs[:3])
             if installs else ''))
    out.append(Criterion('stability', f'Stability (no hand hotfix for {w:g} d)',
                         n <= cfg['max_hand_fixes'], ev))

    rp = f['repair']
    ok = rp['per_feature'] is not None and rp['per_feature'] <= cfg['max_repair_per_feature']
    out.append(Criterion('repair', f"Repair load (≤ {cfg['max_repair_per_feature']:g} per Feature)", ok,
                         f"{rp['sessions']} repair sessions / {rp['landed']} Features landed in {w:g} d = "
                         f"{'—' if rp['per_feature'] is None else format(rp['per_feature'], 'g')}"))

    runs = f['ci_runs']
    if runs is None:
        out.append(Criterion('ci', f"Main CI green (last {cfg['ci_runs']})", False, 'the forge did not answer'))
    else:
        red = [r for r in runs if r.get('conclusion') != 'success']
        ok = len(runs) >= cfg['ci_runs'] and not red
        out.append(Criterion('ci', f"Main CI green (last {cfg['ci_runs']})", ok,
                             f"{len(runs) - len(red)}/{len(runs)} green"
                             + (': red ' + ', '.join(f"{(r.get('headSha') or '')[:7]} {r.get('conclusion')}"
                                                     for r in red[:3]) if red else '')))

    steps = f['ci_steps']
    pat = cfg['ci_steps']['install_from_zero']
    found, green = step_state(steps, pat)
    req_ok, req_ev = _requires(items, cfg, 'install')
    ev = ('the forge did not answer' if steps is None else
          f"CI step '{pat}' " + ('green' if green else 'red' if found else 'absent') + ' on the latest main run')
    out.append(Criterion('install', 'Install from zero (release tag, doctor green, in CI)',
                         bool(green and req_ok), ev + req_ev))

    up = f['upgrade']
    req_ok, req_ev = _requires(items, cfg, 'upgrade')
    ok = (len(up['upgrades']) >= cfg['min_upgrades'] and not up['failed'] and not up['torn']
          and not up['rollbacks'] and req_ok)
    out.append(Criterion('upgrade', f"Upgrade safety (≥ {cfg['min_upgrades']} auto-upgrades, none torn)", ok,
                         f"{len(up['upgrades'])} auto-upgrade(s), {len(up['failed'])} failed, "
                         f"{len(up['torn'])} torn tick(s), {len(up['rollbacks'])} rollback(s) in {w:g} d"
                         + req_ev))

    gp, sp = cfg['ci_steps']['generic'], cfg['ci_steps']['second_product']
    g_found, g_green = step_state(steps, gp)
    s_found, s_green = step_state(steps, sp)
    req_ok, req_ev = _requires(items, cfg, 'generic')
    ev = ('the forge did not answer' if steps is None else
          f"CI '{gp}' {'green' if g_green else 'red' if g_found else 'absent'}, "
          f"'{sp}' {'green' if s_green else 'red' if s_found else 'absent'}")
    out.append(Criterion('generic', 'Genericity (check_generic clean, a second product in CI)',
                         bool(g_green and s_green and req_ok), ev + req_ev))

    d = f['docs']
    missing = [s for s in cfg['readme_sections']
               if not any(s.lower() in h.lower() for h in d['headings'])]
    req_ok, req_ev = _requires(items, cfg, 'docs')
    notes = d['tag'] and d['changelog_section'] and d['changelog_notes']
    ok = not missing and bool(notes) and req_ok
    out.append(Criterion('docs', 'First-user docs and release notes', ok,
                         ('README has every section' if not missing else 'README lacks ' + ', '.join(missing))
                         + '; ' + (f"CHANGELOG has {d['tag']} notes" if notes else
                                   f"no CHANGELOG notes for {d['tag']}" if d['tag'] else 'no release tag')
                         + req_ev))

    ids = cfg['blocking']
    rows = [(fid,) + landed(items, fid) for fid in ids]
    open_ = [r for r in rows if not r[1]]
    out.append(Criterion('blocking', 'Blocking Features landed', bool(ids) and not open_,
                         'no release.blocking list' if not ids else
                         f"{len(ids) - len(open_)}/{len(ids)} landed"
                         + ('; open: ' + ', '.join(f'{r[0]} {r[2]}' for r in open_) if open_ else '')))
    return out


def gather(root, product, cfg, *, now=None, git=_git, gh_json=None, log_dir=None, facts=None):
    """Every fact :func:`evaluate` reads, for ``product`` and its record ``root``."""
    from asf import env
    from asf.scorecard import facts as sfacts, score
    if gh_json is None:
        from asf.metrics.metrics import gh_json
    now = now or datetime.datetime.now(UTC)
    since = (now - datetime.timedelta(days=cfg['window_days'])).strftime('%Y-%m-%dT%H:%M:%SZ')
    repo = product.repo_dir
    ref = trunk_ref(repo, product.main, git=git)
    log_dir = log_dir or env.log_dir()

    items = sfacts.load_cards(root)
    sf = facts or sfacts.load(root, product, registry=False, forge=False,
                              as_of=now.strftime('%Y-%m-%dT%H:%M:%SZ'))
    h = score.headline(sf, days=cfg['window_days'])

    logs = read_tick_logs(log_dir)
    shas = [s for _n, evs in logs for e in evs if e[0] == 'upgrade' for s in e[1:3]]
    up = upgrade_facts(logs, commit_dates(repo, shas, git=git), since)

    slug = product.repo_slug
    runs = ci_runs(slug, product.main, int(cfg['ci_runs']), gh_json) if slug else None
    steps = ci_steps(slug, runs[0]['databaseId'], gh_json) if runs else None

    tag = (git(repo, 'describe', '--tags', '--abbrev=0', ref) or '').strip() or None
    readme = git(repo, 'show', f'{ref}:README.md') or ''
    changelog = git(repo, 'show', f'{ref}:CHANGELOG.md') or ''
    section, notes = changelog_notes(changelog, tag)
    return {
        'as_of': now.strftime('%Y-%m-%dT%H:%M:%SZ'), 'since': since, 'ref': ref, 'items': items,
        'hand_commits': hand_commits(repo, ref, since, cfg['hand_types'], git=git),
        'installs': install_log(log_dir, since),
        'repair': {'sessions': h['repair_sessions'], 'landed': h['landed'],
                   'per_feature': h['repair_per_feature']},
        'upgrade': up, 'ci_runs': runs, 'ci_steps': steps,
        'docs': {'headings': readme_headings(readme), 'tag': tag,
                 'changelog_section': section, 'changelog_notes': notes},
    }


def _utc(stamp):
    try:
        d = datetime.datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
    except ValueError:
        return None
    return d.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def compute(root, product, **kw):
    cfg = settings(product)
    f = gather(root, product, cfg, **kw)
    crit = evaluate(f, cfg)
    return {'product': product.name, 'as_of': f['as_of'], 'window_days': cfg['window_days'],
            'ready': all(c.met for c in crit), 'criteria': [asdict(c) for c in crit]}


# ------------------------------------------------------------ render --

def verdict(d):
    met = sum(1 for c in d['criteria'] if c['met'])
    if d['ready']:
        return f"READY — {met}/{len(d['criteria'])} met"
    unmet = [c['key'] for c in d['criteria'] if not c['met']]
    return f"NOT READY — {met}/{len(d['criteria'])} met; unmet: {', '.join(unmet)}"


def render(d):
    out = [f"**RELEASE READINESS {d['product']}** — {d['as_of']}", '',
           f"Verdict: {verdict(d)}", '',
           '| # | Criterion | Met | Evidence |', '|---|---|---|---|']
    for i, c in enumerate(d['criteria'], 1):
        out.append(f"| {i} | {c['name']} | {'yes' if c['met'] else 'NO'} | "
                   f"{c['evidence'].replace('|', '/')} |")
    return '\n'.join(out) + '\n'


def cell(root, product):
    """The ``Release`` row of ``asf status``: the verdict line."""
    return verdict(compute(root, product))


def cmd_release_readiness(args, root):
    from asf import env
    product = env.load_product(getattr(args, 'product', None))
    d = compute(root, product)
    if getattr(args, 'json', False):
        print(json.dumps(d, indent=1, default=str))
    else:
        print(render(d), end='')
    return 0 if d['ready'] else 1
