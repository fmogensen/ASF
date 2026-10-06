#!/bin/sh
# F-0061 — mint the twelve Stories this spec-amend derived.
#
# Written by the spec-amend session asf/spec-amend-f-0061@20261006T205641Z, which ran in a
# cloud container with no record: no ~/.ASF and no backlog_dir, so `asf new` could not run
# there (it exits `NEEDS OPERATOR: no --product, no $ASF_PRODUCT, ... and config.yaml has no
# default_product`). The ids below are this session's own claimed block (S:53104-53153), which
# the brief names as the other sanctioned id path and which `asf/record/ids.py` makes
# deterministic: with the range exported, `mint_id` returns the next free number of the block,
# so run in this order these twelve commands mint exactly S-53104..S-53115 — the ids already
# written into the `## Stories` block and the frontmatter of docs/specs/f-0061.md.
#
# One Story per `## Stories` bullet of the spec, in the spec's own order, which is the positional
# mapping the plan's PD4 already records. Each title is its bullet, verbatim. No acceptance line
# carries a `proven by` citation: nothing of F-0061 has landed at this head — no `rules/`, no
# `asf/rules/hookcall.py`, no `asf/rules/hooks/`, no `tests/test_rule_hooks.py`, no
# `HookInstallTests` in tests/test_install.py and no `RuleHooksRowTests` in tests/test_doctor.py
# — so every line is for the Task that will prove it to cite.
#
# Each command prints its id. Check each printed id against the comment above it.
set -e
export BACKLOG_ID_RANGE=S:53104-53153,T:53100-53149,B:53100-53149
export ASF_SESSION=asf/spec-amend-f-0061@20261006T205641Z

# expect: S-53104
asf new story \
  --parent F-0061 \
  --title 'The core `rules/` directory — seven cards, six scripts, and the reserved band' \
  --acceptance '`rules/` holds `README.md`, seven `R-000N.md` cards and seven `r000N.sh` shims — seven, not six: every one of the seven cards declares a `hook:` event, which the plan settles as PD5 against the spec prose this title still carries' \
  --acceptance 'Every card in `rules/` parses as frontmatter carrying `id`, `type: rule`, `title`, `check` and `hook`' \
  --acceptance 'Every card'"'"'s `hook:` value is a subset of `hooks.EVENTS` — `PreToolUse`, `PostToolUse`, `Stop`' \
  --acceptance 'Every card'"'"'s `id` is inside the reserved core band `R-0001`–`R-0099`' \
  --acceptance 'Every hooked card has a script at `rules/<id-form>.sh` that is executable and whose shim line names `asf.rules.hooks.<id-form>` — `R-0001` to `r0001`' \
  --acceptance 'A card'"'"'s `check:` line is asserted for its shape only and never that the path exists: this repository ships no `tools/checks/`, and the check half binds only through a product'"'"'s own record card of the same basename' \
  --acceptance '`declared_hooks(RULES_DIR)` returns exactly the seven `(event, name)` pairs the cards declare, in the order `sorted(os.listdir)` gives them, and no others' \
  --acceptance '`rules/README.md` states the reserved band, the card shape, and that a product adopts a core rule'"'"'s check half by writing its own one-line `R-` card pointing at the same basename'

# expect: S-53105
asf new story \
  --parent F-0061 \
  --title '`asf hooks install` writes rule hooks where every worker session reads them' \
  --acceptance '`install` over a config with three accounts writes `approvals` and every declared rule hook into each account'"'"'s own settings file, product-less, so each hook resolves its product from `ASF_PRODUCT` at run time' \
  --acceptance 'The product repo'"'"'s `.claude/settings.json` gets the same rule hooks with `--product <p>`, exactly as it does today' \
  --acceptance 'A second `install` changes no byte of any of the four settings files' \
  --acceptance 'An unrelated key already present in a settings file survives the merge untouched' \
  --acceptance 'A product with no `repo_dir` still leaves every worker account hooked, and the refusal is about the repo copy only' \
  --acceptance '`rule_hooks_missing` is empty after an install, and names the account and the hook after one entry is deleted from one account'"'"'s settings file' \
  --acceptance 'The `install` summary line counts the rule hooks written into the worker accounts beside the ones written into the repo'

