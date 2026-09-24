#!/usr/bin/env python3
"""evidence.py — the evidence pass's raw material.

`discover()` is one call that gathers everything the ingest pass needs to compute the
machine block of every item: what specs/plans/tasks exist in the product repo, what PRs and CI
runs say about them, and where the parity matrix stands. It also carries a handful of pure
functions that turn that evidence into the states README.md's "State — typed intent, derived
state" table defines, so they can be unit-tested without touching git or gh.

Most of the object-store plumbing below (`_batch`, `resolve`, `read_blobs`, `parse_tree`,
`read_trees`, `remote_branches`, `pr_list`, `plan_tasks`, `consumes_edges`, their regexes and
directory constants, and the `sh` helper `pr_list` depends on) is lifted from the first
product's pre-asf board, parity and tick-table scripts, which already solve "discover everything
the board needs" in two `git cat-file --batch` passes over the product repo. `parse_rows` (the
parity-matrix table parser) is lifted from the same. Reviews are read through the one review
reader, :mod:`asf.evidence.review`. The prod/dev deploy-sha lookups follow the same shape: the
newest successful run of `conventions.deploy_workflow` for `prod_sha`, the newest run of
`conventions.ci_workflow` on the trunk whose `conventions.ci_dev_job` succeeded for `dev_sha` —
both `None` when the product names no such workflow.

**Landing is the merge fact.** An item whose lane branch merged is landed by the run line that
says so — ``harvested: <sha>`` or a ``lane`` record in state ``MERGED`` in the product's
``sessions.jsonl`` (:func:`merge_facts`) — never by a commit subject or by the branch head's
ancestry, so a squash merge (whose sha no branch commit is an ancestor of) lands its item. A
document lane's merge (a spec or plan branch) lands its document, never its item (I10).

Read-only against the product repo (see the resolved `Product.repo_dir`): never commit, checkout
or fetch anything there but `git fetch --prune origin`. Python 3 stdlib only.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

from asf import env
from asf.conventions import Conventions
from asf.evidence import review

# An evidence-file path, not per-product config — no obvious Product field for it.
# TODO(config): no Product field for this yet.
CHECKED_FILE = os.path.join(env.ASF_HOME, "checked.txt")
PR_TTL = 180
EVIDENCE_TTL = 180

# The `ci.provider` values whose green runs on main `ci_green_runs` knows how to read.
GH_ACTIONS = ("gh-actions", "github-actions")


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


def sh(cmd, timeout=120, product=None):
    product = product or env.load_product()
    try:
        r = subprocess.run(cmd, shell=True, cwd=product.repo_dir, capture_output=True, text=True,
                           timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


# ---- object store: two batch passes for every tree and blob the board needs -----------------
# Lifted from factory-board.py.
def _batch(kind, requests, product=None):
    """One `git cat-file --batch[-check]` process for the whole request list.

    --batch answers with the OID, not the request, so the only way back to "which request was
    this" is position: the answers arrive in request order, and a missing object still costs one
    line. Both parsers below rely on that.
    """
    if not requests:
        return []
    product = product or env.load_product()
    args = ["git", "-C", product.repo_dir, "cat-file", "--batch-check" if kind == "check" else "--batch"]
    p = subprocess.run(args, input=("\n".join(requests) + "\n").encode(), capture_output=True)
    out = p.stdout
    res, i = [], 0
    for _ in requests:
        j = out.find(b"\n", i)
        if j < 0:
            res.append(None)
            continue
        header = out[i:j].decode("utf-8", "replace").split()
        i = j + 1
        if len(header) < 3 or header[-1] in ("missing", "ambiguous"):
            res.append(None)
            continue
        if kind == "check":
            res.append(header[0])
            continue
        size = int(header[2])
        res.append(out[i:i + size])
        i += size + 1
    return res


def resolve(paths, product=None):
    """rev:path → oid, or None. One process for the whole list."""
    return dict(zip(paths, _batch("check", paths, product=product)))


def read_blobs(shas, product=None):
    """oid → bytes. Deduplicated: several branches share the same review blob."""
    uniq = sorted({s for s in shas if s})
    return dict(zip(uniq, _batch("blob", uniq, product=product)))


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


def read_trees(paths, product=None):
    """rev:dir → {name: oid}. Distinct tree oids are fetched once each."""
    oids = resolve(paths, product=product)
    blobs = read_blobs(oids.values(), product=product)
    return {p: parse_tree(blobs.get(o)) for p, o in oids.items()}


def read_refs(refs, product=None):
    """["rev:path", ...] → {ref: text|None}, one batch round-trip. The general-purpose read
    `backlog.py migrate` uses for spec/plan/report content — every git access it needs stays
    behind this module rather than migrate.py shelling out on its own.
    """
    oids = resolve(refs, product=product)
    blobs = read_blobs(oids.values(), product=product)
    out = {}
    for ref, oid in oids.items():
        blob = blobs.get(oid) if oid else None
        out[ref] = blob.decode("utf-8", "replace") if blob else None
    return out


def read_ref(ref, product=None):
    """"rev:path" → text, or None if missing."""
    return read_refs([ref], product=product)[ref]


def list_tree(rev, path, product=None):
    """rev:path → {name: oid}, the directory listing at that revision."""
    return read_trees([f"{rev}:{path}"], product=product)[f"{rev}:{path}"]


def doc_carriers(path, branches, product):
    """Where a typed document path lives: ``(on_trunk, [remote branches carrying it])``, one
    ``cat-file`` process for the lot. A migrated card's ``links.spec`` can name a spec that sits
    on a pre-lane branch with no PR — the lane's own discovery never looks there, and a Feature
    whose spec is nowhere on the trunk must not read as approved."""
    if not path or product is None:
        return False, []
    main_ref = f"origin/{product.main}"
    others = sorted(b for b in branches or () if b != product.main)
    refs = [f"{main_ref}:{path}"] + [f"origin/{b}:{path}" for b in others]
    found = resolve(refs, product=product)
    return bool(found.get(refs[0])), [b for b in others if found.get(f"origin/{b}:{path}")]


# ---- inputs ---------------------------------------------------------------------------------
def remote_branches(product=None):
    product = product or env.load_product()
    sh("git fetch --prune -q origin", timeout=180, product=product)
    out = sh("git ls-remote --heads origin", timeout=120, product=product)
    return {ln.split("refs/heads/", 1)[1] for ln in out.splitlines() if "refs/heads/" in ln}


def pr_list(product=None):
    product = product or env.load_product()
    cache = _cache_file("prs.json", product)
    if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < PR_TTL:
        try:
            with open(cache) as f:
                return json.load(f)
        except Exception:
            pass
    # mergeCommit is extra vs. factory-board.py's field list: evidence.py needs the merge sha
    # (Feature/Task Closed rules), factory-board.py's board never did; `body` carries id tokens.
    raw = sh(f"gh pr list -R {product.repo_slug} --state all --limit 300 "
             "--json number,title,body,state,headRefName,mergedAt,mergeCommit", timeout=180,
             product=product)
    try:
        data = json.loads(raw) if raw else []
    except Exception:
        data = []
    try:
        with open(cache + f".{os.getpid()}", "w") as f:
            json.dump(data, f)
        os.replace(cache + f".{os.getpid()}", cache)
    except Exception:
        pass
    return data


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


def verdict_of(blob, legacy=True):
    """A review's verdict as the evidence spells it (``APPROVED`` / ``CHANGES REQUESTED`` / ``""``),
    read by the one reader: :func:`asf.evidence.review.verdict_of` — its own ``verdict:`` line —
    and, for a legacy ``<slug>-review-r<n>.md`` file, its first verdict word."""
    if not blob:
        return ""
    v = review.legacy_verdict_of(blob) if legacy else review.verdict_of(blob)
    return VERDICT_WORDS.get(v, "")


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


TASK_HEAD = re.compile(r"^#{2,4}\s+(?:Task\s+|T(?=\d))(\d+[a-z]?)\b(?P<rest>[^\n]*)",
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
    """Split a markdown table row on unescaped `|`; a literal pipe is escaped `\\|`."""
    c = re.split(r"(?<!\\)\|", line.strip())
    if c and not c[0].strip():
        c = c[1:]
    if c and not c[-1].strip():
        c = c[:-1]
    return [x.replace("\\|", "|").strip() for x in c]


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


def _newest_success(workflow, product=None):
    """The newest successful run of `workflow`, or None."""
    runs = _gh_json(f"run list --workflow {workflow} --limit 15 "
                    "--json headSha,conclusion,updatedAt", product=product)
    return next((r for r in runs if r.get("conclusion") == "success"), None)


def _newest_with_job(workflow, job, branch, product=None):
    """The newest completed run of `workflow` on `branch` whose `job` succeeded, or None."""
    runs = _gh_json(f"run list --branch {branch} --workflow {workflow} --limit 40 "
                    "--json headSha,conclusion,updatedAt,status,databaseId", product=product)
    for r in runs:
        if r.get("status") != "completed":
            continue
        jobs = _gh_json(f"api repos/{product.repo_slug}/actions/runs/{r['databaseId']}/jobs?per_page=60",
                        product=product)
        job_list = jobs.get("jobs", []) if isinstance(jobs, dict) else []
        if any(j.get("name") == job and j.get("conclusion") == "success" for j in job_list):
            return r
    return None


def discover(product=None, checked_file=None):
    """The raw evidence `backlog.py ingest` needs, gathered fresh from the product repo and gh."""
    product = product or env.load_product()
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

    branches = remote_branches(product=product)
    prs = pr_list(product=product)
    pr_by_head = {}
    for p in prs:
        pr_by_head.setdefault(p.get("headRefName") or "", []).append(p)
    merged = {p["number"]: p["mergeCommit"]["oid"]
              for p in prs if p.get("state") == "MERGED" and p.get("mergeCommit")}

    tree_paths = [f"{main_ref}:{plans_dir}", f"{main_ref}:{specs_dir}", f"{main_ref}:{reviews_dir}"]
    if briefs_dir:
        tree_paths.append(f"{main_ref}:{briefs_dir}")
    main_trees = read_trees(tree_paths, product=product)
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
    trees = read_trees(tree_paths, product=product)

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
            if f:
                wanted.setdefault(slug, {})[kind] = (f, r, names[f], legacy)

    blobs = read_blobs(list(plan_req.values()) + list(spec_req.values()) +
                       [v[2] for d in wanted.values() for v in d.values()], product=product)

    brief_dirs = {}
    for slug in inits:
        hit = [d for d in main_briefs if d == slug or d.endswith("-" + slug)]
        if hit:
            brief_dirs[slug] = f"{main_ref}:{briefs_dir}/{sorted(hit)[-1]}"
    brief_trees = read_trees(sorted(set(brief_dirs.values())), product=product)

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
    task_trees = read_trees(sorted(set(task_tree_paths)), product=product)

    task_blob_req = []
    for slug, rows in task_rows.items():
        for row in rows:
            if not row["branch"]:
                continue
            names = task_trees.get(f"origin/{row['branch']}:{reviews_dir}", {})
            ids = row["cand"] + [row["branch"].split("/", 1)[-1]]
            f, r, legacy = pick_review(conv, reviews_dir, names, ids,
                                       [p for i in ids for p in (i, f"hotfix-{i}", f"review-{i}")])
            row["review"] = (r, legacy, f) if f else None
            if f:
                task_blob_req.append(names[f])
    task_blobs = read_blobs(task_blob_req, product=product)
    for rows in task_rows.values():
        for row in rows:
            rev = row.pop("review", None)
            if rev:
                r, legacy, f = rev
                names = task_trees.get(f"origin/{row['branch']}:{reviews_dir}", {})
                row["review"] = (r, verdict_of(task_blobs.get(names.get(f)), legacy=legacy))
            else:
                row["review"] = None

    features = {}
    for it in inits.values():
        slug = it["slug"]

        def review_tuple(kind):
            hit = wanted.get(slug, {}).get(kind)
            if not hit:
                return None
            f, r, sha, legacy = hit
            return (r, verdict_of(blobs.get(sha), legacy=legacy), f)

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
                                  "merged_sha": row["merged_sha"],
                                  "landed_no_branch": row["landed_no_branch"]}
                     for row in task_rows.get(slug, [])},
            "prs": pr_numbers,
        }

    # ---- the parity matrix, for Stories — only when the product names one (D1: None = not
    # looked for)
    if matrix_path:
        matrix_text = sh(f"git show {main_ref}:{matrix_path}", product=product)
        rows, _broken = parse_rows(matrix_text) if matrix_text else ([], [])
        stories = {r["id"]: {"status": r["status"], "impl": _matrix_paths(r["impl"]),
                             "test": _matrix_paths(r["tests"]), "area": r["area"],
                             "milestone": r["ms"], "cap": r["cap"]}
                  for r in rows}
    else:
        stories = {}

    # ---- deploy shas: newest successful run of the product's deploy workflow; newest run of
    # its ci workflow on the trunk whose named job succeeded. `None` → that query is not made.
    deploy_workflow, ci_workflow, ci_dev_job = (
        conv.get("deploy_workflow"), conv.get("ci_workflow"), conv.get("ci_dev_job"))
    prod = _newest_success(deploy_workflow, product=product) if deploy_workflow else None
    dev = (_newest_with_job(ci_workflow, ci_dev_job, branch=product.main, product=product)
           if ci_workflow and ci_dev_job else None)

    checked_file = checked_file if checked_file is not None else CHECKED_FILE
    checked = set()
    if os.path.exists(checked_file):
        with open(checked_file) as f:
            for line in f:
                m = re.match(r"\s*#?(\d+)", line)
                if m:
                    checked.add(int(m.group(1)))

    main_sha = sh(f"git rev-parse {main_ref}", product=product)
    commits = main_commits(product)
    merges = merge_facts(product)

    return {
        "features": features,
        "stories": stories,
        "prod_sha": prod["headSha"] if prod else None,
        "dev_sha": dev["headSha"] if dev else None,
        "checked": checked,
        "main_sha": main_sha or None,
        "merged": merged,
        "branches": sorted(branches),
        "ids": id_evidence(product, branches, prs, commits=commits, merges=merges),
        "lane_docs": lane_docs(product, prs, commits=commits, merges=merges),
        "ci": ci_provider(product),
    }


# ---- id tokens: an item's own id in a branch name, a PR or a commit subject on main ----------
ID_TOKEN = re.compile(r"\b([EFSTBDR])-(\d{4})\b")
# branch names are often lower-cased (`fix/b-0003`); titles, bodies and subjects are not read so
BRANCH_ID_TOKEN = re.compile(r"\b([EFSTBDR])-(\d{4})\b", re.IGNORECASE)


#: A commit of the document lanes — a spec, a plan, a review, a ruling — names its item because
#: that is the lane's subject convention, not because the item's work landed (B-0059: the
#: landing of eight specs closed four Features).
DOC_LANE_SUBJECT = re.compile(r"^(spec|plan|review|adjudicate)\(|^docs\((spec|plan|review)\)")


def id_tokens(text, rx=ID_TOKEN):
    """Every `<TYPE>-<nnnn>` in text, in order, deduplicated; `(B-0001)` and `[B-0001]` count."""
    return list(dict.fromkeys(f"{t.upper()}-{n}" for t, n in rx.findall(text or "")))


def ci_provider(product):
    """The product's `ci.provider`, lower-cased; None for `ci: none` or no provider at all."""
    ci = product.ci if product is not None else None
    name = ci if isinstance(ci, str) else (ci or {}).get("provider") if isinstance(ci, dict) else None
    name = str(name).strip().lower() if name else ""
    return None if name in ("", "none", "off") else name


