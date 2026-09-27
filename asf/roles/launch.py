"""asf.roles.launch — what a role is launched with.

`asf/workers/runtime.py:98-110`'s `build_command` has, until this Feature, launched every role
the same way: `bypassPermissions`, one shared settings file, a model label, nothing else. This
module is the table that ends that — the one place the launcher, the doctor and `asf roles
launch` all read for a role's permission mode, its effort, its tools and the paths it may write.

Imports :mod:`asf.roles.roles` and the stdlib at module level, nothing else — `asf.env` and
`asf.workers.capability` are imported locally, inside the one function apiece that needs them, so
this module stays a leaf the launcher, the doctor and the table view can all read alike.
"""
import dataclasses
import json
import os

from asf.roles import roles

#: The runtime's own six permission modes and five effort levels (P5, re-read against the
#: installed binary's own ``--help``) — written down so :data:`TABLE` can be validated against
#: them rather than typo'd row by row.
PERMISSION_MODES = ('acceptEdits', 'auto', 'bypassPermissions', 'manual', 'dontAsk', 'plan')
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')

READ_TOOLS = ('Read', 'Grep', 'Glob')
#: What a role that writes exactly one named file is denied — the editing tools, never `Write`
#: itself (PD4): the reviewer and the prober each write one output file under a hook-enforced
#: path, and denying them `Write` outright would end the lane that writes it (T5's first clause).
EDIT_TOOLS = ('Edit', 'MultiEdit', 'NotebookEdit')
#: The set whose *path argument* `roleguard` checks — every tool that can put text on disk.
WRITE_TOOLS = ('Write',) + EDIT_TOOLS


@dataclasses.dataclass(frozen=True)
class Launch:
    """What a role is launched with — everything the role file is forbidden to say (D1)."""
    role: str                 # the role file's stem, e.g. 'asf-reviewer'
    permission_mode: str      # one of PERMISSION_MODES
    effort: str               # one of EFFORTS, '' for the runtime's own default
    tools: tuple = ()         # the built-in tools the session gets; () is every tool
    deny_tools: tuple = ()    # tools removed even when `tools` is empty
    writes: tuple = ()        # the path patterns the role may write; () is 'its worktree'
    connections: tuple = ()   # external tool connections; () is none, and none is the default
    restricted: bool = False  # the runtime's own --restricted (the prober alone, §1.2)


#: The card's part 2 and part 5, as one table. `effort` is a level name, never a model. Every
#: role in `roles.roles_dir()` has a row here, and no row names a role that is not there (T1).
TABLE = {
    'asf-reviewer':      Launch('asf-reviewer', 'plan', 'high',
                                deny_tools=EDIT_TOOLS, writes=('{reviews_dir}/**',)),
    'asf-prober':        Launch('asf-prober', 'plan', 'low', tools=READ_TOOLS + ('Write',),
                                writes=('{reports_dir}/**',), restricted=True),
    'asf-coder':         Launch('asf-coder', 'acceptEdits', 'high'),
    'asf-fixer':         Launch('asf-fixer', 'acceptEdits', 'high'),
    'asf-diagnostician': Launch('asf-diagnostician', 'acceptEdits', 'high'),
    'asf-writer':        Launch('asf-writer', 'acceptEdits', 'high',
                                writes=('{specs_dir}/**', '{plans_dir}/**', '<record>/**')),
    'asf-interrogator':  Launch('asf-interrogator', 'acceptEdits', 'high', writes=('<record>/**',)),
    'asf-harvester':     Launch('asf-harvester', 'acceptEdits', 'low'),
    'asf-builder':       Launch('asf-builder', 'acceptEdits', 'high'),
    'asf-locator':       Launch('asf-locator', 'plan', 'low', tools=READ_TOOLS, writes=()),
    'asf-documenter':    Launch('asf-documenter', 'acceptEdits', 'low'),
    'asf-security':      Launch('asf-security', 'plan', 'high', deny_tools=WRITE_TOOLS),
}

