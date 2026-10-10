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
import json
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


def _lines_by_ref(lines):
    """``(heads, at)`` parsed once from the lines of one listing: ``heads`` is the channel name
    (under :data:`PREFIX`) mapped to its sha, ``at`` is a commit's sha mapped to every version
    tag whose peel names it. Shared by :func:`channel_tag` and :func:`all_tags`."""
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
    return heads, at


def channel_tag(lines, name):
    """``(tag, commit)`` the channel ``name`` offers, read from the lines of
    ``git ls-remote <url> 'refs/heads/releases/*' 'refs/tags/v*'``: the channel head's commit, and
    the highest version tag whose peel is that commit. ``(None, None)`` when the head is not
    published; ``(None, <commit>)`` when it is published at a commit no version tag names (a hand
    push, or a tag deleted since). Pure: no network, no clock, no config."""
    heads, at = _lines_by_ref(lines)
    commit = heads.get(name)
    tags = sorted(at.get(commit, []), key=version_key)
    return (tags[-1] if tags else None), commit


def all_tags(lines):
    """``[(tag, commit)]``, newest first — every version tag the lines of one listing name, the
    higher tag kept when two name one commit (:func:`channel_tag`'s own rule for a channel's own
    commit). What :func:`asf.channels.edge_candidate` scans, newest-first, for the newest green
    tag (D4). Pure: no network, no clock, no config."""
    _heads, at = _lines_by_ref(lines)
    best = [(sorted(tags, key=version_key)[-1], commit) for commit, tags in at.items()]
    return sorted(best, key=lambda t: version_key(t[0]), reverse=True)


def _out(run, cmd, timeout=60):
    """stdout of ``cmd``, or ``None`` when it fails or cannot start."""
    try:
        p = run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 and isinstance(p.stdout, str) else None


def _listing(url, run):
    """The lines of one ``git ls-remote <url> 'refs/heads/releases/*' 'refs/tags/v*'`` (D3), or
    ``None`` when the listing could not be read. Shared by every reader of one remote's channels
    and tags — :func:`resolve`, :func:`advance` and :func:`report`."""
    heads = 'refs/heads/' + PREFIX + '*'
    text = _out(run, ['git', 'ls-remote', url, heads, 'refs/tags/v*'])  # client-exempt: a url, no local
    # repo — asf.gitops's `cwd`-shaped client doesn't fit, exactly as upgrade.latest_release's
    # sibling call is already counted raw
    return text.splitlines() if text is not None else None


def resolve(url, name, run=subprocess.run):
    """``(tag, commit)`` of the channel ``name`` on ``url`` — one ``git ls-remote`` (D3).
    ``(None, None)`` when the channel is unpublished or the listing could not be read."""
    lines = _listing(url, run)
    return channel_tag(lines, name) if lines is not None else (None, None)


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


def _cadence_row(log, now, opts):
    from asf.release import Criterion
    days = opts['stable_every_days']
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


def _dwell_row(log, candidate, now, opts):
    from asf.release import Criterion
    tag, commit = candidate
    hours = opts['edge_dwell_h']
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


def _rehearsal_row(product, candidate, opts, run):
    from asf.release import Criterion
    tag, commit = candidate
    name = opts['rehearsal_check']
    if str(name).strip().lower() == 'off':
        return Criterion('rehearsal', 'Rehearsal (the named check succeeded)', True,
                         'n/a (release.channels.rehearsal_check off)')
    from asf import upgrade
    slug = getattr(product, 'repo_slug', None)
    url = f'https://github.com/{slug}' if slug else ''
    verdict, detail = upgrade.ci_verdict(url, commit, run=run, checks=[name])
    return Criterion('rehearsal', 'Rehearsal (the named check succeeded)', verdict == 'green',
                     detail)


def _no_s1_row(root, log, candidate, now, opts):
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


def stable_rows(product, root, log, candidate, now, opts=None, run=subprocess.run):
    """``[Criterion]`` for promoting ``candidate`` (a ``(tag, commit)`` off the edge log) to
    stable: the cadence, the dwell, the rehearsal and the S1 window — in that order, each with
    the evidence an operator reads instead of asking. ``candidate`` ``None`` (or with no tag or
    no commit) makes every row unmet with one evidence line saying why there is no candidate."""
    from asf.release import Criterion
    opts = opts if opts is not None else settings(product)
    if not candidate or not candidate[0] or not candidate[1]:
        why = 'no candidate: edge has not published a tag yet'
        return [Criterion('cadence', 'Cadence (days since the last stable release)', False, why),
                Criterion('dwell', 'Dwell (hours published on edge)', False, why),
                Criterion('rehearsal', 'Rehearsal (the named check succeeded)', False, why),
                Criterion('no_s1', 'No S1 (open, or touched since the candidate reached edge)',
                         False, why)]
    now = _parse(now) or now
    return [_cadence_row(log, now, opts), _dwell_row(log, candidate, now, opts),
            _rehearsal_row(product, candidate, opts, run),
            _no_s1_row(root, log, candidate, now, opts)]