def ci_green_runs(product):
    """[headSha, …] of the newest successful CI runs on main, newest first; [] if unreadable."""
    if ci_provider(product) not in GH_ACTIONS:
        return []
    runs = _gh_json(f"run list --branch {product.main} --status success --limit 20 "
                    "--json headSha,createdAt", product=product)
    runs = sorted((r for r in runs if isinstance(r, dict) and r.get("headSha")),
                  key=lambda r: r.get("createdAt") or "", reverse=True)
    return list(dict.fromkeys(r["headSha"] for r in runs))


def record_since(product):
    """The committer date of the record's first commit (the backlog repo's root), or None.

    Commits on main older than the record predate every id in it, so they are not read."""
    d = product.backlog_dir if product is not None else None
    if not d or not os.path.isdir(d):
        return None
    try:
        roots = subprocess.run(["git", "-C", d, "rev-list", "--max-parents=0", "HEAD"],
                               capture_output=True, text=True, timeout=30).stdout.split()
        if not roots:
            return None
        dates = subprocess.run(["git", "-C", d, "show", "-s", "--format=%cI"] + roots,
                               capture_output=True, text=True, timeout=30).stdout.split()
    except Exception:
        return None
    return min(dates) if dates else None


def main_commits(product):
    """[(sha, subject, [path, ...])] on origin/<main> since the record's first commit, newest
    first. A merge commit's paths are its diff against its first parent — what it brought in."""
    since = record_since(product)
    cmd = (f"git log --format=%x00%H%x09%s --name-only --diff-merges=first-parent "
           f"origin/{product.main}")
    if since:
        cmd += f" --since={since}"
    return _parse_name_log(sh(cmd, product=product))


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


