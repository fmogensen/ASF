"""asf.tick.step_watchdog — the tick's ``watchdog`` step: the dwell-time watchdog
(:mod:`asf.dwell`) run with its two actions and its alarms — each breach printed, a
``watchdog`` event and a Bug. Never a failed step: a breach is the alarm itself."""


def run(ctx, out=print):
    from asf import dwell
    return dwell.run_step(ctx, out=out)
