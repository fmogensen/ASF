"""tests/contracts.py — recorded ``gh`` answers for the host behaviours ASF reads, replayed.

Each fixture under ``tests/fixtures/gh/<behaviour>/<call>.json`` is one real ``gh`` call, recorded
read-only against a product repo and then redacted (:class:`Redactor`)::

    {"argv": ["api", "repos/example/repo/commits/<sha>/check-runs?per_page=100"],
     "rc": 0, "stdout": <the parsed JSON, or the raw text>, "stderr": ""}

:func:`load` returns one call's ``(rc, stdout, stderr)``; :class:`FixtureGh` answers every call
of one or more behaviours in the shape of :func:`asf.harvest.harvest._gh` (``fx(args)``), of
:func:`asf.github.call` (:func:`as_call`) and of ``subprocess.run`` (``fx.run(['gh', *args],
...)``), so a reader is driven through its own ``gh`` seam. A call no fixture names answers ``rc 1`` with a ``no fixture`` stderr — never a
silent empty success.

The redactor is deterministic: the product's slug becomes ``example/repo``, its branches
``worker/T-0001`` …, every 40-hex sha the next one of :data:`SHAS`, every number of six digits
or more (run, job and check-run ids, an account id) the next id from :data:`FIRST_ID`, every
check/job name the name the recording maps it to (else ``job-<n>``), and every JSON key not in
:data:`KEEP` is dropped — so no author, title, body, node id or app record is ever stored. The
fixture set is append-only: a new host behaviour gets its fixture before its fix lands.

Record (console, read-only ``gh``)::

    python3 -m tests.contracts record <recipe.json>

with a recipe ``{"slug": "...", "branches": [...], "names": {...}, "behaviours":
{"<behaviour>": {"<call>": [<gh args>...]}}}`` kept outside the repo — it names the product.
"""
import hashlib
import json
import os
import re
import subprocess
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'gh')

SLUG = 'example/repo'
#: the deterministic sha set: a fixture carries no other 40-hex string
SHAS = tuple(hashlib.sha1(f'example-sha-{k}'.encode()).hexdigest() for k in range(1, 1000))
#: the first id a redacted run/job/check-run id takes; ids stay under six digits
FIRST_ID = 10001

#: every JSON key a reader of these calls uses; any other key is dropped by the redactor
KEEP = frozenset({
    # gh api …/check-runs, …/status, …/commits/<sha>, …/actions/runs/<id>
    'total_count', 'check_runs', 'id', 'name', 'status', 'conclusion', 'started_at',
    'completed_at', 'html_url', 'details_url', 'head_sha', 'event', 'created_at', 'updated_at',
    'run_attempt', 'head_branch', 'state', 'statuses', 'context', 'sha', 'commit', 'committer',
    'date',
    # gh pr view / gh run view --json …
    'jobs', 'databaseId', 'startedAt', 'completedAt', 'url', 'headSha', 'mergedAt',
    'mergeCommit', 'oid', 'headRefOid', 'baseRefOid', 'mergeStateStatus', 'headRefName',
    'closedAt', 'number', 'workflowName', 'attempt', 'createdAt', 'headBranch',
    # gh api …/actions/runs?head_sha=…
    'workflow_runs',
})

_SHA_RE = re.compile(r'\b[0-9a-f]{40}\b')
_ID_RE = re.compile(r'(?<![0-9A-Za-z])[0-9]{6,}(?![0-9A-Za-z])')
_REQUEST_ID_RE = re.compile(r'request ID [0-9A-F:]+')
_NAME_KEYS = ('name',)
_LEAD_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_.-]*')


class Redactor:
    """One recording's redaction: the same real sha, id, branch or name always maps to the same
    stand-in across every call of the recording (a run id in a check's link is the run the next
    call reads)."""

    def __init__(self, slug, branches=(), names=None):
        self.slug = slug
        self.branches = {b: f'worker/T-{i:04d}' for i, b in enumerate(branches, 1)}
        self.names = dict(names or {})
        self.shas, self.ids, self.jobs = {}, {}, {}

    def sha(self, real):
        if real not in self.shas:
            self.shas[real] = SHAS[len(self.shas)]
        return self.shas[real]

    def id(self, real):
        real = str(real)
        if real not in self.ids:
            self.ids[real] = str(FIRST_ID + len(self.ids))
        return self.ids[real]

    def name(self, real):
        """A check/job name: its leading token (the job key a required list names) mapped, the
        rest (a matrix suffix, ``${{ matrix.x }}``) kept."""
        m = _LEAD_RE.match(real)
        if not m:
            return real
        lead = m.group(0)
        if lead not in self.names:
            self.names[lead] = self.jobs.setdefault(lead, f'job-{len(self.jobs) + 1}')
        return self.names[lead] + real[m.end():]

    def text(self, s):
        owner = self.slug.split('/', 1)[0]
        s = re.sub(re.escape(self.slug), SLUG, s, flags=re.I)
        for real, fake in sorted(self.branches.items(), key=lambda kv: -len(kv[0])):
            s = s.replace(real, fake)
        s = _SHA_RE.sub(lambda m: self.sha(m.group(0)), s)
        s = _REQUEST_ID_RE.sub('request ID REDACTED', s)
        s = _ID_RE.sub(lambda m: self.id(m.group(0)), s)
        return re.sub(re.escape(owner), SLUG.split('/', 1)[0], s, flags=re.I)

    def value(self, v, key=None):
        if isinstance(v, dict):
            if key == 'committer':  # a person: only when the commit was made
                return {'date': v['date']} if 'date' in v else None
            return {k: self.value(x, k) for k, x in v.items() if k in KEEP
                    and not (k == 'committer' and not isinstance(x, dict) or
                             k == 'committer' and 'date' not in x)}
        if isinstance(v, list):
            return [self.value(x, key) for x in v]
        if isinstance(v, bool) or v is None:
            return v
        if isinstance(v, int):
            return int(self.id(v)) if v >= 100000 else v
        if isinstance(v, str):
            return self.text(self.name(v) if key in _NAME_KEYS else v)
        return v

    def call(self, argv, rc, stdout, stderr):
        """One recorded call, redacted: ``{argv, rc, stdout, stderr}``."""
        try:
            out = self.value(json.loads(stdout)) if stdout.strip() else stdout
        except ValueError:
            out = self.text(stdout)
        return {'argv': [self.text(a) for a in argv], 'rc': rc, 'stdout': out,
                'stderr': self.text(stderr)}


