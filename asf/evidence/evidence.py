#!/usr/bin/env python3
"""evidence.py — the evidence pass's raw material.

`discover()` is one call that gathers everything `tools/backlog.py ingest` needs to compute the
machine block of every item: what specs/plans/tasks exist in the product repo, what PRs and CI
runs say about them, and where the parity matrix stands. It also carries a handful of pure
functions that turn that evidence into the states README.md's "State — typed intent, derived
state" table defines, so they can be unit-tested without touching git or gh.

Most of the object-store plumbing below (`_batch`, `resolve`, `read_blobs`, `parse_tree`,
`read_trees`, `remote_branches`, `pr_list`, `verdict_of`, `newest_review`, `rx_review`,
`plan_tasks`, `consumes_edges`, their regexes and directory constants, and the `sh` helper
`pr_list` depends on) is lifted verbatim from `~/.claude-workers/tools/factory-board.py`, which
already solves "discover everything the board needs" in two `git cat-file --batch` passes over
the product repo. `parse_rows` (the parity-matrix table parser) is lifted verbatim from
`~/.claude-workers/tools/factory-parity.py`. The prod/dev deploy-sha lookups follow
`~/.claude-workers/tools/tick-tables.py` (roughly its lines 44-58): newest successful
`deploy-prod.yml` run for `prod_sha`, newest `ci.yml` run on `main` whose `deploy-dev` job
succeeded for `dev_sha`.

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

# An evidence-file path, not per-product config — no obvious Product field for it.
# TODO(config): no Product field for this yet.
CHECKED_FILE = os.path.join(env.ASF_HOME, "checked.txt")
PR_CACHE = "/tmp/board-prs.json"
PR_TTL = 180
EVIDENCE_CACHE = "/tmp/backlog-evidence.json"
EVIDENCE_TTL = 180

PLANS_DIR = "docs/superpowers/plans"
SPECS_DIR = "docs/superpowers/specs"
BRIEFS_DIR = "docs/superpowers/briefs"
REVIEWS_DIR = ".sdd-input/reviews"
MATRIX_PATH = "docs/research/feature-matrix.md"


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


# ---- inputs ---------------------------------------------------------------------------------
def remote_branches(product=None):
    product = product or env.load_product()
    sh("git fetch --prune -q origin", timeout=180, product=product)
    out = sh("git ls-remote --heads origin", timeout=120, product=product)
    return {ln.split("refs/heads/", 1)[1] for ln in out.splitlines() if "refs/heads/" in ln}


def pr_list(product=None):
    product = product or env.load_product()
    if os.path.exists(PR_CACHE) and time.time() - os.path.getmtime(PR_CACHE) < PR_TTL:
        try:
            with open(PR_CACHE) as f:
                return json.load(f)
        except Exception:
            pass
    # mergeCommit is extra vs. factory-board.py's field list: evidence.py needs the merge sha
    # (Feature/Task Closed rules), factory-board.py's board never did.
    raw = sh(f"gh pr list -R {product.repo_slug} --state all --limit 300 "
             "--json number,title,state,headRefName,mergedAt,mergeCommit", timeout=180,
             product=product)
    try:
        data = json.loads(raw) if raw else []
    except Exception:
        data = []
    try:
        with open(PR_CACHE + f".{os.getpid()}", "w") as f:
            json.dump(data, f)
        os.replace(PR_CACHE + f".{os.getpid()}", PR_CACHE)
    except Exception:
        pass
    return data


# ---- slugs, verdicts, rounds ----------------------------------------------------------------
DATE_PREFIX = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-")
VERDICT_RE = re.compile(r"APPROVED|CHANGES REQUESTED|BOUNCE|REVISE", re.IGNORECASE)
H1_ALIAS = re.compile(r"^#\s+([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*)\s*[—–-]")


def doc_slug(name):
    """2026-09-20-clean-floor.md → clean-floor; preflight files are not documents."""
    base = name[:-3] if name.endswith(".md") else name
    m = DATE_PREFIX.match(base)
    return base[m.end():] if m else base


def verdict_of(blob):
    if not blob:
        return ""
    m = VERDICT_RE.search(blob[:20000].decode("utf-8", "replace"))
    return m.group(0).upper() if m else ""


def newest_review(names, patterns):
    """(filename, round) of the highest-numbered review file matching any pattern."""
    best, best_n = None, -1
    for n in names:
        for rx in patterns:
            m = rx.fullmatch(n)
            if m:
                r = int(m.group("r"))
                if r > best_n:
                    best, best_n = n, r
                break
    return best, best_n


def rx_review(*prefixes):
    return [re.compile(p + r"-review-r(?P<r>\d+)\.md") for p in prefixes]


TASK_HEAD = re.compile(r"^#{2,4}\s+(?:Task\s+|T(?=\d))(\d+[a-z]?)\b(?P<rest>[^\n]*)",
                       re.MULTILINE | re.IGNORECASE)
BRANCH_STEM = re.compile(r"^(.*?)-(?:t\d+[a-z]?|w\d+[a-z]?)$", re.IGNORECASE)
BRANCH_TOKEN = re.compile(r"(?:worktree-m-|cloud/)([A-Za-z0-9][\w.]*(?:-[\w.]+)*)")
TASK_TOKEN = re.compile(r"\bT(\d+[a-z]?)\b")
CONSUMES_LINE = re.compile(r"consumes", re.IGNORECASE)

GOAL_HEAD = re.compile(r"^GOAL\s+(\d+)\s*[—–-]+\s*([^\n]*)", re.MULTILINE)


def natural_key(tid):
    m = re.match(r"T(\d+)([a-z]*)", tid)
    return (int(m.group(1)), m.group(2)) if m else (10 ** 6, tid)


def plan_tasks(text, slug):
    """[(Tn, [branch-id …], declared_done)] — the plan's task headings.

    The dispatch table is the authority on branch names (FACT-6's tasks live on `cloud/fact6-t*`,
    not `cloud/clean-floor-t*`), and where a plan names branches per wave rather than per task
    (`worktree-m-rename-<task>`) the common stem of the names it does give carries the same
    answer. `<slug>-t<n>` is the fallback when it names neither.
    """
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
        found = BRANCH_TOKEN.findall(line)
        tasks = {"T" + t.lower() for t in TASK_TOKEN.findall(line)}
        if len(tasks) != 1:
            continue
        tid = next(iter(tasks))
        for b in found:
            if b.lower().endswith(tid.lower()):
                stated.setdefault(tid, b)
    # the stem is read out of the dispatch table's own rows only. Branch names in prose are
    # dependencies, not ownership: MOBILE-1's plan names `cloud/brand-t7` nine times as a
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
    """(rows, broken) from `docs/research/feature-matrix.md`'s "All requirements" table."""
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


