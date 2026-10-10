"""asf.kernel.briefs — a kernel launch's brief, through the floor's brief builder (ASF 0.2).

The kernel writes no brief text of its own: :func:`build` maps a
:class:`~asf.kernel.actions.Launch` onto a feeder row and hands it to :func:`asf.briefs.build`
(the preamble, the kind's template — with the product's ``pre_push_check`` where the template
carries it — and the common REPORT tail). The kinds map as :func:`brief_kind` says: a first build
is ``coder`` (``fix-bug`` for a Bug), a build on the item's open PR is a ``correct`` round whose
correction is the review's findings or the red checks, and ``review``/``spec``/``plan`` keep
their names. The kernel adds three things the floor's text does not carry: :data:`PUSH_RULE` (a
kernel session pushes its own branch fast-forward and never force-pushes a rebase: it commits it
and reports ``pushed: rebased <sha>``, and the host publishes it — :data:`HOST_REBASE`; the
floor's "the factory publishes" wording is rewritten, its templates untouched), the operator's
answers
already on the card, and — for a review — :data:`VERDICT_RULE`, the lines :func:`parse_verdict`
reads back off the session's report. It also drops the floor's heartbeat wording
(:data:`_HEARTBEAT_REWRITES`): a kernel session is launched with no beat loop — the kernel judges
liveness by pid and REPORT — so its brief names no HEARTBEAT command, no ``refs/asf/hb/`` ref and
no notes file (B-0098: a sandbox refused the loop's write under the shared .git, and the session
stopped on it).
"""
import dataclasses
import fnmatch
import importlib
import re

from asf.kernel import dor as D
from asf.kernel import intake as I
from asf.kernel import settings as kernel_settings
from asf.kernel.model import RED_CONCLUSIONS

#: the brief kind of a docs-only PR's review (:func:`light_review`): a short checklist
LIGHT_REVIEW = 'light-review'

#: the floor's brief kind of a fix round (the round answers a correction: findings or a red)
FIX_KIND = 'correct'
#: the document lanes' launch kinds: one launched on its open PR is a fix round (B-82960)
DOC_KINDS = ('spec', 'plan')
#: what a document fix round answers (the brief keeps its spec/plan kind; this heads the extra)
DOC_ROUND = ('## This is a fix round on PR #{pr} (head `{head}`)\n\n'
             'The document on `{branch}` is already written and its pull request is open: do not '
             'write it again. Answer {correction}\n\n'
             'Change only what the findings ask, keep the rest of the document as it stands, and '
             'push the same branch; the PR stays open and is reviewed again on its new head.')
#: the least room the operator's answers keep in a brief whose findings fill the cap.
FINDINGS_ANSWERS_FLOOR = 500

VERDICT_RULE = """## The kernel's verdict lines

The kernel reads this review's verdict from your final message, not from the review file. After
the REPORT block, print exactly one line `VERDICT: approve` or `VERDICT: changes`, then one line
`FINDINGS: <file:line — the exact fix>` per finding the author must answer (none for an approve),
and nothing after them — this overrides "nothing after it" above. In a CLOUD SESSION your report
commit's body (on its own ref, as the CLOUD block says — never the PR branch) is that final
message: the same lines go there, after the REPORT. A review that ends without a
`VERDICT:` line counts as no review and is run again."""

#: what a review does about tests: CI is the test gate and its result is a fact in the brief; the
#: reviewer reads the diff against the acceptance and the spec (a review that reran the touched
#: suite on a busy host ran 85 minutes, 65 of them waiting on it)
CI_RULE = """## CI is the test gate — do not run the suite

{ci_fact}

Do NOT run the full or the touched test suite (`run_tests.py`, `--touched`, `unittest discover`,
`pytest` over a directory, `make test`): CI already did, on this exact head, and a rerun on this
host only duplicates it and holds your seat. Your review is reading the diff against the
acceptance and the spec. Run at most one or two specific test cases, and only to check a doubt
the diff leaves, each under a short timeout (`timeout 120 …`); say which and why in the evidence."""

#: the floor's review-table rows that ask for a test run, and what a kernel review checks instead
_TEST_ROW_REWRITES = (
    (re.compile(r'^\| those tests were run and are green \|.*\|$', re.M),
     '| the acceptance tests are green in CI on this head | pass \\| fail '
     '| the CI check and its URL, from the CI section |'),
    (re.compile(r'^\| the Gate commands are green \|.*\|$', re.M),
     '| the Gate commands are green in CI on this head | pass \\| fail '
     '| the CI check and its URL, from the CI section |'),
)

