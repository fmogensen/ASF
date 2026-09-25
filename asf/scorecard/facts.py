"""asf.scorecard.facts — everything the scorecard reads, read once into one :class:`Facts`.

Sources, all facts the factory already writes:

* the record's cards: type, parent, state, and the History lines — a card's first dated line is
  when it was carded, the ingest's ``stage … → landed`` / ``state … → Resolved`` line is when it
  landed, ``stage … → on-prod`` / ``state … → Closed`` is when it reached production (a product
  that deploys nothing is Closed the moment it lands);
* the record's metric streams: ``metrics/sessions`` (spend and tokens per session, matched to a
  card), ``metrics/ci`` (runner minutes and jobs, from the forge's CI API) and ``metrics/gates``
  (the local landing gate's seconds);
* the session registry (:func:`asf.improve.measure.ended_runs`): every ended run and why it ended;
* the forge, optionally: open PRs and branches (clutter). ``None`` where it could not be read.

The parsing helpers here are pure; :func:`load` is the only function that reads a file.
"""
import dataclasses
import datetime
import json
import os
import re

STAMP = '%Y-%m-%dT%H:%M:%SZ'
_HIST_RE = re.compile(r'^-\s+(\d{4}-\d\d-\d\d)(?:[ T](\d\d:\d\d)Z?)?\b')
_STAGE_RE = re.compile(r'\bstage (.+?) → (.+?)(?: \(|$)')
_STATE_RE = re.compile(r'\bstate (\w+) → (\w+)')
_ROUND_RE = re.compile(r'\br(\d+)$')
ID_RE = re.compile(r'(?<![A-Za-z0-9])([EFSTBDR])-(\d{4})(?![0-9])', re.IGNORECASE)
LANDED_STAGES = ('landed', 'on-prod')
LANDED_STATES = ('Resolved', 'Closed')
LADDER = ('card', 'spec-draft', 'spec-review', 'spec-approved', 'plan-draft', 'plan-review',
          'plan-approved', 'building', 'landed', 'on-prod')


@dataclasses.dataclass
class Facts:
    items: dict                 # id -> card summary (see :func:`card`)
    sessions: list              # metrics/sessions events
    ci: list                    # metrics/ci events
    gates: list                 # metrics/gates events
    runs: list                  # asf.improve.measure.Run, every ended registry run
    clutter: dict               # {'stale_prs': int|None, 'open_prs': int|None, 'branches': int|None}
    as_of: str = ''             # the reading's clock, ISO


# ----------------------------------------------------------------- time --

def to_dt(stamp):
    """An ISO stamp (``…Z``, an offset, or a bare date) → aware UTC datetime; ``None`` if unreadable."""
    if not stamp:
        return None
    s = str(stamp).strip()
    try:
        if len(s) == 10:
            d = datetime.datetime.strptime(s, '%Y-%m-%d')
        else:
            d = datetime.datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d.astimezone(datetime.timezone.utc)


def iso(d):
    return d.astimezone(datetime.timezone.utc).strftime(STAMP)


def now_iso():
    return iso(datetime.datetime.now(datetime.timezone.utc))


# -------------------------------------------------------------- History --

def history(body):
    """``[(stamp, text)]`` of the ``## History`` section's dated lines, in order. A line with only
    a date is stamped at midnight UTC."""
    out, inside = [], False
    for line in (body or '').splitlines():
        if line.startswith('## '):
            inside = line.strip() == '## History'
            continue
        if not inside:
            continue
        m = _HIST_RE.match(line.strip())
        if not m:
            continue
        out.append((f"{m.group(1)}T{m.group(2) or '00:00'}:00Z", line.strip()))
    return out


def _base(stage):
    s = (stage or '').strip()
    s = re.sub(r'\s+\d+/\d+$', '', s)
    return _ROUND_RE.sub('', s).strip()


def _round(stage):
    m = _ROUND_RE.search((stage or '').strip())
    return int(m.group(1)) if m else None


def timeline(meta, body):
    """What a card's History says about it: ``created``, ``landed``, ``prod`` (stamps or ``None``),
    ``send_backs`` (a review round that went up, or a review that went back to its draft) and
    ``reopens`` (a landed card that went back to Active, or a landed stage that went back).

    ``landed``/``prod`` are the *last* entry into the state that stuck: a card reopened after it
    landed has not landed until it lands again. A card that is landed now but whose History does
    not say when falls back to ``stage_since``."""
    lines = history(body)
    created = lines[0][0] if lines else (meta.get('stage_since') or meta.get('updated'))
    landed = prod = None
    send_backs = reopens = 0
    for stamp, text in lines:
        m = _STAGE_RE.search(text)
        if m:
            a, b = m.group(1).strip(), m.group(2).strip()
            ba, bb = _base(a), _base(b)
            ra, rb = _round(a), _round(b)
            if ba == bb and ra and rb and rb > ra:
                send_backs += 1
            elif ba.endswith('-review') and bb == ba.replace('-review', '-draft'):
                send_backs += 1
            if bb in LANDED_STAGES and landed is None:
                landed = stamp
            if bb == 'on-prod' and prod is None:
                prod = stamp
            if ba in LANDED_STAGES and bb not in LANDED_STAGES:
                reopens += 1
                landed = prod = None
            elif ba == 'on-prod' and bb != 'on-prod':
                prod = None
        m = _STATE_RE.search(text)
        if m:
            a, b = m.group(1), m.group(2)
            if b in LANDED_STATES and landed is None:
                landed = stamp
            if b == 'Closed' and prod is None:
                prod = stamp
            if a in LANDED_STATES and b not in LANDED_STATES:
                reopens += 1
                landed = prod = None
    state, stage = meta.get('state'), _base(meta.get('stage'))
    now_landed = state in LANDED_STATES or stage in LANDED_STAGES
    now_prod = state == 'Closed' or stage == 'on-prod'
    since = meta.get('stage_since') or meta.get('updated')
    if not now_landed:
        landed = prod = None
    elif landed is None:
        landed = since
    if not now_prod:
        prod = None
    elif prod is None:
        prod = since
    return {'created': created, 'landed': landed, 'prod': prod,
            'send_backs': send_backs, 'reopens': reopens}


