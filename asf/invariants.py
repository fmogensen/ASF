"""asf.invariants — the executable invariants (the registry; W4 fills it).

Each :class:`Invariant` has an ``id`` (``I1``…), a ``scope`` and a ``check(ctx) -> [Finding]``,
pure over the facts ``ctx`` carries. They run at three points of the tick, and every one fails
**soft** — an invariant never aborts a tick:

- ``record``: over the staged record tree before ``commit_and_push`` (I2 after ingest's
  restamp). A finding reverts only the offending paths (``git checkout -- <paths>``), the rest
  of the tick commits, and one Bug is filed per ``(invariant, path)``.
- ``feeder``: over ``plan_rows`` before the wave. A violating row is dropped and logged as
  ``INVARIANT <id>: <row>``.
- ``lane``: after harvest. Reported only.

``asf check --invariants`` runs them all read-only against the record and state directory, and
the tests assert the same checks. I6, I12 and I13 are tests, not tick checks; I9 is an event,
not a violation.
"""
from dataclasses import dataclass, field
from typing import Callable

#: The points of the tick an invariant runs at.
SCOPES = ('record', 'feeder', 'lane')


@dataclass(frozen=True)
class Finding:
    """One violation: which invariant, where, and what to revert (``record`` scope) or drop
    (``feeder`` scope, ``subject`` the row)."""
    invariant: str
    scope: str
    subject: str
    message: str
    paths: tuple = field(default_factory=tuple)


@dataclass(frozen=True)
class Invariant:
    """``check(ctx) -> [Finding]``, pure over ``ctx``."""
    id: str
    scope: str
    check: Callable


#: The registry: I1–I3, I10 and I11 (the record, below); W4 adds the rest.
INVARIANTS = []


def run(ctx, scope=None):
    """Every registered invariant (only those of ``scope`` when given) over ``ctx``: the
    findings, in registry order; ``[]`` when all hold."""
    if scope is not None and scope not in SCOPES:
        raise ValueError(f'not an invariant scope: {scope!r} (one of {", ".join(SCOPES)})')
    findings = []
    for inv in INVARIANTS:
        if scope is None or inv.scope == scope:
            findings.extend(inv.check(ctx) or ())
    return findings


# ---- the record invariants (I1, I2, I3, I10, I11) ------------------------------------------
#
# Each reads a ``stage.RecordContext``: the clone ``root`` and one writer's ``Staged`` change.
# A finding names only paths that writer changed, and only a violation the writer *introduced*
# (absent before it ran): a card that was already in that shape is not this writer's to answer
# for, and refusing its write would only freeze the card where it stands.

def _card_paths(staged):
    from asf.record.core import ITEM_FOLDERS
    return [p for p in staged.paths
            if p.endswith('.md') and p.split('/', 1)[0] in ITEM_FOLDERS and p.count('/') == 1]


def _parse(text, path):
    from asf.record import frontmatter
    if text is None:
        return None
    try:
        return frontmatter.parse(text, path=path)[0]
    except frontmatter.FrontmatterError:
        return False


def _after(ctx, path):
    import os
    key = ('after', path)
    if key not in ctx.cache:
        full = os.path.join(ctx.root, path)
        text = None
        if os.path.isfile(full):
            with open(full, encoding='utf-8', errors='surrogateescape') as f:
                text = f.read()
        ctx.cache[key] = (text, _parse(text, path))
    return ctx.cache[key]


def _before(ctx, path):
    key = ('before', path)
    if key not in ctx.cache:
        text = ctx.staged.before.get(path)
        ctx.cache[key] = (text, _parse(text, path))
    return ctx.cache[key]


def _machine(meta):
    from asf.record import frontmatter
    return frontmatter.split_machine(meta)[1] if meta else {}


def _finding(inv, subject, message, path):
    return Finding(inv, 'record', subject, message, (path,))


def check_i1(ctx):
    """I1 — never strip machine keys: every machine key a card had before the writer ran is still
    there after it, except the derivable ones (``frontmatter.DERIVABLE_KEYS``); a card the writer
    left unparseable has lost its whole block."""
    from asf.record import frontmatter
    out = []
    for path in _card_paths(ctx.staged):
        _bt, before = _before(ctx, path)
        text, after = _after(ctx, path)
        if not before or text is None:
            continue  # a card this writer created, or removed: nothing of it was stripped
        if after is False:
            out.append(_finding('I1', path, 'the card no longer parses', path))
            continue
        lost = [k for k in _machine(before) if k not in _machine(after)
                and k not in frontmatter.DERIVABLE_KEYS]
        if lost:
            out.append(_finding('I1', after.get('id') or path,
                                f"machine key(s) stripped: {', '.join(lost)}", path))
    return out


def check_i2(ctx):
    """I2 — every card carries the record's ``schema_version`` (``schema.record_version``). It
    runs after ingest's restamp (R11): a card the migration left below the stamp is brought up by
    the restamp in the same writer, so only a writer that writes a card below the stamp — a new
    one, or one it stripped — is refused."""
    from asf import schema
    if 'record_version' not in ctx.cache:
        ctx.cache['record_version'] = schema.record_version(ctx.root)
    version = ctx.cache['record_version']
    if not version:
        return []  # no index.json yet, or an unstamped record: `schema-migrate`'s to stamp
    out = []
    for path in _card_paths(ctx.staged):
        _bt, before = _before(ctx, path)
        _at, after = _after(ctx, path)
        if not after:
            continue
        have = int(_machine(after).get('schema_version') or 0)
        had = int(_machine(before).get('schema_version') or 0) if before else None
        if have < version and (had is None or had >= version or have < had):
            out.append(_finding('I2', after.get('id') or path,
                                f'schema_version {have or "missing"} below the record\'s {version}',
                                path))
    return out


