"""asf.readonly — the read-only grant: a Bash command the approvals hook allows outright.

A factory session runs headless: every command the runtime's own permission check would put to a
person is a *denial* — a wasted turn, and often a ``NEEDS OPERATOR`` line for plumbing. The job
logs counted about six per session, and almost none were writes: the product's own declared
check script (``bash <its check script>``), ``cd <worktree> && git log``, ``git merge-base … ; echo $?``,
``diff <(git show …) <(sed -n …)``. The runtime refuses those shapes (a compound after ``cd``, a
``$?``, a substitution) whatever its allow rules say; a ``PreToolUse`` hook that answers
``allow`` is the one switch it honours for them.

:func:`grant` is that answer's predicate. It parses the command with a deliberately small shell
grammar and grants only when **every** simple command in it — across ``&&``, ``||``, ``;``,
``|``, lines, ``( … )`` subshells, ``$( … )`` and ``<( … )`` — is one of:

* a reading program (:data:`READ_PROGRAMS`, ``find`` without an action, ``sed -n`` printing
  lines, ``sort`` without ``-o``, ``uniq`` with at most one file);
* ``git`` with a reading subcommand (:func:`_git_reads`) — ``branch``/``tag``/``config``/
  ``remote`` only in their listing forms, ``fetch`` without a ``src:dst`` refspec;
* ``cd`` into an allowed root;
* one of the product's declared check or test commands (:func:`declared_commands`), with or
  without more arguments, its script named by any path that resolves to the same file.

Anything the grammar does not know — a heredoc, a backtick, ``$VAR``, a loop, ``&``, a write
redirection other than to ``/dev/null`` — is not granted: the call goes on to the runtime's own
rules exactly as before. Every path a granted command names (absolute, ``~``, a ``..`` segment,
a ``cd`` target, a redirection source) must resolve inside an allowed root — the session's own
checkout and the directories it was granted (``ASF_READ_ROOTS``) — so the grant never reads what
the runtime's directory sandbox would not have let it read. Mutating commands are untouched: this
module only ever says *allow*, never *refuse*.
"""
import os
import re

#: Programs that only read or print, whatever their arguments.
READ_PROGRAMS = frozenset((
    'ls', 'cat', 'head', 'tail', 'grep', 'egrep', 'fgrep', 'rg', 'wc', 'pwd', 'echo', 'printf',
    'true', 'false', 'test', '[', 'basename', 'dirname', 'realpath', 'readlink', 'which', 'stat',
    'file', 'du', 'date', 'cut', 'tr', 'diff', 'cmp', 'comm', 'nl', 'tac', 'rev', 'column', 'jq',
    'tree', 'type',
))

#: ``find``'s primaries that run or write something.
FIND_ACTIONS = frozenset(('-exec', '-execdir', '-ok', '-okdir', '-delete', '-fprint', '-fprint0',
                          '-fprintf', '-fls'))

#: ``git`` subcommands that only read, whatever their arguments (``--output`` aside).
GIT_READS = frozenset((
    'log', 'show', 'diff', 'status', 'blame', 'ls-files', 'ls-tree', 'rev-parse', 'rev-list',
    'merge-base', 'cat-file', 'shortlog', 'describe', 'grep', 'name-rev', 'show-ref',
    'for-each-ref', 'whatchanged', 'count-objects', 'cherry', 'range-diff', 'diff-tree',
    'diff-index', 'diff-files', 'check-ignore', 'check-attr', 'show-branch', 'ls-remote',
))

#: Virtualization/container tools the grant must never allow — not as a bare command, and not
#: even when a product declares one as a check or test command: the operator's own settings deny
#: these outright, and the grant must never widen past that.
NEVER_GRANT = frozenset(('docker', 'colima', 'multipass', 'limactl'))


def _is_vm_tool(words):
    if not words:
        return False
    if os.path.basename(words[0]) in NEVER_GRANT:
        return True
    return os.path.basename(words[0]) == 'open' and any(
        'docker' in w.lower() for w in words[1:])


#: Leading ``VAR=value`` words a granted command may carry: display settings only. Anything else
#: (``GIT_EXTERNAL_DIFF``, ``PATH``) can make a reading program run something.
SAFE_ASSIGNMENTS = frozenset(('GIT_PAGER', 'PAGER', 'NO_COLOR', 'FORCE_COLOR', 'LC_ALL', 'LANG',
                              'TERM', 'COLUMNS', 'GIT_TERMINAL_PROMPT'))

