"""asf.scorecard.facts — everything the scorecard reads, read once into one :class:`Facts`.

Sources, all facts the factory already writes:

* the record's cards: type, parent, state, and the History lines — a card's first dated line is
  when it was carded, the ingest's ``stage … → landed`` / ``state … → Resolved`` line is when it
  landed, ``stage … → on-prod`` / ``state … → Closed`` is when it reached production (a product
  that deploys nothing is Closed the moment it lands) — and, beside it, what prod runs: a landed
  Feature whose landing commit — its own, else the newest of its Stories'/Tasks' — is an
  ancestor of the deployed prod sha (the Prod row's source, :func:`prod_deployment`) is on prod
  from that deploy (:func:`attribute_prod`); one with no traceable commit is a diagnostic line;
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
    diagnostics: list = dataclasses.field(default_factory=list)  # gaps in the reading, one line each


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


_LANDING_RE = re.compile(r'^(?:merge|commit) ([0-9a-f]{7,40}) (?:of .+ lands|names) ')
#: a plan-matched Task's merged PR (``PR #7 merged (74df364ab)``), a Bug's fix (``fix merged (…)``)
_MERGED_RE = re.compile(r'^(?:PR #\d+ |fix )merged \(([0-9a-f]{7,40})\)')
_SHA_RE = re.compile(r'^[0-9a-f]{7,40}$')
#: what the diagnostics say of a landed Feature no commit can be traced to
UNTRACEABLE = 'landed without a traceable commit'


def landing_shas(meta):
    """The commits the record says landed this card: a typed ``landed: <sha>``, and the ingest's
    evidence (``merge <sha> of <branch> lands <id>``, ``commit <sha> names <id>``,
    ``PR #<n> merged (<sha>)``, ``fix merged (<sha>)``)."""
    ev = meta.get('evidence')
    out = []
    typed = str(meta.get('landed') or '').strip()
    if _SHA_RE.match(typed):
        out.append(typed)
    for line in ev if isinstance(ev, list) else []:
        m = _LANDING_RE.match(str(line)) or _MERGED_RE.match(str(line))
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def card(meta, body):
    """One card's summary: its typed fields, its timeline, and its own text (for mentions)."""
    t = timeline(meta, body)
    stories = meta.get('stories')
    return dict(t, id=meta.get('id'), type=meta.get('type'), parent=meta.get('parent'),
                title=str(meta.get('title') or ''), state=meta.get('state'),
                stage=meta.get('stage'), severity=meta.get('severity'),
                removed=bool(meta.get('removed')), text=_description(body),
                landing_shas=landing_shas(meta),
                stories=[str(x) for x in stories] if isinstance(stories, list) else [],
                lane=str(meta.get('lane') or '').strip().lower() or None,
                size=str(meta.get('size') or '').strip().lower() or None,
                ab_pair=str(meta.get('ab_pair') or '').strip() or None,
                links=meta.get('links') if isinstance(meta.get('links'), dict) else {})


def _later(a, b):
    da, db = to_dt(a), to_dt(b)
    if da is None or db is None:
        return a or b
    return iso(max(da, db))


def _children(items):
    """``{id: [child id, …]}`` — by ``parent``, and a Task under each Story its ``stories:`` name."""
    kids = {}
    for iid, it in items.items():
        for p in [it.get('parent')] + list(it.get('stories') or ()):
            if p and p in items and iid not in kids.get(p, ()):
                kids.setdefault(p, []).append(iid)
    return kids


def _descendants(fid, items, kids):
    """Every live Story/Task beneath ``fid``, nearest first."""
    out, seen, queue = [], {fid}, list(kids.get(fid, ()))
    while queue:
        c = queue.pop(0)
        if c in seen or items[c].get('removed'):
            continue
        seen.add(c)
        out.append(c)
        queue += kids.get(c, ())
    return out


def _newest(shas, is_ancestor):
    """The newest of ``shas`` on the trunk: the one every other is an ancestor of. ``None`` when
    history does not order them (then every one of them must be on prod)."""
    for s in shas:
        if all(o == s or is_ancestor(o, s) for o in shas):
            return s
    return None


def landing_commits(fid, items, kids, naming=None):
    """The commits that landed Feature ``fid``: its own landing sha when it has one; else its
    live descendants' — each Story's/Task's own evidence sha, else the newest trunk commit whose
    subject names it (``naming(id)``). ``[]`` when none can be traced."""
    own = list(items[fid].get('landing_shas') or [])
    if own:
        return own
    shas = []
    for c in _descendants(fid, items, kids):
        found = list(items[c].get('landing_shas') or [])
        if not found and naming is not None:
            sha = naming(c)
            found = [sha] if sha else []
        shas += [s for s in found if s not in shas]
    return shas


def attribute_prod(items, prod_sha, prod_at, is_ancestor, naming=None, untraced=None):
    """Mark each landed Feature the record has not yet put on prod as on prod when the commit
    that landed it (:func:`landing_commits` — its own, else the newest of its descendants') is an
    ancestor of ``prod_sha`` (the deployed sha the Prod row reads). Its ``prod`` stamp is then the
    later of its landing and the deployment (``prod_at``). The record's own ``on-prod``/``Closed``
    also waits on the operator's tick; this is what production runs. A Feature no commit can be
    traced to is appended to ``untraced`` (once), never silently dropped. Returns the ids marked."""
    kids = _children(items)
    marked = []
    for fid, f in items.items():
        if f.get('type') != 'feature' or f.get('removed') or not f.get('landed') or f.get('prod'):
            continue
        shas = landing_commits(fid, items, kids, naming)
        if not shas:
            if untraced is not None and fid not in untraced:
                untraced.append(fid)
            continue
        if not prod_sha:
            continue
        newest = _newest(shas, is_ancestor) if len(shas) > 1 else shas[0]
        f['landing_sha'] = newest
        if (is_ancestor(newest, prod_sha) if newest else all(is_ancestor(s, prod_sha) for s in shas)):
            at = prod_at
            if isinstance(at, (int, float)):  # epoch milliseconds
                at = iso(datetime.datetime.fromtimestamp(at / 1000, tz=datetime.timezone.utc))
            f['prod'] = _later(f['landed'], at) if at else f['landed']
            marked.append(fid)
    return marked


def diagnostics(items, untraced):
    """The scorecard's diagnostic lines: one per landed Feature no commit can be traced to."""
    return [f"{fid} {UNTRACEABLE}" + (f" ({items[fid]['title'][:48]})" if items[fid].get('title') else '')
            for fid in untraced]


def prod_deployment(product):
    """``(sha, at)`` of what prod runs — the Prod row's own source
    (:func:`asf.harvest.deploy.facts`) — or ``(None, None)`` when prod is not managed or does
    not read."""
    from asf.harvest import deploy
    try:
        if not deploy.env_applies(product, 'prod'):
            return None, None
        f = deploy.facts(product, env='prod')
    except Exception:  # noqa: BLE001 — the forge being down never loses the scorecard
        return None, None
    return f.get('deployed'), f.get('at')


def _git_ancestor(product):
    import subprocess

    def check(sha, base):
        try:
            return subprocess.run(['git', '-C', product.repo_dir, 'merge-base', '--is-ancestor',
                                   sha, base], capture_output=True, timeout=30).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    return check


def _trunk_naming(product):
    """``id -> sha``: the newest commit on the trunk (``origin/<main>``, else ``<main>``) whose
    subject names the id — the same id token the evidence reads, a document-lane commit (a spec, a
    plan, a review) excepted. The log is read once, on first use."""
    import subprocess
    from asf.evidence.evidence import DOC_LANE_SUBJECT, naming_ids
    table = []

    def read():
        main = getattr(product, 'main', None) or 'main'
        for ref in (f'origin/{main}', main):
            try:
                r = subprocess.run(['git', '-C', product.repo_dir, 'log', '--format=%H%x09%s', ref],
                                   capture_output=True, text=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                return {}
            if r.returncode == 0:
                break
        else:
            return {}
        out = {}
        for line in r.stdout.splitlines():  # newest first: the first commit naming an id wins
            sha, _, subject = line.partition('\t')
            if not sha or DOC_LANE_SUBJECT.search(subject):
                continue
            for iid in naming_ids(subject, main):
                out.setdefault(iid, sha)
        return out

    def naming(iid):
        if not table:
            table.append(read())
        return table[0].get(iid)
    return naming


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
    items = load_cards(root)
    untraced = []
    if product is not None and getattr(product, 'repo_dir', None):
        sha, at = prod_deployment(product)
        attribute_prod(items, sha, at, _git_ancestor(product), naming=_trunk_naming(product),
                       untraced=untraced)
    return Facts(items=items, sessions=_stream(root, 'sessions'), ci=_stream(root, 'ci'),
                 gates=_stream(root, 'gates'), runs=runs, clutter=clutter,
                 as_of=as_of or now_iso(), diagnostics=diagnostics(items, untraced))


def state_file(product, name):
    from asf import env
    return os.path.join(env.state_dir(product), name)
