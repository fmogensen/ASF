"""asf.tick.step_groom — the tick's ``groom`` step: intake, the policy pass, the questions.

Runs on every tick, between ``health`` (whose ``ended`` lines make the in-flight set true) and
``wave`` (which launches what this step just decided). Two parts, in order: the pending
answers files, then ``asf groom --apply`` against the record clone. What ``cmd_groom`` prints
is the tick's ``groom:`` line.
"""
import argparse


def _ns(**kw):
    return argparse.Namespace(**kw)


def run(ctx, out=print):
    from asf.groom import answers
    from asf.groom.groom import cmd_groom
    from asf.tick.tick import StepFailed
    product, root = ctx.product, ctx.record_root()
    answers.apply_pending_answers(product, root, event=ctx.event, out=out)
    epic = (product.conventions or {}).get('default_bug_epic')
    rc = cmd_groom(_ns(date=None, apply=True, rebuild=False, incremental=True,
                       product=product.name, default_bug_epic=epic, answers_file=None,
                       event=ctx.event), root)
    if rc:
        raise StepFailed(f'groom exited {rc}')
    return 0
