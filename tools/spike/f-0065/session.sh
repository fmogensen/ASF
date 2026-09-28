#!/usr/bin/env bash
# tools/spike/f-0065/session.sh <role> <variant> — F-0065 Task 3 (§2.2, PD5, PD6, PD7). Launches
# **one real session** in the environment a worker actually gets and prints one thing on stdout:
# the path of its stream-json log, `<state>/spike/f-0065/<role>.<variant>.jsonl`. A re-run of a
# leg overwrites its own log, brief and rendered settings file, and no other leg's.
#
# The environment is the real one (PD5): the python3 heredoc below builds a `runtime.Job` and
# calls `asf.workers.runtime.build_env`/`build_command` — imported, never reimplemented — so the
# session gets the same HOME, ASF_HOME, CLAUDE_CONFIG_DIR, hooksPath and git credential helper a
# production worker session gets. Three declared departures and no others: cwd is the fixture
# worktree, `--settings` is the rendered leg file (PD4), and the brief is the spike's own (the
# role block plus a tail that runs probe.sh).
#
# This is a shell script only in name (§2.2): the whole of the launch logic is one python3
# heredoc, so the file count under tools/spike/f-0065/ stays four.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO="$(git -C "$HERE" rev-parse --show-toplevel 2>/dev/null || true)"

role="${1:-}"
variant="${2:-}"
if [ -z "$role" ] || [ -z "$variant" ]; then
  echo "usage: session.sh <coder|reviewer|prober> <variant>" >&2
  echo "  variant: off, bare, grant-N, mode-bypass, mode-default, grant-net, grant-net-passthrough" >&2
  exit 2
fi
if [ -z "$REPO" ]; then
  echo "session.sh: could not find this checkout's own toplevel from $HERE" >&2
  exit 2
fi

exec python3 - "$role" "$variant" "$REPO" "$HERE" <<'PY'
import json
import os
import re
import sys
import tempfile

role, variant, repo, here = sys.argv[1:5]

ROLES = ('coder', 'reviewer', 'prober')
if role not in ROLES:
    print(f"session.sh: unknown role {role!r} — want one of {', '.join(ROLES)}", file=sys.stderr)
    sys.exit(2)