# expect: S-53106
asf new story \
  --parent F-0061 \
  --title 'A core rule card'"'"'s hook resolves to the core script, and an unresolved one goes red' \
  --acceptance 'With `ASF_CORE_RULES_DIR` at a fixture holding `r0001.sh`, `check_script('"'"'r0001'"'"')` returns that path' \
  --acceptance 'With a record `tools/checks/r0001.sh` present as well, the record'"'"'s script wins — the same override order the check half already has' \
  --acceptance 'With neither, `check_script('"'"'r0001'"'"')` is `None` and `asf hook r0001` exits 0' \
  --acceptance '`asf doctor` goes red on that last state with one `rule-hooks` line naming `r0001` unresolved, and carries `asf hooks install --product <p>` as its one fix line' \
  --acceptance 'The environment `cmd_hook` hands the resolved script carries `ASF_PYTHON` and a `PYTHONPATH` whose first entry imports `asf`' \
  --acceptance 'The `rule-hooks` row is green after an install, and red naming the account and the hook after one installed entry is deleted'

# expect: S-53107
asf new story \
  --parent F-0061 \
  --title 'The hook contract — allow, refuse, fail closed, and one ledger row per refusal' \
  --acceptance '`hookcall.run` with `ASF_JOB` unset returns 0 and writes nothing, whatever `decide` would have said' \
  --acceptance 'A `decide` returning `None` returns 0, prints nothing and appends no row' \
  --acceptance 'A `decide` returning lines returns 2, prints those lines to `out`, and appends exactly one `refused-rule` row carrying `rule`, `item`, `job`, `tool`, `detail` and `ts`' \
  --acceptance 'That `refused-rule` row carries no `hold` key, so a refusal parks no item and relaunches nothing' \
  --acceptance 'A `decide` that raises returns 2, prints `HOOK_ERROR_LINE` naming the rule, and appends one `rule-hook-error` row — the boundary fails closed' \
  --acceptance 'The error row'"'"'s event is `rule-hook-error` and not `hook-error`, so the tick does not announce the approvals guard as broken' \
  --acceptance 'The product'"'"'s `approvals.jsonl` is valid JSONL after all four cases' \
  --acceptance '`refusal(subject, title, rule, *lines)` renders the four-part shape, ending with the literal line `This is not a question for a person: do not print NEEDS OPERATOR for it.`'

# expect: S-53108
asf new story \
  --parent F-0061 \
  --title 'R-0001 and R-0002 — a stage names its files, and the shared stash stack is never touched' \
  --acceptance 'R-0001 refuses `git add -A`, `git add .`, `git add --all`, `git commit -am "x"`, and `git add -u` with no path' \
  --acceptance 'R-0001 refuses the `git add -A` in the second simple command of `echo ok && git add -A`' \
  --acceptance 'R-0001 allows `git add asf/hooks.py`, `git add -p` and `git add -u asf/`' \
  --acceptance 'R-0001 allows `git commit -s -m "git add -A"` — the blanket form inside a commit message is not a stage' \
  --acceptance 'R-0001 allows `git add -A` in a `.git`-less fixture directory: the rule binds a worktree or a repo checkout only' \
  --acceptance 'R-0002 refuses `git stash`, `git stash push -u -m t`, `git stash pop` and `git stash drop` in a worktree' \
  --acceptance 'R-0002 allows the read-only `git stash list` and `git stash show`' \
  --acceptance 'R-0002 allows `git stash push -u -m <tag>` under `rules.r0002.allow_tagged: true`, and only that form' \
  --acceptance 'Both rules return `None` for a call whose `tool_name` is not `Bash`' \
  --acceptance '`conventions` carries a `rules` map, defaulted when absent and named by `shape_findings` when misshapen, with `rules.r0002.allow_tagged` as the only key any rule of this Feature reads'

