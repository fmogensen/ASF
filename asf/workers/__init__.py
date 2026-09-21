"""asf.workers — the worker pool as code: spawn, launch wave, health, stall, quota.

``register(sub)`` adds ``asf workers spawn|wave|health|stall|quota --product X``; each leaf sets
``func`` so the caller dispatches with ``args.func(args)``.
"""
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


def cmd_wave(args):
    from asf.workers import wave
    launched, _waits = wave.wave(_product(args), _read_rows(args.rows), args.n)
    return 0


def cmd_health(args):
    from asf.workers import health
    health.health(_product(args), fix=args.fix)
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
    print('| Account | Lane | Load / cap | 5h % | 7d % | Under guard |')
    print('|---|---|---|---|---|---|')
    for a in p.accounts:
        u = p.usage(a)
        ok, why = quota.under_guard(u, p.guards)
        u = u or {}
        print(f"| {a.name} | {a.role} | {p.load(a)} / {a.cap} | {u.get('five_h_pct', '?')} | "
              f"{u.get('seven_d_pct', '?')} | {'yes' if ok else 'no — ' + why} |")
    return 0


def register(sub):
    from asf.env import add_product_arg
    p = sub.add_parser('workers', help='the worker pool: spawn, wave, health, stall, quota')
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
    return p
