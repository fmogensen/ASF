"""The five /asf readers run against this repo's index.json (T5). Imports nothing from tools/: each reader is a
script in ~/.claude-workers/tools, run with ASF_SOURCE=backlog and ASF_INDEX pointing at this checkout's index.json.

Each test asserts exit 0 and that the output has a markdown table header (a `| … |` line followed by its `|---|`
rule). tick-tables.py prints the whole tick's closing block — CI runs, quota, runners, next-work.sh — and takes about
3.5 minutes, so it only runs with ASF_TEST_SLOW=1; the pre-commit gate skips it.
"""
import json
import os
import re
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(REPO, "index.json")
READERS = os.path.expanduser("~/.claude-workers/tools")
ROW = re.compile(r'^STARVED → (SPEC +F-[A-Z0-9]+-[0-9]+ ".*"   → launch spec-[a-z0-9-]+ \(Opus\)'
                 r'|PLAN +F-[A-Z0-9]+-[0-9]+ ".*" \(spec [^ )]+\.md\)   → launch plan-[a-z0-9-]+ \(Opus\))$')


def run(script, *args, source="backlog", timeout=120):
    env = dict(os.environ, ASF_SOURCE=source, ASF_INDEX=INDEX)
    return subprocess.run([sys.executable, os.path.join(READERS, script), *args],
                          capture_output=True, text=True, timeout=timeout, env=env)


def headers(out):
    """The header rows of every markdown table in `out`: a `|` line whose next line is the `|---|` rule."""
    lines = out.splitlines()
    return [[c.strip() for c in lines[i].strip("|").split("|")] for i in range(len(lines) - 1)
            if lines[i].startswith("|") and re.match(r"^\|[-:|]+\|$", lines[i + 1])]


def items():
    with open(INDEX) as f:
        return json.load(f)["items"]


@unittest.skipUnless(os.path.isdir(READERS) and os.path.isfile(INDEX),
                      "the /asf readers are not installed on this machine, or this checkout has no index.json")
class Readers(unittest.TestCase):
    def ok(self, script, *args):
        p = run(script, *args)
        self.assertEqual(p.returncode, 0, f"{script} exited {p.returncode}\n{p.stdout[-800:]}\n{p.stderr[-800:]}")
        return p.stdout

    def test_goals_one_row_per_epic(self):
        out = self.ok("factory-goals.py")
        head = headers(out)
        self.assertTrue(head, "no table header")
        self.assertEqual(head[0][-1], "Spend / budget")
        rows = re.findall(r"^\| [^|]+ \| E-\d+ ", out, re.M)   # rank, then "E-0010 Title (GOAL 10)"
        self.assertEqual(len(rows), sum(1 for v in items().values() if v["type"] == "epic" and not v.get("removed")))

    def test_board_one_row_per_feature(self):
        out = self.ok("factory-board.py")
        head = headers(out)
        self.assertTrue(head, "no table header")
        self.assertEqual(head[0][:2], ["Feature", "Stage"])
        rows = re.findall(r"^\| F-\d+ ", out, re.M)
        self.assertEqual(len(rows), sum(1 for v in items().values() if v["type"] == "feature" and not v.get("removed")))

    def test_parity_counts_every_story(self):
        out = self.ok("factory-parity.py")
        head = headers(out)
        self.assertTrue(head, "no table header")
        self.assertEqual(head[0][0], "Area")
        n = sum(1 for v in items().values() if v["type"] == "story" and not v.get("removed"))
        self.assertRegex(out, rf"(?m)^\| \*\*All\*\*.*\*\*{n}\*\* \|$")
        self.assertTrue(headers(self.ok("factory-parity.py", "full"))[-1][:2] == ["When", "Rows"])

    def test_feeder_rows_have_the_shape_next_work_consumes(self):
        out = self.ok("feeder-rows.py", "50")   # no table: zero rows is a valid night, every row must be a SPEC or PLAN row
        for line in out.splitlines():
            self.assertRegex(line, ROW)

    def test_a_bad_source_is_refused(self):
        self.assertEqual(run("factory-parity.py", source="bogus").returncode, 2)

    @unittest.skipUnless(os.environ.get("ASF_TEST_SLOW"), "tick-tables.py takes ~3.5 min; set ASF_TEST_SLOW=1")
    def test_tick_tables_next_block(self):
        p = run("tick-tables.py", timeout=600)
        self.assertEqual(p.returncode, 0, p.stdout[-800:] + p.stderr[-800:])
        self.assertIn(["What", "State", "ETA"], headers(p.stdout))


if __name__ == "__main__":
    unittest.main()
