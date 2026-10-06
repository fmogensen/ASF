"""asf.connectors.protocols — what each connector kind must answer.

Every call that reads or acts on an external service returns a result that is either a real
answer or **Unknown** (:class:`asf.github.Result`: ``ok``, ``data``, ``reason``) — never an empty
value standing in for "could not tell". An implementation that cannot do an operation at all
returns Unknown with a ``reason`` saying so; it does not raise.

``slug`` is the product's repository id on its forge (``owner/name`` on the default forge);
``**kw`` carries the transport's own knobs (``timeout``, ``run``, ``env``) and an implementation
may ignore any it has no use for.
"""
from typing import Optional, Protocol, Sequence, runtime_checkable


@runtime_checkable
class Forge(Protocol):
    """``forge``: pull requests, their checks, branches, merges."""

    name: str

    def pr(self, slug: str, number, fields: Sequence[str], **kw):
        """One PR's ``fields`` — ``data`` a dict."""

    def prs(self, slug: str, *, state: str = 'all', search: str = '', limit: int = 100,
            fields: Sequence[str] = (), **kw):
        """PRs in ``state`` matching ``search``, newest first — ``data`` a list."""

    def open_prs(self, slug: str, limit: int = 300, fields: Sequence[str] = (), **kw):
        """The open PRs — ``data`` a list (empty is a real "none open")."""

    def merge_commit(self, slug: str, number, **kw):
        """The sha PR ``number`` merged at — ``data`` a str (``''``: not merged)."""

    def checks(self, slug: str, sha: str, **kw):
        """The check runs on ``sha`` — ``data`` a list of dicts."""

    def close_pr(self, slug: str, number, comment: str = '', **kw):
        """Close PR ``number`` (with ``comment`` when given)."""

    def reopen_pr(self, slug: str, number, **kw):
        """Reopen PR ``number``."""

    def api(self, path: str, method: str = 'GET', fields: Optional[dict] = None, **kw):
        """A REST call on the forge's API — ``data`` the parsed JSON. An operation no named
        method covers yet; a forge without such an API returns Unknown."""

    def auth_status(self, **kw):
        """Whether the forge's client is logged in — ``ok`` true when it is."""


@runtime_checkable
class CI(Protocol):
    """``ci``: workflow runs, their jobs and logs, rerun/cancel, the runners."""

    name: str

    def runs(self, slug: str, *, fields: Sequence[str] = (), limit: int = 50, **query):
        """Runs filtered by ``query`` (``workflow``, ``branch``, ``commit``, ``status`` …) —
        ``data`` a list."""

    def run(self, slug: str, run_id, **kw):
        """One run — ``data`` a dict."""

    def runs_for_sha(self, slug: str, sha: str, **kw):
        """The runs on commit ``sha`` — ``data`` the host's listing."""

    def run_log(self, slug: str, run_id, *, failed: bool = True, **kw):
        """A run's log (only its failed jobs by default) — ``data`` text."""

    def rerun(self, slug: str, run_id, failed: bool = True, **kw):
        """Re-run ``run_id`` (only its failed jobs by default)."""

    def cancel(self, slug: str, run_id, **kw):
        """Cancel ``run_id``."""

    def call(self, args: Sequence[str], **kw):
        """A call in the CI client's own argv dialect, for an operation no named method covers
        yet; a CI without such a client returns Unknown."""

    def source(self, product, run=None):
        """What the CI start queue reads and does (:class:`asf.ci_queue.Source`)."""

    def backend(self, product, run=None):
        """The self-hosted runner pool's host (:class:`asf.ci_pool.Backend`)."""


@runtime_checkable
class Runtime(Protocol):
    """``runtime``: where an agent session runs. :meth:`local` and :meth:`cloud` return a session
    runtime — ``run(job, wait=False)`` and ``continue_run(job)`` returning
    :class:`asf.workers.runtime.Result` (``ok`` None while it runs; the log at ``job.log_path``)."""

    name: str

    def local(self):
        """The runtime a session on this machine runs on."""

    def cloud(self, settings, product):
        """The runtime the cloud lane launches on (``settings``: :class:`asf.workers.cloud.Settings`)."""


@runtime_checkable
class Quota(Protocol):
    """``quota``: an account's usage, read for the quota guard."""

    def read(self, account) -> Optional[dict]:
        """``{'five_h_pct': n, 'seven_d_pct': n, …}`` for ``account``, or None when unknown."""


@runtime_checkable
class Secrets(Protocol):
    """``secrets``: resolve one ``auth_env`` reference to its value."""

    name: str

    def read(self, ref: str) -> str:
        """The value ``ref`` names, stripped. Raises :class:`FileNotFoundError` when nothing is
        there and :class:`OSError` (or :class:`UnicodeDecodeError`) when it cannot be read — the
        launch then refuses with NEEDS OPERATOR, naming ``ref`` and never a value."""


@runtime_checkable
class Scheduler(Protocol):
    """``scheduler``: the clocks — one job per clock, labelled ``<prefix>.<product>.<clock>``.
    :func:`asf.scheduler.render` builds the job (``label``, ``argv``, ``log``, ``every_s`` or
    ``at``); the connector writes its definition, loads it, and reads it back. The durable
    pause stays :mod:`asf.scheduler`'s: a connector asks it before loading a clock."""

    name: str

    def render(self, job: dict, workdir: str, env_vars: dict) -> dict:
        """``job`` with this scheduler's definition added (and ``path``, where it goes)."""

    def definition_path(self, label: str) -> str:
        """Where ``label``'s definition lives on disk."""

    def installed_labels(self, pattern: str) -> list:
        """The labels matching the glob ``pattern`` whose definition is on disk."""

    def install(self, job: dict) -> list:
        """Write and load ``job``; the lines to print."""

    def uninstall(self, label: str, remove_definition: bool = True) -> list:
        """Unload ``label`` (and delete its definition); the lines to print."""

    def load(self, path: str):
        """Load the definition at ``path`` — ``(ok, error)``."""

    def stop(self, label: str) -> bool:
        """Unload ``label``, killing a running run of it; True when it was loaded."""

    def status(self, label: str) -> dict:
        """``{'label', 'loaded', 'state', 'runs', 'last_exit', 'never_exited', …}``."""

    def loaded_labels(self) -> Optional[list]:
        """Every label the scheduler holds, or None when it cannot be read."""

    def locate(self, label: str):
        """``(path, {'ProgramArguments': argv, 'WorkingDirectory': cwd})`` of a loaded label."""
