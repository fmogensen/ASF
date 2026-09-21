# Prior art — the 2026-08 prototype

P0.5 of the ASF 0.1 plan (not part of this repository; cited below as `plan:<line>`).

**The subject.** The prototype repository, 446 commits, 2026-08-20 → 2026-08-27. A TypeScript
chain runner: 19 specs (`docs/specs/05`–`23`), a ten-directive `CONSTITUTION.md` with
`RATIONALE.md` behind it and `TRIGGERS.md` beside it, 14 role prompts (`agents/*.md`), 5 chains
(`chains/*.ts`), ten gates (`core/gates.ts`), 30 frozen eval tasks plus 8 live ones (`evals/`),
and **143 backlog findings — 80 `fixed`, 49 `decided`, 14 `deferred`** (`backlog/*.md`, verified
by count).

**What this document is.** The old framework is prior art, not a base ([ADR
0001](../decisions/0001-prior-art-not-base.md)). No code is carried. Every idea below cites the
line it comes from, so a reader can check it rather than trust it.

**One caveat about status.** `docs/backlog.md:56-61` says closure is *derived from a run*, and a
typed `fixed` is history. So `decided` does **not** reliably mean unbuilt — `b103` is typed
`decided` and its work landed in commit `b5b1ef3`. Counts below are of the typed field.

---

## (a) The 19 specs and the 10 directives

Every unprefixed `file:line` below is a citation into the prototype repository, i.e. `prototype:<path>:<line>`. "Where in ASF 0.1" names a plan section or a card from the plan's
own ranked list (`From the old ASF`, plan:235-240).

### The specs

