# Writing a connector

ASF talks to every external service through a **connector**: one small interface per concern,
one in-tree default, and a registry that picks the implementation from `config.yaml`. To run ASF
on another forge, CI, agent runtime or scheduler, you write a connector. You don't fork ASF.

## The kinds

| kind | what it does | in-tree implementations (default first) |
| --- | --- | --- |
| `forge` | pull requests, their checks, close/reopen, the forge's API | `github` (through `gh`), `fake` |
| `ci` | workflow runs, logs, rerun/cancel, the start queue's source, the runner pool | `github-actions` (through `gh`), `fake` |
| `runtime` | where an agent session runs, locally and in the cloud lane | `claude-code`, `fake` |
| `scheduler` | install, pause, resume and read back the clocks | `launchd`, `systemd`, `cron`, `none`, `fake` |
| `quota` | an account's usage, for the quota guard | `none` (every account available), `command`, `fake` |
| `secrets` | turn an `auth_env` reference into its value | `file`, `command` |

The interfaces are `typing.Protocol` classes in `asf/connectors/protocols.py`. That file is the
contract: each method's docstring says what it returns.

## Choosing one

```yaml
# ~/.ASF/config.yaml
connectors:
  forge: github          # a name: in-tree, or a Python entry point (below)
  scheduler: systemd     # Linux: systemd user timers instead of launchd
  ci:                    # the command form: a shell command with a JSON contract
    command: /usr/local/bin/my-ci-bridge
    timeout_s: 120
```

If you leave a kind out, ASF keeps today's behaviour. A few older keys still choose for their
kind: `worker_pool.backend` (runtime), `scheduler.kind` (scheduler) and
`worker_pool.quota_command` (quota command form). `connectors.<kind>` wins over them.

If a configured name resolves to nothing, ASF reports an error. It never falls back to the
default, because the default would act on a service you did not choose. `asf doctor` has a
`connectors` row that lists the active implementation of every kind and where that choice came
from. A kind it cannot resolve shows as a RED row.

## Option 1: a command (no Python)

`connectors.<kind>: {command: "<cmd>"}` turns every operation of `forge`, `ci`, `runtime` or
`scheduler` into one run of `<cmd> <operation>`:

- **stdin**: `{"kind": "ci", "op": "runs", "args": ["owner/repo"], "kwargs": {"limit": 20}}`.
  An object argument (a runtime's job) is sent as its public attributes.
- **stdout**: the answer as JSON, on the last non-empty line.
- **exit status**: 0 means the line is the answer. A non-zero exit, a timeout or output that
  isn't JSON makes the answer **Unknown**: ASF treats it as "could not tell", never as "nothing
  there".

The operation names and their arguments are the Protocol's methods. Answer with Unknown (exit
non-zero) for any operation your service can't do. A `runtime` command answers `run` and
`continue_run` with `{"ok": true|false|null, "pid": …, "result": "…"}`. `null` means the session
is still running.

`quota` and `secrets` each have a one-line contract of their own:

- `quota`: `{command: "my-usage {account}"}`. `{account}` is replaced with the account name, and
  the last stdout line is `{"five_h_pct": n, "seven_d_pct": n}`. This is exactly the contract of
  `worker_pool.quota_command`.
- `secrets`: `{command: "my-vault read {ref}"}`. `{ref}` is replaced with the `auth_env`
  reference, and stdout (stripped) is the value. A failure refuses the launch with NEEDS
  OPERATOR, the same way a missing token file does.

## Option 2: a Python package

Write a class that satisfies the kind's Protocol, plus a factory that takes the operator config
(a dict) and returns an instance:

```python
# my_forge/connector.py
from asf import github                       # Result / unknown: the answer type every call returns


class MyForge:
    name = 'myforge'

    def __init__(self, cfg):
        self.cfg = cfg

    def open_prs(self, slug, limit=300, fields=(), **kw):
        try:
            data = my_client.list_open(slug, limit=limit)
        except MyClientError as e:
            return github.unknown(f'myforge: {e}')     # never an empty list on failure
        return github.Result(True, data, 0, '', '', github.now_iso(), '')

    # … pr, prs, merge_commit, checks, close_pr, reopen_pr, api, auth_status


def factory(cfg):
    return MyForge(cfg)
```

Publish the factory as an entry point in the group `asf.connectors.<kind>`:

```toml
# pyproject.toml of your package
[project.entry-points."asf.connectors.forge"]
myforge = "my_forge.connector:factory"
```

Install the package into the same environment as ASF (`pipx inject asf-factory my-forge`), then
set `connectors.forge: myforge`.

Names resolve in this order: an implementation registered in the process (tests), then an
in-tree one, then an entry point.

## Rules every connector keeps

- **Unknown is not empty.** If a call didn't produce an answer, return a result with `ok` false
  and a `reason`. ASF closes cards, cancels runs and admits jobs on these answers. A failure
  that reads as "none" is how a factory acts on something that isn't true.
- **Don't raise for an operation you lack.** Return Unknown with a reason that says so.
- **Keep secrets out of results and reasons.** A reason is logged and may be shown in a table.
- **Respect `timeout`** when a caller passes one. Ignore transport knobs (`run`, `env`) that
  mean nothing to your service.
- **Only the connector module runs the executable.** In this repository,
  `tools/check_clients.sh` fails when `claude`, `launchctl` or `systemctl` appears outside its
  connector module, or when a new raw `gh` call appears outside `asf/github.py`, which is the
  GitHub connectors' transport.

## Testing one

`forge`, `ci` and `scheduler` each have a `fake` in `asf/connectors/fakes.py`. You give it
`answers={operation: data}`, and it records every call in `calls`. The `runtime` fake replays
scripted results (`worker_pool.fake_script`), and the `quota` fake reads every account as 0/0.
In a test, register your own implementation for the length of the test:

```python
from asf import connectors

connectors.register('forge', 'github', lambda cfg: my_fake)
self.addCleanup(connectors.reset)
```