_SED_RANGE = r'(?:\d+|\$|/[^/]*/)(?:\s*,\s*(?:\d+|\$|\+\d+|/[^/]*/))?'
_SED_PRINT = re.compile(rf'^\s*{_SED_RANGE}\s*p\s*(?:;\s*{_SED_RANGE}\s*p\s*)*;?\s*$')
_ASSIGNMENT = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)=')
_INTERPRETERS = ('bash', 'sh', 'zsh')


class Unsupported(Exception):
    """The command uses shell the grammar does not read: never granted."""


# ---- the grammar --------------------------------------------------------------------------

_OPS = ('&&', '||', '|&', ';;', '>>', '&>>', '&>', '>|', '<<<', '<<', ';', '|', '&', '>', '<',
        '(', ')', '\n')


def _subst_end(text, i):
    """The index just past the ``)`` that closes the ``(`` at ``text[i-1]``, quotes honoured."""
    depth, q = 1, None
    while i < len(text):
        c = text[i]
        if q:
            if c == '\\' and q == '"':
                i += 2
                continue
            if c == q:
                q = None
        elif c in '\'"':
            q = c
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise Unsupported('unclosed substitution')


def tokenize(text):
    """``[(kind, value)]``: ``('word', str)``, ``('op', str)``, ``('sub', inner text)`` for a
    whole-word ``$( … )`` / ``<( … )``. Words carry the substitutions found inside them in
    ``subs``: ``('word', (str, [inner, …]))``. Raises :class:`Unsupported`."""
    out, i, n = [], 0, len(text)
    word, subs, in_word = [], [], False

    def flush():
        nonlocal word, subs, in_word
        if in_word:
            out.append(('word', (''.join(word), subs)))
        word, subs, in_word = [], [], False

    while i < n:
        c = text[i]
        if c in ' \t':
            flush()
            i += 1
            continue
        if c == '#' and not in_word:
            while i < n and text[i] != '\n':
                i += 1
            continue
        if c == '\\':
            if i + 1 < n and text[i + 1] == '\n':
                i += 2
                continue
            if i + 1 >= n:
                raise Unsupported('trailing backslash')
            word.append(text[i + 1])
            in_word = True
            i += 2
            continue
        if c == "'":
            j = text.find("'", i + 1)
            if j < 0:
                raise Unsupported('unbalanced quote')
            word.append(text[i + 1:j])
            in_word = True
            i = j + 1
            continue
        if c == '"':
            i += 1
            in_word = True
            while True:
                if i >= n:
                    raise Unsupported('unbalanced quote')
                d = text[i]
                if d == '"':
                    i += 1
                    break
                if d == '\\' and i + 1 < n:
                    word.append(text[i + 1] if text[i + 1] in '"\\$`\n' else text[i:i + 2])
                    i += 2
                    continue
                if d == '`':
                    raise Unsupported('backtick')
                if d == '$':
                    i = _dollar(text, i, word, subs)
                    continue
                word.append(d)
                i += 1
            continue
        if c == '`':
            raise Unsupported('backtick')
        if c == '$':
            in_word = True
            i = _dollar(text, i, word, subs)
            continue
        if c == '<' and text.startswith('<(', i):
            j = _subst_end(text, i + 2)
            word.append('\0proc\0')
            subs.append(text[i + 2:j - 1])
            in_word = True
            i = j
            continue
        if c == '>' and text.startswith('>(', i):
            raise Unsupported('output process substitution')
        if c in ';&|<>()\n':
            # a digit word right before a redirection is its fd: `2>&1`, `2>/dev/null`
            if c in '<>' and in_word and not subs and ''.join(word).isdigit():
                word, in_word = [], False
            flush()
            for op in _OPS:
                if text.startswith(op, i):
                    out.append(('op', op))
                    i += len(op)
                    break
            # `>&1`, `>&2`: a duplication, not a file
            if out[-1][1] in ('>', '<') and i < n and text[i] == '&':
                j = i + 1
                while j < n and (text[j].isdigit() or text[j] == '-'):
                    j += 1
                out[-1] = ('op', 'dup')
                i = j
            continue
        if c in '{}' and not in_word and (i + 1 >= n or text[i + 1] in ' \t\n;'):
            raise Unsupported('brace group')
        word.append(c)
        in_word = True
        i += 1
    flush()
    return out


