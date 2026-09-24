"""asf.roles.roles — load, validate and render the role files.

A role is ``asf/roles/<name>.md``: a two-key frontmatter and five ``##`` sections in order (see
:mod:`asf.roles`). This module is the whole of what reads one: :func:`load` and :func:`load_all`
parse it with :mod:`asf.record.frontmatter` (there is no second parser), :func:`validate` returns
one problem string per breach of the nine refusals below, :func:`for_kind` reads the one binding
table, and :func:`block` renders the text a brief carries.

The refusals, in the order :func:`validate` reports them::

    1  frontmatter missing ``name`` or ``purpose``, or ``name`` not the file stem
    2  a frontmatter key beyond those two (this is where ``model:`` would arrive)
    3  a missing section, a section out of order, or a heading that is not one of ``SECTIONS``
    4  the file over ``MAX_LINES``
    5  fewer than ``DOCTRINE_MIN`` or more than ``DOCTRINE_MAX`` doctrine bullets
    6  a doctrine bullet citing no incident (no match for ``INCIDENT``)
    7  ``## Economy`` over ``ECONOMY_MAX_LINES``
    8  a banned word anywhere in the file, frontmatter included, case-insensitive
    9  ``## Output`` carrying a REPORT contract — the envelope belongs to the brief's tail

Nothing here reads the operator's config, and nothing here chooses a role at run time: the binding
is data, and an invalid role dir is a red gate and a red doctor row, never a dead wave.
"""
import dataclasses
import hashlib
import importlib
import os
import re

from asf.record import frontmatter

ROLES_DIR = os.path.dirname(os.path.abspath(__file__))

SECTIONS = ('Identity', 'Doctrine', 'Output', 'Economy', 'Boundaries')
KEYS = frozenset(('name', 'purpose'))
MAX_LINES = 60
DOCTRINE_MIN = 4
DOCTRINE_MAX = 8
INCIDENT = r'\b(?:[BDEFRST]-\d{4}|b\d{1,3})\b'
ECONOMY_MAX_LINES = 6
#: The words a role never says. :func:`forbidden` adds the two label constants of
#: ``asf.briefs.build``, which this module does not import at load time.
FORBIDDEN = ('model', 'backend', 'effort', 'tools')

#: Every brief kind → the role it runs under. Read twice: by the brief, and by the tests that
#: hold it equal to ``asf.briefs.build.KINDS``.
BINDINGS = {'spec': 'writer', 'plan': 'writer', 'reshape': 'writer',
            'review': 'reviewer', 'coder': 'coder',
            'fixer': 'fixer', 'correct': 'fixer', 'rebase': 'fixer',
            'fix-bug': 'diagnostician', 'close': 'harvester',
            'groom': 'interrogator', 'adjudicate': 'interrogator',
            'delivery-plan': 'writer', 'delivery-code': 'coder'}

#: Every role no kind binds, with the reason none does.
UNBOUND = {'prober': 'no phase yet — the production probe is a view, not a session',
           'security': 'no phase yet — the panel it belongs to is not in this epic',
           'documenter': 'no phase yet — the docs surface has no lane of its own'}

_HEADING = re.compile(r'^## +(.*?)\s*$')
_FENCE = re.compile(r'^\s*(?:```|~~~)')
_BULLET = re.compile(r'^\s*[-*] ')


class RoleError(Exception):
    """A role that cannot be read: no such file, or a file with no readable frontmatter."""


@dataclasses.dataclass
class Role:
    name: str
    purpose: str
    sections: dict     # 'Identity' -> the section's text, in file order
    text: str          # the body below the frontmatter, verbatim
    sha: str           # sha256 of `text`, first 12 hex
    lines: int         # lines in the whole file — the cap counts what is on disk
    path: str = ''
    raw: str = ''      # the whole file, frontmatter included: refusal 8 reads it
    keys: tuple = ()   # the frontmatter's keys, in file order
    headings: tuple = ()  # every ``## `` heading, in file order, duplicates kept


def roles_dir():
    """The directory the roles are read from: ``$ASF_ROLES_DIR`` when set (the gate and the tests
    point it at a broken one), else the package's own."""
    return os.environ.get('ASF_ROLES_DIR') or ROLES_DIR


def forbidden():
    """:data:`FORBIDDEN` plus the two label constants — ``asf.briefs.build`` is a module the
    ``asf.briefs`` package shadows with its ``build`` function, so it is asked for by name."""
    build = importlib.import_module('asf.briefs.build')
    return FORBIDDEN + (build.HEAVY, build.LIGHT)


def _split(text):
    """(lines before the first ``##``, [(heading, its lines including the heading line)]) — a
    ``##`` inside a fenced block is text, not a heading."""
    pre, chunks, fenced = [], [], False
    for line in text.split('\n'):
        if _FENCE.match(line):
            fenced = not fenced
        m = None if fenced else _HEADING.match(line)
        if m:
            chunks.append((m.group(1), [line]))
        elif chunks:
            chunks[-1][1].append(line)
        else:
            pre.append(line)
    return pre, chunks


def _trim(lines):
    lines = list(lines)
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    return lines


def _parse(path, raw):
    try:
        meta, body = frontmatter.parse(raw, path)
    except frontmatter.FrontmatterError as e:
        raise RoleError(f'{path}: {e}') from e
    _, chunks = _split(body)
    sections = {}
    for heading, lines in chunks:
        sections[heading] = '\n'.join(_trim(lines[1:]))
    return Role(name=str(meta.get('name') or ''), purpose=str(meta.get('purpose') or ''),
                sections=sections, text=body,
                sha=hashlib.sha256(body.encode('utf-8')).hexdigest()[:12],
                lines=len(raw.splitlines()), path=path, raw=raw, keys=tuple(meta),
                headings=tuple(h for h, _ in chunks))


