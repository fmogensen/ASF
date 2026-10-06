# HANDOFF — feat/g1g3 (G1 stop re-asking, G3 loops)

Delete this file before the PR merges.

Spec: the operator's evaluation, sections G1 and G3 (with the go/no-go criteria in §5). Common rules:
generic (no product/host names), every policy value is a config key with a default, hermetic tests
that fail first, one PR with a CHANGELOG/notes line per the release workflow, and a squash merge with
`--match-head-commit` once the checks are green.

## Facts found (verified on the live ledgers, read-only)
- The 73 identical T-0042 delivery-code sessions (09-29 15:25 to 09-30 06:14, one head, one card)
  came before the relaunch cap (#488, 09-30 08:11), and that cap now binds.
- Loops the cap still misses: `correct` jobs whose head keeps moving (lane rebases) while the
  report stays the same. Example: 13 sessions on 7 heads with 1 distinct report in about 6 h.
  The cap's `_same` compares the head, so every launch passes.
- Re-park churn: the lane writes a new correction each cycle (lifting the park) and the cap
  parks again. One job had 114 parks and a groom job had 187. These are not sessions, only
  alarm noise.
- `relaunch_cap`, `loop_cap` and `models.cheap_kinds` are in `KNOWN_FLAGS` with no reader. The
  real caps are the constants `relaunch.CAP=2` and `lifecycle.LOOP_CAP=3`.
- Session logs are per job and overwritten, so `lifecycle.result_of(run)` only sees the newest
  run's result. Stamp `report_key` on each run when it is judged (or when it ends).

## Done (committed)
- `asf/workers/loops.py` (new, pure):
  - Flag readers: `relaunch_cap(product)` and `loop_cap(product)`.
  - `settings(product)` gives `same_report` (flag `relaunch_same_report`, default 2) and
    `daily_cap` (flag `relaunch_daily_cap`, default 6, trailing 24 h). `off` disables each.
  - `report_key(text)`: the REPORT block with shas masked and whitespace folded.
  - `judge(runs, card, now, same_report, daily_cap)` returns `(rule, reason)` or None.
  - `replay(sessions, ...)`: a counterfactual replay where a refused launch is not added to the
    job's history.
- `asf/workers/relaunch.py`:
  - `assess(..., guard=None, now=None)`. When the streak rules allow a launch,
    `loop_guard()` runs `loops.judge` over the job's runs since the latest unpark and stamps
    `report_key` on the newest run in memory.
  - `park_fields` adds `loop_key`, from `loop_key(reason)` with counts and shas masked.
  - `alarmed(path, job, item, key)` is True when a park since the latest unpark already carried
    that key.

## Left — exact next steps
1. G3 wiring, in `asf/tick/step_wave.py`:
   - `relaunch_assessment` passes `cap=loops.relaunch_cap(product)`,
     `guard=loops.settings(product)`.
   - `relaunch_capped`, when acting:
     - Persist `report_key` on the job's newest ended run if it is missing:
       `pool_mod.update_session(product, wrow.job, report_key=...)`.
     - Before parking, compute `key = relaunch.loop_key(reason)`. If
       `not relaunch.alarmed(path, job, item, key)`, print `ALARM loop {job} {item} — {reason}`
       once.
   - `_preview_capped` must stay read-only.
2. `loop_cap` reader:
   - `lifecycle.hold(..., loop_cap=None)` passes it to `same_head_loop(cap=...)` and to
     `loop_text`.
   - Callers pass `loops.loop_cap(product)`: health.py (three `lifecycle.hold` calls; `product`
     is in scope) and harvest/lane.py:1293 (`conv` or `lane.conv`) and :3578 (`lane.conv`).
3. Register `relaunch_same_report`, `relaunch_daily_cap`, `groom.reask_days`, `groom.rank_owner`
   and `groom.structural` in `asf/conventions.py` `KNOWN_FLAGS`. Document every flag in the
   `flags:` block of `docs/products.example.yaml`. Product flags are the registry for product
   policy; `config_keys.py` covers only `~/.ASF/config.yaml`, so say so in the PR.
4. G1 in `asf/groom/groom.py` (`cmd_groom`, after `build_groom_sections` and the extra
   sections, before `policy.suppress`):
   - `flags.groom.structural: report` (default) | `ask`. Under `report`, lines in `no_stories`
     and `no_tasks` become `- <id> <title> — <why> (report)` with no `→ answer:` slot, so
     `OPEN_QUESTION_RE` and the adjudicator never see them. An undecided Feature is still asked
     under `undecided_new` / `undecided3`.
   - Sticky questions:
     - New module `asf/groom/sticky.py`. Its digest covers the typed meta minus the
       groom-written fields (decided, removed, reconciled, rank, parent, severity, blockedBy,
       budget_sessions, budget_usd, landed, reshape) plus the body minus `## History`.
     - Ledger: `<state_dir>/groom-answered.json`, keyed `"<id>|<section group>"`. Group
       inbox, undecided_new, undecided3 and undecided14 as `decide`.
     - Record: `apply_groom_answers` gains `answered=None` to collect `(iid, section, by)` for
       filled answers by the adjudicator or operator, not by controller. After applying,
       `cmd_groom` writes the digests.
     - Filter: drop an open line whose key has the same digest and is younger than
       `flags.groom.reask_days` (default 7; `off` disables). Print the count as `sticky N`.
       With no product, nothing changes.
   - `flags.groom.rank_owner: code` (default) | `adjudicator`:
     - Under `code`, `apply_groom_answers` skips `rank <n>` with the reason "rank is owned by
       code (flags.groom.rank_owner)". The rank stays stable, and the feeder's order (Epic rank,
       Feature rank, id, `priority: later`, blockers) is the rule.
     - Update `asf/briefs/templates/groom.md` and its golden fixture so `rank <n>` appears only
       under `adjudicator`.
     - The PR body records what changes: about 191 rank writes a week stop.