def _dollar(text, i, word, subs):
    """Read the ``$…`` at ``text[i]`` into ``word``; return the index after it. Only ``$?`` and
    ``$( … )`` are read — any other expansion is a value nobody can check before it runs."""
    if text.startswith('$?', i):
        word.append('0')
        return i + 2
    if text.startswith('$((', i):
        raise Unsupported('arithmetic expansion')
    if text.startswith('$(', i):
        j = _subst_end(text, i + 2)
        subs.append(text[i + 2:j - 1])
        word.append('\0sub\0')
        return j
    raise Unsupported('parameter expansion')


# ---- the judge ------------------------------------------------------------------------------

class _Ctx:
    def __init__(self, roots, declared):
        self.roots = [os.path.realpath(r) for r in roots if r]
        self.declared = declared


def _inside(ctx, path):
    real = os.path.realpath(path)
    if real == os.devnull:
        return True
    return any(real == r or real.startswith(r.rstrip(os.sep) + os.sep) for r in ctx.roots)


def _pathish(word):
    return (word.startswith(('/', '~')) or word == '..' or word.startswith('../')
            or '/../' in word or word.endswith('/..'))


def _resolve(cwd, word):
    return os.path.join(cwd, os.path.expanduser(word))


def _paths_ok(ctx, cwd, words):
    for w in words:
        value = w.split('=', 1)[1] if w.startswith('-') and '=' in w else w
        if '\0' in value:
            continue
        if _pathish(value) and not _inside(ctx, _resolve(cwd, value)):
            return False
    return True


def _positional(words):
    return [w for w in words if not w.startswith('-')]


def _git_reads(words):
    """True when ``git <words>`` (globals already consumed) only reads."""
    if not words:
        return False
    sub, rest = words[0], words[1:]
    if any(w.startswith('--output') or w.startswith('--upload-pack') or w.startswith('--exec')
           for w in rest):
        return False
    if sub in GIT_READS:
        return True
    if sub == 'branch':
        if any(w in ('-d', '-D', '-m', '-M', '-c', '-C', '-f', '-u', '--delete', '--move',
                     '--copy', '--force', '--edit-description', '--unset-upstream')
               or w.startswith('--set-upstream') for w in rest):
            return False
        valued = ('--contains', '--no-contains', '--merged', '--no-merged', '--points-at',
                  '--sort', '--format')
        listing = any(w in ('-l', '--list') for w in rest)
        free, skip = [], False
        for w in rest:
            if skip:
                skip = False
            elif w in valued:
                skip = True
            elif not w.startswith('-'):
                free.append(w)
        return listing or not free
    if sub == 'tag':
        return not rest or any(w in ('-l', '--list') for w in rest) and not any(
            w in ('-d', '--delete', '-a', '-s', '-f', '-m', '-F', '--annotate', '--sign')
            for w in rest)
    if sub == 'remote':
        return not rest or rest[0] in ('-v', '--verbose', 'show', 'get-url')
    if sub == 'config':
        reads = ('--get', '--get-all', '--get-regexp', '--list', '-l', '--get-urlmatch')
        writes = ('--add', '--unset', '--unset-all', '--replace-all', '--rename-section',
                  '--remove-section', '--edit', '-e')
        return any(w in reads for w in rest) and not any(w in writes for w in rest)
    if sub in ('reflog', 'worktree', 'stash'):
        return False if sub == 'stash' else (
            not rest or rest[0] in (('show',) if sub == 'reflog' else ('list',)))
    if sub == 'fetch':
        return not any(':' in w for w in _positional(rest))
    return False


