"""asf.channels — ASF's own release channels: ``edge`` takes every green tag, ``stable`` is
promoted weekly behind a rehearsal, 48 h on edge and no S1 (F-0308).

A channel is one head on ASF's own remote — ``refs/heads/releases/<name>`` — pointing at the
**commit** of the release tag the channel offers. One ``git ls-remote`` reads it, which is what
makes a channel usable by the bootstrap, before ``asf``, ``gh`` or a token exists. The prefix is
not decoration: ``releases/`` is the one namespace retention never deletes
(:data:`asf.workers.retention.RELEASE_PREFIXES`) *and* :mod:`asf.refguard` does not protect
(``release/*`` is protected — the plural is deliberate).

Nothing here cuts a tag: :func:`asf.version.cut` does that, one patch version per merge, and a
channel only chooses among the tags that exist and delays when it chooses.

* **edge** — the newest tag whose :func:`asf.upgrade.ci_verdict` is ``green``. No ceremony, no
  wait, and never a tag whose CI is red or unread (``clear`` is not green —
  :func:`asf.upgrade.ci_state` answers it for a sha with no runs at all).
* **stable** — :func:`stable_rows`: the weekly cadence, 48 h on edge, the rehearsal check and no
  S1, each met or unmet with its evidence; promoted only when all four are met.

This module holds the resolver, the channel log, the edge half and the stable gate — the
constants, the ordering key, the pure line parser every consumer shares, the one ``git
ls-remote`` that feeds it, ``settings`` (the overlay for the settings keys the criteria read
their thresholds from), the log's read/write/note helpers, :func:`edge_candidate` and
:func:`publish`, and :func:`stable_rows`/:func:`s1_in_window`. The daily part is a later Task of
the same plan (``docs/plans/f-0308.md``).
"""
import datetime
import re
import subprocess

from asf import cli
from asf.state import store

UTC = datetime.timezone.utc

#: the two channels, in the order a report prints them
NAMES = ('edge', 'stable')
#: the head namespace: retention-exempt and unprotected (see the module docstring)
PREFIX = 'releases/'
REF = 'refs/heads/' + PREFIX + '{name}'
#: the suffix ``git ls-remote`` puts on the line naming the commit an annotated tag points at.
#: A listing given ref patterns prints it only for a pattern that matches it, and ``--refs``
#: hides it altogether — so it is asked for by pattern and read by name, never assumed (F-0303).
PEEL = '^{}'
#: ``release.channels``: every threshold, with its default
DEFAULTS = {'stable_every_days': 7, 'edge_dwell_h': 48, 'rehearsal_check': 'rehearsal',
            'lookback_tags': 10, 'log_keep': 60}
#: ``state/<product>/channels.json`` (:mod:`asf.state.registry`) — registered with no ``ttl_days``:
#: the 48 h dwell is read from it, so the reaper must never age it out.
LOG_NAME = 'channels.json'


def version_key(tag):
    """The integer tuple of ``tag``'s digits — the ordering :func:`asf.install.newest_tag` and
    :func:`asf.cli.latest_release` already use, lifted here so the new readers share one
    spelling rather than a fourth copy (PD3 — the two existing Python readers are not rewritten
    to call it)."""
    return tuple(int(g) for g in re.findall(r'\d+', tag))


def channel_tag(lines, name):
    """``(tag, commit)`` the channel ``name`` offers, read from the lines of
    ``git ls-remote <url> 'refs/heads/releases/*' 'refs/tags/v*'``: the channel head's commit, and
    the highest version tag whose peel is that commit. ``(None, None)`` when the head is not
    published; ``(None, <commit>)`` when it is published at a commit no version tag names (a hand
    push, or a tag deleted since). Pure: no network, no clock, no config."""
    heads, at = {}, {}
    for line in lines:
        sha, _tab, ref = str(line).strip().partition('\t')
        sha, ref = sha.strip(), ref.strip()
        if not sha or not ref:
            continue
        if ref.startswith('refs/heads/' + PREFIX):
            heads[ref[len('refs/heads/' + PREFIX):]] = sha
        elif ref.startswith('refs/tags/') and ref.endswith(PEEL):
            tag = ref[len('refs/tags/'):-len(PEEL)]
            if cli.RELEASE_TAG.fullmatch(tag):
                at.setdefault(sha, []).append(tag)
    commit = heads.get(name)
    tags = sorted(at.get(commit, []), key=version_key)
    return (tags[-1] if tags else None), commit


