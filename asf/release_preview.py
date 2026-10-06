"""asf.release_preview — the ``preview`` release gate: five criteria a product meets before it
ships a pre-release for feedback, where the ``1.0`` gate (:mod:`asf.release`) asks for the
hardening criteria as well. ``release.gate: preview | 1.0`` in the product file picks the gate
``asf release-readiness`` prints (default ``1.0``, so nothing changes silently); ``--gate`` picks
one for a single run.

1. **install** — install from zero on a clean machine to a green doctor, in CI: the newest trunk
   run that has a step matching ``ci_steps.install_linux`` (a Linux container) and the newest
   that has ``ci_steps.install_macos`` (a macOS job with a fresh ``HOME``) both green.
2. **minimal** — a minimal product (one account, no cloud, no queue, hosted CI) ticks end to
   end: a green ``ci_steps.minimal_product`` step.
3. **readme** — the README-only first-user run: exactly the README's Quick start commands, to a
   first landed Task on a stub runtime, a green ``ci_steps.readme_first_user`` step.
4. **privacy** — privacy and genericity: green ``ci_steps.generic`` and ``ci_steps.privacy``
   steps (no operator path, e-mail or private link in a tracked file).
5. **ship** — the newest tested trunk commit (the newest that ran the suite) is green in every
   run of it, a version tag is reachable from the trunk, the trunk carries the release notes
   (``preview.notes``) and a known-issues page with at least one section (``preview.known_issues``), and a feedback
   channel (``feedback``: ``github-issues`` — an issue template on the trunk and a forge — or
   ``url`` with ``feedback.url``) that the README names under a *Feedback* heading.

Each step is read from the newest finished trunk run that ran it (a workflow with a ``paths``
filter does not run on every push), looking back over the last ``preview.lookback_runs`` (50).
Nothing is installed or run on the host; nothing is written. The record is not read.
"""
import datetime
import re

GATES = ('1.0', 'preview')
DEFAULT_GATE = '1.0'
STEPS = {'install_linux': 'install from zero, linux container',
         'install_macos': 'install from zero, macos fresh home',
         'minimal_product': 'a minimal product, end to end',
         'readme_first_user': 'readme-only first-user run',
         'generic': 'check generic',
         'privacy': 'privacy sweep'}
#: ``release.preview.lookback_runs``: how many finished trunk runs a step is looked for in.
LOOKBACK_RUNS = 50
DEFAULTS = {'notes': 'docs/RELEASE-NOTES.md', 'known_issues': 'docs/KNOWN-ISSUES.md'}
#: ``release.preview.defect_severities``: the open Bugs the known-issues page lists.
DEFECT_SEVERITIES = ('S1', 'S2')
FEEDBACK = {'kind': 'github-issues', 'url': '', 'templates': '.github/ISSUE_TEMPLATE'}
FEEDBACK_KINDS = ('github-issues', 'url')
UTC = datetime.timezone.utc
CLOSED_STATES = ('Closed', 'Resolved', 'Rejected', 'Cancelled', 'Done', 'Superseded')


def gate_of(product, override=None):
    """The gate to print: ``override`` (``--gate``), else ``release.gate``, else ``1.0``. A value
    that names no gate raises :class:`ValueError` — never a silent fall back."""
    block = getattr(product, 'release', None)
    configured = block.get('gate') if isinstance(block, dict) else None
    for v in (override, configured):
        if v is None or not str(v).strip():
            continue
        v = str(v).strip().lower()
        if v in ('1', '1.0', '1.0.0'):
            return '1.0'
        if v == 'preview':
            return 'preview'
        raise ValueError(f'release.gate: {v!r} is not one of {", ".join(GATES)}')
    return DEFAULT_GATE


def settings(product):
    block = getattr(product, 'release', None)
    block = block if isinstance(block, dict) else {}
    steps = dict(STEPS)
    if isinstance(block.get('ci_steps'), dict):
        steps.update({k: str(v) for k, v in block['ci_steps'].items() if k in STEPS and v})
    pv = dict(DEFAULTS)
    if isinstance(block.get('preview'), dict):
        pv.update({k: str(v) for k, v in block['preview'].items() if k in DEFAULTS and v})
    fb = dict(FEEDBACK)
    raw = block.get('feedback')
    if isinstance(raw, str) and raw.strip():
        raw = {'kind': raw.strip()}
    if isinstance(raw, dict):
        fb.update({k: str(v) for k, v in raw.items() if k in FEEDBACK and v})
    n = (block.get('preview') or {}).get('lookback_runs') if isinstance(block.get('preview'), dict) else None
    try:
        n = max(1, int(n)) if n is not None else LOOKBACK_RUNS
    except (TypeError, ValueError):
        n = LOOKBACK_RUNS
    return {'ci_steps': steps, 'preview': pv, 'feedback': fb, 'ci_runs': n}


# ------------------------------------------------------------ facts --