#: a docs-only PR's review (:func:`light_review`): the kernel's own short brief, on the light model
LIGHT_REVIEW_TEXT = """# Light review: {item_id} — PR #{pr} (documentation only)

{title_line}You review PR #{pr} on branch `{branch}` (head `{head}`). Every file it changes is
documentation:
{files}

Read the diff: `git fetch origin {branch} {main} && git diff origin/{main}...origin/{branch}`.
Check only this, and keep it short:
- it does what the card asks, and nothing the card does not;
- every fact it states — a name, command, path, key, link or number — is true in this repository;
- it contradicts no document beside it;
- no secret, private name or host detail;
- it renders: headings, lists, code fences and tables are well formed.

{ci_rule}

Never edit, commit or push anything here (a cloud session's one report commit, on its own ref,
excepted): you only read. Block only on a wrong fact, a contradiction or a leak; wording is a
note, not a finding.

Finish with this, and nothing after it but the verdict lines below:

```
REPORT
item: {item_id}
kind: review
status: done
branch: {branch}
pushed: n/a — a light review
commits: none
tests: none — documentation
left out: none
```
"""

#: a groom-fill session's brief (:func:`_groom_brief`): the kernel's own, short, on the light model
GROOM_FILL_TEXT = """# Groom-fill: {item_id} — {title}

The kernel holds {item_id} New: its card does not meet the Definition of Ready.
What is missing: {missing}

You only read and judge — never edit, commit or push anything. Read the card below and the
repository at `origin/{main}`, then decide one verdict:
- `proceed`: the card is ready as it stands (say why);
- `superseded`: the work already landed, or another card covers it — name that card's id or the
  trunk commit's sha in `superseded_by`;
- `fill`: write the acceptance lines, each naming the test that proves it
  (`<line> — tests/<file>.py::<Test>`), and the `writes` (repo paths; one that does not exist
  yet ends ` (new)`);
- `reshape`: the same two fields, when the card's scope must change to be buildable.
Name only tests and paths that exist on the trunk or that the writes create. `risk_raise: high`
when the change touches the record, CI, the kernel or the release tooling.

## The card

- type: {type}; parent: {parent}; after: {after}
- writes: {writes}
- creates: {creates}

{body}

End with exactly this block (lists as JSON), and nothing after it:

```
{schema}
```
"""

#: an intake-decide session's brief (:func:`_intake_brief`): the kernel's own, short, light model
INTAKE_TEXT = """# Intake decision: {item_id} — {title}

{what} waits on a decision before the factory treats it as work. Decide it.

You only read and judge — never edit, commit or push anything, and run no `asf` command.
Read the {noun} below; read the repository at `origin/{main}` only when the text alone does not
settle it.

- `decision`: `need` (build it soon), `nice` (worth building, not urgent), `later` (park it), or
  `close` (not worth building: a duplicate, already done, or obsolete).
- The rule: work made obsolete under the 0.2 kernel (it fixes or extends the old floor's tick,
  harvest, lanes, feeder rows or groom file, which the kernel replaced) ⇒ `close` or `later`.
- `kind`: `bug` when it reports a defect in what exists, `feature` when it asks for new work.
- `parent`: the id it hangs under — a feature under an Epic, a bug under an Epic, a Feature or a
  Story — chosen from the goals below or the card's own; `none` keeps the card's.
- `severity` (bugs only): `S1` the factory stops or loses work, `S2` a wrong result with a
  workaround, `S3` cosmetic.

## The product's goals (its open Epics, by rank)

{goals}

## The {noun}

- type: {type}; parent: {parent}; priority: {priority}{question}

{body}

End with exactly this block, and nothing after it:

```
{schema}
```
"""

#: the most characters of a card's body a groom-fill brief quotes
GROOM_BODY_MAX = 6000

#: what a kernel session does with a rebase: its sandbox refuses a force-push, so the host
#: publishes it (``--force-with-lease`` over origin's tip, only when that tip is in the branch's
#: own history) at any session end
HOST_REBASE = ('Do not force-push yourself; commit the rebased branch locally and end with REPORT '
               '`status: done`, `pushed: rebased <sha>`; the host publishes it.')