| # · idea | Evidence it worked | Call | Where in ASF 0.1 |
|---|---|---|---|
| **05 · permissions + worktrees** — the tree is snapshotted before the call; writes outside `writes:` are reverted with content kept under `runs/<id>/reverted/`. *Detects and reverts; never prevents* (`docs/specs/05-permissions-worktrees.md:13`, `CONSTITUTION.md:68-74`) | b20 fixed (a `code` phase reached no boundary at all); b25 fixed (`chains/feature.ts` ended with `git add -A`); b1 fixed (isolation moved per-phase → per-run, D15) | **adapt** | Card 5 *writes-boundary hold* — harvest **holds**, does not silently revert; plan:231 |
| **06 · panels** — several members dispatched in one batch, tallied; a failed member abstains on the record; quorum floor 2; a split escalates (`docs/specs/06-panels.md:21`, `TRIGGERS.md:37-39`) | X03, `chains/feature.ts:59-66`: `plan-gate` judged 18 plans on 72 model calls and rejected 2 (303 s/run); `review-gate` rejected 4 of 8 on 16 calls and caught every real defect. b70 fixed (every panel could tie); b65 decided (`product`+`security` approved 42/42) | **adapt** | Card 6 *panels as an option* in F-0131 — three Sonnet reviewers, split → Opus adjudication; plan:228 |
| **07 · web UI** — read-only client over the trace, rowid-cursor polling, no build step (`docs/specs/07-web-ui.md:22`, D12) | b57 fixed (console shipped); b68/b110/b118/b129/b133 still open on it | **skip** | Out of scope (plan:344). CLI stamped tables only |
| **08 · the feature chain** — grill → spec → plan → build → test → review-gate → document → commit → merge(human) → land (`chains/feature.ts:28-260`) | The dogfood line: every ASF change above it carries `ASF-Run:` (`TRIGGERS.md:75`). b83 fixed — one chain for every change made cost *inverted* against size | **adapt** | The **tick rows** (plan:131-133): CARD → SPEC, PLAN → CODE, REVIEW → FIXER → ADJUDICATE; size classes replace one ceremony for all |
| **09 · the CLI** — one binary, `run`/`tend`/`ui`/`learn`, `--run <id>` rejoins (`docs/specs/09-cli.md:52`) | b3 fixed (resume re-ran everything); b12 decided (`asf brief/status/revert/intake`) | **adopt** | `asf init tick new groom status roadmap backlog rules metrics harvest product migrate-epic` (plan:133-134) |
| **10 · operator config** — one file, one loader, one refusal; default where a default is a true statement, refuse where it would be a guess (`docs/specs/10-operator-config.md:17`) | b23 fixed (`promptsDir` resolved against the process dir); b124 fixed (the operator's ruling lived in a shell script and a constant) | **adopt** | `~/.ASF/config.yaml` + `products/<name>.yaml`; *"no convention lives in code"* (plan:79) |
| **11 · self-amendment** — the factory may argue with its constitution on its own branch; it cannot adopt the argument on its own (`docs/specs/11-self-amendment.md:33`, `CONSTITUTION.md:20-23`) | b9 fixed; b19 fixed (`amends:` declared, never delivered); b78 decided (bootstrap deadlock) | **adapt** | Card 3 *self-amendment policy* — **propose only, humans approve and merge**; the eval-gated loop is 0.2 (plan:229) |
| **12 · evals + the `ASF-Eval:` gate** — frozen tasks, binary acceptance, a set hash, a trailer verified from the commit's own blobs (`docs/specs/12-evals.md:27`) | 30 tasks, ≥1 must-fire + ≥1 must-not-fire per gate (`evals/tasks/`, `evals/README.md:43-44`) | **adopt** | `evals/` wave 8 (plan:151); see §(c) |
| **13 · the skill** — the operator's surface, a routing table with lazy-loaded cookbooks (`docs/specs/13-skill.md:28`) | Taken from a public upstream software-factory project (`docs/prior-art.md:12`); b11 deferred (specs for `install.sh`) | **adopt** | `plugin/` — `/asf:*` skills, one script each, golden headers, stamp line (plan:44) |
| **14 · the code-phase boundary** — a `code` phase is a phase; `DEFAULT_PROTECTED` must reach it (`docs/specs/14-code-phase-boundary.md:42`) | b20 fixed. Before it, `code('x', { run: 'sed -i "" … CONSTITUTION.md' })` ran unchecked (`:12`) | **adapt** | Hooks refuse by rule id (plan:45); `PreToolUse` is the code-phase equivalent |
| **15 · verify before amend** — the work must pass the rules **as they stood before this run amended them** (`docs/specs/15-verify-before-amend.md:39`) | b21 fixed; the gate is `priorRulesPass` (`core/gates.ts:1221`) | **skip in 0.1** | Nothing to verify while no session may edit the amendable set. Re-enters with 0.2 |
| **16 · the chain-graph generator** — an authored diagram of a thing that changes is a claim with no check behind it (`docs/specs/16-chain-graph.md:15`) | `enforce/check-graph.sh` byte-compares the committed SVG; b35 deferred (three of seven audit checks catch drift generation would prevent) | **adapt** | *One place per fact*; generated tables + stamp line (plan:216) |
| **17 · the backlog seam** — items are typed intent, **every status is derived**; `writes:` is the footprint, `where:` is the grant (`docs/specs/17-backlog.md:30`, `docs/backlog.md:47-53`) | b67 fixed (66 items, zero footprints, so the factory was serial); b55/b91/b128 on the model's thinness | **adopt** | The record: seven item types, `index.json`, derived state (plan:130); the closest match to what already exists |
| **18 · failure classification** — a configuration error is not a transport error; three retries on a missing credential is a promise that something might change (`docs/specs/18-failure-classification.md:6-22`) | b93, b102 ×2, b116, b121, b131, b132 — all fixed | **adopt** | **No card yet — GAP 3.** Proposed as a card |
| **19 · pinned roles** — every role identity read once before phase one and digested to `runs/<id>/identities.json`; the run is attributable after (`docs/specs/19-pinned-roles.md:49`) | b28 fixed (an amended identity was read by the run that amended it); b60 decided (the chain itself is still unpinned) | **adapt** | **Partly a GAP.** Role agents ship in `plugin/agents/` (plan:45); nothing pins what a session ran under |
| **20 · the prompt budget** — four token dimensions itemised, never summed; a cap per phase (`docs/specs/20-prompt-budget.md:6-20`) | Run `2v1w10`: 11,622,009 input for two completed phases, 159 requests, 3 sessions | **adopt** | Card 7 — `metrics/sessions` carries input/output/cache-read/cache-write; per-job caps (plan:227) |
| **21 · prompt evals** — a live set against a seeded fixture repo, `repeats` + `threshold`, readings keyed `<setHash>-<configHash>` (`docs/specs/21-prompt-evals.md:39`) | b32 fixed (a `20/20` gates reading satisfied a check on a 7-file `agents/` commit); b103 — the instrument shipped with **zero of its eight tasks** | **skip in 0.1** | Needs a live model and a fixture repo. 0.2, with a quarter of real data |
| **22 · the grill** — a request is interrogated before it is spent on; a request naming an existing document **refines** it; the licence to reinterpret ends at the seam (`docs/specs/22-grill.md:32`, `CONSTITUTION.md:39-41`) | Run `3y720b`: regenerated a 286-line spec from one sentence, shed eight named tasks, shipped a harness returning `Math.floor(task.threshold * 0.8)` (`docs/specs/22-grill.md:16-25`). b38 fixed | **adopt** | Card 2 — `asf idea` = the interrogator; `CARD → ENRICH`; a rule that bounces a rewrite (plan:225) |
| **23 · the preamble** — **93 % of a run's cost is the agent discovering what the runner already knows**; facts are a `preamble:` field on the phase, code-generated (`docs/specs/23-preamble.md:12-13, 81`) | Run `1b6l3f` `build`: 12,318,685 input tokens, 96 tool calls, 18.0 min for one phase (`:23`) | **adopt** | Card 1, ranked first on cost — every brief opens with a generated preamble; metric is input tokens per session before/after (plan:226) |

### The ten directives

`CONSTITUTION.md` is prepended to every call, so every byte is paid on every call (`:6-7`).
`TRIGGERS.md:23-77` is the honest coverage table — which directives have a mechanism and which
are **NONE · judgement**. That table, not the constitution, is the thing worth copying.

| # · directive | Evidence | Call | Where in ASF 0.1 |
|---|---|---|---|
| **1 · Solve what matters** — the ask is a symptom; *the licence to reinterpret ends at the seam* (`CONSTITUTION.md:27-41`) | `TRIGGERS.md:25-26` — **NONE**, judgement. Made mechanical only by spec 22 / `frontierEmpty`; b38 fixed | **adopt** | Card 2 (`asf idea`) + the *refine, never regenerate* rule (plan:225) |
| **2 · Create value** — *if you can write the invocation down, it is not a model call*; **looking is charged too** (`:43-58`) | `TRIGGERS.md:28` — affordance, not a gate. b34 decided: model-driven discovery = 93 % of run cost | **adopt** | Card 1 preamble + Card 7 prompt budget; `code` steps in the tick |
| **3 · Secure by design** — least privilege, secrets never ship, *a capability list is not a boundary* (`:60-76`) | `TRIGGERS.md:29-33`: boundary ✅ detects-and-reverts; secrets ⚠️ **path patterns only, nothing reads content**; least privilege ⚠️ all three adapters declare `enforcesAccess: false` | **adopt** | Security posture: deny-rules, permission modes, MCP allow-list, redaction, SECURITY.md + threat model (plan:135, 217) |
| **4 · Automate or die** — deterministic first; **parallel by default** (`:78-87`) | `TRIGGERS.md:35` — panels only; *nothing checks that a plan's independent steps ran together*. b42 fixed (the frontier computed parallelism and `tend` ran serially) | **adopt** | Waves by footprint (P2, plan:141); the tick's footprint gate |
| **5 · Never send a human to do an agent's job** — the escalation ladder; route a decision by what it is about; **never ask without proposing**; a `human` phase parks rather than blocks (`:89-133`) | `TRIGGERS.md:36-41`: escalation-parks ✅, quorum ✅, *never ask without proposing* **NONE**. b53 decided (the merge seat is anonymous) | **adapt** | **The approval matrix as code** — `approvals:` in `products/<p>.yaml`, `auto \| groom \| human-now`, read by hooks and the tick (plan:244-246) |
| **6 · Data-driven, always** — cite the source; **an absence is reported as an absence** (`:135-143`) | `TRIGGERS.md:42-44` — `criteriaHaveEvidence` ✅ *where a chain lists the gate*; `RecallResult.indexed` renders the rule into the prompt | **adopt** | Evidence + `links.impl`/`links.test`; `ingest` derives state; zero cards without a `source:` (plan:277) |
| **7 · Don't trust, validate** — **a report is a claim, not a result**; a phase defaults to `fail`; **a near miss is corrected in place, never restarted** (`:145-160`) | `TRIGGERS.md:45-50` — the densest row in the table, six ✅ and no gaps. The one directive that is fully mechanised | **adopt** | Card 4 *typed envelopes* — a JSON report per kind beside the prose; a parse failure goes back to the session, F-0130 (plan:230) |
| **8 · Record all, improve all** — files are the record, the trace is the queryable mirror; memory is read-**then-write**, both halves mandatory (`:162-186`) | `TRIGGERS.md:51-55`: record ✅ ✅ ✅, **write-back NONE — `Brain.write` is called by nothing (b24)**. The half that compounds is the half that was never wired | **adapt** | Metrics streams + `file-bugs` + the groom. **Name the memory interface; the backend belongs in operator config** |
| **9 · Version everything, lose nothing** — commit each coherent unit; *unpushed is not saved*; **verify against the base you will land on, not the base you forked from**; version claims are global (`:188-214`) | `TRIGGERS.md:60-62`: commit-per-step ⚠️ *once per run, not per step*; **base drift on merge — NONE**; version claims global — **NONE**. b82/b97/b126 are that gap arriving | **adopt** | Trains per lane per product, `CONFLICT → REBASE`, merge queue as a provider (plan:148, 168); semver + generated changelog (plan:218) |
| **10 · Don't reinvent the wheel** — question zero (*is this the standard library's job*); three probes: **here, anywhere, in what you already have**; *a document the author addressed to you outranks their source* (`:216-241`) | `TRIGGERS.md:63-67`: zero-deps ⚠️ posture not a check; `core/` depends on no vendor ✅ *an assertion, not an intention*; **the three probes — NONE, judgement** | **adapt** | `asf init` as an **adopter** — discover conventions, adopt existing material, never scaffold from blank (plan:255-269) |

