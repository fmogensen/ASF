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
from asf.record import frontmatter, ingest, match

HERE = os.path.dirname(os.path.abspath(__file__))
PRS_FIXTURE = os.path.join(HERE, "fixtures", "evidence", "prs.json")

# The first product's conventions: task and doc branches share one prefix, and an older merge
# prefix is still honoured. Every test run with these reproduces its pre-config result.
FIRST_PRODUCT_PREFIXES = {"code": "cloud/", "spec": "cloud/spec-", "plan": "cloud/plan-",
                          "legacy": ["worktree-m-"]}
FIRST_TOKEN = evidence.branch_token(evidence.branch_prefixes(
    env.Product("first", {"conventions": {"branch_prefixes": FIRST_PRODUCT_PREFIXES}})))


class PlanTasksTests(unittest.TestCase):
    def test_task_headings_in_order(self):
        text = (
            "## Task 1: Wire the model\n"
            "some prose\n"
            "## Task 2: Ship the UI\n"
            "more prose\n"
        )
        out = evidence.plan_tasks(text, "free-plan")
        self.assertEqual([t for t, _c, _d in out], ["T1", "T2"])

    def test_declared_done_from_rest_of_heading(self):
        text = "## Task 1: Wire the model DONE\n## Task 2: Ship the UI\n"
        out = evidence.plan_tasks(text, "free-plan")
        done = {t: d for t, _c, d in out}
        self.assertTrue(done["T1"])
        self.assertFalse(done["T2"])

    def test_branch_named_in_dispatch_table(self):
        # The stem `fp` differs from the slug-based fallback `free-plan-t1`, so this only passes
        # if the dispatch-table line (task token + branch token on the same line) was read.
        text = (
            "## Task 1: Wire the model\n"
            "Dispatch: T1 -> cloud/fp-t1\n"
        )
        out = evidence.plan_tasks(text, "free-plan", FIRST_TOKEN)
        cand = dict((t, c) for t, c, _d in out)
        self.assertIn("fp-t1", cand["T1"])

    def test_branch_named_in_dispatch_table_default_prefix(self):
        text = "## Task 1: Wire the model\nDispatch: T1 -> worker/fp-t1\n"
        cand = dict((t, c) for t, c, _d in evidence.plan_tasks(text, "free-plan"))
        self.assertIn("fp-t1", cand["T1"])
        # another product's prefix is prose to this one
        text = "## Task 1: Wire the model\nDispatch: T1 -> cloud/fp-t1\n"
        cand = dict((t, c) for t, c, _d in evidence.plan_tasks(text, "free-plan"))
        self.assertNotIn("fp-t1", cand["T1"])

    def test_legacy_prefix_names_a_branch(self):
        text = "## Task 1: Rename\nDispatch: T1 -> worktree-m-rename-t1\n"
        cand = dict((t, c) for t, c, _d in evidence.plan_tasks(text, "rename", FIRST_TOKEN))
        self.assertIn("rename-t1", cand["T1"])

    def test_common_stem_from_dispatch_rows_used_as_fallback(self):
        text = (
            "## Task 1: Wire the model\n"
            "Dispatch: T1 -> cloud/fact6-t1\n"
            "## Task 2: Ship the UI\n"
            "Dispatch: T2 -> cloud/fact6-t2\n"
            "## Task 3: Untouched\n"
        )
        out = evidence.plan_tasks(text, "clean-floor", FIRST_TOKEN)
        cand = dict((t, c) for t, c, _d in out)
        self.assertIn("fact6-t3", cand["T3"])

    def test_prose_branch_mentions_do_not_steal_the_stem(self):
        # MOBILE-1's plan cites `cloud/brand-t7` nine times as a precondition; that must not
        # hand BRAND-1's task ids over to mobile-1's own T-numbers.
        text = (
            "## Task 1: Depends on brand work\n"
            "see cloud/brand-t7 for context, cloud/brand-t7 again, cloud/brand-t7 thrice\n"
        )
        out = evidence.plan_tasks(text, "mobile-1", FIRST_TOKEN)
        cand = dict((t, c) for t, c, _d in out)
        self.assertIn("mobile-1-t1", cand["T1"])
        self.assertNotIn("brand-t1", cand["T1"])


