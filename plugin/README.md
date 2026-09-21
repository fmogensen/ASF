# ASF plugin for Claude Code

The factory's tables in the console: `/asf:roadmap`, `/asf:backlog`, `/asf:parity`, `/asf:prod`,
`/asf:sessions`, `/asf:status`, `/asf:next`. Each skill runs the matching `asf` view and prints it
verbatim.

## Install

```
/plugin marketplace add <owner>/ASF
/plugin install asf@ASF
```

Local development:

```
claude --plugin-dir ./plugin
```

## Requirements

- `python3` (stdlib only, nothing to pip-install).
- `asf` on `PATH`, so the skills work from any directory:

  ```
  ln -s <repo>/tools/asf ~/.local/bin/asf
  ```

  `tools/asf` sets `PYTHONPATH` to its own checkout and runs `python3 -m asf.cli`. Without `asf`
  on `PATH` the skills fall back to `python3 -m asf.cli`, which only works with the checkout on
  `PYTHONPATH`.
- A product: `$ASF_PRODUCT`, else `default_product` in `~/.ASF/config.yaml`.

Arguments after the skill name are passed through (`/asf:backlog E-0003`).

## Not yet

| skill | needs | wave |
| --- | --- | --- |
| `health` | the worker views | workers |
| `ci` | the metrics views | metrics |
| `performance` | the metrics views | metrics |
| `experiments` | no asf view yet | unscheduled |
