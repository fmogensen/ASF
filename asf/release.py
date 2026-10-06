"""asf.release — ``asf release-readiness [--product p] [--json]``: is this product ready to be
released as a framework? A computed gate, not an opinion: the criteria, each read from facts
and printed met or unmet with its evidence. Nothing is written.

1. **stability** — ``max_hand_fixes`` (default off: a developer committing by hand is normal; a
   product that wants the rule sets it in its own ``release:`` block) or fewer hand hotfixes in the last ``window_days``: commits on the trunk of a
   ``hand_types`` kind (``fix``, ``hotfix``, ``revert``) that carry no ``ASF-Session:`` trailer
   (so no factory session made them), plus installs by hand: every run of the install script (``install.sh``)
   (``logs/install.log``) and every break in the auto-upgrade chain the tick logs show (an
   upgrade that starts from a commit the previous upgrade did not install — something installed
   it in between, by hand).
2. **repair load** — the factory's repair sessions per landed Feature over the window
   (:mod:`asf.scorecard`) at most ``max_repair_per_feature``.
3. **main CI** — the last ``ci_runs`` finished trunk push runs are all green; a cancelled run
   is a non-verdict and is read past.
4. **install from zero** — the newest finished trunk run that ran the named steps has a green step matching
   ``ci_steps.install_from_zero`` (the release-tag install path), and ``requires.install`` landed.
5. **upgrade safety** — at least ``min_upgrades`` auto-upgrades in the window, none failed, no
   torn tick (an ``ImportError`` after an upgrade) and no rollback; ``requires.upgrade`` landed.
   An upgrade the tick skipped while offline (``asf.tick.network``) counts neither way.
6. **genericity** — the newest finished trunk run that ran the named steps has the ``ci_steps.generic`` step and the
   ``ci_steps.second_product`` step green; ``requires.generic`` landed.
7. **first-user docs** — the trunk's README has every ``readme_sections`` heading, the latest
   release tag has a CHANGELOG section with notes; ``requires.docs`` landed. Structural only.
8. **blocking Features** — every id in ``blocking`` has landed in the record.
9. **floor clean** — no factory leftover older than its deadline (:data:`FLOOR_DEFAULTS`,
   ``release.floor``): a PR the lane marks STALE past ``stale_pr_days``; a CI run still queued or
   running past ``run_min`` whose branch is gone or whose PR is closed; a live cloud run silent
   past ``heartbeat_factor`` × its heartbeat; a Task in one stage past ``stage_factor`` × its
   ``stage_limits``; a remote head past ``branch_retention`` awaiting delete. Each kind reads what
   the factory already keeps (the lane registry, the forge's runs, the session ledger, the record,
   the retention census), is ``off`` by its key, and is ``n/a`` — never red — when the product
   has no such thing (no forge, no cloud lane, no census).
10. **seats used** — no idle-while-launchable stretch in the window: ``release.seats.idle_min``
   minutes or more below ``release.seats.min_pct`` % of the available seats while the wave had
   launchable rows (:mod:`asf.metrics.throughput`, over each tick line's ``seats`` reading). A
   quiet factory never counts against it; no reading in the window is *pending* — not met.

A criterion that does not apply to the product (``n/a``) is met: its evidence says why.

**One gate for every product.** No product is special — the factory's own included: what
applies is read from the product's own configuration. Criteria 4–7 apply only when its
``release:`` block configures them (``requires.<key>``, ``ci_steps.<step>``, ``min_upgrades``,
``readme_sections``), criterion 8 only with a ``blocking`` list, criterion 11 only with
``tune.enabled`` or ``tune.required`` — each is ``n/a`` otherwise. Criterion 3 is ``n/a`` with no
forge or ``ci: none``; criterion 2 is ``n/a`` while nothing landed and nothing was repaired.
Release notes (criterion 7) are checked when the product keeps a changelog: one on its trunk, or
the rollup's opt-in (:func:`asf.metrics.metrics.changelog_on`).

**Self-tuning live** (criterion 11, appended after the others by :func:`compute`, read whole from
:func:`asf.tune.criterion`): the self-tuning loop is on, kept at least one change in the window
that it has not reverted since, and leaves no regression unreverted.

**PR CI healthy** (criterion 12, appended after 11 by :func:`compute`, read from
:func:`asf.metrics.reds.criterion`): the first PR run green at or above
``improve.scorecard.throughput.first_pass_min`` over the last ``first_pass_window`` PRs, and no
unclassified red in the last ``reds_window`` PR runs. ``n/a`` (met) with no forge or no CI;
*pending* (not met) while no PR run is recorded.

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
    # None: off — the stability criterion reads n/a until the product's own release block sets it
    'max_hand_fixes': None,
    'max_repair_per_feature': 3.0,
    'ci_runs': 10,
    'min_upgrades': 3,
    'hand_types': ['fix', 'hotfix', 'revert'],
    'readme_sections': ['Install', 'Quick start', 'Configuration', 'Upgrade'],
    'ci_steps': {'install_from_zero': 'asf install, zero to green', 'generic': 'check generic',
                 'second_product': 'sample product'},
    'requires': {},
    'blocking': [],
}
#: ``release.floor`` — criterion 9's limits; a kind whose key is ``off`` is not checked.
FLOOR_DEFAULTS = {'stale_pr_days': 3, 'run_min': 30, 'heartbeat_factor': 2, 'stage_factor': 3,
                  'branches': True}
FLOOR_KINDS = ('stale_prs', 'runs', 'cloud', 'tasks', 'branches')
NA = 'n/a'
KEYS = ('stability', 'repair', 'ci', 'install', 'upgrade', 'generic', 'docs', 'blocking',
        'floor', 'seats')

_UPGRADE_RE = re.compile(r'tick: ran asf upgrade \((?:\S*@)?([0-9a-f]{7,40}) → (?:\S*@)?([0-9a-f]{7,40})\), '
                         r'exit (-?\d+)')
_SKIPPED_RE = re.compile(r'tick: asf upgrade skipped — offline \((.*)\)\s*$')
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
    """:data:`DEFAULTS` under the product's ``release:`` block; ``configured`` names the opt-in
    criteria the block turns on (see the module docstring)."""
    block = getattr(product, 'release', None) or {}
    block = block if isinstance(block, dict) else {}
    out = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
           for k, v in DEFAULTS.items()}
    for k in ('window_days', 'max_hand_fixes', 'max_repair_per_feature', 'ci_runs', 'min_upgrades'):
        v = block.get(k)
        if k == 'max_hand_fixes' and _off(v):
            out[k] = None
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
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
    out['floor'] = floor_settings(block.get('floor'))
    steps = block.get('ci_steps') if isinstance(block.get('ci_steps'), dict) else {}
    req = out['requires']
    out['configured'] = {
        'install': bool(req.get('install') or steps.get('install_from_zero')),
        'upgrade': bool(req.get('upgrade') or block.get('min_upgrades') is not None),
        'generic': bool(req.get('generic') or steps.get('generic') or steps.get('second_product')),
        'docs': bool(req.get('docs') or isinstance(block.get('readme_sections'), list)),
        'blocking': bool(out['blocking']),
    }
    out['ci_off'] = _ci_off(product)
    out['notes'] = _changelog_on(product)
    from asf.metrics import throughput
    out['seats'] = throughput.settings(product)[0]
    return out


def _off(v):
    return v is False or (isinstance(v, str) and v.strip().lower() in ('off', 'false', 'no', 'none'))


def _ci_off(product):
    """No CI to read: no hosted repo, or ``ci: none`` / ``ci.provider: none``."""
    if not getattr(product, 'repo_slug', None):
        return True
    ci = getattr(product, 'ci', None)
    name = ci if isinstance(ci, str) else ci.get('provider') if isinstance(ci, dict) else None
    return isinstance(name, str) and name.strip().lower() in ('none', 'off')


def _changelog_on(product):
    try:
        from asf.metrics.metrics import changelog_on
        return changelog_on(product)
    except Exception:  # noqa: BLE001 — a product the reader cannot read writes no changelog
        return False


def applies(cfg, key):
    """Does the opt-in criterion ``key`` apply: did the product configure it?"""
    return bool((cfg.get('configured') or {}).get(key))


def _na(key, name, why):
    return Criterion(key, name, True, f'{NA} — {why}')


def floor_settings(block):
    """:data:`FLOOR_DEFAULTS` under ``release.floor``; ``off`` → ``None`` (the kind is not checked)."""
    out = dict(FLOOR_DEFAULTS)
    for k, v in (block.items() if isinstance(block, dict) else ()):
        if k not in out:
            continue
        if _off(v):
            out[k] = None
        elif k == 'branches':
            out[k] = True
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = v
        elif isinstance(v, str):
            try:
                out[k] = float(v)
            except ValueError:
                pass
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
    """One tick log → ordered events: ``('upgrade', from, to, rc)``, ``('upgrade-skipped', reason)``,
    ``('torn', line)``, ``('rollback', line)``."""
    events = []
    for line in (text or '').splitlines():
        m = _UPGRADE_RE.search(line)
        s = None if m else _SKIPPED_RE.search(line)
        if m:
            events.append(('upgrade', m.group(1), m.group(2), int(m.group(3))))
        elif s:
            events.append(('upgrade-skipped', s.group(1)))
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
    the next upgrade started from. An upgrade the tick skipped because the host was offline is
    ``skipped`` — neither an upgrade nor a failure."""
    ups, failed, torn, rollbacks, breaks, skipped = [], [], [], [], [], []
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
            elif ev[0] == 'upgrade-skipped':
                skipped.append(ev[1])
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
            'chain_breaks': breaks, 'skipped': skipped}


