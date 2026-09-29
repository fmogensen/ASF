"""A PR closed without merging resets its item: the Task derives back to ready (New) with the
closed PR and its archive tag as provenance, the Feature is re-derived over it, and the registry
stops counting the item's old rounds, corrections, rulings and waits. ``hotfix/*`` and PRs still
open are untouched; a branch pushed past the closed head is new work, alive."""
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
from asf.record import ingest
from asf.workers import lifecycle
from tests.test_evidence import GIT_ENV, ProductRepo, git

PREFIXES = {"code": "cloud/", "fix": "cloud/fix-", "spec": "cloud/spec-", "plan": "cloud/plan-"}
HEAD = "a" * 40


def pr(n, branch, state, head=HEAD):
    return {"number": n, "headRefName": branch, "state": state, "headRefOid": head,
            "title": "", "body": "", "mergedAt": None, "mergeCommit": None}


class DeadBranchesTests(unittest.TestCase):
    def dead(self, heads, prs, archive=None):
        return evidence.dead_branches(heads, prs, archive, PREFIXES)

    def test_closed_unmerged_at_its_head_is_dead_with_its_archive_tag(self):
        out = self.dead({"cloud/T-0001": HEAD}, [pr(7, "cloud/T-0001", "CLOSED")], {7: HEAD})
        self.assertEqual(out, {"cloud/T-0001": {"pr": 7, "head": HEAD, "archive": "archive/pr-7"}})

    def test_no_archive_tag_is_still_dead_and_names_none(self):
        out = self.dead({"cloud/T-0001": HEAD}, [pr(7, "cloud/T-0001", "CLOSED")])
        self.assertEqual(out["cloud/T-0001"]["archive"], "")

    def test_the_archive_tag_stands_in_for_a_missing_pr_head(self):
        out = self.dead({"cloud/T-0001": HEAD}, [pr(7, "cloud/T-0001", "CLOSED", head=None)],
                        {7: HEAD})
        self.assertIn("cloud/T-0001", out)

    def test_open_merged_moved_hotfix_and_prless_branches_are_alive(self):
        heads = {"cloud/T-0001": HEAD, "cloud/T-0002": HEAD, "cloud/T-0003": "b" * 40,
                 "hotfix/T-0004": HEAD, "cloud/T-0005": HEAD, "archive/cloud/T-0006": HEAD}
        prs = [pr(1, "cloud/T-0001", "CLOSED"), pr(2, "cloud/T-0001", "OPEN"),  # reopened as #2
               pr(3, "cloud/T-0002", "MERGED"), pr(4, "cloud/T-0002", "CLOSED"),
               pr(5, "cloud/T-0003", "CLOSED"),                                  # pushed since
               pr(6, "hotfix/T-0004", "CLOSED"), pr(8, "archive/cloud/T-0006", "CLOSED")]
        self.assertEqual(self.dead(heads, prs), {})

    def test_a_push_committed_before_the_close_is_still_dead(self):
        moved = "c" * 40
        p = dict(pr(7, "cloud/T-0001", "CLOSED"), closedAt="2026-09-29T12:53:49Z")
        heads = {"cloud/T-0001": moved}
        self.assertEqual(evidence.closed_moved_heads(heads, [p], {7: HEAD}, PREFIXES), [moved])
        out = self.dead(heads, [p], {7: HEAD})
        self.assertEqual(out, {})  # no commit time known: alive
        out = evidence.dead_branches(heads, [p], {7: HEAD}, PREFIXES,
                                     {moved: "2026-09-29T14:50:46+02:00"})
        self.assertEqual(out["cloud/T-0001"], {"pr": 7, "head": moved, "archive": ""})
        out = evidence.dead_branches(heads, [p], {7: HEAD}, PREFIXES,
                                     {moved: "2026-09-29T15:00:00+02:00"})
        self.assertEqual(out, {})  # committed after the close: new work

    def test_a_pr_older_than_the_list_is_read_off_its_archive_tag(self):
        out = self.dead({"cloud/tm-t1": HEAD, "cloud/tm-t2": "d" * 40}, [], {653: HEAD})
        self.assertEqual(out, {"cloud/tm-t1": {"pr": 653, "head": HEAD,
                                               "archive": "archive/pr-653"}})

    def test_resets_name_the_item_unless_a_live_branch_still_does(self):
        dead = {"cloud/T-0001": {"pr": 7, "head": HEAD, "archive": ""},
                "cloud/plan-T-0002": {"pr": 8, "head": HEAD, "archive": ""}}
        out = evidence.resets_of(dead, live={"cloud/T-0002"})
        self.assertEqual(list(out), ["T-0001"])
        self.assertEqual(out["T-0001"]["branch"], "cloud/T-0001")


class RegistryResetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "sessions.jsonl")
        self.write(
            {"job": "t-0001", "item": "T-0001", "kind": "task", "branch": "cloud/T-0001",
             "started": "2026-09-20T10:00:00Z", "pid": 1, "ended": "2026-09-20T11:00:00Z",
             "end_reason": "finished", "rounds": 3, "harvest": "pr",
             "correction": {"kind": "review", "text": "fix C1", "at": "2026-09-20T12:00:00Z"}},
            {"job": "adjudicate-t-0001", "item": "T-0001", "kind": "adjudicate",
             "branch": "cloud/T-0001", "started": "2026-09-20T13:00:00Z", "pid": 2,
             "ended": "2026-09-20T13:30:00Z", "end_reason": "finished"})

    def write(self, *recs):
        with open(self.path, "a") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")

    def reset(self, **kw):
        return lifecycle.note_reset(self.path, "T-0001",
                                    {"pr": 7, "branch": "cloud/T-0001", "head": HEAD,
                                     "archive": "archive/pr-7"},
                                    now="2026-09-29T00:00:00Z", alive=lambda _pid: False, **kw)

    def test_before_the_reset_the_old_rounds_and_correction_count(self):
        self.assertEqual(lifecycle.rounds_of(self.path, "T-0001"), 3)
        self.assertIn("T-0001", lifecycle.corrections(self.path))
        self.assertEqual(lifecycle.attempts(self.path)["T-0001"], 2)

    def test_the_reset_clears_rounds_corrections_attempts_and_waits(self):
        self.assertTrue(self.reset())
        self.assertEqual(lifecycle.rounds_of(self.path, "T-0001"), 0)
        self.assertEqual(lifecycle.corrections(self.path), {})
        self.assertNotIn("T-0001", lifecycle.attempts(self.path))
        self.assertEqual(lifecycle.item_runs(self.path, "T-0001"), [])
        self.assertEqual(lifecycle.unlanded(self.path, alive=lambda _p: False), {})
        occ = lifecycle.occupancy(self.path, alive=lambda _p: False)
        self.assertNotIn("T-0001", occ["waiting_landing"])
        self.assertEqual(lifecycle.reset_of_branch(self.path, "cloud/T-0001")["head"], HEAD)
        # the runs themselves stay in the ledger (cost, history); only the item stops counting them
        self.assertIn("t-0001", lifecycle.runs(self.path))
        self.assertNotIn("reset-t-0001", lifecycle.runs(self.path))

    def test_the_reset_is_written_once_per_pr_and_head(self):
        self.assertTrue(self.reset())
        self.assertFalse(self.reset())
        with open(self.path) as f:
            self.assertEqual(sum('"reset"' in ln for ln in f), 1)

    def test_a_run_after_the_reset_counts_again(self):
        self.reset()
        self.write({"job": "t-0001", "item": "T-0001", "kind": "task", "branch": "cloud/T-0001",
                    "started": "2026-09-29T01:00:00Z", "pid": 3, "ended": "2026-09-29T02:00:00Z",
                    "end_reason": "finished", "rounds": 1,
                    "correction": {"kind": "gate", "text": "red", "at": "2026-09-29T03:00:00Z"}})
        self.assertEqual(lifecycle.rounds_of(self.path, "T-0001"), 1)
        self.assertEqual(lifecycle.corrections(self.path)["T-0001"]["text"], "red")

    def test_a_live_session_is_never_reset_under_it(self):
        self.write({"job": "t-0001", "item": "T-0001", "kind": "task", "branch": "cloud/T-0001",
                    "started": "2026-09-28T01:00:00Z", "pid": 4})
        self.assertFalse(lifecycle.note_reset(
            self.path, "T-0001", {"pr": 7, "branch": "cloud/T-0001", "head": HEAD},
            alive=lambda pid: pid == 4))