**Tally over 29 rows: adopt 16 · adapt 10 · skip 3.**
(specs: adopt 9, adapt 7, skip 3 — 07, 15, 21. directives: adopt 7, adapt 3, skip 0.)

---

## (b) The 143 findings, clustered

Each class carries its ids so the count is checkable. **"Answer in ASF 0.1"** names the plan
section or card that closes the class; **GAP** means the plan as approved has no answer and the
spec (P1) must produce one. This is the checklist.

| # | Failure class | n | Ids | Answer in ASF 0.1 |
|---|---|---|---|---|
| 1 | **The line cannot run a night alone** — no orchestrator; a crash holds the queue; the supervisor cannot see the engine it merged | 14 | b47 b50 b51 b52 b58 b76 b86 b88 b92 b106 b107 b108 b135 E01 | `asf tick` + scheduler adapters + `asf.workers` (`--resume`, caps) + `asf doctor` (plan:148-150, 215). **Partial GAP**: nothing reconciles a dead session's claim, and nothing halts the line on repeated failure |
| 2 | **Cost is unbounded, and most of it is discovery** | 12 | b31 b34 b39 b73 b77 b80 b81 b83 b87 b95 b111 b112 | Card 1 preamble (cost, ranked first) + Card 7 prompt-budget dimensions + size classes (plan:226-227, 148). **GAP 1**: no attempt cap per item and no already-merged guard — b111 (11.7 %) + b112 (16.6 %) = **28.3 % of all spend** |
| 3 | **A failure is attributed to the wrong cause** — the gate blames the phase for the machine, the base, or a refusal it cannot see | 7 | b40 b61 b97 b104 b105 b109 b115 | **GAP 4.** The plan's red-CI row names a diagnostician role (plan:232) but no inherited/environmental control |
| 4 | **A provider or quota event is read as a quality verdict** | 7 | b93 b102 b102-dup b116 b121 b131 b132 | **GAP 3.** Quota guards exist in `~/.ASF/config.yaml` (plan:57); nothing classifies a refusal, a 402, a 529 or an allowance message as *not an attempt* |
| 5 | **The writes boundary leaks, or refuses the wrong thing** | 8 | b18 b20 b25 b44 b45 b78 b79 b122 | Card 5 harvest-holds (plan:231). **GAP 2**: `writes:` is used both to size waves and to hold a branch — b122's exact defect, and it runs in the dangerous direction |
| 6 | **Isolation and the shared tree** — the operator's checkout on the critical path; a stranger's edit charged to the phase | 7 | b1 b10 b13 b14 b15 b59 b136 | Worktrees per job under `~/.ASF/state/<product>/` (plan:80); P4b takes the operator's checkout off the merge path |
| 7 | **Self-amendment verifies its own weakening** | 4 | b9 b19 b21 b28 | Card 3 — **no session edits the amendable set; it proposes, humans merge** (plan:229). The strongest available answer: the class is removed rather than gated |
| 8 | **A verdict decides nothing; a panel cannot be judged** | 6 | b41 b64 b65 b70 b94 b125 | REVIEW → FIXER → ADJUDICATE (plan:131); Card 6 panels as an option with an odd roster and a split → Opus (plan:228) |
| 9 | **A gate is missing, or runs in no phase** | 3 | b29 b71 b127 | `asf rules` checks + hooks + reviews-as-checks (plan:147, 289); the dogfood CI runs the whole tick (plan:249-251) |
| 10 | **The eval instrument does not cover the lever it is stamped on** | 6 | b22 b32 b43 b46 b85 b103 | `evals/` wave 8, must-fire/must-not-fire, scorecard, the no-task+tool hook (plan:151). **GAP 5**: no lever/coverage concept and no `ASF-Eval-None:` escape |
| 11 | **Concurrency and claims** — two operators invisible to each other; a frontier that sizes the wrong thing | 4 | b42 b67 b75 b90 | Footprint gate in the tick; `~/.ASF/state/<product>/inflight`, id ranges per job (plan:80, 150) |
| 12 | **Merge and base drift** | 3 | b82 b89 b126 | Trains per lane per product, `CONFLICT → REBASE`, merge queue as a provider (plan:148, 168) |
| 13 | **There is no front door a person can type into** | 6 | b12 b38 b69 b114 b123 b130 | Card 2 — `asf idea "<text>"`, the interrogator writes the cards and asks into the groom (plan:191-193) |
| 14 | **The operator cannot see the line** | 10 | b57 b68 b98 b99 b100 b110 b118 b129 b133 E03 | `asf status`/`roadmap`/`backlog` stamped tables; no web UI in 0.1 (plan:344). **Partial GAP**: b118/b133 — two surfaces answering *what is startable* and neither is what the line starts; the plan gives `status` and `tick` separate computations |
| 15 | **The backlog model is too thin** — one item kind; `deferred` is a one-way door; a ruled item comes out unroutable | 5 | b55 b72 b91 b128 E02 | Seven item types, epics/features/stories/tasks/bugs/decisions/rules, groom + stale rows (plan:130-132). **Partial GAP**: nothing re-checks a deferral against the reason for it (b128) |
| 16 | **The record cannot answer who, what, or which version** | 7 | b4 b5 b26 b27 b48 b53 b60 | Typed intent / derived state, `index.json`, `schema_version`, `moved_from`/`moved_to`, metrics events with item ids (plan:130, 212). **Partial GAP**: b60 — nothing pins the brief, role file or rule set a session ran under |
| 17 | **Tokens are recorded; money never is** | 4 | b30-dup b33 b74 E04 | `asf.metrics` — streams, rollup, cost, **USD pricing table**; tokens per Feature beside USD on the scorecard (plan:146, 227) |
| 18 | **Doctrine contradicts itself, or is recited and unenforced** | 9 | b2 b6 b7 b24 b35 b36 b37 b49 E05 | Rules as checks + hooks; *one place per fact*; three docs surfaces one audience each (plan:147, 216). `TRIGGERS.md` itself is the pattern to copy — see §(c) |
| 19 | **The operator's ruling is not in the operator's config** | 4 | b16 b23 b96 b124 | `~/.ASF/config.yaml` + `products/<p>.yaml`; *no convention lives in code* (plan:79). **Minor GAP**: nothing says the tick re-reads config per iteration (b96) |
| 20 | **Docs drift; the repo will not build from a clean clone** | 6 | b8 b11 b17 b30 b117 b119 | GitHub Actions on the OSS repo; `sample/` quickstart followed by a fresh no-context session; a grep for the operator's product name returns nothing (plan:322-324) |
| 21 | **The factory learns nothing from itself** — nothing reads two runs together; it has filed one `open` item in its life | 2 | b54 b113 | Every error logged → Bugs → **PROPOSED** cards, humans approve (plan:134, 327). The 0.2 loop is built on a quarter of this data |
| 22 | **One installation, one company** | 1 | E06 | Multi-product from day one + the SaaS seam (plan:69-83, 180-204) |

