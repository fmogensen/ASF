"""asf.evidence.checked — the one owner of the operator's checked list.

A ``manual`` product's gate (``asf.record.ingest._in_prod``) does not trust a deploy alone: a
person has to have opened production and said so. This module is the list that says it — where
it lives, how a line is written and read, and the string that names a landing still waiting on
one. Nothing in this module writes a tick on ASF's own behalf (D2); the only caller that calls
:func:`add` is a person's own ``asf checked`` invocation.

Two homes. ``path(product)`` is the per-product file, under the product's own state directory —
the shape :func:`asf.approvals.ledger_path` already uses. :data:`LEGACY_PATH` is the one global
file ASF read before products had state directories of their own; it is read and never written,
so a hand-ticked entry there is never lost and never duplicated.

Imports ``os``, ``re``, ``time`` and :mod:`asf.env` only — nothing from
:mod:`asf.evidence.evidence`, so that module can import this one without a cycle.
"""
import os
import re
import time

from asf import env

#: The one global checked file ASF read before per-product state directories existed.
#: read, never written
LEGACY_PATH = os.path.join(env.ASF_HOME, 'checked.txt')

#: Written by `add` the first time it creates a product's file, and never otherwise.
HEADER = '# asf checked — landings verified on production by hand. `asf checked --help`\n'

#: The three shapes a tick's first token may take: ``#<digits>``, bare ``<digits>``, or
#: ``[0-9a-f]{7,40}``. Anything else — including a ``#`` not followed by a digit — is a comment.
_PR_HASH_TOKEN = re.compile(r'^#(\d+)$')
_PR_BARE_TOKEN = re.compile(r'^\d+$')
_SHA_TOKEN = re.compile(r'^[0-9a-f]{7,40}$')

#: The one string that names a landing still waiting on a tick — written onto a held card, read
#: back out of `index.json` by the tick, and printed by `asf checked`'s own footer.
WAITING_PREFIX = 'waiting on your check: '


def path(product):
    """`<state dir>/checked.txt` for ``product`` — the operator's per-product checked list.
    Creates the state directory, as `env.state_dir` always does."""
    return os.path.join(env.state_dir(product), 'checked.txt')


def _line_token(line):
    """The normalised tick token ``line`` carries: its first word that is a PR tick
    (`#<digits>` or bare `<digits>`, normalised to decimal digits with any `#` stripped) or a
    sha tick (`[0-9a-f]{7,40}`, lowercase) — skipping over a hand-written line's leading `#`
    comment marker, or an `add`-written line's leading timestamp and item fields, neither of
    which can itself take that shape. Everything from that word's note onward is never parsed;
    a line with no such word (a comment, a blank line) yields None."""
    for tok in line.split():
        m = _PR_HASH_TOKEN.match(tok)
        if m:
            return m.group(1)
        if _PR_BARE_TOKEN.match(tok) or _SHA_TOKEN.match(tok):
            return tok
    return None


def _tokens(file_path):
    """The set of ticked tokens ``file_path`` holds (:func:`_line_token` of each line). A
    missing file, or any other `OSError` reading one, is the empty set."""
    out = set()
    try:
        with open(file_path, encoding='utf-8') as f:
            lines = f.readlines()
    except OSError:
        return out
    for line in lines:
        tok = _line_token(line)
        if tok is not None:
            out.add(tok)
    return out


def read(product=None, checked_file=None):
    """The ticked tokens — PR numbers as decimal strings, landing shas as lowercase hex — that
    count as checked for ``product``. With ``checked_file`` given, reads **only** that file and
    nothing else: that is what every caller of the argument means (a test fixture standing the
    real files down). Otherwise reads the product's own file (`path`) unioned with the legacy
    global one (`LEGACY_PATH`)."""
    if checked_file is not None:
        return _tokens(checked_file)
    return _tokens(path(product)) | _tokens(LEGACY_PATH)


def matches(checked, pr, sha):
    """True when ``pr`` (an int or a decimal string) is ticked, or when some token in
    ``checked`` is at least 7 characters of lowercase hex and a prefix of ``sha``. ``pr=None``
    contributes nothing — it is never coerced to the string `'None'` — and an empty or missing
    ``sha`` matches nothing on the sha side."""
    if pr is not None and str(pr) in checked:
        return True
    sha = (sha or '').lower()
    if not sha:
        return False
    for tok in checked:
        if len(tok) >= 7 and re.match(r'^[0-9a-f]+$', tok) and sha.startswith(tok):
            return True
    return False


def add(product, landings):
    """Append one line per landing in ``landings`` not already ticked (by `read(product)`, so a
    token the legacy file already carries is skipped too); return the tokens written, in order.
    ``landings`` is an iterable of ``(token, item, note)`` triples — ``token`` the decimal PR
    string or the full lowercase sha, ``item`` the id the tick is for (or `''`), ``note`` the
    landing's subject (or `''`). Ticking the same token twice in one call writes it once. Writes
    `HEADER` first if the product's file does not exist yet; opens the file once, in `'a'`."""
    seen = read(product)
    lines = []
    written = []
    for token, item, note in landings:
        if token in seen:
            continue
        seen.add(token)
        stamp = time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime())
        lines.append(f'{stamp}  {item or "-"}  {token}  {note}'.rstrip() + '\n')
        written.append(token)
    if not lines:
        return []
    p = path(product)
    is_new = not os.path.exists(p)
    with open(p, 'a', encoding='utf-8') as f:
        if is_new:
            f.write(HEADER)
        f.writelines(lines)
    return written


def remove(product, tokens):
    """Drop every line of the product's file (`path(product)`) whose first token is in
    ``tokens``; return the tokens actually dropped. Rewrites the file through a `+'.tmp'` /
    `os.replace` pair; every other line is kept byte for byte. `LEGACY_PATH` is never opened for
    writing — a token that lives only there is not in the return value, because an operator who
    hand-wrote the global file needs to be told why `--undo` did nothing there."""
    p = path(product)
    wanted = set(tokens)
    try:
        with open(p, encoding='utf-8') as f:
            raw_lines = f.readlines()
    except OSError:
        return []
    dropped = []
    kept = []
    for line in raw_lines:
        tok = _line_token(line)
        if tok is not None and tok in wanted:
            dropped.append(tok)
            continue
        kept.append(line)
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.writelines(kept)
    os.replace(tmp, p)
    return dropped


def waiting_line(iid, landings):
    """`'waiting on your check: #869, #871 — asf checked --item F-0119'` — ``landings`` is an
    iterable of ``(pr, sha)`` pairs, each rendered `#<pr>` when it has one and the sha's first 9
    characters when it does not."""
    tokens = ', '.join(f'#{pr}' if pr is not None else sha[:9] for pr, sha in landings)
    return f'{WAITING_PREFIX}{tokens} — asf checked --item {iid}'


def held(root):
    """`{iid: [token, …]}` — every item in `<root>/index.json` whose `evidence` carries a
    `waiting_line`, mapped to the raw display tokens that line names, in order. `{}` when the
    record has no `index.json`."""
    from asf.views import index_reader
    try:
        items, _generated = index_reader.load(root)
    except OSError:
        return {}
    out = {}
    for iid, item in items.items():
        for line in item.get('evidence') or []:
            if line.startswith(WAITING_PREFIX):
                rest = line[len(WAITING_PREFIX):]
                tokens_part = rest.split(' — ', 1)[0]
                out[iid] = [t.strip() for t in tokens_part.split(',')]
                break
    return out
