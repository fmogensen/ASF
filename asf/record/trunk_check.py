"""asf.record.trunk_check — is this Task's work already on the trunk? (F-0106)

Three coder sessions on 2026-09-23 ended `empty branch: nothing to land` because a plan cut Tasks
whose work was already on `main`: T-0083 duplicated T-0076 (landed at dff2286), and T-0084 named a
change — EMPTY_CAP, empty_ends, park_text, EmptyEndsTests — that was on main at 989b775. Nothing
between the plan landing and the coder's seat asked.

The predicate is the tests the Task's own section names (C2): a plan copies the spec's fenced
acceptance into every Task, so a Task whose named tests are *already* on the trunk is a Task
asking for work that is there. Timid by construction — every uncertain answer is None:

  * no test id named           -> None (nothing to check is not evidence)
  * a test id not on the trunk -> None (the Task has work to do)
  * a `writes:` path the trunk tree does not carry -> None (the Task creates a file)

A bare file path is not a test id (C3): every Task writes a file that already exists; only a class
or function name is falsifiable.

Two callers: `plan_tasks` (the mint) and `tick.step_wave.trunk_preflight` (the seat). They must
agree, or the factory refuses to launch a Task the minter was happy to mint — which is why the
predicate is one function and not two.
"""
import re

#: ``tests.test_empty_ends.EmptyEndsTests`` / ``…EmptyEndsTests.test_cap`` — the unittest form the
#: product's own acceptance blocks use (`python3 -m unittest -v tests.test_x.Klass`).
DOTTED_RE = re.compile(r'\btests\.(test_[a-z0-9_]+)((?:\.[A-Za-z_][A-Za-z0-9_]*)+)')
#: ``tests/test_empty_ends.py::EmptyEndsTests[::test_cap]`` — the pytest form.
PATH_RE = re.compile(r'\b(tests/[\w/]*test_[a-z0-9_]+\.py)((?:::[A-Za-z_][A-Za-z0-9_]*)+)')
#: a definition of ``node`` in a python blob: the falsifiable half of a test id (C3)
DEF_RE = '^[ \t]*(?:class|def)[ \t]+%s\\b'
GLOB = set('*?[')
CAP = 8   #: at most this many test ids are read — the cost ceiling on one Task's question


def test_ids(text):
    """``[(path, node), ...]`` — every test id ``text`` names, in first-seen order, at most
    :data:`CAP`. ``node`` is the last component: a class, or a method. A bare ``tests/x.py`` with
    no ``::node`` yields nothing (C3)."""
    out = []
    for m in DOTTED_RE.finditer(text or ''):
        out.append((f'tests/{m.group(1)}.py', m.group(2).strip('.').split('.')[-1]))
    for m in PATH_RE.finditer(text or ''):
        out.append((m.group(1), m.group(2).strip(':').split('::')[-1]))
    seen, uniq = set(), []
    for pair in out:
        if pair not in seen:
            seen.add(pair)
            uniq.append(pair)
    return uniq[:CAP]


def satisfied_on_trunk(product, text, writes, read_ref=None, sh=None):
    """``(sha, subject, why)`` when the trunk already carries every test ``text`` names and every
    concrete path ``writes`` declares — else None. Never guesses: an unreadable ref is None.

    ``read_ref``/``sh`` are injected for the tests; by default they are
    :func:`asf.evidence.evidence.read_ref` and :func:`~asf.evidence.evidence.sh`, both of which
    batch and cache per product (P13)."""
    from asf.evidence import evidence
    read_ref = read_ref or (lambda ref: evidence.read_ref(ref, product=product))
    sh = sh or (lambda cmd: evidence.sh(cmd, product=product))
    main = product.main
    ids = test_ids(text)
    if not ids:
        return None
    blobs = {}
    for path, node in ids:
        if path not in blobs:
            blobs[path] = read_ref(f'origin/{main}:{path}')
        blob = blobs[path]
        if not blob or not re.search(DEF_RE % re.escape(node), blob, re.M):
            return None
    for w in writes or ():
        if not (GLOB & set(w)) and read_ref(f'origin/{main}:{w}') is None:
            return None
    sha, subject = _completing_commit(sh, main, ids)
    if not sha:
        return None
    named = ', '.join(f'{p}::{n}' for p, n in ids[:3]) + (' …' if len(ids) > 3 else '')
    return sha, subject, f'origin/{main} already carries {named}'


def _completing_commit(sh, main, ids):
    """``(sha, subject)`` of the newest commit on ``origin/<main>`` that introduced one of the
    named test ids — the commit that completed the surface the Task asks for. ``('', '')`` when
    git cannot answer, which :func:`satisfied_on_trunk` reads as "do not close"."""
    best = (0, '', '')
    for path, node in ids:
        # node/path are both bound by the two module regexes above (`[A-Za-z_][A-Za-z0-9_]*` /
        # `tests/[\w/]*test_[a-z0-9_]+\.py`), so neither can carry a shell metacharacter — this
        # interpolation into `sh`'s shell string is safe only because those regexes stay narrow.
        out = sh(f'git log -1 --format=%H%x09%ct%x09%s -S{node} origin/{main} -- {path}')
        parts = (out or '').strip().split('\t', 2)
        if len(parts) == 3 and parts[1].isdigit() and int(parts[1]) > best[0]:
            best = (int(parts[1]), parts[0], parts[2])
    return best[1], best[2]


def card_body(root, card):
    """The card's own file text, opened directly — never through ``load_items`` (PD5): this runs
    once per launching Task row, per tick, and the record is large. ``''`` when the file cannot be
    read, which :func:`satisfied_on_trunk` reads as no test id named."""
    import os
    from asf.record.core import TYPES
    folder = TYPES[card.get('type') or 'task'][0]
    path = os.path.join(root, folder, f"{card['id']}.md")
    try:
        with open(path, encoding='utf-8') as f:
            return f.read()
    except OSError:
        return ''


def close_by_trunk(root, iid, sha, out=print, product=None):
    """Write the trunk's answer onto the card: typed ``landed: <sha>``, and nothing else (C12).
    ``state``, ``evidence:`` and the ``ingest:`` History line are the ingest's keys; the next
    ingest derives ``Closed`` from this sha by ``closing``'s ``reconciled`` rule. Returns True
    when the card now carries it. Idempotent: a card already carrying the sha is not rewritten.

    ``product`` is passed down to :func:`~asf.record.setfield.set_typed`'s own stage check (I3);
    this call is not itself wrapped in a second ``stage.guarded`` — ``set_typed`` already runs its
    write through the stage (PD4), and a second wrapper would walk the whole record twice."""
    from asf.record.core import canonicalize, load_items
    from asf.record.setfield import set_typed
    by_id, _errors = load_items(root)
    canonical, _dupes = canonicalize(by_id)
    rec = canonical.get(iid)
    if rec is None:
        return False
    if (rec['meta'].get('landed') or '') == sha:
        return True
    err = set_typed(rec, {'landed': sha}, writer='trunk-check', product=product)
    if err:
        out(f'trunk-check: {iid}: {err}')
        return False
    return True