# expect: S-53109
asf new story \
  --parent F-0061 \
  --title 'R-0003 — the trunk pushes the refspec classifier cannot see, and every force-push' \
  --acceptance 'Refuses a bare `git push` from a checkout whose `HEAD` is the trunk' \
  --acceptance 'Refuses `git push origin HEAD` and `git push origin @` from the trunk' \
  --acceptance 'Refuses a bare `git push` from a branch whose upstream `branch.<name>.merge` is the trunk' \
  --acceptance 'Refuses `git push --force`, `-f`, `--force-with-lease` and `--mirror` to any branch, not only the trunk' \
  --acceptance 'Refuses `git push origin :main` — the trunk deleted by an empty-source refspec' \
  --acceptance 'Allows a bare `git push` from `spec/F-0061` with a matching upstream, and `git push origin spec/F-0061`' \
  --acceptance 'Returns `None` for `git push origin main`, leaving the explicit refspec to `touch_production`, and the refusal text for the shapes it does catch names `touch_production` as the owner of the rest' \
  --acceptance 'An inherited `GIT_DIR` naming another repository changes no answer: the branch and upstream reads run under `hermetic.git_env()`' \
  --acceptance 'The trunk name comes from `ctx.product.conventions.main` and appears as no literal anywhere in the module'

# expect: S-53110
asf new story \
  --parent F-0061 \
  --title 'R-0005 — a session opens no pull request, and a lane'"'"'s body carries its lines' \
  --acceptance 'Refuses `gh pr create`, `gh pr merge` and `gh pr ready` from a coder job, naming the standing rule that the session pushes its branch and the lane opens and lands the PR' \
  --acceptance 'Refuses a `git push` carrying `-o merge_request.*` from a job that is not a lane job' \
  --acceptance 'From a lane job, refuses a `gh pr create` whose `--body` is missing a line `conventions.lane.pr_body_lines` requires, and names each missing line' \
  --acceptance 'From a lane job, reads a `--body-file` resolved against the call'"'"'s cwd, and reads `--body-file -` or a heredoc body from the command text' \
  --acceptance 'Allows a body carrying every required line, in both the literal and the `/regex/` form' \
  --acceptance 'Allows any body under an empty `pr_body_lines` — the default, which means there is nothing to check' \
  --acceptance 'A `--body-file` that does not exist yet allows the call and records exactly one `rule-hook-error`' \
  --acceptance '`conventions` carries `lane.pr_body_lines`, defaulting to empty, named by `shape_findings` when it is not a list, and the lane-shape refusal string names all three lane keys'

# expect: S-53111
asf new story \
  --parent F-0061 \
  --title 'R-0004 — a card written straight to disk is minted from the session'"'"'s range' \
  --acceptance 'Over a record fixture that is a linked worktree, refuses a `Write` of `tasks/T-0900.md` under `BACKLOG_ID_RANGE=T:30550-30599` — the id is outside the block' \
  --acceptance 'Refuses that same `Write` when `BACKLOG_ID_RANGE` names no range for the prefix and `BACKLOG_ALLOW_MINT` is unset' \
  --acceptance 'Allows a `Write` of `tasks/T-30551.md`, an id inside the range' \
  --acceptance 'Allows that refused write once `BACKLOG_ALLOW_MINT=1` is set' \
  --acceptance 'Allows an `Edit` of a card file that already exists — an edit is not a mint' \
  --acceptance 'Allows a `Write` outside the record'"'"'s card folders' \
  --acceptance 'Allows every write in a canonical clone, where `.git` is a directory and not a file' \
  --acceptance 'Returns `None` for a `Bash` call at the same path' \
  --acceptance 'The card folders come from `asf.record.core.TYPES` and the range from `asf.record.ids._id_range` — one parser for `BACKLOG_ID_RANGE`, never a second regex' \
  --acceptance 'The refusal names the range this session holds and `asf new --type <t> --title "…"` as the path that mints from it'

# expect: S-53112
asf new story \
  --parent F-0061 \
  --title 'R-0006 — a card written is a card checked, reported and never refused' \
  --acceptance 'Over a card inside `backlog_dir` carrying two real errors: rc 0, and both `asf check` lines printed under one `R-0006 asf check <relpath>` heading' \
  --acceptance 'The report ends with the line `fix these before you commit — the record'"'"'s pre-commit refuses them anyway.`' \
  --acceptance 'Over a clean card: rc 0 and no output at all' \
  --acceptance 'Over a file outside `backlog_dir`: no `asf check` subprocess is spawned at all' \
  --acceptance 'A check that hangs is cut off by the 20 s timeout, prints one line, and records exactly one `rule-hook-error`' \
  --acceptance 'The rc is 0 in every case — `PostToolUse` fires after the write, so a refusal there could not undo it, only confuse' \
  --acceptance 'Returns `None` for a call whose `tool_name` is not `Write`, `Edit` or `MultiEdit`'

