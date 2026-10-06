"""An approved spec that is not on the trunk is landed as written before any coder starts.

The lane pass adopts the spec's branch (:mod:`asf.tick.land_spec` through
:meth:`asf.harvest.lane.Lane.adopt`): a documents-only lane branch that merges cleanly becomes a
finished run the lane holds PUSHED and lands, with no session; one that cannot land as it stands
is BACK with a ``landing-gate`` correction, which the feeder hands to a BACK → REBASE session
told to land the existing spec, not rewrite it."""
import os
import shutil
import subprocess
import tempfile
import unittest

from asf import env
from asf.feeder import rows
from asf.tick import land_spec
from asf.workers import lifecycle
from asf.workers import pool as pool_mod

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, text=True,
                          env=dict(os.environ, **GIT_ENV)).stdout.strip()


class LandTheApprovedSpec(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="land_spec_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, "home")
        self.addCleanup(setattr, env, "ASF_HOME", home)
        origin, self.work = os.path.join(self.tmp, "origin.git"), os.path.join(self.tmp, "work")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", origin], check=True,
                       env=dict(os.environ, **GIT_ENV))
        subprocess.run(["git", "clone", "-q", origin, self.work], check=True, capture_output=True,
                       env=dict(os.environ, **GIT_ENV))
        git(self.work, "checkout", "-q", "-b", "main")
        self.commit("init", {"README.md": "x\n", "docs/specs/other.md": "a\n"})
        git(self.work, "push", "-q", "origin", "main")
        self.product = env.Product("sample", {"repo_dir": self.work, "main": "main"})
        self.lane = rows.branch_for(self.product, "spec", "F-0001")

    def commit(self, subject, files):
        for path, text in files.items():
            full = os.path.join(self.work, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(text)
        git(self.work, "add", ".")
        git(self.work, "commit", "-q", "-m", subject)

    def push_branch(self, name, files, subject="spec(F-0001): the widget spec"):
        git(self.work, "checkout", "-q", "-b", name, "main")
        self.commit(subject, files)
        git(self.work, "push", "-q", "origin", name)
        git(self.work, "checkout", "-q", "main")
        git(self.work, "fetch", "-q", "origin")

    def items(self, branch, stage="spec-approved"):
        return {"F-0001": {"id": "F-0001", "type": "feature", "decided": True, "state": "Active",
                           "stage": stage,
                           "evidence": [f"spec on {branch} (review r1 APPROVED)",
                                        "plan on origin/main"]}}

    def runs(self):
        return lifecycle.by_branch(pool_mod.sessions_path(self.product))

    def declared(self, *globs):
        """The same repo, read by a product that declares `globs` as documents."""
        return env.Product("sample", {"repo_dir": self.work, "main": "main",
                                      "conventions": {"doc_paths": list(globs)}})

    def test_a_branch_touching_a_declared_doc_path_lands_as_it_stands(self):
        self.push_branch(self.lane, {"docs/specs/f-0001.md": "# widgets\n",
                                     "decisions/0007-widgets.md": "the register\n"})
        product = self.declared("decisions/**")
        self.assertEqual(land_spec.why_not_as_is(product, self.lane, "F-0001"), ("", ""))
        self.assertEqual(land_spec.adopt(product, self.items(self.lane), out=lambda *_a: None),
                         [("F-0001", self.lane, "")])
        self.assertEqual(self.runs()[self.lane]["lane"]["state"], "PUSHED")
        self.assertIsNone(lifecycle.pending_correction(self.runs()[self.lane]))

    def test_an_undeclared_path_is_not_docs_only(self):
        self.push_branch(self.lane, {"docs/specs/f-0001.md": "# widgets\n",
                                     "decisions/0007-widgets.md": "the register\n"})
        self.assertIn("more than documents",
                      land_spec.why_not_as_is(self.product, self.lane, "F-0001")[1])

    def test_a_declared_doc_path_never_carries_code_in_with_it(self):
        self.push_branch(self.lane, {"docs/specs/f-0001.md": "# widgets\n",
                                     "decisions/0007-widgets.md": "the register\n",
                                     "src/a.py": "x = 1\n"})
        self.assertIn("more than documents",
                      land_spec.why_not_as_is(self.declared("decisions/**"), self.lane,
                                              "F-0001")[1])

    def test_a_clean_docs_only_lane_branch_is_handed_to_the_docs_lane(self):
        self.push_branch(self.lane, {"docs/specs/f-0001.md": "# widgets\n"})
        done = land_spec.adopt(self.product, self.items(self.lane), now="2026-01-01T00:00:00Z",
                               out=lambda *_a: None)
        self.assertEqual(done, [("F-0001", self.lane, "")])
        run = self.runs()[self.lane]
        self.assertTrue(lifecycle.eligible(run))
        self.assertIsNone(lifecycle.pending_correction(run))
        # the run now speaks for the branch: the next tick adopts nothing again
        self.assertEqual(land_spec.adopt(self.product, self.items(self.lane),
                                         out=lambda *_a: None), [])
        self.assertEqual(run['lane']['state'], 'PUSHED')
        # and the feeder reads it as pushed and waiting, no session
        occupancy = lifecycle.occupancy(pool_mod.sessions_path(self.product))
        (row,) = rows.feature_rows(self.items(self.lane), self.product, set(), [],
                                   occupancy=occupancy)
        self.assertEqual((row.kind, row.launches), (rows.PUSHED_LAND, False))

    def test_a_conflicting_branch_goes_to_a_session_that_lands_it_unrewritten(self):
        self.push_branch(self.lane, {"docs/specs/other.md": "b\n"})
        self.commit("docs: move the trunk on", {"docs/specs/other.md": "c\n"})
        git(self.work, "push", "-q", "origin", "main")
        git(self.work, "fetch", "-q", "origin")
        (done,) = land_spec.adopt(self.product, self.items(self.lane), out=lambda *_a: None)
        self.assertIn("conflicts", done[2])
        path = pool_mod.sessions_path(self.product)
        corr = lifecycle.corrections(path)
        self.assertEqual(corr["F-0001"]["kind"], rows.LANDING_GATE)
        self.assertEqual(corr["F-0001"]["why"], "conflict")
        self.assertEqual(self.runs()[self.lane]["lane"]["state"], "BACK")
        (row,) = rows.candidates({"items": self.items(self.lane)}, self.product, [],
                                 occupancy=lifecycle.occupancy(path))
        self.assertEqual((row.kind, row.branch, row.launches, row.hold_why),
                         (rows.BACK_REBASE, self.lane, True, "conflict"))
        self.assertIn("don't rewrite it", row.correction)

    def test_a_spec_on_a_pre_lane_branch_is_landed_by_a_session_on_the_lane_branch(self):
        self.push_branch("old/widgets", {"docs/specs/widgets.md": "# widgets\n"},
                         subject="widgets spec")
        (done,) = land_spec.adopt(self.product, self.items("old/widgets"), out=lambda *_a: None)
        self.assertIn("no spec/plan lane branch", done[2])
        corr = lifecycle.corrections(pool_mod.sessions_path(self.product))
        self.assertEqual(corr["F-0001"]["branch"], self.lane)
        self.assertEqual(corr["F-0001"]["why"], "not a lane branch")
        self.assertIn("old/widgets", corr["F-0001"]["text"])

    def test_why_not_as_is_names_a_branch_not_on_origin(self):
        self.assertEqual(land_spec.why_not_as_is(self.product, "spec/F-9999", "F-0001")[0],
                         "not on origin")

    def test_only_an_approved_spec_off_the_trunk_is_adopted(self):
        self.push_branch(self.lane, {"docs/specs/f-0001.md": "# widgets\n"})
        for stage in ("spec-draft", "plan-approved", "building 0/2"):
            with self.subTest(stage=stage):
                self.assertEqual(land_spec.wanted(self.items(self.lane, stage)), [])
        on_trunk = {"F-0001": dict(self.items(self.lane)["F-0001"],
                                   evidence=["spec on origin/main", "plan on origin/main"])}
        self.assertEqual(land_spec.wanted(on_trunk), [])
        self.assertEqual(land_spec.wanted(self.items(self.lane)),
                         [("F-0001", self.lane, "spec")])


class LandTheApprovedPlan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="land_plan_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, "home")
        self.addCleanup(setattr, env, "ASF_HOME", home)
        origin, self.work = os.path.join(self.tmp, "origin.git"), os.path.join(self.tmp, "work")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", origin], check=True,
                       env=dict(os.environ, **GIT_ENV))
        subprocess.run(["git", "clone", "-q", origin, self.work], check=True, capture_output=True,
                       env=dict(os.environ, **GIT_ENV))
        git(self.work, "checkout", "-q", "-b", "main")
        self.commit("init", {"README.md": "x\n", "docs/plans/other.md": "a\n"})
        git(self.work, "push", "-q", "origin", "main")
        self.product = env.Product("sample", {"repo_dir": self.work, "main": "main"})
        self.lane = rows.branch_for(self.product, "plan", "F-0001")

    def commit(self, subject, files):
        for path, text in files.items():
            full = os.path.join(self.work, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(text)
        git(self.work, "add", ".")
        git(self.work, "commit", "-q", "-m", subject)

    def push_branch(self, name, files, subject="plan(F-0001): the widget plan"):
        git(self.work, "checkout", "-q", "-b", name, "main")
        self.commit(subject, files)
        git(self.work, "push", "-q", "origin", name)
        git(self.work, "checkout", "-q", "main")
        git(self.work, "fetch", "-q", "origin")

    def items(self, branch, stage="plan-approved", spec_evidence="spec on origin/main"):
        return {"F-0001": {"id": "F-0001", "type": "feature", "decided": True, "state": "Active",
                           "stage": stage,
                           "evidence": [spec_evidence, f"plan on {branch}"]}}

    def runs(self):
        return lifecycle.by_branch(pool_mod.sessions_path(self.product))

    def test_a_clean_docs_only_lane_branch_is_handed_to_the_docs_lane(self):
        self.push_branch(self.lane, {"docs/plans/f-0001.md": "# widgets\n"})
        self.assertEqual(land_spec.wanted(self.items(self.lane)),
                         [("F-0001", self.lane, "plan")])
        done = land_spec.adopt(self.product, self.items(self.lane), now="2026-01-01T00:00:00Z",
                               out=lambda *_a: None)
        self.assertEqual(done, [("F-0001", self.lane, "")])
        run = self.runs()[self.lane]
        self.assertEqual(run["job"], f"land-plan-f-0001")
        self.assertTrue(lifecycle.eligible(run))
        self.assertIsNone(lifecycle.pending_correction(run))
        self.assertEqual(land_spec.adopt(self.product, self.items(self.lane),
                                         out=lambda *_a: None), [])
        self.assertEqual(run['lane']['state'], 'PUSHED')

    def test_a_feature_at_building_is_named_too(self):
        self.push_branch(self.lane, {"docs/plans/f-0001.md": "# widgets\n"})
        self.assertEqual(land_spec.wanted(self.items(self.lane, stage="building 1/3")),
                         [("F-0001", self.lane, "plan")])

    def test_a_branch_carrying_code_goes_to_a_session_that_lands_it_unrewritten(self):
        self.push_branch(self.lane, {"asf/tick/widget.py": "x = 1\n"})
        (done,) = land_spec.adopt(self.product, self.items(self.lane), out=lambda *_a: None)
        self.assertIn("more than documents", done[2])
        path = pool_mod.sessions_path(self.product)
        corr = lifecycle.corrections(path)
        self.assertEqual(corr["F-0001"]["kind"], rows.LANDING_GATE)
        self.assertEqual(corr["F-0001"]["why"], "more than documents")
        self.assertIn("plan", corr["F-0001"]["text"])
        self.assertIn(self.lane, corr["F-0001"]["text"])
        self.assertEqual(self.runs()[self.lane]["lane"]["state"], "BACK")
        (row,) = rows.candidates({"items": self.items(self.lane)}, self.product, [],
                                 occupancy=lifecycle.occupancy(path))
        self.assertEqual((row.kind, row.branch, row.launches, row.hold_why),
                         (rows.BACK_REBASE, self.lane, True, "more than documents"))
        self.assertIn("don't rewrite it", row.correction)

    def test_a_conflicting_branch_goes_to_a_session_that_lands_it_unrewritten(self):
        self.push_branch(self.lane, {"docs/plans/other.md": "b\n"})
        self.commit("docs: move the trunk on", {"docs/plans/other.md": "c\n"})
        git(self.work, "push", "-q", "origin", "main")
        git(self.work, "fetch", "-q", "origin")
        (done,) = land_spec.adopt(self.product, self.items(self.lane), out=lambda *_a: None)
        self.assertIn("conflicts", done[2])
        self.assertEqual(self.runs()[self.lane]["lane"]["state"], "BACK")
        corr = lifecycle.corrections(pool_mod.sessions_path(self.product))
        self.assertEqual(corr["F-0001"]["why"], "conflict")

    def test_a_plan_on_a_pre_lane_branch_is_landed_by_a_session_on_the_lane_branch(self):
        self.push_branch("old/widgets", {"docs/plans/widgets.md": "# widgets\n"},
                         subject="widgets plan")
        (done,) = land_spec.adopt(self.product, self.items("old/widgets"), out=lambda *_a: None)
        self.assertIn("no spec/plan lane branch", done[2])
        corr = lifecycle.corrections(pool_mod.sessions_path(self.product))
        self.assertEqual(corr["F-0001"]["branch"], self.lane)
        self.assertEqual(corr["F-0001"]["why"], "not a lane branch")
        self.assertIn("old/widgets", corr["F-0001"]["text"])

    def test_a_spec_approved_feature_with_a_plan_off_the_trunk_yields_only_the_spec_triple(self):
        spec_lane = rows.branch_for(self.product, "spec", "F-0001")
        items = self.items(self.lane, stage="spec-approved",
                           spec_evidence=f"spec on {spec_lane} (review r1 APPROVED)")
        self.assertEqual(land_spec.wanted(items), [("F-0001", spec_lane, "spec")])

    def test_a_feature_owed_a_correction_is_skipped(self):
        self.push_branch("old/widgets", {"docs/plans/widgets.md": "# widgets\n"},
                         subject="widgets plan")
        land_spec.adopt(self.product, self.items("old/widgets"), out=lambda *_a: None)
        self.assertIn("F-0001", lifecycle.corrections(pool_mod.sessions_path(self.product)))
        self.push_branch(self.lane, {"docs/plans/f-0001.md": "# widgets\n"})
        self.assertEqual(land_spec.adopt(self.product, self.items(self.lane),
                                         out=lambda *_a: None), [])

    def test_a_spec_and_a_plan_landing_each_get_their_own_job(self):
        spec_lane = rows.branch_for(self.product, "spec", "F-0002")
        git(self.work, "checkout", "-q", "-b", spec_lane, "main")
        self.commit("spec(F-0002): the widget spec", {"docs/specs/f-0002.md": "# widgets\n"})
        git(self.work, "push", "-q", "origin", spec_lane)
        git(self.work, "checkout", "-q", "main")
        git(self.work, "fetch", "-q", "origin")
        self.push_branch(self.lane, {"docs/plans/f-0001.md": "# widgets\n"})
        items = self.items(self.lane)
        items["F-0002"] = {"id": "F-0002", "type": "feature", "decided": True, "state": "Active",
                           "stage": "spec-approved",
                           "evidence": [f"spec on {spec_lane} (review r1 APPROVED)",
                                        "plan on origin/main"]}
        self.assertEqual(sorted(land_spec.wanted(items)),
                         sorted([("F-0001", self.lane, "plan"), ("F-0002", spec_lane, "spec")]))
        done = land_spec.adopt(self.product, items, out=lambda *_a: None)
        self.assertEqual(sorted(done), sorted([("F-0001", self.lane, ""), ("F-0002", spec_lane, "")]))
        self.assertEqual(self.runs()[spec_lane]["job"], "land-spec-f-0002")
        self.assertEqual(self.runs()[self.lane]["job"], "land-plan-f-0001")


class LandingWhyFence(unittest.TestCase):
    def test_every_why_a_producer_writes_is_in_the_vocabulary(self):
        """C6's fence: every token :func:`land_spec.why_not_as_is` can return, plus
        :func:`asf.harvest.lane.send_back`'s two literals, is in :data:`rows.LANDING_WHY` — the
        two writers cannot drift from the reader."""
        producer_tokens = {'not on origin', 'not a lane branch', 'nothing past the trunk',
                           'more than documents', 'lane refusal', 'conflict', 'gate red'}
        self.assertTrue(producer_tokens <= set(rows.LANDING_WHY))


if __name__ == "__main__":
    unittest.main()
