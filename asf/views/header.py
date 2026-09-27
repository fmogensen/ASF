"""asf.views.header — the registry every table's first and last line reads from."""

#: Every command whose whole stdout is an operator table, and the title that table opens with.
#: The key is the command as an operator types it; a sub-command is joined with a space, because
#: that is what the stamp prints back. A command in this dict gets both lines (§2.2) and is
#: covered by tests/test_views_header.py; a command not in it prints neither.
TITLES = {
    'status': 'FACTORY STATUS',   'next': 'NEXT',
    'backlog': 'BOARD',           'roadmap': 'ROADMAP',
    'parity': 'PARITY',           'prod': 'PROD',
    'sessions': 'SESSIONS',       'scorecard': 'SCORECARD',
    'release-readiness': 'RELEASE READINESS',
    'capacity': 'CAPACITY',       'tokens': 'TOKENS',
    'stale': 'STALE',             'doctor': 'DOCTOR',
    'rules check': 'RULES',       'ci': 'CI',
    'ci queue': 'CI QUEUE',       'ci reconcile': 'CI RECONCILE',
    'ci reserve': 'CI RESERVE',   'health': 'HEALTH',
    'performance': 'PERFORMANCE',
}

#: The commands whose stamp names asf's own release rather than the product's HEAD: a table that
#: reports on the install, not on the product (asf/doctor.py:1000).
STAMP_VERSION = frozenset({'doctor'})


def head(command, product, clause=''):
    """``**FACTORY STATUS asf** — 04:11`` — the frozen first line of every table in TITLES.
    `product` is a name, never a Product (D10); `clause` is the renderer's own summary, passed
    through unchanged."""
    title = f"**{TITLES[command]} {product}**"
    return f"{title} — {clause}" if clause else title


def command_key(args):
    """The TITLES key for a parsed command line: ``args.command``, plus the sub-command when the
    parser declares one as ``<command>_command`` (``ci_command``, ``rules_command``)."""
    dest = args.command.replace('-', '_') + '_command'
    return f"{args.command} {getattr(args, dest)}" if getattr(args, dest, None) else args.command
