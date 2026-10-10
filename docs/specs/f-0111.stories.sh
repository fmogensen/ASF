#!/bin/sh
# F-0111 — mint the four Stories the spec-amend session declared.
#
# Written by the adjudicate session asf/adjudicate-f-0111@20261008T002053Z, ruling on F-0111's
# STALEMATE (NO STORIES -> SPEC-AMEND, three rounds). The spec-amend session
# asf/spec-amend-f-0111@20261007T234347Z declared these four Stories as `### S-nnnnn:` headings
# in docs/specs/f-0111.md, with ids from its own claimed block, because it ran in a cloud
# container with no `asf` on PATH and could not mint them (the same gap F-0069 and F-0092 hit).
# It left the mint itself undone -- that gap, not the spec's text, is why SPEC-AMEND kept firing:
# `has_stories()` reads the record's Story-type children, not the spec's prose.
#
# Run this on the factory host, from the record checkout, in this order: mint_id takes the next
# free number of the claimed block, so the ids it prints are exactly the ones already written
# into docs/specs/f-0111.md's `## Stories` and carried by docs/plans/f-0111.md's `stories:` lines.
#
# Each command prints its id. Check each printed id against the comment above it.
set -e
export BACKLOG_ID_RANGE='S:68654-68703,T:68650-68699,B:68650-68699'
export ASF_SESSION='asf/spec-amend-f-0111@20261007T234347Z'

# expect: S-68654
asf new story \
  --parent F-0111 \
  --title 'the resolver names the entry point of the install that is running, and all three writers take its answer' \
  --acceptance '`running_asf` with an `argv[0]` naming `<bin>/asf-live` and a `which` that answers `<bin>/asf` returns `Entry('"'"'<bin>/asf-live'"'"', '"'"'argv0'"'"')` — the install the operator invoked, not the one `PATH` names' \
  --acceptance 'An `argv[0]` of `pytest`, of `asf/cli.py`, of a non-executable `asf` and of a directory each fall through the `argv0` route instead of being written into a hook' \
  --acceptance 'With `argv[0]` falling through and a fixture venv whose `asf` script is executable and whose `#!` line names the interpreter passed as `executable`, `running_asf` answers that script with route `venv`' \
  --acceptance 'With the `argv0` and `venv` routes both falling through, `running_asf` answers `which('"'"'asf'"'"')` made absolute, with route `path`' \
  --acceptance 'A fixture venv whose `asf-factory` dist-info resolves to a tree other than the running package is refused by the `venv` route, and the ladder falls through to `PATH`' \
  --acceptance 'A package importable only through a `PYTHONPATH` shadow — the clock-snapshot shape — fails `same_install`, so the `venv` route never answers from a snapshot' \
  --acceptance 'When no route answers, `runnable_asf` refuses with `NEEDS OPERATOR: this asf install has no entry point to name (argv[0] <argv0>, none beside <executable>, none on PATH) — pipx install asf-factory`, naming all three inputs, and writes no hook file' \
  --acceptance 'A route that answers with a missing path, a non-executable file or a directory refuses with `NEEDS OPERATOR: <path> is not an executable asf — no hook is written naming it; pipx install asf-factory`, byte for byte as today, and writes no hook file' \
  --acceptance 'With the ladder answering `<bin>/asf-live`, the `pre-push` body'"'"'s `exec` line names `<bin>/asf-live` and not the `asf` that `which` returns' \
  --acceptance 'With the ladder answering `<bin>/asf-live`, the foreign-hook refusal tells the operator to add `"<bin>/asf-live" redact --pre-push --product <p>` — this card'"'"'s own report, asserted against' \
  --acceptance 'With the ladder answering `<bin>/asf-live`, every Claude Code settings command written names `<bin>/asf-live`, suffix and all' \
  --acceptance '`install`'"'"'s summary line ends with `entry: <path> (<route>)`, the route being the `argv0`, `venv` or `path` literal that answered, and its `rc` is unchanged at 0 on success and 2 on a refusal' \
  --acceptance '`docs/guide/troubleshooting.md`'"'"'s `asf is not on PATH` row carries the no-entry-point refusal the code now emits, while the installer row `asf is not on PATH after pipx install` is left as it is'

