"""asf.security.cmd — the ``asf security`` parser (T-0365): one subcommand, ``alerts``, and the
``--check`` contract ``rules.py`` reads (P10) — one line per place on stdout, exit 1 when there is
one, 0 when there is none. ``add_subparsers(required=True)`` so a second subcommand (``pass``,
``paths``) is an added line when F-0060's pass lands, not a reshape of this one."""
from asf import env
from asf.security import alerts


def _cmd_alerts(args):
    product = env.load_product(args.product)
    lines = alerts.violations(product)
    for line in lines:
        print(line)
    if args.check:
        return 1 if lines else 0
    return 0


def cmd_security(args):
    if args.security_cmd == 'alerts':
        return _cmd_alerts(args)


def register(sub):
    p = sub.add_parser('security', help="the host-read security checks (R-0009 and siblings)")
    ssub = p.add_subparsers(dest='security_cmd', required=True)

    ap = ssub.add_parser('alerts', help="the host's own open secret-scanning and Dependabot alerts")
    env.add_product_arg(ap)
    ap.add_argument('--check', action='store_true',
                    help='the rule contract: exit 1 if any line printed, 0 if none')

    p.set_defaults(run=cmd_security)
    return p
