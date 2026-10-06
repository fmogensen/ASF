"""asf.workers.loops — the loop guard's two rules past the relaunch cap.

The relaunch cap (:mod:`asf.workers.relaunch`) parks a job handed the same head, card and cause
twice. A job whose head keeps moving under it — the lane rebasing a branch onto a newer trunk,
a session committing a no-op — passes that cap on every launch and still says the same thing
each time: a product's correct rows ran 13 times in six hours on seven heads with one report
between them. Two rules close that gap, both read off the session ledger, both pure here:

* **Same report.** The job's last ``same_report`` ended runs since the item's latest unpark
  ended on the same report (:func:`report_key`: the REPORT block with every sha masked and its
  whitespace folded, so a moved head alone does not make a new report), and the card each was
  handed is the card now. The session has said all it is going to say on this card: one park,
  one alarm, until the card changes or ``asf unpark``.
* **Daily cap.** ``daily_cap`` launches of one job (one kind on one item) in the trailing 24 h
  since the latest unpark park the row whatever changed between them — the hard ceiling a loop
  no rule foresaw still stops at.

The two caps are product flags (:meth:`asf.conventions.Conventions.flag`), read here and only
here, by :func:`settings`: ``relaunch_same_report`` (default :data:`SAME_REPORT`) and
``relaunch_daily_cap`` (default :data:`DAILY_CAP`). ``off`` on a rule's flag turns that rule
off; a value that is not a positive integer is its default. (The relaunch cap's own streak
length and the hold's same-head loop guard are config keys, not flags —
``worker_pool.caps.relaunch`` and ``worker_pool.caps.same_head_loop``, read by
:mod:`asf.workers.relaunch` and :mod:`asf.workers.lifecycle` themselves.)
"""
import datetime
import hashlib
import re

#: Ended runs of one job on one card ending on the same report before the row parks.
SAME_REPORT = 2
#: Launches of one job in the trailing 24 h before the row parks, whatever changed between them.
DAILY_CAP = 6
DAY_S = 86400
#: A park's rule names (``loop_rule`` on the park's correction), read by the alarm's dedupe.
SAME_REPORT_RULE, DAILY_RULE = 'same report', 'daily cap'
_REPORT_RE = re.compile(r'^\s*REPORT\s*$', re.M)
_SHA_RE = re.compile(r'\b[0-9a-f]{7,40}\b')
_OFF = ('off', 'false', 'no', 'none', '0')


def _int_flag(conv, name, default):
    """``flags.<name>`` as a positive int; ``None`` for ``off``; ``default`` otherwise."""
    if conv is None:
        return default
    value = conv.flag(name) if hasattr(conv, 'flag') else None
    if value is None:
        return default
    if value is False or str(value).strip().lower() in _OFF:
        return None
    try:
        n = int(str(value).strip())
    except ValueError:
        return default
    return n if n > 0 else default


def _conv(product_or_conv):
    return getattr(product_or_conv, 'conventions', product_or_conv)


def settings(product):
    """``{'same_report': n|None, 'daily_cap': n|None}`` — this module's two rules' caps."""
    conv = _conv(product)
    return {'same_report': _int_flag(conv, 'relaunch_same_report', SAME_REPORT),
            'daily_cap': _int_flag(conv, 'relaunch_daily_cap', DAILY_CAP)}


def report_key(text):
    """A short digest of the last REPORT block in ``text`` — shas masked, whitespace folded — or
    '' when there is none: two runs that differ only in the head they reported share a key."""
    text = str(text or '')
    heads = list(_REPORT_RE.finditer(text))
    if not heads:
        return ''
    body = text[heads[-1].end():].split('```', 1)[0]
    body = ' '.join(_SHA_RE.sub('<sha>', body).split())
    return hashlib.sha256(body.encode('utf-8')).hexdigest()[:12] if body else ''


def _ts(iso):
    try:
        return datetime.datetime.strptime(str(iso)[:19], '%Y-%m-%dT%H:%M:%S').replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def judge(runs, card='', now=None, same_report=SAME_REPORT, daily_cap=DAILY_CAP):
    """``(rule, reason)`` when the launch about to be made is refused, else ``None``. ``runs``:
    the job's runs since the item's latest unpark (any order), each a ledger dict — ``started``,
    ``ended``, ``card_digest`` and ``report_key`` read. ``card``: the digest the launch's brief
    states; ``now``: epoch seconds (default: the clock)."""
    runs = sorted(runs or (), key=lambda r: r.get('started') or '', reverse=True)
    now = now if now is not None else datetime.datetime.now(datetime.timezone.utc).timestamp()
    if same_report:
        ended = [r for r in runs if r.get('ended')][:same_report]
        keys = {r.get('report_key') or '' for r in ended}
        cards = {r.get('card_digest') for r in ended if r.get('card_digest')}
        if len(ended) >= same_report and len(keys) == 1 and '' not in keys \
                and (not card or cards <= {card}):
            heads = len({(r.get('launch_head') or '')[:9] for r in ended})
            return SAME_REPORT_RULE, (
                f'{len(ended)} runs ended on the same report ({keys.pop()}) across {heads} '
                f'head(s) with the card unchanged — another session would say it again')
    if daily_cap:
        recent = [r for r in runs if (_ts(r.get('started')) or 0) > now - DAY_S]
        if len(recent) >= daily_cap:
            return DAILY_RULE, (
                f'{len(recent)} launches in 24 h, at the daily cap of {daily_cap} '
                f'(flags.relaunch_daily_cap) — whatever changed between them did not finish it')
    return None


def replay(sessions, same_report=SAME_REPORT, daily_cap=DAILY_CAP):
    """The launches :func:`judge` would have refused, over a ledger replayed in time order:
    ``sessions`` are run dicts carrying ``job`` (or ``item`` + ``kind``), ``started``,
    ``card_digest``, ``report_key`` and optional ``unparked``. A refused launch is not added to
    the job's history (it never ran), so what follows is judged on the runs that would have
    happened. Returns ``[(session, rule)]``."""
    hist, out = {}, []
    for s in sorted(sessions, key=lambda r: r.get('started') or ''):
        job = s.get('job') or f"{s.get('kind')}-{s.get('item')}"
        if s.get('unparked'):
            hist[job] = []
        prior = hist.setdefault(job, [])
        got = judge(prior, card=s.get('card_digest') or '', now=_ts(s.get('started')),
                    same_report=same_report, daily_cap=daily_cap)
        if got:
            out.append((s, got[0]))
            continue
        prior.append(dict(s, ended=s.get('ended') or s.get('started')))
    return out
