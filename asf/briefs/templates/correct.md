## Your job: correct {item_id} — the harvest held `{branch}`

The harvest gate held `{branch}` and sent it back to you: your worktree is already on `{branch}`, and the rebase onto `origin/{main}` was started for you — if `git status` shows a conflict it is still in place: resolve it (or, if the rebase finished cleanly, carry on), fix what the failure below names (a conflict is resolved so both sides survive), run the full suite, and push the same branch — never a new one, never a merge of `origin/{branch}` or `origin/{main}` into it, never a force. A push refused as non-fast-forward is the rebase you were handed: stop there and report `pushed: rebased <sha> — the factory publishes`. Change nothing the failure does not ask for; paste the suite's last line in the report.

THE BOUNDARY IS `writes:` — {writes}
When the failure below says the footprint was widened, the paths it added are inside that list now: change them as the failure asks. A file still outside it that must change goes, as a full repo path, on the REPORT's `needs writes:` line with `status: partial` — never edited, never a question to a person.
