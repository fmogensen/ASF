> Source of truth for the directives; the rule cards that enforce each live in the record's rules/ and are listed in the TRIGGERS column.

# The ASF constitution — draft

Ten directives. Each one is here because something went wrong without it, and each one names the
cards that enforce it and says honestly whether anything mechanically does.

**This is a document people read and cards enforce — not a prompt prefix every session pays for.**
The prototype prepended its constitution to every model call, so every byte was charged on every
call, and its own coverage table admitted that several of its directives had no mechanism at all.
That table, not the text above it, is the part worth copying: *a directive that is only recited is
decoration.*

**The TRIGGERS line.** Every section ends with one:

| value | meaning |
| --- | --- |
| `checked` | a rule check fails when the directive is broken, and the tick reports it |
| `hooked` | a hook refuses the action at the tool boundary, before it happens |
| `recited only` | nothing enforces this yet; the line names what would |

A section may carry more than one value when different parts of the directive are enforced
differently. The honest value today is `recited only` for four of the ten, and that is the point of
writing it down.

**Status: draft.** The wording is the operator's to edit. The evidence and the card references are
the parts that should survive editing unchanged. Ruling: [[D-0018]].

---

## 1 · Solve what matters

The ask is a symptom. Find what the person is actually trying to achieve, and solve that — but the
licence to reinterpret ends at the seam: a request that names an existing document **refines** it and
never regenerates it.

**Evidence.** In the prototype, one run took a single sentence and regenerated a 286-line
specification from scratch, shedding eight named tasks in the process, then shipped a harness that
returned a hardcoded approximation of the number it was asked to compute. Six findings cluster into
*there is no front door a person can type into*.

**Enforced by.** [[R-0125]] every instruction in a brief carries its reason and the document it came
from · [[R-0141]] every review brief names its binding source · [[R-0100]] every ask gets a spec and
a plan before it is built · [[R-0140]] no factory work starts unless a product Feature is blocked on
it.

**TRIGGERS: checked.** The interrogator is `asf idea` ([[F-0023]]); the harvest holds a spec or plan
branch that drops an anchor of the document already on the trunk, and `asf refine-check` is the
rule's check.

---

## 2 · Create value, not motion

If you can write the invocation down, it is not a model call — it is a line of code. And **looking is
charged too**: every file a session opens is re-sent on every turn after it.

**Evidence.** The prototype measured that **93 % of a run's cost was the agent discovering what the
runner already knew**. One build phase spent 12,318,685 input tokens over 96 tool calls in 18
minutes. Twelve findings cluster into *cost is unbounded, and most of it is discovery*; two of them
alone — rebuilding work already on the main branch, and fourth-and-later attempts — account for
**28.3 % of everything that factory ever spent**.

**Enforced by.** [[R-0060]] attach what an agent could fail by not finding · [[R-0061]] cite the plan
in a brief, never paste its task bodies · [[R-0085]] cap the output: read the last forty lines,
never paste a gate · [[R-0094]] return at most fifteen lines, the detail goes in the report file ·
[[R-0069]] keep an expensive model off unless it measurably saves tokens elsewhere · [[R-0135]]
write every recurring step as code, never as a line to remember.

**TRIGGERS: recited only.** It becomes `checked` when the generated preamble lands ([[F-0022]]) and
input tokens per session sits on the scorecard beside cost ([[F-0028]]).

---

## 3 · Secure by design

Least privilege by default. Secrets are never printed, never committed, never in a transcript. **A
capability list is not a boundary**: what a tool is allowed to do is not the same as what it is
prevented from doing.

**Evidence.** The prototype's own coverage table is the argument: its writes boundary *detected and
reverted* rather than preventing; its secret protection matched **path patterns only, with nothing
reading content**; and all three of its runtime adapters declared, in code, that they did not
enforce access at all. Eight findings cluster into *the writes boundary leaks, or refuses the wrong
thing*. On our own side: a scanner reached a test database through a publicly reachable port and
dropped it.

**Enforced by.** [[R-0092]] never source the local environment file directly · [[R-0122]] the
controller does secrets and production by hand, through pipes that never print a value · [[R-0066]]
never give a subagent a browser tool · [[R-0065]] every writing agent gets its own worktree ·
[[R-0088]] never let two live sessions write the same file · [[R-0091]] stop a background job by its
process id, never by a pattern.

**TRIGGERS: hooked, and checked in part.** The deny rules and the redaction hook are hooks
([[F-0062]]), the rule-enforcing hooks are [[F-0061]], the credential expiry is a check ([[F-0042]]),
and secret and dependency scanning are checks ([[F-0069]]). Until those land, the rules above are
recited.

---

## 4 · Automate or die

