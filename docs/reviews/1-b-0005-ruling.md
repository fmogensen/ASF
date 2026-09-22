# D-7800 — B-0005 adjudicated: the layout check stands; the loop ends here

## Context

`fix/B-0005` was held three times for adjudication. The reason recorded was "unpushed", not a disputed
finding: `docs/reviews/1-b-0005.md` exists in no ref, local or remote. `origin/fix/B-0005` did not
exist either, so no reviewer ever saw the branch, and a fixer was never answering one. Nobody had
tested the fix commit (`f186b0c`, `asf check` reports each missing item or stream folder with the
`mkdir -p` that fixes it) as a whole.

## Decision

- **No reviewer finding is open, so none is upheld or overruled.** The review this session was sent
  to answer was never written. Any "finding" said to come from that round is void. It does not come
  back in a later round, and this ruling ends the branch's review loop.
- **One defect, found by the adjudicator and upheld:** the fix broke
  `tests/test_schema_version.py::test_index_and_every_card_carry_schema_version`. Its fixture
  (`setUp`, line 18) built a record without the stream folders, and `check` now fails such a record
  on `groom/`, `inbox/`, `metrics/*` and `releases/`. The edit is the one `CheckCommandTests.setUp`
  already makes: create every entry of `asf.init.STREAM_FOLDERS` in `setUp`. It is committed on this
  branch.
- **The fix itself stands as written.** The concern that git drops empty folders, so a fresh clone
  or worktree of a record would fail `check` and its pre-commit hook, is ruled out:
  `asf/init.py:220-221` writes a `.gitkeep` into every entry of `ITEM_FOLDERS + STREAM_FOLDERS`.
- **Environmental noise, not a defect:** 21 `ConfigError: no product config` errors appear only when
  the suite inherits a live job's `ASF_PRODUCT` and `ASF_HOME`. With those unset, all 801 tests pass
  (13 skipped).

## Consequences

- `fix/B-0005` is ready to merge. B-0005 closes when it lands on `main`.
- A record that predates `asf init`'s stream folders fails `asf check` until someone runs the
  `mkdir -p` it names. That is the behaviour B-0005 asked for, so it is not a regression.
- The test suite is not hermetic against an inherited `ASF_PRODUCT` and `ASF_HOME`. That is worth
  its own bug, apart from B-0005.
- The id `D-7800` comes from this job's reserved block `7800-7849`. `BACKLOG_ID_RANGE` reserves that
  block for `S`, `T` and `B` only, so whoever files this card should confirm the `D` number when
  filing.

## Links

- Item: B-0005 — `check` does not validate the record's layout, only its items
- Review answered: `docs/reviews/1-b-0005.md` (round 1, never written, so there was nothing to answer)
- Fix: `f186b0c` on `fix/B-0005`
- Epic: E-0001 — ASF 0.1 — a working factory
