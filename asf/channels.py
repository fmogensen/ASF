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
:func:`publish`, and :func:`stable_rows`/:func:`s1_in_window`. The daily part (:func:`advance`)
and the read-only report (:func:`report`, :func:`cmd_channels`) are this same module's last
Task of the plan (``docs/plans/f-0308.md``).
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


# ------------------------------------------------------------- the daily part and the report --

def _release_tags(repo_dir):
    """``[(tag, commit)]`` — every release tag in ``repo_dir`` (:func:`asf.gitops.git`), newest
    first by :func:`version_key`. The commit is an annotated tag's peel, or the tag's own object
    for a lightweight one. ``[]`` when the checkout or its tags cannot be read (an unreadable
    repo is never a candidate, never an exception)."""
    from asf import gitops
    result = gitops.git(
        ['for-each-ref', '--format=%(refname:short)%09%(*objectname)%09%(objectname)',
         'refs/tags/v*'], repo_dir)
    tags = []
    for line in (result.data or '').splitlines() if result.ok else []:
        fields = line.split('\t')
        if len(fields) != 3:
            continue
        name, peeled, obj = (f.strip() for f in fields)
        if not cli.RELEASE_TAG.fullmatch(name):
            continue
        commit = peeled or obj
        if commit:
            tags.append((name, commit))
    return sorted(tags, key=lambda t: version_key(t[0]), reverse=True)


def _stable_candidate(log, now, opts):
    """``(tag, commit)`` — the newest entry in ``log['edge_log']`` (itself newest first) whose
    publication is at least ``edge_dwell_h`` hours old: the dwell row's own rule (:func:`_dwell_row`
    re-checks exactly this candidate against the same log entry). ``(None, None)`` when nothing
    has dwelled long enough yet, or the log has no edge history at all."""
    hours = opts['edge_dwell_h']
    for entry in log.get('edge_log') or []:
        age_h = _hours_since(entry.get('at'), now)
        if age_h is not None and age_h >= hours:
            return entry.get('tag'), entry.get('commit')
    return None, None


def _advance_stable(product, log, root, opts, now, now_iso, out, run, event):
    candidate = _stable_candidate(log, now, opts)
    rows = stable_rows(product, root, log, candidate, now, opts=opts, run=run)
    if not all(r.met for r in rows):
        unmet = next(r for r in rows if not r.met)
        held_at = (log.get('stable') or {}).get('tag') or 'no stable release yet'
        out(f'channels: stable holds at {held_at} — {unmet.key} no: {unmet.evidence}')
        return
    tag, commit = candidate
    before = log.get('stable') or {}
    if commit == before.get('commit'):
        out(f'channels: stable unchanged at {tag}')
        return
    ok, why = publish(product, 'stable', commit, run=run, out=out)
    if not ok:
        out(f'channels: stable publish failed — {why}')
        return
    was = before.get('tag') or before.get('commit')
    keys = [r.key for r in rows]
    note_stable(log, tag, commit, now_iso, keys)
    ok_log, log_detail = write_log(product, lambda l: note_stable(l, tag, commit, now_iso, keys))
    if not ok_log:
        out(f'channels: stable log not written — {log_detail}')
    out(f'channels: stable {tag}' + (f' (was {was})' if was else '') + ' — ' +
       ', '.join(f'{k} yes' for k in keys))
    if event is not None:
        event('channel', channel='stable', tag=tag, commit=commit, previous=was, rows=keys)


def advance(product, root, event=None, out=print, now=None, run=subprocess.run):
    """The daily part (D10): publish ``edge`` at the newest green tag, then promote ``stable``
    when all four rows of :func:`stable_rows` are met. Always returns **0** — a remote, record
    or log this cannot read is one line and this still moves on, because a channel that does not
    move today is not a failed tick. Publishes nothing at all for a product whose repo is not
    the factory's own source (:func:`asf.drift.is_factory_source`, P11): no product is named
    anywhere. Each publication is one ``event('channel', ...)`` line, never raised when ``event``
    is left ``None``."""
    from asf import drift
    repo_dir = getattr(product, 'repo_dir', None)
    if not repo_dir or not drift.is_factory_source(repo_dir):
        out("channels: not run — this product's repo is not the factory's own source")
        return 0
    now_dt = _parse(now) or datetime.datetime.now(UTC)
    now_iso = now_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    log = read_log(product)
    opts = settings(product)
    slug = getattr(product, 'repo_slug', None)
    url = f'https://github.com/{slug}' if slug else ''

    _advance_edge(product, log, url, opts, now_iso, out, run, event)
    _advance_stable(product, log, root, opts, now_dt, now_iso, out, run, event)
    return 0