Deterministic first: if a step can be a script, it is a script. **Parallel by default**: independent
work starts together, and anything that serialises it must say why.

**Evidence.** The prototype's frontier computed which steps were parallel and its runner then ran
them one after another — *nothing checked that a plan's independent steps ran together*. Four
findings cluster into *concurrency and claims*: two operators invisible to each other, a frontier
sizing the wrong thing, sixty-six items with zero declared footprints, so the whole line was serial
by accident.

**Enforced by.** [[R-0004]] dispatch every ready row a worker has room for · [[R-0006]] turn every
spawning row into a launch in the same cycle · [[R-0007]] run the capacity pass on a fixed schedule
outside the session · [[R-0057]] at most four local agents at once · [[R-0058]] route a launch to
the account with the most room · [[R-0112]] run checks and diagnoses as background jobs · [[R-0135]]
write every recurring step as code · [[R-0109]] every status command prints script-generated tables.

**TRIGGERS: checked in part.** `check` already fails two Active Tasks whose footprints intersect, so
the parallelism claim is verifiable. The dispatch half is recited until the tick ships ([[F-0034]]).

---

## 5 · Never send a human to do an agent's job

Route a decision by what it is about, not by who is nearest. **Never ask without proposing.** A step
that genuinely needs a person **parks** the work and lets the rest of the line continue; it does not
block it.

**Evidence.** The prototype's own table records *never ask without proposing* as having **no
mechanism at all**. Ten findings cluster into *the operator cannot see the line*, and one of them is
blunt about the cause: every surface was built for a developer reading a trace, while the questions
the person paying actually has are *which item, what is startable, what waits on me*.

**Enforced by.** [[R-0012]] act without asking when the change cuts errors, time or fixes ·
[[R-0014]] surface every needs-operator line in the same cycle · [[R-0015]] feed the build column
instead of waiting · [[R-0016]] decide the open items as recorded decisions while the operator is
away · [[R-0110]] ask one question at a time · [[R-0115]] answer a question with facts and a
recommendation · [[R-0113]] delegate long work and keep the controller responsive.

**TRIGGERS: recited only.** It becomes `checked` when the approval matrix is code ([[F-0031]]): an
action class maps to `auto`, `groom` or `human-now`, hooks refuse on it and the tick raises the line.

---

## 6 · Data-driven, always

Cite the source of every claim. **An absence is reported as an absence** — "I found nothing" is a
result, and "nothing was checked" is a different result that must never be printed as the first one.

**Evidence.** Seven findings cluster into *the record cannot answer who, what, or which version*.
The sharpest case: a gate that passes when it was given nothing to check is worse than no gate,
because it reports verification that did not occur. And a search that did not run is not evidence
that nothing was found.

**Enforced by.** [[R-0081]] attach the command and its output to every done claim · [[R-0107]] prove
done by tests running green, never by a report · [[R-0109]] every status command prints
script-generated tables · [[R-0111]] paste the orchestrator's tables verbatim · [[R-0128]] count the
CI jobs that actually ran before calling a run good · [[R-0133]] give every verification a control
that must fire · [[R-0130]] match on the field that declares the relation, never on a filename.

**TRIGGERS: checked.** The record itself is the mechanism: state is derived from evidence, `check`
fails a stale index, and every generated table carries a stamp line. Typed intent, derived state —
*a status a human maintains is a status that lies.*

---

## 7 · Don't trust, validate

**A report is a claim, not a result.** A phase defaults to failed and is proven to have succeeded. And
**a near miss is corrected in place, never restarted** — the session that produced it holds the
context.

**Evidence.** This is the one directive the prototype fully mechanised: six mechanisms, no gaps in
its own coverage table. What happens without it is the six findings clustered into *a verdict decides
nothing, and a panel cannot be judged*. On our own side, three incident Bugs in this record are all
one shape — a step trusted rather than verified: a reap before its fast-forward succeeded
([[B-0009]]), a reap of a worktree whose job was still alive ([[B-0010]]), and a staging command
that committed a tree it did not own ([[B-0008]]).

**Enforced by.** [[R-0019]] never merge without a gate on that exact sha · [[R-0027]] every pull
request gets its own green run before it joins a batch · [[R-0031]] preflight every approved pull
request against current main · [[R-0038]] let a merge finish only when the sha carries no failed
check · [[R-0067]] resume the session that holds the context, never re-dispatch it · [[R-0082]] run
the mechanical self-check before asking for a review · [[R-0083]] never push with the verification
skipped · [[R-0087]] run the local control before dispatching a fixer · [[R-0095]] round one lists
every critical · [[R-0106]] a review file is a findings table, not a restated diff · [[R-0126]]
reproduce a CI-only red at CI's scope before calling it unreproducible.