def commit_paths(product, shas):
    """{sha: [path, ...]} for commits the log did not already give — one `git show` for all."""
    shas = sorted({s for s in shas if s and FULL_SHA_RE.fullmatch(s)})
    if not shas:
        return {}
    text = sh("git show --format=%x00%H%x09%s --name-only --diff-merges=first-parent "
              + " ".join(shas), product=product)
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


def merge_facts(product, path=None):
    """``{"code": {item: {sha, branch, pr}}, "docs": {item: [{kind, sha, branch, files}]}}`` —
    the lane's merge facts off the product's run lines (``sessions.jsonl``): every run whose
    branch merged (:func:`_merge_sha`), newest merge per item. A spec/plan lane branch
    (:func:`lane_kind`) is a document's merge and goes under ``docs``; any other lands its item.
    Read-only; ``{}`` halves when the product has no ledger."""
    out = {"code": {}, "docs": {}}
    if product is None:
        return out
    from asf.workers import lifecycle
    if path is None:
        try:
            path = os.path.join(env.state_dir(product), "sessions.jsonl")
        except Exception:
            return out
    prefixes = branch_prefixes(product)
    for rs in lifecycle.runs(path).values():
        for run in rs:
            sha, branch = _merge_sha(run), run.get("branch") or ""
            item = str(run.get("item") or "").upper()
            if not sha or not item:
                continue
            lane = run.get("lane") if isinstance(run.get("lane"), dict) else {}
            kind = lane_kind(branch, prefixes)
            if kind:
                out["docs"].setdefault(item, []).append(
                    {"kind": kind, "sha": sha, "branch": branch,
                     "files": list(lane.get("files") or ())})
            else:
                out["code"][item] = {"sha": sha, "branch": branch, "pr": lane.get("pr")}
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


