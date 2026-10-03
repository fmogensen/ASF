replan: F-0170 6db6d5a0aebe

The groom's reshape cuts F-0170 to **putting the 32 on a clock**: the standing `writes:` overlaps
the invariants audit reports are resolved by the clocked tick writing an order over them, not by an
operator hand-editing sixteen cards. That is the spec's `## The design` §1 and §2 — one definition
of the overlap that violates I3 (C1, C2, C3) and the tick pass that writes the order (C4, C5, C6) —
and it is carried by T-0544 and T-0545, both landed.

The card's other half is the one the cut removes: `asf doctor` saying `NEEDS OPERATOR: step groom is
on no clock`, and the install-time guard the spec's §3 builds so that a step left on no clock fails
the install that left it there (C7). That is T-0546, the Feature's only open Task. **This replan
therefore rewrites no Task, mints none, and drops one.**

Both halves of that statement were checked in this worktree at the branch point rather than read
out of the record, because a replan that drops the last open Task of a Feature is the end of that
Feature and is worth being sure about.

**The kept half is on the trunk and the audit it exists to clear reads zero.**
`_after_edges`, `ordered` and `unordered_overlaps` are in `asf/invariants.py:221`, `:242` and `:251`
— T-0544's one definition, with `_intersecting` gone. `serialize_overlaps` is at
`asf/tick/widen_footprint.py:594` and `run()` calls it last at `:652`, after `revert_overlaps`,
exactly as the plan's Task 2 step 7 asks; `step_health.widen_footprints` calls `run()`
(`asf/tick/step_health.py:160-166`) and the step reaches it at `:203`. `widen.SERIALIZED` is at
`asf/feeder/widen.py:46`. The three acceptance classes the two Tasks were cut to write are all
present — `tests/test_invariants.py:500` `OrderedOverlapTests`, `tests/test_backlog.py:785`
`CheckOverlapTests`, `tests/test_widen.py:544` `SerializeOverlapTests` — and
`python3 -m unittest tests.test_widen tests.test_invariants` is `Ran 88 tests … OK` here.

Most of all, the thing the Feature was filed for is gone: `invariants.record_audit` over the
operator's own record (read-only) returns **0 findings, 0 of them I3**. The card says 32 and the
plan's PD2 measured 58; the clocked pass has since serialized every one of those pairs. The 32 are
on a clock, and the clock has run.

**The dropped half is unstarted, so nothing is being reverted.** `asf/doctor.py` has
`check_clock_steps` at `:878` and **no** `clockless_steps` beside it; `asf/install.py` contains no
`clockless` reference at all. No line of T-0546 exists on the trunk, so the standing rule that a
replan never reverts landed work does not bite here — there is nothing landed under this Task to
keep.

**The drop dangles no `after:`.** T-0546 carries `after: none`, and no Task of this Feature names it:
T-0544 is `after: none` and T-0545 is `after: T-0544`. Removing it leaves no card waiting on a card
that will never land.

---

### Drop T-0546: the install-time guard for a clock-less step is the card's second half, which the cut removes — it is unstarted, and the condition it would catch is not in the record today

`doctor.clockless_steps` splitting the step **names** out of `check_clock_steps`' **lines**, and
`install.py`'s `_clockless_row` failing step 8 with `step(s) on no clock: <steps>` at its three
post-install success returns (PD6), is the whole of the spec's §3 and of Story S-36852. It is a
good guard and the plan argues for it honestly — *"the reason to build a guard is not that the thing
is broken now but that nothing noticed when it was"* (PD7). It is also not *putting the 32 on a
clock*. The 32 are I3 overlaps between Active Tasks; `ASF_STEPS` on no clock is a scheduler
condition that shares no file, no concept and no order with them — the plan's own Task 3 header
says so (*"Independent of Tasks 1 and 2 in every way — no shared file, no shared concept, no
order"*), and that independence is exactly what makes it severable by a cut aimed at the other
half. The decision names the first half and this Task is the second, so it goes.

The live reading says the same thing from the other side, and it is the reason this drop costs
nothing today: `doctor.check_clock_steps(env.load_product('asf'))` returns **`[]`** in this
worktree. Every `asf`-owned step is on one of the operator's clocks — the groom step that the card's
description reports as clock-less included. PD7 measured this when the plan was written and it still
holds, so the symptom the card filed under this half fixed itself before any code was written for
it, and the Task was already building a guard with no live defect behind it.

What survives the drop, and must not be read as having been decided against: `docs/specs/f-0170.md`
keeps its §3, its C7 and its acceptance block 4 intact — the approved spec is not edited by a
replan — and Story S-36852 keeps them. **That Story is left with no Task by this cut**, which is a
consequence of the decision rather than a hole in it, and is stated here so the record carries it
plainly: whatever card next picks the install guard up inherits a written design (`clockless_steps`
with its docstring, `_clockless_row` at three returns and not at two), a measured hole in the
spec's own snippet (PD6: the `repaired (clock retried)` path), and the reason the five existing
step-8 tests stay green against it (PD6: `product='demo'` does not load, so the soft `except`
catches it). None of that is lost by dropping the Task; it is why the Task was cheap and why it
will be cheap again.