def _out(run, cmd, timeout=60):
    """stdout of ``cmd``, or ``None`` when it fails or cannot start."""
    try:
        p = run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 and isinstance(p.stdout, str) else None


def resolve(url, name, run=subprocess.run):
    """``(tag, commit)`` of the channel ``name`` on ``url`` — one ``git ls-remote`` (D3).
    ``(None, None)`` when the channel is unpublished or the listing could not be read."""
    heads = 'refs/heads/' + PREFIX + '*'
    text = _out(run, ['git', 'ls-remote', url, heads, 'refs/tags/v*'])  # client-exempt: a url, no local
    # repo — asf.gitops's `cwd`-shaped client doesn't fit, exactly as upgrade.latest_release's
    # sibling call is already counted raw
    return channel_tag((text or '').splitlines(), name) if text is not None else (None, None)


def settings(product):
    """:data:`DEFAULTS` under the product's ``release.channels`` map (``release.channels``,
    documented and unchecked — PD7, D11). A key the map does not name keeps its default, and a
    key the map names that this module does not define is ignored."""
    release = getattr(product, 'release', None)
    block = release.get('channels') if isinstance(release, dict) else None
    block = block if isinstance(block, dict) else {}
    out = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in block:
            out[key] = block[key]
    return out


def _empty_log():
    """The cold start (D5): no channel published yet, the shape every later reader can take for
    granted."""
    return {'edge': None, 'stable': None, 'edge_log': []}


def read_log(product):
    """The channel log — through :func:`asf.state.store.read`. An absent file reads as
    :func:`_empty_log`, never ``None``: *no channel published yet* (D5)."""
    return store.read(product, LOG_NAME, default=_empty_log()).data


def write_log(product, fn):
    """Read-modify-write the channel log under its own lock (:func:`asf.state.store.update`):
    ``fn(log)`` mutates it in place, or returns the new one. ``(ok, detail)``, the same
    convention as :func:`publish` — a log the store cannot parse is never written over: the
    detail names why, and the caller publishes nothing this run."""
    try:
        store.update(product, LOG_NAME, fn, default=_empty_log())
    except store.StoreCorrupt as e:
        return False, f'{LOG_NAME} is corrupt: {e.why}'
    return True, ''


def note_edge(log, tag, commit, at, log_keep=None):
    """Set ``log['edge']`` and **prepend** one ``edge_log`` entry, newest first, then trim to
    ``log_keep`` (:data:`DEFAULTS`'s, or the product's own — :func:`settings`). The entry carries
    only ``tag``, ``commit`` and ``at``: the dwell the stable gate reads is this logged time,
    never the tag's own creation date (D5 — a tag is cut before its suite finishes, P1).
    Mutates ``log`` in place and returns it."""
    entry = {'tag': tag, 'commit': commit, 'at': at}
    log['edge'] = dict(entry)
    log['edge_log'] = [entry] + list(log.get('edge_log') or [])
    del log['edge_log'][(log_keep if log_keep is not None else DEFAULTS['log_keep']):]
    return log


def note_stable(log, tag, commit, at, rows):
    """Set ``log['stable']`` with its ``promoted_for`` — the row keys (:func:`stable_rows`'s)
    the promotion was made for, so an operator reads why without re-deriving the gate. Mutates
    ``log`` in place and returns it."""
    log['stable'] = {'tag': tag, 'commit': commit, 'at': at, 'promoted_for': list(rows)}
    return log


def _last_line(text):
    """The last non-empty line of ``text``, stripped — a failed push's ``stderr`` boiled down
    to the one line a report prints."""
    lines = [ln.strip() for ln in (text or '').splitlines() if ln.strip()]
    return lines[-1] if lines else ''


