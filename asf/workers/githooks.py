"""asf.workers.githooks — the per-product git hook dir a session runs under (F-0076, D3/D4/D13).

``ensure(product)`` writes ``state/<product>/githooks/asf-hook`` and one pass-through file per
client-side hook name (:data:`HOOKS`), and returns the directory — handed to the session as
``core.hooksPath`` through :func:`asf.workers.runtime.build_env`. Every hook in the dir ``exec``s
``asf-hook <name>``, which stamps an ``ASF-Session`` trailer on ``prepare-commit-msg`` and then
chains to the product repo's own hook of the same name, found at hook time (not frozen in at
spawn, since ``core.hooksPath`` can change without a respawn). A file is rewritten only when its
content differs, so a session that runs ``ensure`` on every launch never touches a file it
already wrote correctly. Nothing here is written into the product repo or its ``.git``.

``commit-msg`` names the item: a worker session's commit whose subject lacks its item id as a
token (:func:`names_item`) is reworded to :func:`name_subject`'s — ``<kind>(<ID>): …`` — before
the product's own hook sees it, so the lane never refuses a branch for naming. The id is the
job's ``ASF_ITEM`` (:func:`item_env`), else the first id token in ``ASF_JOB``; a commit with
neither, or outside a worker session (no ``ASF_JOB``), is left alone. It never blocks.

Under the product's ``commit.signoff`` (``ASF_SIGNOFF=1``, :func:`item_env`) ``commit-msg`` also
appends ``Signed-off-by: <author name> <author email>`` — the worktree's git identity — when the
message carries no ``Signed-off-by`` yet (``git interpret-trailers --if-exists doNothing``), so a
product's required DCO check passes whatever the brief said about ``git commit -s``.
"""
import os
import re

from asf import env

#: The client-side hook names from githooks(5) — every one a shim passes through to the
#: product's own hook of the same name.
HOOKS = ('applypatch-msg', 'pre-applypatch', 'post-applypatch', 'pre-commit', 'pre-merge-commit',
         'prepare-commit-msg', 'commit-msg', 'post-commit', 'pre-rebase', 'post-checkout',
         'post-merge', 'pre-push', 'post-rewrite', 'reference-transaction', 'pre-auto-gc',
         'post-index-change', 'push-to-checkout', 'sendemail-validate')

#: The shim every hook name in the dir is: ``exec``s ``asf-hook`` with its own name.
_SHIM = '#!/bin/sh\nexec "$(dirname "$0")/asf-hook" {name} "$@"\n'