# ------------------------------------------------------------- the daily part --

def _repo_url(product):
    """The ``https://github.com/<slug>`` url :func:`asf.upgrade.ci_verdict` and the remote
    listing both read — the same construction :func:`_rehearsal_row` already makes for one gh
    call. ``''`` for a product with no ``repo_slug``: every reader of it answers Unknown or
    unpublished, never a guess."""
    slug = getattr(product, 'repo_slug', None)
    return f'https://github.com/{slug}' if slug else ''


def _age(entry, now):
    """``'<n> d'``/``'<n> h'`` since ``entry['at']`` (a log entry — :func:`note_edge`'s or
    :func:`note_stable`'s own shape), or ``None`` when it has no ``at`` or cannot be read."""
    h = _hours_since(entry.get('at'), now) if entry.get('at') else None
    if h is None:
        return None
    d = h / 24
    return f'{d:.0f} d' if d >= 1 else f'{h:.0f} h'


def _stable_candidate(log, now, opts):
    """``(tag, commit)`` — the newest entry of ``log['edge_log']`` (itself newest first,
    :func:`note_edge`'s own prepend) published at least ``opts['edge_dwell_h']`` hours ago, the
    scan :func:`_dwell_row`'s own evidence describes (D5): a tag just published to edge is
    skipped in favour of the newest one that has already dwelled long enough, so stable always
    trails edge by the dwell window rather than being handed a candidate too young to promote.
    ``(None, None)`` when nothing in the log has dwelled that long yet."""
    hours = opts['edge_dwell_h']
    for entry in log.get('edge_log') or []:
        age_h = _hours_since(entry.get('at'), now)
        if age_h is not None and age_h >= hours:
            return entry.get('tag'), entry.get('commit')
    return None, None


def _edge_candidate_for(product, event, out, now, opts, url, lines, run):
    """Publish edge at the newest green tag when it differs from what is already published;
    mutates the log through :func:`write_log`. Returns ``(log, line)`` — the log as it now
    reads, and the one line the part prints for edge."""
    log = read_log(product)
    _tag, published = channel_tag(lines, 'edge')
    tag, commit, detail = edge_candidate(product, url, all_tags(lines), run=run,
                                         limit=opts['lookback_tags'])
    if not commit or commit == published:
        held = tag or (log.get('edge') or {}).get('tag') or 'none'
        return log, f'channels: edge {held} unchanged — {detail}'
    ok, pub_detail = publish(product, 'edge', commit, run=run, out=out)
    if not ok:
        return log, f'channels: edge holds — publish refused: {pub_detail}'
    at = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    wok, wdetail = write_log(product, lambda l: note_edge(l, tag, commit, at,
                                                          log_keep=opts['log_keep']))
    if not wok:
        return log, f'channels: edge {tag} {commit[:7]} published, log unreadable — {wdetail}'
    log = read_log(product)
    if event:
        event('channel', channel='edge', tag=tag, commit=commit, previous=published)
    was = f'was {published[:7]}' if published else 'first publication'
    return log, f'channels: edge {tag} {commit[:7]} ({was}) — {detail}'


def _stable_candidate_for(product, root, event, out, now, opts, log, run):
    """Promote stable to the dwelled candidate (:func:`_stable_candidate`) when all four rows
    are met; mutates the log through :func:`write_log`. Returns the one line the part prints
    for stable."""
    candidate = _stable_candidate(log, now, opts)
    rows = stable_rows(product, root, log, candidate, now, opts=opts, run=run)
    if not all(r.met for r in rows):
        held = (log.get('stable') or {}).get('tag') or 'none'
        unmet = next(r for r in rows if not r.met)
        return f'channels: stable holds at {held} — {unmet.key} no: {unmet.evidence}'
    tag, commit = candidate
    ok, pub_detail = publish(product, 'stable', commit, run=run, out=out)
    if not ok:
        return f'channels: stable holds — publish refused: {pub_detail}'
    at = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    keys = [r.key for r in rows]
    wok, wdetail = write_log(product, lambda l: note_stable(l, tag, commit, at, keys))
    if not wok:
        return f'channels: stable {tag} published, log unreadable — {wdetail}'
    if event:
        event('channel', channel='stable', tag=tag, commit=commit,
             previous=(log.get('stable') or {}).get('commit'), rows=keys)
    summary = ', '.join(f'{r.key} yes' for r in rows)
    return f'channels: stable {tag} — {summary}'