def discover(product=None, checked_file=None):
    """The raw evidence `backlog.py ingest` needs, gathered fresh from the product repo and gh."""
    product = product or env.load_product()
    branches = remote_branches(product=product)
    prs = pr_list(product=product)
    pr_by_head = {}
    for p in prs:
        pr_by_head.setdefault(p.get("headRefName") or "", []).append(p)
    merged = {p["number"]: p["mergeCommit"]["oid"]
              for p in prs if p.get("state") == "MERGED" and p.get("mergeCommit")}

    main_trees = read_trees([f"origin/main:{PLANS_DIR}", f"origin/main:{SPECS_DIR}",
                             f"origin/main:{REVIEWS_DIR}", f"origin/main:{BRIEFS_DIR}"],
                            product=product)
    main_plans = main_trees[f"origin/main:{PLANS_DIR}"]
    main_specs = main_trees[f"origin/main:{SPECS_DIR}"]
    main_reviews = set(main_trees[f"origin/main:{REVIEWS_DIR}"])
    main_briefs = main_trees[f"origin/main:{BRIEFS_DIR}"]

    # ---- discovery: spec/plan branches, plans and specs already on main
    inits = {}

    def init(slug):
        return inits.setdefault(slug, {"slug": slug, "spec_branch": None, "plan_branch": None,
                                       "plan_doc": None, "spec_doc": None})

    for b in branches:
        if b.startswith("cloud/spec-"):
            init(b[len("cloud/spec-"):])["spec_branch"] = b
        elif b.startswith("cloud/plan-"):
            init(b[len("cloud/plan-"):])["plan_branch"] = b
    for name in main_plans:
        if name.startswith("preflight-") or not name.endswith(".md"):
            continue
        init(doc_slug(name))["plan_doc"] = (f"origin/main:{PLANS_DIR}/{name}", main_plans[name])
    for name in main_specs:
        if not name.endswith(".md"):
            continue
        slug = doc_slug(name)
        init(slug)["spec_doc"] = (f"origin/main:{SPECS_DIR}/{name}", main_specs[name])

    # ---- trees: reviews/plans/specs on every spec/plan branch we found
    doc_branches = [it[k] for it in inits.values() for k in ("spec_branch", "plan_branch") if it[k]]
    tree_paths = [f"origin/{b}:{REVIEWS_DIR}" for b in doc_branches]
    tree_paths += [f"origin/{b}:{PLANS_DIR}" for b in doc_branches]
    tree_paths += [f"origin/{b}:{SPECS_DIR}" for b in doc_branches]
    trees = read_trees(tree_paths, product=product)

    plan_req, spec_req = {}, {}
    for it in inits.values():
        slug = it["slug"]
        if not it["plan_doc"]:
            for b in (it["plan_branch"], it["spec_branch"]):
                if not b:
                    continue
                t = trees.get(f"origin/{b}:{PLANS_DIR}", {})
                hit = next((n for n in t if n.endswith(f"-{slug}.md") and not n.startswith("preflight-")), None)
                if hit:
                    it["plan_doc"] = (f"origin/{b}:{PLANS_DIR}/{hit}", t[hit])
                    break
        if not it["spec_doc"]:
            b = it["spec_branch"] or it["plan_branch"]
            t = trees.get(f"origin/{b}:{SPECS_DIR}", {}) if b else {}
            hit = next((n for n in t if n.endswith(f"-{slug}.md")), None)
            if hit:
                it["spec_doc"] = (f"origin/{b}:{SPECS_DIR}/{hit}", t[hit])
        if it["plan_doc"]:
            plan_req[slug] = it["plan_doc"][1]
        if it["spec_doc"]:
            spec_req[slug] = it["spec_doc"][1]

    for it in inits.values():
        for kind in ("spec", "plan"):
            doc = it.get(f"{kind}_doc")
            rev = doc[0].split(":", 1)[0] if doc else None
            it[f"{kind}_on_main"] = rev == "origin/main"
            it[f"{kind}_carrier"] = (it[f"{kind}_branch"] or
                                     (rev[len("origin/"):] if rev and rev != "origin/main" else None))

    # ---- the review files each row needs a verdict from
    wanted = {}
    for it in inits.values():
        slug = it["slug"]
        for kind in ("spec", "plan"):
            branch = it[f"{kind}_carrier"]
            if not branch:
                continue
            names = trees.get(f"origin/{branch}:{REVIEWS_DIR}", {})
            pats = rx_review(re.escape(f"{kind}-{slug}"), re.escape(slug), re.escape(f"{slug}-{kind}"))
            f, r = newest_review(names, pats)
            if f:
                wanted.setdefault(slug, {})[kind] = (f, r, names[f])

    blobs = read_blobs(list(plan_req.values()) + list(spec_req.values()) +
                       [v[2] for d in wanted.values() for v in d.values()], product=product)

    brief_dirs = {}
    for slug in inits:
        hit = [d for d in main_briefs if d == slug or d.endswith("-" + slug)]
        if hit:
            brief_dirs[slug] = f"origin/main:{BRIEFS_DIR}/{sorted(hit)[-1]}"
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
        it["tasks_parsed"] = plan_tasks(text, slug) if text else []
        it["edges"] = consumes_edges(text) if text else {}
        for _tid, cand, _d in it["tasks_parsed"]:
            claimed.update(f"cloud/{c}" for c in cand if f"cloud/{c}" in branches)

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
            br = next((f"cloud/{c}" for c in cand if f"cloud/{c}" in branches), None)
            if not br and alias_stem and alias_stem != slug:
                weak = f"{alias_stem}-{tid.lower()}"
                if f"cloud/{weak}" in branches and f"cloud/{weak}" not in claimed:
                    br, cand = f"cloud/{weak}", cand + [weak]
                    claimed.add(br)
            cand_prs = [p for c in cand for p in pr_by_head.get(f"cloud/{c}", []) + pr_by_head.get(f"worktree-m-{c}", [])]
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
                task_tree_paths.append(f"origin/{br}:{REVIEWS_DIR}")
        task_rows[slug] = rows
    task_trees = read_trees(sorted(set(task_tree_paths)), product=product)

    task_blob_req = []
    for slug, rows in task_rows.items():
        for row in rows:
            if not row["branch"]:
                continue
            names = task_trees.get(f"origin/{row['branch']}:{REVIEWS_DIR}", {})
            ids = row["cand"] + [row["branch"].split("/", 1)[-1]]
            pats = [p for i in ids for p in rx_review(re.escape(i), re.escape(f"hotfix-{i}"), re.escape(f"review-{i}"))]
            f, r = newest_review(names, pats)
            row["review"] = (r, None, f) if f else None
            if f:
                task_blob_req.append(names[f])
    task_blobs = read_blobs(task_blob_req, product=product)
    for rows in task_rows.values():
        for row in rows:
            rev = row.pop("review", None)
            if rev:
                r, _v, f = rev
                names = task_trees.get(f"origin/{row['branch']}:{REVIEWS_DIR}", {})
                row["review"] = (r, verdict_of(task_blobs.get(names.get(f))))
            else:
                row["review"] = None

    features = {}
    for it in inits.values():
        slug = it["slug"]

        def review_tuple(kind):
            hit = wanted.get(slug, {}).get(kind)
            if not hit:
                return None
            f, r, sha = hit
            return (r, verdict_of(blobs.get(sha)), f)

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

    # ---- the parity matrix, for Stories
    matrix_text = sh(f"git show origin/main:{MATRIX_PATH}", product=product)
    rows, _broken = parse_rows(matrix_text) if matrix_text else ([], [])
    stories = {r["id"]: {"status": r["status"], "impl": _matrix_paths(r["impl"]),
                         "test": _matrix_paths(r["tests"]), "area": r["area"], "milestone": r["ms"],
                         "cap": r["cap"]}
              for r in rows}

    # ---- deploy shas (tick-tables.py's approach): newest successful deploy-prod run; newest
    # `ci` run on main whose deploy-dev job succeeded.
    prod_runs = _gh_json("run list --workflow deploy-prod.yml --limit 15 "
                         "--json headSha,conclusion,updatedAt", product=product)
    prod = next((r for r in prod_runs if r.get("conclusion") == "success"), None)
    main_runs = _gh_json("run list --branch main --workflow ci.yml --limit 40 "
                         "--json headSha,conclusion,updatedAt,status,databaseId", product=product)
    dev = None
    for r in main_runs:
        if r.get("status") != "completed":
            continue
        jobs = _gh_json(f"api repos/{product.repo_slug}/actions/runs/{r['databaseId']}/jobs?per_page=60",
                        product=product)
        job_list = jobs.get("jobs", []) if isinstance(jobs, dict) else []
        if any(j.get("name") == "deploy-dev" and j.get("conclusion") == "success" for j in job_list):
            dev = r
            break

    checked_file = checked_file if checked_file is not None else CHECKED_FILE
    checked = set()
    if os.path.exists(checked_file):
        with open(checked_file) as f:
            for line in f:
                m = re.match(r"\s*#?(\d+)", line)
                if m:
                    checked.add(int(m.group(1)))

    main_sha = sh("git rev-parse origin/main", product=product)

    return {
        "features": features,
        "stories": stories,
        "prod_sha": prod["headSha"] if prod else None,
        "dev_sha": dev["headSha"] if dev else None,
        "checked": checked,
        "main_sha": main_sha or None,
        "merged": merged,
        "branches": sorted(branches),
    }


