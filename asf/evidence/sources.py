"""asf/evidence/sources.py — the three interfaces the evidence pass reads through.

`GitSource` is every read of the product repo; `HostSource` is what the evidence pass reads off
the code host (pull requests, completed runs, a run's jobs — never runners or live jobs, those
are :class:`asf.ci_queue.Source`'s, see its module docstring); `DeploySource` is one environment's
deployed sha. `Sources(git, host, deploy)` carries the three together, built by
:func:`for_product` or replayed from a snapshot by :class:`Recorded`.

Stdlib only. No path literal, no product name, no vendor string outside the one concrete class
that needs it (`GitHubHost`'s ``gh``). Imports :mod:`asf.env` and nothing else of ``asf`` — in
particular not :mod:`asf.record`, so the direction of the passes (ingest reads evidence, never
the reverse) is enforced by the import graph, not just convention.
"""
import base64
import collections
import json
import os
import subprocess
import time

from asf import env

#: `pr_list`'s cache TTL, moved here with it.
PR_TTL = 180

#: the `ci.provider` values whose green runs on main a `GitHubHost` knows how to read.
GH_ACTIONS = ("gh-actions", "github-actions")

Sources = collections.namedtuple("Sources", "git host deploy")


class GitSource:
    """Every read of the product repo the evidence pass makes. Read-only: the one write it may
    do is `fetch --prune`."""

    def run(self, args, timeout=120):
        """`git <args>` in the product repo: stdout, stripped. '' on any failure."""
        raise NotImplementedError

    def cat_file(self, kind, requests):
        """One `cat-file --batch[-check]` for the whole list, answers in request order, None for
        a missing object. `kind` is 'check' (→ oid) or 'blob' (→ bytes)."""
        raise NotImplementedError

    def fetch(self):
        """`fetch --prune origin`. A no-op for a source that cannot fetch."""
        return None


class HostSource:
    """What the evidence pass reads off the code host. Never runners, never live jobs — those
    are `asf.ci_queue.Source`'s."""

    #: what `prs()` answers with, per pull request
    PR_FIELDS = ("number", "title", "body", "state", "headRefName", "mergedAt",
                 "mergeCommit", "mergeable", "createdAt")

    def prs(self):
        """Every pull request, any state, newest first: [{PR_FIELDS}]. [] when there is none or
        it is unreadable."""
        raise NotImplementedError

    def runs(self, workflow, branch=None, status=None, limit=20):
        """[{headSha, conclusion, status, createdAt, updatedAt, databaseId}], newest first."""
        raise NotImplementedError

    def run_jobs(self, run_id):
        """[{name, conclusion}] of one run's jobs."""
        return []


class DeploySource:
    """The deployed sha per environment — the code host's deploy workflow, a vendor's API or a
    product script, behind one call."""

    def deployment(self, env_name):
        """(sha, at) deployed to `env_name` ('prod', 'dev', a named target), or (None, None)
        when the product names no way to read it."""
        raise NotImplementedError

    def sha(self, env_name):
        """The sha half of :meth:`deployment` — kept because most callers want only that."""
        return self.deployment(env_name)[0]


def _run(run_fn, args, cwd, timeout, **kwargs):
    try:
        return run_fn(args, cwd=cwd, timeout=timeout, **kwargs)
    except Exception:
        return None


