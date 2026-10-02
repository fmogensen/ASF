"""asf.trunk_ruleset — the host side of "one door to the trunk": the doctor's read of the ruleset.

Under ``merge: queue`` the merge queue posts the commit status
:meth:`asf.conventions.Conventions.queue_status` (``asf/queue``) = success on the batch sha just
before it fast-forwards the trunk (:func:`asf.merge_queue.post_queue_status`). A repository ruleset on the
trunk — no bypass actors, no force-push, no deletion, that status required — then refuses every
other way onto the trunk: a ``gh pr merge``, a hand push, a web merge button. Every session and
worker shares one GitHub user, so a bypass actor would protect nothing; the status is the key.

:func:`doctor_rows` reads the rules GitHub applies to the trunk now
(``GET repos/<slug>/rules/branches/<trunk>`` — active rulesets only, one call) and is red when
none requires the queue's status; the row then names the ruleset to enable, when a disabled one
exists (one more call). :func:`break_glass` is the exact call to switch it off and on again.
"""
import json

from asf import gh_limit
from asf.harvest import harvest as H


def watched(product):
    """True when the product lands through the merge queue on a GitHub repo."""
    conv = getattr(product, 'conventions', None)
    return bool(getattr(product, 'repo_slug', None) and conv is not None
                and getattr(conv, 'merge_queue', lambda: False)())


def context_of(product):
    return product.conventions.queue_status()


def break_glass(slug, ruleset_id, on):
    """The one ``gh api`` call that sets ruleset ``ruleset_id`` to ``active`` (on) or
    ``disabled`` (off)."""
    state = 'active' if on else 'disabled'
    return (f'gh api -X PUT repos/{slug}/rulesets/{ruleset_id} '
            f'-f enforcement={state}')


def requiring(rules, context):
    """The ids of the rulesets among ``rules`` (the branch-rules listing) whose required status
    checks name ``context``."""
    ids = []
    for r in rules or ():
        if not isinstance(r, dict) or r.get('type') != 'required_status_checks':
            continue
        checks = (r.get('parameters') or {}).get('required_status_checks') or ()
        if any(isinstance(c, dict) and c.get('context') == context for c in checks):
            ids.append(r.get('ruleset_id'))
    return ids


def doctor_rows(product, gh=None):
    """``[(required, ok, detail)]``: one row — green with the ruleset id and the break-glass
    call, red when no active ruleset on the trunk requires the queue's status (naming a disabled
    one to enable), unknown when the host cannot be read. ``[]`` off the merge queue."""
    if not watched(product):
        return []
    gh = gh or H._gh
    slug, trunk, context = product.repo_slug, product.conventions.main, context_of(product)
    try:
        rc, out, err = gh(['api', f'repos/{slug}/rules/branches/{trunk}'])
        rules = json.loads(out) if rc == 0 and out.strip() else None
        if rules is None:
            return [(True, None, f'{trunk} rules unreadable — {H.tail(err or out) or rc}')]
        ids = requiring(rules, context)
        if ids:
            rid = ids[0]
            return [(True, True, f'ruleset {rid} requires {context} on {trunk} — break-glass '
                                 f'(operator-approved outage only, re-enable at once): '
                                 f'{break_glass(slug, rid, False)}; re-enable: '
                                 f'{break_glass(slug, rid, True)}')]
        rc, out, _err = gh(['api', f'repos/{slug}/rulesets'])
        sets = json.loads(out) if rc == 0 and out.strip() else []
        off = [s for s in sets if isinstance(s, dict) and s.get('enforcement') != 'active'
               and s.get('target') == 'branch']
        if off:
            rid = off[0].get('id')
            return [(True, False, f"ruleset {rid} ({off[0].get('name')}) is "
                                  f"{off[0].get('enforcement')} — {trunk} is open to direct "
                                  f'merges; enable: {break_glass(slug, rid, True)}')]
        return [(True, False, f'no active ruleset requires {context} on {trunk} — any merge '
                              f'bypasses the queue (docs/guide/operating.md, "The trunk ruleset")')]
    except gh_limit.RateLimited as e:
        return [(True, None, f'{trunk} ruleset not read — {e}')]
    except ValueError as e:
        return [(True, None, f'{trunk} rules unparseable — {e}')]
