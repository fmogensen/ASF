"""asf.views.pr_annotate — ``#123`` → ``#123 (merged|open|closed)`` from the live PR list.

Ported from the operator's per-table scripts' shared ``annotator``/``pr_states`` helpers, minus
the "in CI" tier: that tier read a batch-launcher's own runner log (an operator-internal file,
not part of any product's config) to tell "open, sitting in a live CI batch" apart from plain
"open". Without that log every batched PR shows as plain ``open`` instead of ``open (in CI)`` —
a known, one-line difference from the pre-``asf`` tables (see ``asf shadow-diff``), not a bug
here: the batch-log integration has no generic home yet.
"""
import json
import re
import subprocess

PRNUM = re.compile(r'#(\d+)')


def pr_states(product, timeout=60):
    """{number: state} for every PR in the product repo, one ``gh`` call."""
    if not product.repo_slug:
        return {}
    try:
        out = subprocess.run(
            ['gh', 'pr', 'list', '-R', product.repo_slug, '--state', 'all', '--limit', '300',
             '--json', 'number,state'],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if out.returncode != 0 or not out.stdout.strip():
        return {}
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return {}
    return {p['number']: p.get('state', '') for p in data}


def annotator(states):
    """(annotate, tally, named): ``#123`` → ``#123 (merged|open|closed)`` from ``states``."""
    tally = {'merged': 0, 'in CI': 0, 'open': 0}
    named = set()

    def annotate(text):
        def sub(m):
            n = int(m.group(1))
            state = states.get(n)
            if state == 'MERGED':
                mark = 'merged'
            elif state == 'OPEN':
                mark = 'open'
            elif state == 'CLOSED':
                mark = 'closed'
            else:
                return m.group(0)
            if n not in named:
                named.add(n)
                if mark in tally:
                    tally[mark] += 1
            return f"{m.group(0)} ({mark})"
        return PRNUM.sub(sub, text)

    return annotate, tally, named


def table(rows, head):
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in rows:
        lines.append("| " + " | ".join((c or "—").replace("|", "\\|") for c in r) + " |")
    return "\n".join(lines)