class VerdictOfTests(unittest.TestCase):
    def test_approved(self):
        self.assertEqual(evidence.verdict_of(b"Verdict: APPROVED\n"), "APPROVED")

    def test_changes_requested_case_insensitive(self):
        self.assertEqual(evidence.verdict_of(b"changes requested, see below"), "CHANGES REQUESTED")

    def test_no_verdict_found(self):
        self.assertEqual(evidence.verdict_of(b"just some prose"), "")

    def test_empty_blob(self):
        self.assertEqual(evidence.verdict_of(None), "")
        self.assertEqual(evidence.verdict_of(b""), "")


class ParseRowsTests(unittest.TestCase):
    HEADER = "| Id | Area | Cap | User | MS | Status | Typed | Impl | Tests | Notes |\n"

    def test_valid_row(self):
        text = self.HEADER + "| F-BILLING-1 | billing | **Free plan** | anyone | M2 | done | y | a.ts | a.test.ts | |\n"
        rows, broken = evidence.parse_rows(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "F-BILLING-1")
        self.assertEqual(rows[0]["status"], "done")
        self.assertEqual(broken, [])

    def test_broken_status_counted_as_todo(self):
        text = self.HEADER + "| F-BILLING-2 | billing | Weird | anyone | M2 | ??? | y | | | |\n"
        rows, broken = evidence.parse_rows(text)
        self.assertEqual(rows[0]["status"], "todo")
        self.assertEqual(rows[0]["ms"], "broken")
        self.assertEqual(len(broken), 1)

    def test_escaped_pipe_in_cell_does_not_shift_columns(self):
        text = self.HEADER + r"| F-DEV-1 | dev | privacy export\|erase | anyone | M3 | doing | y | a.ts | | |" + "\n"
        rows, _broken = evidence.parse_rows(text)
        self.assertEqual(rows[0]["cap"], "privacy export|erase")
        self.assertEqual(rows[0]["status"], "doing")

    def test_short_row_ignored(self):
        text = self.HEADER + "| F-X-1 | area |\n"
        rows, _broken = evidence.parse_rows(text)
        self.assertEqual(rows, [])

    def test_non_feature_rows_ignored(self):
        text = self.HEADER + "| notes | something |\n"
        rows, _broken = evidence.parse_rows(text)
        self.assertEqual(rows, [])


class DocSlugTests(unittest.TestCase):
    def test_strips_date_prefix(self):
        self.assertEqual(evidence.doc_slug("2026-09-20-clean-floor.md"), "clean-floor")

    def test_no_prefix(self):
        self.assertEqual(evidence.doc_slug("clean-floor.md"), "clean-floor")


class TaskStateTests(unittest.TestCase):
    def test_new_in_plan_no_branch(self):
        self.assertEqual(evidence.task_state(True, None, None, None), "New")

    def test_active_with_branch(self):
        self.assertEqual(evidence.task_state(True, "cloud/free-plan-t1", None, None), "Active")

    def test_active_with_open_pr_no_branch_recorded(self):
        self.assertEqual(evidence.task_state(True, None, "OPEN", None), "Active")

    def test_closed_when_merged(self):
        self.assertEqual(evidence.task_state(True, "cloud/free-plan-t1", "MERGED", "abc123"), "Closed")

    def test_merged_wins_over_branch_state(self):
        self.assertEqual(evidence.task_state(False, "cloud/x", "MERGED", "sha"), "Closed")


class StoryStateTests(unittest.TestCase):
    def test_new_no_task_no_matrix_progress(self):
        self.assertEqual(evidence.story_state(False, "todo"), "New")

    def test_active_when_a_task_lists_it_active(self):
        self.assertEqual(evidence.story_state(True, "todo"), "Active")

    def test_resolved_when_matrix_says_doing(self):
        self.assertEqual(evidence.story_state(False, "doing"), "Resolved")

    def test_closed_when_matrix_says_done(self):
        self.assertEqual(evidence.story_state(False, "done"), "Closed")

    def test_done_wins_over_active_task(self):
        self.assertEqual(evidence.story_state(True, "done"), "Closed")


