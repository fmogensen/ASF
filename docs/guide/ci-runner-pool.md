# The CI runner pool

A product whose CI runs on self-hosted runners declares those runners once, in `ci.pool:` of its
product file. From that list ASF reports drift (`asf doctor`), plans and applies the runners'
labels (`asf ci reconcile`), puts a newly enabled runner on trial for one job, and derives the
product's CI ceiling (`asf capacity`). Nobody edits a runner's labels by hand on the CI host.

The first backend is GitHub Actions (runners and workflow files, through `gh api`); the planned
`ci.provider: vm` reads the same pool (see [the end of this page](#and-ciprovider-vm)).

## Routing labels name a capability, never a provider

A job's `runs-on` asks for **what it needs**: `heavy`, `light` (later `gpu`, `docker`, …). It
never asks for **who sells the machine** or **how big it is** — provider and size are inventory,
kept in `ci.pool`, and nowhere else.

Why: a `runs-on: [self-hosted, <provider>, heavy]` can only run on that provider's boxes. Add
heavy boxes from a second provider and they sit idle, however deep the queue: the jobs ask for a
label those boxes do not carry. That is exactly what happened on one fleet — several 8-core boxes
idle for days while 170 jobs queued, because every job named the first provider, and the fix
was hand edits that even put the first provider's name on the second provider's boxes so the jobs
would take them. Nothing had reported it.

So the rule, which the doctor enforces and the reconcile writes:

- a runner carries **its host defaults** (`self-hosted`, OS, architecture), **its role**, and
  optionally **`provider-<name>`** — informational only, for the operator reading the host;
- a `runs-on` asks for `self-hosted` plus a role; a label that is not a declared role, or any
  `provider-` label, in a `runs-on` is a drift row.

The decision is recorded as [ADR 0002](../decisions/0002-ci-routing-labels-are-capabilities.md).

## Declaring a pool

```yaml
ci:
  provider: github-actions
  runner_org: acme            # an organisation's runners; without it, the repo's own
  pool:
    - {runner: ci-1,   box: box-1, provider: alpha, size: 8c-16g, role: heavy, slots: 1}
    - {runner: ci-1b,  box: box-1, provider: alpha, size: 8c-16g, role: light}
    - {runner: ci-2,   box: box-2, provider: alpha, size: 16c-32g, role: heavy}
    - {runner: ci-h1,  box: box-7, provider: beta,  size: 8c-24g, role: heavy}
    - {runner: ci-l1,  box: box-8, provider: beta,  size: 6c-12g, role: light}
```

| key | required | what it is |
|---|---|---|
| `runner` | yes | the runner's name on the CI host |
| `box` | no | the machine it runs on (two runners may share one box) |
| `provider` | yes | who hosts the box; becomes the `provider-<provider>` label |
| `size` | no | inventory only, free text |
| `role` | yes | the one routing label: a capability, never the provider, an OS or a `provider-` label |
| `slots` | no | jobs it takes at once (default 1) |

Every product load validates it: unknown keys, a missing required key, a non-capability role, a
`slots` below 1 and a runner declared twice all refuse the load, with the line.

**Where it lives.** The pool is in the product file, not in the operator's `config.yaml`. The
drift it is checked for is between the runners and *this product's* workflow files, and the CI
ceiling it sets is this product's. Two products that share one organisation's runners each
declare the runners they route to; the doctor of each names a runner the other declares as
`undeclared`, which is informational.

## The doctor's `ci pool` rows

`asf doctor --product <p>` reads the host (read-only) when the product declares a pool — and makes
no host call when it does not. One row per finding, or one `ok` row naming the slots per role:

| row | what it means | fix |
|---|---|---|
| `stranded: <runner> is online but no job's runs-on matches its labels` | capacity nobody can use | `asf ci reconcile --apply` gives it its role; if the jobs still name a provider, migrate them (below) |
| `unsatisfiable: <workflow>:<job> [labels] — no online runner carries all of it` | those jobs queue forever | ask for a role in `runs-on`, or bring the runner online |
| `role: <runner> lacks its role '<role>'` / `carries another role` | host labels differ from the declaration | `asf ci reconcile --apply` |
| `provider-like label in runs-on: <workflow> asks for '<label>'` | a job routes by provider (or by a label that is no declared role) | change the `runs-on` to a role |
| `missing: <runner> … is declared but not registered` | the box lost its runner, or the name is wrong | re-register the runner, or fix `ci.pool` |
| `offline: <runner> … — n <role> slot(s) lost` | the runner service is down | restart it on the box |
| `undeclared: <runner> is registered but not in ci.pool` | a runner ASF does not manage | declare it, or remove it from the host |

Jobs on hosted runners (no `self-hosted` in their `runs-on`) and a `runs-on` that is an
expression ASF cannot resolve (`${{ matrix.os }}`) are not judged. `${{ vars.X || 'heavy' }}` is
read as its default, `heavy`. A host that cannot be read is one `skip` row with the reason.

## `asf ci reconcile`

```
asf ci reconcile --product <p>            # dry run: the plan, nothing written
asf ci reconcile --product <p> --apply    # write it
```

The plan is one row per runner: `runner | current labels | target labels | action`. The target
is the host's own labels plus the role plus `provider-<provider>`; anything else is removed.

```
runner  current labels                          target labels                                   action
ci-1    linux, self-hosted, x64, alpha, heavy   linux, self-hosted, x64, heavy, provider-alpha   add provider-alpha; keep alpha — blocked until workflows migrate
ci-h1   linux, self-hosted, x64, beta-heavy     linux, self-hosted, x64, heavy, provider-beta    add heavy, provider-beta; remove beta-heavy; trial: one heavy job
```

**Capacity is never lost on the way.**

- Every runner's adds are written before any runner's removes.
- A label a current `runs-on` still needs on that runner is not removed: the row says
  `keep <label> — blocked until workflows migrate`, and the next reconcile after the migration
  removes it.
- A runner that is not in `ci.pool` is left untouched.

**A trial of one job.** A runner that gains its role label (newly enabled for a role) is put on
trial: the reconcile records it in `~/.ASF/state/<p>/ci-trials.json`. The tick's `health` step,
and every later reconcile, look at the first job that runner ran since:

- no job yet: it waits;
- a job running: the role label is withdrawn until it ends, so no second job lands before the
  verdict;
- the job passed: the role label stays (it is put back if it was withdrawn), and the trial ends;
- the job failed (`failure`, `timed_out`, `startup_failure`): the runner's labels go back to what
  they were before the reconcile, and the next tick files a Bug
  `ci trial failed: <runner> as <role>` (S2, the job linked). The next `--apply` starts a fresh
  trial.

A cancelled or skipped job does not count; the trial waits for the next one. Every verdict is
appended to `~/.ASF/state/<p>/ci-trials.jsonl`. A dry run prints the verdicts but changes nothing.

`--apply` changes labels on shared runners: it is an operator action, never run by the tick.

## The pool sets `asf capacity`

With a pool declared, the product's CI ceiling is the sum of every runner's `slots`, and
`asf capacity` names it in `ci from`: `ci.pool: heavy 12, light 7`. An explicit `capacity.ci`
overrides it and says so: `product (overrides ci.pool 19)` — useful while the ceiling counts
workflow *runs* (each of which fans out into many jobs) rather than jobs. The order is:
`capacity.ci`, then the pool, then the operator's `capacity.per_product.ci`, then capped by
`capacity.total.ci`.

## The CI start queue

A ceiling on runs in flight does not know that one run fans out into three heavy jobs and another
into one light one. So with a pool declared, every CI run ASF itself starts waits in one queue per
product until the runners it will need are free:

| start | held how |
| --- | --- |
| the lane opening a PR (its `pull_request` run) | the PR is not opened; the branch stays `PUSHED` |
| the lane merging a PR, or fast-forwarding the trunk (the trunk's `push` run) | the green branch waits (`WAITING`, reason `ci queue`) |
| the tick's `batch` step | the step waits |
| a deploy dispatch | not dispatched; the next tick asks again |

A push the CI host turns into a run on its own cannot be delayed once made, so the push (or the
PR, or the merge) is what is held.

**When a run starts.** A batch start needs runs in flight below the CI ceiling (`capacity.ci`,
above): the ceiling is the batch step's gate and holds no other start — with the ceiling's worth of
runs always in flight, a PR held by it would never open. Runs in flight are one count, read by the
queue and the status row alike: the runs of `ci.workflow` not completed — queued or running, PR,
trunk and batch alike. A batch held at the ceiling sets no runners aside for the starts behind it.
A batch or an ordinary PR start needs its runners (an S1 or hotfix start, a trunk run and a deploy
reserve nothing, so PR runs in flight never hold the trunk every deploy waits on): per runner class,
the free runners (online, not busy, at their `slots`, from the runners API) must cover the run's
**expected jobs** — over the last `history` completed runs of the workflow that start triggers,
per run and class the peak number of jobs running at once (a job counts only if it got a runner and
was not skipped, over its started..completed span, so skipped or conditional jobs never count and
sequential stages never add up), grouped by the class of the runner that actually ran it (the
`class`, else the `role`) — never by the job's `runs-on`. A runner outside the pool is classed by
its labels: a label every carrier of which sits in one class names that class, so a sub-label
carried only by `heavy` runners counts in `heavy`, once, and never adds a demand of its own; a job
listed twice counts once. The median of that over the runs, capped at what the pool has of that
class. Everything
ahead in line has its expected jobs set aside first, so
a heavy run at the head is not starved by lighter ones behind it; a run only needing a class with
room still goes. A run admitted in the last three minutes still holds its runners, since its jobs
queue on the host before any runner shows busy. **Starvation guard:** an ordinary PR start that
has waited longer than `ci.queue.pr_wait_min` minutes (default 45) starts once half its expected
jobs per class (rounded up) are free, with one line:
`ci queue: F-0112 starts — starvation guard — waited 1h05m (> 45m), heavy 4 free, needs 8; half is free`.

**Order.** S1 and hotfix items first, then trunk runs (every deploy waits on a green trunk), then
PRs of customer-facing Features (the Feature says `customer_facing: true`, or the branch touches
`customer_paths`), then everything else; oldest first within each. An entry nobody asked about for
30 minutes leaves the line.

**Every hold is one line:**

```
ci queue: T-0341 waits — heavy 0 free, needs 3 (S2, 4th in line)
ci queue: batch waits — at the ci ceiling (4 runs in flight; batch starts below 4) (other, 2nd in line)
```

and the status Capacity row ends with the depth and the head: `ci queue 3, head T-0341 waits — …`;
a head held at the ceiling is re-stated there with the row's own in-flight count.

**Superseded trunk runs.** A trunk run judges every commit below it, so an older trunk `push` run
still queued behind a newer one is moot. Each tick the lane cancels those, per trunk workflow,
keeping the newest queued-or-running run; a run already in progress finishes. One line per cancel:
`ci queue: cancelled superseded main run 2 (checks.yml at bbbbbbbbb) — run 4 at ddddddddd judges it`.

**Trunk starvation relief.** The host's own queue is first in, first out, and the start queue
cannot reorder runs the host already holds: a trunk run pushed after PR runs waits behind all of
them. Each tick, when the newest trunk `push` run has been queued longer than
`ci.queue.trunk_wait_min` minutes (default 20; the wait is UTC now minus the host's UTC
`createdAt`), the lane cancels runs queued *ahead* of it and not yet started — PR runs of ordinary
items first, then of customer-facing Features, then batch runs, newest first within each — until
the free runners plus what the cancelled runs would have taken cover the trunk run's expected jobs
in every class. A run in progress, an S1 or a hotfix run is never cancelled. Each cancel is kept in
the queue file and re-run (`gh run rerun`) through the queue at its original priority once the
trunk run has started. One line each:
`ci queue: cancelled queued pr run 101 (B-0008, S2) — main run 900 at fffffffff has waited 25m for runners`,
`ci queue: re-ran pr run 101 (B-0008, S2) — main run 900 at fffffffff started after waiting 24m`.

```yaml
ci:
  queue:
    mode: on        # on (the default with a ci.pool) | dry-run | off
    history: 10     # completed runs of each workflow measured
    trunk_wait_min: 20   # minutes a queued trunk run waits before runs ahead of it are cancelled
    pr_wait_min: 45      # minutes an ordinary PR start waits before it starts on half its jobs
    workflows: {pr: checks.yml, trunk: checks.yml, batch: batch.yml}   # default: ci.workflow
```

`mode: dry-run` decides every start and prints each hold as `ci queue (dry-run): … would wait`
(and each cancel as `would cancel`) but starts everything and writes nothing — the way to watch it
before trusting it. The mode is read from the product file at each ask, so a switch to `dry-run` or
`off` holds nothing from the next start on, even inside a tick that loaded the product earlier.
`asf ci queue --product <p>` prints the line with each entry's expected jobs
and what would start now, writing nothing. A product with no `ci.pool`, or `mode: off`, is not
queued: every start goes at once as before, and the queue makes no `gh` call.

## Moving an existing product over

A product whose jobs route by provider today moves in three steps, each safe on its own:

1. **Declare the pool and give every runner its role.** Write `ci.pool`, run
   `asf ci reconcile --product <p>` and read the plan, then `--apply`. Runners gain their role and
   `provider-` label; the old provider labels stay, `blocked until workflows migrate`, because
   the jobs still ask for them. A runner that was stranded gains its role and goes on trial.
2. **Change every `runs-on` to a role.** `[self-hosted, <provider>, heavy]` becomes
   `[self-hosted, heavy]`; a job with no role (`[self-hosted, <provider>]`) gets one. Land it like
   any other change. The doctor's `provider-like label` rows go away.
3. **Remove the old labels.** Run the reconcile again: the provider labels are no longer needed
   by any `runs-on`, so the plan now removes them. `--apply`. The doctor's `ci pool` row is `ok`.

## And `ci.provider: vm`

The planned external-CI provider runs jobs on connected machines with no CI host in between.
It reads the same `ci.pool`: a runner's `role` is what a job asks for, `slots` is how many jobs
the machine takes, and provider and size stay inventory. The drift checks and the reconcile sit
behind a small backend interface (`asf.ci_pool.Backend`: list runners, read every job's
`runs-on`, add and remove a label, list a runner's jobs); GitHub Actions is the first backend, and
`vm` becomes the second. Until then a product with `ci.provider: vm` gets one `skip` row saying
there is no backend for it.