def _record(ctx):
    """``{id: meta}`` of every card after the writer, loaded once per validation."""
    if 'record' not in ctx.cache:
        from asf.record.core import canonicalize, load_items
        by_id, _errors = load_items(ctx.root)
        canonical, _dupes = canonicalize(by_id)
        ctx.cache['record'] = {iid: rec['meta'] for iid, rec in canonical.items()}
        ctx.cache['relpath'] = {iid: rec['relpath'] for iid, rec in canonical.items()}
    return ctx.cache['record']


def _before_record(ctx):
    """The same record as it stood before the writer: the changed cards read from ``before``."""
    if 'record_before' not in ctx.cache:
        metas = dict(_record(ctx))
        for path in _card_paths(ctx.staged):
            _at, after = _after(ctx, path)
            if after and after.get('id') in metas:
                del metas[after['id']]
            _bt, before = _before(ctx, path)
            if before and before.get('id'):
                metas[before['id']] = before
        ctx.cache['record_before'] = metas
    return ctx.cache['record_before']


def _state(meta):
    return _machine(meta).get('state', 'New')


def _active_writes(metas):
    return {iid: [str(w) for w in (m.get('writes') or ())] for iid, m in metas.items()
            if m.get('type') == 'task' and not m.get('removed') and _state(m) == 'Active'
            and m.get('writes')}


def _intersecting(metas):
    from asf.record.core import writes_intersect
    tasks = sorted(_active_writes(metas).items())
    pairs = set()
    for i, (a, wa) in enumerate(tasks):
        for b, wb in tasks[i + 1:]:
            if any(writes_intersect(x, y) for x in wa for y in wb):
                pairs.add((a, b))
    return pairs


def check_i3(ctx):
    """I3 — never write intersecting ``writes:``: no two Active Tasks' ``writes:`` intersect by
    ``asf check``'s own test (``core.writes_intersect``). A pair this writer's change created is
    refused at the card(s) of the pair it wrote — the widen pass, ``asf set`` and the plan minter
    are refused before their commit, not after it."""
    changed_ids = {}
    for path in _card_paths(ctx.staged):
        _at, after = _after(ctx, path)
        if after and after.get('id'):
            changed_ids[after['id']] = path
    if not changed_ids or not any(m.get('type') == 'task' for p in changed_ids.values()
                                  for m in [_after(ctx, p)[1]]):
        return []
    new = _intersecting(_record(ctx)) - _intersecting(_before_record(ctx))
    out = []
    for a, b in sorted(new):
        for iid, other in ((a, b), (b, a)):
            if iid in changed_ids:
                out.append(_finding('I3', iid, f"writes: intersects Active task {other}'s writes:",
                                    changed_ids[iid]))
    return out


def _unclosed_tasks(metas, fid):
    return sorted(iid for iid, m in metas.items() if m.get('type') == 'task'
                  and m.get('parent') == fid and not m.get('removed') and _state(m) != 'Closed')


def _landed(meta):
    m = _machine(meta)
    return m.get('state') in ('Resolved', 'Closed') or m.get('stage') in ('landed', 'on-prod')


def check_i10(ctx):
    """I10 — a Feature is Resolved (or landed) only when every Task under it is Closed; so a
    merged docs PR, which closes no Task, never lands a Feature that has Tasks."""
    out = []
    for path in _card_paths(ctx.staged):
        _at, after = _after(ctx, path)
        if not after or after.get('type') != 'feature' or not _landed(after):
            continue
        fid = after.get('id')
        open_now = _unclosed_tasks(_record(ctx), fid)
        if not open_now:
            continue
        _bt, before = _before(ctx, path)
        if before and _landed(before) and _unclosed_tasks(_before_record(ctx), fid):
            continue  # already so before this writer
        out.append(_finding('I10', fid, f"{_state(after)} with open Task(s) {', '.join(open_now)}",
                            path))
    return out


DERIVED_HEADINGS = ('## Children', '## Backlinks')


def _derived_text(text):
    from asf.record.core import parse_sections
    _pre, sections = parse_sections(text or '')
    return '\n'.join(c for h, c in sections if h.strip() in DERIVED_HEADINGS)


def check_i11(ctx):
    """I11 — derived text (Backlinks, Children) never carries a token the redaction gate refuses
    (``redact.scan_text`` with the operator's patterns): the titles it quotes are scrubbed."""
    from asf import redact
    pats = ctx.cache.get('redact')
    if pats is None:
        pats = ctx.cache['redact'] = redact.default_patterns(ctx.root)
    if not pats:
        return []
    out = []
    for path in _card_paths(ctx.staged):
        text, after = _after(ctx, path)
        if not after:
            continue
        hits = redact.scan_text(path, _derived_text(text), pats)
        if not hits:
            continue
        before_text, _b = _before(ctx, path)
        if before_text is not None and redact.scan_text(path, _derived_text(before_text), pats):
            continue
        out.append(_finding('I11', after.get('id') or path,
                            f'derived Children/Backlinks carry a {hits[0].kind} ({hits[0].source})',
                            path))
    return out


def _staged_only(check):
    """A record check reads a writer's staged change; any other context has none to judge."""
    def run_it(ctx):
        if getattr(ctx, 'staged', None) is None or not getattr(ctx, 'root', None):
            return []
        return check(ctx)
    run_it.__name__ = check.__name__
    run_it.__doc__ = check.__doc__
    return run_it


INVARIANTS.extend([
    Invariant('I1', 'record', _staged_only(check_i1)),
    Invariant('I2', 'record', _staged_only(check_i2)),
    Invariant('I3', 'record', _staged_only(check_i3)),
    Invariant('I10', 'record', _staged_only(check_i10)),
    Invariant('I11', 'record', _staged_only(check_i11)),
])
