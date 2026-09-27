#!/usr/bin/env bash
# tools/spike/f-0065/fixture.sh — F-0065 Task 1 (§2.4). The throwaway product that gives five of
# probe.sh's fourteen probes (P09-P13) something to ask. Built under the state dir, outside every
# repository, and never imported by anything under asf/. Idempotent: a second run reuses what is
# already there and only prints the path again.
#
# Layout, all under FIXTURE_DIR (this same formula is duplicated in probe.sh — the two must agree):
#   repo/           a git repo with one commit, origin -> ../origin.git (P02, P03)
#   origin.git/     the bare remote beside it, so P03's local ref negotiation needs no host
#   package/        a package manifest with exactly one dependency, not yet installed (P10)
#   browser/        a headless-browser script and its own installed engine, downloaded HERE,
#                   outside any session (P11)
#   server/         a one-script loopback development server (P12)
#   test.sh         the fixture's own test command: exec, a TMPDIR write, a git fixture with its
#                   own hooksPath (P09, PD8)
#   briefs/         a fixture brief naming the one side file a prober probe writes (P13)
set -u

ASF_HOME="${ASF_HOME:-$HOME/.ASF}"
FIXTURE_DIR="${ASF_HOME}/state/spike/f-0065/fixture"

mkdir -p "$FIXTURE_DIR"

# ---- repo/ + origin.git/ (P02, P03) ---------------------------------------------------------

repo="$FIXTURE_DIR/repo"
origin="$FIXTURE_DIR/origin.git"
if [ ! -d "$repo/.git" ]; then
  mkdir -p "$origin"
  git init -q --bare "$origin"
  git init -q -b main "$repo"
  git -C "$repo" config user.email 'f0065@example.invalid'
  git -C "$repo" config user.name 'f0065 fixture'
  printf '# f0065 fixture repo\n\nThrowaway. Built by tools/spike/f-0065/fixture.sh for F-0065.\nDeleted with the rest of tools/spike/f-0065/ once the spike lands its findings.\n' \
    > "$repo/README.md"
  git -C "$repo" add README.md
  git -C "$repo" -c core.hooksPath="$repo/.git/hooks" commit -q -m 'f0065 fixture: initial commit'
  git -C "$repo" remote add origin "$origin"
  git -C "$repo" -c core.hooksPath="$repo/.git/hooks" push -q origin HEAD:refs/heads/main
fi

# ---- package/ (P10) --------------------------------------------------------------------------
# One dependency, nothing installed here: P10 is the install happening *inside* the session.

mkdir -p "$FIXTURE_DIR/package"
cat > "$FIXTURE_DIR/package/package.json" <<'JSON'
{
  "name": "f0065-package-fixture",
  "private": true,
  "description": "F-0065 throwaway fixture for P10 (dependency installation inside a session).",
  "dependencies": {
    "ms": "2.1.3"
  }
}
JSON

# ---- browser/ (P11) --------------------------------------------------------------------------
# The engine's own download happens here, outside any session (D6): a coder probe only execs it.

mkdir -p "$FIXTURE_DIR/browser"
cat > "$FIXTURE_DIR/browser/package.json" <<'JSON'
{
  "name": "f0065-browser-fixture",
  "private": true,
  "description": "F-0065 throwaway fixture for P11 (the headless browser engine).",
  "dependencies": {
    "playwright": "1.63.0"
  }
}
JSON
cat > "$FIXTURE_DIR/browser/browser.js" <<'JS'
// F-0065 throwaway fixture for P11: launch the engine, load about:blank, print its version, exit.
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch();
  const version = browser.version();
  const page = await browser.newPage();
  await page.goto('about:blank');
  await page.close();
  await browser.close();
  console.log('engine ' + version);
})().catch((e) => {
  console.error('ERR ' + (e && e.message ? e.message : e));
  process.exit(1);
});
JS
if [ ! -d "$FIXTURE_DIR/browser/node_modules/playwright" ]; then
  ( cd "$FIXTURE_DIR/browser" && npm install --no-audit --no-fund --silent )
fi
# Idempotent: installs only the browsers this build of playwright does not already have cached.
( cd "$FIXTURE_DIR/browser" && npx --yes playwright install chromium )

# ---- server/ (P12) ---------------------------------------------------------------------------

mkdir -p "$FIXTURE_DIR/server"
cat > "$FIXTURE_DIR/server/server.js" <<'JS'
// F-0065 throwaway fixture for P12: bind a loopback port, answer one path, exit on SIGTERM.
const http = require('http');

const server = http.createServer((req, res) => {
  if (req.url === '/f0065') {
    res.writeHead(200, { 'Content-Type': 'text/plain' });
    res.end('f0065 ok');
  } else {
    res.writeHead(404);
    res.end();
  }
});
server.listen(0, '127.0.0.1', () => {
  console.log('PORT ' + server.address().port);
});
process.on('SIGTERM', () => process.exit(0));
JS

# ---- test.sh (P09, PD8) -----------------------------------------------------------------------
# The fixture product's own test command: exec, a TMPDIR write, and a git fixture with its own
# hooksPath — never this repository's suite (PD8).

cat > "$FIXTURE_DIR/test.sh" <<'SH'
#!/usr/bin/env bash
# F-0065 throwaway fixture test command (P09): exec, a TMPDIR write, a git fixture with its own
# hooksPath.
set -u
work="$(mktemp -d "${TMPDIR:-/tmp}/f0065-test.XXXXXX")" || exit 1
trap 'rm -rf "$work"' EXIT

hooks="$work/hooks"
mkdir -p "$hooks"
cat > "$hooks/pre-commit" <<'HOOK'
#!/usr/bin/env bash
echo "f0065 fixture hook ran" > "$(dirname "$0")/../hook-ran"
HOOK
chmod +x "$hooks/pre-commit"

git init -q "$work/repo" || exit 1
git -C "$work/repo" config user.email 'f0065@example.invalid'
git -C "$work/repo" config user.name 'f0065 fixture'
git -C "$work/repo" config core.hooksPath "$hooks"
git -C "$work/repo" commit -q --allow-empty -m 'f0065 fixture test' || exit 1

if [ ! -f "$work/hook-ran" ]; then
  echo "f0065 test: the fixture's own hooksPath never ran" >&2
  exit 1
fi
echo "f0065 test: ok"
SH
chmod +x "$FIXTURE_DIR/test.sh"

# ---- briefs/ (P13) ----------------------------------------------------------------------------
# The one file a prober session exists to write: a fixture brief naming the side file's path, in
# the same shape as a real brief under <state>/<product>/briefs.

mkdir -p "$FIXTURE_DIR/briefs"
cat > "$FIXTURE_DIR/briefs/f0065-prober.brief.md" <<EOF
# f0065 prober brief (fixture, F-0065 P13)

This is not a real brief: it is what fixture.sh gives P13 to read, in the same shape as a real
one under <state>/<product>/briefs. Read the readings file's path below and write the reading
there — nothing else.

SIDE_FILE: $FIXTURE_DIR/readings/prober-reading.md
EOF

echo "$FIXTURE_DIR"