**TRIGGERS: checked, and hooked at the record.** The pre-commit hook runs `check` before anything
lands here. Reviews become machine-readable checks in [[F-0053]]; the in-place correction is
[[F-0039]]; the typed report beside the prose is [[F-0025]].

---

## 8 · Record all, improve all

Files are the record; the generated index is its queryable mirror. Memory is **read-then-write** —
both halves, or neither.

**Evidence.** The prototype shipped the read half of its memory interface into every prompt and
**never called the write half from anywhere**. The half that makes a system compound was the half
never wired. Two findings cluster into *the factory learns nothing from itself*: nothing read two
runs together, and it filed one item in its whole life. Our own ruling is the same one from the
other side — anything the factory relies on an agent remembering is a defect ([[D-0025]]).

**Enforced by.** [[R-0124]] a memory file older than a fortnight must point at a card, unless it is
style · [[R-0135]] write every recurring factory step as code, never as a line to remember ·
[[R-0108]] record as a decision every decision the person did not make · [[R-0080]] one session, one
topic — side defects go on the list · [[R-0121]] write ledgers and reports in the main checkout.

**TRIGGERS: checked.** `check` and the index keep the record honest; the bug filer turns repeated
errors into cards. The memory-pointer rule becomes `checked` when its check is ported — it is
`enforced: false` today, like all 142 migrated rules.

---

## 9 · Version everything, lose nothing

Commit each coherent unit. **Unpushed is not saved.** Verify against the base you will land on, not
the base you forked from. A claim about a version is a claim about a specific version.

**Evidence.** The prototype's own table records *base drift on merge* and *version claims are
global* as having **no mechanism**, and three findings are that gap arriving. Our own worst near-miss
is the same directive: twenty-six rewritten Stories survived only because unreachable objects could
be recovered after a worktree was reaped ahead of its fast-forward ([[B-0009]]).

**Enforced by.** [[R-0032]] base every branch and pull request on the main branch · [[R-0079]] push
every commit before the turn ends · [[R-0077]] push within thirty minutes of launch and every thirty
minutes after · [[R-0031]] preflight every approved pull request against current main · [[R-0039]]
name an already-reviewed branch that a merge commit carries in · [[R-0089]] never stash to compare
against the base · [[R-0139]] check a branch tree, not the working tree, before pushing it ·
[[R-0047]] reserve every migration and decision number through a reservation tool · [[R-0134]] name
only public packages in a changeset.

**TRIGGERS: hooked at the record, recited elsewhere.** The pre-commit hook is the one mechanism that
exists today. The base-drift half becomes `checked` with the harvest step ([[F-0034]]) and the
rebase row ([[F-0036]]).

---

## 10 · Don't reinvent the wheel

Question zero: is this the standard library's job? Then three probes, in order: **here**, **anywhere**,
and **in what you already have**. A document its author addressed to you outranks their source code.

**Evidence.** The prototype recorded its three probes as having **no mechanism — judgement**, and its
zero-dependency posture as *an assertion, not an intention*. Six findings cluster into *documentation
drifts and the repository will not build from a clean clone*. The cheapest instance of the directive
is also the most common miss: checking a configuration key against the installed version rather than
against what a model remembers about it.

**Enforced by.** [[R-0136]] check a config key against the installed version before trusting it ·
[[R-0086]] check every anchor the brief quotes before writing code · [[R-0060]] attach what an agent
could fail by not finding · [[R-0098]] build the tooling repository locally, with a unit-test gate ·
[[R-0141]] name the binding source in every review brief.

**TRIGGERS: recited only.** It becomes `checked` when the quickstart is executed by CI from a clean
clone ([[F-0033]]) and the documentation surfaces are one-place-per-fact by rule ([[D-0014]]).

---

## The tally, honestly

| # | Directive | TRIGGERS today |
| --- | --- | --- |
| 1 | Solve what matters | checked |
| 2 | Create value, not motion | recited only |
| 3 | Secure by design | hooked (partly), checked (partly) |
| 4 | Automate or die | checked (partly) |
| 5 | Never send a human to do an agent's job | recited only |
| 6 | Data-driven, always | checked |
| 7 | Don't trust, validate | checked, hooked at the record |
| 8 | Record all, improve all | checked |
| 9 | Version everything, lose nothing | hooked at the record |
| 10 | Don't reinvent the wheel | recited only |

Three of ten are decoration today. Every one of them names the card that would change that, and each
of those cards is in this record with a rank. A constitution whose coverage table is generated by
`asf rules check` cannot quietly rot — that is [[F-0055]]'s job, and until it exists this table is
maintained by hand and is therefore itself an instance of directive 6's warning.