5. Update the existing tests that expect structural questions:
   - test_groom.py ~902/919/938
   - test_groom_policy.py ~77, 514-581, 781-796, 1123
   - test_for_you_end_to_end.py 92/121
   - test_briefs.py 113
   - tests/fixtures/briefs/golden/groom.md

   Either assert the report-line form, or set `groom.structural: ask` in fixtures that test the
   policy pass.
6. Replay fixtures and tests. Build them from the evaluation's replay data:
   - `groom.json` (fields p, day, id, t=qtype, verb).
   - The sessions ledgers, each run reduced to item, kind, started, launch_head, card_digest,
     cause, report_key, end.
   - Anonymise them: map product names to a/b and drop the question and answer text.

   Fixtures: `tests/fixtures/replay/groom_answers.json` and
   `tests/fixtures/replay/loop_sessions.jsonl`. Tests:
   - `tests/test_loops.py`: unit tests for `judge`, `report_key` and the flag readers, plus
     replay pins of the sessions avoided by the same-report rule and by the daily cap in the
     window 2026-09-29T06:00Z to 2026-10-06T06:10Z.
   - `tests/test_groom_sticky.py`: digest exclusion, re-ask after N days or a changed digest,
     report lines, rank_owner, and replay pins of the questions avoided.
   - Add cases to `tests/test_relaunch.py` for guard, `loop_key` and `alarmed`.
   - Report the pinned numbers in the PR.
7. Add the CHANGELOG/notes line per `.github/workflows` and the #767 convention. Delete
   HANDOFF.md, open one PR, wait for green checks on the exact head, then
   `gh pr merge <n> --squash --match-head-commit <sha>`.

## Test modules to run (`-j 2`, then every `tools/check_*.sh`)
tests.test_loops tests.test_relaunch tests.test_groom_sticky tests.test_groom tests.test_groom_policy
tests.test_for_you_end_to_end tests.test_briefs tests.test_lifecycle tests.test_trunkclose
tests.test_env tests.test_config_keys tests.test_tick_steps