class LocalGit(GitSource):
    """`GitSource` over the resolved `product.repo_dir`."""

    def __init__(self, product, run=subprocess.run):
        self.product = product
        self._run = run

    def run(self, args, timeout=120, cwd=None):
        r = _run(self._run, ["git", *args], cwd or self.product.repo_dir, timeout,
                 capture_output=True, text=True)
        return r.stdout.strip() if r is not None else ""

    def cat_file(self, kind, requests):
        """One `git cat-file --batch[-check]` process for the whole request list.

        --batch answers with the OID, not the request, so the only way back to "which request
        was this" is position: the answers arrive in request order, and a missing object still
        costs one line.
        """
        if not requests:
            return []
        args = ["git", "cat-file", "--batch-check" if kind == "check" else "--batch"]
        p = self._run(args, cwd=self.product.repo_dir,
                      input=("\n".join(requests) + "\n").encode(), capture_output=True)
        out = p.stdout
        res, i = [], 0
        for _ in requests:
            j = out.find(b"\n", i)
            if j < 0:
                res.append(None)
                continue
            header = out[i:j].decode("utf-8", "replace").split()
            i = j + 1
            if len(header) < 3 or header[-1] in ("missing", "ambiguous"):
                res.append(None)
                continue
            if kind == "check":
                res.append(header[0])
                continue
            size = int(header[2])
            res.append(out[i:i + size])
            i += size + 1
        return res

    def fetch(self):
        self.run(["fetch", "--prune", "-q", "origin"], timeout=180)


class GitHubHost(HostSource):
    """`HostSource` over `gh` — the only `gh` strings the evidence pass carries."""

    def __init__(self, product, run=subprocess.run):
        self.product = product
        self._run = run
        self._cache = os.path.join(env.state_dir(product), "cache-prs.json")

    def _gh(self, args, timeout=180):
        r = _run(self._run, ["gh", *args], self.product.repo_dir, timeout,
                 capture_output=True, text=True)
        return r.stdout.strip() if r is not None else ""

    def prs(self):
        if os.path.exists(self._cache) and time.time() - os.path.getmtime(self._cache) < PR_TTL:
            try:
                with open(self._cache) as f:
                    return json.load(f)
            except Exception:
                pass
        raw = self._gh(["pr", "list", "-R", self.product.repo_slug, "--state", "all",
                       "--limit", "300", "--json", ",".join(self.PR_FIELDS)])
        try:
            data = json.loads(raw) if raw else []
        except Exception:
            data = []
        try:
            tmp = self._cache + f".{os.getpid()}"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self._cache)
        except Exception:
            pass
        return data

    def runs(self, workflow, branch=None, status=None, limit=20):
        args = ["run", "list", "-R", self.product.repo_slug, "--workflow", workflow,
                "--limit", str(limit), "--json",
                "headSha,conclusion,status,createdAt,updatedAt,databaseId"]
        if branch:
            args += ["--branch", branch]
        if status:
            args += ["--status", status]
        raw = self._gh(args)
        try:
            return json.loads(raw) if raw else []
        except Exception:
            return []

    def run_jobs(self, run_id):
        raw = self._gh(["api", f"repos/{self.product.repo_slug}/actions/runs/{run_id}/jobs"
                        "?per_page=60"])
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            data = {}
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        return [{"name": j.get("name"), "conclusion": j.get("conclusion")} for j in jobs]


class NoHost(HostSource):
    """Every method answers empty. Starts no process — the `run` it is handed, if any, is
    never called (D4)."""

    def __init__(self, run=None):
        self._run = run

    def prs(self):
        return []

    def runs(self, workflow, branch=None, status=None, limit=20):
        return []

    def run_jobs(self, run_id):
        return []


class WorkflowDeploy(DeploySource):
    """The newest success of `deploy_sha.<env>.workflow` through the *host* provider — the one
    place `GitSource`/`HostSource`/`DeploySource` compose. `dev` reads `ci.workflow`'s newest
    trunk run whose `ci.dev_job` succeeded, through `host.run_jobs`."""

    def __init__(self, product, host):
        self.product = product
        self.host = host

    def deployment(self, env_name):
        if env_name == "dev":
            return self._dev_deployment()
        workflow = self._workflow_for(env_name)
        if not workflow:
            return None, None
        for run in self.host.runs(workflow, limit=15):
            if run.get("conclusion") == "success":
                return run.get("headSha"), run.get("updatedAt")
        return None, None

    def _workflow_for(self, env_name):
        if env_name == "prod":
            return self.product.conventions.deploy_workflow
        deploy = self.product.deploy_sha
        block = deploy.get(env_name) if isinstance(deploy, dict) else None
        return block.get("workflow") if isinstance(block, dict) else None

    def _dev_deployment(self):
        conv = self.product.conventions
        workflow, job = conv.ci_workflow, conv.ci_dev_job
        if not workflow or not job:
            return None, None
        for run in self.host.runs(workflow, branch=self.product.main, limit=40):
            if run.get("status") != "completed":
                continue
            jobs = self.host.run_jobs(run.get("databaseId"))
            if any(j.get("name") == job and j.get("conclusion") == "success" for j in jobs):
                return run.get("headSha"), run.get("updatedAt")
        return None, None