def install_log(log_dir, since):
    """The install script's runs since ``since``: one ``<iso>\\t<product>\\t<ref>`` line each.
    A row whose ref is ``uninstall`` (``asf uninstall``'s own log line, T-0407 PD8) is skipped —
    it is a teardown, not a hand install, and counting it would make a torn-down product read as
    evidence against the machine's stability."""
    out = []
    try:
        with open(os.path.join(log_dir, 'install.log'), encoding='utf-8') as f:
            for line in f:
                parts = line.rstrip('\n').split('\t')
                ref = parts[2] if len(parts) > 2 else ''
                if parts and parts[0] >= since and ref != 'uninstall':
                    out.append({'date': parts[0], 'product': parts[1] if len(parts) > 1 else '',
                                'ref': ref[:12]})
    except OSError:
        pass
    return out


#: the run conclusions that judged no code — a cancel, a skip, a run the host let go stale: never
#: red, never green; criterion 3 reads past them to the next run that reached a verdict
NONVERDICT = ('cancelled', 'skipped', 'stale', 'neutral')
#: how many of the newest verdict runs criteria 4 and 6 look through for the one that ran the
#: named steps (the trunk's newest commit can be one that runs only a light workflow)
STEP_RUNS = 5


def ci_runs(slug, main, n, gh_json):
    """The last ``n`` finished push runs on the trunk that reached a verdict, newest first — a
    cancelled (or skipped) run is a non-verdict and is read past (:data:`NONVERDICT`); ``None``
    when the forge did not answer."""
    runs = gh_json(['run', 'list', '--repo', slug, '--branch', main, '--event', 'push',
                    '--limit', str(max(n * 3, 20)),
                    '--json', 'databaseId,conclusion,status,headSha,createdAt,workflowName'])
    if not isinstance(runs, list):
        return None
    return [r for r in runs if r.get('status') == 'completed'
            and r.get('conclusion') not in NONVERDICT][:n]