class IngestResetTests(unittest.TestCase):
    """End to end over a real origin: the Task's branch is kept after its PR closed unmerged."""

    def setUp(self):
        self.r = ProductRepo(branch="cloud/T-0009")
        self.addCleanup(self.r.close)
        self.r.item("E-0001", "epic")
        self.r.item("F-0007", "feature", parent="E-0001")
        self.r.item("T-0009", "task", parent="F-0007")

    def product(self):
        return self.r.product(prefixes=PREFIXES)

    def discover(self, state, tag=False):
        if tag:
            work = os.path.join(self.r.tmp, "tagger")
            subprocess.run(["git", "clone", "-q", self.r.origin, work], check=True,
                           capture_output=True, env=dict(os.environ, **GIT_ENV))
            git(work, "push", "-q", "origin", f"{self.r.head}:refs/tags/archive/pr-12")
        return self.r.discover(self.product(), prs=[pr(12, "cloud/T-0009", state, self.r.head)])

    def test_a_closed_pr_resets_the_task_to_ready_with_its_provenance(self):
        ev = self.discover("OPEN")
        self.r.ingest(ev)
        self.assertEqual(self.r.meta("T-0009", "task")["state"], "Active")

        ev = self.discover("CLOSED", tag=True)
        self.assertEqual(ev["resets"]["T-0009"]["archive"], "archive/pr-12")
        self.assertNotIn("cloud/T-0009", ev["branches"])
        self.r.ingest(ev)
        m = self.r.meta("T-0009", "task")
        self.assertEqual(m["state"], "New")
        self.assertIn("PR #12 closed unmerged on cloud/T-0009; its head kept as archive/pr-12 "
                      "— reset to ready", m["evidence"])
        self.assertEqual(m["evidence"][-1], "rule: planned")

    def test_the_registry_reset_is_written_by_ingest_for_the_product(self):
        product = self.product()
        path = os.path.join(env.state_dir(product), "sessions.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(json.dumps({"job": "t-0009", "item": "T-0009", "branch": "cloud/T-0009",
                                "started": "2026-09-20T10:00:00Z", "pid": 1, "rounds": 3,
                                "ended": "2026-09-20T11:00:00Z", "end_reason": "finished"}) + "\n")
        ev = self.discover("CLOSED")
        with mock.patch.object(ingest.env, "load_product", return_value=product), \
                mock.patch.object(ingest.evidence, "load", return_value=ev):
            ingest.cmd_ingest(types.SimpleNamespace(fresh=False, product="sample"), self.r.backlog)
            ingest.cmd_ingest(types.SimpleNamespace(fresh=False, product="sample"), self.r.backlog)
        self.assertEqual(lifecycle.rounds_of(path, "T-0009"), 0)
        self.assertEqual(lifecycle.resets(path)["T-0009"]["pr"], 12)
        with open(path) as f:
            self.assertEqual(sum('"reset"' in ln for ln in f), 1)

    def test_an_open_pr_is_untouched(self):
        ev = self.r.discover(self.product(), prs=[pr(12, "cloud/T-0009", "OPEN", self.r.head)])
        self.assertEqual(ev["resets"], {})


class RetireDeadBranchTests(unittest.TestCase):
    """The restart cuts the branch fresh from the trunk: the closed head is kept as its tag."""

    def setUp(self):
        self.r = ProductRepo(branch="cloud/T-0009")
        self.addCleanup(self.r.close)
        self.product = self.r.product(prefixes=PREFIXES)
        self.path = os.path.join(env.state_dir(self.product), "sessions.jsonl")
        open(self.path, "w").close()

    def origin_refs(self):
        out = git(self.r.origin, "for-each-ref", "--format=%(refname)")
        return set(out.split())

    def test_no_reset_leaves_the_branch_alone(self):
        from asf.workers import spawn
        self.assertFalse(spawn.retire_dead_branch(self.product, self.r.repo, "t-0009",
                                                  "cloud/T-0009"))
        self.assertIn("refs/heads/cloud/T-0009", self.origin_refs())

    def test_a_reset_branch_at_its_dead_head_is_archived_and_freed(self):
        from asf.workers import spawn
        lifecycle.note_reset(self.path, "T-0009", {"pr": 12, "branch": "cloud/T-0009",
                                                   "head": self.r.head, "archive": ""},
                             alive=lambda _p: False)
        self.assertTrue(spawn.retire_dead_branch(self.product, self.r.repo, "t-0009",
                                                 "cloud/T-0009"))
        refs = self.origin_refs()
        self.assertNotIn("refs/heads/cloud/T-0009", refs)
        self.assertIn("refs/tags/archive/pr-12", refs)

    def test_a_pr_tag_elsewhere_gets_a_second_tag_for_the_dead_head(self):
        from asf.workers import spawn
        git(self.r.repo, "push", "-q", "origin", f"{self.r.b0001}:refs/tags/archive/pr-12")
        lifecycle.note_reset(self.path, "T-0009", {"pr": 12, "branch": "cloud/T-0009",
                                                   "head": self.r.head, "archive": ""},
                             alive=lambda _p: False)
        self.assertTrue(spawn.retire_dead_branch(self.product, self.r.repo, "t-0009",
                                                 "cloud/T-0009"))
        self.assertIn(f"refs/tags/archive/pr-12-{self.r.head[:9]}", self.origin_refs())

    def test_a_branch_pushed_past_the_dead_head_is_alive(self):
        from asf.workers import spawn
        lifecycle.note_reset(self.path, "T-0009", {"pr": 12, "branch": "cloud/T-0009",
                                                   "head": "b" * 40, "archive": ""},
                             alive=lambda _p: False)
        self.assertFalse(spawn.retire_dead_branch(self.product, self.r.repo, "t-0009",
                                                  "cloud/T-0009"))
        self.assertIn("refs/heads/cloud/T-0009", self.origin_refs())


if __name__ == "__main__":
    unittest.main()