def _advance_edge(product, log, url, opts, now_iso, out, run, event):
    tags = _release_tags(product.repo_dir)
    tag, commit, detail = edge_candidate(product, url, tags, run=run, limit=opts['lookback_tags'])
    if commit is None:
        out(f'channels: edge holds — {detail}')
        return
    previous = log.get('edge') or {}
    if commit == previous.get('commit'):
        out(f'channels: edge unchanged — {detail}')
        return
    ok, why = publish(product, 'edge', commit, run=run, out=out)
    if not ok:
        out(f'channels: edge publish failed — {why}')
        return
    was = previous.get('tag') or previous.get('commit')
    note_edge(log, tag, commit, now_iso, log_keep=opts['log_keep'])
    ok_log, log_detail = write_log(
        product, lambda l: note_edge(l, tag, commit, now_iso, log_keep=opts['log_keep']))
    if not ok_log:
        out(f'channels: edge log not written — {log_detail}')
    out(f'channels: edge {detail}' + (f' (was {was})' if was else ''))
    if event is not None:
        event('channel', channel='edge', tag=tag, commit=commit, previous=was)


def _age_str(hours):
    """``'2 h'`` under two days, else ``'6 d'`` — the table's ``age`` column. ``'?'`` when the
    log names no time at all (a channel never published)."""
    if hours is None:
        return '?'
    if hours < 48:
        return f'{hours:.0f} h'
    return f'{hours / 24:.0f} d'


def report(product, root, now=None, run=subprocess.run):
    """The data behind ``asf channels`` — both channels, the stable candidate and the four gate
    rows with their evidence, shared by the table and ``--json`` so the two cannot drift.
    Read-only (D12): it reads the log and the record and asks ``gh`` for the rehearsal check; it
    never writes the log, pushes a ref, or writes an event."""
    now_dt = _parse(now) or datetime.datetime.now(UTC)
    log = read_log(product)
    opts = settings(product)
    candidate = _stable_candidate(log, now_dt, opts)
    rows = stable_rows(product, root, log, candidate, now_dt, opts=opts, run=run)
    channels_out = []
    for name in NAMES:
        entry = log.get(name) or {}
        channels_out.append({
            'channel': name,
            'tag': entry.get('tag'),
            'commit': entry.get('commit'),
            'age_h': _hours_since(entry.get('at'), now_dt) if entry.get('at') else None,
        })
    return {
        'now': now_dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'channels': channels_out,
        'candidate': {'tag': candidate[0], 'commit': candidate[1]},
        'rows': [{'key': r.key, 'name': r.name, 'met': r.met, 'evidence': r.evidence}
                for r in rows],
    }


def render(data):
    """The ``asf channels`` table — two channels, the stable candidate and its four rows — over
    exactly what :func:`report` returns, so the table and ``--json`` can never drift apart."""
    lines = [f"**RELEASE CHANNELS** — {data['now']}", '',
            '| channel | version | commit | age |', '|---|---|---|---|']
    for c in data['channels']:
        lines.append(f"| {c['channel']} | {c['tag'] or '—'} | "
                     f"{(c['commit'] or '—')[:7]} | {_age_str(c['age_h'])} |")
    lines.append('')
    cand = data['candidate']
    lines.append(f"Stable candidate: {cand['tag']}" if cand['tag'] else 'Stable candidate: none')
    lines.append('')
    lines.append('| # | Criterion | Met | Evidence |')
    lines.append('|---|---|---|---|')
    for i, row in enumerate(data['rows'], 1):
        lines.append(f"| {i} | {row['name']} | {'yes' if row['met'] else 'NO'} | "
                     f"{row['evidence'].replace('|', '/')} |")
    return '\n'.join(lines) + '\n'


def cmd_channels(args, root):
    """``asf channels [--product P] [--json]`` — the table of :func:`render`, ending with
    ``cli.stamp`` the way every other stamped table does, or the same data as one JSON document
    (D12). Writes nothing: no state file, no ref, no event, no commit."""
    from asf import env
    product = env.load_product(getattr(args, 'product', None))
    data = report(product, root)
    if getattr(args, 'json', False):
        print(json.dumps(data, indent=1, default=str))
        return 0
    print(render(data))
    print(cli.stamp('channels', version=cli.version_string()))
    return 0