**143 total. 5 classes carry a full GAP; 6 more carry a partial.**

### The five biggest GAPs

1. **No attempt cap and no already-merged guard** (class 2 · b111, b112). The old factory spent
   219.3 M tokens re-building work already on `main` and 309.5 M on fourth-and-later attempts —
   **28.3 % of everything it ever spent**, measured over 4.2 days. The plan has budgets per Epic
   as a card and per-job token caps; neither counts attempts per item nor checks `main` before
   starting.
2. **`writes:` means two things and the disagreement runs the dangerous way** (class 5 · b122).
   The plan uses a Task's `writes:` both to size footprint-disjoint waves (P2) and to hold a
   branch at harvest (Card 5). b122 is exactly that: the frontier said two items were parallel
   while the boundary granted each of them the other's files. The spec must separate *footprint*
   from *grant*, as `docs/specs/17-backlog.md` separated `writes:` from `where:`.
3. **Nothing classifies a failure** (class 4 · 7 findings; spec 18 unadopted). A 529, a 402, a
   weekly-limit message, a turn that returned no JSON and a panel saying *no* are five different
   things. The old ASF read them as one and paid for it eight times in twenty-two minutes
   (b121). The plan's tick has no failure taxonomy, so a refused session will consume an attempt
   and a retry budget for a run that never happened.
