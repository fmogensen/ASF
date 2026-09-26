"""asf.workers — the worker pool as code: spawn, launch wave, health, stall, quota, reserve-id,
sessions.

``register(sub)`` adds ``asf workers spawn|wave|health|stall|quota|reserve-id|sessions --product
X``; each leaf sets ``func`` so the caller dispatches with ``args.func(args)``.
"""
import json
import sys


def _product(args):
    from asf import env
    return env.load_product(args.product)


def _read_rows(path):
    from asf.workers import pool
    if path in (None, '-'):
        text = sys.stdin.read()
    else:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    return [r for r in (pool.parse_row(ln) for ln in text.splitlines()) if r is not None]


def cmd_spawn(args):
    from asf.workers import pool, spawn
    product = _product(args)
    cfg = spawn.load_cfg()
    row = pool.parse_row(args.row)
    if row is None:
        print(f'workers spawn: not a feeder row: {args.row!r}', file=sys.stderr)
        return 2
    accts = {a.name: a for a in pool.accounts_from_config(cfg)}
    if args.account:
        acct = accts.get(args.account)
        if acct is None:
            print(f'workers spawn: no account {args.account!r} in worker_pool.accounts',
                  file=sys.stderr)
            return 2
    else:
        acct, reason = pool.Pool.from_config(cfg, product).pick_account(
            row.kind, row.model, is_fix=row.is_fix, lane=row.lane)
        if acct is None:
            print(f'waits {row.job} — {reason}')
            return 1
    with open(args.brief, encoding='utf-8') as f:
        brief = f.read()
    rec = spawn.spawn(product, row, acct, brief, cfg=cfg)
    print(f"launched {rec['job']} → {rec['account']} ({rec['model']}) pid {rec['pid']}")
    return 0


def cmd_reserve_id(args):
    """Reserve (or return, if the job already holds one) a BACKLOG_ID_RANGE block and print it —
    for a launcher outside ``workers spawn`` to export before it runs ``new`` in a worktree
    (B-0007). The block is released by ``workers health --fix`` once the worktree is reaped."""
    from asf.workers import spawn
    product = _product(args)
    cfg = spawn.load_cfg()
    wp = cfg.get('worker_pool') or {}
    rng = spawn.reserve_id_range(product, args.job,
                                 prefixes=wp.get('id_range_prefixes') or spawn.DEFAULT_ID_PREFIXES,
                                 start=int(wp.get('id_range_start', spawn.DEFAULT_ID_START)),
                                 size=int(wp.get('id_range_size', spawn.DEFAULT_ID_SIZE)))
    print(rng)
    return 0


def cmd_wave(args):
    from asf.workers import wave
    launched, _waits = wave.wave(_product(args), _read_rows(args.rows), args.n)
    return 0


def cmd_health(args):
    from asf.workers import health
    from asf.workers import retention
    product = _product(args)
    from asf.workers import worktrees
    health.health(product, fix=args.fix)
    if args.fix:
        worktrees.reap(product)
    else:  # what the worktree reaper would remove, and why the rest stay
        worktrees.report(worktrees.dry_run(product)[0])
    retention.sweep(product, fix=args.fix)  # without --fix: what would go
    return 0


def cmd_stall(args):
    from asf.workers import stall
    found = stall.stall(_product(args))
    return 1 if found else 0


def cmd_quota(args):
    from asf.workers import pool, quota, spawn
    product = _product(args)
    cfg = spawn.load_cfg()
    p = pool.Pool.from_config(cfg, product)
    print('| Account | Lane | Load / cap | 5h % | 7d % | Band |')
    print('|---|---|---|---|---|---|')
    for a in p.accounts:
        u = p.usage(a)
        # the pool's band: a session limit (quota-limits.json) stops the account whatever the
        # reading says — the same stop the wave and the status row read
        state, why = p.band(a)
        u = u or {}
        cell = state if state == quota.FREE else f'{state} — {why}'
        print(f"| {a.name} | {a.role} | {p.load(a)} / {a.cap} | {u.get('five_h_pct', '?')} | "
              f"{u.get('seven_d_pct', '?')} | {cell} |")
    return 0


def cmd_sessions(args):
    """``asf workers sessions [--json]`` (F-0076 S-8156): every session
    :mod:`asf.workers.observe` sees on this machine, in pid order, and whose it is. Unreadable
    observation is ``sessions: unreadable — <why>`` on stderr and exit 2, never a table."""
    from asf.workers import observe, pool, spawn
    product = _product(args)
    cfg = spawn.load_cfg()
    accounts = pool.accounts_from_config(cfg)
    source = observe.source_from_config(cfg)
    observed, why = observe.read(cfg, accounts, source=source)
    if why:
        print(f'sessions: unreadable — {why}', file=sys.stderr)
        return 2
    observed = sorted(observed, key=lambda o: o.pid)
    if getattr(args, 'json', False):
        print(json.dumps([{'pid': o.pid, 'account': o.account, 'owner': o.owner,
                           'session': o.session} for o in observed]))
        return 0
    for o in observed:
        account = o.account if o.account is not None else '—'
        session = o.session if o.session is not None else '—'
        if o.account is None:
            session += ' (unattributed)'
        print(f'{o.pid}  {account}   {o.owner}   {session}')
    p = pool.Pool.from_config(cfg, product, session_source=source)
    for a in p.accounts:
        load = p.load(a)
        foreign = sum(1 for s in p.live
                     if s.get('account') == a.name and s.get('owner') == 'foreign')
        print(f'load: {a.name} {load}/{a.cap} (asf {load - foreign}, foreign {foreign})')
    return 0


def register(sub):
    from asf.env import add_product_arg
    p = sub.add_parser('workers', help='the worker pool: spawn, wave, health, stall, quota, sessions')
    wsub = p.add_subparsers(dest='workers_command', required=True)

    s = wsub.add_parser('spawn', help='launch one feeder row now')
    add_product_arg(s)
    s.add_argument('--row', required=True, help='one feeder row (text or JSON)')
    s.add_argument('--brief', required=True, help='the brief file')
    s.add_argument('--account', help='default: the pick rule')
    s.set_defaults(func=cmd_spawn)

    w = wsub.add_parser('wave', help="launch up to N of the feeder's rows")
    add_product_arg(w)
    w.add_argument('--rows', default='-', help='feeder rows file (default: stdin)')
    w.add_argument('-n', type=int, default=1)
    w.set_defaults(func=cmd_wave)

    h = wsub.add_parser('health', help='dead pids, finished sessions, orphan worktrees')
    add_product_arg(h)
    h.add_argument('--fix', action='store_true', help='reap worktrees that pass the reap rule')
    h.set_defaults(func=cmd_health)

    st = wsub.add_parser('stall', help='silent or dead live sessions')
    add_product_arg(st)
    st.set_defaults(func=cmd_stall)

    q = wsub.add_parser('quota', help="each account's windows against the guard")
    add_product_arg(q)
    q.set_defaults(func=cmd_quota)

    ri = wsub.add_parser('reserve-id', help="a job's BACKLOG_ID_RANGE, for a launcher to export")
    add_product_arg(ri)
    ri.add_argument('--job', required=True, help='the job id the range is reserved for')
    ri.set_defaults(func=cmd_reserve_id)

    se = wsub.add_parser('sessions', help='every agent session on this machine, and whose it is')
    add_product_arg(se)
    se.add_argument('--json', action='store_true')
    se.set_defaults(func=cmd_sessions)
    return p