#: Kinds with a right answer and no judgement in them — the same three that already launch on
#: the `cheap` model label (`asf/briefs/build.py:79-81`).
MECHANICAL = ('rebase', 'close', 'groom-clerk')

#: The product override key a `conventions.roles.<role>` block may set — nothing else, because
#: widening a boundary (`tools`, `writes`, `connections`) is an operator action through the
#: amendable set (`asf/amendable.py:53`), not a product setting.
_OVERRIDABLE = frozenset(('effort', 'permission_mode'))


def resolve_writes(launch, product):
    """``launch.writes`` with the product's own convention placeholders filled in:
    ``{reviews_dir}``, ``{specs_dir}``, ``{plans_dir}`` and ``{reports_dir}`` from
    ``product.conventions``, ``<record>`` from ``product.backlog_dir``. No literal path is ever
    written in this module — the placeholder form is the only form the conventions lint lets
    through."""
    conv = product.conventions
    mapping = {'reviews_dir': conv.reviews_dir, 'specs_dir': conv.specs_dir,
              'plans_dir': conv.plans_dir, 'reports_dir': conv.reports_dir}
    record = product.backlog_dir or ''
    return tuple(pattern.replace('<record>', record).format(**mapping)
                for pattern in launch.writes)


def launch_for(kind, product=None):
    """The role a brief ``kind`` runs under (`roles.for_kind`), its :data:`TABLE` row, with the
    product's own ``conventions.roles.<role>`` override applied to ``effort`` and
    ``permission_mode`` only. An override of any other key — ``tools``, ``writes``,
    ``connections`` — raises, naming the key and the amendable set."""
    role = roles.for_kind(kind)
    launch = TABLE[role]
    override = product.conventions.roles.get(role) if product is not None else None
    if not isinstance(override, dict) or not override:
        return launch
    bad = sorted(set(override) - _OVERRIDABLE)
    if bad:
        raise ValueError(
            f"conventions.roles.{role}.{bad[0]} cannot be set from a product's config — "
            "widening tools, writes or connections is an operator action through the amendable "
            "set (asf/amendable.py:53), not a product setting")
    return dataclasses.replace(launch, **override)


def effort_for(kind, binary=None, caps=None):
    """The effort level ``kind`` launches at (§2.6): for a :data:`MECHANICAL` kind, the lowest
    level the installed runtime offers (``''`` when it has no ``--effort`` at all); for every
    other kind, the role's own from :data:`TABLE`. ``caps`` — the levels
    ``capability.effort_levels`` would report — is injectable so every test runs without a
    binary."""
    role = roles.for_kind(kind)
    launch = TABLE[role]
    if kind not in MECHANICAL:
        return launch.effort
    if caps is None:
        from asf.workers import capability
        caps = capability.effort_levels(binary)
    order = [e for e in EFFORTS if e in caps]
    return order[0] if order else ''


def agents_path(product, job):
    """``<state>/<product>/agents/<job>.json`` — a sibling of the briefs directory (PD14),
    removed with the rest of the job's state because nothing under it is reaped on its own."""
    from asf import env
    d = os.path.join(env.state_dir(product), 'agents')
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f'{job}.json')


def write_agents_file(product, job, launch):
    """Renders ``launch``'s role into the one-key JSON object §2.4 describes, at
    :func:`agents_path`, and returns that path. A role whose file fails :func:`roles.validate`
    raises before anything is written, naming the role and the problem — same role text plus
    same row always gives the same bytes, so a diff in the file is a diff in one of the two."""
    role = roles.load(launch.role)
    problems = roles.validate(role)
    if problems:
        raise roles.RoleError(f'{launch.role}: {"; ".join(problems)}')
    tools = tuple(t for t in launch.tools if t not in launch.deny_tools)
    obj = {launch.role: {
        'description': role.purpose,
        'prompt': roles.block(role, roles.MAX_LINES),
        'tools': list(tools),
    }}
    path = agents_path(product, job)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=2, sort_keys=True)
    return path