def step_run_steps(slug, runs, patterns, gh_json, limit=STEP_RUNS):
    """The steps of the newest trunk run among ``runs`` (newest first) that ran any step matching
    ``patterns`` — the newest commit on the trunk may be one (a release or changelog commit) whose
    push runs no full CI, and *absent there* says nothing about the trunk. Falls back to the newest
    run's steps when none of the first ``limit`` ran one; ``None`` when the forge did not answer."""
    first = None
    for r in (runs or [])[:limit]:
        steps = ci_steps(slug, r['databaseId'], gh_json)
        if steps is None:
            continue
        if first is None:
            first = steps
        if any(step_state(steps, p)[0] for p in patterns if p):
            return steps
    return first


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
    name = f'Stability (no hand hotfix for {w:g} d)'
    if cfg['max_hand_fixes'] is None:
        out.append(_na('stability', name, 'release.max_hand_fixes is off (a hand commit is normal '
                                          'for this product)'))
    else:
        out.append(Criterion('stability', name, n <= cfg['max_hand_fixes'], ev))

    rp = f['repair']
    name = f"Repair load (≤ {cfg['max_repair_per_feature']:g} per Feature)"
    kinds = rp.get('by_kind') or {}
    if not rp['sessions'] and not rp['landed']:
        out.append(_na('repair', name, f'nothing landed and nothing repaired in {w:g} d'))
    else:
        ok = rp['per_feature'] is not None and rp['per_feature'] <= cfg['max_repair_per_feature']
        out.append(Criterion('repair', name, ok,
                             f"{rp['sessions']} repair sessions / {rp['landed']} Features landed in {w:g} d = "
                             f"{'—' if rp['per_feature'] is None else format(rp['per_feature'], 'g')}"
                             + (' (' + ', '.join(f'{k} {n}' for k, n in kinds.items()) + ')' if kinds else '')))

    runs = f['ci_runs']
    name = f"Main CI green (last {cfg['ci_runs']})"
    if cfg.get('ci_off') and not runs:
        out.append(_na('ci', name, 'no hosted CI (no repo_slug, or ci: none)'))
    elif runs is None:
        out.append(Criterion('ci', name, False, 'the forge did not answer'))
    else:
        red = [r for r in runs if r.get('conclusion') != 'success']
        ok = len(runs) >= cfg['ci_runs'] and not red
        out.append(Criterion('ci', name, ok,
                             f"{len(runs) - len(red)}/{len(runs)} green"
                             + (': red ' + ', '.join(f"{(r.get('headSha') or '')[:7]} {r.get('conclusion')}"
                                                     for r in red[:3]) if red else '')))

    steps = f['ci_steps']
    name = 'Install from zero (release tag, doctor green, in CI)'
    if not applies(cfg, 'install'):
        out.append(_na('install', name, 'not configured (release.requires.install, '
                                        'release.ci_steps.install_from_zero)'))
    else:
        pat = cfg['ci_steps']['install_from_zero']
        found, green = step_state(steps, pat)
        req_ok, req_ev = _requires(items, cfg, 'install')
        ev = ('the forge did not answer' if steps is None else
              f"CI step '{pat}' " + ('green' if green else 'red' if found else 'absent') + ' on the latest main test run')
        out.append(Criterion('install', name, bool(green and req_ok), ev + req_ev))

    up = f['upgrade']
    name = f"Upgrade safety (≥ {cfg['min_upgrades']} auto-upgrades, none torn)"
    if not applies(cfg, 'upgrade'):
        out.append(_na('upgrade', name, 'not configured (release.min_upgrades, release.requires.upgrade)'))
    else:
        req_ok, req_ev = _requires(items, cfg, 'upgrade')
        ok = (len(up['upgrades']) >= cfg['min_upgrades'] and not up['failed'] and not up['torn']
              and not up['rollbacks'] and req_ok)
        out.append(Criterion('upgrade', name, ok,
                             f"{len(up['upgrades'])} auto-upgrade(s), {len(up['failed'])} failed, "
                             + (f"{len(up.get('skipped') or ())} skipped offline, " if up.get('skipped') else '')
                             + f"{len(up['torn'])} torn tick(s), {len(up['rollbacks'])} rollback(s) in {w:g} d"
                             + req_ev))

    name = 'Genericity (check_generic clean, a second product in CI)'
    if not applies(cfg, 'generic'):
        out.append(_na('generic', name, 'not configured (release.requires.generic, release.ci_steps.generic)'))
    else:
        gp, sp = cfg['ci_steps']['generic'], cfg['ci_steps']['second_product']
        g_found, g_green = step_state(steps, gp)
        s_found, s_green = step_state(steps, sp)
        req_ok, req_ev = _requires(items, cfg, 'generic')
        ev = ('the forge did not answer' if steps is None else
              f"CI '{gp}' {'green' if g_green else 'red' if g_found else 'absent'}, "
              f"'{sp}' {'green' if s_green else 'red' if s_found else 'absent'}")
        out.append(Criterion('generic', name, bool(g_green and s_green and req_ok), ev + req_ev))

    d = f['docs']
    name = 'First-user docs and release notes'
    if not applies(cfg, 'docs'):
        out.append(_na('docs', name, 'not configured (release.readme_sections, release.requires.docs)'))
    else:
        missing = [s for s in cfg['readme_sections']
                   if not any(s.lower() in h.lower() for h in d['headings'])]
        req_ok, req_ev = _requires(items, cfg, 'docs')
        want_notes = cfg.get('notes') or d.get('changelog_kept', False)
        notes = d['tag'] and d['changelog_section'] and d['changelog_notes']
        ok = not missing and (bool(notes) or not want_notes) and req_ok
        out.append(Criterion('docs', name, ok,
                             ('README has every section' if not missing else 'README lacks ' + ', '.join(missing))
                             + '; ' + (f'release notes {NA} (no changelog for this product)' if not want_notes else
                                       f"CHANGELOG has {d['tag']} notes" if notes else
                                       f"no CHANGELOG notes for {d['tag']}" if d['tag'] else 'no release tag')
                             + req_ev))

    ids = cfg['blocking']
    rows = [(fid,) + landed(items, fid) for fid in ids]
    open_ = [r for r in rows if not r[1]]
    name = 'Blocking Features landed'
    if not applies(cfg, 'blocking'):
        out.append(_na('blocking', name, 'no release.blocking list'))
    else:
        out.append(Criterion('blocking', name, bool(ids) and not open_,
                             'no release.blocking list' if not ids else
                             f"{len(ids) - len(open_)}/{len(ids)} landed"
                             + ('; open: ' + ', '.join(f'{r[0]} {r[2]}' for r in open_) if open_ else '')))
    out.append(floor_criterion(f.get('floor') or {}, cfg))
    out.append(seats_criterion(f.get('seats'), f.get('since'), cfg))
    return out


