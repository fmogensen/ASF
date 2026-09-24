## Your job: interrogate the idea, and write one tree file

The ask: {reason}

THE ONE FILE YOU WRITE IS `{tree_path}`. This session has no branch, no worktree and nothing to
push — the push rules under the heartbeat do not apply to you. Do not commit, do not edit a card,
do not file anything: the tree is read by `asf idea apply`, which files the cards and runs no model
of its own.

READ THE RECORD FIRST. The preamble above names the Epics and Features the record already holds;
read the ones the ask touches (`asf` over the record, or the cards themselves) before you propose a
node. A node the record already carries is answered by the record — it is filed for nobody, so
write only what is new.

THE RULE IS: never ask — propose. There is no question section: an unsettled point is an `### Assumptions`
bullet carrying the answer you propose, and the operator accepts it with one word. A `###
Questions` section is refused and the whole tree with it.

THE SHAPE is one file: `# The ask` and its paragraph, then one block per node, blocks separated by
a line that is exactly `---`:

    # The ask
    <the ask, verbatim>

    ---

    ## Epic E1: <title>

    <what it is, in a paragraph>

    ### Assumptions
    - <a proposal, with why>

    ---

    ## Feature F1 under E1: <title>

    <a description of at least forty words: what a spec is written from>

    ### Acceptance
    - [ ] <a command or an observable result>

    ---

    ## Story S1 under F1: <title>

    ### Acceptance
    - [ ] <a test named by its full path>

The node header is `## <Epic|Feature|Story> <key> [under <key>]: <title>`. A key is unique and an
`under` names a key defined above it. An Epic is a business outcome spanning several Features
(two at least), a Feature is one spec and one plan landing as one deployable thing and hangs under
an Epic, a Story is one PR with one acceptance list (at least one item) and hangs under a Feature.

WHEN THE ASK IS ONE CARD TO ENRICH — the card `{item_id}`, when this brief names one above — the
tree is exactly one node: a `## Feature F1: <title>` block with no `under`, carrying the
description, the `### Acceptance` items and the `### Assumptions` the card lacks. It is rooted on
that card; the command adds to it and rewrites nothing of what it says.

Final message: the path `{tree_path}` you wrote, how many nodes it holds, and how many
assumptions carry a proposal. When the factory started this session, your last act is
`asf idea apply --tree {tree_path}`.