#: 1. the hook name, and stamp the ``ASF-Session`` trailer on a ``prepare-commit-msg``;
#: 2. find the product's own hooks dir at hook time (D4): ``core.hooksPath`` with
#:    ``GIT_CONFIG_COUNT`` unset, anchored at the worktree's toplevel if relative, else
#:    ``<git-common-dir>/hooks``;
#: 3. nothing to chain when that dir is this one;
#: 4. otherwise exec the product's own hook of the same name, stdin passed through by ``exec``.
ASF_HOOK = r'''#!/bin/sh
# written by asf.workers.githooks.ensure — the ASF-Session trailer and hook passthrough (F-0076)
name=$1
shift
here=$(cd "$(dirname "$0")" && pwd -P)

if [ "$name" = "prepare-commit-msg" ] && [ -n "$ASF_SESSION" ]; then
    git interpret-trailers --in-place --if-exists doNothing \
        --trailer "ASF-Session: $ASF_SESSION" "$1"
fi

# a pre-commit that found no scanner (no asf, no tools/checks/redact.sh) left the flag: the
# commit carries "Redaction: unchecked", and the landing re-scans it before it merges
if [ "$name" = "commit-msg" ] && [ -f "$1" ]; then
    flag=$(git rev-parse --git-path asf-redaction-unchecked 2>/dev/null)
    if [ -n "$flag" ] && [ -f "$flag" ]; then
        git interpret-trailers --in-place --if-exists doNothing \
            --trailer "Redaction: unchecked" "$1" 2>/dev/null && rm -f "$flag"
    fi
fi

# a worker's commit names its item: a subject lacking the id gets <kind>(<ID>): — never a block
if [ "$name" = "commit-msg" ] && [ -n "$ASF_JOB" ] && [ -f "$1" ]; then
    item=$ASF_ITEM
    if [ -z "$item" ]; then
        item=$(printf '%s\n' "$ASF_JOB" | grep -oE '[A-Za-z]+-[0-9]{4,}' | head -n 1 \
            | tr '[:lower:]' '[:upper:]')
    fi
    if [ -n "$item" ]; then
        awk -v id="$item" -v kind="${ASF_ITEM_KIND:-chore}" '
            done || /^[ \t]*$/ || /^#/ { print; next }
            {
                done = 1; s = $0
                if (s ~ /^(fixup|squash|amend)! / || s ~ /^Merge /) { print s; next }
                if (tolower(s) ~ ("(^|[^a-z0-9_-])" tolower(id) "([^a-z0-9_]|$)")) { print s; next }
                if (match(s, /^[a-z]+!?: /)) {
                    head = substr(s, 1, RLENGTH - 2); rest = substr(s, RLENGTH + 1); bang = ""
                    if (substr(head, length(head)) == "!") {
                        bang = "!"; head = substr(head, 1, length(head) - 1)
                    }
                    print head "(" id ")" bang ": " rest
                } else {
                    print kind "(" id "): " s
                }
            }' "$1" > "$1.asf-name" 2>/dev/null && mv "$1.asf-name" "$1" || rm -f "$1.asf-name"
    fi
fi

# a product that requires a DCO sign-off (commit.signoff): the commit's author signs it off
# unless the message already carries a Signed-off-by — an empty message stays empty (git aborts)
if [ "$name" = "commit-msg" ] && [ "$ASF_SIGNOFF" = "1" ] && [ -f "$1" ] \
        && grep -qv -e '^[[:space:]]*$' -e '^#' "$1"; then
    who=$(git var GIT_AUTHOR_IDENT 2>/dev/null | sed 's/ [0-9][0-9]* [-+][0-9][0-9]*$//')
    if [ -n "$who" ]; then
        git interpret-trailers --in-place --if-exists doNothing \
            --trailer "Signed-off-by: $who" "$1" 2>/dev/null || :
    fi
fi

own=$(env -u GIT_CONFIG_COUNT git config --get core.hooksPath 2>/dev/null)
if [ -n "$own" ]; then
    case "$own" in
        /*) : ;;
        *) top=$(git rev-parse --show-toplevel 2>/dev/null) && own="$top/$own" ;;
    esac
else
    common=$(git rev-parse --git-common-dir 2>/dev/null) && own="$common/hooks"
fi

same=0
if [ -n "$own" ] && [ -d "$own" ]; then
    resolved=$(cd "$own" && pwd -P)
    [ "$resolved" = "$here" ] && same=1
fi

# a scratch push: a repo that is not a linked worktree (a session's worktree always is) pushing
# to a local path — a product test's throwaway fixture repo. The session's guard, trunk check and
# push log are not for it: they inherit core.hooksPath through GIT_CONFIG_* and refused every
# fixture push of a branch named main (2026-10-02: five sessions ended "hook refused")
scratch=0
if [ "$name" = "pre-push" ]; then
    case "$2" in
        file://*) local_url=1 ;;
        *://*) local_url=0 ;;
        *:*) case "${2%%:*}" in */*) local_url=1 ;; *) local_url=0 ;; esac ;;
        *) local_url=1 ;;
    esac
    if [ "$local_url" = 1 ]; then
        gd=$(git rev-parse --path-format=absolute --git-dir 2>/dev/null)
        cd_=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)
        [ -n "$gd" ] && [ "$gd" = "$cd_" ] && scratch=1
    fi
fi

# one push per correction round (asf.workers.pushlog): a push the product's own pre-push hook
# passed is logged — its local sha, one line — and a correction session is told on its second.
# A session pushes only to factory branches (ASF_PUSH_ALLOW, asf.workers.spawn.push_allow): a
# refs/heads/ ref under none of its prefixes is refused before anything else runs
if [ "$name" = "pre-push" ] && [ "$scratch" = 0 ] \
        && { [ -n "$ASF_PUSH_LOG" ] || [ -n "$ASF_PUSH_ALLOW" ]; }; then
    input=$(cat)
    # a refusal is written down (asf.workers.refusals, ASF_REFUSAL_LOG): one JSON object per
    # line, and a write that fails never changes the hook's answer (F-0266 C4)
    _json() { tr -d '\000-\037' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/^/"/' -e 's/$/"/'; }
    asf_refused() {   # $1 kind, $2 line
        [ -n "$ASF_REFUSAL_LOG" ] || return 0
        printf '{"at":"%s","kind":"%s","where":"shim","line":%s}\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" \
            "$(printf '%s' "$2" | tr '\n' ' ' | cut -c1-2000 | _json)" \
            >> "$ASF_REFUSAL_LOG" 2>/dev/null || :
    }
    # a heartbeat (asf.workers.heartbeat) pushes only refs/asf/hb/<job>: no work, no CI, no count
    if [ -n "$input" ] && ! printf '%s\n' "$input" | awk '
            NF >= 4 && $3 !~ /^refs\/asf\/hb\// { work = 1 } END { exit work ? 0 : 1 }'; then
        exit 0
    fi
    if [ -n "$ASF_PUSH_ALLOW" ] && [ -n "$input" ]; then
        bad=$(printf '%s\n' "$input" | awk -v allow="$ASF_PUSH_ALLOW" '
            NF >= 4 && $3 ~ /^refs\/heads\// {
                b = substr($3, 12); ok = 0; n = split(allow, a, " ")
                for (i = 1; i <= n; i++) if (a[i] != "" && index(b, a[i]) == 1) ok = 1
                if (!ok) print b
            }')
        if [ -n "$bad" ]; then
            msg=$(echo "asf: push refused — an ASF session pushes only to factory branches, not:" $bad)
            echo "$msg" >&2
            asf_refused push-allow "$msg"
            exit 1
        fi
    fi
    rc=0
    if [ "$same" = 0 ] && [ -n "$own" ] && [ -x "$own/$name" ]; then
        out=$(if [ -n "$input" ]; then printf '%s\n' "$input"; fi | "$own/$name" "$@" 2>&1)
        rc=$?
        [ -n "$out" ] && printf '%s\n' "$out" >&2
        [ "$rc" = 0 ] || asf_refused "hook refused" "$out"
    fi
    cli="$HOME/.local/bin/asf"
    # CI judges the branch merged into the trunk, by the trunk's own check list and scripts: the
    # product's pre-push check runs once more on that merge (asf.workers.trunkmerge)
    if [ "$rc" = 0 ] && [ -n "$input" ] && [ -n "$ASF_PRODUCT" ]; then
        if [ -x "$cli" ] && ! head -n 3 "$cli" 2>/dev/null | grep -qF '# asf dispatcher'; then
            msg="asf: push refused — $cli is not asf's dispatcher; asf hooks install --product $ASF_PRODUCT"
            echo "$msg" >&2
            asf_refused "hook refused" "$msg"
            rc=1
        elif [ -x "$cli" ]; then
            out=$(printf '%s\n' "$input" \
                | "$cli" trunk-check --pre-push --product "$ASF_PRODUCT" 2>&1)
            rc=$?
            [ -n "$out" ] && printf '%s\n' "$out" >&2
            [ "$rc" = 0 ] || asf_refused "hook refused" "$out"
        fi
    fi
    # the brief's own rules — package tests not run, a diff outside the footprint — are checked
    # the same push stops in, matched to the paths this push adds (asf.prepush, F-0301). No
    # dispatcher at $cli: trunk-check already skipped above (an old install, no asf built yet),
    # and the door skips the same way — only a stale or malformed $cli is refused, never a push
    # with none at all
    if [ "$rc" = 0 ] && [ -n "$input" ] && [ -n "$ASF_PRODUCT" ] && [ -x "$cli" ]; then
        out=$(printf '%s\n' "$input" | "$cli" pre-push --product "$ASF_PRODUCT" 2>&1)
        rc=$?
        [ -n "$out" ] && printf '%s\n' "$out" >&2
        [ "$rc" = 0 ] || asf_refused "hook refused" "$out"
    fi
    if [ "$rc" = 0 ] && [ -n "$input" ] && [ -n "$ASF_PUSH_LOG" ]; then
        printf '%s\n' "$input" | awk 'NF >= 4 && $2 !~ /^0+$/ { print $2 }' \
            >> "$ASF_PUSH_LOG" 2>/dev/null || :
        n=$(sort -u "$ASF_PUSH_LOG" 2>/dev/null | grep -c .)
        if [ -n "$ASF_ONE_PUSH" ] && [ "${n:-0}" -gt 1 ]; then
            echo "asf: push $n of this correction round — a correction pushes once, as its last act; each push restarts CI and cancels the run before it. This run's report counts it as a defect." >&2
        fi
    fi
    exit $rc
fi

[ "$same" = 1 ] && exit 0

if [ -n "$own" ] && [ -x "$own/$name" ]; then
    exec "$own/$name" "$@"
fi

exit 0
'''