def edge_candidate(product, url, tags, run=subprocess.run, limit=None):
    """``(tag, commit, detail)``: the newest of ``tags`` (newest first, at most ``limit`` or
    ``DEFAULTS['lookback_tags']``) whose every landing check succeeded
    (:func:`asf.upgrade.ci_verdict`) — the first green wins, and a red or Unknown tag is skipped
    rather than stopping the scan. ``(None, None, <why>)`` when none did, ``<why>`` naming every
    verdict examined so the report says what it is waiting for. Unknown is never green: a tag
    with no CI run at all (``clear``), a pending check or an unreadable ``gh`` call all land here
    (PD10) — this function writes no verdict logic of its own."""
    from asf import upgrade
    seen = []
    for tag, commit in list(tags)[:limit or DEFAULTS['lookback_tags']]:
        verdict, detail = upgrade.ci_verdict(url, commit, run=run)
        if verdict == 'green':
            return tag, commit, f'{tag} {commit[:7]} green ({detail})'
        seen.append(f'{tag} {verdict}' + (f' ({detail})' if detail else ''))
    return None, None, '; '.join(seen) or 'no release tag to examine'


def publish(product, name, commit, run=subprocess.run, out=print):
    """Fast-forward ``refs/heads/releases/<name>`` on origin to ``commit``. ``(ok, detail)``; a
    guard refusal, a dry run (:func:`asf.mutation_guard.is_active`) and a non-fast-forward are
    each a detail, never an exception (D1) — the non-fast-forward is left as git's own refusal,
    the backstop for a candidate chosen from a stale log and what keeps a head from moving
    backwards."""
    from asf import gitpush, refguard
    target = f'{commit}:{REF.format(name=name)}'
    guard = refguard.Guard(main=product.main, protected=refguard.listed(product.conventions))
    p = gitpush.push(['-q', 'origin', target], product.repo_dir, guard=guard, refs_only=True,
                     log=lambda line: out(f'channels: {line}'))
    return (True, '') if p.returncode == 0 else (False, _last_line(p.stderr) or f'rc {p.returncode}')


# ------------------------------------------------------------- the stable gate --

def _parse(value):
    """An aware UTC :class:`datetime.datetime` for ``value`` — already one, or an ISO 8601
    string (``Z`` or an explicit offset). ``None`` when it is neither, or unreadable."""
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        d = datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return (d if d.tzinfo else d.replace(tzinfo=UTC)).astimezone(UTC)


def _hours_since(value, now):
    """Hours between ``value`` (an ISO stamp, or a ``datetime``) and ``now``, or ``None`` when
    ``value`` cannot be read."""
    d = _parse(value)
    return None if d is None else (now - d).total_seconds() / 3600


def s1_in_window(root, since, now=None):
    """``[(id, title, when)]`` — every Bug of severity S1 in the record whose ``stage_since`` or
    ``updated`` falls at or after ``since`` (and at or before ``now``, when given): an S1 raised,
    reopened or moved while the candidate sat on edge, open or closed again since (D9). The
    typed machine fields are read, never ``## History``: its dates are days, and the window is
    hours. Most severe first is moot (every row is S1); sorted by id."""
    from asf.record.core import canonicalize, load_items
    lo, hi = _parse(since), (_parse(now) if now is not None else None)
    by_id, _errors = load_items(root)
    canonical, _dupes = canonicalize(by_id)
    rows = []
    for iid, rec in sorted(canonical.items()):
        fm = rec.get('meta') or {}
        if str(fm.get('type') or '').lower() != 'bug':
            continue
        if str(fm.get('severity') or '').upper() != 'S1':
            continue
        when = None
        for field in ('stage_since', 'updated'):
            raw = fm.get(field)
            d = _parse(raw)
            if d is None or (lo is not None and d < lo) or (hi is not None and d > hi):
                continue
            when = raw
            break
        if when is not None:
            rows.append((iid, str(fm.get('title') or '').strip(), when))
    return rows


