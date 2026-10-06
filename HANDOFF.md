# HANDOFF — G2: permission denials (branch feat/g2-denials)

Delete this file in the final PR.

## Ask (operator-approved: "sessions may run check scripts and read-only commands without approval")
1. The approvals hook (and spawn) grant a read-only allowlist (git read subcommands, ls, cat, head,
   tail, grep, rg, find, wc, the product's declared check and test commands). It must also match
   compound and cd-prefixed forms. Mutating commands stay governed as today.
2. Briefs only ask for commands the sandbox allows. Add a test that every command a brief names
   passes the hook.
3. Harvest runs the product's declared checks itself and attaches the output, so sessions needn't.
4. Add a metric to `asf scorecard`: denials per session, read from the job logs.
5. Report the top denial sources with counts, before and after.
Follow the handbuild rules: generic, configurable, tests first, one PR, merge with
`gh pr merge --squash --match-head-commit` on green. Never start Docker or VMs. Keep the operator
settings' deny rules (docker, colima, multipass, limactl, `open -a Docker`).

## Measured: the real denial source (baseline, job logs ~/.ASF/logs/jobs/*/*.jsonl, all-time to 2026-10-06)
Each run's `result` line carries `permission_denials` [{tool_name, tool_use_id, tool_input}]. The
matching `tool_result` (is_error) text gives the reason.
- 4,632 runs; 1,937 logs with ≥1 denial; **23,870 denials** (Bash 22,504, Write 525, Read 487, Monitor 284).
- Top reasons (count | runtime message | example):
  - 5,224 | "This command requires approval" | `bash tools/check_conventions.sh`. No allow rule existed
    until the console added `Bash(bash tools/check_*.sh*)` today.
  - 1,387 + 67 | "multiple operations… requires approval: bash/sh tools/check_conventions.sh" | `bash tools/check_conventions.sh 2>&1 | tail -30; echo "EXIT $?"`
  - 1,582 | "Contains simple_expansion" | `git merge-base --is-ancestor X HEAD; echo $?`
  - 503 | "shell syntax that cannot be statically analyzed" | heredocs, `T=$(mktemp -d)`
  - 488 | "changes directory before running a version-control command" | `cd <worktree> && git fetch && git log`
  - 394 | "brace with quote character" | python heredocs
  - 323 | "Contains command_substitution" | `$(cat …)`
  - 293 | "A variable in this command can't be checked" | `for f in …; do sed -n …"$f"`
  - 250 | "Contains expansion" | `${VAR:-unset}`
  - 226 + 87 | GIT_EDITOR=true git rebase --continue (env-prefixed git)
  - 151 bash -n; 146 find /Users/…/.asf (outside the working dirs); 140 + 77 scripts/check-claims.sh (a product check)
  - 146 + 67 | "grep/cat reads a relative path after a cd in a compound command"
  - 138 process_substitution `diff <(git show …) <(sed -n …)`; 137 git add in a compound; 118 gh run list
  - 110 xargs; 104 "shell operators"; 92 python -m pytest; 91 `asf show`; 86 `asf --help`; 85 printenv
  - 83 `ls -la /…/worktrees`; 76 grep -v; 74 which asf; 62 cat ~/.claude/settings.json (outside the working dirs, correctly blocked)
- ~/.claude-workers/settings.json already allows `git *`, `ls *`, `cat *` and `cd *`. Those rules do
  not help: the runtime's own shell-safety checks (`$?`, `cd && git`, `$( )`, `<( )`, compound
  parts) override allow rules in headless `-p` (permission_mode acceptEdits, passed via
  `--settings`).
- **Proved (probe, claude 2.1.291, haiku):** the same three commands (`…; echo "rc=$?"`,
  `cd /tmp && git -C <repo> log`, `echo "$(git rev-parse …)" > /dev/null; ls <(echo x)`) gave
  3 denials with no hook. With a PreToolUse hook that prints
  `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}` they gave
  **0 denials**. So the fix is the hook answering *allow* for commands that are provably read-only.
- The ASF approvals hook (`asf/approvals.py` run_hook/_enforce) today only refuses (rc 2) or
  passes through (rc 0, no decision). It never allows. The brief text in asf/briefs/build.py
  PRE_PUSH_DOC_RULE already claims "the approvals hook allows exactly these commands", which is
  false today.
