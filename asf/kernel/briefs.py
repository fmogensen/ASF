"""asf.kernel.briefs — a kernel launch's brief, through the floor's brief builder (ASF 0.2).

The kernel writes no brief text of its own: :func:`build` maps a
:class:`~asf.kernel.actions.Launch` onto a feeder row and hands it to :func:`asf.briefs.build`
(the preamble, the kind's template — with the product's ``pre_push_check`` where the template
carries it — and the common REPORT tail). The kinds map as :func:`brief_kind` says: a first build
is ``coder`` (``fix-bug`` for a Bug), a build on the item's open PR is a ``correct`` round whose
correction is the review's findings or the red checks, and ``review``/``spec``/``plan`` keep
their names. The kernel adds three things the floor's text does not carry: :data:`PUSH_RULE` (a
kernel session pushes its own branch, a rebase with `--force-with-lease`; the floor's "the factory
publishes" wording is rewritten, its templates untouched), the operator's answers
already on the card, and — for a review — :data:`VERDICT_RULE`, the lines :func:`parse_verdict`
reads back off the session's report. It also drops the floor's heartbeat wording
(:data:`_HEARTBEAT_REWRITES`): a kernel session is launched with no beat loop — the kernel judges
liveness by pid and REPORT — so its brief names no HEARTBEAT command, no ``refs/asf/hb/`` ref and
no notes file (B-0098: a sandbox refused the loop's write under the shared .git, and the session
stopped on it).
"""
import dataclasses
import importlib
import re

from asf.kernel.model import RED_CONCLUSIONS

#: the floor's brief kind of a fix round (the round answers a correction: findings or a red)
FIX_KIND = 'correct'

VERDICT_RULE = """## The kernel's verdict lines

The kernel reads this review's verdict from your final message, not from the review file. After
the REPORT block, print exactly one line `VERDICT: approve` or `VERDICT: changes`, then one line
`FINDINGS: <file:line — the exact fix>` per finding the author must answer (none for an approve),
and nothing after them — this overrides "nothing after it" above. A review that ends without a
`VERDICT:` line counts as no review and is run again."""

#: the kernel's push rule: a session publishes its own branch — the kernel never pushes a rebase
#: for it (B-82658, B-83312). Appended to every kernel brief; overrides the floor's text above.
PUSH_RULE = """## Pushing (the kernel's rule — it overrides anything above)

You push your own branch; nothing pushes it for you. Your last act is `git push origin {branch}`.
After a rebase (onto `origin/{main}`, or a push refused as non-fast-forward because the branch was
rebased), push it yourself with `git push --force-with-lease origin {branch}` — never a bare
`--force`, never a merge of `origin/{main}` into it — then report
`pushed: yes <the sha origin/{branch} now points at>`. Work that is committed but not on origin
is lost."""

#: the floor's "the factory publishes a rebase" wording, and what a kernel brief says instead
_FLOOR_REWRITES = (
    # the TAIL's lane paragraph: refused push → stop, the factory publishes
    (re.compile(r'never force-push,(\s+)never'),
     r'never force-push except `--force-with-lease` after a rebase,\1never'),
    (re.compile(r'If that is refused as\s+non-fast-forward, the rebase is why:.*?'
                r'a refused push is never a `NEEDS OPERATOR`\.', re.S),
     'If that is refused as non-fast-forward, the rebase is why: push it yourself with '
     '`git push --force-with-lease origin {branch}` — never merge, never a bare force. '
     'Publishing is yours; landing is the factory\'s: never run `asf land` or any other '
     '`asf` command to publish, and a refused push is never a `NEEDS OPERATOR`.'),
    # correct.md: refused push → stop and report the factory publishes
    (re.compile(r'never a force\.(\s+)A push refused as non-fast-forward is the rebase you were '
                r'handed: stop there and report `pushed: rebased <sha> — the factory publishes`\.'),
     r'never a bare force.\1A push refused as non-fast-forward is the rebase you were handed: '
     r'push it yourself with `git push --force-with-lease origin {branch}` and report '
     r'`pushed: yes <sha>`.'),
    # the REPORT line's alternative
    (re.compile(r'rebased <sha> — the factory publishes'),
     'rebased <sha> — pushed with --force-with-lease'),
    # anything else of the floor's in the same vein
    (re.compile(r'the factory publishes the (rebased |rewritten )?branch'),
     r'you push the \1branch yourself (`git push --force-with-lease`)'),
)


#: the floor's heartbeat wording, gone from a kernel brief (the kernel launches no beat loop)
_HEARTBEAT_REWRITES = (
    (re.compile(r'^## The heartbeat, the marker, and the report$', re.M),
     '## Progress, the marker, and the report'),
    (re.compile(r'\s*The heartbeat belongs to the runtime, never to a loop you must keep alive:'
                r'.*?or a `NEEDS OPERATOR`\.', re.S), ''),
    (re.compile(r'\s*The heartbeat is the runtime\'s: a beat that cannot start never ends your '
                r'session\.'), ''),
)


def kernel_push_text(text, branch, main='main'):
    """``text`` (a floor brief) with every "the factory publishes a rebase" line rewritten to the
    kernel's rule, the floor's heartbeat wording dropped, and :data:`PUSH_RULE` appended: a kernel
    session pushes its own branch."""
    for pat, repl in _HEARTBEAT_REWRITES:
        text = pat.sub(repl, text)
    for pat, repl in _FLOOR_REWRITES:
        text = pat.sub(lambda m, r=repl: m.expand(r.replace('{branch}', branch)), text)
    return (text.rstrip('\n') + '\n\n' + PUSH_RULE.format(branch=branch, main=main) + '\n')