4. **A red test is blamed on the phase with no control** (class 3 · b61, b104, b105, b109).
   Inherited (already red at base), environmental (green when re-run in the same tree), and
   phase-caused are three verdicts; the plan has one. Under concurrent lanes this was not rare —
   the same 1,092 tests went green in 90.11 s and red at 237.56 s in the same tree (b109).
5. **The eval set can be stamped on a change it does not measure** (class 10 · b22, b32, b43,
   b103). The plan's `evals/` is must-fire/must-not-fire per judging tool with no notion of
   *which lever a commit moved*. The old ASF let a seven-file `agents/` commit satisfy the check
   with a gates-only `20/20` reading, then built the coverage rule and the `ASF-Eval-None:`
   escape to record the gap **in the commit that widened it** (`evals/README.md:77-98`). 0.1
   should ship the coverage line even while every lever but one reads zero.

---

## (c) The ten gates and the eval discipline

The reference for `asf.rules` and `evals/`.

### The design rule, first

> *"A gate returns **one check per thing it looked at**, not a verdict. Violations are derived
> from the failed ones."* — `core/gates.ts:4-6`

Two consequences the whole design rests on:

- **A green gate can answer *what did you verify*.** A boolean gate can only say it passed
  (`core/gates.ts:5-8`). `violationsOf` derives failures in one place so check and verdict can
  never disagree (`:1628`).
- **No gate reads the agent's opinion of its own work, because that opinion is the thing under
  test** (`:10-11`). `frontierEmpty` reads `runs/<id>/frontier.json`, not the envelope
  (`:996-998`); panel findings are read from `{{handoffDir}}/<role>.json` keyed on the members
  the panel actually seated, never on a role name compiled into the gate (`:840-844`).
- **A gate that passes when it was given nothing to check is worse than no gate**: it reports
  verification that did not occur (`:148-155`, `:638-640`).
- **Every gate on a phase runs, even after one fails** — a partial list makes the retry fix one
  thing and meet the next objection, at a full model turn each (`:1637-1641`).

### The ten

