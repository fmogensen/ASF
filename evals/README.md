# evals — the factory's frozen eval set

This is not the test suite. A test in `tests/` is code the factory maintains: when it is wrong it
is fixed. An eval here is an instrument: a frozen set of tasks, each one an input and the firing
condition a judging tool should meet on it, read by `asf/evals/set.py` and scored as a rate.

**An eval is frozen and scored as a rate.** No single task passes or fails the factory; a lever's
reading is the share of its tasks it gets right, and a reading is comparable with an earlier one
only while the set is the same set.

**A wrong task is deleted and replaced, never edited.** Editing a task to make it pass would
change what is being measured while claiming to measure the same thing. The set's hash is
computed from the tasks' canonical bytes on every read and stored in no file, so a deletion and a
replacement are recorded as what they are: a new instrument.

**A lever with only one polarity measures half an instrument.** Every lever carries at least one
task that must fire and one that must not (`require_both_polarities`), and at least `min_tasks`
tasks in all. A lever that cannot have its pair yet is listed in `exemptions.json` with the reason
and the card that will add it.

## Adding a lever

1. Add an entry to `manifest.json`'s `levers`: its `id`, the `adapter` that runs it, the dotted
   name it `judges`, the paths it `implements`, and the `tasks` directory under `evals/`.
2. Add its tasks under that directory, one JSON file each: `id`, `lever`, `expect` (`fire` or
   `no-fire`), `why`, `given` and `want`. `want` states the firing condition, never the outcome.
3. Give it both polarities, or an entry in `exemptions.json` saying why not and which card will.