# expect: S-68655
asf new story \
  --parent F-0111 \
  --title 'both recognisers read back every form asf can write, so a second install from the same install changes nothing' \
  --acceptance '`is_git_hook_ours` reads back as ours every form asf can write — a bare `asf`, a quoted and an unquoted absolute path, a quoted and an unquoted suffixed basename, `-m asf.redact` and `-m asf.cli redact` — for `pre-commit` and for `pre-push` — proven by tests/test_hooks_recognise.py' \
  --acceptance '`is_git_hook_ours` rejects every near miss: a different program whose name starts with `asf`, the line inside a shell string, a `#` comment, and a command that merely ends in `asf` — proven by tests/test_hooks_recognise.py' \
  --acceptance 'A line naming the other hook is not read as this hook'"'"'s, and `hook_entry` answers `None` for it — proven by tests/test_hooks_recognise.py' \
  --acceptance '`hook_entry` answers the entry-point path a recognised line names, the interpreter for a module form, and `None` for a bare name and for a line that is not ours — proven by tests/test_hooks_recognise.py' \
  --acceptance '`_is_ours` reads back every settings command `hook_command` can write — a directory path, a suffixed path, a bare `asf` and `-m asf.cli` — for a rule hook and for `approvals`, with and without the `--product` tail — proven by tests/test_hooks_recognise.py' \
  --acceptance '`_is_ours` rejects a command naming a different program, one whose `--product` disagrees, and one carrying trailing text — proven by tests/test_hooks_recognise.py' \
  --acceptance 'A second and a third `hooks install` from a suffixed install leave both git hooks, the repo settings and the worker account settings byte-identical — proven by tests/test_hooks_recognise.py' \
  --acceptance 'After those runs each settings file holds exactly one `PreToolUse` entry carrying exactly one hook, never a second appended one — proven by tests/test_hooks_recognise.py' \
  --acceptance '`approvals_missing` — the doctor'"'"'s `approvals-hook` row — reads an account as having the hook through every form `_is_ours` accepts, and still answers the account for a near miss and for no settings file at all — proven by tests/test_hooks_recognise.py'

# expect: S-68656
asf new story \
  --parent F-0111 \
  --title 'hooks install repairs a hook of its own whose entry point has gone or names a checkout, and never downgrades a usable one' \
  --acceptance '`entry_point_source` answers `('"'"'editable'"'"', <checkout>)` for an entry point whose shebang interpreter'"'"'s `asf-factory` dist-info has `dir_info.editable` true, taking the checkout from the `file://` url' \
  --acceptance 'It answers `('"'"'installed'"'"', <site-packages>)` for a dist-info that carries no `direct_url.json`' \
  --acceptance 'It answers `None` for a venv with no dist-info, for a `#!/bin/sh` wrapper, for a script with no shebang, for a missing path and for a directory' \
  --acceptance 'It spawns no subprocess: the whole class passes with `subprocess.run` patched to raise' \
  --acceptance '`prefer_entry` is true when the written entry point is missing or not executable and the running one is usable, and true when the written one is editable and the running one is not' \
  --acceptance '`prefer_entry` is false for an unknown source on either side, false for an equal pair, and false when the running entry point is not itself usable' \
  --acceptance 'A hook whose named entry point is deleted is repaired by the next `ensure_git_hooks`, whose summary carries `repaired <path>: <old> → <new>`, and a second run changes the file no further' \
  --acceptance 'A hook naming an editable fixture install is repaired when the running install is not editable' \
  --acceptance 'A `pre-commit` of asf'"'"'s carrying a second `asf check --staged` line and a trailing operator line is repaired on its `exec` line only, with every other byte identical — the record'"'"'s own commit gate survives the repair' \
  --acceptance '`asf init`'"'"'s `PRE_COMMIT` and `PRE_PUSH` templates, whose line names a bare `asf`, are left exactly as written by `ensure_git_hooks` and by a second run of it' \
  --acceptance 'Alternating `ensure_git_hooks` and `install` runs from two usable non-editable installs leave the hook file and the settings file byte-identical after each — no pair of installs can flap them' \
  --acceptance 'An editable running install never replaces a non-editable written one, in a git hook or in a settings command' \
  --acceptance 'A matching settings command `prefer_entry` declines to replace is left exactly as it is and still counts as found, so no second entry is appended and `_write_merged` writes no file at all' \
  --acceptance 'A settings entry whose written entry point is removed before a second `install` is replaced by the newly resolved one'

# expect: S-68657
asf new story \
  --parent F-0111 \
  --title 'the doctor'"'"'s redaction-hooks row says when a product'"'"'s gate names a dead entry point or a checkout' \
  --acceptance '`check_redaction_hooks` reports `<path> names <entry>, which is not an executable asf` for a hook of asf'"'"'s whose named entry point is not an executable file, and the row is red' \
  --acceptance 'It reports `<path> names an editable install (<checkout>)` for a hook whose entry point'"'"'s source is editable, naming that checkout, and the row is red' \
  --acceptance 'The same editable hook leaves the row green when `is_factory_repo(product)`, with the detail `pre-commit, pre-push in <n> repos (editable install: this product is the factory'"'"'s own source)`' \
  --acceptance 'A hook whose entry-point source cannot be read leaves the row green and adds neither a problem nor a claim about the install' \
  --acceptance 'A record hook naming a bare `asf` leaves the row green and adds nothing' \
  --acceptance 'A product that is not the factory and whose hooks are all fine keeps the detail `pre-commit, pre-push in <n> repos`, unchanged' \
  --acceptance 'The detail'"'"'s remedy ends `— asf hooks install --product <p>, run from the install the hook should name`' \
  --acceptance 'The row'"'"'s name, its position in the doctor table and its `required` flag are unchanged' \
  --acceptance '`check_redaction_hooks` writes nothing — every hook file'"'"'s bytes are identical before and after — and a foreign hook is still red' \
  --acceptance '`docs/guide/troubleshooting.md`'"'"'s redaction-hooks table carries a row for each of the two new findings, each with the remedy: rerun `asf hooks install --product <p>` from the install the hook should name' \
  --acceptance '`docs/guide/getting-started.md`'"'"'s `asf hooks install` step says each hook names the install that ran the command'