VERDICT_RE = re.compile(r'^\s*VERDICT:\s*(approve|changes)\s*$', re.M | re.I)
FINDING_RE = re.compile(r'^\s*FINDINGS?:\s*(.*?)\s*$', re.I)


def brief_kind(launch, item, fix):
    """The floor's brief kind for ``launch`` of ``item``; ``fix``: a build on an open PR."""
    if launch.kind != 'build':
        return launch.kind
    if fix:
        return FIX_KIND
    return 'fix-bug' if item.type == 'bug' else 'coder'


def correction(findings, pr):
    """What a fix round answers: the review's findings, else the PR's red checks."""
    from asf.kernel.decide import rebase_finding, red_findings
    rebase = [f for f in findings if rebase_finding(f)]
    if rebase and len(rebase) == len(findings):
        return 'the PR conflicts with its base:\n' + '\n'.join('- %s' % f for f in rebase)
    red = red_findings(findings)
    rest = [f for f in findings if f not in red]
    out = []
    if rest:
        out.append('the review asked for changes:\n' + '\n'.join('- %s' % f for f in rest))
    if red:
        out.append('red required check(s) on PR #%s — fix the cause in this PR:\n'
                   % (pr.number if pr else '?') + '\n'.join('- %s' % f for f in red))
    if out:
        return '\n\n'.join(out)
    reds = [c.name for c in (pr.checks if pr else ()) if c.conclusion in RED_CONCLUSIONS]
    return 'red check(s) on PR #%d: %s' % (pr.number, ', '.join(reds) or 'unknown')


def _entry(item):
    """An index entry for a card the index does not hold yet (minted after the last ingest)."""
    return {'id': item.id, 'title': item.title, 'type': item.type, 'parent': item.parent,
            'writes': list(item.writes), 'after': list(item.after), 'state': 'New'}


def build(product, launch, item, findings=(), pr=None, index=None, repo_facts=None):
    """The :class:`asf.briefs.build.Brief` for ``launch`` of ``item`` (a kernel Item). ``index``
    is the record's ``index.json`` (``{'items': {...}}``); ``repo_facts`` a callable
    ``(product, row, index) -> dict`` (the floor's :func:`asf.briefs.facts.repo_facts` when
    ``None``; a failure there leaves the git facts out, never the brief)."""
    from asf.feeder.rows import LAUNCH, Row
    floor = importlib.import_module('asf.briefs.build')
    fix = launch.kind == 'build' and pr is not None
    kind = brief_kind(launch, item, fix)
    items = dict((index or {}).get('items') or {})
    items.setdefault(item.id, _entry(item))
    index = dict(index or {}, items=items)
    row = Row(tier=0, kind='KERNEL → %s' % kind.upper(), item_id=item.id, feature_id='',
              action=LAUNCH, brief_kind=kind, branch=launch.branch,
              reason='kernel: %s %s' % (launch.kind, item.id),
              correction=correction(findings, pr) if fix else '')
    if repo_facts is None:
        from asf.briefs import facts as facts_mod
        repo_facts = facts_mod.repo_facts
    try:
        rf = repo_facts(product, row, index)
    except Exception:  # noqa: BLE001 — a brief is never lost to a git read
        rf = None
    b = floor.build(product, row, index, [], rf)
    b = dataclasses.replace(b, text=kernel_push_text(b.text, launch.branch,
                                                     getattr(product, 'main', 'main') or 'main'))
    extra = []
    if item.answers:
        extra += ['## Operator answers', ''] + ['- %s' % a for a in item.answers]
    if launch.kind == 'review':
        if pr is not None:
            extra += ['', 'The PR under review: #%d, head `%s`.' % (pr.number, pr.head_sha)]
        extra += ['', VERDICT_RULE]
    if extra:
        b = dataclasses.replace(b, text=b.text.rstrip('\n') + '\n\n' + '\n'.join(extra).strip()
                                + '\n')
    return b


def parse_verdict(text):
    """``(verdict, findings)`` from a review session's report: the last ``VERDICT:`` line
    (``approve`` or ``changes``) and the ``FINDINGS:`` lines after it; ``None`` when it has none."""
    found = list(VERDICT_RE.finditer(text or ''))
    if not found:
        return None
    last = found[-1]
    findings = []
    for line in text[last.end():].splitlines():
        m = FINDING_RE.match(line)
        if m and m.group(1) and m.group(1).lower() not in ('none', 'n/a', '-'):
            findings.append(m.group(1))
    return last.group(1).lower(), findings


class Briefer:
    """The real ports' brief maker: :func:`build` for one product, its index read once."""

    def __init__(self, product):
        self.product = product
        self._index = None

    def index(self):
        if self._index is None:
            import json
            import os
            try:
                with open(os.path.join(self.product.backlog_dir, 'index.json'),
                          encoding='utf-8') as f:
                    self._index = json.load(f)
            except (OSError, ValueError, TypeError):
                self._index = {'items': {}}
        return self._index

    def __call__(self, item, launch, findings=(), pr=None):
        return build(self.product, launch, item, findings, pr, index=self.index())