- Where `check_conventions.sh` comes from: plan docs (docs/plans/*.md "Gate commands") and review
  briefs. These are ASF's own repo checks. Harvest already runs them in asf/harvest/harvest.py
  product_gate via `ASF_GATE_SCRIPTS`, which is an `asf_repo` special case and should become the
  generic `check_commands`.

## Done (this commit)
- `asf/readonly.py` (new, untested): a small shell grammar plus the grant predicate.
  - Entry points: `grant(command, cwd, read_roots, declared)` and
    `grant_for(product, command, cwd, environ)`.
  - Handles `&& || ; |`, newlines, `( )` subshells (cwd is restored), `$( )`/`<( )` (judged
    recursively), `$?`, fd duplication, and `>/dev/null`.
  - Refuses heredocs, backticks, `$VAR`/`${}`, `&`, brace groups, and write redirections.
  - Paths: absolute, `~`, `..` and `cd` targets must resolve inside the roots. The roots are the
    cwd's git toplevel plus `ASF_READ_ROOTS` (os.pathsep).
  - git: reads plus listing forms of branch/tag/remote/config/reflog/worktree, and `fetch` without
    `src:dst`. `-c`, `--git-dir` and `--output` are refused.
  - Wrappers: safe VAR= (display vars only), `env -u`, `timeout`, `command`, `time`.
  - Declared commands are `conventions.check_commands` (new), the `pre_push_check` code and doc
    steps (cut at `<placeholder>`), and `test_command`, matched as prefixes. The interpreter and
    script path are canonicalised: bash/sh/./abs.
  - New product key `conventions.read_only_allow` (default true).

## Left / exact next steps
1. **Tests first:** add `tests/test_readonly.py` (hermetic: a tmp git repo as root). Grant cases:
   - every example command above that is read-only;
   - `cd <root> && git log`;
   - `bash tools/check_x.sh 2>&1 | tail -3; echo $?`, declared via check_commands;
   - `diff <(git show HEAD:f) <(sed -n 1,5p f)`.

   Refuse cases:
   - `git push`, `git commit`, `git -c core.fsmonitor=x status`, `rm`, `sed -i`, `find -delete`,
     `> out.txt`, `cat ~/.ssh/x`, `cd / && ls`, `cat ../../x` that escapes the root;
   - `$HOME`, backticks, heredocs, `&`, `GIT_EXTERNAL_DIFF=rm git diff`, `git branch newname`,
     `git config user.x y`;
   - docker, colima, multipass, limactl, `open -a Docker` are never granted.
2. **Hook:** in `asf/approvals.py` `_enforce`, at every `return 0` after classification (nothing
   refused) for `tool_name == 'Bash'`, call `readonly.grant_for(prod, command, cwd, environ)`.
   When it grants, print the allow JSON to **stdout** (add a `stdout=sys.stdout` param to
   `run_hook`/`_enforce`), then rc 0. A non-factory session (no ASF_JOB) stays untouched. Extend
   tests/test_approvals.py.
3. **Spawn:** in `asf/workers/spawn.py` (~line 1183, Job env), add
   `'ASF_READ_ROOTS': os.pathsep.join(add_dirs)`. Do the same in asf/workers/heartbeat.py ~593 if
   it builds a Job.
4. **Conventions:** in `asf/conventions.py` validate_mapping, `check_commands` must be a list of
   command strings and `read_only_allow` a bool.
   - Add `check_commands` to PINNED_VALIDATED_CONVENTIONS in tests/test_env.py if a test demands it.
   - Document both keys in docs/products.example.yaml (next to full_suite_commands) and in
     docs/guide/product-config.md.
5. **Brief test (ask 2):** `tests/test_brief_commands.py`. For each template kind, build a brief on
   a fixture product with pre_push_check, check_commands and test_command set (see
   tests/test_briefs.py helpers).
   - Extract the backticked commands that the brief tells the session to run (pre-push block,
     doc steps, check_commands).
   - Assert that `approvals.run_hook` returns rc 0 with an allow decision for each.
   - Make `PRE_PUSH_DOC_RULE`'s claim true.
6. **Harvest checks (ask 3):** new `asf/harvest/product_checks.py`, modelled on
   asf/harvest/rebuild_check.py (detached job, flock, store keyed by tree+commands, prune).
   - Run each `check_commands` entry on the session's pushed head in a throwaway
     `git worktree add --detach`, as lane.pre_push_check_at does. Store `[{command, rc, tail}]`.
   - The lane calls `ensure()` when it sees a new head.
   - asf/briefs/build.py adds a "Checks on <head> (run by the harvest — do not re-run)" section
     for the review and correct kinds, reading the store. Empty when the product has no
     check_commands, so it is a no-op for a minimal product.
   - Replace harvest.py `ASF_GATE_SCRIPTS`/`asf_repo` with `check_commands` only if that is
     cheap. Otherwise leave it and note it in the report.
7. **Metric (ask 4):** in asf/scorecard, a "denials/session" line over the window.
   - Scan `env.log_dir()/jobs/<product>/*.jsonl` result lines (`permission_denials`) for runs
     ended in the window. Report mean per run, the share of runs with ≥1 denial, and the top 3
     reasons (from the tool_result text).
   - Read-only. Test with fixture logs.
8. Run the targeted tests (`-j 2`): test_readonly, test_approvals, test_briefs, test_brief_commands,
   test_env, test_conventions, test_scorecard, test_workers, test_harvest, test_lane and
   test_config_keys. Also run every tools/check_*.sh.
   - Add a CHANGELOG/notes line per the #767 PR convention (see .github/workflows).
   - Open one PR. Merge on green with `--match-head-commit`.
9. After measurement: re-run the counting script (below) on logs newer than the install time.
   Report the before/after counts per source.

### Counting script (host only)
For each `~/.ASF/logs/jobs/*/*.jsonl`:
1. Map `tool_use_id` to the text of each user `tool_result` with `is_error`.
2. Sum the `permission_denials` of every `type == "result"` line.
3. Bucket each denial by its tool_result text, with
   `re.sub(r'requires approval: (\S+ \S+).*', r'requires approval: \1', msg)`.
