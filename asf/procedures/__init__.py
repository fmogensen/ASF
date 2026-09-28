"""asf.procedures — the procedures a brief cites rather than repeats."""
import os

#: The four skill directories under ``skills/``, in the order the card lists them.
_NAMES = ('review-standard', 'pre-push-gate', 'spec-shape', 'report-shape')


def plugin_dir():
    """The package's own directory — what ``--plugin-dir`` is pointed at."""
    return os.path.dirname(os.path.abspath(__file__))


def skill_names():
    """The four procedure skill names."""
    return _NAMES