class NoDeploy(DeploySource):
    """(None, None) for every environment."""

    def deployment(self, env_name):
        return None, None


class Recorded:
    """Satisfies `GitSource`, `HostSource` and `DeploySource` from a directory of `git.json` /
    `host.json` / `deploy.json`, each a flat map from a call key to its answer.

    The key is `<interface>.<method>` plus the arguments, never a command line (D7): `git.run`
    joins its argv with spaces (the shape of the command it replaces), the rest join their
    arguments with `:`. `blob` answers are base64, because a blob is bytes. A key with no
    recorded answer raises `KeyError` naming the call (D8) — a fixture that silently answers
    empty would turn "the pass stopped asking" into a passing test.
    """

    def __init__(self, snapshot_dir):
        self._answers = {}
        for name in ("git", "host", "deploy"):
            path = os.path.join(snapshot_dir, f"{name}.json")
            if not os.path.exists(path):
                continue
            with open(path) as f:
                self._answers.update(json.load(f))

    def _answer(self, key):
        if key not in self._answers:
            raise KeyError(f"evidence fixture: no answer for {key}")
        return self._answers[key]

    # ---- GitSource ----
    def run(self, args, timeout=120, cwd=None):
        return self._answer("git.run:" + " ".join(args))

    def cat_file(self, kind, requests):
        out = []
        for req in requests:
            value = self._answer(f"git.cat_file:{kind}:{req}")
            out.append(base64.b64decode(value) if kind == "blob" and value is not None else value)
        return out

    def fetch(self):
        return None

    # ---- HostSource ----
    def prs(self):
        return self._answer("host.prs")

    def runs(self, workflow, branch=None, status=None, limit=20):
        key = "host.runs:" + ":".join(str(p) for p in (workflow, branch) if p is not None)
        return self._answer(key)

    def run_jobs(self, run_id):
        return self._answer(f"host.run_jobs:{run_id}")

    # ---- DeploySource ----
    def deployment(self, env_name):
        return tuple(self._answer(f"deploy.deployment:{env_name}"))

    def sha(self, env_name):
        return self.deployment(env_name)[0]


def ci_provider(product):
    """The product's `ci.provider`, lower-cased; None for `ci: none` or no provider at all."""
    ci = product.ci if product is not None else None
    name = ci if isinstance(ci, str) else (ci or {}).get("provider") if isinstance(ci, dict) else None
    name = str(name).strip().lower() if name else ""
    return None if name in ("", "none", "off") else name


def _deploy_configured(product):
    d = product.deploy_sha
    return isinstance(d, dict) and bool(d)


def for_product(product, git=None, host=None, deploy=None):
    """`Sources(git, host, deploy)`: `LocalGit`; `NoHost` when `ci_provider(product) is None`,
    else `GitHubHost`; `WorkflowDeploy` when the product configures a `deploy_sha`, else
    `NoDeploy` (PD14). Each argument overrides its default."""
    picked_git = git if git is not None else LocalGit(product)
    picked_host = host if host is not None else (
        NoHost() if ci_provider(product) is None else GitHubHost(product))
    picked_deploy = deploy if deploy is not None else (
        WorkflowDeploy(product, picked_host) if _deploy_configured(product) else NoDeploy())
    return Sources(picked_git, picked_host, picked_deploy)
