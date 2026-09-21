import unittest

from asf.evidence import evidence


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
        out = evidence.plan_tasks(text, "free-plan")
        cand = dict((t, c) for t, c, _d in out)
        self.assertIn("fp-t1", cand["T1"])

    def test_common_stem_from_dispatch_rows_used_as_fallback(self):
        text = (
            "## Task 1: Wire the model\n"
            "Dispatch: T1 -> cloud/fact6-t1\n"
            "## Task 2: Ship the UI\n"
            "Dispatch: T2 -> cloud/fact6-t2\n"
            "## Task 3: Untouched\n"
        )
        out = evidence.plan_tasks(text, "clean-floor")
        cand = dict((t, c) for t, c, _d in out)
        self.assertIn("fact6-t3", cand["T3"])

    def test_prose_branch_mentions_do_not_steal_the_stem(self):
        # MOBILE-1's plan cites `cloud/brand-t7` nine times as a precondition; that must not
        # hand BRAND-1's task ids over to mobile-1's own T-numbers.
        text = (
            "## Task 1: Depends on brand work\n"
            "see cloud/brand-t7 for context, cloud/brand-t7 again, cloud/brand-t7 thrice\n"
        )
        out = evidence.plan_tasks(text, "mobile-1")
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


if __name__ == "__main__":
    unittest.main()