| Gate | What it checks | One check per | Fires for real on |
|---|---|---|---|
| `artifactsExist` `:163` | every declared path exists and is non-empty; a path resolved only against the operator's checkout **and committed there** is refused — a phase never produces a tracked file | declared artifact | b59 — a handoff written to `handoff/…` instead of `{{handoffDir}}/…` killed a paid run and seeded the checkout |
| `changesMatchDiff` `:203` | declared set == real diff set, **both directions**. An undeclared change is one the review never sees | path, each side | scope creeping in under a green run |
| `testsPass` `:632` | **the exit code is the verdict, never a reading of the output** — then three controls: name the failing tests; re-run them *here* (green ⇒ environmental); re-run at the base (red there ⇒ `inherited`, and **still fails the phase**) | failing test, plus `(re-run)` and `(base)` | b61, b109, b104. The base is consulted lazily and cached per (repo, commit, file-set) `:398` |
| `commandSucceeded` `:768` | for a phase whose command is a git pipeline, not a suite: exit 0, and the note carries the command's own words **head-first across both streams** | the command | b115 — a 1,129-test-green run died reading `GateViolation: testsPass … ASF-Eval: … 28/28` while `nothing to commit, working tree clean` sat unread on stdout |
| `criteriaHaveEvidence` `:808` | each reported criterion cites a **locator** — a path, a path:line, a backticked command or test, an exit code (`LOCATOR` `:795`). Evidence that restates the criterion fails | acceptance criterion | *"a verdict without evidence is an opinion"* (`agents/reviewer.md:11`). Deliberately silent on whether criteria were **met** — a review that ran correctly and found the work wanting is a **successful phase** |
| `priorRulesPass` `:1221` | the work passes the rules **as they stood before this run amended them**. Five answers in order: created-by-this-run ✓ · digest == `constitution.json` ✓ · digest ∈ `identities.json` ✓ · it is a program → run it · anything else **not ok** | amended path | b21, b28. A run that amends nothing pays one failed `readFile` and gets one green check |
| `frontierEmpty` `:1007` | every branch the grill opened is closed: a recommendation on every question, no shrug answer (`SHRUGS` `:977`), a named respondent, and **a `fact` never put to a person** — facts are looked up | branch | b38, run `3y720b`. A failure here stops the chain **before `spec`**, so the phase is never paid for |
| `smellBaseline` `:927` | one check per smell in a **fixed baseline of twelve**, Fowler's order, **the list lives in the gate, not in the role** (`SMELLS` `:878`) | smell | a review that quietly stopped considering `shotgun-surgery` shows as a red check, not a shorter report nobody counted. `ok` answers *was it examined*, never *does the code have it* |
| `loopIsRed` `:1419` | six fixed items — `command`, `already-ran` (read from `raw.jsonl`, the record not the claim), `red-capable` (**the declared symptom must appear in the output**), `deterministic` (5 identical verdicts), `fast` (≤ 10 s mean), `agent-runnable` (no tty, stdin closed, 60 s kill) | criterion, always six | *"no red-capable command, no hypothesis"* (`agents/diagnostician.md:13`). The gate **runs the loop** — determinism and speed have no other evidence |
| `noDebugInstrumentation` `:1543` | one grep for `[DEBUG-<handle>]` over the tree; markdown excluded so the convention can be documented; **grep exit ≠ 0/1 fails** — a search that did not run is not evidence nothing survived | surviving tag | tagged instrumentation dies to one grep, and the last phase runs that grep |

**For `asf.rules`:** the shape to copy is *one check per thing looked at*, *derived violations*,
*the list lives in the check not in the prompt*, *never read the agent's account of its own
work*, and **a note that shows the failure rather than naming it** (`failureHead` `:66`).

And copy `TRIGGERS.md`'s discipline, not just the rules: a table of every rule against **what
mechanically enforces it**, with `NONE · judgement` written out where nothing does
(`TRIGGERS.md:25-27, 34, 41, 55, 61-62, 67, 77`). *"A directive that is only recited is
decoration"* (`CONSTITUTION.md:12`). That table is what makes a rule set auditable, and it is
cheap: `asf rules check` can generate it.

### The eval discipline

> *"An eval is not a test. A test asserts a contract and is edited whenever the contract
> legitimately changes. An eval is a **measurement instrument**: frozen, scored as a rate, and
> compared between a candidate and an incumbent."* — `evals/README.md:6-8`

| Rule | Statement |
|---|---|
| **Must-fire / must-not-fire** | *"A new gate arrives with **two** tasks at minimum — one where it must fire, one where it must not. One of the two alone measures an opinion rather than a discrimination."* (`evals/README.md:43-44`). Shipped: **30 tasks over 10 gates**, every gate with both |
| **Set equality, not containment** | *"A gate that also fires on something unrelated fails the task"* — because *"one that always fires is miscalibrated"* is half of what the set detects (`:38-41`). `note` is never compared |
| **The freeze rule, three layers** | (1) **separation** — `tasks/` holds tasks and nothing else; harness and runner are guarded by `__tests__/`. (2) **the check** — a commit touching a task *and* a lever (`agents/`, `chains/`, `core/gates.ts`) fails. *"You may change the set; you may not change it in the same breath as the thing it measures."* (3) **the set hash** — a changed set gets a new id, so an old reading becomes **incomparable** rather than silently comparable (`:46-58`) |
| **The trailer** | `ASF-Eval: <setHash> <passed>/<total>`. *"The hash says **which instrument**; the count says **what it read**."* `check-evals.sh` recomputes the hash from the commit's own blobs, so a trailer not produced by the set that commit contains is caught **at any point in history, with no checkout** (`:60-69`) |
| **No trailer from a dirty set** | `run.ts` prints none while `tasks/` is dirty — *"a scorecard from an uncommitted set describes an instrument no commit contains"* (`:71-72`) |
| **A red set exits 0** | *"A failing eval is a reading to act on, and making it a build failure would make 'edit the task' the obvious repair — the one thing a frozen set forbids."* The build fails only when a commit **claims a measurement it cannot produce** (`:16-20`) |
| **Coverage, and the escape** | A verifiable trailer is not a **relevant** one (B32). Paths map to levers: `agents/**` + `prompts/<role>/system.md` → **prompts**, `chains/**` → **chains**, `core/gates.ts` → **gates**, `models` has no path. A lever change the set has no task for lands with `ASF-Eval-None: <lever> <why>` — it needs a reason, excuses only the lever it names, and **every one is counted and printed** (`:77-98`) |
| **The gap is recorded where it is widened** | *"That rule is red today, because nothing measures prompts — which is the finding, not a reason to write a task that makes a check green."* (`:87-89`) |
| **The four levers, in descending leverage** | prompts · chains · gates · models (`docs/improvement.md:43-45`). The set covers the **third**. Deterministic tasks have no variance, so the variance guardrail is inert until the first non-deterministic task (`evals/README.md:112-113`) |