class FeatureStateTests(unittest.TestCase):
    def test_new_no_spec_no_plan(self):
        self.assertEqual(evidence.feature_state(False, False, False, False, False), "New")

    def test_active_spec_on_main(self):
        self.assertEqual(evidence.feature_state(True, False, False, False, False), "Active")

    def test_active_plan_approved(self):
        self.assertEqual(evidence.feature_state(False, True, False, False, False), "Active")

    def test_resolved_all_tasks_closed(self):
        self.assertEqual(evidence.feature_state(True, True, True, False, False), "Resolved")

    def test_closed_requires_prod_and_checked_too(self):
        self.assertEqual(evidence.feature_state(True, True, True, True, True), "Closed")

    def test_all_tasks_closed_but_not_on_prod_stays_resolved(self):
        self.assertEqual(evidence.feature_state(True, True, True, False, True), "Resolved")

    def test_all_tasks_closed_but_not_all_checked_stays_resolved(self):
        self.assertEqual(evidence.feature_state(True, True, True, True, False), "Resolved")


class FeatureStageTests(unittest.TestCase):
    NO_DOC = {"exists": False, "review": None, "approved": False}

    def test_card_when_nothing_exists(self):
        stage = evidence.feature_stage(self.NO_DOC, self.NO_DOC, [], False)
        self.assertEqual(stage, "card")

    def test_spec_draft(self):
        spec = {"exists": True, "review": None, "approved": False}
        stage = evidence.feature_stage(spec, self.NO_DOC, [], False)
        self.assertEqual(stage, "spec-draft")

    def test_spec_review_round(self):
        spec = {"exists": True, "review": (2, "CHANGES REQUESTED"), "approved": False}
        stage = evidence.feature_stage(spec, self.NO_DOC, [], False)
        self.assertEqual(stage, "spec-review r2")

    def test_spec_approved(self):
        spec = {"exists": True, "review": (1, "APPROVED"), "approved": True}
        stage = evidence.feature_stage(spec, self.NO_DOC, [], False)
        self.assertEqual(stage, "spec-approved")

    def test_plan_draft(self):
        spec = {"exists": True, "review": (1, "APPROVED"), "approved": True}
        plan = {"exists": True, "review": None, "approved": False}
        stage = evidence.feature_stage(spec, plan, [], False)
        self.assertEqual(stage, "plan-draft")

    def test_plan_review_round(self):
        spec = {"exists": True, "review": (1, "APPROVED"), "approved": True}
        plan = {"exists": True, "review": (3, "CHANGES REQUESTED"), "approved": False}
        stage = evidence.feature_stage(spec, plan, [], False)
        self.assertEqual(stage, "plan-review r3")

    def test_plan_approved_no_tasks_yet(self):
        spec = {"exists": True, "review": (1, "APPROVED"), "approved": True}
        plan = {"exists": True, "review": (1, "APPROVED"), "approved": True}
        stage = evidence.feature_stage(spec, plan, [], False)
        self.assertEqual(stage, "plan-approved")

    def test_building_k_of_n(self):
        spec = {"exists": True, "review": (1, "APPROVED"), "approved": True}
        plan = {"exists": True, "review": (1, "APPROVED"), "approved": True}
        stage = evidence.feature_stage(spec, plan, ["Closed", "Active", "New"], False)
        self.assertEqual(stage, "building 1/3")

    def test_landed_when_all_tasks_closed(self):
        spec = {"exists": True, "review": (1, "APPROVED"), "approved": True}
        plan = {"exists": True, "review": (1, "APPROVED"), "approved": True}
        stage = evidence.feature_stage(spec, plan, ["Closed", "Closed"], False)
        self.assertEqual(stage, "landed")

    def test_on_prod_when_all_closed_and_ancestor_of_prod(self):
        spec = {"exists": True, "review": (1, "APPROVED"), "approved": True}
        plan = {"exists": True, "review": (1, "APPROVED"), "approved": True}
        stage = evidence.feature_stage(spec, plan, ["Closed", "Closed"], True)
        self.assertEqual(stage, "on-prod")


