"""A spec or plan merged by the PR lane is spec-on-main / plan-on-main — never the Feature's
landing. Native PR landing squash-merged eight spec/plan PRs whose subjects named their Feature
(`docs(plan): F-0047 — …`, `plan(OPS-1): the F-0037 plan …`, `plan(F-0019): …`), and the record
marked every one of them `landed`/`Resolved`: the feeder, which launches coders only at
plan-approved/building, then had nothing to do. And a plan whose file is date-prefixed
(`2026-09-20-free-plan.md` for F-0019) minted no Task cards, because the minter looked the plan
up by the id-shaped file name alone.

The paths a commit touched and the lane branch its PR came from decide — never the subject."""
import json
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from asf import env
from asf.evidence import evidence
from asf.record import frontmatter, ingest, plan_tasks
from asf.record.index import do_index

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_NOSYSTEM": "1"}
PREFIXES = {"code": "cloud/", "spec": "cloud/spec-", "plan": "cloud/plan-", "legacy": []}
FOLDER_OF = {"epic": "epics", "feature": "features", "story": "stories", "task": "tasks",
             "bug": "bugs", "decision": "decisions", "rule": "rules"}
PLAN = "# Plan\n\n### Task 1: the reader\nwrites: src/reader.ts\n\n### Task 2: the table\nwrites: src/table.ts\n"


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, text=True,
                          env=dict(os.environ, **GIT_ENV)).stdout.strip()


class Product:
    """A bare origin whose main carries the lane's squash commits, a clone of it as the product
    checkout, and an empty record beside it."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="doc_lane_")
        self.origin = os.path.join(self.tmp, "origin.git")
        self.repo = os.path.join(self.tmp, "product")
        self.backlog = os.path.join(self.tmp, "backlog")
        self.work = os.path.join(self.tmp, "work")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.origin], check=True,
                       env=dict(os.environ, **GIT_ENV))
        subprocess.run(["git", "clone", "-q", self.origin, self.work], check=True,
                       capture_output=True, env=dict(os.environ, **GIT_ENV))
        git(self.work, "checkout", "-q", "-b", "main")
        self.sha = {}
        self.commit("init", {"README.md": "x"})
        os.makedirs(self.backlog)
        for folder in FOLDER_OF.values():
            os.makedirs(os.path.join(self.backlog, folder))

    def commit(self, subject, files, key=None):
        for path, text in files.items():
            full = os.path.join(self.work, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(text)
        git(self.work, "add", ".")
        git(self.work, "commit", "-q", "-m", subject)
        self.sha[key or subject] = git(self.work, "rev-parse", "HEAD")
        return self.sha[key or subject]

    def publish(self):
        git(self.work, "push", "-q", "-f", "origin", "main")
        if not os.path.isdir(self.repo):
            subprocess.run(["git", "clone", "-q", self.origin, self.repo], check=True,
                           capture_output=True, env=dict(os.environ, **GIT_ENV))
        else:
            git(self.repo, "fetch", "-q", "origin")

    def product(self):
        return env.Product("sample", {
            "repo_dir": self.repo, "repo_slug": "sample/product", "main": "main",
            "backlog_dir": self.backlog, "ci": "none",
            "conventions": {"branch_prefixes": PREFIXES}})

    def discover(self, prs):
        with mock.patch.object(evidence, "pr_list", return_value=prs), \
                mock.patch.object(evidence, "_gh_json", return_value=[]):
            return evidence.discover(product=self.product(),
                                     checked_file=os.path.join(self.tmp, "none.txt"))

    def item(self, id_, type_, parent=None, typed_lines=(), state="New", stage=None,
             decided=True):
        lines = [f"id: {id_}", f"type: {type_}", f"title: {id_} item"]
        if decided:
            lines.append("decided: true")
        if parent:
            lines.append(f"parent: {parent}")
        machine = ["schema_version: 1", f"state: {state}"] + ([f"stage: {stage}"] if stage else []) + [
            "stage_since: 2026-01-01T00:00:00Z", "updated: 2026-01-01T00:00:00Z"]
        lines += list(typed_lines) + ["# ---- machine ----"] + machine
        with open(os.path.join(self.backlog, FOLDER_OF[type_], f"{id_}.md"), "w") as f:
            f.write("---\n" + "\n".join(lines) + "\n---\n## Description\n\n## History\n"
                    "- 2026-01-01: created\n")

    def meta(self, id_, type_="feature"):
        with open(os.path.join(self.backlog, FOLDER_OF[type_], f"{id_}.md")) as f:
            return frontmatter.parse(f.read(), path=f"{id_}.md")[0]

    def record_step(self, ev):
        """The tick's record step, as run_step0 runs it: ingest, plan-tasks, index."""
        with mock.patch.object(ingest.evidence, "load", return_value=ev):
            assert ingest.cmd_ingest(types.SimpleNamespace(fresh=False), self.backlog) == 0
        made = plan_tasks.mint_plan_tasks(self.backlog, self.product(), ev, out=lambda *_a: None)
        do_index(self.backlog)
        return made

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def merged_pr(number, head, title, sha):
    return {"number": number, "title": title, "body": "", "state": "MERGED", "headRefName": head,
            "mergedAt": "2026-09-24T06:11:00Z", "mergeCommit": {"oid": sha}}


