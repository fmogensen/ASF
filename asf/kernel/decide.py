"""asf.kernel.decide — the one pure decision of the kernel (ASF 0.2).

``decide(facts, config) -> Plan`` reads nothing but its arguments and returns the state of every
item and the actions of one tick. The rules it holds, in the design's words:

- Launch: Ready items in rank order alone, while ``config.max_sessions`` allows; ``facts.paused``
  means no :class:`Launch` at all. An item whose own errors repeat is Stuck; the lane never is.
- Waits: only a declared ``after:`` edge between two non-``later`` items, and "two Building items
  with overlapping ``writes``: one at a time". A ``priority: later`` item is invisible.
- Review: one reviewer per PR head; the verdict is keyed by the head's tree, so a rebase with the
  same tree keeps it. ``changes`` sends the item to Ready with the findings. A PR on a document
  branch (``config.doc_branches``) that changes a path outside ``config.doc_paths`` gets a review
  like any other.
- Landing: an approved PR gets :class:`EnableAutoMerge`; a behind one :class:`UpdateBranch`.
  Every open PR's item is in Review or Landing (or Ready on a fix round, or Stuck) — never stateless.
- Red: only ``failure``/``timed_out``. A red whose ``failing_files`` meet none of the PR's files
  gets ``config.max_reruns`` reruns, then Stuck(owner=ci). A red meeting the PR's files sends it to
  Ready (a fix round), at most ``config.max_fix_rounds``, then Stuck.
- Stuck: ``config.max_attempts`` failed attempts on one reason; a conflict the update cannot
  resolve; a session question (owner=operator); a session that ended without a push
  (owner=session, its last line as reason). A dead pid ends the session and frees its worktree.
- Record: a landed spec's declared Stories (:func:`asf.kernel.stories.declared_stories`) not on
  the record are minted; pending answers are applied at once.
"""


def decide(facts, config):
    """Return the :class:`asf.kernel.actions.Plan` for ``facts`` (:class:`asf.kernel.model.Facts`)
    under ``config`` (:class:`asf.kernel.model.Config`). Pure: same arguments, same plan."""
    raise NotImplementedError('asf.kernel.decide: phase 2b')
