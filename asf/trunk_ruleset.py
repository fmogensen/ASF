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

The other half, for a product that lands green PRs through the merge script (merged by the
shared token): ``asf ruleset install|status|break-glass`` (:func:`cmd_ruleset`). The ruleset
:data:`NAME` requires ``conventions.landing_checks`` on the merged head (``strict``), forbids
deletion and force-push, and requires a pull request with no approvals — so ``gh pr merge`` is
the only door and a hand push to the trunk is refused by the host. ``--dry-run`` prints the exact
API payload and the diff against what the host has; nothing is written.
"""
import datetime
import json
import os
import tempfile

from asf import env, gh_limit
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


# ---- asf ruleset install | status | break-glass --------------------------------------------

NAME = 'asf trunk'


def required_contexts(product):
    """``conventions.landing_checks`` as a list of check names (a lone string is one name)."""
    named = product.conventions.get('landing_checks')
    return [str(named)] if isinstance(named, str) else [str(n) for n in named or ()]


def desired(product):
    """The ruleset body ``asf ruleset install`` sends: the trunk, ``active``, no bypass actors,
    no deletion, no force-push, the landing checks required on the merged head, a pull request
    with 0 approvals. Raises :class:`ValueError` when the product names no landing check (a
    ruleset that requires nothing would only lock the door)."""
    contexts = required_contexts(product)
    if not contexts:
        raise ValueError('conventions.landing_checks names no check — nothing to require')
    return {
        'name': NAME,
        'target': 'branch',
        'enforcement': 'active',
        'bypass_actors': [],
        'conditions': {'ref_name': {'include': [f'refs/heads/{product.conventions.main}'],
                                    'exclude': []}},
        'rules': [
            {'type': 'deletion'},
            {'type': 'non_fast_forward'},
            {'type': 'required_status_checks', 'parameters': {
                'strict_required_status_checks_policy': True,
                'do_not_enforce_on_create': False,
                'required_status_checks': [{'context': c} for c in contexts]}},
            {'type': 'pull_request', 'parameters': {
                'required_approving_review_count': 0,
                'dismiss_stale_reviews_on_push': False,
                'require_code_owner_review': False,
                'require_last_push_approval': False,
                'required_review_thread_resolution': False}},
        ],
    }


def _rule_key(rule):
    return rule.get('type')


def _norm(body):
    """The comparable shape of a ruleset: enforcement, target, bypass actors, ref conditions and
    the rules by type (the host adds ids, timestamps and links; those never count)."""
    body = body or {}
    ref = ((body.get('conditions') or {}).get('ref_name') or {})
    rules = {}
    for r in body.get('rules') or ():
        if isinstance(r, dict):
            params = r.get('parameters')
            if r.get('type') == 'required_status_checks' and isinstance(params, dict):
                params = dict(params)
                params['required_status_checks'] = sorted(
                    c.get('context') for c in params.get('required_status_checks') or ()
                    if isinstance(c, dict))
            rules[_rule_key(r)] = params
    return {'enforcement': body.get('enforcement'), 'target': body.get('target'),
            'bypass_actors': body.get('bypass_actors') or [],
            'include': sorted(ref.get('include') or ()), 'exclude': sorted(ref.get('exclude') or ()),
            'rules': rules}


def diff(existing, want):
    """Human lines describing how ``existing`` (the host's ruleset body, or ``None``) differs from
    ``want``. ``[]`` when it already matches."""
    if existing is None:
        return [f'+ ruleset {want["name"]!r} would be created']
    have, new = _norm(existing), _norm(want)
    out = []
    for key in ('enforcement', 'target', 'bypass_actors', 'include', 'exclude'):
        if have[key] != new[key]:
            out.append(f'~ {key}: {have[key]!r} -> {new[key]!r}')
    for typ in sorted(set(have['rules']) | set(new['rules'])):
        if typ not in have['rules']:
            out.append(f'+ rule {typ} {json.dumps(new["rules"][typ], sort_keys=True)}')
        elif typ not in new['rules']:
            out.append(f'- rule {typ}')
        elif have['rules'][typ] != new['rules'][typ]:
            out.append(f'~ rule {typ}: {json.dumps(have["rules"][typ], sort_keys=True)} -> '
                       f'{json.dumps(new["rules"][typ], sort_keys=True)}')
    return out


def _read(gh, slug):
    """``(rulesets, error)``: every branch ruleset of the repo, or ``(None, why)``."""
    rc, out, err = gh(['api', f'repos/{slug}/rulesets'])
    if rc != 0:
        return None, H.tail(err or out) or f'rc {rc}'
    try:
        sets = json.loads(out) if out.strip() else []
    except ValueError as e:
        return None, f'unparseable — {e}'
    return [s for s in sets if isinstance(s, dict)], None


def _find(gh, slug):
    """``(id, body, error, others)``: our ruleset by name (its full body is one more call), and
    the names of the other active branch rulesets."""
    sets, err = _read(gh, slug)
    if sets is None:
        return None, None, err, []
    others = [s.get('name') for s in sets if s.get('name') != NAME
              and s.get('target') == 'branch' and s.get('enforcement') == 'active']
    mine = [s for s in sets if s.get('name') == NAME]
    if not mine:
        return None, None, None, others
    rid = mine[0].get('id')
    rc, out, e = gh(['api', f'repos/{slug}/rulesets/{rid}'])
    try:
        body = json.loads(out) if rc == 0 and out.strip() else None
    except ValueError:
        body = None
    if body is None:
        return rid, None, f'ruleset {rid} unreadable — {H.tail(e or out) or rc}', others
    return rid, body, None, others


def _send(gh, method, endpoint, body):
    """One mutating call, the body through a file (``gh api --input``)."""
    fd, path = tempfile.mkstemp(suffix='.json', prefix='asf-ruleset-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(body, f)
        return gh(['api', '-X', method, endpoint, '--input', path])
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def install(product, gh=None, dry_run=False, say=print):
    """Create or update (by name) ruleset :data:`NAME` on the product's repo; ``0`` on success.
    ``dry_run`` prints the method, endpoint, exact payload and the diff, and writes nothing."""
    gh = gh or H._gh
    slug = getattr(product, 'repo_slug', None)
    if not slug:
        say('asf ruleset: the product has no GitHub repo')
        return 2
    try:
        want = desired(product)
    except ValueError as e:
        say(f'asf ruleset: {e}')
        return 2
    try:
        rid, body, err, others = _find(gh, slug)
    except gh_limit.RateLimited as e:
        say(f'asf ruleset: host not read — {e}')
        return 1
    if err:
        say(f'asf ruleset: {slug}: {err}')
        return 1
    method, endpoint = (('PUT', f'repos/{slug}/rulesets/{rid}') if rid is not None
                        else ('POST', f'repos/{slug}/rulesets'))
    changes = diff(body, want)
    for other in others:
        say(f'note: another active ruleset applies to the branches of {slug}: {other}')
    if dry_run:
        say(f'DRY RUN — nothing written. {method} {endpoint}')
        say(json.dumps(want, indent=2, sort_keys=True))
        say('diff against the host: ' + ('none — already installed' if not changes else ''))
        for line in changes:
            say('  ' + line)
        return 0
    if rid is not None and not changes:
        say(f'ruleset {NAME!r} ({rid}) on {slug} already matches — nothing to do')
        return 0
    rc, out, e = _send(gh, method, endpoint, want)
    if rc != 0:
        say(f'asf ruleset: {method} {endpoint} failed — {H.tail(e or out) or rc}')
        return 1
    say(f'ruleset {NAME!r} {"updated" if rid is not None else "created"} on {slug}: '
        f'{want["conditions"]["ref_name"]["include"][0]} requires '
        f'{", ".join(required_contexts(product))}')
    return 0


def status(product, gh=None, say=print):
    """Print whether ruleset :data:`NAME` is installed and how it differs from :func:`desired`."""
    gh = gh or H._gh
    slug = getattr(product, 'repo_slug', None)
    if not slug:
        say('asf ruleset: the product has no GitHub repo')
        return 2
    try:
        want = desired(product)
        rid, body, err, others = _find(gh, slug)
    except ValueError as e:
        say(f'asf ruleset: {e}')
        return 2
    except gh_limit.RateLimited as e:
        say(f'asf ruleset: host not read — {e}')
        return 1
    if err:
        say(f'asf ruleset: {slug}: {err}')
        return 1
    for other in others:
        say(f'note: another active ruleset applies to the branches of {slug}: {other}')
    if rid is None:
        say(f'ruleset {NAME!r} is not installed on {slug} — the trunk takes direct pushes')
        return 1
    changes = diff(body, want)
    say(f'ruleset {NAME!r} ({rid}) on {slug}: {body.get("enforcement")}'
        + (' — matches' if not changes else ' — differs:'))
    for line in changes:
        say('  ' + line)
    return 0 if not changes else 1


def log_break_glass(product, what, now=None):
    """Append one line to ``<state>/break-glass.log`` (when, product, what)."""
    stamp = (now or datetime.datetime.now(datetime.timezone.utc)).strftime('%Y-%m-%dT%H:%M:%SZ')
    path = os.path.join(env.state_dir(product), 'break-glass.log')
    with open(path, 'a', encoding='utf-8') as f:
        f.write(f'{stamp} {product.name} {what}\n')
    return path


def break_glass_off(product, gh=None, say=print, now=None):
    """Delete ruleset :data:`NAME` — the trunk is open — with a loud line and a log entry."""
    gh = gh or H._gh
    slug = getattr(product, 'repo_slug', None)
    if not slug:
        say('asf ruleset: the product has no GitHub repo')
        return 2
    rid, _body, err, _others = _find(gh, slug)
    if err:
        say(f'asf ruleset: {slug}: {err}')
        return 1
    if rid is None:
        say(f'ruleset {NAME!r} is not installed on {slug} — the trunk is already open')
        return 0
    rc, out, e = gh(['api', '-X', 'DELETE', f'repos/{slug}/rulesets/{rid}'])
    if rc != 0:
        say(f'asf ruleset: DELETE failed — {H.tail(e or out) or rc}')
        return 1
    log_break_glass(product, f'OFF ruleset {rid} deleted on {slug}', now)
    say(f'!!! BREAK-GLASS: ruleset {NAME!r} ({rid}) DELETED — {slug} {product.conventions.main} '
        f'is OPEN to direct pushes. Land the one change, then: asf ruleset break-glass --on')
    return 0


def cmd_ruleset(args, gh=None):
    """``asf ruleset install [--dry-run] | status | break-glass --off|--on [--product P]``."""
    product = env.load_product(getattr(args, 'product', None))
    action = args.action
    if action == 'install':
        return install(product, gh=gh, dry_run=bool(getattr(args, 'dry_run', False)))
    if action == 'status':
        return status(product, gh=gh)
    on, off = bool(getattr(args, 'on', False)), bool(getattr(args, 'off', False))
    if on == off:
        print('asf ruleset break-glass: give exactly one of --off or --on')
        return 2
    if off:
        return break_glass_off(product, gh=gh)
    rc = install(product, gh=gh)
    if rc == 0:
        log_break_glass(product, f'ON ruleset reinstalled on {product.repo_slug}')
    return rc


def register(sub):
    p = sub.add_parser('ruleset', help='install, read or lift the trunk ruleset (required checks, '
                                       'no direct push)')
    p.add_argument('action', choices=('install', 'status', 'break-glass'))
    p.add_argument('--dry-run', action='store_true', dest='dry_run',
                   help='install: print the exact API payload and the diff; write nothing')
    p.add_argument('--off', action='store_true', help='break-glass: delete the ruleset')
    p.add_argument('--on', action='store_true', help='break-glass: reinstall the ruleset')
    env.add_product_arg(p)
    p.set_defaults(func=cmd_ruleset)
    return p