#: A session's report commit — its end marker (:func:`report_subject`): ``asf(<ID>): report
#: <job>``, the older unnamed ``asf: report <job>``, or either after a commit-msg hook stacked an
#: item scope onto it (``plan(F-0116): asf(F-0116): report …``). It lands no work: the lane never
#: refuses it for naming, nor counts it — or any empty commit — as a copy of a trunk commit (F-0278).
REPORT_SUBJECT_RE = re.compile(r"^(?:[A-Za-z][\w-]*\([^)]*\)!?:\s*)*asf(?:\([^)]*\))?!?:\s*report\s")

#: an item id token in a job name (``plan-f-1131``) — the ``commit-msg`` hook's fallback
_JOB_ID_RE = re.compile(r'[A-Za-z]+-[0-9]{4,}')


def is_report_subject(subject):
    """True for a session's report commit subject (:data:`REPORT_SUBJECT_RE`), either form."""
    return bool(REPORT_SUBJECT_RE.match((subject or '').strip()))


def report_subject(job_name, item=None):
    """The subject a session's report commit carries: ``asf(<ID>): report <job>`` — ``item``,
    else the first id token in ``job_name`` as the hook reads ``ASF_JOB`` — so the lane's naming
    check holds with no reword; ``asf: report <job>`` when neither names an item."""
    if not item:
        m = _JOB_ID_RE.search(job_name or '')
        item = m.group(0).upper() if m else ''
    return f'asf({item}): report {job_name}' if item else f'asf: report {job_name}'


