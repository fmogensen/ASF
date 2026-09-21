## Your job: fix {item_id} — the incident lane

This is the incident lane, not the Feature ladder: **no spec, no plan, no review round**. The
card's `## Fix` IS the plan, and the test it names is the acceptance. Everything below is the
whole of the process.

THE FIX, from the card:
{fix}

THE TEST THAT CLOSES IT: {tests}
Write that test FIRST and watch it fail, then make it pass. The harvest requires it by name: a
fix that lands without a test that failed before it is not a landed fix, it is a claim.

Branch `{branch}`, cut from `origin/{main}` — the row names it; do not cut a second one.

Do the smallest change that makes the test pass. A refactor you noticed on the way, a second bug
you found next to this one, a rename that would be tidier: all of those go in the report, not in
this diff. Speed here is the point — the lane exists so an incident closes in hours while the
Feature work waits.

Final message: the pushed sha, the test's name and the run's last line, what you changed.
