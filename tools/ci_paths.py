"""tools/ci_paths.py — is a pull request docs-only (the tests workflow's fast path)?

  python3 tools/ci_paths.py [--base <rev>] [--head <rev>]     prints docs_only=true|false

A pull request whose diff touches only the Markdown files under :data:`DOCS_ONLY_DIRS` — the
spec, plan, decision, research and review records, which no test module and no end-to-end step
reads from this checkout — needs no test shard: the `tests` workflow runs only the checks that
do read every tracked file (the factory-only merge rule on the diff, check_generic, the privacy
sweep) and its `tests (<python>)` jobs report green on those. Anything else — README.md, the
guide, docs/KNOWN-ISSUES.md, a `.sh` or `.yaml` beside a spec, a workflow file — is a full run.

No file list (an unreadable diff, an empty one) is never docs-only: the full suite runs. The diff
defaults to ``HEAD^1..HEAD``, which on a pull request's merge commit is exactly what the PR
changes on top of its base.
"""
import argparse
import subprocess
import sys

#: the directories whose Markdown files no test reads (verified against tests/ and the
#: workflow's end-to-end steps; tests/test_workflow.py pins this list against the repository)
DOCS_ONLY_DIRS = ('docs/specs/', 'docs/plans/', 'docs/decisions/', 'docs/research/',
                  'docs/reviews/')


def docs_only(files):
    """True when ``files`` is non-empty and every one is a ``.md`` under :data:`DOCS_ONLY_DIRS`."""
    files = [f.strip() for f in files or () if f and f.strip()]
    if not files:
        return False
    return all(f.endswith('.md') and f.startswith(DOCS_ONLY_DIRS) for f in files)


def changed(base, head):
    """The files ``base..head`` changes, or None when git cannot tell."""
    p = subprocess.run(['git', 'diff', '--name-only', base, head], capture_output=True, text=True)
    if p.returncode != 0:
        return None
    return [l for l in p.stdout.splitlines() if l.strip()]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--base', default='HEAD^1')
    ap.add_argument('--head', default='HEAD')
    args = ap.parse_args(argv)
    files = changed(args.base, args.head)
    verdict = docs_only(files)
    print(f'ci_paths: {len(files) if files is not None else "unknown"} changed file(s); '
          f'docs-only: {"yes" if verdict else "no"}', file=sys.stderr)
    print(f'docs_only={"true" if verdict else "false"}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