**Why the loop failed before, in the old ASF's own words** (`docs/improvement.md:6-20`): no
trajectory data (transcripts, not labelled outcomes) and no held-out set, so *"beats the prior
version"* had no operational meaning. A runner removes the first **by construction** — a run is
accepted or not, so a finished run is a labelled trajectory. That is the argument for ASF's own
metrics streams.

---

## (d) The 14 role prompts, distilled

### What a role definition must carry

Five sections, every one of the 14 files has them, and none is longer than 53 lines
(`agents/*.md`, 475 lines total — mean 34).

| Part | Where | Rule |
|---|---|---|
| **Identity** | frontmatter: `name`, `kind` (`chain` \| `panel` \| `operator`), `purpose` one line, `routes: [...]` on panel roles, `doctrine: full` where the thin tier is not enough | `core/roster.ts:8-11` reads **frontmatter and nothing else** — *"the roster is data an operator can edit, not a list compiled into the engine"*. **No model call chooses a panel**: selection is a deterministic keyword match over `routes:` (`:12-15`) |
| **Doctrine** | 4–8 bullets, each the rule *and the incident behind it* | `agents/interrogator.md:15-18` cites the three runs that died on an incomplete `writes:`; `agents/diagnostician.md:10-12` states *"phase one is the whole skill"*. A rule with no incident is decoration |
| **Output** | `## Report` per the schema, **plus the named side file** — `frontier.json`, `loop.json`, `<role>.json` | The report contract is **generated from the schema**; a template writing its own is refused (`TRIGGERS.md:49`, D8). A per-role field on the shared envelope is a private contract every consumer then has to know (`core/gates.ts:836-841`) |
| **Economy** | *"Every file you open is re-sent on every turn after it. Read what your brief names, then what those files reference — do not survey."* | `agents/reviewer.md:22-25`. Directive 2's *looking is charged too*, restated per role. This is the section spec 23's preamble replaces with generated facts |
| **Boundaries** | what it may write, and what it explicitly does **not** do | `agents/reviewer.md:27-29` — *"You do not fix what you find."* `agents/diagnostician.md:43-46` — the fix is a later phase with its own boundary; *"merging the two is how a diagnosis becomes an unreviewed change"* |

**What the file does *not* carry, and must not.** Model, backend and effort are **operator
config**, not identity — `assignments: { builder: { name, model, extraArgs } }`
(`config.example.json`, D14). Tools/access come from the phase: no `writes:` ⇒ the call goes out
`readOnly` (`chains/feature.ts:35-39`). Gates come from the phase too (`gates: ['frontierEmpty']`
`:43`). One role file, many phases, many models — b6 fixed the collision where "roster" meant
both.

**Two sharp rules worth carrying verbatim:**

- *"`status` says whether the review ran; `approved` says whether it passed. They are different
  questions and a red review is a successful phase."* (`agents/reviewer.md:19-20`)
- *"An interview is a failed grill. Every question you raise costs someone's attention… If you
  could have found it, you owed it."* (`agents/interrogator.md:19-21`)

### The 14, and what ASF 0.1 does with each

| Old role | `kind` | What it is for | ASF 0.1 |
|---|---|---|---|
| `builder` | chain | implement the plan inside the declared boundary | → **coder** |
| `spec` | chain | acceptance criteria executable *without asking anyone anything* | → **spec-writer** |
| `planner` | chain | order the work over files that exist, so the builder decides nothing material | → **planner** |
| `reviewer` | chain | one finding per acceptance criterion, each with evidence | → **reviewer** |
| `interrogator` | chain | the frontier: find it, close it, record what closed it | → **interrogator** (`asf idea`, Card 2) |
| `diagnostician` | chain | build the red loop first; hypotheses with predictions; tagged probes | → **diagnostician** (red-CI triage) |
| `security` | panel | attack the guarantee before a user does | → **security** |
| `documenter` | chain | the docs surface | → **documenter** |
| `scout` | chain | read-only recon, absence reported as absence | **not in 0.1's set** — the preamble (Card 1) is the cheaper answer to the same need: facts the runner already knows, generated rather than discovered |
| `standards` | chain | the change as code, on the twelve-smell baseline, **independently of the spec** | **not in 0.1's set — and `smellBaseline` then has no producer.** Either drop the gate or add the role; the plan does neither |
| `architect` | panel | the smallest thing that works | not in 0.1's set — a panel option (Card 6) |
| `product` | panel | is this worth building at all | not in 0.1's set — b65: approved 42/42, never rejected |
| `privacy` | panel | personal data, legal basis, transfers | not in 0.1's set |
| `co-founder` | operator | the deciding vote when a panel splits, weighing `~/.config/asf/operator.md` | replaced by *split → Opus adjudication* (plan:228). The **operator profile** is worth keeping: standing preferences, not per-decision ones (`operator.example.md:11-12`) |
| — | — | — | **new in 0.1, no old counterpart: `fixer`, `prober`, `harvester`** — each needs a role file with all five sections above |

