#!/usr/bin/env python3
"""tools/spike/f-0065/collect.py — reads the probe logs, pairs the legs, prints the tables.

Throwaway (F-0065, D8): named for deletion in ``docs/research/sandboxed-worker-shell.md`` once the
launch-path card adopts its findings. Nothing here is imported by ``asf/``.

``--surface`` is the check this Feature exists to run before any other mode does anything (D2,
§3.10): it lists every settings key ``settings/*.json`` actually uses, with the ``//`` comment that
names where in the harness it was read from, and refuses (exit 1) while any *key* is still a
``<...>`` slot. A ``${...}`` in a *value* is a path ``session.sh`` renders later (PD4) and is not a
slot.

The other modes (``--require-control``, ``--pair``, ``--markdown``, ``--all``, ``--check-doc``) read
the stream-json logs a launched session leaves at ``<state>/spike/f-0065/<role>.<variant>.jsonl``
(§4) and are exercised for real once Task 3 launches a session; they are written here, against the
shape the spec's own §2.2/§2.3 describe, because §2.5's collector is whole in this Task.
"""
import argparse
import json
import os
import re
import sys
from collections import OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_DIR = os.path.join(HERE, 'settings')

sys.path.insert(0, os.path.join(HERE, '..', '..', '..'))
try:
    from asf import env as asf_env
except Exception:  # pragma: no cover - collect.py must still run --surface with no asf/ on the path
    asf_env = None

CONTROL_PROBES = ('P00', 'P01')

# §2.1's role column, read once and used everywhere a table needs "needed by the role" —
# never guessed per probe, this is the spec's own table transcribed.
ROLE_PROBES = {
    'coder': ('P00', 'P01', 'P02', 'P03', 'P04', 'P05', 'P06', 'P07', 'P08', 'P09', 'P10', 'P11', 'P12'),
    'reviewer': ('P00', 'P01', 'P02', 'P03', 'P04', 'P05', 'P06', 'P07', 'P08', 'P09'),
    'prober': ('P00', 'P01', 'P07', 'P08', 'P13'),
}

PROBE_TEXT = {
    'P00': 'control: write a file at $HOME/.f0065-control',
    'P01': 'control: a TLS connection to a host no grant names',
    'P02': 'edit a tracked file in the worktree',
    'P03': 'git commit -s --allow-empty',
    'P04': 'git ls-remote origin',
    'P05': 'git push --dry-run origin HEAD:refs/heads/<scratch>',
    'P06': "the code host's API CLI, its rate-limit endpoint",
    'P07': 'the factory CLI, one read-only view',
    'P08': 'a Bash call the approvals matrix classifies',
    'P09': "the product's test command",
    'P10': 'dependency installation inside the session',
    'P11': 'the headless browser engine',
    'P12': 'the development server on a loopback port',
    'P13': 'read the brief from the state dir and write the named side file',
}

# §2.5's grant slots. Each maps the probe(s) it flips to the one settings.json entry the plan
# names for it (Task 2's own "Files" bullet) — used only to label "what must be granted" in
# --markdown; the grant itself lives in settings/<role>.grant-<n>.json, never hand-typed here.
GRANT_FOR_PROBE = {
    'P03': 'sandbox.filesystem.allowWrite: ${REPO}/.git',
    'P04': 'sandbox.network.allowedDomains: the code host',
    'P05': 'sandbox.network.allowedDomains: the code host',
    'P06': "sandbox.network.allowedDomains: the code host's API domain",
    'P07': 'sandbox.filesystem.allowWrite: ${HOME}/.local/bin',
    'P08': 'sandbox.filesystem.allowWrite: ${STATE}',
    'P13': 'sandbox.filesystem.allowWrite: ${STATE}',
    'P09': 'sandbox.filesystem.allowWrite: ${TMPDIR}',
    'P10': 'sandbox.network.allowedDomains: the package registry',
    'P12': 'sandbox.network.allowLocalBinding',
}