def _read(path):
    try:
        with open(path, encoding='utf-8') as f:
            return f.read()
    except OSError as e:
        raise RoleError(f'{path}: {e.strerror or e}') from e


def load(name):
    """The role called ``name``, from ``roles_dir()/<name>.md``. RoleError when there is no such
    file or its frontmatter cannot be read."""
    if not name or os.path.basename(name) != name:
        raise RoleError(f'no role called {name!r}')
    path = os.path.join(roles_dir(), name + '.md')
    return _parse(path, _read(path))


def load_all():
    """``{name: Role}`` for every ``*.md`` in ``roles_dir()``, keyed by file stem, sorted."""
    directory = roles_dir()
    try:
        files = sorted(f for f in os.listdir(directory) if f.endswith('.md'))
    except OSError as e:
        raise RoleError(f'{directory}: {e.strerror or e}') from e
    return {f[:-3]: _parse(os.path.join(directory, f), _read(os.path.join(directory, f)))
            for f in files}


def _stem(role):
    return os.path.basename(role.path)[:-3] if role.path.endswith('.md') else role.name


def _bullets(text):
    """The doctrine's bullets, each with its continuation lines, as one string apiece."""
    out = []
    for line in text.split('\n'):
        if _BULLET.match(line):
            out.append(line)
        elif out and line.strip():
            out[-1] += '\n' + line
    return out


def _fenced(text):
    """The text of every fenced block in ``text``."""
    blocks, cur = [], None
    for line in text.split('\n'):
        if _FENCE.match(line):
            if cur is None:
                cur = []
            else:
                blocks.append('\n'.join(cur))
                cur = None
        elif cur is not None:
            cur.append(line)
    if cur is not None:
        blocks.append('\n'.join(cur))
    return blocks


def validate(role):
    """One problem string per breach of the nine refusals, in their order; ``[]`` is a valid
    role. Each string opens with the name of the refusal, so a problem names itself."""
    problems = []
    missing = sorted(KEYS - set(role.keys))
    if missing:
        problems.append(f"frontmatter: missing {', '.join(missing)}")
    elif role.name != _stem(role):
        problems.append(f'frontmatter: name {role.name!r} does not match the file stem '
                        f'{_stem(role)!r}')
    unknown = [k for k in role.keys if k not in KEYS]
    if unknown:
        problems.append(f"frontmatter: unknown key {', '.join(unknown)} — only name and "
                        f"purpose are allowed")
    if list(role.headings) != list(SECTIONS):
        absent = [s for s in SECTIONS if s not in role.headings]
        extra = [h for h in role.headings if h not in SECTIONS]
        why = ('missing ' + ', '.join(absent) if absent else
               'unknown heading ' + ', '.join(extra) if extra else 'out of order')
        problems.append(f"sections: {why} — expected {', '.join(SECTIONS)}, in that order")
    if role.lines > MAX_LINES:
        problems.append(f'lines: {role.lines} lines, over the cap of {MAX_LINES}')
    doctrine = role.sections.get('Doctrine')
    if doctrine is not None:
        bullets = _bullets(doctrine)
        if not DOCTRINE_MIN <= len(bullets) <= DOCTRINE_MAX:
            problems.append(f'doctrine: {len(bullets)} bullets, must be {DOCTRINE_MIN} to '
                            f'{DOCTRINE_MAX}')
        for bullet in bullets:
            if not re.search(INCIDENT, bullet):
                problems.append(f'incident: doctrine bullet cites no incident: '
                                f'{bullet.split(chr(10))[0].strip()[:60]}')
    economy = role.sections.get('Economy')
    if economy is not None:
        n = len([l for l in economy.split('\n') if l.strip()])
        if n > ECONOMY_MAX_LINES:
            problems.append(f'economy: {n} lines, over the cap of {ECONOMY_MAX_LINES}')
    lowered = role.raw.lower()
    hits = [w for w in forbidden() if w.lower() in lowered]
    if hits:
        problems.append(f"forbidden: names {', '.join(hits)} — a role carries identity, not "
                        f"cost or access")
    output = role.sections.get('Output')
    if output is not None:
        envelope = any(l.strip() == 'REPORT' for l in output.split('\n'))
        if envelope or any('status:' in b for b in _fenced(output)):
            problems.append('output: restates the REPORT contract — the envelope belongs to '
                            "the brief's tail")
    return problems


def for_kind(kind):
    """The role a brief kind runs under. RoleError on a kind the table does not know."""
    try:
        return BINDINGS[kind]
    except KeyError:
        raise RoleError(f'no role is bound to kind {kind!r} '
                        f"(have: {', '.join(sorted(BINDINGS))})") from None


def block(role, cap):
    """The role's text as the brief carries it: ``## Who you are — <name>`` and the body, cut to
    ``cap`` lines by whole sections, last section first, and ending with ``… (role <name>
    truncated at <cap> lines)`` when it cut, so a short brief is never taken for a short
    doctrine (the discipline of ``asf.briefs.preamble.fit``, over sections)."""
    pre, chunks = _split(role.text)
    head = [f'## Who you are — {role.name}', ''] + (_trim(pre) + [''] if _trim(pre) else [])
    kept = [_trim(lines) for _, lines in chunks]

    def render(parts):
        out = list(head)
        for lines in parts:
            out += lines + ['']
        return out

    if len(render(kept)) - 1 <= cap:
        return '\n'.join(render(kept)).rstrip('\n')
    while kept and len(render(kept)) + 1 > cap:
        kept.pop()
    return '\n'.join(render(kept) + [f'… (role {role.name} truncated at {cap} lines)'])