# ------------------------------------------------------------ 9. floor clean --

def _age(minutes):
    m = int(minutes or 0)
    return f'{m // 1440} d' if m >= 1440 else f'{m // 60} h' if m >= 60 else f'{m} min'


def floor_over(found, cfg):
    """``{kind: None (n/a or off) | [(name, age_min)] past its limit, oldest first}`` over the
    gathered leftovers ``found`` (see :func:`floor_facts`) and ``cfg['floor']``."""
    lim = cfg.get('floor') or FLOOR_DEFAULTS
    out = {}
    limits = {'stale_prs': None if lim['stale_pr_days'] is None else lim['stale_pr_days'] * 1440,
              'runs': lim['run_min']}
    for kind in FLOOR_KINDS:
        got = found.get(kind)
        key = {'stale_prs': 'stale_pr_days', 'runs': 'run_min', 'cloud': 'heartbeat_factor',
               'tasks': 'stage_factor', 'branches': 'branches'}[kind]
        if got is None or lim[key] is None:
            out[kind] = None
            continue
        if kind in limits:
            over = [(n, a) for n, a in got if a > limits[kind]]
        elif kind == 'cloud':
            over = [(n, a) for n, a, beat in got if a > lim['heartbeat_factor'] * beat]
        elif kind == 'tasks':
            over = [(n, a) for n, a, limit in got if a > lim['stage_factor'] * limit]
        else:
            over = [(n, a) for n, a in got]
        out[kind] = sorted(over, key=lambda x: -(x[1] or 0))
    return out