def _cadence_row(log, now, cfg):
    from asf.release import Criterion
    days = cfg['stable_every_days']
    stable = log.get('stable') or {}
    age_h = _hours_since(stable.get('at'), now) if stable.get('at') else None
    if age_h is None:
        return Criterion('cadence', 'Cadence (days since the last stable release)', True,
                         'no stable release yet')
    age_d = age_h / 24
    tag = stable.get('tag') or '?'
    if age_d >= days:
        return Criterion('cadence', 'Cadence (days since the last stable release)', True,
                         f'last promoted {tag} {age_d:.0f} d ago (every {days} d)')
    return Criterion('cadence', 'Cadence (days since the last stable release)', False,
                     f'{tag} promoted {age_d:.0f} d ago — due in {days - age_d:.0f} d')


def _dwell_row(log, candidate, now, cfg):
    from asf.release import Criterion
    tag, commit = candidate
    hours = cfg['edge_dwell_h']
    entry = next((e for e in (log.get('edge_log') or []) if e.get('commit') == commit), None)
    if entry is None:
        return Criterion('dwell', 'Dwell (hours published on edge)', False,
                         f'{tag} never published to edge')
    age_h = _hours_since(entry.get('at'), now)
    if age_h is None:
        return Criterion('dwell', 'Dwell (hours published on edge)', False,
                         f'{tag} never published to edge')
    return Criterion('dwell', 'Dwell (hours published on edge)', age_h >= hours,
                     f'{tag} on edge {age_h:.0f} h ({hours} h)')


def _rehearsal_row(product, candidate, cfg, run):
    from asf.release import Criterion
    tag, commit = candidate
    name = cfg['rehearsal_check']
    if str(name).strip().lower() == 'off':
        return Criterion('rehearsal', 'Rehearsal (the named check succeeded)', True,
                         'n/a (release.channels.rehearsal_check off)')
    from asf import upgrade
    slug = getattr(product, 'repo_slug', None)
    url = f'https://github.com/{slug}' if slug else ''
    verdict, detail = upgrade.ci_verdict(url, commit, run=run, checks=[name])
    return Criterion('rehearsal', 'Rehearsal (the named check succeeded)', verdict == 'green',
                     detail)


def _no_s1_row(root, log, candidate, now, cfg):
    from asf.release import Criterion
    from asf import release_preview
    _tag, commit = candidate
    entry = next((e for e in (log.get('edge_log') or []) if e.get('commit') == commit), None)
    since = entry.get('at') if entry else None
    if since is not None:
        windowed = s1_in_window(root, since, now=now)
        if windowed:
            iid, _title, when = windowed[0]
            return Criterion('no_s1', 'No S1 (open, or touched since the candidate reached '
                             'edge)', False, f'{iid} raised {str(when)[:10]} (S1)')
    opens = release_preview.open_defects(root, ('S1',))
    if opens:
        iid, _title, sev = opens[0]
        return Criterion('no_s1', 'No S1 (open, or touched since the candidate reached edge)',
                         False, f'{iid} open ({sev})')
    return Criterion('no_s1', 'No S1 (open, or touched since the candidate reached edge)', True,
                     f'no S1 since {since}' if since else 'no S1')


def stable_rows(product, root, log, candidate, now, cfg=None, run=subprocess.run):
    """``[Criterion]`` for promoting ``candidate`` (a ``(tag, commit)`` off the edge log) to
    stable: the cadence, the dwell, the rehearsal and the S1 window — in that order, each with
    the evidence an operator reads instead of asking. ``candidate`` ``None`` (or with no tag or
    no commit) makes every row unmet with one evidence line saying why there is no candidate."""
    from asf.release import Criterion
    cfg = cfg if cfg is not None else settings(product)
    if not candidate or not candidate[0] or not candidate[1]:
        why = 'no candidate: edge has not published a tag yet'
        return [Criterion('cadence', 'Cadence (days since the last stable release)', False, why),
                Criterion('dwell', 'Dwell (hours published on edge)', False, why),
                Criterion('rehearsal', 'Rehearsal (the named check succeeded)', False, why),
                Criterion('no_s1', 'No S1 (open, or touched since the candidate reached edge)',
                         False, why)]
    now = _parse(now) or now
    return [_cadence_row(log, now, cfg), _dwell_row(log, candidate, now, cfg),
            _rehearsal_row(product, candidate, cfg, run),
            _no_s1_row(root, log, candidate, now, cfg)]