def id_evidence(product, branches, prs, commits=None, green=None, merges=None):
    """{id: {branches, open_prs, commit, green}} — every id a branch, PR or commit on main names.

    `commit` is first the lane's merge fact for the id (``merges``, the ``code`` half of
    :func:`merge_facts`: the run line's merge sha — a squash merge included, whatever its subject
    says and whatever its ancestry); else the newest commit on main naming the id (a direct
    trunk commit, a merged PR naming it in its title or body through its merge commit). `green`
    says a CI run on main passed at or after it, and is true outright for a product with no CI
    provider.
    """
    main_ref = f"origin/{product.main}"
    out = {}
    on_main = None  # one rev-list of main, made the first time a merged PR asks

    def rec(iid):
        return out.setdefault(iid, {"branches": [], "open_prs": [], "commit": None,
                                    "pr": None, "green": False})

    for b in sorted(branches):
        if b == product.main:
            continue
        for iid in id_tokens(b, BRANCH_ID_TOKEN):
            rec(iid)["branches"].append(b)
    for iid, fact in sorted(((merges or {}).get("code") or {}).items()):
        r = rec(iid)
        r["commit"], r["merge"] = fact["sha"], fact.get("branch") or ""
        r["pr"] = fact.get("pr")
    commits = main_commits(product) if commits is None else commits
    dirs = doc_dirs(product)
    prefixes = branch_prefixes(product)
    known = {}
    # A Feature is never landed by its spec or plan: a commit whose diff is only documents under
    # specs_dir/plans_dir/reviews_dir, or a merged spec/plan lane PR, names its item because
    # that is the lane's convention — whatever the subject says (`docs(plan): F-0047 — …`,
    # `plan(OPS-1): the F-0037 plan`). The paths and the lane branch decide, not the subject.
    for sha, subject, paths in _commit_rows(commits):  # newest first: first seen is newest
        known[sha] = paths
        if DOC_LANE_SUBJECT.match(subject or "") or docs_only(paths, dirs):
            continue
        for iid in id_tokens(subject):
            r = rec(iid)
            r["commit"] = r["commit"] or sha
    merged_prs = []
    for p in sorted(prs, key=lambda p: p.get("number") or 0):
        ids = id_tokens(f"{p.get('title') or ''}\n{p.get('body') or ''}")
        state = p.get("state")
        sha = (p.get("mergeCommit") or {}).get("oid") if state == "MERGED" else None
        for iid in ids:
            if state == "OPEN":
                rec(iid)["open_prs"].append(p["number"])
        if sha and ids and not lane_kind(p.get("headRefName"), prefixes):
            merged_prs.append((p, sha, ids))
    unknown = [sha for _p, sha, _ids in merged_prs if sha not in known]
    if unknown:
        known.update(commit_paths(product, unknown))
    for p, sha, ids in merged_prs:
        if docs_only(known.get(sha), dirs):
            continue
        for iid in ids:
            if (out.get(iid) or {}).get("commit"):
                continue
            if on_main is None:
                on_main = ancestry(product, [main_ref])
            if not on_main(sha):
                continue
            r = rec(iid)
            r["commit"], r["pr"] = sha, p["number"]

    if ci_provider(product) is None:
        for r in out.values():
            r["green"] = bool(r["commit"])
        return out
    green = ci_green_runs(product) if green is None else green
    covered = {}
    under_green = None  # one rev-list over every green run's sha, made on first use
    for r in out.values():
        c = r["commit"]
        if not c:
            continue
        if c not in covered:
            if under_green is None:
                under_green = ancestry(product, list(green))
            covered[c] = under_green(c)
        r["green"] = covered[c]
    return out


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
        lines = [f"PR #{n} OPEN" for n in iev["open_prs"]]
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


