"""asf.scorecard — the standing value loop: measure what a shipped Feature cost, diagnose where
cost and time go, file one card per cause over its threshold, verify the card moved its number.

* :mod:`asf.scorecard.facts`    — the facts, read from the record, its metric streams, the session
  registry and (optionally) the forge; the only module that touches a file or a network.
* :mod:`asf.scorecard.score`    — per-Feature rows, weekly rows, the headline. Pure.
* :mod:`asf.scorecard.diagnose` — the rankings and the causes over threshold. Pure.
* :mod:`asf.scorecard.loop`     — snapshots, filing, verifying, the daily part and the command.
"""