class DocLaneMergeIsNotALanding(unittest.TestCase):
    def setUp(self):
        self.p = Product()
        self.addCleanup(self.p.close)
        p = self.p
        # the eight squash subjects' shapes, each a docs-only diff
        p.commit("plan(F-0019): FREE-1 — seven tasks (#737)",
                 {"docs/plans/2026-09-20-free-plan.md": "# FREE-1 — Free plan\n\n" + PLAN}, "f19")
        p.commit("plan(OPS-1): the F-0037 plan — nine tasks (#739)",
                 {"docs/plans/2026-09-21-fleet-ops.md": "# OPS-1 — ops\n\n" + PLAN}, "f37")
        p.commit("docs(plan): F-0047 — the plan for the Stripe mirror (#743)",
                 {"docs/plans/f-0047.md": "# F-0047 — money\n\n" + PLAN}, "f47")
        p.commit("docs(spec): F-0129 — pricing page (#745)",
                 {"docs/specs/f-0129.md": "# F-0129 — pricing\n"}, "f129")
        # a subject in no lane format at all: only its paths say it is a document
        p.commit("chore: F-0061 — status page plan, cut against head (#744)",
                 {"docs/plans/f-0061.md": "# F-0061 — status\n\n" + PLAN}, "f61")
        # F-0003's spec sits on its lane branch under one slug (`avatar`); its plan landed on
        # main under another (`2026-09-20-avatar-system.md`) from a lane branch and a PR title
        # that name no id — only the card's typed `links.plan` ties the two together
        git(p.work, "checkout", "-q", "-b", "side")
        p.commit("spec: avatar", {"docs/specs/2026-09-19-avatar.md": "# AVATAR-1 — avatars\n"})
        git(p.work, "push", "-q", "origin", "side:refs/heads/cloud/spec-avatar")
        git(p.work, "checkout", "-q", "main")
        p.commit("docs(plan): AVATAR-1 implementation plan — 6 tasks (#747)",
                 {"docs/plans/2026-09-20-avatar-system.md": "# AVATAR-1 — avatars\n\n" + PLAN},
                 "f3")
        # the specs those plans were cut from, on the trunk: a plan is approved only on an
        # approved spec (F-0003's spec is still on its lane branch, and stays a spec row)
        p.commit("docs(spec): three specs", {"docs/specs/f-0019.md": "# F-0019 — free\n",
                                             "docs/specs/f-0037.md": "# F-0037 — ops\n",
                                             "docs/specs/f-0061.md": "# F-0061 — status\n"},
                 "specs")
        # real work: a Task's code, and a Feature named by a code commit
        p.commit("feat(reader): the reader for T-0900 (#750)", {"src/reader.ts": "x"}, "t900")
        p.commit("fix: wire F-0077 through (#751)", {"src/wire.ts": "x", "docs/plans/x.md": "y"},
                 "f77")
        p.publish()
        s = p.sha
        self.prs = [
            merged_pr(737, "cloud/plan-F-0019", "F-0019 — Free plan: a working bot", s["f19"]),
            merged_pr(739, "cloud/plan-F-0037", "F-0037 — the monitoring solution", s["f37"]),
            merged_pr(743, "cloud/plan-F-0047", "F-0047 — money: the Stripe mirror", s["f47"]),
            merged_pr(745, "cloud/spec-F-0129", "F-0129 — Pricing page", s["f129"]),
            merged_pr(744, "cloud/plan-F-0061", "F-0061 — the public status page", s["f61"]),
            merged_pr(750, "cloud/t-0900-reader", "T-0900: the reader", s["t900"]),
            merged_pr(747, "cloud/plan-avatar-system", "AVATAR-1 implementation plan", s["f3"]),
        ]
        p.item("F-0003", "feature", parent="E-0001", stage="plan-draft",
               typed_lines=["links:", "  spec: docs/specs/2026-09-19-avatar.md",
                            "  plan: docs/plans/2026-09-20-avatar-system.md"])
        p.item("E-0001", "epic")
        # F-0019 links nothing: its date-prefixed plan is reached through its lane PR alone
        p.item("F-0019", "feature", parent="E-0001", state="Resolved", stage="landed")
        # F-0037 links its date-prefixed plan, as a migrated card does
        p.item("F-0037", "feature", parent="E-0001", state="Resolved", stage="landed",
               typed_lines=["links:", "  plan: docs/plans/2026-09-21-fleet-ops.md"])
        # F-0047 matches an old spec by its legacy alias first; its own lane landed its plan
        p.commit("docs(spec): the P2-A money spec", {"docs/specs/2026-09-14-p2-a-money.md":
                                                     "# P2-A — money\n"}, "p2a")
        p.publish()
        p.item("F-0047", "feature", parent="E-0001", state="Resolved", stage="landed",
               typed_lines=["legacy_id: P2-A"])
        p.item("F-0061", "feature", parent="E-0001", state="Resolved", stage="landed")
        p.item("F-0129", "feature", parent="E-0001", state="Resolved", stage="landed")
        p.item("F-0077", "feature", parent="E-0001")
        p.item("F-0090", "feature", parent="E-0001")
        # a migrated, undecided milestone linking an old plan: no fresh Task cards for it
        p.commit("docs: the M3 plan", {"docs/plans/2026-09-03-m3-infra.md": "# M3 — infra\n\n" + PLAN})
        p.publish()
        p.item("F-0026", "feature", parent="E-0001", stage="plan-approved", decided=False,
               typed_lines=["links:", "  plan: docs/plans/2026-09-03-m3-infra.md"])
        # removed and moved-away cards, decided and linking a landed plan: never touched
        p.item("F-0010", "feature", parent="E-0001", stage="plan-approved",
               typed_lines=["moved_to: other:F-0049", 'removed: "moved"', "links:",
                            "  plan: docs/plans/2026-09-03-m3-infra.md"])
        p.item("F-0084", "feature", parent="E-0001", stage="plan-approved",
               typed_lines=['removed: "superseded"', "links:",
                            "  plan: docs/plans/2026-09-20-free-plan.md"])
        p.item("F-0085", "feature", parent="E-0001", stage="plan-approved",
               typed_lines=["moved_to: other:F-0050", "links:",
                            "  plan: docs/plans/2026-09-21-fleet-ops.md"])
        p.item("T-0900", "task", parent="F-0090")

    def test_docs_only_commits_and_lane_prs_name_no_landing(self):
        ev = self.p.discover(self.prs)
        ids = ev["ids"]
        for fid in ("F-0019", "F-0037", "F-0047", "F-0061", "F-0129"):
            self.assertIsNone((ids.get(fid) or {}).get("commit"), fid)
        # a real Task merge, and a code commit naming a Feature, still count
        self.assertEqual(ids["T-0900"]["commit"], self.p.sha["t900"])
        self.assertEqual(ids["F-0077"]["commit"], self.p.sha["f77"])
        self.assertEqual(ev["lane_docs"]["F-0019"],
                         {"spec": [], "plan": ["docs/plans/2026-09-20-free-plan.md"], "prs": [737]})

    def test_the_record_step_re_derives_plan_approved_and_mints_the_tasks(self):
        ev = self.p.discover(self.prs)
        made = self.p.record_step(ev)
        stages = {f: (self.p.meta(f)["state"], self.p.meta(f)["stage"])
                  for f in ("F-0019", "F-0037", "F-0047", "F-0061", "F-0129")}
        self.assertEqual(stages, {"F-0019": ("Active", "plan-approved"),
                                  "F-0037": ("Active", "plan-approved"),
                                  "F-0047": ("Active", "plan-approved"),
                                  "F-0061": ("Active", "plan-approved"),
                                  "F-0129": ("Active", "spec-approved")})
        parents = {}
        for tid in made:
            parents.setdefault(self.p.meta(tid, "task")["parent"], []).append(tid)
        # both date-prefixed plans — one through its lane PR, one through links.plan — mint
        self.assertEqual({f: len(t) for f, t in parents.items()},
                         {"F-0003": 2, "F-0019": 2, "F-0037": 2, "F-0047": 2, "F-0061": 2})
        # its old plan landed, but no spec is anywhere: a card, not plan-approved
        self.assertEqual(self.p.meta("F-0026")["stage"], "card")
        self.assertEqual(self.p.meta(parents["F-0019"][0], "task")["links"],
                         {"plan": "docs/plans/2026-09-20-free-plan.md"})
        # real work still lands
        self.assertEqual(self.p.meta("T-0900", "task")["state"], "Closed")
        self.assertEqual(self.p.meta("F-0077")["stage"], "landed")
        self.assertEqual(self.p.meta("F-0090")["stage"], "landed")

    def test_a_typed_links_plan_on_main_on_a_spec_off_the_trunk_lands_the_spec_first(self):
        ev = self.p.discover(self.prs)
        self.assertEqual(ev["features"]["avatar"]["plan"], None)  # the spec's slug has no plan
        self.p.record_step(ev)
        m = self.p.meta("F-0003")
        # the plan is on the trunk, the spec only on its lane branch: no coder starts on it
        self.assertEqual((m["state"], m["stage"]), ("Active", "spec-draft"))
        self.assertIn("plan on origin/main", m["evidence"])
        self.assertIn("spec on cloud/spec-avatar", m["evidence"])
        with open(os.path.join(self.p.backlog, "index.json")) as f:
            index = json.load(f)
        from asf.feeder import rows
        got = [r for r in rows.candidates(index, self.p.product(), []) if r.feature_id == "F-0003"]
        self.assertEqual([(r.kind, r.branch) for r in got],
                         [(rows.STARVED_SPEC, "cloud/spec-avatar")])
        self.assertIn("land the existing spec", got[0].reason)

    def test_removed_or_moved_features_mint_nothing_and_keep_their_machine_block(self):
        before = {}
        for fid in ("F-0010", "F-0084", "F-0085"):
            with open(os.path.join(self.p.backlog, "features", f"{fid}.md")) as f:
                before[fid] = f.read()
        made = self.p.record_step(self.p.discover(self.prs))
        parents = {self.p.meta(t, "task")["parent"] for t in made}
        self.assertFalse(parents & {"F-0010", "F-0084", "F-0085"})
        for fid, text in before.items():
            with open(os.path.join(self.p.backlog, "features", f"{fid}.md")) as f:
                self.assertEqual(f.read(), text, fid)

    def test_rerunning_the_record_step_is_a_no_op(self):
        ev = self.p.discover(self.prs)
        self.p.record_step(ev)
        # the second pass reads the Tasks the first one minted ("0/2 tasks Closed"); from then
        # on the same evidence derives the same record, byte for byte
        self.p.record_step(ev)
        snap = {}
        for folder in ("features", "tasks"):
            d = os.path.join(self.p.backlog, folder)
            for n in sorted(os.listdir(d)):
                with open(os.path.join(d, n)) as f:
                    snap[(folder, n)] = f.read()
        self.assertEqual(self.p.record_step(ev), [])
        after = {}
        for folder in ("features", "tasks"):
            d = os.path.join(self.p.backlog, folder)
            for n in sorted(os.listdir(d)):
                with open(os.path.join(d, n)) as f:
                    after[(folder, n)] = f.read()
        self.assertEqual(after, snap)
        self.assertEqual(self.p.meta("F-0019")["stage"], "plan-approved")