FLOOR_WORDS = {'stale_prs': 'stale PR(s)', 'runs': 'orphan CI run(s)', 'cloud': 'silent cloud run(s)',
               'tasks': 'Task(s) stuck in a stage', 'branches': 'expired head(s)'}


def floor_criterion(found, cfg):
    over = floor_over(found, cfg)
    parts, red = [], 0
    for kind in FLOOR_KINDS:
        v = over[kind]
        if v is None:
            parts.append(f'{FLOOR_WORDS[kind]} {NA}')
            continue
        red += len(v)
        parts.append(f'{len(v)} {FLOOR_WORDS[kind]}'
                     + (f' (oldest {v[0][0]} {_age(v[0][1])})' if v and v[0][1] is not None else ''))
    return Criterion('floor', 'Floor clean (no leftover past its deadline)', red == 0, '; '.join(parts))


def floor_facts(root, product, cfg, now, git=_git, gh_json=None):
    """``{kind: None | [...]}`` — what is left on the floor now, aged in minutes. ``None`` when
    the product has no such thing or it could not be read (n/a, never red). Each kind is read from
    what the factory already keeps; a reader that fails is that kind's ``None``."""
    lim = cfg.get('floor') or FLOOR_DEFAULTS
    out = {k: None for k in FLOOR_KINDS}
    forge = bool(getattr(product, 'repo_slug', None))
    readers = {'stale_prs': lambda: _stale_prs(product, now) if forge else None,
               'runs': lambda: _orphan_runs(product, now, git, gh_json) if forge else None,
               'cloud': lambda: _silent_cloud(product, now),
               'tasks': lambda: _stuck_tasks(root, product, now),
               'branches': lambda: _expired_heads(product)}
    for kind, read in readers.items():
        key = {'stale_prs': 'stale_pr_days', 'runs': 'run_min', 'cloud': 'heartbeat_factor',
               'tasks': 'stage_factor', 'branches': 'branches'}[kind]
        if lim[key] is None:
            continue
        try:
            out[kind] = read()
        except Exception:  # noqa: BLE001 — an unreadable kind is n/a, not a red gate
            out[kind] = None
    return out