# A spec filename to look for, not per-product config — no obvious Product field for it.
# TODO(config): no Product field for this yet.
DESIGN_SPEC_NAME = "design.md"
SDD_DECISIONS = ".sdd-input/decisions.md"
HOTFIX_RE = re.compile(r"^(?:hotfix-.+-report|ci-diag-.+)\.md$")


def migrate_sources(product=None, design_spec_name=None):
    """Raw content `backlog.py migrate` needs beyond discover(): every spec doc on origin/main
    (the design spec's D-row table lives among them), `.sdd-input/decisions.md`, and every
    hotfix/ci-diag report on origin/main and on cloud/* branches whose PR is not merged. All
    git/gh access `migrate` needs stays behind this one function, same as `discover()`.
    """
    product = product or env.load_product()
    design_spec_name = design_spec_name if design_spec_name is not None else DESIGN_SPEC_NAME
    branches = remote_branches(product=product)
    prs = pr_list(product=product)
    pr_by_head = {}
    for p in prs:
        pr_by_head.setdefault(p.get("headRefName") or "", []).append(p)
    open_branches = sorted(
        b for b in branches
        if b.startswith("cloud/")
        and not any(p.get("state") == "MERGED" for p in pr_by_head.get(b, []))
    )

    main_specs_tree = list_tree("origin/main", SPECS_DIR, product=product)
    spec_refs = [f"origin/main:{SPECS_DIR}/{n}" for n in main_specs_tree if n.endswith(".md")]
    main_spec_texts = {os.path.basename(r): t for r, t in read_refs(spec_refs, product=product).items()}

    revs = ["origin/main"] + [f"origin/{b}" for b in open_branches]
    sdd_trees = read_trees([f"{r}:.sdd-input" for r in revs], product=product)
    main_names = {n for n in sdd_trees.get("origin/main:.sdd-input", {}) if HOTFIX_RE.match(n)}
    hotfix_refs = [f"origin/main:.sdd-input/{n}" for n in main_names]
    for r in revs[1:]:
        names = sdd_trees.get(f"{r}:.sdd-input", {})
        for n in names:
            # a name already on origin/main is inherited history, not this branch's own
            # unmerged report — counting it here would multiply one main-side report by
            # every stale branch that happens to carry it too.
            if HOTFIX_RE.match(n) and n not in main_names:
                hotfix_refs.append(f"{r}:.sdd-input/{n}")

    return {
        "design_spec_text": main_spec_texts.get(design_spec_name),
        "sdd_decisions_text": read_ref(f"origin/main:{SDD_DECISIONS}", product=product),
        "main_spec_texts": main_spec_texts,
        "hotfix_texts": read_refs(hotfix_refs, product=product),
        "open_branches": open_branches,
    }


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

    `spec`/`plan` are {"exists": bool, "review": (round, verdict)|None, "approved": bool}.
    `tasks` is a list of per-task state strings ("New"/"Active"/"Closed").
    """
    total = len(tasks)
    closed = sum(1 for t in tasks if t == "Closed")
    if total and closed == total:
        return "on-prod" if on_prod else "landed"
    if total and (closed or any(t == "Active" for t in tasks)):
        return f"building {closed}/{total}"
    if plan["approved"]:
        return "plan-approved"
    if plan["exists"]:
        if plan["review"] and plan["review"][1] != "APPROVED":
            return f"plan-review r{plan['review'][0]}"
        return "plan-draft"
    if spec["approved"]:
        return "spec-approved"
    if spec["exists"]:
        if spec["review"] and spec["review"][1] != "APPROVED":
            return f"spec-review r{spec['review'][0]}"
        return "spec-draft"
    return "card"


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
    string (a plain string not resolving to a known item id)."""
    open_blockers = []
    for b in blocked_by or []:
        if b in state_by_id:
            if state_by_id[b] != "Closed":
                open_blockers.append(b)
        else:
            open_blockers.append(b)
    return bool(open_blockers), open_blockers


# -------------------------------------------------------------------------------------- cli ----
def load(fresh=False, product=None, checked_file=None):
    """discover(), through the 3-minute cache at EVIDENCE_CACHE. Shared by the CLI and by
    `backlog.py ingest`, so two calls a few seconds apart cost one round-trip to gh/git."""
    if not fresh and os.path.exists(EVIDENCE_CACHE):
        if time.time() - os.path.getmtime(EVIDENCE_CACHE) < EVIDENCE_TTL:
            with open(EVIDENCE_CACHE) as f:
                data = json.load(f)
            data["checked"] = set(data["checked"])
            return data

    data = discover(product=product, checked_file=checked_file)
    text = json.dumps({**data, "checked": sorted(data["checked"])}, indent=2, sort_keys=True) + "\n"
    try:
        with open(EVIDENCE_CACHE + f".{os.getpid()}", "w") as f:
            f.write(text)
        os.replace(EVIDENCE_CACHE + f".{os.getpid()}", EVIDENCE_CACHE)
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