def _strip_wrappers(words):
    """``words`` past the wrappers a granted command may carry: safe ``VAR=value``s, ``env``
    with ``-u NAME`` and safe assignments, ``timeout <duration>``, ``command``, ``time``.
    None when a wrapper is not one of those."""
    while words:
        m = _ASSIGNMENT.match(words[0])
        if m:
            if m.group(1) not in SAFE_ASSIGNMENTS:
                return None
            words = words[1:]
            continue
        head = words[0]
        if head == 'env':
            words = words[1:]
            while words and (words[0] == '-u' or _ASSIGNMENT.match(words[0])):
                if words[0] == '-u':
                    words = words[2:]
                elif _ASSIGNMENT.match(words[0]).group(1) in SAFE_ASSIGNMENTS:
                    words = words[1:]
                else:
                    return None
            continue
        if head == 'timeout':
            words = words[1:]
            while words and words[0].startswith('-'):
                words = words[2:] if words[0] in ('-s', '-k', '--signal', '--kill-after') \
                    else words[1:]
            words = words[1:]          # the duration
            continue
        if head in ('command', 'time', 'nice'):
            words = words[1:]
            continue
        break
    return words


def _canon(words, cwd, root):
    """``words`` with an interpreter before a script dropped and the script made absolute — so
    ``bash <script>``, ``sh ./<script>``, a bare relative ``<script>`` and its absolute path all
    compare equal. ``root`` resolves a declared command; ``cwd`` a session's."""
    words = list(words)
    if len(words) >= 2 and os.path.basename(words[0]) in _INTERPRETERS \
            and not words[1].startswith('-'):
        words = words[1:]
    if words and ('/' in words[0]) and '\0' not in words[0]:
        words[0] = os.path.realpath(os.path.join(cwd or root, os.path.expanduser(words[0])))
    return words


def _declared(ctx, words, cwd):
    for root, form in ctx.declared:
        want = _canon(form, root, root)
        have = _canon(words, cwd, root)
        if want and have[:len(want)] == want:
            return True
    return False


def _simple_ok(ctx, words, cwd):
    """True when the simple command ``words`` (redirections already gone) only reads."""
    words = _strip_wrappers(words)
    if words is None:
        return False
    if not words:
        return True
    if _is_vm_tool(words):
        return False
    if not _paths_ok(ctx, cwd, words[1:]):
        return False
    if _declared(ctx, words, cwd):
        return True
    prog = words[0]
    if '/' in prog:
        return False
    rest = words[1:]
    if prog in READ_PROGRAMS:
        return not (prog == 'printf' and '-v' in rest)
    if prog == 'find':
        return not any(w in FIND_ACTIONS for w in rest)
    if prog == 'sort':
        return not any(w == '-o' or w.startswith('--output') or (w.startswith('-o') and
                                                                  not w.startswith('--'))
                       for w in rest)
    if prog == 'uniq':
        return len(_positional(rest)) <= 1
    if prog == 'sed':
        if any(w.startswith('-i') or w.startswith('--in-place') for w in rest):
            return False
        if '-n' not in rest and '--quiet' not in rest:
            return False
        scripts, i = [], 0
        while i < len(rest):
            if rest[i] in ('-e', '--expression'):
                scripts.append(rest[i + 1] if i + 1 < len(rest) else '')
                i += 2
                continue
            i += 1
        if not scripts:
            pos = _positional(rest)
            scripts = pos[:1]
        return bool(scripts) and all(_SED_PRINT.match(s) for s in scripts)
    if prog == 'git':
        i = 0
        while i < len(rest) and rest[i].startswith('-'):
            if rest[i] == '-C':
                if i + 1 >= len(rest) or not _inside(ctx, _resolve(cwd, rest[i + 1])):
                    return False
                i += 2
            elif rest[i] in ('--no-pager', '--no-optional-locks', '-P', '--literal-pathspecs'):
                i += 1
            else:
                return False                 # -c, --git-dir, --exec-path, …: not judged
        return _git_reads(rest[i:])
    return False


