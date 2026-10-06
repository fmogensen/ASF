#!/bin/sh
# F-0069 — mint the ten Stories this adjudication derived.
#
# Written by the adjudicate session asf/adjudicate-f-0069@20261006T205646Z, which ran in a
# cloud container with no record: no ~/.ASF, no backlog_dir, no `asf` on PATH, so `asf new`
# could not run there (the same gap the three prior spec-amend sessions on this item almost
# certainly hit — NO STORIES kept firing because no Story child card was ever minted, not
# because the spec's own Stories text was wrong). The ids below are this session's claimed
# block (S:53204-53253). Run this on the factory host, from the record checkout, in this
# order: mint_id takes the next free number of the claimed block, so the ids it prints are
# exactly the ones already written into docs/specs/f-0069.md's ## Stories.
#
# Each command prints its id. Check each printed id against the comment above it.
set -e
export BACKLOG_ID_RANGE='S:53204-53253,T:53200-53249,B:53200-53249'
export ASF_SESSION='asf/adjudicate-f-0069@20261006T205646Z'

# expect: S-53204
asf new story \
  --parent F-0069 \
  --title 'The `conventions.security` block and the path classes a diff touches' \
  --acceptance 'python3 -m unittest -v tests.test_security_paths'

# expect: S-53205
asf new story \
  --parent F-0069 \
  --title 'A sensitive diff owes a row per class and is never read at the shallowest level' \
  --acceptance 'python3 -m unittest -v tests.test_precheck'

# expect: S-53206
asf new story \
  --parent F-0069 \
  --title 'The lane makes the pass required for a sensitive diff, whatever the product waived' \
  --acceptance 'python3 -m unittest -v tests.test_lane_security'

# expect: S-53207
asf new story \
  --parent F-0069 \
  --title 'The precheck brief names the classes, their files and the rows they owe' \
  --acceptance 'python3 -m unittest -v tests.test_briefs_security tests.test_briefs.GoldenBriefTest'

# expect: S-53208
asf new story \
  --parent F-0069 \
  --title 'The code host'"'"'s secret-scanning and dependency alerts, read with a cache whose staleness is a violation' \
  --acceptance 'python3 -m unittest -v tests.test_security_alerts'

# expect: S-53209
asf new story \
  --parent F-0069 \
  --title 'A violation line may carry its own severity and signature, and a place with one becomes its own Bug' \
  --acceptance 'python3 -m unittest -v tests.test_file_bugs.RuleViolationFieldTests tests.test_file_bugs.SecretAlertTests'

# expect: S-53210
asf new story \
  --parent F-0069 \
  --title 'The nightly exposed-port probe from a runner, and a result that names boxes and never an address' \
  --acceptance 'python3 -m unittest -v tests.test_security_ports'

# expect: S-53211
asf new story \
  --parent F-0069 \
  --title '`asf security` — one parser, four subcommands, the `--check` contract the rule scripts call' \
  --acceptance 'python3 -m unittest -v tests.test_cli.SecurityParserTests'

# expect: S-53212
asf new story \
  --parent F-0069 \
  --title 'The core `rules/` directory — three cards, three scripts, and the band' \
  --acceptance 'python3 -m unittest -v tests.test_core_rules'

# expect: S-53213
asf new story \
  --parent F-0069 \
  --title 'The doctor'"'"'s `security` row, the guide, and the documented config block' \
  --acceptance 'python3 -m unittest -v tests.test_doctor.SecurityRowTests'
