# Release notes

## v0.1.0-preview

The first pre-release of ASF, for feedback. It is cut only when `asf release-readiness --gate
preview` is green: five criteria, each read from CI and the trunk, never from an opinion.

| # | The preview gate | Where it is proved |
|---|---|---|
| 1 | Install from zero on a clean machine to a green doctor | CI: a Linux container and a macOS runner with a fresh `HOME` (`.github/workflows/install.yml`) |
| 2 | A minimal product (one account, no cloud, no merge queue, hosted CI) ticks end to end | CI: `tests/test_minimal_product.py` |
| 3 | A first user gets from the README alone to a landed Task | CI: `tests/test_readme_first_user.py` runs exactly the README's Quick start, on a stub runtime |
| 4 | No product name, operator path, e-mail or private link in the repository | CI: `tools/check_generic.sh` and `tools/check_privacy.sh` |
| 5 | Main CI green, a version tag, these notes, the known issues, a feedback channel | the trunk itself |

The hardening criteria (stability over a window, repair load, upgrade safety, a clean factory
floor, seat use, self-tuning) are the **1.0** gate. What of it is still open is listed in
[KNOWN-ISSUES.md](KNOWN-ISSUES.md).

### What is in it

- **The record** — Epics, Features, Stories, Tasks and Bugs as Markdown cards with typed intent;
  the state of each card is derived from the trunk, never typed by hand.
- **The tick** — one deterministic pass (record, health, groom, wave, PRs, harvest) that decides
  what runs next; agent sessions only write and judge.
- **The lanes** — spec, plan, code, review and landing, each with its own gate; a red gate goes
  back to its session with the reason.
- **The views** — `asf next`, `asf status`, `asf roadmap`, `asf backlog`, `asf scorecard`,
  `asf doctor`, `asf release-readiness`, and the same tables as Claude Code skills.
- **The installer** — `tools/install.sh` pins a release with pipx and hands off to `asf install`:
  config, record, account, hooks, clocks (launchd on macOS, cron on Linux), plugin and doctor.

### Install

```bash
curl -fsSL https://raw.githubusercontent.com/fmogensen/ASF/main/tools/install.sh | bash -s -- <product> v0.1.0-preview -- --repo <dir> --record <dir>
```

Or try it first without an account: `bash tools/quickstart.sh` from a clone (the README's Quick
start).

### Feedback

Open an issue in this repository's GitHub Issues: a **bug report**, an **install problem** or a
**feature request**. Check [KNOWN-ISSUES.md](KNOWN-ISSUES.md) first.

### Licence

Apache-2.0 — see `LICENSE` and `NOTICE`.
