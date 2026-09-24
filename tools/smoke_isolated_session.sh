#!/usr/bin/env bash
# tools/smoke_isolated_session.sh <product> [account] — R21, the live smoke before a release tag.
#
# Launches ONE real worker session for <product> the way a tick launches one — the configured
# runtime binary, the account's config dir, the worker environment (allow-list +
# worker_pool.env_passthrough), the account's ISOLATED HOME seeded from its home_seed, and its
# credentials from auth_env files (never HOME or the keychain) — and checks that it:
#
#   0. has an auth_env file for the runtime's login (CLAUDE_CODE_OAUTH_TOKEN) and, for the push,
#      GH_TOKEN — created once per account:
#        mkdir -p ~/.ASF/secrets && chmod 700 ~/.ASF/secrets
#        claude setup-token        # authorize as that account; save what it prints:
#        pbpaste > ~/.ASF/secrets/<account>.token && chmod 600 ~/.ASF/secrets/<account>.token
#        # a fine-grained GitHub token, the product repo only, Contents + Pull requests read/write:
#        pbpaste > ~/.ASF/secrets/<account>.gh && chmod 600 ~/.ASF/secrets/<account>.gh
#      and in config.yaml, under the account:
#        auth_env: {CLAUDE_CODE_OAUTH_TOKEN: ~/.ASF/secrets/<account>.token,
#                   GH_TOKEN: ~/.ASF/secrets/<account>.gh}
#   1. runs with HOME = the account's own home (never the operator's) and no variable outside
#      the allow-list, auth_env and git's own config;
#   2. authenticates (the runtime's result is not an auth failure);
#   3. runs one harmless command (`git rev-parse --short HEAD`, echoed back in its report);
#   4. commits and pushes a throwaway branch `asf-smoke/<stamp>` to the product's origin.
#
# Nothing here goes through the ledger: no sessions.jsonl line, no id range, no row — the tick
# never sees this session. The throwaway branch is deleted from origin and the worktree removed
# at the end (pass --keep to leave both for a look). One session, the light model, a few cents.
#
# Exit 0 when every check passes, 1 when one fails (each check prints PASS/FAIL), 2 on a usage or
# config error. Run it by hand; it is never part of the suite.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

keep=0
args=()
for a in "$@"; do
  case "$a" in
    --keep) keep=1 ;;
    -h|--help) sed -n '2,31p' "$0"; exit 0 ;;
    *) args+=("$a") ;;
  esac
done
if [ "${#args[@]}" -lt 1 ]; then
  echo "usage: smoke_isolated_session.sh <product> [account] [--keep]" >&2
  exit 2
fi

SMOKE_PRODUCT="${args[0]}" SMOKE_ACCOUNT="${args[1]:-}" SMOKE_KEEP="$keep" \
  PYTHONPATH="$ROOT" exec python3 - <<'PY'
import json
import os
import subprocess
import sys
import tempfile
import time

from asf import env, hermetic
from asf.workers import githooks, pool, runtime, spawn

product_name = os.environ['SMOKE_PRODUCT']
account_name = os.environ.get('SMOKE_ACCOUNT') or None
keep = os.environ.get('SMOKE_KEEP') == '1'
failed = []


def check(name, ok, detail=''):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f'  — {detail}' if detail else ''), flush=True)
    if not ok:
        failed.append(name)


def git(args, cwd, **kw):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, **kw)


try:
    cfg = env.load_config()
    product = env.load_product(product_name)
except env.ConfigError as e:
    print(f'smoke: {e}', file=sys.stderr)
    sys.exit(2)
accounts = pool.accounts_from_config(cfg)
workers = [a for a in accounts if (a.name == account_name if account_name else a.role != 'cloud')]
if not workers:
    print(f'smoke: no account {account_name or "(any local)"} in worker_pool.accounts', file=sys.stderr)
    sys.exit(2)
acct = workers[0]
wp = cfg.get('worker_pool') or {}
models = wp.get('models') or {}
model = models.get('light') or models.get('Sonnet') or next(iter(models.values()), None)
repo, main = product.repo_dir, product.main
stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
branch = f'asf-smoke/{stamp}'

print(f'smoke: product {product_name}, account {acct.name}, model {model}, branch {branch}')
print(f'smoke: isolate_home={acct.isolate_home} home={runtime.session_home(acct)} '
      f'config_dir={acct.config_dir or "(none: the runtime reads $HOME/.claude*)"}')

# ---- the isolated home, seeded exactly as a launch seeds it ------------------------------------
home, missing = runtime.seed_home(acct)
check('isolated home', home is not None and os.path.realpath(home) != os.path.realpath(os.path.expanduser('~')),
      f'{home}' if home else 'isolate_home is false: the session runs on the operator HOME')
if missing:
    check('home_seed paths exist', False, ', '.join(missing))
if home:
    seeded = sorted(os.path.relpath(os.path.join(r, f), home)
                    for r, _d, fs in os.walk(home) for f in fs)
    print(f'smoke: the home holds {len(seeded)} file(s): ' + ', '.join(seeded[:20])
          + (' …' if len(seeded) > 20 else ''))

# ---- the account's credentials, by name only (auth_env) --------------------------------------
print('smoke: auth_env ' + (', '.join(f'{k}={v}' for k, v in sorted(acct.auth_env.items()))
                            or '(none)'))
