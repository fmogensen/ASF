"""asf.workers.capability — what the installed runtime binary accepts.

`asf/workers/runtime.py:98-110`'s `build_command` is the whole of this factory's launch path;
this module tells it, and the doctor, which of the newer flags the installed binary actually
understands, by reading its own `--help` once and caching the answer by (resolved path, mtime,
size) — an upgrade under a running tick is picked up without a restart.

The parse is deliberately dull: every `--<name>` at the start of an option line, plus each
comma-separated alias on the same line (`--allowedTools, --allowed-tools` yields both), and never
a continuation line or the description text — a reworded help page never changes an answer.
`effort_levels` is the one exception: it reads `--effort`'s own parenthesised choices, whether
they land on the option line or its wrapped continuation, because a mechanical launch (§2.6)
needs to know which level is the *lowest* the installed version offers rather than assume a name.

Every failure — the binary is absent, exits non-zero, prints nothing, hangs past
`PROBE_TIMEOUT_S`, or raises an `OSError` — answers as if the binary carried nothing, never an
exception: a probe that raises would stop a wave.
"""
import os
import re
import shutil
import subprocess

#: How long the probe waits for `<binary> --help`. A binary that cannot answer in this is treated
#: as answering nothing — every new flag is skipped and the doctor row is red.
PROBE_TIMEOUT_S = 20

#: The binary probed when none is given. Named again here, rather than imported from
#: `asf.workers.runtime`, so this module stays a leaf `runtime` can import (Task 4).
DEFAULT_BINARY = 'claude'

#: `--effort`'s own choices, used only when its help entry carries no parenthesised list to
#: parse — a malformed page, not a missing flag.
_FALLBACK_EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')

_OPTION_LINE = re.compile(r'^\s*-')
_LONG_NAME = re.compile(r'--([A-Za-z][\w-]*)')

#: (resolved path, mtime, size) -> the binary's `--help` text, or ``None`` for a probe that
#: failed. A failure is cached too, so a binary that cannot answer is not re-run every call.
_CACHE = {}


def reset():
    """Clear the probe cache — for the tests."""
    _CACHE.clear()


def _spec_part(line):
    """The option-spec portion of a help line: everything before the first run of two or more
    spaces after its own leading indent, which is where the wrapped description starts."""
    stripped = line.lstrip()
    m = re.search(r'  +', stripped)
    return stripped[:m.start()] if m else stripped


def _entries(text):
    """``[(names, lines)]`` — one entry per option line, its continuation lines (the wrapped
    description) included, in the file's order. A blank line, or a fresh option line, ends the
    entry before it."""
    out = []
    current = None
    for line in text.split('\n'):
        if _OPTION_LINE.match(line):
            current = (_LONG_NAME.findall(_spec_part(line)), [line])
            out.append(current)
        elif current is not None and line.strip():
            current[1].append(line)
        else:
            current = None
    return out


def _resolve(binary, environ):
    if os.sep in binary or (os.altsep and os.altsep in binary):
        return binary if os.path.isfile(binary) else None
    path = (environ or {}).get('PATH', os.environ.get('PATH'))
    return shutil.which(binary, path=path)


def _cache_key(resolved):
    try:
        st = os.stat(resolved)
    except OSError:
        return None
    return (os.path.realpath(resolved), st.st_mtime, st.st_size)


def _fetch(binary, environ):
    """The cached `--help` text for ``binary`` (``None`` on any failure)."""
    resolved = _resolve(binary or DEFAULT_BINARY, environ)
    if resolved is None:
        return None
    key = _cache_key(resolved)
    if key is None:
        return None
    if key in _CACHE:
        return _CACHE[key]
    try:
        proc = subprocess.run([resolved, '--help'], capture_output=True, text=True,
                              timeout=PROBE_TIMEOUT_S, env=environ)
        text = proc.stdout if proc.returncode == 0 and proc.stdout else None
    except (OSError, subprocess.SubprocessError):
        text = None
    _CACHE[key] = text
    return text


def flags(binary=None, environ=None):
    """The long-option names ``<binary> --help`` prints (``{'agent', 'agents', 'effort', …}``),
    cached by (resolved path, mtime, size). ``set()`` when the binary is absent, fails, or times
    out — never an exception, because a probe that raises stops a wave."""
    text = _fetch(binary, environ)
    return {name for names, _ in _entries(text) for name in names} if text else set()


def supports(name, binary=None):
    """Whether the installed runtime carries ``--<name>``."""
    return name in flags(binary)


def missing(names, binary=None):
    """The names of ``names`` the installed runtime lacks, in order — the doctor's row (§2.12)."""
    have = flags(binary)
    return tuple(n for n in names if n not in have)


def effort_levels(binary=None, environ=None):
    """The level names ``--effort`` accepts, in the order its help text lists them: the first
    parenthesised comma list in its whole entry (option line and continuations together), or
    :data:`_FALLBACK_EFFORTS` when the entry carries no such list. ``()`` when the runtime has no
    ``--effort`` at all."""
    text = _fetch(binary, environ)
    if not text:
        return ()
    entry = next((lines for names, lines in _entries(text) if 'effort' in names), None)
    if entry is None:
        return ()
    block = ' '.join(line.strip() for line in entry)
    match = re.search(r'\(([^()]+)\)', block)
    if not match:
        return _FALLBACK_EFFORTS
    return tuple(s.strip() for s in match.group(1).split(',') if s.strip())
