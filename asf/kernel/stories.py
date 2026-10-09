"""asf.kernel.stories — the Stories a spec declares, and nothing else (ASF 0.2).

A spec declares a Story in its Stories section (the ``## Stories`` heading, to the next heading of
the same or a higher level) in one of two forms:

- a heading ``### S-<digits>: <title>``, its acceptance lines the bullets beneath it up to the next
  heading;
- a bullet ``- S-<digits>: <title>``, its acceptance lines the bullets nested beneath it.

The id grammar is :data:`asf.record.core.ID_DIGITS`. Only these declarations count: an id named in
prose, anywhere else in the spec or inside a fenced code block (even within the Stories section),
is never declared, and so never minted or checked.
"""


def declared_stories(spec_text):
    """``{story_id: {'title': str, 'acceptance': [str, ...]}}`` for every Story ``spec_text``
    declares, in declaration order; ``{}`` when it has no Stories section."""
    raise NotImplementedError('asf.kernel.stories: phase 2b')