check('auth_env has the runtime login', any(v in acct.auth_env for v in runtime.RUNTIME_AUTH_VARS),
      f'add auth_env: {{{runtime.RUNTIME_AUTH_VARS[0]}: ~/.ASF/secrets/{acct.name}.token}} '
      f'(`{runtime.RUNTIME_TOKEN_COMMAND}`)')
check('auth_env has the push token', runtime.GIT_TOKEN_VAR in acct.auth_env,
      f'add auth_env: {{{runtime.GIT_TOKEN_VAR}: ~/.ASF/secrets/{acct.name}.gh}}')
try:
    secrets = runtime.auth_env_values(acct)
except runtime.AuthEnvError as e:
    print(f'smoke: {e}', file=sys.stderr)
    sys.exit(2)

# ---- a throwaway worktree off the trunk -------------------------------------------------------
wt_root = os.path.join(env.state_dir(product), 'smoke')
os.makedirs(wt_root, exist_ok=True)
wt = tempfile.mkdtemp(prefix=f'{stamp}-', dir=wt_root)
os.rmdir(wt)
git(['fetch', '-q', 'origin', main], repo)
p = git(['worktree', 'add', '-q', '-b', branch, wt, f'origin/{main}'], repo)
if p.returncode:
    print(f'smoke: worktree add failed: {p.stderr.strip()}', file=sys.stderr)
    sys.exit(2)

brief = os.path.join(wt_root, f'{stamp}.brief.md')
with open(brief, 'w', encoding='utf-8') as f:
    f.write(f"""This is a smoke test of the factory's worker environment. Do exactly this, nothing else:

1. Run `git rev-parse --short HEAD` and `echo "HOME=$HOME"`.
2. Create the file `asf-smoke-{stamp}.txt` containing the single line `smoke {stamp}`.
3. `git add` it, `git commit -m "chore(smoke): isolated session {stamp}"`.
4. `git push origin HEAD:{branch}` (this throwaway branch only; never the trunk).

End with one line: `SMOKE head=<the short sha from step 1> pushed=<yes|no>`.
""")

log = os.path.join(wt_root, f'{stamp}.jsonl')
job = runtime.Job(product.name, f'smoke-{stamp}', wt, brief, model, account=acct,
                  add_dirs=[], permission_mode=wp.get('permission_mode') or runtime.DEFAULT_PERMISSION_MODE,
                  env={'ASF_SESSION': f'{product.name}/smoke-{stamp}@{stamp}'}, log_path=log,
                  settings_file=spawn.settings_file(wp), hooks_dir=githooks.ensure(product),
                  passthrough=env.env_passthrough(cfg))

# ---- the environment the session gets --------------------------------------------------------
job_env = runtime.build_env(job)
allowed = set(hermetic.WORKER_ALLOW) | set(job.passthrough) | set(secrets) | {
    'HOME', 'CLAUDE_CONFIG_DIR', 'ASF_PRODUCT', 'ASF_JOB', 'ASF_SESSION'}
stray = sorted(k for k in job_env if k not in allowed and not k.startswith(('LC_', 'GIT_CONFIG_')))
check('environment is the allow-list', not stray, ', '.join(stray))
check('HOME is not the operator\'s', job_env.get('HOME') != os.path.expanduser('~'), job_env.get('HOME', ''))
helpers = git(['config', '--get-all', f'credential.{runtime.GIT_TOKEN_HOST}.helper'], wt,
              env=job_env).stdout.splitlines()
check('git pushes with GH_TOKEN, not a keychain',
      runtime.GIT_TOKEN_VAR not in secrets or helpers[-1:] == [runtime.GIT_CREDENTIAL_HELPER],
      f'credential helpers for {runtime.GIT_TOKEN_HOST}: {len(helpers)}')

# ---- one real session ------------------------------------------------------------------------
rt = runtime.from_config(cfg)
started = time.monotonic()
result = rt.run(job, wait=True)
print(f'smoke: session exited {result.returncode} after {time.monotonic() - started:.0f}s; log {log}')
text = runtime.mask(result.text or '', secrets)
check('authenticates', result.reason != 'auth' and bool(runtime.init_line(log)),
      result.reason or ('no init line in the log' if not runtime.init_line(log) else ''))
check('session succeeded', bool(result.ok), (result.reason or text[-200:]).strip())
short = git(['rev-parse', '--short', f'origin/{main}'], repo).stdout.strip()
check('ran a harmless command', f'head={short}' in text or short in text,
      f'expected the trunk head {short} in the report: {text[-200:].strip()!r}')
remote = git(['ls-remote', '--heads', 'origin', branch], repo).stdout.split()
local = git(['rev-parse', 'HEAD'], wt).stdout.strip()
check('pushed the throwaway branch', bool(remote) and remote[0] == local,
      f'origin {branch} = {remote[0] if remote else "(absent)"}, worktree HEAD = {local}')

# ---- clean up --------------------------------------------------------------------------------
if keep:
    print(f'smoke: kept worktree {wt} and origin branch {branch} (--keep)')
else:
    if remote:
        git(['push', '-q', 'origin', '--delete', branch], repo)
    git(['worktree', 'remove', '--force', wt], repo)
    git(['branch', '-D', branch], repo)
print('smoke: ' + ('ALL PASS' if not failed else f"FAILED: {', '.join(failed)}"))
sys.exit(1 if failed else 0)
PY