def _description(body):
    """The card's own words — every section but the derived Children/Backlinks and History."""
    out, skip = [], False
    for line in (body or '').splitlines():
        if line.startswith('## '):
            skip = line.strip() in ('## Children', '## Backlinks', '## History')
        if not skip:
            out.append(line)
    return '\n'.join(out)


def card(meta, body):
    """One card's summary: its typed fields, its timeline, and its own text (for mentions)."""
    t = timeline(meta, body)
    return dict(t, id=meta.get('id'), type=meta.get('type'), parent=meta.get('parent'),
                title=str(meta.get('title') or ''), state=meta.get('state'),
                stage=meta.get('stage'), severity=meta.get('severity'),
                removed=bool(meta.get('removed')), text=_description(body))


def ids_in(text):
    """Every card id a branch name or a line names, upper-cased, in order, once."""
    seen = []
    for m in ID_RE.finditer(text or ''):
        iid = f'{m.group(1).upper()}-{m.group(2)}'
        if iid not in seen:
            seen.append(iid)
    return seen


# ---------------------------------------------------------------- load --

def load_cards(root):
    from asf.record.core import canonicalize, load_items
    by_id, _errors = load_items(root)
    canonical, _dupes = canonicalize(by_id)
    return {iid: card(rec['meta'], rec['body']) for iid, rec in canonical.items() if iid}


def _stream(root, name):
    from asf.metrics.metrics import read_stream
    try:
        return read_stream(root, name)
    except (OSError, json.JSONDecodeError):
        return []


def forge_clutter(product, stale_days=3, now=None, gh_json=None, gh_lines=None):
    """``{'open_prs', 'stale_prs', 'branches'}`` from the forge: open PRs, those not updated for
    ``stale_days``, and branches other than the trunk that no open PR carries. ``None`` for a
    number the forge did not answer."""
    out = {'open_prs': None, 'stale_prs': None, 'branches': None}
    slug = getattr(product, 'repo_slug', None)
    if not slug:
        return out
    if gh_json is None:
        from asf.metrics.metrics import gh_json
    if gh_lines is None:
        from asf.metrics.metrics import gh_lines
    now = now or datetime.datetime.now(datetime.timezone.utc)
    prs = gh_json(['pr', 'list', '--repo', slug, '--state', 'open', '--limit', '500',
                   '--json', 'number,updatedAt,headRefName'])
    heads = set()
    if isinstance(prs, list):
        out['open_prs'] = len(prs)
        cut = now - datetime.timedelta(days=stale_days)
        out['stale_prs'] = sum(1 for p in prs if (to_dt(p.get('updatedAt')) or now) < cut)
        heads = {p.get('headRefName') for p in prs}
    # one JSON string per line: --paginate prints one document per page, never one array
    branches = gh_lines(['api', f'repos/{slug}/branches?per_page=100', '--paginate',
                         '--jq', '.[].name|@json'])
    if branches:        # a repo always has its trunk: nothing back is the forge not answering
        main = getattr(product, 'main', 'main')
        out['branches'] = sum(1 for b in branches if b != main and b not in heads)
    return out


def load(root, product=None, *, registry=True, forge=True, as_of=None):
    """Every fact the scorecard reads for ``product`` from its record ``root``. ``registry`` and
    ``forge`` off skip the two slower sources (the status row reads neither)."""
    runs = []
    if registry and product is not None:
        try:
            from asf.improve import measure
            runs = measure.ended_runs(product)
        except (OSError, ValueError):
            runs = []
    clutter = {'open_prs': None, 'stale_prs': None, 'branches': None}
    if forge and product is not None:
        try:
            clutter = forge_clutter(product)
        except Exception:  # noqa: BLE001 — the forge being down never loses the scorecard
            pass
    return Facts(items=load_cards(root), sessions=_stream(root, 'sessions'), ci=_stream(root, 'ci'),
                 gates=_stream(root, 'gates'), runs=runs, clutter=clutter,
                 as_of=as_of or now_iso())


def state_file(product, name):
    from asf import env
    return os.path.join(env.state_dir(product), name)
