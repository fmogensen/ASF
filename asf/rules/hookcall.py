"""The contract every core rule hook shares (F-0061 §2.4): one entry point, one refusal shape,
one ledger write.

A rule module under ``asf.rules.hooks`` is a ``decide(call, ctx)`` and a :func:`run` call in its
``__main__``. ``decide`` returns None to allow the call, or the refusal lines to print.

No module-level import here reads a config file or a product: a hook fires on every tool call of
every session, and the operator's console (no ``ASF_JOB``) must leave before any of that runs.
"""
import dataclasses
import datetime
import json
import os
import sys

CALL = 'the call the runtime sent: {tool_name, tool_input, cwd, hook_event_name}'
HOOK_ERROR_LINE = 'rule hook {rule} broke ({why}) — the call is refused; this is the hook, not you'

#: The last line of every refusal: a rule's answer is for the session, never a person.
NOT_A_QUESTION = 'This is not a question for a person: do not print NEEDS OPERATOR for it.'

#: How much of the command or path a ledger row keeps.
DETAIL_LIMIT = 120


@dataclasses.dataclass(frozen=True)
class Ctx:
    """What every rule needs and none re-derives."""
    product: object
    job: str
    item: str
    cwd: str
    in_worktree: bool
    in_backlog: bool
    in_repo: bool


def _inside(path, root):
    """True when ``path`` is ``root`` or under it, both resolved; False when either is unset."""
    if not path or not root:
        return False
    a = os.path.realpath(os.path.expanduser(str(path)))
    b = os.path.realpath(os.path.expanduser(str(root)))
    try:
        return os.path.commonpath([a, b]) == b
    except ValueError:                               # different drives: never inside
        return False


def context(call, environ):
    """The :class:`Ctx` for one call: the product from ``ASF_PRODUCT``, the job and item from
    ``ASF_JOB`` and ``ASF_ITEM``, the call's ``cwd`` (else this process's), and where that cwd
    sits — a linked worktree, the product's backlog, its repo."""
    from asf import env
    from asf.record import ids
    product = env.load_product(environ.get('ASF_PRODUCT'))
    cwd = (call or {}).get('cwd') or os.getcwd()
    return Ctx(
        product=product,
        job=environ.get('ASF_JOB') or '',
        item=environ.get('ASF_ITEM') or '',
        cwd=cwd,
        in_worktree=ids._in_worktree(cwd),
        in_backlog=_inside(cwd, getattr(product, 'backlog_dir', None)),
        in_repo=_inside(cwd, getattr(product, 'repo_dir', None)),
    )


def refusal(subject, title, rule, *lines):
    """§2.4's four-part refusal: what was refused and by which rule, why, ``instead:``, and
    :data:`NOT_A_QUESTION`. ``lines`` are the indented middle — the why, then the instead."""
    return [f'REFUSED {subject} — {title} ({rule})'] + [f'  {l}' for l in lines] + [
        f'  {NOT_A_QUESTION}']


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _detail(call):
    """The command or the path the call carried, first :data:`DETAIL_LIMIT` characters,
    scrubbed of protected names."""
    tool_input = (call or {}).get('tool_input') or {}
    text = (tool_input.get('command') or tool_input.get('file_path')
            or tool_input.get('notebook_path') or '')
    text = str(text)[:DETAIL_LIMIT]
    try:
        from asf import redact
        return redact.scrub(text, redact.default_patterns())
    except Exception:                                # noqa: BLE001 — a scrub never blocks a row
        return text


def run(rule, decide, stdin_text=None, environ=None, out=sys.stderr):
    """The rc the runtime reads: 0 lets the call through, 2 refuses it and feeds ``out`` back to
    the model. ``decide(call, ctx)`` returns None to allow, or the refusal lines to print.

    ``ASF_JOB`` unset → 0 before anything else (not a factory session); None → 0, silent,
    nothing written; lines → 2, the lines on ``out`` and one ``refused-rule`` row (no ``hold``
    key: nothing parks); any exception → 2 with :data:`HOOK_ERROR_LINE` and one
    ``rule-hook-error`` row — fail closed, and say it was the hook."""
    environ = os.environ if environ is None else environ
    if not environ.get('ASF_JOB'):
        return 0
    job = environ.get('ASF_JOB')
    ctx = None
    try:
        if stdin_text is None:
            stdin_text = sys.stdin.read()
        call = json.loads(stdin_text or '{}') or {}
        ctx = context(call, environ)
        lines = decide(call, ctx)
        if not lines:
            return 0
        for line in lines:
            print(line, file=out)
        _append(ctx.product, {'event': 'refused-rule', 'rule': rule, 'item': ctx.item,
                              'job': job, 'tool': call.get('tool_name') or '',
                              'detail': _detail(call), 'ts': _now_iso()})
        return 2
    except Exception as e:                           # noqa: BLE001 — fail closed (D5)
        why = str(e) or type(e).__name__
        print(HOOK_ERROR_LINE.format(rule=rule, why=why), file=out)
        _record_error(ctx.product if ctx else environ.get('ASF_PRODUCT'), rule, job, why, e)
        return 2


def _append(product, record):
    from asf import approvals
    approvals.append(product, record)


def _record_error(product, rule, job, why, exc):
    """One ``rule-hook-error`` row and the traceback in the hook log — never ``hook-error``,
    which the tick counts as the approvals guard breaking. Never raises: the failure being
    recorded is often the state directory itself."""
    from asf import approvals
    for write in (lambda: approvals._log_traceback(job, exc),
                  lambda: _append(product, {'event': 'rule-hook-error', 'rule': rule, 'job': job,
                                            'why': why[:200], 'ts': _now_iso()})):
        try:
            write()
        except Exception:                            # noqa: BLE001 — see the docstring
            pass