def _minutes_since(stamp, now):
    d = _parse(stamp)
    return None if d is None else max(0.0, (now - d).total_seconds() / 60)


def _parse(stamp):
    try:
        d = datetime.datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return (d if d.tzinfo else d.replace(tzinfo=UTC)).astimezone(UTC)


def _stale_prs(product, now):
    from asf.harvest import pr_hygiene
    return [(f"PR #{r['pr']}" if r.get('pr') else r['branch'], _minutes_since(r.get('since'), now) or 0)
            for r in pr_hygiene.rows(product) if r['kind'] == pr_hygiene.STALE_CLOSE]


def _orphan_runs(product, now, git, gh_json):
    """Runs still queued or in progress whose branch is gone, or whose PR is closed."""
    if gh_json is None:
        from asf.metrics.metrics import gh_json
    slug = product.repo_slug
    live = []
    for status in ('in_progress', 'queued'):
        got = gh_json(['run', 'list', '--repo', slug, '--status', status, '--limit', '100',
                       '--json', 'databaseId,headBranch,createdAt,event'])
        if not isinstance(got, list):
            return None
        live += got
    if not live:
        return []
    heads_text = git(product.repo_dir, 'ls-remote', '--heads', 'origin')
    if heads_text is None:
        return None
    heads = {line.split('refs/heads/', 1)[1].strip() for line in heads_text.splitlines()
             if 'refs/heads/' in line}
    prs = gh_json(['pr', 'list', '--repo', slug, '--state', 'open', '--limit', '200',
                   '--json', 'headRefName'])
    if not isinstance(prs, list):
        return None
    open_heads = {p.get('headRefName') for p in prs}
    out = []
    for r in live:
        branch = r.get('headBranch') or ''
        gone = branch not in heads
        closed = r.get('event') == 'pull_request' and branch not in open_heads
        if gone or closed:
            out.append((f"run {r.get('databaseId')} ({branch}, {'ref gone' if gone else 'PR closed'})",
                        _minutes_since(r.get('createdAt'), now) or 0))
    return out


def _silent_cloud(product, now):
    """``[(job, silent minutes, heartbeat minutes)]`` of the live cloud runs; ``None`` with the
    cloud lane off."""
    from asf import env
    from asf.workers import cloud as cloud_mod, continuation, lifecycle, pool as pool_mod
    try:
        cfg = env.load_config()
    except env.ConfigError:
        cfg = {}
    lane = cloud_mod.settings(cfg, product)
    if not getattr(lane, 'on', False):
        return None
    beat = getattr(lane, 'heartbeat_min', None) or continuation.heartbeat_min(product)
    out = []
    for job, runs in lifecycle.runs(pool_mod.sessions_path(product)).items():
        run = runs[-1] if runs else None
        if not run or not lifecycle.is_live(run) or not cloud_mod.is_cloud(run):
            continue
        at = run.get('heartbeat_at')
        if at:
            silent = _minutes_since(at, now)
        else:
            try:
                silent = (now.timestamp() - os.path.getmtime(run.get('log'))) / 60
            except (OSError, TypeError):
                continue
        if silent is not None:
            out.append((job, silent, float(beat)))
    return out


def _stuck_tasks(root, product, now):
    """``[(id, minutes in its stage, its limit in minutes)]`` of the Tasks over their stage limit."""
    from asf.record.core import canonicalize, load_items
    from asf.tick import stale
    by_id, _errors = load_items(root)
    canonical, _dupes = canonicalize(by_id)
    return [(iid, age / 60, stale.limit_seconds(limit) / 60)
            for iid, _label, age, key, limit in stale.find_stale(canonical, stale.load_limits(product), now)
            if key == 'task_active']


def _expired_heads(product):
    """The retention census's heads past their retention, awaiting delete; ``None`` with no census."""
    from asf import env
    from asf.workers import retention
    path = os.path.join(env.state_dir(product), retention.STATE_FILE)
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    due = data.get('due')
    n = len(due) if isinstance(due, list) else int(due or 0)
    return [(f"census {str(data.get('at') or '')[:16]}", None)] * n


# ------------------------------------------------------------ 10. seats used --

