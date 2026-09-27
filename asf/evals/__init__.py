"""asf.evals — the factory's frozen measurement instrument.

A **lever** is one judged surface reported on its own line: the matcher, the brief kind
detector, one enforced rule check. A **task** is one JSON file under a lever's own directory
in ``evals/``: one input (``given``), one expected firing condition (``want``), one polarity
(``expect``: ``fire`` or ``no-fire``), and one ``why`` a human can read a failure against.

The one rule that separates this package from ``tests/``: an eval task is never edited to make
a reading pass. A task that turns out to be wrong is deleted and a new one is added in its
place — the set's hash changes, which is the record of a new instrument, never a repaired old
one. Everything in :mod:`asf.evals.set` exists to make that edit either impossible in one
commit (the freeze, ``asf/evals/freeze.py``) or visible as a changed hash.
"""