class BugStateTests(unittest.TestCase):
    def test_new_with_no_evidence(self):
        self.assertEqual(evidence.bug_state(False, None, False), "New")

    def test_active_with_fixer_branch(self):
        self.assertEqual(evidence.bug_state(True, None, False), "Active")

    def test_resolved_when_merged(self):
        self.assertEqual(evidence.bug_state(True, "sha123", False), "Resolved")

    def test_still_resolved_when_merged_sha_reaches_prod(self):
        # Closed also needs the 3-day signature-quiet rule, which is T10's; T3 stops at Resolved.
        self.assertEqual(evidence.bug_state(True, "sha123", True), "Resolved")


class EpicStateTests(unittest.TestCase):
    def test_new_with_no_children(self):
        self.assertEqual(evidence.epic_state([], False), "New")

    def test_active_when_a_child_is_active(self):
        self.assertEqual(evidence.epic_state(["New", "Active", "New"], False), "Active")

    def test_resolved_when_all_children_closed(self):
        self.assertEqual(evidence.epic_state(["Closed", "Closed"], False), "Resolved")

    def test_no_children_never_resolved(self):
        self.assertEqual(evidence.epic_state([], False), "New")

    def test_closed_only_when_typed(self):
        self.assertEqual(evidence.epic_state(["Active"], True), "Closed")
        self.assertEqual(evidence.epic_state(["Closed", "Closed"], True), "Closed")


class BlockedOfTests(unittest.TestCase):
    def test_no_blockers(self):
        self.assertEqual(evidence.blocked_of([], {}), (False, []))

    def test_open_item_blocker(self):
        blocked, open_ = evidence.blocked_of(["F-0051"], {"F-0051": "Active"})
        self.assertTrue(blocked)
        self.assertEqual(open_, ["F-0051"])

    def test_closed_item_blocker_not_open(self):
        blocked, open_ = evidence.blocked_of(["F-0051"], {"F-0051": "Closed"})
        self.assertFalse(blocked)
        self.assertEqual(open_, [])

    def test_human_string_always_open(self):
        blocked, open_ = evidence.blocked_of(["Ops: cloud project + key"], {})
        self.assertTrue(blocked)
        self.assertEqual(open_, ["Ops: cloud project + key"])

    def test_mixed_blockers(self):
        blocked, open_ = evidence.blocked_of(
            ["F-0051", "Ops: key"], {"F-0051": "Closed"})
        self.assertTrue(blocked)
        self.assertEqual(open_, ["Ops: key"])


# ---- id tokens and configured prefixes, against a bare product repo --------------------------
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd, *args, extra_env=None):
    e = dict(os.environ, **GIT_ENV, **(extra_env or {}))
    return subprocess.run(["git", "-C", cwd] + list(args), env=e, check=True,
                          capture_output=True, text=True).stdout.strip()


FOLDER_OF = {"epic": "epics", "feature": "features", "story": "stories", "task": "tasks",
             "bug": "bugs"}
BODY = "## Description\n\n## History\n- 2026-01-01: created\n\n## Children\n\n## Backlinks\n"


