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