def seats_criterion(ticks, since, cfg):
    from asf.metrics import throughput
    sc = cfg.get('seats') or throughput.SEAT_DEFAULTS
    name = (f"Seats used (no {sc['idle_min']:g} min below {sc['min_pct']:g} % "
            'while launchable)')
    points = throughput.seat_points(ticks or ())
    start = _parse(since)
    if start is not None:
        points = [p for p in points if p[0] >= start]
    if not points:   # no data is not yet, never met: only a product the rule cannot apply to is n/a
        return Criterion('seats', name, False,
                         'pending — no tick carries a seat reading in the window yet')
    stretches = throughput.idle_stretches(points, sc)
    if not stretches:
        return Criterion('seats', name, True, f'0 idle stretches over {len(points)} tick(s)')
    longest = max(stretches, key=lambda x: x['minutes'])
    causes = [x['cause'] for x in stretches if x['cause']]
    top = max(set(causes), key=causes.count) if causes else ''
    return Criterion('seats', name, False,
                     f"{len(stretches)} idle stretch(es); longest {longest['start'][:16]} "
                     f"{longest['minutes']:g} min at {longest['busy']:g}/{longest['available']:g}"
                     + (f'; top cause: {top}' if top else ''))


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
    steps = step_run_steps(slug, runs, list(cfg['ci_steps'].values()), gh_json) if runs else None

    tag = (git(repo, 'describe', '--tags', '--abbrev=0', '--exclude', '*-*', ref) or '').strip() or None
    readme = git(repo, 'show', f'{ref}:README.md') or ''
    conv = getattr(product, 'conventions', None)
    from asf.conventions import DEFAULT_CHANGELOG_FILE
    log_file = (conv.get('changelog_file') if conv is not None else None) or DEFAULT_CHANGELOG_FILE
    changelog = git(repo, 'show', f'{ref}:{log_file}') or ''
    kept = bool(changelog.strip())
    section, notes = changelog_notes(changelog, tag)
    return {
        'as_of': now.strftime('%Y-%m-%dT%H:%M:%SZ'), 'since': since, 'ref': ref, 'items': items,
        'hand_commits': hand_commits(repo, ref, since, cfg['hand_types'], git=git),
        'installs': install_log(log_dir, since),
        'repair': {'sessions': h['repair_sessions'], 'landed': h['landed'],
                   'per_feature': h['repair_per_feature'], 'by_kind': h.get('repair_by_kind') or {}},
        'floor': floor_facts(root, product, cfg, now, git=git, gh_json=gh_json),
        'seats': _ticks(root),
        'pr_ci': getattr(sf, 'ci', None) or [],
        'upgrade': up, 'ci_runs': runs, 'ci_steps': steps,
        'docs': {'headings': readme_headings(readme), 'tag': tag,
                 'changelog_section': section, 'changelog_notes': notes, 'changelog_kept': kept},
    }


def _ticks(root):
    from asf.metrics.metrics import read_stream
    try:
        return read_stream(root, 'ticks')
    except (OSError, ValueError):
        return []


def _utc(stamp):
    try:
        d = datetime.datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
    except ValueError:
        return None
    return d.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def tune_criterion(product, window_days, as_of):
    """Criterion 11, *Self-tuning live* (:func:`asf.tune.criterion`); ``n/a`` while the loop is
    off and ``tune.required`` is not set (:func:`asf.tune.required`)."""
    from asf import tune
    try:
        met, ev = tune.criterion(product, window_days, now=datetime.datetime.strptime(
            as_of, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=UTC))
    except Exception as e:  # noqa: BLE001 — an unreadable tune record is unmet, never a crash
        met, ev = False, f'unreadable: {type(e).__name__}: {e}'
    if not met and ev == 'tune.enabled is off' and not tune.required(product):
        met, ev = True, f'{NA} — tune.enabled is off'
    return Criterion('tune', 'Self-tuning live (≥ 1 kept change, 0 unreverted regressions)', met, ev)


