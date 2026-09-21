"""asf.tick.step_daily — the tick's once-a-day step, in the record clone.

``groom --apply`` (yesterday's answers applied, the inbox filed, today's ``groom/<day>.md``),
``stale``, ``file-bugs``, ``rollup`` for yesterday with its releases, written into the record
clone — the tick's one commit (:func:`asf.tick.tick.finish`) carries them with the rest of the
tick. One line per part: ``daily: <part> ok|FAILED — <its last line>``. A part that fails does
not stop the others; the step fails (and the day is not stamped) when any did.
Whether it is due today is the tick's stamp (:func:`asf.tick.steps.daily_due`).
"""
import argparse
import contextlib
import datetime
import io


def _ns(**kw):
    return argparse.Namespace(**kw)


def yesterday(today=None):
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    return (today - datetime.timedelta(days=1)).isoformat()


def parts(product, root):
    """``[(name, thunk)]`` in order; each thunk returns an exit code."""
    from asf.groom.groom import cmd_groom
    from asf.metrics.metrics import cmd_rollup
    from asf.tick.file_bugs import cmd_file_bugs
    from asf.tick.stale import cmd_stale
    epic = (product.conventions or {}).get('default_bug_epic')
    return [
        ('groom', lambda: cmd_groom(_ns(date=None, apply=True, product=product.name,
                                        default_bug_epic=epic), root)),
        ('stale', lambda: cmd_stale(_ns(json=False), root)),
        ('file-bugs', lambda: cmd_file_bugs(_ns(default_bug_epic=epic), root)),
        ('rollup', lambda: cmd_rollup(_ns(day=yesterday(), no_releases=False,
                                          product=product.name), root)),
    ]


def run_part(thunk):
    """``(rc, last line of what it printed)``; an exception is rc 1 and its message."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = thunk() or 0
    except Exception as e:  # noqa: BLE001 — a part's failure never stops the other parts
        return 1, (str(e) or type(e).__name__).strip().splitlines()[0]
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    return rc, (lines[-1] if lines else '')


def run(ctx, out=print):
    product = ctx.product
    root = ctx.record_root()
    failed = []
    for name, thunk in parts(product, root):
        rc, last = run_part(thunk)
        if rc:
            failed.append(name)
        out(f"daily: {name} {'FAILED' if rc else 'ok'}" + (f' — {last}' if last else ''))
    if failed:
        raise RuntimeError(f"daily parts failed: {', '.join(failed)}")
    return 0