class ProductRepo:
    """A bare `origin` with commits on main naming B-0001, T-0002 and F-0005, one extra branch,
    and a clone of it as the product checkout; plus an empty backlog record beside it."""

    def __init__(self, branch="fix/B-0003", extra_branches=()):
        self.tmp = tempfile.mkdtemp(prefix="evidence_repo_")
        self.origin = os.path.join(self.tmp, "origin.git")
        self.repo = os.path.join(self.tmp, "product")
        self.backlog = os.path.join(self.tmp, "backlog")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.origin], check=True,
                       env=dict(os.environ, **GIT_ENV))
        work = os.path.join(self.tmp, "work")
        subprocess.run(["git", "clone", "-q", self.origin, work], check=True, capture_output=True,
                       env=dict(os.environ, **GIT_ENV))
        git(work, "checkout", "-q", "-b", "main")
        subjects = ["init", "asf(record): drop the double read (B-0001)",
                    "asf(views): wire the board [T-0002]", "asf(free): land the free plan F-0005"]
        for n, subject in enumerate(subjects):
            with open(os.path.join(work, f"f{n}.txt"), "w") as f:
                f.write(subject)
            git(work, "add", ".")
            git(work, "commit", "-q", "-m", subject)
        self.head = git(work, "rev-parse", "HEAD")
        self.b0001 = git(work, "rev-parse", "HEAD~2")
        git(work, "push", "-q", "origin", "main")
        for b in (branch,) + tuple(extra_branches):
            git(work, "push", "-q", "origin", f"main:refs/heads/{b}")
        subprocess.run(["git", "clone", "-q", self.origin, self.repo], check=True,
                       capture_output=True, env=dict(os.environ, **GIT_ENV))
        os.makedirs(self.backlog)
        for folder in FOLDER_OF.values():
            os.makedirs(os.path.join(self.backlog, folder))

    def product(self, ci="gh-actions", prefixes=None):
        data = {"repo_dir": self.repo, "repo_slug": "sample/product", "main": "main",
                "backlog_dir": self.backlog, "ci": {"provider": ci} if ci else "none",
                "conventions": {"branch_prefixes": prefixes} if prefixes else {}}
        return env.Product("sample", data)

    def discover(self, product, green=None, prs=None):
        green = [self.head] if green is None else green
        with open(PRS_FIXTURE) as f:
            pr_stub = prs if prs is not None else json.load(f)

        def fake_gh(args, timeout=60, product=None):
            if args.startswith("run list") and "--status success" in args:
                return [{"headSha": sha, "createdAt": f"2026-09-21T10:0{i}:00Z"}
                        for i, sha in enumerate(green)]
            return [] if not args.startswith("api ") else {}

        with mock.patch.object(evidence, "pr_list", return_value=pr_stub), \
                mock.patch.object(evidence, "_gh_json", side_effect=fake_gh):
            return evidence.discover(product=product,
                                     checked_file=os.path.join(self.tmp, "none.txt"))

    def item(self, id_, type_, parent=None, typed_lines=()):
        lines = [f"id: {id_}", f"type: {type_}", f"title: {id_} item"]
        if parent:
            lines.append(f"parent: {parent}")
        lines += list(typed_lines) + ["# ---- machine ----", "state: New",
                                      "stage_since: 2026-01-01T00:00:00Z",
                                      "updated: 2026-01-01T00:00:00Z"]
        with open(os.path.join(self.backlog, FOLDER_OF[type_], f"{id_}.md"), "w") as f:
            f.write("---\n" + "\n".join(lines) + "\n---\n" + BODY)

    def standard_items(self):
        self.item("E-0001", "epic")
        self.item("F-0005", "feature", parent="E-0001")
        self.item("F-0007", "feature", parent="E-0001")
        self.item("B-0001", "bug", parent="F-0005")
        self.item("T-0002", "task", parent="F-0007")
        self.item("B-0003", "bug", parent="F-0007")
        self.item("S-0004", "story", parent="F-0007")

    def ingest(self, ev):
        with mock.patch.object(ingest.evidence, "load", return_value=ev):
            self_rc = ingest.cmd_ingest(types.SimpleNamespace(fresh=False), self.backlog)
        assert self_rc == 0

    def meta(self, id_, type_):
        path = os.path.join(self.backlog, FOLDER_OF[type_], f"{id_}.md")
        with open(path) as f:
            return frontmatter.parse(f.read(), path=path)[0]

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class IdTokenTests(unittest.TestCase):
    def test_tokens_bare_parenthesised_and_bracketed(self):
        self.assertEqual(evidence.id_tokens("fix (B-0001) and [T-0002], F-0005; X-0001 B-12345"),
                         ["B-0001", "T-0002", "F-0005"])

    def test_branch_tokens_are_case_insensitive(self):
        self.assertEqual(evidence.id_tokens("fix/b-0003-banner", evidence.BRANCH_ID_TOKEN), ["B-0003"])

    def test_ci_provider(self):
        self.assertIsNone(evidence.ci_provider(env.Product("x", {"ci": "none"})))
        self.assertIsNone(evidence.ci_provider(env.Product("x", {})))
        self.assertIsNone(evidence.ci_provider(env.Product("x", {"ci": {"provider": "none"}})))
        self.assertEqual(evidence.ci_provider(env.Product("x", {"ci": {"provider": "gh-actions"}})),
                         "gh-actions")

    def test_id_state_ladder(self):
        self.assertEqual(evidence.id_state("B-0001", None), (None, []))
        self.assertEqual(evidence.id_state("B-0003", {"branches": ["fix/B-0003"]}),
                         ("Active", ["branch fix/B-0003"]))
        self.assertEqual(evidence.id_state("S-0004", {"open_prs": [4], "branches": []})[0], "Active")
        self.assertEqual(evidence.id_state("B-0001", {"commit": "abcdef123", "green": False}),
                         ("Resolved", ["commit abcdef1 names B-0001"]))
        self.assertEqual(evidence.id_state("B-0001", {"commit": "abcdef123", "green": True})[0],
                         "Closed")


class DiscoverIdEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.r = ProductRepo()
        self.addCleanup(self.r.close)

    def test_branches_prs_and_commits_on_main(self):
        ev = self.r.discover(self.r.product())
        ids = ev["ids"]
        self.assertEqual(ids["B-0001"]["commit"], self.r.b0001)
        self.assertTrue(ids["B-0001"]["green"])
        self.assertTrue(ids["T-0002"]["green"])
        self.assertEqual(ids["B-0003"]["branches"], ["fix/B-0003"])
        self.assertEqual(ids["S-0004"]["open_prs"], [4])
        self.assertNotIn("T-0008", ids)  # a closed, unmerged PR is not evidence
        self.assertEqual(ev["ci"], "gh-actions")

    def test_no_green_run_at_or_after_the_commit_stays_unverified(self):
        ev = self.r.discover(self.r.product(), green=[])
        self.assertFalse(ev["ids"]["B-0001"]["green"])

    def test_green_run_before_the_commit_does_not_cover_it(self):
        first = git(self.r.repo, "rev-parse", "origin/main~3")
        ev = self.r.discover(self.r.product(), green=[first])
        self.assertFalse(ev["ids"]["B-0001"]["green"])

    def test_commits_older_than_the_record_are_not_read(self):
        git(self.r.backlog, "init", "-q")
        git(self.r.backlog, "commit", "-q", "--allow-empty", "-m", "record starts",
            extra_env={"GIT_COMMITTER_DATE": "2099-01-01T00:00:00Z",
                       "GIT_AUTHOR_DATE": "2099-01-01T00:00:00Z"})
        ev = self.r.discover(self.r.product())
        self.assertNotIn("B-0001", ev["ids"])
        self.assertIn("B-0003", ev["ids"])  # branches are live, not history

    def test_evidence_cache_is_per_product_under_its_own_state_dir(self):
        # F-0087 (the hermetic rule): the cache lives under the product's state directory in
        # ASF_HOME — never a shared path two homes naming the same product would both read
        from asf import env
        with mock.patch.object(evidence, "discover", return_value={"checked": set(), "x": 1}):
            evidence.load(fresh=True, product=self.r.product())
        cache = os.path.join(env.state_dir(self.r.product()), "cache-backlog-evidence.json")
        self.assertTrue(os.path.exists(cache), cache)
        self.assertTrue(cache.startswith(env.ASF_HOME), cache)


class IngestIdEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.r = ProductRepo()
        self.addCleanup(self.r.close)
        self.r.standard_items()

    def states(self):
        return {i: self.r.meta(i, t) for i, t in (("B-0001", "bug"), ("T-0002", "task"),
                                                  ("B-0003", "bug"), ("S-0004", "story"),
                                                  ("F-0005", "feature"), ("F-0007", "feature"))}

    def test_green_ci_at_head(self):
        self.r.ingest(self.r.discover(self.r.product()))
        m = self.states()
        self.assertEqual(m["B-0001"]["state"], "Closed")
        self.assertEqual(m["T-0002"]["state"], "Closed")
        self.assertEqual(m["B-0003"]["state"], "Active")
        self.assertEqual(m["S-0004"]["state"], "Active")
        self.assertIn(f"commit {self.r.b0001[:7]} names B-0001", m["B-0001"]["evidence"])
        self.assertEqual(m["B-0003"]["evidence"], ["branch fix/B-0003"])
        self.assertEqual(m["S-0004"]["evidence"], ["PR #4 OPEN"])

    def test_resolved_until_ci_is_green(self):
        self.r.ingest(self.r.discover(self.r.product(), green=[]))
        m = self.states()
        self.assertEqual(m["B-0001"]["state"], "Resolved")
        self.assertEqual(m["T-0002"]["state"], "Resolved")

    def test_ci_none_closes_on_the_commit(self):
        product = self.r.product(ci=None)
        ev = self.r.discover(product, green=[])
        self.r.ingest(ev)
        m = self.states()
        self.assertEqual(m["B-0001"]["state"], "Closed")
        self.assertEqual(m["T-0002"]["state"], "Closed")
        self.assertEqual(m["B-0003"]["state"], "Active")
        self.assertEqual(m["S-0004"]["state"], "Active")
        self.assertIn("no CI provider", m["B-0001"]["evidence"])

    def test_first_product_prefixes_give_the_same_result(self):
        self.r.close()
        self.r = ProductRepo(branch="cloud/B-0003", extra_branches=("cloud/spec-free-plan",))
        self.r.standard_items()
        product = self.r.product(prefixes=FIRST_PRODUCT_PREFIXES)
        ev = self.r.discover(product)
        self.assertEqual(ev["features"]["free-plan"]["spec_branch"], "cloud/spec-free-plan")
        self.r.ingest(ev)
        m = self.states()
        self.assertEqual([m[i]["state"] for i in ("B-0001", "T-0002", "B-0003", "S-0004")],
                         ["Closed", "Closed", "Active", "Active"])
        self.assertEqual(m["B-0003"]["evidence"], ["branch cloud/B-0003"])

    def test_default_prefixes_find_a_spec_branch(self):
        self.r.close()
        self.r = ProductRepo(extra_branches=("spec/free-plan", "cloud/spec-other"))
        ev = self.r.discover(self.r.product())
        self.assertEqual(ev["features"]["free-plan"]["spec_branch"], "spec/free-plan")
        self.assertNotIn("other", ev["features"])

    def test_feature_with_closed_bug_and_naming_commit_lands(self):
        self.r.ingest(self.r.discover(self.r.product()))
        m = self.states()
        self.assertEqual(m["F-0005"]["stage"], "landed")
        self.assertEqual(m["F-0005"]["state"], "Resolved")
        self.assertIn("1/1 children Closed", m["F-0005"]["evidence"])
        # F-0007's children are not all Closed and no commit names it: still a card
        self.assertEqual(m["F-0007"]["stage"], "card")

    def test_legacy_match_wins_over_an_id_token(self):
        self.r.item("B-0003", "bug", parent="F-0007", typed_lines=["links:", "  prs: [9]"])
        prs = [{"number": 9, "title": "the banner fix", "body": "", "state": "MERGED",
                "headRefName": "worker/banner", "mergedAt": "2026-09-21T10:00:00Z",
                "mergeCommit": {"oid": self.r.head}}]
        self.r.ingest(self.r.discover(self.r.product(), prs=prs))
        m = self.r.meta("B-0003", "bug")
        self.assertEqual(m["state"], "Resolved")
        self.assertEqual(m["evidence"], [f"fix merged ({self.r.head[:9]})"])


class MatchPrefixesTests(unittest.TestCase):
    ITEMS = {"F-0042": {"type": "feature", "title": "Free plan", "legacy_id": "free-plan",
                        "children": []}}

    def test_any_directory_prefix_is_dropped(self):
        for b in ("worker/free-plan", "spec/free-plan", "cloud/spec-free-plan"):
            self.assertEqual(match.match_event(self.ITEMS, branch=b)[0], ["F-0042"], b)

    def test_a_configured_prefix_without_a_slash_is_dropped(self):
        product = env.Product("first", {"conventions": {"branch_prefixes": FIRST_PRODUCT_PREFIXES}})
        prefixes = match.branch_prefixes(product)
        self.assertEqual(match.match_event(self.ITEMS, branch="worktree-m-free-plan")[0], [])
        self.assertEqual(match.match_event(self.ITEMS, branch="worktree-m-free-plan",
                                           prefixes=prefixes)[0], ["F-0042"])


if __name__ == "__main__":
    unittest.main()
