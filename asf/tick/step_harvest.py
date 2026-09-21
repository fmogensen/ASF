"""asf.tick.step_harvest — the tick's ``harvest`` step: land the finished lane branches.

Runs after ``prs`` and before ``batch``. Every ``origin/<prefix>*`` branch of the product repo
whose session ended green and whose commits all name its item is landed by the product's
``landing`` convention (:func:`asf.harvest.harvest.run_product_harvest`): fast-forwarded onto
the trunk behind the gate, or — with a merge queue — left to the PR the ``prs`` step opened. A
red gate holds the branch and files (or bumps) a Bug in the tick's record clone.

A held branch is not a failed step: it is one line, and the next tick looks again.
"""
from asf.harvest import harvest


def run(ctx, out=print):
    product = ctx.product
    if not product.repo_dir:
        out('harvest: no repo_dir — nothing to harvest')
        return 0
    results = harvest.run_product_harvest(product, bug_root=ctx.record_root, out=out)
    if not results:
        out('harvest: none to land')
    return 0