def pr_ci_criterion(product, ci, as_of, claims=None):
    """Criterion 12, *PR CI healthy* (:func:`asf.metrics.reds.criterion`)."""
    from asf.metrics import reds, throughput
    _seats, tcfg = throughput.settings(product)
    forge = bool(getattr(product, 'repo_slug', None)) and getattr(product, 'ci', None) != 'none'
    d = reds.compute({'ci': ci or [], 'forge': forge, 'main': getattr(product, 'main', None),
                      'claims': reds.load_claims(product) if claims is None else claims,
                      'base_time': None if claims is not None else reds.git_base_time(
                          getattr(product, 'repo_dir', None), getattr(product, 'main', None))},
                     as_of, tcfg)
    met, ev = reds.criterion(d, tcfg)
    lim = tcfg.get('first_pass_min')
    return Criterion('pr_ci', 'PR CI healthy (first pass'
                     + (f" ≥ {lim:g} over {tcfg.get('first_pass_window'):g} PRs" if lim is not None else '')
                     + ', 0 unclassified reds)', met, ev)


def compute(root, product, gate=None, **kw):
    """The gate's verdict: ``gate`` (``--gate``), else ``release.gate``, else ``1.0`` — every
    criterion above; ``preview`` is :mod:`asf.release_preview`'s five."""
    from asf import release_preview
    if release_preview.gate_of(product, gate) == 'preview':
        return release_preview.compute(product, **{k: v for k, v in kw.items()
                                                   if k in ('now', 'git', 'gh_json')})
    cfg = settings(product)
    f = gather(root, product, cfg, **kw)
    crit = evaluate(f, cfg)
    crit.append(tune_criterion(product, cfg['window_days'], f['as_of']))
    crit.append(pr_ci_criterion(product, f.get('pr_ci'), f['as_of']))
    return {'product': product.name, 'as_of': f['as_of'], 'gate': '1.0', 'window_days': cfg['window_days'],
            'ready': all(c.met for c in crit), 'criteria': [asdict(c) for c in crit]}


# ------------------------------------------------------------ render --

def verdict(d):
    met = sum(1 for c in d['criteria'] if c['met'])
    if d['ready']:
        return f"READY — {met}/{len(d['criteria'])} met"
    unmet = [c['key'] for c in d['criteria'] if not c['met']]
    return f"NOT READY — {met}/{len(d['criteria'])} met; unmet: {', '.join(unmet)}"


def render(d):
    gate = f" (gate {d['gate']})" if d.get('gate') else ''
    out = [f"**RELEASE READINESS {d['product']}**{gate} — {d['as_of']}", '',
           f"Verdict: {verdict(d)}", '',
           '| # | Criterion | Met | Evidence |', '|---|---|---|---|']
    for i, c in enumerate(d['criteria'], 1):
        out.append(f"| {i} | {c['name']} | {'yes' if c['met'] else 'NO'} | "
                   f"{c['evidence'].replace('|', '/')} |")
    return '\n'.join(out) + '\n'


def doctor_rows(product, root=None, now=None, git=_git, gh_json=None):
    """``asf doctor``'s mirror of criteria 9 and 10: ``[(row, ok, evidence)]`` — the record clone
    (or the operator's checkout) is the root; with neither, the Task kind and the seats read n/a."""
    if root is None:
        from asf import dwell
        root = dwell._record_root(product)
    now = now or datetime.datetime.now(UTC)
    cfg = settings(product)
    since = (now - datetime.timedelta(days=cfg['window_days'])).strftime('%Y-%m-%dT%H:%M:%SZ')
    if gh_json is None:
        from asf.metrics.metrics import gh_json
    found = floor_facts(root, product, cfg, now, git=git, gh_json=gh_json)
    if not root:
        found['tasks'] = None
    out = []
    for c in (floor_criterion(found, cfg), seats_criterion(_ticks(root) if root else [], since, cfg)):
        out.append((f'release {c.key}', c.met, c.evidence))
    return out


def cell(root, product):
    """The ``Release`` row of ``asf status``: the verdict line."""
    return verdict(compute(root, product))


def cmd_release_readiness(args, root):
    from asf import env
    product = env.load_product(getattr(args, 'product', None))
    if getattr(args, 'known_issues', False) is True:
        from asf import release_preview
        d = compute(root, product, gate='1.0')
        print(release_preview.known_issues([c for c in d['criteria'] if not c['met']],
                                           release_preview.open_defects(
                                               root, release_preview.defect_severities(product))), end='')
        return 0
    try:
        gate = getattr(args, 'gate', None)
        d = compute(root, product, gate=gate if isinstance(gate, str) else None)
    except ValueError as e:
        print(f'release-readiness: {e}')
        return 2
    if getattr(args, 'json', False):
        print(json.dumps(d, indent=1, default=str))
    else:
        print(render(d), end='')
    return 0 if d['ready'] else 1