def migrate_sources(product=None, design_spec_name=None):
    """Raw content `backlog.py migrate` needs beyond discover(): every spec doc on origin/main
    (the design spec's D-row table lives among them), the product's decisions file, and every
    report under its reports dir on origin/main and on code/spec/plan branches whose PR is not
    merged. All git/gh access `migrate` needs stays behind this one function, same as
    `discover()`. A product that declares none of `design_spec_name`, `decisions_file` or
    `reports_dir` gets empty sources for it — the provider does not look for what was not named.
    """
    product = product or env.load_product()
    conv = product.conventions
    # `design_spec_name`/`decisions_file`/`reports_dir`/`report_pattern` are not yet fields of
    # `Conventions` (asf/conventions.py is out of this Task's footprint) — `.get()` reads them
    # from `extra` until they land there, and will keep reading them once they do.
    design_spec_name = design_spec_name if design_spec_name is not None else conv.get("design_spec_name")
    decisions_file = conv.get("decisions_file")
    reports_dir = conv.get("reports_dir")
    report_re = re.compile(conv.get("report_pattern")) if conv.get("report_pattern") else None

    branches = remote_branches(product=product)
    prs = pr_list(product=product)
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
    main_specs_tree = list_tree("origin/main", specs_dir, product=product)
    spec_refs = [f"origin/main:{specs_dir}/{n}" for n in main_specs_tree if n.endswith(".md")]
    main_spec_texts = {os.path.basename(r): t for r, t in read_refs(spec_refs, product=product).items()}

    sdd_decisions_text = (read_ref(f"origin/main:{decisions_file}", product=product)
                          if decisions_file else None)

    hotfix_texts = {}
    if reports_dir:
        revs = ["origin/main"] + [f"origin/{b}" for b in open_branches]
        report_trees = read_trees([f"{r}:{reports_dir}" for r in revs], product=product)
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
        hotfix_texts = read_refs(hotfix_refs, product=product)

    return {
        "design_spec_text": main_spec_texts.get(design_spec_name),
        "sdd_decisions_text": sdd_decisions_text,
        "main_spec_texts": main_spec_texts,
        "hotfix_texts": hotfix_texts,
        "open_branches": open_branches,
    }


FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")


def ancestry(product, bases):
    """``sha -> bool``: is ``sha`` an ancestor of (or equal to) any of ``bases``, as
    :func:`ancestor_of` answers it — from one ``rev-list`` over every base, read once, instead of
    one ``merge-base`` fork per question (a hundred-odd a tick: most of ``ingest``'s time). When
    a base cannot be resolved, or a sha is not spelled in full, it asks :func:`ancestor_of`."""
    bases = [b for b in bases if b]
    reach = None
    if bases:
        r = subprocess.run(["git", "-C", product.repo_dir, "rev-list", *bases, "--"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            reach = set(r.stdout.split())

    def check(sha):
        if not sha or not bases:
            return False
        if reach is not None and FULL_SHA_RE.fullmatch(sha):
            return sha in reach
        return any(ancestor_of(sha, b, product=product) for b in bases)
    return check


def ancestor_of(sha, base, product=None):
    """True if `sha` is an ancestor of `base` in the product repo (a merge landed in `base`)."""
    if not sha or not base:
        return False
    product = product or env.load_product()
    r = subprocess.run(["git", "-C", product.repo_dir, "merge-base", "--is-ancestor", sha, base],
                       capture_output=True)
    return r.returncode == 0


def _gh_json(args, timeout=60, product=None):
    product = product or env.load_product()
    raw = sh(f"gh {args} -R {product.repo_slug}" if not args.startswith("api ") else f"gh {args}",
             timeout, product=product)
    try:
        return json.loads(raw) if raw else ([] if not args.startswith("api ") else {})
    except json.JSONDecodeError:
        return [] if not args.startswith("api ") else {}


# ---------------------------------------------------------------------- state rules (README) ---
# README.md's "State — typed intent, derived state" table and the "State rules" section of the T3
# task brief, each as one pure function taking plain evidence, no git/gh access.

def task_state(in_plan, branch, pr_state, merged_sha):
    """Task: New (in plan, no branch) → Active (branch or PR) → Closed (PR merged)."""
    if merged_sha:
        return "Closed"
    if branch or pr_state:
        return "Active"
    if in_plan:
        return "New"
    return "New"


def story_state(any_task_active, matrix_status):
    """Story: New (no impl cited) → Active (a Task lists it, Active) → Resolved (matrix "doing")
    → Closed (matrix "done")."""
    if matrix_status == "done":
        return "Closed"
    if matrix_status == "doing":
        return "Resolved"
    if any_task_active:
        return "Active"
    return "New"


def feature_state(spec_on_main, plan_approved, all_tasks_closed, all_merged_in_prod, all_prs_checked):
    """Feature: New (no spec on main) → Active (spec on main or plan approved) →
    Resolved (every Task Closed) → Closed (+ merge sha in prod deploy + the operator's checked ✓)."""
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
    with a "pending 3-day quiet" note)."""
    if merged_sha:
        return "Resolved"
    if has_fixer_evidence:
        return "Active"
    return "New"


def epic_state(children_states, typed_closed):
    """Epic: New (typed) → Active (any child Active) → Resolved (all children Closed) →
    Closed only if `closed: true` is typed by the operator."""
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
        if b in state_by_id:
            if state_by_id[b] != "Closed":
                open_blockers.append(b)
        else:
            open_blockers.append(b)
    return bool(open_blockers), open_blockers


# -------------------------------------------------------------------------------------- cli ----
def load(fresh=False, product=None, checked_file=None):
    """discover(), through the 3-minute cache under the product's state directory (one file per
    product). Shared by the CLI and by `backlog.py ingest`, so two calls a few seconds apart cost
    one round-trip to gh/git — the main-branch commit log and the CI green runs included."""
    product = product or env.load_product()
    cache = _cache_file("evidence.json", product)
    if not fresh and os.path.exists(cache):
        if time.time() - os.path.getmtime(cache) < EVIDENCE_TTL:
            with open(cache) as f:
                data = json.load(f)
            data["checked"] = set(data["checked"])
            return data

    data = discover(product=product, checked_file=checked_file)
    text = json.dumps({**data, "checked": sorted(data["checked"])}, indent=2, sort_keys=True) + "\n"
    try:
        with open(cache + f".{os.getpid()}", "w") as f:
            f.write(text)
        os.replace(cache + f".{os.getpid()}", cache)
    except Exception:
        pass
    return data


def main(argv=None):
    p = argparse.ArgumentParser(prog="evidence.py")
    p.add_argument("--json", action="store_true", help="print discover() as JSON")
    p.add_argument("--fresh", action="store_true", help="bypass the 3-minute cache")
    env.add_product_arg(p)
    args = p.parse_args(argv)

    if not args.json:
        p.print_help()
        return 2

    product = env.load_product(args.product)
    data = load(fresh=args.fresh, product=product)
    text = json.dumps({**data, "checked": sorted(data["checked"])}, indent=2, sort_keys=True) + "\n"
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