def run_line(job, item, branch, **fields):
    """A launch line and, when ``fields`` are given, the line that marks it (harvested, lane)."""
    lines = [{"job": job, "item": item, "branch": branch, "pid": 1, "product": "sample",
              "started": "2026-09-24T06:00:00Z"}]
    if fields:
        lines.append(dict(fields, job=job))
    return lines


class LandedIsTheMergeFact(unittest.TestCase):
    """The merge fact on the run line lands an item — never a commit subject, never the branch
    head's ancestry. A squash merge's sha is no descendant of the branch head and its subject
    names nothing, yet its Task lands; a spec/plan lane's merge lands its document, never its
    Feature (I10); and a date-prefixed plan a fast-forward plan lane landed (no PR at all) mints
    its Tasks (F-0123)."""

    def setUp(self):
        self.p = Product()
        self.addCleanup(self.p.close)
        self.home = os.path.join(self.p.tmp, "asf-home")
        patcher = mock.patch.object(env, "ASF_HOME", self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        p = self.p
        p.commit("docs(spec): the spec", {"docs/specs/f-0200.md": "# F-0200 — reader\n"}, "spec")
        # the fast-forward plan lane's tip: a date-prefixed plan, a subject that names nothing
        p.commit("the plan", {"docs/plans/2026-09-24-reader-plan.md": "# Reader plan\n\n" + PLAN},
                 "plan")
        # the squash merge of the Task's PR: its subject names no item
        p.commit("Reader (#12)", {"src/reader.ts": "x"}, "squash")
        p.publish()
        p.item("E-0001", "epic")
        p.item("F-0200", "feature", parent="E-0001")

    def ledger(self, *runs):
        path = os.path.join(self.home, "state", "sample", "sessions.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            for lines in runs:
                for line in lines:
                    f.write(json.dumps(line) + "\n")

    def test_a_squash_merge_on_the_run_line_lands_its_task(self):
        s = self.p.sha
        self.p.item("T-0901", "task", parent="F-0200")
        self.p.item("T-0902", "task", parent="F-0200")
        self.ledger(run_line("coder-t-0901", "T-0901", "cloud/t-0901", harvested=s["squash"]),
                    run_line("coder-t-0902", "T-0902", "cloud/t-0902",
                             lane={"state": "MERGED", "sha": s["squash"], "pr": 12}),
                    run_line("coder-t-0903", "T-0903", "cloud/t-0903", harvested="superseded"))
        ev = self.p.discover([])
        self.assertEqual(ev["ids"]["T-0901"]["commit"], s["squash"])
        self.assertEqual((ev["ids"]["T-0902"]["commit"], ev["ids"]["T-0902"]["pr"]),
                         (s["squash"], 12))
        self.assertNotIn("T-0903", ev["ids"])  # archived: nothing landed
        self.p.record_step(ev)
        self.assertEqual(self.p.meta("T-0901", "task")["state"], "Closed")
        self.assertIn(f"merge {s['squash'][:7]} of cloud/t-0901 lands T-0901",
                      self.p.meta("T-0901", "task")["evidence"])

    def test_a_plan_lane_merge_lands_the_plan_not_the_feature_and_mints_its_tasks(self):
        s = self.p.sha
        self.ledger(run_line("plan-f-0200", "F-0200", "cloud/plan-F-0200", harvested=s["plan"]))
        ev = self.p.discover([])
        self.assertIsNone((ev["ids"].get("F-0200") or {}).get("commit"))
        self.assertEqual(ev["lane_docs"]["F-0200"]["plan"], ["docs/plans/2026-09-24-reader-plan.md"])
        made = self.p.record_step(ev)
        self.assertEqual(len(made), 2)
        self.assertEqual({self.p.meta(t, "task")["parent"] for t in made}, {"F-0200"})
        m = self.p.meta("F-0200")
        self.assertEqual((m["state"], m["stage"]), ("Active", "plan-approved"))


class DocsOnlyTests(unittest.TestCase):
    DIRS = ("docs/specs/", "docs/plans/", "docs/reviews/")

    def test_only_documents(self):
        self.assertTrue(evidence.docs_only(["docs/plans/a.md", "docs/reviews/r.md"], self.DIRS))

    def test_any_code_path_is_a_landing(self):
        self.assertFalse(evidence.docs_only(["docs/plans/a.md", "src/a.ts"], self.DIRS))

    def test_unknown_paths_are_not_judged_docs_only(self):
        self.assertFalse(evidence.docs_only([], self.DIRS))

    def test_lane_kind_reads_the_products_prefixes(self):
        pre = evidence.branch_prefixes(env.Product("x", {"conventions": {"branch_prefixes": PREFIXES}}))
        self.assertEqual(evidence.lane_kind("cloud/plan-F-0019", pre), "plan")
        self.assertEqual(evidence.lane_kind("origin/cloud/spec-F-0129", pre), "spec")
        self.assertIsNone(evidence.lane_kind("cloud/t-0900-reader", pre))


if __name__ == "__main__":
    unittest.main()