def _state_dir():
    home = None
    if asf_env is not None:
        home = asf_env.ASF_HOME
    home = home or os.environ.get('ASF_HOME') or os.path.expanduser('~/.ASF')
    return os.path.join(home, 'state', 'spike', 'f-0065')


def _log_path(role, variant):
    return os.path.join(_state_dir(), f'{role}.{variant}.jsonl')


# ---- JSONC: a settings file is JSON plus // line comments, never inside a string ------------

def strip_jsonc_comments(text):
    out = []
    in_string = False
    escape = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == '/' and i + 1 < n and text[i + 1] == '/':
            while i < n and text[i] != '\n':
                i += 1
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


_KEY_START_RE = re.compile(r'^\s*"([^"]+)"\s*:')


def _comment_start(line):
    """The index of the ``//`` that starts a trailing comment, outside any string — never the
    ``//`` inside a value like an ``https://`` URL — or -1 when the line carries none."""
    in_string = False
    escape = False
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if in_string:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == '/' and i + 1 < n and line[i + 1] == '/':
            return i
        i += 1
    return -1


def _line_provenance(raw_text):
    """The ``// ...`` comment on a key's own opening line, keyed by that JSON string alone.

    Two different objects nested at different points may reuse a short key (``"enabled"``
    appears once here, under ``sandbox``); §3.10 only needs one place naming where each key
    was read from, and the settings files never define the same short key two different ways.
    """
    prov = {}
    for line in raw_text.splitlines():
        km = _KEY_START_RE.match(line)
        if not km:
            continue
        idx = _comment_start(line)
        if idx == -1:
            continue
        prov.setdefault(km.group(1), line[idx + 2:].strip())
    return prov


def _walk_keys(obj, prefix=()):
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = prefix + (k,)
            yield '.'.join(path), k
            yield from _walk_keys(v, path)


def settings_files():
    if not os.path.isdir(SETTINGS_DIR):
        return []
    return sorted(
        os.path.join(SETTINGS_DIR, name)
        for name in os.listdir(SETTINGS_DIR)
        if name.endswith('.json')
    )


def leg_name(path):
    return os.path.basename(path)[: -len('.json')]


def load_settings(path):
    with open(path) as f:
        raw = f.read()
    return json.loads(strip_jsonc_comments(raw)), raw


def surface():
    """dotted key path -> the provenance comment on its line (or '' if none was found)."""
    keys = OrderedDict()
    for path in settings_files():
        data, raw = load_settings(path)
        prov = _line_provenance(raw)
        for dotted, leaf in _walk_keys(data):
            if dotted not in keys or not keys[dotted]:
                keys[dotted] = prov.get(leaf, '')
    return keys


def cmd_surface(args):
    keys = surface()
    if not keys:
        print('no settings/*.json files yet', file=sys.stderr)
        return 1
    slots = [k for k in keys if any(part.startswith('<') for part in k.split('.'))]
    if args.json:
        print(json.dumps(keys, indent=2))
    else:
        for k, v in keys.items():
            print(f'{k} — {v}' if v else f'{k} — (no provenance comment on this key)')
        if slots:
            print(f'refused: {len(slots)} key(s) still a <...> slot: {", ".join(slots)}', file=sys.stderr)
    return 1 if slots else 0


# ---- the logs: PROBE lines live inside the assistant's own tool results ---------------------

_PROBE_LINE_RE = re.compile(r'^PROBE (P\d\d) (ok|refused|error)(?:\s+(.*))?$')


def _iter_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_strings(v)


def probes_in_record(record):
    """[(probe id, verdict, one-line detail), ...] — §3.3's fence: never trust one known field."""
    if 'PROBE ' not in json.dumps(record):
        return []
    found = []
    for s in _iter_strings(record):
        if 'PROBE ' not in s:
            continue
        for line in s.split('\n'):
            m = _PROBE_LINE_RE.match(line.strip())
            if m:
                found.append((m.group(1), m.group(2), (m.group(3) or '').strip()))
    return found