# expect: S-53113
asf new story \
  --parent F-0061 \
  --title 'R-0007 — a session ends with nothing uncommitted and nothing unpushed, refused once' \
  --acceptance 'Over a dirty git fixture worktree the `Stop` is refused with the exact `push_gap` detail `not pushed: <n> uncommitted file(s), <m> unpushed commit(s)`' \
  --acceptance 'The refusal carries the first lines of `git status --porcelain` and the unpushed commit count' \
  --acceptance 'An unpushed commit on an otherwise clean tree is refused with that commit count' \
  --acceptance 'A clean, pushed branch allows the stop' \
  --acceptance 'The file list is capped at 20 lines with `… and <n> more`' \
  --acceptance 'An account name planted in a filename does not appear in the refusal: the whole text goes through `redact.scrub` before it is printed' \
  --acceptance 'A second `Stop` with the same gap allows and records `rule-stop-repeat` — a `Stop` hook that refuses forever is an unkillable session' \
  --acceptance 'The one-shot marker is a file under `<state>/<product>/hooks/stop/<ASF_SESSION>`, built from `env.state_dir(product)` and never from a literal path' \
  --acceptance 'The gap comes from `workers.health.push_gap` and the trunk from `ctx.product.conventions.main` — one definition of finished, not two'

# expect: S-53114
asf new story \
  --parent F-0061 \
  --title 'The named refusals fire in a fixture session, through the installed commands' \
  --acceptance 'In a fixture product, worker account and worktree under a tmpdir `ASF_HOME`, `asf hooks install` runs for real and the account'"'"'s settings file is read back' \
  --acceptance 'Every hook command in that settings file is executed as the runtime would execute it, with the call JSON on stdin and the session environment set — `asf hook <name>` is run, never assumed' \
  --acceptance 'Per rule, the refused call returns rc 2 and its first refusal line is that rule'"'"'s own' \
  --acceptance 'Per rule, the allowed call returns rc 0 with empty stderr' \
  --acceptance 'Each refusal appends exactly one `refused-rule` row to the product'"'"'s `approvals.jsonl`' \
  --acceptance 'One call per rule carrying the wrong `tool_name` returns rc 0 and is silent' \
  --acceptance 'With `ASF_JOB` unset every rule returns rc 0 — the console is a person'"'"'s, not a session'"'"'s' \
  --acceptance 'R-0006'"'"'s refused case is its report, rc 0 with its lines on stdout, and R-0007'"'"'s is a `Stop` over a dirty fixture worktree' \
  --acceptance '`asf rules hooks --json` over that ledger reports seven declared, seven installed, zero unresolved, and the per-rule refusal counts the run produced'

# expect: S-53115
asf new story \
  --parent F-0061 \
  --title 'The measure — declared, installed, refused, and the violations that should fall' \
  --acceptance '`asf rules hooks` over a seeded ledger and a seeded Bug set renders the header `== RULE HOOKS <n> declared, <n> installed in <n> accounts, <n> unresolved`' \
  --acceptance 'One row per rule carries `rule`, `event`, `installed` as `<k>/<n accounts>`, `refused(Nd)`, `errors(Nd)` and `violations(Nd)`' \
  --acceptance '`refused` renders an em dash for R-0006, which cannot refuse, rather than `0`, which would read as enforced and never triggered' \
  --acceptance '`--days` narrows the window and the counts change with it' \
  --acceptance '`--json` carries the same numbers as the table' \
  --acceptance 'A product with nothing installed renders `0/<n>` in `installed` and does not render as healthy' \
  --acceptance '`installed` comes from `hooks.rule_hooks_missing`, the declared set from `hooks.declared_hooks()`, and every count from `approvals.read(product)` — no new metrics stream' \
  --acceptance '`asf rules check` still runs the check after `rules_command` gains the `hooks` choice, and `python3 -m asf.rules.rules hooks` works the way `check` does'