**Note for P1.** `product`, `privacy` and `architect` being absent from 0.1's named set is
consistent with b65's evidence. `standards` being absent is not consistent with shipping
`smellBaseline`'s equivalent; and `scout`'s absence is only safe if the preamble actually lands
first, because otherwise the coder pays scout's cost inside its own session, which is b34.

---

## (e) What the old ASF never got to, and why

| Thing | How far it got | Finding ids | Why it stopped |
|---|---|---|---|
| **The operations console** | Spec 07 shipped `ui/` as a read-only client with no build step (D12, `docs/specs/07-web-ui.md:22`) — 5 files, a rowid-cursor poll, gate evidence per item. b57 (*"`asf ui` is a debugger for one run, and the founder needs an operations console"*) is `fixed` | **b57** fixed · **b68** decided (the backlog — the thing the founder steers by — has no view at all, *"and the founder is not a developer"*) · **b98 b99 b100** (palette specified and unshipped; cards styled twice; the four `--kind-*` hues answer to nobody) · **b110** (the runs table shows the item's whole markdown instead of its id) · **b118 b129 b133** (*what is ready* has three computations and none is the line; the queue is a census, never a list) · **E03** decided | Every surface was built for a developer reading a trace. The founder's questions — *which item, what is startable, what waits on me* — needed the backlog as a data source, not a template, and E03 never closed. **In 0.1: out of scope** (plan:344). Stamped CLI tables instead |
| **Prompt evals — the top lever** | Spec 21 built the entire instrument: `live-harness.ts`, `live.ts`, `cost.ts`, `fixture/seed` + `materialise.ts`, `readings/` keyed `<setHash>-<configHash>`, `bun run evals:live` — **and shipped with zero of the eight tasks it measures with** (`evals/live-harness.ts:51` resolved `LIVE_DIR` to a directory that did not exist). The eight landed later, in `b5b1ef3` | **b103** (*"six backlog items wait on a directory that does not exist"*) · parked behind it: **b34** (93 % of cost), **b37**, **b39**, **b31**, **b36**, **b85** · **b22** (the set measures gates only) · **b32** (a `20/20` gates reading satisfied a check on a 7-file `agents/` commit) · **b43** (the lever map counts a role's identity as a prompt change and its task as nothing) | *"An eval that does not exist is worse than no eval"* (`docs/improvement.md:58-60`): a prompt eval needs a live model **and** a seeded fixture repository. The honest consequence is written into the tooling rather than hidden — `bun run evals` still prints `prompts 0` and the escape records the gap in the commit that widened it (`evals/README.md:87-89`). The cost: three `need` items held for a frozen thing nobody had built, so *"the condition currently reads as a way of not doing the work rather than a way of proving it"* (`backlog/b103-…:44-45`). **In 0.1: skip** — 0.2, on a quarter of real data |
| **The SaaS — more than one company** | A written epic and nothing else | **E06** deferred | *"Stated by the founder on 2026-08-22 as **direction, not work to start** — a factory that cannot feed one product will not feed six."* What it names as the real obstacles: everything scoped to a run is single-valued (`cwd`, `runsRoot`, `promptsDir`, `agentsDir`, one SQLite trace, one `backlog/`, one roster in one config); and **spend, not isolation, is the binding constraint** — *"There is no cap of any kind — per run, per night or at all."* The open question it leaves: N installations vs. tenancy inside one. **In 0.1: multi-product from day one** (plan:69-83) and the SaaS seam designed for but not built (plan:180-204) — which answers E06's first obstacle by construction and leaves the second (a global spend ceiling) open |
| **The improvement Gate itself** | — | `TRIGGERS.md:77` — *"The Gate · — · — · **NONE** · needs a frozen held-out set"* | The loop the constitution declares (act → reflect → gate → adopt) has a mechanism for *act* and *record* and none for *adopt*. This is exactly why the operator postponed self-improvement to 0.2 (plan:229) |
| **Memory write-back** | The `Brain` interface shipped; the read half is wired into every prompt | **b24** decided — *"Directive 8's write-back half has no call site"*; `TRIGGERS.md:55` — *"`Brain.write` is optional and called by nothing"* | The half that makes the system compound was the half never wired. Worth one line in the 0.1 spec: if the memory interface ships, both halves ship or neither does |

---

## What P1 should take from this, in one paragraph

Adopt the preamble, the grill, typed envelopes, the derived-state record, the gate shape (*one
check per thing looked at*), the eval freeze rules and `TRIGGERS.md`'s discipline of writing
`NONE` where nothing enforces a rule. Adapt the boundary to *hold, not revert*, panels to an
option, self-amendment to propose-only. Skip the web UI, prompt evals and verify-before-amend
for 0.1. Then close the five GAPs — an attempt cap and an already-merged guard, a clean split
between footprint and grant, a failure taxonomy, red-test attribution, and eval lever coverage —
because between them they account for the largest measured waste in the old factory's whole
history.
