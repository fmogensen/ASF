"""asf.surface — what every ``asf`` command is called, what it does, and what it writes.

One row per top-level command. ``asf --help`` is rendered from this table, and
``tests/test_surface.py`` refuses a parser that disagrees with it: a command registered and not
declared here, or declared here and not registered, fails the build. That is the whole point —
the surface grew from 30 commands to 49 in one week, and the descriptions grew apart.
"""

#: What running a command changes. ``None`` is read-only: it prints and exits.
RECORD = 'the record'          # the backlog checkout: items, index, tables
REPO = 'the product repo'      # files in the product's own checkout
HOST = 'this host'             # the install, the schedule, the pool
PRS = 'a pull request'         # state on the product's open pull requests

#: The only verbs a description may open with. One voice is a closed list, not an instruction.
#: A verb enters by editing this line, and never by loosening the check that reads it.
VERBS = frozenset({
    'adopt', 'bring', 'build', 'check', 'claim', 'compare', 'derive', 'file', 'follow',
    'import', 'install', 'land', 'list', 'mint', 'print', 'read', 'rebuild', 'record',
    'reinstall', 'release', 'render', 'reopen', 'run', 'scan', 'set', 'sort', 'type', 'write',
})

#: command -> (description, effect, flag)
#: ``effect`` is ``None`` for read-only. ``flag`` names the flag or subcommand that makes it
#: write, when it does not write by default; ``None`` means it always writes.
COMMANDS = {
    'new':                ('mint a work item and print its id', RECORD, None),
    'inbox':               ('file an untyped note for the next groom to type', RECORD, None),
    'set':                  ('set typed fields on a card', RECORD, None),
    'retire':               ('record an item as landed by hand and take it off the board, a card or an unread note', RECORD, None),
    'undeliver':            ("record that a member is not built by its lead's delivery and send it back to its own lane", RECORD, None),
    'reopen':               ('reopen an item closed in error and re-derive its state from the evidence', RECORD, None),
    'untick':               ("clear a Story's acceptance tick so the line must be proved again", RECORD, None),
    'audit-proofs':         ('list every done Story with an acceptance line no test proved', RECORD, '--apply'),
    'check':                ('check the record for errors and print each one with its item', None, None),
    'index':                ("rebuild every item's children and backlinks, and the record's index", RECORD, None),
    'ingest':               ("derive each item's state from the product's commits, branches and pull requests", RECORD, None),
    'import-items':         ("import a product's existing goals, specs and decisions as items", RECORD, None),
    'groom':                ("type the filed notes into cards and write the day's questions", RECORD, None),
    'stale':                ('list the items that have sat in one stage longer than it is allowed to take', None, None),
    'file-bugs':            ('file or bump a Bug for every failure the factory has now seen twice', RECORD, None),
    'rules':                ('check the product against its written rules and print every violation', None, None),
    'evidence':             ("print what the product's commits and pull requests say about each item", None, None),
    'harvest':              ('land the worker branches that are finished and close what they finished', RECORD, None),
    'pr-hygiene':           ('sort every open pull request into the lane that can finish it', None, None),
    'doctor':               ("check this product's install and print one row per check", None, None),
    'reserve':              ('claim the next free number in a declared sequence', RECORD, None),
    'readme':               ("check or refresh the generated parts of the product's README", REPO, '--refresh'),
    'tick':                 ('run one turn of the factory and print what each step did', RECORD, None),
    'watch':                ("follow the factory's turns as they land", None, None),
    'init':                 ('adopt a product: write its config, its record and its first roadmap', HOST, None),
    'schema-migrate':       ('bring the record up to the current schema', RECORD, None),
    'upgrade':              ("reinstall the factory at the current release and check every product's schema", HOST, None),
    'hooks':                ("write the product's editor hook entries", REPO, None),
    'hook':                 ('run one named check the product declares', None, None),
    'console-permissions':  ("print or write the console's own allow and deny rules", HOST, '--write'),
    'scheduler':            ('render, install and read back the jobs that run the factory on a clock', HOST, 'install'),
    'next':                 ('print what the next turn would start, most urgent first', None, None),
    'workers':              ('run the worker pool: spawn a wave, read its health, its stalls and its quota', HOST, 'spawn'),
    'unpark':               ('release an item the pool is holding', RECORD, None),
    'park':                 ('hold an item or a branch until it is released', RECORD, None),
    'correct':              ('ask for one correction round on an item, with instructions', RECORD, None),
    'reset':                ("record that an item's landing claim was wrong, void it and start the item over", RECORD, None),
    'ruleset':              ("install, read or lift the host's trunk ruleset: required checks, no direct push", HOST, 'install'),
    'facts':                ('compare the landing fact with the old deciders over the ledger, offline', None, None),
    'brief':                ('print the brief a worker would be given, for the next row or for one item', None, None),
    'import-sessions':      ("import an earlier runner's session log into the factory's metrics", RECORD, None),
    'approvals':            ('print the approval matrix and the open holds, or resolve one', RECORD, 'resolve'),
    'redact':               ('scan for operator names and secrets before anything is committed', None, None),
    'roadmap':              ('print one row per Epic: what it is for and where it stands', None, None),
    'board':                ('print one row per Feature, grouped by Epic', None, None),
    'stories':              ('print one row per Story and the Tasks that carry it', None, None),
    'prod':                 ('print what production is running and what just shipped', None, None),
    'sessions':             ('print one row per worker session and how it ended', None, None),
    'tokens':               ('print input tokens per session, before and after a chosen day', None, None),
    'status':               ('print where the factory stands right now', None, None),
    'scorecard':            ('print the value shipped per week, what it cost, and the causes the loop filed', None, None),
    'release-readiness':    ('print every release criterion, whether it is met, and its evidence', None, None),
    'capacity':             ('print how many sessions and CI runs each product is using', None, None),
    'shadow-diff':          ("compare a product's shadow turn against a reference run", None, None),
    'plugin':               ('build or check the editor plugin generated from this surface', REPO, 'build'),
    'ci':                   ('run the CI pool: reconcile this host with what is declared, and read the queue', HOST, '--apply'),
    'net-probe':            ('probe how this host reaches the forge and log which layer fails', HOST, None),
    'cloud':                ("install or check the cloud lane's CI workflow", REPO, 'install'),
    'deploy':               ('record the sha a deploy target now runs', RECORD, None),
    'rehearse':             ('run the CLI and the hooks end to end on a record-shaped '
                              'snapshot before a version is offered', REPO, '--build'),
}

#: A name that was retired, and the name that replaced it (F-0084).
RENAMED = {
    'migrate': 'import-items',
    'backlog': 'board',
    'parity': 'stories',
}


def marker(effect, flag):
    """The ``[writes …]`` suffix for one row, or `''` when it only reads."""
    if effect is None:
        return ''
    return f" [writes {effect}]" if flag is None else f" [writes {effect} with {flag}]"


class SurfaceError(Exception):
    """Raised when the registered parser and the declared table disagree."""


def apply(sub):
    """Set every registered command's ``help`` from :data:`COMMANDS`, marker included.

    Raises ``SurfaceError`` naming any command registered but not declared, so a new command
    cannot reach ``--help`` without a description and an effect (F-0084).
    """
    for action in sub._choices_actions:
        row = COMMANDS.get(action.dest)
        if row is None:
            raise SurfaceError(f"{action.dest!r} is registered but not declared in asf.surface")
        text, effect, flag = row
        action.help = text + marker(effect, flag)