#: A lane branch kind → the commit kind its subjects open with (:func:`name_subject`).
COMMIT_KIND = {'code': 'task', 'fix': 'fix', 'spec': 'spec', 'plan': 'plan'}

#: A conventional subject with no scope: ``type: rest`` or ``type!: rest``.
_CONVENTIONAL_RE = re.compile(r'^(?P<type>[a-z]+)(?P<bang>!?): (?P<rest>.*)$', re.S)


def names_item(subject, item):
    """True when ``subject`` carries ``item`` as a token (case-insensitive) — the lane's naming
    rule, and the ``commit-msg`` hook's."""
    return bool(item) and re.search(r'(?<![\w-])' + re.escape(item) + r'(?![\w])',
                                    subject or '', re.I) is not None


def name_subject(subject, item, kind=None):
    """``subject`` naming ``item``: unchanged when it does, ``type(<ID>): rest`` for a
    conventional ``type: rest``, else ``<kind>(<ID>): <subject>`` (``chore`` with no kind) — the
    rewrite the ``commit-msg`` hook makes."""
    if not item or names_item(subject, item):
        return subject
    m = _CONVENTIONAL_RE.match(subject or '')
    if m:
        return f"{m['type']}({item}){m['bang']}: {m['rest']}"
    return f"{kind or 'chore'}({item}): {subject}"


def item_env(conv, item, branch):
    """``{ASF_ITEM, ASF_ITEM_KIND}`` for a session on ``branch`` for ``item`` — what the
    ``commit-msg`` hook names each commit with — plus ``ASF_SIGNOFF=1`` under the product's
    ``commit.signoff`` (the hook then signs each commit off); ``{}`` with neither."""
    out = {}
    signoff = getattr(conv, 'signoff', None)
    if callable(signoff) and signoff():
        out['ASF_SIGNOFF'] = '1'
    if not item:
        return out
    of = getattr(conv, 'branch_kind', None)
    kind = COMMIT_KIND.get(of(branch) if callable(of) and branch else None, 'chore')
    return {**out, 'ASF_ITEM': str(item), 'ASF_ITEM_KIND': kind}


def _write_if_changed(path, text):
    data = text.encode('utf-8')
    if os.path.isfile(path):
        with open(path, 'rb') as f:
            if f.read() == data:
                return
    with open(path, 'wb') as f:
        f.write(data)
    os.chmod(path, 0o755)


def ensure(product):
    """Writes ``state/<product>/githooks/asf-hook`` and one pass-through file per :data:`HOOKS`
    name, each rewritten only when its content differs. Returns the directory."""
    d = os.path.join(env.state_dir(product), 'githooks')
    os.makedirs(d, exist_ok=True)
    _write_if_changed(os.path.join(d, 'asf-hook'), ASF_HOOK)
    for name in HOOKS:
        _write_if_changed(os.path.join(d, name), _SHIM.format(name=name))
    return d
