#!/usr/bin/env python3
"""evidence.py — the evidence pass's raw material.

`discover()` is one call that gathers everything the ingest pass needs to compute the
machine block of every item: what specs/plans/tasks exist in the product repo, what PRs and CI
runs say about them, and where the parity matrix stands. It also carries a handful of pure
functions that turn that evidence into the states README.md's "State — typed intent, derived
state" table defines, so they can be unit-tested without touching git or gh.

Most of the object-store plumbing below (`_batch`, `resolve`, `read_blobs`, `parse_tree`,
`read_trees`, `remote_branches`, `pr_list`, `plan_tasks`, `consumes_edges`, their regexes and
directory constants, and the `sh` helper) is lifted from the first product's pre-asf board,
parity and tick-table scripts, which already solve "discover everything the board needs" in two
`git cat-file --batch` passes over the product repo. `parse_rows` (the parity-matrix table
parser) is lifted from the same. Reviews are read through the one review reader,
:mod:`asf.evidence.review`. Every git read, host read (pull requests, workflow runs) and deploy
read goes through the three providers of :mod:`asf.evidence.sources` — `discover(sources=None)`
resolves them once, through `for_product`, and threads the same `sources` to every helper below.
The prod/dev deploy-sha lookups follow the same shape: the newest successful run of
`conventions.deploy_workflow` for `prod_sha`, the newest run of `conventions.ci_workflow` on the
trunk whose `conventions.ci_dev_job` succeeded for `dev_sha` — both `None` when the product names
no such workflow.

**Landing is the merge fact.** An item whose lane branch merged is landed by the run line that
says so — ``harvested: <sha>`` or a ``lane`` record in state ``MERGED`` in the product's
``sessions.jsonl`` (:func:`merge_facts`) — never by a commit subject or by the branch head's
ancestry, so a squash merge (whose sha no branch commit is an ancestor of) lands its item. A
document lane's merge (a spec or plan branch) lands its document, never its item (I10).

Read-only against the product repo (see the resolved `Product.repo_dir`): never commit, checkout
or fetch anything there but `git fetch --prune origin`. Python 3 stdlib only.
"""
import argparse
import base64
import json
import os
import re
import shlex
import subprocess
import sys
import time

from asf import env, proves, reviews
from asf.conventions import Conventions
from asf.evidence import review
from asf.evidence import review_store
from asf.evidence import sources as sources_mod
from asf.evidence.sources import GH_ACTIONS  # noqa: F401 (re-exported, PD15)

# An evidence-file path, not per-product config — no obvious Product field for it.
# TODO(config): no Product field for this yet.
CHECKED_FILE = os.path.join(env.ASF_HOME, "checked.txt")
PR_TTL = 180
EVIDENCE_TTL = 180


def _default_sources(product, need_host=False):
    """The `sources` a function builds for itself when none is threaded down to it — every
    caller outside :func:`discover` and the real recorder in `main()`, both of which need the
    full dispatch. Never resolves `sources.deploy` (`for_product`'s only reader of
    `product.deploy_sha`): nothing below `discover()` reads a deployed sha, and a minimal test
    double that carries no `deploy_sha` must not crash resolving `sources.git` for a plain
    `ancestry` or `main_commits` call."""
    git = sources_mod.LocalGit(product)
    host = (sources_mod.NoHost() if not need_host or ci_provider(product) is None
            else sources_mod.GitHubHost(product))
    return sources_mod.Sources(git, host, sources_mod.NoDeploy())


def branch_prefixes(product=None):
    """{code, fix, spec, plan: str, legacy: [str]} — the product's branch conventions, read
    straight off `Conventions` (its own default when there is no product)."""
    conv = product.conventions if product is not None else Conventions()
    return {
        "code": conv.prefix("code"), "fix": conv.prefix("fix"),
        "spec": conv.prefix("spec"), "plan": conv.prefix("plan"),
        "legacy": list(conv.legacy_prefixes()),
    }


def branch_token(prefixes=None):
    """The regex that finds a task branch named in a plan: the code prefix or a legacy one."""
    prefixes = prefixes or branch_prefixes()
    alts = sorted({prefixes["code"], *prefixes.get("legacy", [])}, key=len, reverse=True)
    return re.compile(r"(?:" + "|".join(re.escape(a) for a in alts) + r")([A-Za-z0-9][\w.]*(?:-[\w.]+)*)")


def _cache_file(name, product):
    """One cache file per product, under the product's own state directory (``ASF_HOME``): two
    products — or two operator homes naming the same product, the suite's and the live one —
    must never read each other's evidence (F-0087, the hermetic rule)."""
    product_name = getattr(product, "name", None)
    if not product_name:
        raise ValueError("_cache_file needs a product")
    return os.path.join(env.state_dir(product), "cache-" + name)


