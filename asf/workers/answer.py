"""``asf answer <job|item> --text TEXT | --file PATH`` — the operator answers a session that
stopped with a question (the tick log's ``INPUT <job> needs input — …`` row).

Before this verb the only route was ``asf correct --why``, which spends a correction round and is
refused while the session runs. An answer is neither a correction nor a round:

* it is appended to ``<state>/operator-answers.jsonl`` (``{item, job, text, at, question}``) —
  never to the session ledger, so no run's rounds, findings or loop count move;
* every later brief of the item quotes it (:func:`brief_section`, read by
  :func:`asf.briefs.build.build`) — the next relaunch of the job that asked gets it;
* the question's own park — a ``blocked`` end, or a relaunch cap whose report asked (see
  :func:`asf.tick.rejudge.asked`) — is released the way ``asf unpark`` releases it, so the item
  relaunches with the answer; any other park stands;
* the job's ``INPUT`` row is acknowledged (``input_flagged``): the question is answered;
* a session still running is no refusal: the answer waits for the next launch.
"""
import datetime
import json
import os

from asf import env
from asf.workers import lifecycle
from asf.workers import pool as pool_mod

#: The file the answers are kept in, under ``env.state_dir(product)``.
ANSWERS_FILE = 'operator-answers.jsonl'
#: The most answers one brief quotes (the newest).
MAX_IN_BRIEF = 5
#: The head the brief's answers open with.
HEAD = ('OPERATOR ANSWER — an earlier run of this item stopped with a question, and the '
        'operator answered it. Act on the answer; do not ask the same question again:')


def answers_path(product):
    return os.path.join(env.state_dir(product), ANSWERS_FILE)


def answers(product, item):
    """Every answer recorded for ``item``, oldest first."""
    out = []
    try:
        with open(answers_path(product), encoding='utf-8') as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get('item') == item and rec.get('text'):
                    out.append(rec)
    except OSError:
        return []
    return sorted(out, key=lambda r: r.get('at') or '')


def brief_section(product, item):
    """The brief's ``OPERATOR ANSWER`` section for ``item`` — '' when none was given."""
    if product is None or not item:
        return ''
    recs = answers(product, item)[-MAX_IN_BRIEF:]
    if not recs:
        return ''
    lines = [HEAD]
    for r in recs:
        asked = f' to `{r["job"]}`' if r.get('job') else ''
        lines.append('')
        lines.append(f'- {r.get("at") or ""}{asked}'
                     + (f' — question: {r["question"]}' if r.get('question') else ''))
        lines.append('  answer: ' + '\n  '.join(str(r['text']).strip().splitlines()))
    return '\n'.join(lines)


def _question(run):
    """The question ``run``'s report asked, or ''."""
    from asf.workers import report as report_mod
    from asf.workers import runtime as runtime_mod
    try:
        rec = runtime_mod.read_result((run or {}).get('log'))
        return report_mod.needs_input(str((rec or {}).get('result') or '')) or ''
    except Exception:  # noqa: BLE001 — an unreadable log asked nothing we can quote
        return ''


def _question_park(corr):
    from asf.tick import rejudge
    return rejudge.asked(corr)


def _text(args):
    text = str(getattr(args, 'text', '') or '').strip()
    path = getattr(args, 'file', None)
    if path:
        with open(os.path.expanduser(path), encoding='utf-8') as f:
            text = (text + '\n\n' if text else '') + f.read().strip()
    return text


def cmd_answer(args):
    from asf.workers.unpark import _target
    product = env.load_product(getattr(args, 'product', None))
    try:
        text = _text(args)
    except OSError as e:
        print(f'asf answer: cannot read --file: {e}')
        return 2
    if not text:
        print('asf answer: --text or --file is required — it is the answer the session is given')
        return 2
    path = pool_mod.sessions_path(product)
    target = (args.target or '').strip()
    scope, item, _branch, job = _target(path, target)
    if scope is None or not any(r.get('item') == item
                                for rs in lifecycle.runs(path).values() for r in rs):
        print(f'asf answer: {target} names no job or item in the ledger — nothing asked')
        return 1
    latest = lifecycle.latest(path)
    mine = [r for r in latest.values() if r.get('item') == item and (not job or r.get('job') == job)]
    asker = max(mine, key=lambda r: r.get('started') or '') if mine else {}
    question = _question(asker)
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    rec = {'item': item, 'job': job or asker.get('job') or '', 'text': text, 'at': now,
           'question': question}
    os.makedirs(os.path.dirname(answers_path(product)), exist_ok=True)
    with open(answers_path(product), 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, sort_keys=True) + '\n')
    released = []
    for r in mine:
        corr = lifecycle.pending_correction(r, path)
        if corr and _question_park(corr):
            pool_mod.update_session(product, r['job'], correction=None, unparked=now,
                                    unpark_why=f'answered: {" ".join(text.split())[:200]}')
            released.append(r['job'])
        if question and not r.get('input_flagged'):
            pool_mod.update_session(product, r['job'], input_flagged=1)
    print(f'answered {item}' + (f' (job {rec["job"]})' if rec['job'] else '')
          + (f' — question: {question}' if question else '')
          + ': the next launch of the item gets it in its brief; no correction round spent'
          + (f'; released the question park on {", ".join(released)}' if released else ''))
    return 0


def register(sub):
    """``asf answer <job|item> (--text TEXT | --file PATH) [--product P]``."""
    p = sub.add_parser('answer', help='answer a session that stopped with "needs input" (an INPUT '
                                      'row): the next launch of the item gets the answer in its '
                                      'brief; no correction round is spent')
    p.add_argument('target', help='the job that asked (an INPUT row names it), or its item id')
    p.add_argument('--text', help='the answer')
    p.add_argument('--file', help='a file holding the answer (appended after --text)')
    env.add_product_arg(p)
    p.set_defaults(func=cmd_answer)
    return p