def latest_steps(runs, patterns, steps_of):
    """``{key: (found, green, run)}`` — for each pattern, the newest run in ``runs`` (newest
    first) that has a step matching it; ``steps_of(run_id)`` reads a run's steps lazily, and
    ``None`` from it means the forge did not answer (that run is skipped)."""
    from asf.release import step_state
    out = {k: (False, False, None) for k in patterns}
    todo = set(patterns)
    for r in runs or ():
        if not todo:
            break
        steps = steps_of(r.get('databaseId'))
        if steps is None:
            continue
        for key in list(todo):
            found, green = step_state(steps, patterns[key])
            if found:
                out[key] = (True, green, r)
                todo.discard(key)
    return out


def newest_tested(runs, tested=None):
    """The newest tested trunk sha and its finished runs: ``(sha, [run])``. ``tested`` is the
    newest run that ran the test suite (the one the ``generic`` step was read from) — a release
    or changelog commit whose push runs no suite is not *tested*; without it, the newest run."""
    if not runs:
        return None, []
    sha = (tested or runs[0]).get('headSha')
    return sha, [r for r in runs if r.get('headSha') == sha]


def tree_has(git, repo, ref, path):
    return bool((git(repo, 'ls-tree', '--name-only', ref, '--', path) or '').strip())


def gather(product, st, *, now=None, git=None, gh_json=None):
    from asf import release
    git = git or release._git
    if gh_json is None:
        from asf.metrics.metrics import gh_json
    now = now or datetime.datetime.now(UTC)
    repo, slug = product.repo_dir, getattr(product, 'repo_slug', None)
    ref = release.trunk_ref(repo, product.main, git=git)
    runs = release.ci_runs(slug, product.main, st['ci_runs'], gh_json) if slug else None
    cache = {}

    def steps_of(run_id):
        if run_id not in cache:
            cache[run_id] = release.ci_steps(slug, run_id, gh_json)
        return cache[run_id]

    steps = latest_steps(runs or [], st['ci_steps'], steps_of) if runs else None
    tag = (git(repo, 'describe', '--tags', '--abbrev=0', '--exclude', '*-*', ref) or '').strip() or None
    pv, fb = st['preview'], st['feedback']
    readme = git(repo, 'show', f'{ref}:README.md') or ''
    known = git(repo, 'show', f"{ref}:{pv['known_issues']}")
    notes = git(repo, 'show', f"{ref}:{pv['notes']}")
    changelog = git(repo, 'show', f'{ref}:CHANGELOG.md') or ''
    return {
        'as_of': now.strftime('%Y-%m-%dT%H:%M:%SZ'), 'ref': ref, 'slug': slug,
        'runs': runs, 'steps': steps, 'tag': tag,
        'changelog_notes': release.changelog_notes(changelog, tag)[1] if tag else False,
        'notes': notes, 'known_issues': known, 'readme': readme,
        'templates': tree_has(git, repo, ref, fb['templates'].rstrip('/') + '/'),
    }


# ------------------------------------------------------------ criteria --

def _step_ev(steps, key, st):
    pat = st['ci_steps'][key]
    found, green, run = steps.get(key, (False, False, None))
    if not found:
        return False, f"CI step '{pat}' absent in the last {st['ci_runs']} trunk runs"
    sha = ((run or {}).get('headSha') or '')[:7]
    return green, f"CI step '{pat}' {'green' if green else 'red'} at {sha}"