def refuse(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


TEMPLATE = os.path.join(here, 'settings', f'{role}.{variant}.json')
if not os.path.isfile(TEMPLATE):
    refuse(f'session.sh: no settings template at {TEMPLATE}', 2)

sys.path.insert(0, repo)
sys.path.insert(0, here)

from asf import env as asf_env
from asf import hooks
from asf.roles import roles as roles_mod
from asf.workers import githooks, pool, runtime
import collect  # tools/spike/f-0065/collect.py — the surface reading, never re-guessed here

# Not a real product: the identifier this spike's state dir, git hooks and log path are keyed
# on, in the same shape a real one would be (`<state>/<product>/...`, §2.5's `${STATE}` slot).
PRODUCT = 'spike/f-0065'
STATE = asf_env.state_dir(PRODUCT)
FIXTURE_REPO = os.path.join(STATE, 'fixture', 'repo')
if not os.path.isdir(os.path.join(FIXTURE_REPO, '.git')):
    refuse(f'session.sh: no fixture worktree at {FIXTURE_REPO} (run fixture.sh first)', 2)

JOB_NAME = f'{role}.{variant}'
LOG_PATH = os.path.join(STATE, f'{JOB_NAME}.jsonl')
if os.path.exists(LOG_PATH):
    os.remove(LOG_PATH)  # a re-run of a leg overwrites its own file and no other (§4)

# ---- the account: the local lane only, never the cloud lane this Feature does not measure -----
cfg = asf_env.load_config()
accounts = [a for a in pool.accounts_from_config(cfg) if pool.in_lane(a, 'local')]
if not accounts:
    refuse('NEEDS OPERATOR: no local worker_pool.accounts configured — add one to config.yaml '
           f'and re-run: bash tools/spike/f-0065/session.sh {role} {variant}')
acct = accounts[0]

# ---- the credential refusal, verbatim from §4: no money spent on an unauthenticated session ---
if not any(v in acct.auth_env for v in runtime.RUNTIME_AUTH_VARS):
    var = runtime.RUNTIME_AUTH_VARS[0]
    refuse('NEEDS OPERATOR: no runtime credential in this environment — set the account\'s '
           f'auth_env ({var}) and re-run: bash tools/spike/f-0065/session.sh {role} {variant}')

# ---- PD6: the account's own user settings file must carry no sandbox key already --------------
surface_keys = collect.surface()
sandbox_top_keys = {k.split('.', 1)[0] for k in surface_keys} - {'permissions'}
acct_settings_path = hooks.account_settings_path(acct)
if os.path.isfile(acct_settings_path):
    with open(acct_settings_path, encoding='utf-8') as f:
        acct_settings = json.load(f)
    carried = sorted(sandbox_top_keys & set(acct_settings))
    if carried:
        refuse(f'session.sh: {acct_settings_path} already carries {carried} — off and bare '
               'would not differ (PD6); clear it before measuring this leg')

# ---- the brief: the role block plus the tail that runs probe.sh and reports it (D10) -----------
role_obj = roles_mod.load(role)
probe_path = os.path.join(here, 'probe.sh')
brief_text = roles_mod.block(role_obj, roles_mod.MAX_LINES) + (
    '\n\n---\n\n'
    f'Run `bash {probe_path} {role}` now. Paste every `PROBE ...` line it prints, verbatim and '
    'in full, as your entire report. Do nothing else in this session.\n'
)
briefs_dir = os.path.join(STATE, 'briefs')
os.makedirs(briefs_dir, exist_ok=True)
brief_path = os.path.join(briefs_dir, f'{JOB_NAME}.md')
with open(brief_path, 'w', encoding='utf-8') as f:
    f.write(brief_text)

# ---- the job and the real environment (PD5): built through the package, never by hand ----------
hooks_dir = githooks.ensure(PRODUCT)
permission_mode = 'default' if variant == 'mode-default' else runtime.DEFAULT_PERMISSION_MODE
job = runtime.Job(PRODUCT, JOB_NAME, FIXTURE_REPO, brief_path, None, account=acct,
                  permission_mode=permission_mode, log_path=LOG_PATH, hooks_dir=hooks_dir,
                  passthrough=asf_env.env_passthrough(cfg),
                  product_auth_env=asf_env.product_auth_env(PRODUCT))
try:
    built_env = runtime.build_env(job)
except runtime.AuthEnvError as e:
    refuse(str(e))

session_home = runtime.session_home(acct) or built_env.get('HOME') or os.path.expanduser('~')
tmpdir = built_env.get('TMPDIR') or tempfile.gettempdir()

# ---- PD4: the settings file is rendered, never passed through as a tracked path ----------------
with open(TEMPLATE, encoding='utf-8') as f:
    rendered_text = f.read()
for placeholder, value in (('${REPO}', repo), ('${STATE}', STATE), ('${HOME}', session_home),
                          ('${TMPDIR}', tmpdir)):
    rendered_text = rendered_text.replace(placeholder, value)
rendered_text = collect.strip_jsonc_comments(rendered_text)

# ---- PD7: refused here, before any money is spent, rather than silently ignored under -p -------
try:
    rendered = json.loads(rendered_text)
except json.JSONDecodeError as e:
    refuse(f'session.sh: {TEMPLATE} does not render to valid JSON: {e}')

leftover = sorted(set(re.findall(r'\$\{[^}]*\}', rendered_text)))
if leftover:
    refuse(f'session.sh: {TEMPLATE} still carries {leftover} after rendering — refusing the '
           'launch rather than sending a leg the harness would silently ignore (PD7)')

allowed_top = sorted({k.split('.', 1)[0] for k in surface_keys})
bad_top = sorted(set(rendered) - set(allowed_top))
if bad_top:
    refuse(f'session.sh: {TEMPLATE} has top-level key(s) {bad_top} outside the surface\'s own '
           f'list {allowed_top} (PD7)')

settings_dir = os.path.join(STATE, 'settings')
os.makedirs(settings_dir, exist_ok=True)
rendered_path = os.path.join(settings_dir, f'{JOB_NAME}.json')
with open(rendered_path, 'w', encoding='utf-8') as f:
    json.dump(rendered, f, indent=2, sort_keys=True)
    f.write('\n')
job.settings_file = rendered_path

# ---- the launch: one real session, foreground, waited on ---------------------------------------
print(f'session.sh: launching {JOB_NAME} …', file=sys.stderr)
result = runtime.ClaudeCodeRuntime().run(job, wait=True)
print(job.log_path or LOG_PATH)
detail = f' ({result.reason})' if result.reason else ''
print(f'session.sh: {JOB_NAME} session result: {"ok" if result.ok else "not ok"}{detail}',
      file=sys.stderr)
PY
