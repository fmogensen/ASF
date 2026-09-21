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

Every commit subject names the card — `fix({item_id}): <what>` — because a commit on the trunk that
names the id is what turns the card `Resolved`; a fix nobody can trace to the card does not exist.

If the fix is ALREADY on `origin/{main}` when you look (someone landed it before this session):
verify it against THE FIX above and run the test that covers it, then make one empty, signed commit
on your branch — `git commit --allow-empty -s -m "fix({item_id}): already landed in <sha> — verified
by <test name>"` — and push. That commit is the evidence the record needs; a branch pushed with no
commit closes nothing and the lane relaunches this session every wave.

Final message: the pushed sha, the test's name and the run's last line, what you changed.