def read_log(role, variant):
    path = _log_path(role, variant)
    if not os.path.isfile(path):
        return None
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def probes_from_log(role, variant):
    """probe id -> (verdict, detail); the last PROBE line for an id in the log wins."""
    records = read_log(role, variant)
    if records is None:
        return None
    result = {}
    for rec in records:
        for pid, verdict, detail in probes_in_record(rec):
            result[pid] = (verdict, detail)
    return result


def controls_ok(probes):
    return bool(probes) and all(probes.get(pid, (None,))[0] == 'refused' for pid in CONTROL_PROBES)


def cmd_require_control(args):
    role = args.role
    legs = [args.leg] if args.leg else ['bare']
    rc = 0
    for leg in legs:
        probes = probes_from_log(role, leg)
        if probes is None:
            print(f'{role}.{leg}: no log — nothing to require a control from', file=sys.stderr)
            rc = 1
            continue
        bad = [pid for pid in CONTROL_PROBES if probes.get(pid, (None,))[0] != 'refused']
        if bad:
            reasons = ', '.join(f'{pid}={probes.get(pid, ("unpaired",))[0]}' for pid in bad)
            print(
                f'{role}.{leg}: control probe(s) not refused ({reasons}) — the settings file '
                'was ignored (P11); no table from this leg',
                file=sys.stderr,
            )
            rc = 1
        else:
            print(f'{role}.{leg}: controls {" and ".join(CONTROL_PROBES)} read refused')
    return rc


def cmd_pair(args):
    leg_a, leg_b = args.pair
    role_a, variant_a = leg_a.split('.', 1)
    role_b, variant_b = leg_b.split('.', 1)
    probes_a = probes_from_log(role_a, variant_a) or {}
    probes_b = probes_from_log(role_b, variant_b) or {}
    log_a_missing = read_log(role_a, variant_a) is None
    log_b_missing = read_log(role_b, variant_b) is None
    print(f'| probe | {leg_a} | {leg_b} |')
    print('| --- | --- | --- |')
    unpaired = 0
    for pid in args.probe:
        a = probes_a.get(pid)
        b = probes_b.get(pid)
        a_s = 'unpaired' if (a is None or log_a_missing) else a[0]
        b_s = 'unpaired' if (b is None or log_b_missing) else b[0]
        if a_s == 'unpaired' or b_s == 'unpaired':
            unpaired += 1
        print(f'| {pid} | {a_s} | {b_s} |')
    return 1 if unpaired else 0


def _grant_leg_for(role, probe_id, bare_verdict):
    """the grant-<n> leg whose settings file added exactly the entry this probe needed."""
    if bare_verdict == 'ok':
        return None
    for path in settings_files():
        name = leg_name(path)
        if not name.startswith(f'{role}.grant-'):
            continue
        probes = probes_from_log(role, name.split('.', 1)[1])
        if probes and probes.get(probe_id, (None,))[0] == 'ok':
            return name
    return None


def _role_table(role):
    rows = []
    bare = probes_from_log(role, 'bare')
    for pid in ROLE_PROBES.get(role, ()):
        bare_v = (bare or {}).get(pid)
        bare_s = 'unpaired' if bare is None or bare_v is None else bare_v[0]
        grant_leg = _grant_leg_for(role, pid, bare_s) if bare is not None else None
        granted = GRANT_FOR_PROBE.get(pid, 'not needed') if bare_s == 'refused' else 'not needed'
        pair = f'{role}.bare' + (f' / {grant_leg}' if grant_leg else '')
        rows.append((pid, PROBE_TEXT.get(pid, ''), bare_s, granted, pair))
    return rows