def evaluate(f, st):
    from asf.release import Criterion, readme_headings
    out = []
    steps = f['steps']
    none = 'the forge did not answer' if f['slug'] else 'no forge (repo_slug unset)'

    def from_steps(key, name, keys):
        if steps is None:
            return Criterion(key, name, False, none)
        parts = [_step_ev(steps, k, st) for k in keys]
        return Criterion(key, name, all(ok for ok, _ in parts), '; '.join(ev for _, ev in parts))

    out.append(from_steps('install', 'Install from zero on a clean machine (Linux container, macOS '
                          'fresh HOME), doctor green, in CI', ('install_linux', 'install_macos')))
    out.append(from_steps('minimal', 'A minimal product ticks end to end', ('minimal_product',)))
    out.append(from_steps('readme', 'README-only first-user run to a landed Task (stub runtime)',
                          ('readme_first_user',)))
    out.append(from_steps('privacy', 'Privacy and genericity (check_generic, no operator paths, '
                          'e-mails or private links)', ('generic', 'privacy')))

    parts, ok = [], True
    tested = (steps or {}).get('generic', (False, False, None))[2]
    sha, head_runs = newest_tested(f['runs'] or [], tested)
    if f['runs'] is None:
        ok = False
        parts.append(none)
    elif not head_runs:
        ok = False
        parts.append('no finished trunk run')
    else:
        red = [r for r in head_runs if r.get('conclusion') != 'success']
        ok = ok and not red
        parts.append(f"newest tested {(sha or '')[:7]}: {len(head_runs) - len(red)}/{len(head_runs)} run(s) green"
                     + (' (red: ' + ', '.join(str(r.get('workflowName') or r.get('databaseId'))
                                              for r in red[:3]) + ')' if red else ''))
    ok = ok and bool(f['tag'])
    parts.append(f"tag {f['tag']}" if f['tag'] else 'no version tag')
    pv = st['preview']
    has_notes = bool((f['notes'] or '').strip())
    ok = ok and has_notes
    parts.append(f"release notes {pv['notes']}" + ('' if has_notes else ' missing'))
    sections = re.findall(r'^##\s+\S', f['known_issues'] or '', re.M)
    ok = ok and bool(sections)
    parts.append(f"{pv['known_issues']} " + (f'{len(sections)} section(s)' if sections else
                                             'missing' if f['known_issues'] is None else 'has no section'))
    fb = st['feedback']
    if fb['kind'] == 'github-issues':
        ch = bool(f['templates'] and f['slug'])
        ch_ev = (f"feedback: GitHub Issues of {f['slug']}" if f['slug'] else 'feedback: no forge') + (
            '' if f['templates'] else f", no template under {fb['templates']}")
    elif fb['kind'] == 'url':
        ch = bool(fb['url'])
        ch_ev = f"feedback: {fb['url']}" if fb['url'] else 'feedback: release.feedback.url unset'
    else:
        ch, ch_ev = False, f"feedback: kind {fb['kind']!r} is not one of {', '.join(FEEDBACK_KINDS)}"
    named = any('feedback' in h.lower() for h in readme_headings(f['readme']))
    ok = ok and ch and named
    parts.append(ch_ev + ('' if named else '; README has no Feedback heading'))
    out.append(Criterion('ship', 'Main CI green, a version tag, release notes, KNOWN-ISSUES, a '
                         'feedback channel', ok, '; '.join(parts)))
    return out


def compute(product, **kw):
    from dataclasses import asdict
    st = settings(product)
    f = gather(product, st, **kw)
    crit = evaluate(f, st)
    return {'product': product.name, 'as_of': f['as_of'], 'gate': 'preview',
            'window_days': None, 'ready': all(c.met for c in crit),
            'criteria': [asdict(c) for c in crit]}


# ------------------------------------------------------------ known issues --

#: the line after which ``docs/KNOWN-ISSUES.md`` is kept by hand: a regeneration rewrites the
#: generated sections above it and copies everything from it on unchanged
HAND_MARKER = '<!-- maintained by hand below: kept as is by asf release-readiness --known-issues -->'


def hand_tail(text):
    """The hand-kept part of an existing known-issues page: :data:`HAND_MARKER` and all after it,
    or '' when the page has none."""
    i = (text or '').find(HAND_MARKER)
    return text[i:] if i >= 0 else ''


def known_issues(open_criteria, defects, *, title='Known issues', gate='1.0', tail=''):
    """``docs/KNOWN-ISSUES.md``: one section for the ``gate`` criteria still unmet
    (``[{name, evidence}]``) and one for the known defects (``[(id, title, severity)]``), then the
    hand-kept ``tail`` (from :data:`HAND_MARKER` on) of the page it replaces, unchanged."""
    out = [f'# {title}', '',
           'Generated by `asf release-readiness --known-issues` down to the hand-kept marker; edit '
           'only below it.', '',
           f'## Not yet met for {gate}', '']
    out += ([f"- **{c['name']}** — {c['evidence']}" for c in open_criteria]
            or [f'- none: every {gate} criterion is met.'])
    out += ['', '## Known defects', '']
    out += ([f'- {i} {f"({s}) " if s else ""}— {t}' for i, t, s in defects] or ['- none open.'])
    body = '\n'.join(out) + '\n'
    return body + ('\n' + tail.rstrip('\n') + '\n' if tail else '')


def defect_severities(product):
    block = getattr(product, 'release', None)
    pv = block.get('preview') if isinstance(block, dict) else None
    v = pv.get('defect_severities') if isinstance(pv, dict) else None
    return tuple(str(x).upper() for x in v) if isinstance(v, list) and v else DEFECT_SEVERITIES


def open_defects(root, severities=DEFECT_SEVERITIES):
    """The record's open Bugs of ``severities``: ``[(id, title, severity)]``, most severe first."""
    from asf.record.core import canonicalize, load_items
    by_id, _errors = load_items(root)
    canonical, _dupes = canonicalize(by_id)
    rows = []
    for iid, rec in sorted(canonical.items()):
        fm = rec.get('meta') or {}
        if not iid or str(fm.get('type') or '').lower() != 'bug':
            continue
        if str(fm.get('state') or '') in CLOSED_STATES:
            continue
        if severities and str(fm.get('severity') or '').upper() not in severities:
            continue
        rows.append((iid, str(fm.get('title') or '').strip(), str(fm.get('severity') or '')))
    return sorted(rows, key=lambda r: (r[2] or 'S9', r[0]))
