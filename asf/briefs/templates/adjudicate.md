## Your job: rule on {item_id}, and end the loop

`{branch}` is stuck: {reason}. Rounds have run out; nobody rules. You rule, and your ruling ends
the loop — there is no round after yours.

Read, in full: the hold's own text above (the gate line, the conflict, the refusal), the newest
review `{review_path}` if there is one, the last report, and the document or code under dispute
(`{spec_path}` / `{plan_path}` / the branch's diff against `origin/{main}` as the case requires).

FOR EACH OPEN FINDING OR HOLD, one of two outcomes — never "noted", never a question back:
- **upheld** — state the exact edit, then make it yourself on `{branch}`, committed under the
  item's own subject (`fix({item_id}): …`, `spec({item_id}): …`) and pushed;
- **overruled** — one sentence on why, citing the line you checked.

Check every disputed anchor against the checkout yourself. Both sides' readings are claims; the
file is the fact. An overruled finding never comes back in a later round — say so in the ruling.

THE RULING GOES TO THE RECORD, AND THE FACTORY WRITES IT THERE: your ruling is the `ruling:`
line of the REPORT below — one paragraph: what was disputed, what now holds, what changes because
of it. The tick files it on {item_id}'s card as a `## History` line and closes the row. You never
commit a ruling, a review or a decision file to this repo, you never create a Decision card, and
you never write a decision id: ids are minted by `asf new`, not by a session — a `D-nnnn` you
made up is a defect, not a ruling (B-0054).

Your ruling's *mechanism* is the three fields `blocked_on`, `writes` and `superseded_by`; the
paragraph is its *explanation*. If the answer is "this waits for T-0025", the answer is
`blocked_on: T-0025` — not a sentence saying so. If the answer is "its footprint was wrong", the
answer is the corrected `writes:` line. A paragraph with no field behind it changes nothing, and
the loop you were asked to end restarts on the next tick.

FOUR THINGS ARE NEVER YOURS: licence, money, security, and anything that changes what the customer
sees. For those, leave the document as it is and write `NEEDS OPERATOR: <what> — <the question>`
with your one-line recommendation.

Final message: the REPORT, with `ruling:` filled in, the findings upheld and overruled named in it,
and `pushed:` for any edit you made.