def advance(product, root, event=None, out=print, now=None, run=subprocess.run):
    """The daily part: publish edge at the newest green tag, then promote stable when all four
    rows are met. Returns **0** always — a remote, record or log that cannot be read is one
    line and a zero exit, because a channel that does not move today is not a failed tick
    (D10, P13). Nothing is published for a product whose repo is not the factory's own source
    (:func:`asf.drift.is_factory_source`). Each publication is one ``event`` line: ``kind``
    ``'channel'``, with ``channel``, ``tag``, ``commit``, ``previous`` and, for stable, ``rows``."""
    from asf import drift
    if not drift.is_factory_source(product.repo_dir or ''):
        return 0
    now = _parse(now) or datetime.datetime.now(UTC)
    opts = settings(product)
    url = _repo_url(product)
    lines = _listing(url, run)
    if lines is None:
        out('channels: edge unreadable — the remote listing could not be read')
        out('channels: stable unreadable — the remote listing could not be read')
        return 0
    log, edge_line = _edge_candidate_for(product, event, out, now, opts, url, lines, run)
    out(edge_line)
    out(_stable_candidate_for(product, root, event, out, now, opts, log, run))
    return 0


def report(product, root, now=None, run=subprocess.run):
    """The data behind ``asf channels``: both channels (resolved live from the remote), the
    stable candidate and the four gate rows with their evidence — shared by :func:`render` and
    ``--json`` so the two cannot drift. Read-only: no state file, no ref, no event, no commit."""
    now = _parse(now) or datetime.datetime.now(UTC)
    opts = settings(product)
    url = _repo_url(product)
    log = read_log(product)
    lines = _listing(url, run)
    channels_out = []
    for name in NAMES:
        tag, commit = channel_tag(lines, name) if lines is not None else (None, None)
        entry = log.get(name) or {}
        age = _age(entry, now) if entry.get('commit') == commit else None
        channels_out.append({'name': name, 'tag': tag, 'commit': commit, 'age': age})
    candidate = _stable_candidate(log, now, opts)
    rows = stable_rows(product, root, log, candidate, now, opts=opts, run=run)
    return {
        'as_of': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'channels': channels_out,
        'candidate': {'tag': candidate[0], 'commit': candidate[1]},
        'criteria': [{'key': r.key, 'name': r.name, 'met': r.met, 'evidence': r.evidence}
                    for r in rows],
        'ready': all(r.met for r in rows),
    }


def render(d, product):
    """The ``asf channels`` table — both channels, the stable candidate, and the four rows with
    their evidence — ending with :func:`asf.cli.stamp` the way every other stamped table does."""
    out = [f"**RELEASE CHANNELS** — {d['as_of']}", '',
          '| channel | version | commit | age |', '|---|---|---|---|']
    for row in d['channels']:
        tag = row['tag'] or 'unpublished'
        commit = (row['commit'] or '')[:7] or '—'
        age = row['age'] or '—'
        out.append(f"| {row['name']} | {tag} | {commit} | {age} |")
    out.append('')
    cand = d['candidate']
    out.append(f"Stable candidate: {cand['tag']}" if cand['tag'] else 'Stable candidate: none')
    out.append('')
    out += ['| # | Criterion | Met | Evidence |', '|---|---|---|---|']
    for i, c in enumerate(d['criteria'], 1):
        out.append(f"| {i} | {c['name']} | {'yes' if c['met'] else 'NO'} | "
                  f"{c['evidence'].replace('|', '/')} |")
    out.append('')
    out.append(cli.stamp('channels', repo=product.repo_dir))
    return '\n'.join(out) + '\n'


def cmd_channels(args, root):
    """``asf channels [--product P] [--json]`` — read-only (D12): resolves both channels from
    the remote, reads the log and the record, and prints. Nothing is written."""
    from asf import env
    product = env.load_product(getattr(args, 'product', None))
    d = report(product, root)
    if getattr(args, 'json', False):
        print(json.dumps(d, indent=1, default=str))
    else:
        print(render(d, product), end='')
    return 0