def sh(cmd, timeout=120, product=None, sources=None):
    """`cmd` through the git provider: split with `shlex.split` and run as `git <args>` via
    `sources.git.run`. A `git ...` string is the whole contract; the one caller whose command is
    not that shape (`full_shas`'s `printf | git cat-file`) falls back to a plain shell, never
    through the provider."""
    product = product or env.load_product()
    sources = sources or _default_sources(product)
    parts = shlex.split(cmd)
    if parts and parts[0] == "git":
        return sources.git.run(parts[1:], timeout=timeout)
    try:
        r = subprocess.run(cmd, shell=True, cwd=product.repo_dir, capture_output=True, text=True,
                           timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


# ---- object store: the git provider's batch reads, by position ------------------------------
def _batch(kind, requests, product=None, sources=None):
    """One `cat-file --batch[-check]` round-trip for the whole request list, through the git
    provider (:meth:`asf.evidence.sources.GitSource.cat_file`)."""
    if not requests:
        return []
    sources = sources or _default_sources(product or env.load_product())
    return sources.git.cat_file(kind, requests)


def resolve(paths, product=None, sources=None):
    """rev:path → oid, or None. One process for the whole list."""
    return dict(zip(paths, _batch("check", paths, product=product, sources=sources)))


def read_blobs(shas, product=None, sources=None):
    """oid → bytes. Deduplicated: several branches share the same review blob."""
    uniq = sorted({s for s in shas if s})
    return dict(zip(uniq, _batch("blob", uniq, product=product, sources=sources)))


def parse_tree(data):
    """A raw git tree object → {name: oid}. The dir listings here are flat, so no recursion."""
    out, i = {}, 0
    if not data:
        return out
    while i < len(data):
        sp = data.index(b" ", i)
        nul = data.index(b"\0", sp)
        name = data[sp + 1:nul].decode("utf-8", "replace")
        out[name] = data[nul + 1:nul + 21].hex()
        i = nul + 21
    return out


def read_trees(paths, product=None, sources=None):
    """rev:dir → {name: oid}. Distinct tree oids are fetched once each."""
    oids = resolve(paths, product=product, sources=sources)
    blobs = read_blobs(oids.values(), product=product, sources=sources)
    return {p: parse_tree(blobs.get(o)) for p, o in oids.items()}


def read_refs(refs, product=None, sources=None):
    """["rev:path", ...] → {ref: text|None}, one batch round-trip. The general-purpose read
    `backlog.py migrate` uses for spec/plan/report content — every git access it needs stays
    behind this module rather than migrate.py shelling out on its own.
    """
    oids = resolve(refs, product=product, sources=sources)
    blobs = read_blobs(oids.values(), product=product, sources=sources)
    out = {}
    for ref, oid in oids.items():
        blob = blobs.get(oid) if oid else None
        out[ref] = blob.decode("utf-8", "replace") if blob else None
    return out


def read_ref(ref, product=None, sources=None):
    """"rev:path" → text, or None if missing."""
    return read_refs([ref], product=product, sources=sources)[ref]


def list_tree(rev, path, product=None, sources=None):
    """rev:path → {name: oid}, the directory listing at that revision."""
    return read_trees([f"{rev}:{path}"], product=product, sources=sources)[f"{rev}:{path}"]


def doc_carriers(path, branches, product, sources=None):
    """Where a typed document path lives: ``(on_trunk, [remote branches carrying it])``, one
    ``cat-file`` process for the lot. A migrated card's ``links.spec`` can name a spec that sits
    on a pre-lane branch with no PR — the lane's own discovery never looks there, and a Feature
    whose spec is nowhere on the trunk must not read as approved."""
    if not path or product is None:
        return False, []
    main_ref = f"origin/{product.main}"
    others = sorted(b for b in branches or () if b != product.main)
    refs = [f"{main_ref}:{path}"] + [f"origin/{b}:{path}" for b in others]
    found = resolve(refs, product=product, sources=sources)
    return bool(found.get(refs[0])), [b for b in others if found.get(f"origin/{b}:{path}")]


# ---- inputs ---------------------------------------------------------------------------------
def remote_branches(product=None, sources=None):
    return set(remote_heads(product=product, sources=sources))


def remote_heads(product=None, sources=None):
    """``{branch: sha}`` of every branch on origin (one fetch, one ``ls-remote``)."""
    product = product or env.load_product()
    sources = sources or _default_sources(product)
    sources.git.fetch()
    out = sources.git.run(["ls-remote", "--heads", "origin"], timeout=120)
    heads = {}
    for ln in out.splitlines():
        sha, _, ref = ln.partition("\t")
        if ref.startswith("refs/heads/"):
            heads[ref[len("refs/heads/"):]] = sha.strip()
    return heads


#: The tag a closed-unmerged PR's head is kept under when the PR is closed without landing
#: (``archive/pr-<N>``) — the provenance a reset Task records.
ARCHIVE_PR_TAG = "archive/pr-{}"
_ARCHIVE_PR_RE = re.compile(r"^refs/tags/archive/pr-(\d+)(\^\{\})?$")


def archive_tags(product=None, sources=None):
    """``{pr number: sha}`` of every ``archive/pr-<N>`` tag on origin (an annotated tag read as
    the commit it points at)."""
    product = product or env.load_product()
    out = sh("git ls-remote --tags origin 'refs/tags/archive/pr-*'", timeout=120, product=product,
             sources=sources)
    tags = {}
    for ln in out.splitlines():
        sha, _, ref = ln.partition("\t")
        m = _ARCHIVE_PR_RE.match(ref.strip())
        if m and (m.group(2) or int(m.group(1)) not in tags):
            tags[int(m.group(1))] = sha.strip()
    return tags


#: Branches a reset never touches: an operator's hotfix, and the factory's own archives.
RESET_EXEMPT = ("hotfix/", "archive/")


def dead_branches(heads, prs, archive=None, prefixes=None, committed=None):
    """``{branch: {pr, head, archive}}`` — the lane branches whose work was closed without
    landing: the newest PR on the branch is CLOSED unmerged, no PR on it is open or merged,
    and the branch holds nothing newer than the close — it sits at that PR's head or at its
    ``archive/pr-<N>`` tag, or its head was committed at or before ``closedAt`` (a session's
    push that arrived after the close). A branch the list shows no PR for is dead when an
    ``archive/pr-<N>`` tag sits at its head (the closer's own record, for a PR older than the
    list). Such a branch is no evidence of work in flight: its item restarts from its
    spec/plan. A branch committed past the close carries new work and is alive; ``hotfix/*``
    and ``archive/*`` are never dead here. ``archive`` is the tag's name when one sits at the
    head (provenance), else ''. Pure: ``heads`` is :func:`remote_heads`, ``prs``
    :func:`pr_list`, ``archive`` :func:`archive_tags`, ``committed`` ``{sha: committer ISO
    time}`` of the heads it asks about (:func:`commit_times`)."""
    prefixes = prefixes or branch_prefixes()
    lanes = tuple(v for k in ("code", "fix", "spec", "plan") for v in [prefixes.get(k)] if v)
    archive = archive or {}
    committed = committed or {}
    tag_at = {}
    for n, sha in archive.items():
        tag_at.setdefault(sha, max(n, tag_at.get(sha, 0)))
    by_head = {}
    for p in prs or ():
        by_head.setdefault(p.get("headRefName") or "", []).append(p)
    out = {}
    for b, sha in sorted((heads or {}).items()):
        if b.startswith(RESET_EXEMPT) or not lanes or not b.startswith(lanes) or not sha:
            continue
        on_b = by_head.get(b) or []
        if not on_b:
            n = tag_at.get(sha)
            if n:
                out[b] = {"pr": n, "head": sha, "archive": ARCHIVE_PR_TAG.format(n)}
            continue
        if any(p.get("state") in ("OPEN", "MERGED") for p in on_b):
            continue
        last = max(on_b, key=lambda p: p.get("number") or 0)
        n = last.get("number")
        if last.get("state") != "CLOSED" or not n:
            continue
        tag = archive.get(n)
        closed_at = _utc(last.get("closedAt"))
        stale = bool(closed_at) and bool(_utc(committed.get(sha))) \
            and _utc(committed.get(sha)) <= closed_at
        if sha not in (last.get("headRefOid"), tag) and not stale:
            continue  # committed past the close: new work, alive
        out[b] = {"pr": n, "head": sha, "archive": ARCHIVE_PR_TAG.format(n) if tag == sha else ""}
    return out


def _utc(stamp):
    """An ISO time as a comparable UTC string (``YYYY-MM-DDTHH:MM:SS``), '' when unreadable."""
    import datetime
    try:
        t = datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return t.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def closed_moved_heads(heads, prs, archive=None, prefixes=None):
    """The heads :func:`dead_branches` needs a commit time for: lane branches whose newest PR
    is closed unmerged and whose head is neither that PR's head nor its archive tag."""
    prefixes = prefixes or branch_prefixes()
    lanes = tuple(v for k in ("code", "fix", "spec", "plan") for v in [prefixes.get(k)] if v)
    last = {}
    for p in prs or ():
        b = p.get("headRefName") or ""
        if b in (heads or {}) and (p.get("number") or 0) > (last.get(b) or {}).get("number", 0):
            last[b] = p
    return sorted({heads[b] for b, p in last.items()
                   if lanes and b.startswith(lanes) and p.get("state") == "CLOSED"
                   and heads[b] not in (p.get("headRefOid"), (archive or {}).get(p.get("number")))})


def commit_times(shas, product=None, sources=None):
    """``{sha: committer ISO time}`` for the commits the local clone holds (one ``git show``)."""
    if not shas:
        return {}
    out = sh("git show -s --format=%H%x09%cI " + " ".join(shas), product=product, sources=sources)
    times = {}
    for ln in (out or "").splitlines():
        sha, _, at = ln.partition("\t")
        if sha and at:
            times[sha.strip()] = at.strip()
    return times


def resets_of(dead, live=()):
    """``{item id: {branch, pr, head, archive}}`` — the items :func:`dead_branches` names, by
    the id tokens in each dead branch's name (the newest PR wins when two name one item). An
    item a ``live`` branch still names (its plan PR closed, its code branch at work) is not
    reset: its work is in flight elsewhere."""
    alive = {iid for b in live or () for iid in id_tokens(b, BRANCH_ID_TOKEN)}
    out = {}
    for b, d in sorted((dead or {}).items(), key=lambda kv: kv[1].get("pr") or 0):
        for iid in id_tokens(b, BRANCH_ID_TOKEN):
            if iid not in alive:
                out[iid] = dict(d, branch=b)
    return out


def pr_list(product=None, sources=None):
    """Every pull request, through the host provider (:meth:`HostSource.prs`) — `GitHubHost`
    keeps its own cache file under the same path this used to manage directly."""
    product = product or env.load_product()
    sources = sources or _default_sources(product, need_host=True)
    return sources.host.prs()


def _gh_json(args, timeout=60, product=None):
    """No caller left in this module (D3 — every host read now goes through `sources.host`).
    Kept only so a test fixture outside this Task's footprint
    (`tests/test_doc_lane_landing.py`'s `Product.discover()`, and `tests/test_review_reader.py`
    which imports it) can still `mock.patch.object(evidence, "_gh_json", …)` without an
    `AttributeError` (needs writes: tests/test_doc_lane_landing.py, to drop the mock)."""
    return [] if not args.startswith("api ") else {}


# ---- slugs, verdicts, rounds ----------------------------------------------------------------
DATE_PREFIX = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-")
H1_ALIAS = re.compile(r"^#\s+([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*)\s*[—–-]")


def doc_slug(name):
    """2026-09-20-clean-floor.md → clean-floor; preflight files are not documents."""
    base = name[:-3] if name.endswith(".md") else name
    m = DATE_PREFIX.match(base)
    return base[m.end():] if m else base


#: The verdict strings the evidence carries (``spec_review``/``plan_review``/a Task's
#: ``review``), from the one reader's verdicts (:mod:`asf.evidence.review`).
VERDICT_WORDS = {review.APPROVED: "APPROVED", review.CHANGES: "CHANGES REQUESTED"}


def verdict_of(blob, legacy=True, required=()):
    """A review's verdict as the evidence spells it (``APPROVED`` / ``CHANGES REQUESTED`` / ``""``),
    read by the one reader: :func:`asf.evidence.review.verdict_of` — the check table first, its
    own ``verdict:`` line for a file with no table — and, for a legacy ``<slug>-review-r<n>.md``
    file, its first verdict word."""
    if not blob:
        return ""
    v = review.legacy_verdict_of(blob) if legacy else review.verdict_of(blob, required)
    return VERDICT_WORDS.get(v, "")


#: The key a review filed in the review store (:mod:`asf.evidence.review_store`) is read under,
#: beside the blob shas of the branch's own review files.
STORED_KEY = "store:"


def stored_name(conv, reviews_dir, slug, n):
    """The name (under ``reviews_dir``) the convention gives round ``n`` of ``slug`` — what a
    stored review is reported as, the name it would carry on the branch."""
    path = conv.review_path(str(slug).lower(), n)
    rdir = str(reviews_dir).strip("/") + "/"
    return path[len(rdir):] if path.startswith(rdir) else path


def pick_review(conv, reviews_dir, names, slugs, legacy_prefixes, trunk=None):
    """``(file name, round, legacy)`` of the newest review among ``names`` (a reviews-dir tree:
    ``{name: blob sha}``) for any of ``slugs`` — the one contract, ``conventions.review_pattern``
    (:func:`asf.evidence.review.pick`), with the legacy ``<prefix>-review-r<n>.md`` form as the
    fallback — or ``(None, -1, False)``. A file the trunk holds with the same blob (``trunk``:
    the trunk's reviews tree) is an earlier document's review carried along, not this one's."""
    rdir = str(reviews_dir).strip("/")
    paths = [f"{rdir}/{n}" for n, sha in (names or {}).items()
             if not (trunk and trunk.get(n) == sha)]
    best = None
    for slug in dict.fromkeys(s for s in slugs if s):
        hit = review.pick(conv, paths, slug, legacy_prefixes)
        if hit and (best is None or (hit[0], not hit[2]) > (best[0], not best[2])):
            best = hit
    if best is None:
        return None, -1, False
    n, path, legacy = best
    return path[len(rdir) + 1:], n, legacy


#: A plan's Task heading. The shapes plans really use: `### Task 1:`, `### Task T1:`, `### T1:`,
#: `### T1 —`, `## Task T1 -`, with or without `**` bold, any case. `rest` is what follows the id.
TASK_HEAD = re.compile(r"^#{2,4}[ \t]+\**[ \t]*(?:Task[ \t]+T?|T(?=\d))(\d+[a-z]?)\b(?P<rest>[^\n]*)",
                       re.MULTILINE | re.IGNORECASE)
#: A heading that looks like a Task heading — the doctor/minter flag one a plan has when
#: TASK_HEAD matches none, so a format drift cannot silently mint nothing.
TASK_LIKE_HEAD = re.compile(
    r"^#{1,6}[ \t]+[*_ \t]*(?:Task[ \t]*[#:.\-]*[ \t]*[A-Za-z]{0,2}|T[#:.\-]*)\d+[^\n]*$",
    re.MULTILINE | re.IGNORECASE)
BRANCH_STEM = re.compile(r"^(.*?)-(?:t\d+[a-z]?|w\d+[a-z]?)$", re.IGNORECASE)
BRANCH_TOKEN = branch_token()  # the default prefixes; discover() builds the product's own
TASK_TOKEN = re.compile(r"\bT(\d+[a-z]?)\b")
CONSUMES_LINE = re.compile(r"consumes", re.IGNORECASE)

GOAL_HEAD = re.compile(r"^GOAL\s+(\d+)\s*[—–-]+\s*([^\n]*)", re.MULTILINE)


def natural_key(tid):
    m = re.match(r"T(\d+)([a-z]*)", tid)
    return (int(m.group(1)), m.group(2)) if m else (10 ** 6, tid)


def plan_tasks(text, slug, token=None):
    """[(Tn, [branch-id …], declared_done)] — the plan's task headings.

    The dispatch table is the authority on branch names (FACT-6's tasks live on `<code>fact6-t*`,
    not `<code>clean-floor-t*`), and where a plan names branches per wave rather than per task
    (`<legacy>rename-<task>`) the common stem of the names it does give carries the same
    answer. `<slug>-t<n>` is the fallback when it names neither. `token` is `branch_token()` of
    the product's prefixes (the default prefixes when omitted).
    """
    token = token or BRANCH_TOKEN
    ids, seen, declared = [], set(), set()
    for m in TASK_HEAD.finditer(text):
        tid = "T" + m.group(1).lower()
        if tid not in seen:
            seen.add(tid)
            ids.append(tid)
        if re.search(r"\bDONE\b", m.group("rest")):
            declared.add(tid)
    stated = {}
    for line in text.splitlines():
        found = token.findall(line)
        tasks = {"T" + t.lower() for t in TASK_TOKEN.findall(line)}
        if len(tasks) != 1:
            continue
        tid = next(iter(tasks))
        for b in found:
            if b.lower().endswith(tid.lower()):
                stated.setdefault(tid, b)
    # the stem is read out of the dispatch table's own rows only. Branch names in prose are
    # dependencies, not ownership: MOBILE-1's plan names `<code>brand-t7` nine times as a
    # precondition, and a stem taken from prose handed BRAND-1's whole task list to MOBILE-1.
    stems = {}
    for b in stated.values():
        sm = BRANCH_STEM.match(b)
        if sm and sm.group(1) and sm.group(1) != slug:
            stems[sm.group(1)] = stems.get(sm.group(1), 0) + 1
    stem = max(stems, key=lambda k: (stems[k], -len(k))) if stems else None
    out = []
    for t in sorted(ids, key=natural_key):
        cand = [c for c in (stated.get(t), f"{stem}-{t.lower()}" if stem else None,
                            f"{slug}-{t.lower()}") if c]
        out.append((t, list(dict.fromkeys(cand)), t in declared))
    return out


def consumes_edges(text):
    """producer → {consumers}, for the plan lines that use the word `consumes`."""
    edges = {}
    for line in text.splitlines():
        if not CONSUMES_LINE.search(line):
            continue
        head, _, tail = line.partition("consumes")
        prod = ["T" + t.lower() for t in TASK_TOKEN.findall(head)]
        cons = ["T" + t.lower() for t in TASK_TOKEN.findall(tail)]
        if not prod:
            continue
        for c in cons:
            if c != prod[-1]:
                edges.setdefault(prod[-1], set()).add(c)
    return edges


# ---- the parity matrix ------------------------------------------------------------------------
# Lifted from factory-parity.py.
def cells(line):
    """Split a markdown table row on unescaped `|`; a literal pipe is escaped `\\|` — the one
    splitter, owned by :func:`asf.reviews.cells`."""
    return reviews.cells(line)


VALID_STATUS = {"done", "doing", "todo"}


def parse_rows(text):
    """(rows, broken) from the parity matrix (`conventions.matrix_path`) "All requirements" table."""
    rows = []
    for line in text.splitlines():
        if line.startswith("| F-"):
            c = cells(line)
            if len(c) < 10:
                continue
            rows.append(dict(id=c[0], area=c[1], cap=c[2], user=c[3], ms=c[4], status=c[5],
                             typed=c[6], impl=c[7], tests=c[8]))
    broken = [r for r in rows if r["status"] not in VALID_STATUS]
    for r in broken:
        r["status"], r["ms"] = "todo", "broken"  # a mangled row is not proven; count as not done
    return rows, broken


# ------------------------------------------------------------------------------ discover() ----
def _matrix_paths(spec_paths):
    """Split a `[a, b, ...]`-style impl/tests cell into individual file paths."""
    inner = spec_paths.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    return [p.strip().strip("`") for p in inner.split(",") if p.strip()]


def _newest_success(workflow, product=None, sources=None):
    """The newest successful run of `workflow`, or None."""
    sources = sources or _default_sources(product or env.load_product(), need_host=True)
    runs = sources.host.runs(workflow, limit=15)
    return next((r for r in runs if r.get("conclusion") == "success"), None)


def _newest_with_job(workflow, job, branch, product=None, sources=None):
    """The newest completed run of `workflow` on `branch` whose `job` succeeded, or None."""
    sources = sources or _default_sources(product or env.load_product(), need_host=True)
    runs = sources.host.runs(workflow, branch=branch, limit=40)
    for r in runs:
        if r.get("status") != "completed":
            continue
        jobs = sources.host.run_jobs(r["databaseId"])
        if any(j.get("name") == job and j.get("conclusion") == "success" for j in jobs):
            return r
    return None


def _prod_mode(product):
    """``deploy_sha.prod.mode`` as the deploy pass reads it (``auto`` | ``manual``)."""
    from asf.harvest import deploy  # the deploy pass owns the reading of its own config
    return deploy.mode(product, 'prod')


def discover(product=None, checked_file=None, sources=None):
    """The raw evidence `backlog.py ingest` needs, gathered fresh from the product repo and the
    three providers (:mod:`asf.evidence.sources`) — `sources` resolved once, here, and threaded
    to every helper below."""
    product = product or env.load_product()
    sources = sources or sources_mod.for_product(product)
    prefixes = branch_prefixes(product)
    code, spec_p, plan_p = prefixes["code"], prefixes["spec"], prefixes["plan"]
    token = branch_token(prefixes)
    main_ref = f"origin/{product.main}"
    conv = product.conventions
    plans_dir, specs_dir, reviews_dir = conv.plans_dir, conv.specs_dir, conv.reviews_dir
    # `briefs_dir`/`matrix_path` are not yet fields of `Conventions` (asf/conventions.py is out
    # of this Task's footprint) — `.get()` reads them from `extra` until they land there, and
    # will keep reading them once they do (asf/conventions.py:97-111).
    briefs_dir = conv.get("briefs_dir")
    matrix_path = conv.get("matrix_path")

    heads = remote_heads(product=product, sources=sources)
    prs = pr_list(product=product, sources=sources)
    # a lane branch whose PR was closed unmerged is not work in flight (its item resets to its
    # spec/plan); an `archive/*` branch is a kept copy, never work in flight either
    tags = archive_tags(product=product, sources=sources)
    moved = closed_moved_heads(heads, prs, tags, prefixes)
    dead = dead_branches(heads, prs, tags, prefixes,
                         commit_times(moved, product=product, sources=sources))
    branches = {b for b in heads if b not in dead and not b.startswith("archive/")}
    pr_by_head = {}
    for p in prs:
        pr_by_head.setdefault(p.get("headRefName") or "", []).append(p)
    merged = {p["number"]: p["mergeCommit"]["oid"]
              for p in prs if p.get("state") == "MERGED" and p.get("mergeCommit")}

    tree_paths = [f"{main_ref}:{plans_dir}", f"{main_ref}:{specs_dir}", f"{main_ref}:{reviews_dir}"]
    if briefs_dir:
        tree_paths.append(f"{main_ref}:{briefs_dir}")
    main_trees = read_trees(tree_paths, product=product, sources=sources)
    main_plans = main_trees[f"{main_ref}:{plans_dir}"]
    main_specs = main_trees[f"{main_ref}:{specs_dir}"]
    main_review_tree = dict(main_trees[f"{main_ref}:{reviews_dir}"])
    main_reviews = set(main_review_tree)
    main_briefs = main_trees.get(f"{main_ref}:{briefs_dir}", {}) if briefs_dir else {}

    # ---- discovery: spec/plan branches, plans and specs already on main
    inits = {}

    def init(slug):
        return inits.setdefault(slug, {"slug": slug, "spec_branch": None, "plan_branch": None,
                                       "plan_doc": None, "spec_doc": None})

    for b in branches:
        if b.startswith(spec_p):
            init(b[len(spec_p):])["spec_branch"] = b
        elif b.startswith(plan_p):
            init(b[len(plan_p):])["plan_branch"] = b
    for name in main_plans:
        if name.startswith("preflight-") or not name.endswith(".md"):
            continue
        init(doc_slug(name))["plan_doc"] = (f"{main_ref}:{plans_dir}/{name}", main_plans[name])
    for name in main_specs:
        if not name.endswith(".md"):
            continue
        slug = doc_slug(name)
        init(slug)["spec_doc"] = (f"{main_ref}:{specs_dir}/{name}", main_specs[name])

    # ---- trees: reviews/plans/specs on every spec/plan branch we found
    doc_branches = [it[k] for it in inits.values() for k in ("spec_branch", "plan_branch") if it[k]]
    tree_paths = [f"origin/{b}:{reviews_dir}" for b in doc_branches]
    tree_paths += [f"origin/{b}:{plans_dir}" for b in doc_branches]
    tree_paths += [f"origin/{b}:{specs_dir}" for b in doc_branches]
    trees = read_trees(tree_paths, product=product, sources=sources)

    plan_req, spec_req = {}, {}
    for it in inits.values():
        slug = it["slug"]
        if not it["plan_doc"]:
            for b in (it["plan_branch"], it["spec_branch"]):
                if not b:
                    continue
                t = trees.get(f"origin/{b}:{plans_dir}", {})
                hit = next((n for n in t if n.endswith(f"-{slug}.md") and not n.startswith("preflight-")), None)
                if hit:
                    it["plan_doc"] = (f"origin/{b}:{plans_dir}/{hit}", t[hit])
                    break
        if not it["spec_doc"]:
            b = it["spec_branch"] or it["plan_branch"]
            t = trees.get(f"origin/{b}:{specs_dir}", {}) if b else {}
            hit = next((n for n in t if n.endswith(f"-{slug}.md")), None)
            if hit:
                it["spec_doc"] = (f"origin/{b}:{specs_dir}/{hit}", t[hit])
        if it["plan_doc"]:
            plan_req[slug] = it["plan_doc"][1]
        if it["spec_doc"]:
            spec_req[slug] = it["spec_doc"][1]

    for it in inits.values():
        for kind in ("spec", "plan"):
            doc = it.get(f"{kind}_doc")
            rev = doc[0].split(":", 1)[0] if doc else None
            it[f"{kind}_on_main"] = rev == main_ref
            it[f"{kind}_carrier"] = (it[f"{kind}_branch"] or
                                     (rev[len("origin/"):] if rev and rev != main_ref else None))

    # ---- the review files each row needs a verdict from
    wanted = {}
    store = review_store.root(product)
    stored_blobs = {}
    for it in inits.values():
        slug = it["slug"]
        for kind in ("spec", "plan"):
            branch = it[f"{kind}_carrier"]
            if not branch:
                continue
            names = trees.get(f"origin/{branch}:{reviews_dir}", {})
            # the one reader's contract (`review_pattern`, the item's slug), the legacy
            # `<kind>-<slug>-review-r<n>.md` forms as the fallback; a review the trunk already
            # holds is the earlier document's (the spec's, carried on the plan branch)
            f, r, legacy = pick_review(conv, reviews_dir, names, [slug],
                                       (f"{kind}-{slug}", slug, f"{slug}-{kind}"),
                                       trunk=main_review_tree)
            # the review store first: a review filed off the branch it reviewed
            stored = review_store.newest_of(store, [slug], branch) if store else None
            if review_store.prefer(stored, r if f else None):
                key = STORED_KEY + stored["file"]
                stored_blobs[key] = stored["text"].encode("utf-8")
                wanted.setdefault(slug, {})[kind] = (
                    stored_name(conv, reviews_dir, slug, stored["round"]), stored["round"], key,
                    False)
            elif f:
                wanted.setdefault(slug, {})[kind] = (f, r, names[f], legacy)

    blobs = read_blobs(list(plan_req.values()) + list(spec_req.values()) +
                       [v[2] for d in wanted.values() for v in d.values()
                        if not str(v[2]).startswith(STORED_KEY)], product=product, sources=sources)
    blobs.update(stored_blobs)

    brief_dirs = {}
    for slug in inits:
        hit = [d for d in main_briefs if d == slug or d.endswith("-" + slug)]
        if hit:
            brief_dirs[slug] = f"{main_ref}:{briefs_dir}/{sorted(hit)[-1]}"
    brief_trees = read_trees(sorted(set(brief_dirs.values())), product=product, sources=sources)

    for it in inits.values():
        head = (blobs.get(plan_req.get(it["slug"])) or blobs.get(spec_req.get(it["slug"])) or b"")
        lines = head[:200].decode("utf-8", "replace").splitlines()
        m = H1_ALIAS.match(lines[0]) if lines else None
        it["alias"] = m.group(1) if m else None

    # ---- task branches: a plan's own dispatch table first, an alias stem for the rest
    claimed = set()
    stem_owners = {}
    for it in inits.values():
        st = re.sub(r"-\d+$", "", (it["alias"] or "").lower())
        if st:
            stem_owners.setdefault(st, []).append(it["slug"])
    for it in inits.values():
        slug = it["slug"]
        text = (blobs.get(plan_req.get(slug)) or b"").decode("utf-8", "replace")
        it["plan_text"] = text
        it["tasks_parsed"] = plan_tasks(text, slug, token) if text else []
        it["edges"] = consumes_edges(text) if text else {}
        for _tid, cand, _d in it["tasks_parsed"]:
            claimed.update(f"{code}{c}" for c in cand if f"{code}{c}" in branches)

    task_tree_paths = []
    task_rows = {}
    for it in inits.values():
        slug = it["slug"]
        alias_stem = re.sub(r"-\d+$", "", (it["alias"] or "").lower()) or None
        if alias_stem and len(stem_owners.get(alias_stem, [])) > 1:
            alias_stem = None
        briefs = brief_trees.get(brief_dirs.get(slug, ""), {})
        rows = []
        for tid, cand, declared_done in it["tasks_parsed"]:
            br = next((f"{code}{c}" for c in cand if f"{code}{c}" in branches), None)
            if not br and alias_stem and alias_stem != slug:
                weak = f"{alias_stem}-{tid.lower()}"
                if f"{code}{weak}" in branches and f"{code}{weak}" not in claimed:
                    br, cand = f"{code}{weak}", cand + [weak]
                    claimed.add(br)
            cand_prs = [p for c in cand for pre in [code] + prefixes["legacy"]
                        for p in pr_by_head.get(f"{pre}{c}", [])]
            merged_pr = next((p for p in cand_prs if p.get("state") == "MERGED"), None)
            n = tid[1:]
            landed = (declared_done
                      or f"task-{n}-report.md" in briefs
                      or any(f"{c}-writer-report.md" in main_reviews for c in cand)
                      or any(re.fullmatch(rf"{re.escape(c)}-review-r\d+\.md", r)
                             for c in cand for r in main_reviews))
            open_pr = next((p for p in cand_prs if p.get("state") != "CLOSED"), None)
            chosen_pr = merged_pr or open_pr
            rows.append({
                "id": tid, "cand": cand, "branch": br,
                "pr": chosen_pr.get("number") if chosen_pr else None,
                "pr_state": chosen_pr.get("state") if chosen_pr else None,
                "merged_sha": merged.get(chosen_pr.get("number")) if merged_pr else None,
                "landed_no_branch": landed and not br,
            })
            if br:
                task_tree_paths.append(f"origin/{br}:{reviews_dir}")
        task_rows[slug] = rows
    task_trees = read_trees(sorted(set(task_tree_paths)), product=product, sources=sources)

    task_blob_req = []
    for slug, rows in task_rows.items():
        for row in rows:
            if not row["branch"]:
                continue
            names = task_trees.get(f"origin/{row['branch']}:{reviews_dir}", {})
            ids = row["cand"] + [row["branch"].split("/", 1)[-1]]
            f, r, legacy = pick_review(conv, reviews_dir, names, ids,
                                       [p for i in ids for p in (i, f"hotfix-{i}", f"review-{i}")])
            stored = review_store.newest_of(store, ids, row["branch"]) if store else None
            if review_store.prefer(stored, r if f else None):
                key = STORED_KEY + stored["file"]
                stored_blobs[key] = stored["text"].encode("utf-8")
                row["review"] = (stored["round"], False,
                                 stored_name(conv, reviews_dir, ids[0], stored["round"]), key)
            else:
                row["review"] = (r, legacy, f, names[f]) if f else None
                if f:
                    task_blob_req.append(names[f])
    task_blobs = read_blobs(task_blob_req, product=product, sources=sources)
    task_blobs.update(stored_blobs)
    code_required = reviews.required("code")
    for rows in task_rows.values():
        for row in rows:
            rev = row.pop("review", None)
            if rev:
                r, legacy, f, key = rev
                blob = task_blobs.get(key)
                row["review"] = (r, verdict_of(blob, legacy=legacy, required=code_required))
                if blob:
                    text = blob[:review.READ_CHARS].decode("utf-8", "replace")
                    checks, _faults = reviews.parse(text)
                    row["checks"] = {"path": f, "round": r, "passed": reviews.passed(checks)}
                else:
                    row["checks"] = None
            else:
                row["review"] = None
                row["checks"] = None

    features = {}
    for it in inits.values():
        slug = it["slug"]

        def review_tuple(kind):
            hit = wanted.get(slug, {}).get(kind)
            if not hit:
                return None
            f, r, sha, legacy = hit
            return (r, verdict_of(blobs.get(sha), legacy=legacy, required=reviews.required(kind)), f)

        own_branches = {b for b in (it["spec_branch"], it["plan_branch"]) if b}
        seen_pr, pr_numbers = set(), []
        for row in task_rows.get(slug, []):
            if row["branch"]:
                own_branches.add(row["branch"])
        for b in sorted(own_branches):
            for p in pr_by_head.get(b, []):
                if p["number"] not in seen_pr:
                    seen_pr.add(p["number"])
                    pr_numbers.append(p["number"])

        features[slug] = {
            "alias": it["alias"],
            "spec": it["spec_doc"][0] if it["spec_doc"] else None,
            "spec_branch": it["spec_branch"],
            "spec_on_main": it["spec_on_main"],
            "spec_review": review_tuple("spec"),
            "plan": it["plan_doc"][0] if it["plan_doc"] else None,
            "plan_branch": it["plan_branch"],
            "plan_on_main": it["plan_on_main"],
            "plan_review": review_tuple("plan"),
            "tasks": {row["id"]: {"branch": row["branch"], "pr": row["pr"],
                                  "pr_state": row["pr_state"], "review": row["review"],
                                  "checks": row["checks"],
                                  "merged_sha": row["merged_sha"],
                                  "landed_no_branch": row["landed_no_branch"]}
                     for row in task_rows.get(slug, [])},
            "prs": pr_numbers,
        }

    # ---- the parity matrix, for Stories — only when the product names one (D1: None = not
    # looked for)
    if matrix_path:
        matrix_text = sh(f"git show {main_ref}:{matrix_path}", product=product, sources=sources)
        rows, _broken = parse_rows(matrix_text) if matrix_text else ([], [])
        stories = {r["id"]: {"status": r["status"], "impl": _matrix_paths(r["impl"]),
                             "test": _matrix_paths(r["tests"]), "area": r["area"],
                             "milestone": r["ms"], "cap": r["cap"]}
                  for r in rows}
    else:
        stories = {}

    # ---- deploy shas: one call each, through the deploy provider (`None` when the product
    # names no way to read it — PD14's dispatch, not "unconfigured" meaning "trunk is prod").
    deploy_workflow = conv.get("deploy_workflow")
    prod_sha = sources.deploy.sha("prod")
    dev_sha = sources.deploy.sha("dev")

    checked_file = checked_file if checked_file is not None else CHECKED_FILE
    checked = set()
    if os.path.exists(checked_file):
        with open(checked_file) as f:
            for line in f:
                m = re.match(r"\s*#?(\d+)", line)
                if m:
                    checked.add(int(m.group(1)))

    main_sha = sh(f"git rev-parse {main_ref}", product=product, sources=sources)
    commits = main_commits(product, sources=sources)
    merges = merge_facts(product, sources=sources)

    return {
        "features": features,
        "stories": stories,
        "prod_sha": prod_sha,
        # the product deploys prod (a None `prod_sha` then means unknown, never "trunk is prod"),
        # and who dispatches it: `auto` (ASF) needs no operator tick in checked.txt
        "prod_deploys": bool(deploy_workflow),
        "prod_mode": _prod_mode(product),
        "dev_sha": dev_sha,
        "checked": checked,
        "main_sha": main_sha or None,
        "merged": merged,
        "branches": sorted(branches),
        "resets": resets_of(dead, branches),
        "ids": id_evidence(product, branches, prs, commits=commits, merges=merges, sources=sources),
        "lane_docs": lane_docs(product, prs, commits=commits, merges=merges),
        "proves": landed_proves(product, prs, sources=sources),
        "ci": ci_provider(product),
        "reverts": trunk_reverts(product),
    }


# ---- id tokens: an item's own id in a branch name, a PR or a commit subject on main ----------
ID_TOKEN = re.compile(r"\b([EFSTBDR])-(\d{4})\b")
# branch names are often lower-cased (`fix/b-0003`); titles, bodies and subjects are not read so
BRANCH_ID_TOKEN = re.compile(r"\b([EFSTBDR])-(\d{4})\b", re.IGNORECASE)


#: A commit of the document lanes — a spec, a plan, a review, a ruling — names its item because
#: that is the lane's subject convention, not because the item's work landed (B-0059: the
#: landing of eight specs closed four Features).
DOC_LANE_SUBJECT = re.compile(r"^(spec|plan|review|adjudicate)\(|^docs\((spec|plan|review)\)")
#: A session's end marker — `asf: report <job>` (asf.workers.spawn.REPORT_SUBJECT), also after a
#: product's commit-msg hook stacked an item scope onto it (`asf(F-1131): report plan-f-1131`,
#: `plan(F-0116): asf(F-0116): report …`). It names the item its session ran for and lands none
#: of its work: a product's F-1131 closed as landed on its plan session's empty report commit,
#: with no plan on the trunk and not one Task built.
REPORT_SUBJECT = re.compile(r"^(?:[A-Za-z][\w-]*\([^)]*\)!?:\s*)*asf(?:\([^)]*\))?!?:\s*report\s")


def lands_nothing(subject):
    """True when a commit subject (or a PR title) names its item by a lane convention — a spec,
    plan, review or ruling commit, or a session's report commit — and so never by landing it."""
    s = (subject or "").strip()
    return bool(DOC_LANE_SUBJECT.match(s) or REPORT_SUBJECT.match(s))


def id_tokens(text, rx=ID_TOKEN):
    """Every `<TYPE>-<nnnn>` in text, in order, deduplicated; `(B-0001)` and `[B-0001]` count."""
    return list(dict.fromkeys(f"{t.upper()}-{n}" for t, n in rx.findall(text or "")))


#: A subject's item scope: `kind(<ids>)!:` — the ids a conventional scope carries.
_SCOPE_SUBJECT = re.compile(r"^[A-Za-z][\w-]*\((?P<scope>[^)]*)\)!?:")
#: A subject that leads with its item: `F-0047 — …`, `[B-0004] …`, `fix: T-0361 …`,
#: `asf(tick): T-0002 — …` (the code lane's squash under its PR title, and a hand-written lead).
_LEAD_SUBJECT = re.compile(r"^(?:[A-Za-z][\w-]*(?:\([^)]*\))?!?:\s*)?"
                           r"\[?(?P<ids>[EFSTBDR]-\d{4}(?:\s*[,/&]\s*[EFSTBDR]-\d{4})*)\]?(?![\w-])")
#: A merge of a branch into the trunk: the branch merged in, never the one merged into.
_MERGE_SUBJECT = (
    re.compile(r"^Merge pull request #\d+ from (?:[^/\s]+/)?(?P<branch>\S+)"),
    re.compile(r"^Merge (?:remote-tracking )?branch '(?P<branch>[^']+)'"),
    re.compile(r"^merge-queue: #\d+ \((?P<branch>[^\s@)]+)"),
)


def branch_ids(branch):
    """The ids a branch name carries as its item — `<prefix>direct-F-0112`, `<prefix>b-0003`:
    the item's own branch."""
    return id_tokens(branch, BRANCH_ID_TOKEN)


def naming_ids(subject, main=None):
    """The ids a commit subject (or a PR title) names as its item — the only way a commit names
    one (a product's F-0112: a PR body quoting `origin/<prefix>direct-F-0112` landed F-0112).

    - the conventional scope: `feat(F-0112): …`, `fix(T-0359, T-0360)!: …`, and scopes stacked
      one after another — `task(T-0448): task(T-0450): …`, one commit carrying two Tasks' work
      (a product's T-0450 landed that way; crediting only the first sent its lane round a loop
      of empty-branch corrections);
    - a lead id: `F-0113 — Parity … (#830)`, `[B-0004] …`, `fix: T-0361 …`;
    - a merge of the item's own branch: `Merge pull request #820 from o/<prefix>F-0112`,
      `Merge branch '<prefix>T-0359'`, `merge-queue: #752 (<prefix>T-0001 @ <sha>)` — the branch
      merged in, never a trunk merged into it.

    Nothing else: an id in prose (`…, for F-0115`), in a branch path the subject quotes, in a
    `Revert "…"`, or in a PR body names nothing."""
    s = (subject or "").strip()
    ids, rest = [], s
    while True:  # stacked scopes: `task(T-0448): task(T-0450): …` names both
        m = _SCOPE_SUBJECT.match(rest)
        found = id_tokens(m["scope"]) if m else []
        if not found:
            break
        ids += [i for i in found if i not in ids]
        rest = rest[m.end():].lstrip()
    if ids:
        return ids
    m = _LEAD_SUBJECT.match(s)
    if m:
        return id_tokens(m["ids"])
    for rx in _MERGE_SUBJECT:
        m = rx.match(s)
        if m:
            b = m["branch"]
            for pre in ("refs/heads/", "origin/"):
                if b.startswith(pre):
                    b = b[len(pre):]
            if main and b == main:
                return []
            return branch_ids(b)
    return []


def pr_naming_ids(pr):
    """The ids a PR is the work of: its head branch's (the item's own branch), else what its
    title names as :func:`naming_ids` reads a subject. Never its body."""
    return branch_ids(pr.get("headRefName")) or naming_ids(pr.get("title"))


def ci_provider(product):
    """The product's `ci.provider`, lower-cased; None for `ci: none`, no provider, or a product
    with no `ci` field at all.

    Kept local rather than aliased straight to :func:`asf.evidence.sources.ci_provider` (PD15):
    that one reads `product.ci` directly, and a bare `types.SimpleNamespace`/`Mock` test double
    with no `ci` attribute — common throughout the suite, `tests/test_landing_stamp.py` among
    them — raises `AttributeError` there where this answers `None`, same as `ci: none`. The two
    agree on every product that actually sets `ci`; `GH_ACTIONS` is still the one import, so the
    vocabulary itself cannot drift."""
    ci = getattr(product, "ci", None) if product is not None else None
    name = ci if isinstance(ci, str) else (ci or {}).get("provider") if isinstance(ci, dict) else None
    name = str(name).strip().lower() if name else ""
    return None if name in ("", "none", "off") else name


def ci_green_runs(product, sources=None):
    """[headSha, …] of the newest successful CI runs on main, newest first; [] if unreadable."""
    sources = sources or _default_sources(product, need_host=True)
    if ci_provider(product) not in GH_ACTIONS:
        return []
    runs = sources.host.runs(None, branch=product.main, status="success", limit=20)
    runs = sorted((r for r in runs if isinstance(r, dict) and r.get("headSha")),
                  key=lambda r: r.get("createdAt") or "", reverse=True)
    return list(dict.fromkeys(r["headSha"] for r in runs))


def record_since(product, sources=None):
    """The committer date of the record's first commit (the backlog repo's root), or None.

    Commits on main older than the record predate every id in it, so they are not read."""
    d = product.backlog_dir if product is not None else None
    if not d or not os.path.isdir(d):
        return None
    sources = sources or _default_sources(product)
    roots = sources.git.run(["rev-list", "--max-parents=0", "HEAD"], timeout=30, cwd=d).split()
    if not roots:
        return None
    dates = sources.git.run(["show", "-s", "--format=%cI"] + roots, timeout=30, cwd=d).split()
    return min(dates) if dates else None


def main_commits(product, sources=None):
    """[(sha, subject, [path, ...])] on origin/<main> since the record's first commit, newest
    first. A merge commit's paths are its diff against its first parent — what it brought in."""
    sources = sources or _default_sources(product)
    since = record_since(product, sources=sources)
    cmd = (f"git log --format=%x00%H%x09%s --name-only --diff-merges=first-parent "
           f"origin/{product.main}")
    if since:
        cmd += f" --since={since}"
    return _parse_name_log(sh(cmd, product=product, sources=sources))


def _parse_name_log(text):
    """``--format=%x00%H%x09%s --name-only`` output → [(sha, subject, [path, ...])]."""
    out = []
    for chunk in (text or "").split("\0"):
        lines = chunk.strip("\n").splitlines()
        if not lines:
            continue
        sha, _, subject = lines[0].partition("\t")
        if sha.strip():
            out.append((sha.strip(), subject, [ln.strip() for ln in lines[1:] if ln.strip()]))
    return out


def commit_paths(product, shas, sources=None):
    """{sha: [path, ...]} for commits the log did not already give — one `git show` for all."""
    shas = sorted({s for s in shas if s and FULL_SHA_RE.fullmatch(s)})
    if not shas:
        return {}
    text = sh("git show --format=%x00%H%x09%s --name-only --diff-merges=first-parent "
              + " ".join(shas), product=product, sources=sources)
    return {sha: paths for sha, _subject, paths in _parse_name_log(text)}


def doc_dirs(product):
    """The directories a document lane writes: ``specs_dir``, ``plans_dir``, ``reviews_dir``."""
    conv = product.conventions if product is not None else Conventions()
    return tuple(str(d).strip("/") + "/" for d in (conv.specs_dir, conv.plans_dir, conv.reviews_dir)
                 if d)


def docs_only(paths, dirs):
    """True when every path a commit touched is a spec, plan or review document. A commit whose
    paths are unknown (none read) is not judged docs-only — it stays landing evidence."""
    paths = [p for p in paths or () if p]
    return bool(paths) and all(any(p.startswith(d) for d in dirs) for p in paths)


#: The lane kinds whose merged PR is a document landing, never the Feature's code landing
#: (B-0114). Read by :func:`lane_kind` here and by
#: :func:`asf.harvest.lane.squash_subject`, which writes the subject this file then recognises —
#: the two must name the same kinds, so they name them once.
DOC_LANE_KINDS = ("spec", "plan")


def lane_kind(branch, prefixes):
    """"spec" / "plan" when `branch` is a document-lane branch (the product's spec/plan
    prefix), else None."""
    b = (branch or "").strip()
    for pre in ("refs/heads/", "origin/"):
        if b.startswith(pre):
            b = b[len(pre):]
    for kind in DOC_LANE_KINDS:
        pre = prefixes.get(kind)
        if pre and b.lower().startswith(pre.lower()):
            return kind
    return None


MERGED_STATE = "MERGED"
SHA_RE = re.compile(r"[0-9a-f]{7,40}")
#: a PR's own `mergeable` vocabulary (never `UNKNOWN`, never a missing field, D12) — the host's
#: own two states a PR can be read in, not a value either module invents. `asf/feeder/rows.py`
#: carries the same `CONFLICTING` string as its own module-level constant (read, never
#: imported: a module-level import of `asf.feeder.rows` from here is circular — `rows.py`
#: reaches back into `asf.evidence.evidence` through `asf.amendable`/`asf.briefs`).
CONFLICTING = "CONFLICTING"
MERGEABLE = "MERGEABLE"


def _merge_sha(run):
    """The sha a run's lane merged at, or None: its ``lane`` record in state ``MERGED`` (the
    merge's ``sha``), else its ``harvested:`` value — a full or short commit sha, never
    ``superseded`` (archived, nothing landed) nor a ``PR #n`` placeholder."""
    lane = run.get("lane") if isinstance(run.get("lane"), dict) else {}
    if lane.get("state") == MERGED_STATE:
        for key in ("sha", "merge_sha"):
            v = str(lane.get(key) or "")
            if SHA_RE.fullmatch(v):
                return v
    v = str(run.get("harvested") or "")
    return v if SHA_RE.fullmatch(v) else None


def merge_facts(product, path=None, sources=None):
    """``{"code": {item: {sha, branch, pr}}, "docs": {item: [{kind, sha, branch, files}]}}`` —
    the lane's merge facts off the product's run lines (``sessions.jsonl``): every run whose
    branch merged (:func:`_merge_sha`), newest merge per item. A spec/plan lane branch
    (:func:`lane_kind`) is a document's merge and goes under ``docs``; any other lands its item.
    Read-only; ``{}`` halves when the product has no ledger. A claim ``asf reset`` voided
    (:func:`asf.workers.lifecycle.voided_run`) is no merge fact, whatever the host says."""
    out = {"code": {}, "docs": {}}
    if product is None:
        return out
    from asf.workers import lifecycle
    if path is None:
        try:
            path = os.path.join(env.state_dir(product), "sessions.jsonl")
        except Exception:
            return out
    sources = sources or _default_sources(product)
    prefixes = branch_prefixes(product)
    on_main = None
    for rs in lifecycle.runs(path).values():
        for run in rs:
            sha, branch = _merge_sha(run), run.get("branch") or ""
            item = str(run.get("item") or "").upper()
            if not sha or not item:
                continue
            if lifecycle.voided_run(path, run, sha):
                continue  # `asf reset` voided this claim: the host's merge is not the item's
            lane = run.get("lane") if isinstance(run.get("lane"), dict) else {}
            if _needs_tip(run, lane):
                if on_main is None:
                    on_main = _tip_on_main(product, sources=sources)
                if not on_main(lane.get("head"), branch):
                    continue  # the trunk's sha, not the branch's: no landing of this item
            # a run closed on verified trunk evidence (asf.workers.trunkclose) lands its item
            # whatever lane its branch was: the sha is the item's work, not a document's merge
            kind = None if run.get("trunk_closed") else lane_kind(branch, prefixes)
            if kind:
                out["docs"].setdefault(item, []).append(
                    {"kind": kind, "sha": sha, "branch": branch,
                     "files": list(lane.get("files") or ())})
            else:
                out["code"][item] = {"sha": sha, "branch": branch, "pr": lane.get("pr")}
                if run.get("trunk_closed"):
                    # who closed it, for the card's `landing:` stamp (asf.record.ingest)
                    out["code"][item].update(trunk_closed=str(run["trunk_closed"]),
                                             trunk_arm=str(run.get("trunk_arm") or ""))
    return out


def _needs_tip(run, lane):
    """True when a run's merge fact names no merge of its own to trust: a lane record that says
    the branch is ``on-trunk`` (its sha is the trunk tip at the time, not a merge of the branch),
    or one with neither a PR nor a head behind it. Such a fact stands only when the branch's tip
    is really on the trunk (:func:`_tip_on_main`) — a PR merged by the host (its ``pr`` and the
    merge sha the host gave) and a run closed on verified trunk evidence stand as they are.
    2026-10-05: a Task was closed on "merge 062ad27 of <its branch>", where 062ad27 was another
    PR's merge and the branch tip was on no trunk commit."""
    if not lane or run.get("trunk_closed"):
        return False
    return lane.get("method") == "on-trunk" or not (lane.get("pr") or lane.get("head"))


def _tip_on_main(product, sources=None):
    """``(head, branch) -> bool``: is the branch's tip — the lane's ``head``, else the branch as
    origin has it now — an ancestor of (or the) trunk tip. False when neither is known: a merge
    fact with no tip behind it proves nothing."""
    main = f"origin/{getattr(product, 'main', None) or 'main'}"
    try:
        reach = ancestry(product, [main], sources=sources)
    except Exception:  # noqa: BLE001 — no repo: nothing can be shown on the trunk
        return lambda head, branch: False

    def check(head, branch):
        tip = head
        if not tip and branch:
            tip = sh("git rev-parse --verify -q " + shlex.quote(f"origin/{branch}^{{commit}}"),
                     product=product, sources=sources).strip()
        return bool(tip) and reach(tip)
    return check


#: A revert's own message: ``git revert`` writes ``This reverts commit <sha>.`` into the body.
REVERTS_RE = re.compile(r"This reverts commit ([0-9a-f]{40})")


def trunk_reverts(product, sources=None):
    """``{reverted sha: reverting sha}`` — every trunk commit a later trunk commit reverts
    (``This reverts commit <sha>`` in its message), one ``git log --grep`` for all. A revert that
    is itself reverted puts its target back: such a target is left out. ``{}`` when git says
    nothing (no product, no trunk)."""
    if product is None:
        return {}
    text = sh(f"git log --format=%H%x1e%B --grep='This reverts commit' origin/{product.main} --",
              product=product, sources=sources)
    rev = {}
    for sha, body in reversed(_parse_proves_log(text)):  # oldest first: a newer revert wins
        for target in REVERTS_RE.findall(body or ""):
            rev[target] = sha

    def reverted(sha, seen=()):
        r = rev.get(sha)
        return bool(r) and r not in seen and not reverted(r, seen + (sha,))

    return {t: r for t, r in rev.items() if reverted(t)}


def full_shas(product, shas):
    """``{sha: full 40-hex sha}`` for each of ``shas`` git knows as a commit in the product repo
    (one ``git cat-file --batch-check`` for all); an unknown or ambiguous one is left out."""
    shas = sorted({str(s) for s in shas or () if s and re.fullmatch(r"[0-9a-f]{4,40}", str(s))})
    if product is None or not shas:
        return {}
    # each line is `<sha> <sha>`: the name to look up, and itself again as %(rest) to key by
    text = sh("printf '%s %s\\n' " + " ".join(f"{s} {s}" for s in shas)
              + " | git cat-file --batch-check='%(objectname) %(objecttype) %(rest)'",
              product=product)
    out = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "commit" and FULL_SHA_RE.fullmatch(parts[0]):
            out[parts[2] if len(parts) > 2 else parts[0]] = parts[0]
    return out


def _commit_rows(commits):
    """(sha, subject, [path]) rows, from (sha, subject) and (sha, subject, paths) rows alike."""
    for c in commits or ():
        yield c[0], c[1], (list(c[2]) if len(c) > 2 and c[2] is not None else [])


def lane_docs(product, prs, commits=None, paths_of=None, merges=None):
    """{ID: {"spec": [path], "plan": [path], "prs": [n]}} — the documents each merged spec/plan
    lane PR brought onto the trunk, keyed by the id its head branch (else its title) names — and
    the documents each spec/plan lane run the ledger records as merged brought (``merges``: the
    ``docs`` half of :func:`merge_facts`; a fast-forward landing has no PR at all).

    A lane PR can land a date-prefixed plan (`2026-09-20-free-plan.md`) for `F-0019`: the file
    name does not carry the id — the lane branch (`<plan prefix>F-0019`) does."""
    prefixes = branch_prefixes(product)
    conv = product.conventions
    dirs = {"spec": str(conv.specs_dir).strip("/") + "/",
            "plan": str(conv.plans_dir).strip("/") + "/"}
    known = {sha: paths for sha, _s, paths in _commit_rows(commits)}
    lanes = []
    for p in prs or []:
        kind = lane_kind(p.get("headRefName"), prefixes)
        sha = (p.get("mergeCommit") or {}).get("oid") if p.get("state") == "MERGED" else None
        if not kind or not sha:
            continue
        ids = id_tokens(p.get("headRefName"), BRANCH_ID_TOKEN) or id_tokens(p.get("title") or "")
        if ids:
            lanes.append((p, sha, ids))
    runs = [(iid, f) for iid, facts in sorted(((merges or {}).get("docs") or {}).items())
            for f in facts]
    missing = [sha for _p, sha, _i in lanes if sha not in known]
    missing += [f["sha"] for _i, f in runs if not f.get("files") and f["sha"] not in known]
    if missing:
        known.update((paths_of or (lambda s: commit_paths(product, s)))(missing))
    out = {}
    for p, sha, ids in sorted(lanes, key=lambda t: t[0].get("number") or 0):
        for iid in ids:
            rec = out.setdefault(iid, {"spec": [], "plan": [], "prs": []})
            rec["prs"].append(p.get("number"))
            for path in known.get(sha) or []:
                for kind, d in dirs.items():
                    if path.startswith(d) and path.endswith(".md") and path not in rec[kind]:
                        rec[kind].append(path)
    for iid, f in runs:
        rec = out.setdefault(iid, {"spec": [], "plan": [], "prs": []})
        for path in f.get("files") or known.get(f["sha"]) or []:
            for kind, d in dirs.items():
                if path.startswith(d) and path.endswith(".md") and path not in rec[kind]:
                    rec[kind].append(path)
    return out


def merge_landed(ids, landed, product, green=None, sources=None):
    """``ids`` (an :func:`id_evidence` map) with every ``{iid: sha}`` of ``landed`` folded in:
    ``commit`` fills when the id carries none yet, ``landed`` carries the sha itself (so
    ``closing.Ev.landed`` can tell a reconciled entry from a commit-subject one), and ``green``
    is computed by the same ``ancestor_of`` walk over the same green runs :func:`id_evidence`'s
    own tail asks — one function, so a reconciled item's entry is indistinguishable from a
    commit-subject one except for carrying ``landed``. Called last: a sha a commit subject
    already carried is never overwritten."""
    if not landed:
        return ids
    out = dict(ids)
    # an explicit `green` (even `[]`) is the caller's own answer to "is there CI" — it stands in
    # for `ci_provider(product)` without re-asking the product, the way a caller holding the
    # product's `ci:` field through `ev['ci']` already resolved it
    has_ci = green is not None or ci_provider(product) is not None
    if has_ci and green is None:
        green = ci_green_runs(product, sources=sources)
    under_green = None
    for iid, sha in landed.items():
        r = dict(out.get(iid) or {"branches": [], "open_prs": [], "commit": None,
                                  "pr": None, "green": False})
        r["landed"] = sha
        if not r.get("commit"):
            if has_ci:
                if under_green is None:
                    under_green = ancestry(product, list(green), sources=sources)
                is_green = under_green(sha)
            else:
                is_green = True
            if is_green:
                r["commit"], r["green"] = sha, True
        out[iid] = r
    return out


def id_evidence(product, branches, prs, commits=None, green=None, merges=None, landed=None,
                sources=None):
    """{id: {branches, open_prs, commit, green}} — every id a branch, PR or commit on main names.

    `commit` is first the lane's merge fact for the id (``merges``, the ``code`` half of
    :func:`merge_facts`: the run line's merge sha — a squash merge included, whatever its subject
    says and whatever its ancestry); else the newest commit on main naming the id
    (:func:`naming_ids` — its subject's scope or lead id, or a merge of its branch), else a merged
    PR whose head is its branch or whose title names it (:func:`pr_naming_ids`), through its
    merge commit. A PR body names nothing. `green`
    says a CI run on main passed at or after it, and is true outright for a product with no CI
    provider. `landed` (``{iid: sha}``, a typed reconciliation sha) is folded in last through
    :func:`merge_landed`, never overwriting a commit a lane or a commit subject already carried.
    """
    main_ref = f"origin/{product.main}"
    out = {}
    on_main = None  # one rev-list of main, made the first time a merged PR asks

    def rec(iid):
        return out.setdefault(iid, {"branches": [], "open_prs": [], "commit": None,
                                    "pr": None, "green": False, "mergeable": None,
                                    "conflicting": []})

    for b in sorted(branches):
        if b == product.main:
            continue
        for iid in id_tokens(b, BRANCH_ID_TOKEN):
            rec(iid)["branches"].append(b)
    commits = main_commits(product, sources=sources) if commits is None else commits
    dirs = doc_dirs(product)
    facts = sorted(((merges or {}).get("code") or {}).items())
    # A lane merge whose diff is only documents — a writer report saying NO-CHANGE, a review, a
    # fixer's "no change needed" — lands nothing: it names its item because the run was that
    # item's, not because the item's work reached the trunk (a product's T-0047 closed on its
    # writer report, and its Feature then closed on that one commit).
    fact_paths = {sha: paths for sha, _s, paths in _commit_rows(commits)}
    unread = [f["sha"] for _i, f in facts if f.get("sha") and f["sha"] not in fact_paths]
    if unread:
        fact_paths.update(commit_paths(product, unread, sources=sources))
    for iid, fact in facts:
        if docs_only(fact_paths.get(fact["sha"]), dirs):
            continue
        r = rec(iid)
        r["commit"], r["merge"] = fact["sha"], fact.get("branch") or ""
        r["pr"] = fact.get("pr")
        if fact.get("trunk_closed"):
            r["trunk_closed"], r["trunk_arm"] = fact["trunk_closed"], fact.get("trunk_arm") or ""
    prefixes = branch_prefixes(product)
    known = {}
    # A Feature is never landed by its spec or plan: a commit whose diff is only documents under
    # specs_dir/plans_dir/reviews_dir, or a merged spec/plan lane PR, names its item because
    # that is the lane's convention — whatever the subject says (`docs(plan): F-0047 — …`,
    # `plan(OPS-1): the F-0037 plan`). The paths and the lane branch decide, not the subject.
    for sha, subject, paths in _commit_rows(commits):  # newest first: first seen is newest
        known[sha] = paths
        if lands_nothing(subject) or docs_only(paths, dirs):
            continue
        for iid in naming_ids(subject, product.main):
            r = rec(iid)
            r["commit"] = r["commit"] or sha
    merged_prs = []
    for p in sorted(prs, key=lambda p: p.get("number") or 0):
        ids = pr_naming_ids(p)
        state = p.get("state")
        sha = (p.get("mergeCommit") or {}).get("oid") if state == "MERGED" else None
        for iid in ids:
            if state == "OPEN":
                r = rec(iid)
                r["open_prs"].append(p["number"])
                pr_mergeable = p.get("mergeable")  # never UNKNOWN, never a missing field (D12)
                if pr_mergeable == CONFLICTING:
                    r["mergeable"] = CONFLICTING
                    r["conflicting"].append(p["number"])
                elif pr_mergeable == MERGEABLE and r["mergeable"] != CONFLICTING:
                    r["mergeable"] = MERGEABLE
        if sha and ids and not lane_kind(p.get("headRefName"), prefixes):
            merged_prs.append((p, sha, ids))
    unknown = [sha for _p, sha, _ids in merged_prs if sha not in known]
    if unknown:
        known.update(commit_paths(product, unknown, sources=sources))
    for p, sha, ids in merged_prs:
        if docs_only(known.get(sha), dirs) or lands_nothing(p.get("title")):
            continue
        for iid in ids:
            if (out.get(iid) or {}).get("commit"):
                continue
            if on_main is None:
                on_main = ancestry(product, [main_ref], sources=sources)
            if not on_main(sha):
                continue
            r = rec(iid)
            r["commit"], r["pr"] = sha, p["number"]

    if ci_provider(product) is None:
        for r in out.values():
            r["green"] = bool(r["commit"])
        return merge_landed(out, landed, product, sources=sources)
    green = ci_green_runs(product, sources=sources) if green is None else green
    covered = {}
    under_green = None  # one rev-list over every green run's sha, made on first use
    for r in out.values():
        c = r["commit"]
        if not c:
            continue
        if c not in covered:
            if under_green is None:
                under_green = ancestry(product, list(green), sources=sources)
            covered[c] = under_green(c)
        r["green"] = covered[c]
    return merge_landed(out, landed, product, green=green, sources=sources)


#: A trunk commit's ``git log --format=%H%x1e%B`` record: the 40-hex sha the record separator
#: follows, at line start — robust against a commit body with blank lines (there is no other
#: reliable terminator: %B's own trailing newline is not distinct from a blank body line).
_LOG_RECORD_RE = re.compile(r"(?m)^([0-9a-f]{40})\x1e")


def _parse_proves_log(text):
    """[(sha, body), ...] from ``git log --format=%H%x1e%B`` output."""
    text = text or ""
    marks = list(_LOG_RECORD_RE.finditer(text))
    return [(m.group(1), text[m.end():(marks[i + 1].start() if i + 1 < len(marks) else len(text))])
            for i, m in enumerate(marks)]


def landed_proves(product, prs, sources=None):
    """{story: [{line, test, task, pr, sha, source}, ...]} — every claim that has **landed**
    (§2.6): a claim is not evidence until it is on the trunk.

    Two sources, both keyed by Story id: a merged pull request whose merge commit is on the
    trunk contributes its body's claims (``source: "pr"``, ``pr`` its number, ``sha`` the merge
    oid, ``task`` the Task id among the PR's own ids, :func:`pr_naming_ids`) — no new
    round-trip, `pr_list` already asks for ``body``. A ``Proves:`` trailer on a trunk commit
    itself contributes that commit's claims (``source: "commit"``, ``pr`` ``None``, ``sha`` the
    commit, ``task`` the id token in its subject) — what makes the tick work for a product that
    lands by fast-forward and has no pull requests at all. A claim named by both collapses to
    the pull-request entry, which is why it is read first. Each Story's list sorts by
    ``(line, task)``.
    """
    main_ref = f"origin/{product.main}"
    per_story = {}
    seen = set()
    on_main = None

    def add(claim, task, pr, sha, source):
        key = (claim.story, claim.line, claim.test)
        if key in seen:
            return
        seen.add(key)
        per_story.setdefault(claim.story, []).append(
            {"line": claim.line, "test": claim.test, "task": task, "pr": pr, "sha": sha,
             "source": source})

    for p in sorted(prs or [], key=lambda p: p.get("number") or 0):
        if p.get("state") != MERGED_STATE:
            continue
        sha = (p.get("mergeCommit") or {}).get("oid")
        if not sha:
            continue
        if on_main is None:
            on_main = ancestry(product, [main_ref], sources=sources)
        if not on_main(sha):
            continue
        task = next((i for i in pr_naming_ids(p) if i.startswith("T-")), None)
        for claim in proves.parse(p.get("body") or ""):
            add(claim, task, p.get("number"), sha, "pr")

    since = record_since(product, sources=sources)
    cmd = f"git log --format=%H%x1e%B --grep=^Proves: {main_ref}"
    if since:
        cmd += f" --since={since}"
    for sha, body in _parse_proves_log(sh(cmd, product=product, sources=sources)):
        subject = body.splitlines()[0] if body else ""
        task = next((i for i in naming_ids(subject, product.main) if i.startswith("T-")), None)
        for claim in proves.parse(body):
            add(claim, task, None, sha, "commit")

    for entries in per_story.values():
        entries.sort(key=lambda e: (e["line"], e["task"] or ""))
    return per_story


def id_state(iid, iev, has_ci=True):
    """(state, [evidence line]) for an item from its id-token evidence, or (None, []) if none.

    A commit on main naming it → Resolved, Closed once CI is green at or after that commit (or
    at once without a CI provider); else an open PR naming it → Active; else a branch → Active.
    """
    if not iev:
        return None, []
    if iev.get("commit"):
        line = (f"merge {iev['commit'][:7]} of {iev['merge']} lands {iid}" if iev.get("merge")
                else f"commit {iev['commit'][:7]} names {iid}")
        if iev.get("pr"):
            line += f" (PR #{iev['pr']})"
        if iev.get("green"):
            return "Closed", [line, "CI green on main at or after it" if has_ci else "no CI provider"]
        return "Resolved", [line]
    if iev.get("open_prs"):
        conflicting = set(iev.get("conflicting") or ())
        lines = [f"PR #{n} OPEN (CONFLICTING)" if n in conflicting else f"PR #{n} OPEN"
                 for n in iev["open_prs"]]
        return "Active", lines + [f"branch {b}" for b in iev.get("branches") or []]
    if iev.get("branches"):
        return "Active", [f"branch {b}" for b in iev["branches"]]
    return None, []


def _report_name_ok(name, report_re):
    """A hotfix/diagnostic report's name is wanted: matching `report_re` when the product set
    one, else any `.md` (D7 — `report_pattern: None` means every report is read)."""
    if report_re is not None:
        return bool(report_re.search(name))
    return name.endswith(".md")


def migrate_sources(product=None, design_spec_name=None, sources=None):
    """Raw content `backlog.py migrate` needs beyond discover(): every spec doc on origin/main
    (the design spec's D-row table lives among them), the product's decisions file, and every
    report under its reports dir on origin/main and on code/spec/plan branches whose PR is not
    merged. All git/host access `migrate` needs stays behind this one function, same as
    `discover()`. A product that declares none of `design_spec_name`, `decisions_file` or
    `reports_dir` gets empty sources for it — the provider does not look for what was not named.
    """
    product = product or env.load_product()
    sources = sources or _default_sources(product, need_host=True)
    conv = product.conventions
    # `design_spec_name`/`decisions_file`/`reports_dir`/`report_pattern` are not yet fields of
    # `Conventions` (asf/conventions.py is out of this Task's footprint) — `.get()` reads them
    # from `extra` until they land there, and will keep reading them once they do.
    design_spec_name = design_spec_name if design_spec_name is not None else conv.get("design_spec_name")
    decisions_file = conv.get("decisions_file")
    reports_dir = conv.get("reports_dir")
    report_re = re.compile(conv.get("report_pattern")) if conv.get("report_pattern") else None

    branches = remote_branches(product=product, sources=sources)
    prs = pr_list(product=product, sources=sources)
    pr_by_head = {}
    for p in prs:
        pr_by_head.setdefault(p.get("headRefName") or "", []).append(p)
    prefixes = branch_prefixes(product)
    work = tuple({prefixes[k] for k in ("code", "spec", "plan")})
    open_branches = sorted(
        b for b in branches
        if b.startswith(work)
        and not any(p.get("state") == "MERGED" for p in pr_by_head.get(b, []))
    )

    specs_dir = conv.specs_dir
    main_specs_tree = list_tree("origin/main", specs_dir, product=product, sources=sources)
    spec_refs = [f"origin/main:{specs_dir}/{n}" for n in main_specs_tree if n.endswith(".md")]
    main_spec_texts = {os.path.basename(r): t
                       for r, t in read_refs(spec_refs, product=product, sources=sources).items()}

    sdd_decisions_text = (read_ref(f"origin/main:{decisions_file}", product=product, sources=sources)
                          if decisions_file else None)

    hotfix_texts = {}
    if reports_dir:
        revs = ["origin/main"] + [f"origin/{b}" for b in open_branches]
        report_trees = read_trees([f"{r}:{reports_dir}" for r in revs], product=product,
                                  sources=sources)
        main_names = {n for n in report_trees.get(f"origin/main:{reports_dir}", {})
                     if _report_name_ok(n, report_re)}
        hotfix_refs = [f"origin/main:{reports_dir}/{n}" for n in main_names]
        for r in revs[1:]:
            names = report_trees.get(f"{r}:{reports_dir}", {})
            for n in names:
                # a name already on origin/main is inherited history, not this branch's own
                # unmerged report — counting it here would multiply one main-side report by
                # every stale branch that happens to carry it too.
                if _report_name_ok(n, report_re) and n not in main_names:
                    hotfix_refs.append(f"{r}:{reports_dir}/{n}")
        hotfix_texts = read_refs(hotfix_refs, product=product, sources=sources)

    return {
        "design_spec_text": main_spec_texts.get(design_spec_name),
        "sdd_decisions_text": sdd_decisions_text,
        "main_spec_texts": main_spec_texts,
        "hotfix_texts": hotfix_texts,
        "open_branches": open_branches,
    }


FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")


def ancestry(product, bases, sources=None):
    """``sha -> bool``: is ``sha`` an ancestor of (or equal to) any of ``bases``, as
    :func:`ancestor_of` answers it — from one ``rev-list`` over every base, read once, instead of
    one ``merge-base`` fork per question (a hundred-odd a tick: most of ``ingest``'s time). When
    a base cannot be resolved, or a sha is not spelled in full, it asks :func:`ancestor_of`."""
    bases = [b for b in bases if b]
    reach = None
    if bases:
        sources = sources or _default_sources(product)
        out = sources.git.run(["rev-list", *bases, "--"])
        if out:
            reach = set(out.split())

    def check(sha):
        if not sha or not bases:
            return False
        if reach is not None and FULL_SHA_RE.fullmatch(sha):
            return sha in reach
        return any(ancestor_of(sha, b, product=product, sources=sources) for b in bases)
    return check


def ancestor_of(sha, base, product=None, sources=None):
    """True if `sha` is an ancestor of `base` in the product repo (a merge landed in `base`).

    `GitSource.run` answers with stdout alone (D7: never a command line, never an exit code), so
    this cannot run `--is-ancestor` and read its exit status; it resolves `sha` to its full oid
    (:meth:`GitSource.cat_file`) and asks `git merge-base <sha> <base>` instead — the common
    ancestor of an ancestor and its descendant is the ancestor itself, so `sha` is one exactly
    when the answer comes back as `sha`."""
    if not sha or not base:
        return False
    product = product or env.load_product()
    sources = sources or _default_sources(product)
    full = sources.git.cat_file("check", [sha])[0]
    if not full:
        return False
    out = sources.git.run(["merge-base", full, base]).strip()
    return bool(out) and out == full


# ---------------------------------------------------------------------- state rules (README) ---
# README.md's "State — typed intent, derived state" table and the "State rules" section of the T3
# task brief, each as one pure function taking plain evidence, no git/gh access.

def task_state(in_plan, branch, pr_state, merged_sha):
    """Task: New (in plan, no branch) → Active (branch or PR) → Closed (PR merged).

    ``asf.evidence.closing.RULES`` is the live rule a real ingest closes a Task by; this ladder
    is not it."""
    if merged_sha:
        return "Closed"
    if branch or pr_state:
        return "Active"
    if in_plan:
        return "New"
    return "New"


def story_state(any_task_active, matrix_status):
    """Story: New (no impl cited) → Active (a Task lists it, Active) → Resolved (matrix "doing")
    → Closed (matrix "done").

    ``asf.evidence.closing.RULES`` is the live rule a real ingest closes a Story by; this ladder
    is not it."""
    if matrix_status == "done":
        return "Closed"
    if matrix_status == "doing":
        return "Resolved"
    if any_task_active:
        return "Active"
    return "New"


def feature_state(spec_on_main, plan_approved, all_tasks_closed, all_merged_in_prod, all_prs_checked):
    """Feature: New (no spec on main) → Active (spec on main or plan approved) →
    Resolved (every Task Closed) → Closed (+ merge sha in prod deploy + the operator's checked ✓).

    ``asf.evidence.closing.RULES`` is the live rule a real ingest closes a Feature by; this
    ladder is not it."""
    if all_tasks_closed and all_merged_in_prod and all_prs_checked:
        return "Closed"
    if all_tasks_closed:
        return "Resolved"
    if spec_on_main or plan_approved:
        return "Active"
    return "New"


def feature_stage(spec, plan, tasks, on_prod):
    """The finer `card → ... → on-prod` ladder that drives the board.

    `spec`/`plan` are {"exists": bool, "review": (round, verdict)|None, "approved": bool};
    `spec` may say "on_trunk" (default: its "approved"). `tasks` is a list of per-task state
    strings ("New"/"Active"/"Closed").

    Coders read the spec from the trunk, so a Feature is plan-approved or building only on a
    spec ON the trunk: a plan landed, or Tasks run, on a spec that is not there (a migrated
    record's spec can still sit on a lane or pre-lane branch) stays on the spec ladder —
    spec-approved (an approved review: it waits to be landed), else spec-review/spec-draft, else
    card.
    """
    total = len(tasks)
    closed = sum(1 for t in tasks if t == "Closed")
    if total and closed == total:
        return "on-prod" if on_prod else "landed"
    started = bool(total) and bool(closed or any(t == "Active" for t in tasks))
    if not spec.get("on_trunk", spec["approved"]) and (plan["approved"] or started):
        # a plan landed, or Tasks run, on a spec the trunk never got: no coder starts until
        # the spec is on the trunk
        return "spec-approved" if spec["approved"] else _spec_stage(spec) or "card"
    if started:
        return f"building {closed}/{total}"
    if plan["approved"]:
        return "plan-approved"
    if plan["exists"]:
        if plan["review"] and plan["review"][1] != "APPROVED":
            return f"plan-review r{plan['review'][0]}"
        return "plan-draft"
    if spec["approved"]:
        return "spec-approved"
    return _spec_stage(spec) or "card"


def _spec_stage(spec):
    """``spec-review rN`` / ``spec-draft`` for a spec that exists unapproved, else ''."""
    if not spec["exists"]:
        return ""
    if spec["review"] and spec["review"][1] != "APPROVED":
        return f"spec-review r{spec['review'][0]}"
    return "spec-draft"


def bug_state(has_fixer_evidence, merged_sha, merged_in_prod):
    """Bug: New → Active (fixer branch/PR) → Resolved (merged) → Closed (merged sha in prod AND
    the signature absent 3 days — that half is T10's; a Bug merged in prod stays Resolved here
    with a "pending 3-day quiet" note).

    ``asf.evidence.closing.RULES`` is the live rule a real ingest closes a Bug by; this ladder
    is not it."""
    if merged_sha:
        return "Resolved"
    if has_fixer_evidence:
        return "Active"
    return "New"


def epic_state(children_states, typed_closed):
    """Epic: New (typed) → Active (any child Active) → Resolved (all children Closed) →
    Closed only if `closed: true` is typed by the operator.

    ``asf.evidence.closing.RULES`` is the live rule a real ingest closes an Epic by; this ladder
    is not it."""
    if typed_closed:
        return "Closed"
    if children_states and all(s == "Closed" for s in children_states):
        return "Resolved"
    if any(s == "Active" for s in children_states):
        return "Active"
    return "New"


def blocked_of(blocked_by, state_by_id):
    """(blocked: bool, blocked_by_open: [...]) — every listed item not Closed, plus every human
    string (a plain string not resolving to a known item id). ``blocked_by`` may itself be a bare
    string (``asf set X blockedBy=D-0001`` stores one value that way, not as a one-item list)."""
    from asf.record.core import as_list
    open_blockers = []
    for b in as_list(blocked_by):
        if isinstance(b, str) and not b.strip():
            continue  # ``blockedBy: ""`` names nothing: an empty blocker never blocks
        if b in state_by_id:
            if state_by_id[b] != "Closed":
                open_blockers.append(b)
        else:
            open_blockers.append(b)
    return bool(open_blockers), open_blockers


# -------------------------------------------------------------------------------------- cli ----
def load(fresh=False, product=None, checked_file=None, sources=None):
    """discover(), through the 3-minute cache under the product's state directory (one file per
    product). Shared by the CLI and by `backlog.py ingest`, so two calls a few seconds apart cost
    one round-trip to the host/git — the main-branch commit log and the CI green runs included.
    A `Recorded` `sources` is fed to `discover` directly: recording is not a cache mode."""
    product = product or env.load_product()
    cache = _cache_file("evidence.json", product)
    if sources is None and not fresh and os.path.exists(cache):
        if time.time() - os.path.getmtime(cache) < EVIDENCE_TTL:
            with open(cache) as f:
                data = json.load(f)
            data["checked"] = set(data["checked"])
            return data

    data = discover(product=product, checked_file=checked_file, sources=sources)
    text = json.dumps({**data, "checked": sorted(data["checked"])}, indent=2, sort_keys=True) + "\n"
    try:
        with open(cache + f".{os.getpid()}", "w") as f:
            f.write(text)
        os.replace(cache + f".{os.getpid()}", cache)
    except Exception:
        pass
    return data


class _RecordingGit:
    """Wraps a real `GitSource`, logging every call under the key :class:`Recorded` replays it
    from — `--record`'s whole mechanism."""

    def __init__(self, inner, log):
        self._inner, self._log = inner, log

    def run(self, args, timeout=120, cwd=None):
        out = self._inner.run(args, timeout=timeout, cwd=cwd)
        self._log["git.run:" + " ".join(args)] = out
        return out

    def cat_file(self, kind, requests):
        out = self._inner.cat_file(kind, requests)
        for req, value in zip(requests, out):
            self._log[f"git.cat_file:{kind}:{req}"] = (
                base64.b64encode(value).decode("ascii") if kind == "blob" and value is not None
                else value)
        return out

    def fetch(self):
        return self._inner.fetch()


class _RecordingHost:
    """Wraps a real `HostSource`, logging every call under the key :class:`Recorded` replays it
    from."""

    def __init__(self, inner, log):
        self._inner, self._log = inner, log

    def prs(self):
        out = self._inner.prs()
        self._log["host.prs"] = out
        return out

    def runs(self, workflow, branch=None, status=None, limit=20):
        out = self._inner.runs(workflow, branch=branch, status=status, limit=limit)
        key = "host.runs:" + ":".join(str(p) for p in (workflow, branch) if p is not None)
        self._log[key] = out
        return out

    def run_jobs(self, run_id):
        out = self._inner.run_jobs(run_id)
        self._log[f"host.run_jobs:{run_id}"] = out
        return out


class _RecordingDeploy:
    """Wraps a real `DeploySource`, logging every call under the key :class:`Recorded` replays
    it from."""

    def __init__(self, inner, log):
        self._inner, self._log = inner, log

    def deployment(self, env_name):
        out = self._inner.deployment(env_name)
        self._log[f"deploy.deployment:{env_name}"] = list(out)
        return out

    def sha(self, env_name):
        return self.deployment(env_name)[0]


def _record(product, record_dir):
    """`discover()` over the real providers, logging every call made; the three logs are
    written to `record_dir/{git,host,deploy}.json` — a snapshot :class:`Recorded` replays
    offline."""
    real = sources_mod.for_product(product)
    logs = {"git": {}, "host": {}, "deploy": {}}
    sources = sources_mod.Sources(_RecordingGit(real.git, logs["git"]),
                                  _RecordingHost(real.host, logs["host"]),
                                  _RecordingDeploy(real.deploy, logs["deploy"]))
    discover(product=product, sources=sources)
    os.makedirs(record_dir, exist_ok=True)
    paths = []
    for name in ("git", "host", "deploy"):
        path = os.path.join(record_dir, f"{name}.json")
        with open(path, "w") as f:
            json.dump(logs[name], f, indent=2, sort_keys=True)
            f.write("\n")
        paths.append(path)
    return paths


def main(argv=None):
    p = argparse.ArgumentParser(prog="evidence.py")
    p.add_argument("--json", action="store_true", help="print discover() as JSON")
    p.add_argument("--fresh", action="store_true", help="bypass the 3-minute cache")
    p.add_argument("--record", metavar="DIR",
                   help="record every git/host/deploy answer discover() used, to "
                        "DIR/{git,host,deploy}.json")
    env.add_product_arg(p)
    args = p.parse_args(argv)

    if not args.json:
        p.print_help()
        return 2

    product = env.load_product(args.product)
    if args.record:
        for path in _record(product, args.record):
            print(path)
        return 0

    data = load(fresh=args.fresh, product=product)
    text = json.dumps({**data, "checked": sorted(data["checked"])}, indent=2, sort_keys=True) + "\n"
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