#: the kernel's push rule: a session pushes its own branch fast-forward; a rebase is published by
#: the host (B-84832). Appended to every kernel brief; overrides the floor's text above.
PUSH_RULE = """## Pushing (the kernel's rule — it overrides anything above)

You push your own branch fast-forward: your last act is `git push origin {branch}`. After a rebase
(onto `origin/{main}`, or a push refused as non-fast-forward because the branch was rebased) —
never a merge of `origin/{main}` into it, never a `--force` of any kind: %s
Committed work the host can publish is never lost; uncommitted work is.""" % HOST_REBASE

#: the floor's "the factory publishes a rebase" wording, and what a kernel brief says instead
_FLOOR_REWRITES = (
    # the TAIL's lane paragraph: refused push -> stop, the factory publishes
    (re.compile(r'If that is refused as\s+non-fast-forward, the rebase is why:.*?'
                r'a refused push is never a `NEEDS OPERATOR`\.', re.S),
     'If that is refused as non-fast-forward, the rebase is why: never merge. ' + HOST_REBASE +
     ' Landing is the factory\'s: never run `asf land` or any other `asf` command to publish, '
     'and a refused push is never a `NEEDS OPERATOR`.'),
    # correct.md: refused push -> stop and report the factory publishes
    (re.compile(r'A push refused as non-fast-forward is the rebase you were handed: stop there and '
                r'report `pushed: rebased <sha> — the factory publishes`\.'),
     'A push refused as non-fast-forward is the rebase you were handed: ' + HOST_REBASE),
    # the REPORT line's alternative
    (re.compile(r'rebased <sha> — the factory publishes'), 'rebased <sha> — the host publishes it'),
    # anything else of the floor's in the same vein
    (re.compile(r'the factory publishes the (rebased |rewritten )?branch'),
     r'the host publishes the \1branch'),
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
    kernel's rule (:data:`HOST_REBASE`), the floor's heartbeat wording dropped, and
    :data:`PUSH_RULE` appended: a kernel session pushes its own branch, the host its rebase."""
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


def kernel_block(product):
    """``product``'s ``kernel:`` block, every default filled in (the defaults for a stand-in)."""
    k = getattr(product, 'kernel', None)
    return k if isinstance(k, dict) else kernel_settings.read(None)


def model_for(product, kind):
    """``kernel.models.<kind>`` (:data:`asf.kernel.settings.MODEL_KINDS`): a model id, or a
    ``worker_pool.models`` label; '' for a kind it does not name (the floor's label stands)."""
    return str((kernel_block(product).get('models') or {}).get(kind) or '')


def light_review(product, pr):
    """Whether ``pr`` (a :class:`~asf.kernel.model.PR`) changes documentation only: every file
    under ``kernel.review.light_paths`` or the product's document trees
    (:func:`asf.kernel.ports.doc_globs`). A PR whose files are unread is not."""
    files = list(getattr(pr, 'files', None) or ())
    if not files:
        return False
    from asf.kernel.ports import doc_globs
    try:
        docs = list(doc_globs(product))
    except AttributeError:  # a stand-in product with no conventions
        docs = []
    globs = list(kernel_block(product)['review']['light_paths']) + docs
    return all(any(fnmatch.fnmatchcase(f, g) for g in globs) for f in files)


def cap_sections(sections, limit):
    """``(kept, dropped)``: ``sections`` (``[[entry, …], …]``, each list oldest first) cut to at
    most ``limit`` characters in all, keeping the newest entries — the last of every section
    first, then the one before it, and so on; one entry longer than what is left is cut short.
    ``kept`` has the shape of ``sections``; ``dropped`` counts the entries left out or cut.
    ``limit`` 0 keeps everything."""
    sections = [list(sec) for sec in sections]
    if not limit or sum(len(str(e)) for sec in sections for e in sec) <= limit:
        return sections, 0
    keep = [[] for _ in sections]
    left, dropped = limit, 0
    for depth in range(max(len(sec) for sec in sections)):
        for n, sec in enumerate(sections):
            if depth >= len(sec):
                continue
            entry = str(sec[-1 - depth])
            if len(entry) <= left:
                keep[n].insert(0, entry)
                left -= len(entry)
            elif left > 200 and not keep[n]:  # the newest of a section is never lost whole
                keep[n].insert(0, entry[:left - 1] + '…')
                left, dropped = 0, dropped + 1
            else:
                dropped += 1
    return keep, dropped


def ci_fact(product, pr):
    """The CI result on ``pr``'s head as one fact: ``CI is green on head `<sha>`: <check> <run
    URL>, …`` (the run URL from the product's ``repo_slug`` and the check's run id), or what is
    not green. The kernel launches a review only on green (:func:`asf.kernel.decide.ci_green`)."""
    slug = getattr(product, 'repo_slug', '') or ''
    checks = list(getattr(pr, 'checks', None) or ())
    done = [c for c in checks if c.status == 'completed']
    bad = [c for c in checks if c not in done or c.conclusion in RED_CONCLUSIONS]
    parts = ['%s — %s' % (c.name, 'https://github.com/%s/actions/runs/%s' % (slug, c.run_id)
                          if slug and c.run_id else 'run url unknown') for c in done if c not in bad]
    head = getattr(pr, 'head_sha', '') or '?'
    if bad or not checks:
        return ('CI is NOT confirmed green on head `%s` (%s): trust no result, and say so in the '
                'review.' % (head, ', '.join('%s %s' % (c.name, c.conclusion or c.status)
                                              for c in bad) or 'no checks reported'))
    return 'CI is green on head `%s`:\n%s' % (head, '\n'.join('- %s' % p for p in parts))


def _light_brief(product, launch, item, pr, model):
    """The :class:`asf.briefs.build.Brief` of a docs-only PR's review: :data:`LIGHT_REVIEW_TEXT`,
    the operator's answers and :data:`VERDICT_RULE`."""
    floor = importlib.import_module('asf.briefs.build')
    files = list(pr.files)
    shown = ['- `%s`' % f for f in files[:30]] + (
        ['- … and %d more' % (len(files) - 30)] if len(files) > 30 else [])
    text = LIGHT_REVIEW_TEXT.format(
        item_id=item.id, pr=pr.number, branch=launch.branch, head=pr.head_sha or '?',
        main=getattr(product, 'main', 'main') or 'main', files='\n'.join(shown),
        title_line=('The card: %s\n\n' % item.title) if item.title else '',
        ci_rule=CI_RULE.format(ci_fact=ci_fact(product, pr)))
    return floor.Brief(kind=LIGHT_REVIEW, item_id=item.id, text=text, model=model, add_dirs=[],
                       id_ranges_needed=False)


def _groom_brief(product, launch, item, findings, model):
    """The :class:`asf.briefs.build.Brief` of a groom-fill session (:data:`GROOM_FILL_TEXT`)."""
    floor = importlib.import_module('asf.briefs.build')
    body = str(item.body or '').strip()
    if len(body) > GROOM_BODY_MAX:
        body = body[:GROOM_BODY_MAX] + '\n…'
    text = GROOM_FILL_TEXT.format(
        item_id=item.id, title=item.title or '', type=item.type, parent=item.parent or 'none',
        after=', '.join(item.after) or 'none', writes=', '.join(item.writes) or 'none',
        creates=', '.join(getattr(item, 'creates', ()) or ()) or 'none',
        missing='; '.join(str(f)[len(D.PREFIX):] if str(f).startswith(D.PREFIX) else str(f)
                          for f in findings) or 'see the card',
        main=getattr(product, 'main', 'main') or 'main', body=body, schema=D.VERDICT_SCHEMA)
    return floor.Brief(kind=D.GROOM_FILL, item_id=item.id, text=text, model=model, add_dirs=[],
                       id_ranges_needed=False)


def _intake_brief(product, launch, item, findings, model):
    """The :class:`asf.briefs.build.Brief` of an intake-decide session (:data:`INTAKE_TEXT`):
    the card (or the inbox note and the question intake asked about it), the product's goals
    (``findings``: :func:`asf.kernel.intake.goals`), the rule and the verdict block."""
    floor = importlib.import_module('asf.briefs.build')
    note = item.type == I.NOTE
    body = str(item.body or '').strip()
    if len(body) > GROOM_BODY_MAX:
        body = body[:GROOM_BODY_MAX] + '\n…'
    text = INTAKE_TEXT.format(
        item_id=item.id, title=item.title or '',
        what=('The inbox note %s' % I.note_name(item.id)) if note else 'The card %s' % item.id,
        noun='inbox note' if note else 'card', main=getattr(product, 'main', 'main') or 'main',
        goals='\n'.join('- %s' % g for g in findings) or '- (no open Epic)',
        type='not minted yet' if note else item.type, parent=item.parent or 'none',
        priority=item.priority or 'none',
        question=('\n- intake asked: %s' % item.question) if note and item.question else '',
        body=body, schema=I.VERDICT_SCHEMA)
    return floor.Brief(kind=I.KIND, item_id=item.id, text=text, model=model, add_dirs=[],
                       id_ranges_needed=False)


def build(product, launch, item, findings=(), pr=None, index=None, repo_facts=None, log=None):
    """The :class:`asf.briefs.build.Brief` for ``launch`` of ``item`` (a kernel Item). ``index``
    is the record's ``index.json`` (``{'items': {...}}``); ``repo_facts`` a callable
    ``(product, row, index) -> dict`` (the floor's :func:`asf.briefs.facts.repo_facts` when
    ``None``; a failure there leaves the git facts out, never the brief)."""
    from asf.feeder.rows import LAUNCH, Row
    floor = importlib.import_module('asf.briefs.build')
    fix = launch.kind == 'build' and pr is not None
    kind = brief_kind(launch, item, fix)
    if kind == I.KIND:
        return _intake_brief(product, launch, item, findings,
                             getattr(launch, 'model', '') or model_for(product, I.KIND))
    if kind == D.GROOM_FILL:
        return _groom_brief(product, launch, item, findings,
                            getattr(launch, 'model', '') or model_for(product, D.GROOM_FILL))
    limit = kernel_block(product)['briefs']['max_appended_chars']
    # The review's findings are never capped: each FINDINGS line reaches the session whole, in its
    # own section (a cloud session cannot read the review report, which lives in the host's
    # session log). The cap trims the operator's answers first, to what the findings leave.
    findings = [str(f) for f in findings]
    spent = sum(len(f) for f in findings)
    room = max(limit - spent, FINDINGS_ANSWERS_FLOOR) if limit else 0
    (answers,), dropped = cap_sections([list(item.answers)], room)
    if dropped:
        (log or print)('brief %s %s: answers capped at %d chars — %d older '
                       'entr%s left out' % (kind, item.id, room, dropped,
                                            'y' if dropped == 1 else 'ies'))
    if limit and spent > limit:
        (log or print)('brief %s %s: findings alone are %d chars, over the %d cap — all kept whole'
                       % (kind, item.id, spent, limit))
    if launch.kind == 'review' and pr is not None and light_review(product, pr):
        b = _light_brief(product, launch, item, pr, getattr(launch, 'model', '')
                         or model_for(product, LIGHT_REVIEW))
        extra = (['## Operator answers', ''] + ['- %s' % a for a in answers] + ['']
                 if answers else []) + [VERDICT_RULE]
        return dataclasses.replace(b, text=b.text.rstrip('\n') + '\n\n'
                                   + '\n'.join(extra).strip() + '\n')
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
                                                     getattr(product, 'main', 'main') or 'main'),
                            model=getattr(launch, 'model', '') or model_for(product, kind)
                            or b.model)
    extra = []
    if launch.kind in DOC_KINDS and pr is not None:
        extra += [DOC_ROUND.format(pr=pr.number, head=pr.head_sha or '?', branch=launch.branch,
                                   correction='the review findings below.' if findings
                                   else correction(findings, pr)), '']
    if findings:
        extra += ['## Review findings (every line, in full)', ''] + [
            '- %s' % f for f in findings] + ['']
    if answers:
        extra += ['## Operator answers', ''] + ['- %s' % a for a in answers]
    if launch.kind == 'review':
        if pr is not None:
            extra += ['', 'The PR under review: #%d, head `%s`.' % (pr.number, pr.head_sha)]
            extra += ['', CI_RULE.format(ci_fact=ci_fact(product, pr))]
            for pat, repl in _TEST_ROW_REWRITES:
                b = dataclasses.replace(b, text=pat.sub(lambda m, r=repl: r, b.text))
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

    def __init__(self, product, log=None):
        self.product = product
        self._index = None
        self.log = log

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
        return build(self.product, launch, item, findings, pr, index=self.index(), log=self.log)
