# sample — the second product

A product ASF's own tests run end to end (`tests/test_sample_product.py`, and its own step in CI),
so "works for any product" is something the build proves. Its conventions differ from the
defaults on purpose:

| | |
| --- | --- |
| branches | `feature/`, `bugfix/`, `spec/`, `plan/` |
| specs, plans | `specs/`, `plans/` at the repo root |
| reviews | `reviews/` at the repo root |
| review pattern | `reviews/{slug}-r{n}.md` — the round comes after the slug |
| intake | `cards/`, not `inbox/` |
| CI | none (`ci: {provider: none}`) — no PR host, no `gh` |
| deploy | none (`deploy_sha: none`) |
| workers | the `fake` runtime, replaying `fake_script.json` — no agent, no account |

- `repo/` — the product repo: a word counter, with one passing test.
- `backlog/` — its record: ten cards (1 Epic, 2 decided Features, 3 Stories, 2 Tasks with
  `writes:`, 1 S1 Bug with a `## Fix`, 1 Decision) and their `index.json`.
- `product.yaml` — `~/.ASF/products/sample.yaml`; `@REPO@` and `@BACKLOG@` are filled in by the test.
- `config.yaml` — the operator config it runs under (`@SAMPLE@` is this directory's copy): the fake
  worker backend, one launch slot, no scheduler.