def _judge(ctx, tokens, cwd):
    """True when every simple command of ``tokens`` only reads; ``cwd`` follows each ``cd``,
    restored at the end of a subshell."""
    stack = [cwd]
    words, redirect = [], None
    pending_subs = []

    def finish():
        nonlocal words
        if not words:
            return True
        argv = words
        words = []
        if argv[0] == 'cd':
            target = argv[1] if len(argv) > 1 else '~'
            if target == '-' or '\0' in target:
                return False
            dest = _resolve(stack[-1], target)
            if not _inside(ctx, dest):
                return False
            stack[-1] = os.path.realpath(dest)
            return True
        return _simple_ok(ctx, argv, stack[-1])

    for kind, value in tokens:
        if kind == 'word':
            text, subs = value
            pending_subs.extend(subs)
            if redirect == 'out':
                if text != os.devnull:
                    return False
                redirect = None
                continue
            if redirect == 'in':
                if '\0' not in text and not _inside(ctx, _resolve(stack[-1], text)):
                    return False
                redirect = None
                continue
            words.append(text)
            continue
        if redirect:
            return False                      # a redirection with no target
        if value in ('>', '>>', '&>', '&>>', '>|'):
            redirect = 'out'
        elif value == '<':
            redirect = 'in'
        elif value == 'dup':
            pass
        elif value in ('<<', '<<<', '&', ';;'):
            return False
        elif value == '(':
            if words:
                return False                  # a function definition or worse
            stack.append(stack[-1])
        elif value == ')':
            if not finish() or len(stack) < 2:
                return False
            stack.pop()
        else:                                 # && || ; | |& newline
            if not finish():
                return False
    if redirect or not finish() or len(stack) != 1:
        return False
    return all(grant_tokens(ctx, sub, cwd) for sub in pending_subs)


def grant_tokens(ctx, text, cwd):
    try:
        tokens = tokenize(text)
    except Unsupported:
        return False
    return _judge(ctx, tokens, cwd)


def declared_commands(product):
    """The product's own check and test commands as ``argv`` lists: ``conventions.
    check_commands``, the code ``pre_push_check``, every doc-kind pre-push step (placeholders
    stop the comparison there) and ``test_command``. Each compound is split into its simple
    commands; a command the grammar cannot read is skipped."""
    if product is None:
        return []
    from asf import approvals
    conv = getattr(product, 'conventions', None)
    raw = []
    checks = conv.get('check_commands') if conv is not None else None
    if isinstance(checks, list):
        raw += [c for c in checks if isinstance(c, str)]
    code = approvals.pre_push_check(product)
    if code:
        raw.append(code)
    for kind in approvals.PRE_PUSH_DOC_KEYS:
        raw += [run for run, _note in approvals.pre_push_steps(product, kind)]
    test = getattr(conv, 'test_command', None) if conv is not None else None
    if isinstance(test, str) and test.strip():
        raw.append(test)
    out = []
    for command in dict.fromkeys(raw):
        try:
            tokens = tokenize(command)
        except Unsupported:
            continue
        argv = []
        for kind, value in tokens + [('op', ';')]:
            if kind == 'word':
                argv.append(value[0])
            elif argv:
                argv = [w for w in argv if not w.startswith('<')] if argv else argv
                cut = next((i for i, w in enumerate(argv) if re.fullmatch(r'<[^<>\s]+>', w)),
                           len(argv))
                if argv[:cut] and argv[:cut] not in out:
                    out.append(argv[:cut])
                argv = []
    return out


def enabled(product):
    """``conventions.read_only_allow`` (default true): whether the hook grants read-only
    commands for this product's sessions."""
    conv = getattr(product, 'conventions', None) if product is not None else None
    value = conv.get('read_only_allow') if conv is not None else None
    return value is not False


def roots(cwd, environ):
    """The directories a granted command may read: the git checkout ``cwd`` sits in (else
    ``cwd``) and each of ``ASF_READ_ROOTS`` (``os.pathsep``-separated)."""
    out = []
    top = _toplevel(cwd)
    out.append(top or cwd)
    for d in (environ.get('ASF_READ_ROOTS') or '').split(os.pathsep):
        if d.strip():
            out.append(os.path.expanduser(d.strip()))
    return out


def _toplevel(cwd):
    """The worktree root above ``cwd``: the nearest directory holding a ``.git`` entry."""
    d = os.path.realpath(cwd or '.')
    while True:
        if os.path.exists(os.path.join(d, '.git')):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def grant(command, cwd, read_roots, declared=()):
    """True when the Bash ``command``, run in ``cwd``, only reads inside ``read_roots`` or runs
    a ``declared`` (``[(root, argv)]``) check — the hook may answer *allow*."""
    if not command or not command.strip():
        return False
    ctx = _Ctx(read_roots, list(declared))
    return grant_tokens(ctx, command, os.path.realpath(cwd))


def grant_for(product, command, cwd, environ):
    """:func:`grant` with the product's roots and declared commands — what the hook calls."""
    if not enabled(product):
        return False
    rs = roots(cwd, environ or {})
    root = rs[0]
    return grant(command, cwd, rs, [(root, argv) for argv in declared_commands(product)])