def _markdown_table(role):
    lines = [
        f'#### {role}',
        '',
        '| probe | needed by the role | under the bare fence | what must be granted | the pair |',
        '| --- | --- | --- | --- | --- |',
    ]
    for pid, text, bare_s, granted, pair in _role_table(role):
        lines.append(f'| `{pid} {text}` | yes | {bare_s} | {granted} | `{pair}` |')
    return '\n'.join(lines)


def cmd_markdown(args):
    roles = ['coder', 'reviewer', 'prober'] if args.all else [args.role]
    print('\n\n'.join(_markdown_table(r) for r in roles))
    return 0


def cmd_unpaired(args):
    count = 0
    for role, probes in ROLE_PROBES.items():
        for pid, _text, bare_s, _granted, _pair in _role_table(role):
            if bare_s == 'unpaired':
                count += 1
            elif bare_s == 'refused' and _grant_leg_for(role, pid, bare_s) is None:
                count += 1
    print(count)
    return 0


def cmd_check_doc(args):
    with open(args.path) as f:
        doc = f.read()
    rows = re.findall(r'^\|\s*`(P\d\d)[^`]*`\s*\|.*\|\s*`([a-z]+\.[\w.-]+)(?:\s*/\s*([a-z]+\.[\w.-]+))?`\s*\|\s*$',
                       doc, re.MULTILINE)
    if not rows:
        print('check-doc: no probe table rows found', file=sys.stderr)
        return 1
    rc = 0
    for pid, leg_a, leg_b in rows:
        role, variant = leg_a.split('.', 1)
        probes = probes_from_log(role, variant)
        if probes is None or pid not in probes:
            print(f'check-doc: {pid} in {leg_a} has no matching paired result in the logs', file=sys.stderr)
            rc = 1
        if leg_b:
            role_b, variant_b = leg_b.split('.', 1)
            probes_b = probes_from_log(role_b, variant_b)
            if probes_b is None or pid not in probes_b:
                print(f'check-doc: {pid} in {leg_b} has no matching paired result in the logs', file=sys.stderr)
                rc = 1
    for role in ROLE_PROBES:
        table_marker = f'#### {role}'
        if table_marker in doc:
            after = doc.split(table_marker, 1)[1]
            first_rows = re.findall(r'^\|\s*`(P\d\d)[^`]*`.*$', after, re.MULTILINE)[:2]
            if first_rows != list(CONTROL_PROBES):
                print(f'check-doc: {role}\'s table does not open with {CONTROL_PROBES}', file=sys.stderr)
                rc = 1
    m = re.search(r'unpaired[^\d]*(\d+)', doc, re.IGNORECASE)
    if m:
        stamped = int(m.group(1))
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_unpaired(argparse.Namespace())
        actual = int(buf.getvalue().strip())
        if stamped != actual:
            print(f'check-doc: the stamp says {stamped} unpaired rows, the collector counts {actual}', file=sys.stderr)
            rc = 1
    else:
        print('check-doc: no unpaired count found in the stamp', file=sys.stderr)
        rc = 1
    return rc


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--surface', action='store_true')
    p.add_argument('--json', action='store_true')
    p.add_argument('--role', choices=sorted(ROLE_PROBES))
    p.add_argument('--leg')
    p.add_argument('--require-control', action='store_true')
    p.add_argument('--pair', nargs=2, metavar=('LEG_A', 'LEG_B'))
    p.add_argument('--probe', action='append', default=[])
    p.add_argument('--markdown', action='store_true')
    p.add_argument('--all', action='store_true')
    p.add_argument('--unpaired', action='store_true')
    p.add_argument('--check-doc', dest='check_doc_path')
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.check_doc_path:
        args.path = args.check_doc_path
        return cmd_check_doc(args)
    if args.surface:
        return cmd_surface(args)
    if args.pair:
        return cmd_pair(args)
    if args.require_control:
        return cmd_require_control(args)
    if args.unpaired:
        return cmd_unpaired(args)
    if args.markdown:
        return cmd_markdown(args)
    print('nothing to do — see collect.py --help', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