def path(behaviour, call):
    return os.path.join(ROOT, behaviour, f'{call}.json')


def fixture(behaviour, call):
    with open(path(behaviour, call), encoding='utf-8') as f:
        return json.load(f)


def _text(stdout):
    return stdout if isinstance(stdout, str) else json.dumps(stdout)


def load(behaviour, call):
    """``(rc, stdout, stderr)`` of one recorded call, as ``gh`` printed it."""
    fx = fixture(behaviour, call)
    return fx['rc'], _text(fx['stdout']), fx['stderr']


def behaviours():
    """Every recorded behaviour (a directory under :data:`ROOT`, nested ones as ``a/b``)."""
    out = []
    for dirpath, _dirs, files in os.walk(ROOT):
        if any(f.endswith('.json') for f in files):
            out.append(os.path.relpath(dirpath, ROOT).replace(os.sep, '/'))
    return sorted(out)


def calls(behaviour):
    d = os.path.join(ROOT, behaviour)
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith('.json'))


class FixtureGh:
    """Answers ``gh`` from the recorded calls of ``behaviours``: an exact argv match, else
    ``rc 1`` / ``no fixture``. ``calls`` keeps every argv asked, in order."""

    def __init__(self, *behaviours_):
        self.answers, self.calls = {}, []
        for b in behaviours_:
            for c in calls(b):
                fx = fixture(b, c)
                self.answers[tuple(fx['argv'])] = (fx['rc'], _text(fx['stdout']), fx['stderr'])

    def __call__(self, args):
        args = [str(a) for a in args]
        self.calls.append(args)
        return self.answers.get(tuple(args), (1, '', f"no fixture for gh {' '.join(args)}"))

    def run(self, cmd, *_a, **_kw):
        """``subprocess.run`` for a ``['gh', *args]`` argv."""
        rc, out, err = self(list(cmd)[1:])
        return subprocess.CompletedProcess(list(cmd), rc, out, err)


def as_call(fake):
    """``fake`` — a ``gh`` that answers ``(rc, stdout, stderr)`` (a :class:`FixtureGh`, a test's
    own) — in the shape of :func:`asf.github.call`. Patch ``asf.github.call`` with it and every
    reader answers from ``fake``: :mod:`asf.github`'s own and the :func:`asf.harvest.harvest._gh`
    shim over it alike."""
    from asf import github

    def call(args, *, timeout=None, run=None, env=None):
        rc, out, err = fake(list(args))
        out, err = out or '', err or ''
        if rc != 0:
            first = next((ln.strip() for ln in err.splitlines() if ln.strip()), '')
            return github.Result(False, None, rc, out, err, github.now_iso(),
                                 f'rc {rc}: {first}' if first else f'rc {rc}')
        return github.Result(True, out, rc, out, err, github.now_iso(), '')
    return call


def record(recipe, run=subprocess.run, root=ROOT):
    """Record every call of ``recipe`` (see the module doc) through ``gh`` and write the redacted
    fixtures under ``root``. One :class:`Redactor` per behaviour."""
    for behaviour, wanted in recipe['behaviours'].items():
        red = Redactor(recipe['slug'], recipe.get('branches', ()), recipe.get('names'))
        for call, argv in wanted.items():
            p = run(['gh', *argv], capture_output=True, text=True)
            rec = red.call(argv, p.returncode, p.stdout, p.stderr)
            dest = os.path.join(root, behaviour, f'{call}.json')
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, 'w', encoding='utf-8') as f:
                json.dump(rec, f, indent=1, sort_keys=True)
                f.write('\n')


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != 'record':
        sys.exit('usage: python3 -m tests.contracts record <recipe.json>')
    with open(sys.argv[2], encoding='utf-8') as fh:
        record(json.load(fh))
