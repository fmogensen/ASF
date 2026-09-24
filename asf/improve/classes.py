"""asf.improve.classes — the four classes of waste, their thresholds, and the rank their excess gives.

A class is a name, a default threshold, a measurement over :func:`asf.improve.measure.table`, and
one sentence with the number in it. A product overrides any default under ``improve:`` in its yaml
(``improve: {thresholds: {non_landing_sessions: 0.30}, epic: E-0001, window_days: null,
premium_models: [...]}``); a key it leaves out falls back to the default in this module.

:func:`evaluate` returns one record per class — over threshold or not, so the page can print the
distance of the ones that are not — ordered by ``excess_usd``, the spend the excess covers.
Pure: no file, no clock.
"""
import collections
import dataclasses

DEFAULT_PREMIUM_MODELS = ('claude-opus-5',)

REPEAT_SESSIONS = 'repeat_sessions'
TIME_PER_LANDED_ITEM = 'time_per_landed_item'
PREMIUM_MODEL_IN_KIND = 'premium_model_in_kind'
NON_LANDING_SESSIONS = 'non_landing_sessions'


@dataclasses.dataclass(frozen=True)
class Settings:
    """A product's ``improve:`` block with the defaults filled in."""
    thresholds: dict
    epic: str | None
    window_days: int | None
    premium_models: tuple


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def settings(improve=None):
    """``improve`` is the ``improve:`` mapping, a :class:`asf.env.Product` (its ``improve``), or
    ``None``. A threshold naming no class, or not a number, is ignored; so is a ``premium_models``
    that is not a list."""
    if improve is not None and not isinstance(improve, dict):
        improve = getattr(improve, 'improve', None)
    improve = improve if isinstance(improve, dict) else {}
    thresholds = {c.name: c.threshold for c in CLASSES}
    given = improve.get('thresholds')
    for name, value in (given.items() if isinstance(given, dict) else ()):
        if name in thresholds and _number(value):
            thresholds[name] = value
    models = improve.get('premium_models')
    window = improve.get('window_days')
    return Settings(
        thresholds=thresholds,
        epic=str(improve['epic']) if improve.get('epic') else None,
        window_days=window if isinstance(window, int) and not isinstance(window, bool) else None,
        premium_models=tuple(str(m) for m in models) if isinstance(models, list) else DEFAULT_PREMIUM_MODELS)


def premium_cell(table, premium_models=DEFAULT_PREMIUM_MODELS):
    """``(kind, model, cell)`` of the largest ``by_kind_model`` cell, by hours, whose model is
    premium — ``None`` when there is none."""
    cells = [(cell.hours, kind, model, cell)
             for (kind, model), cell in table['by_kind_model'].items() if model in premium_models]
    if not cells:
        return None
    _, kind, model, cell = max(cells, key=lambda c: (c[0], c[1], c[2]))
    return kind, model, cell


def _usd_per_hour(table):
    return table['usd'] / table['hours'] if table['hours'] else 0.0


# measure(table, cfg) -> the number; excess(table, measured, threshold, cfg) -> USD;
# sentence(table, measured, threshold, cfg) -> one sentence with the number in it;
# closes(threshold) -> the number that would close a card, for its Acceptance.

def _repeat_measure(table, cfg):
    return table['repeat_items']['usd_share']


def _repeat_excess(table, measured, threshold, cfg):
    return (measured - threshold) * table['usd']


def _repeat_sentence(table, measured, threshold, cfg):
    return (f"{measured * 100:.0f} % of spend went on items that took 3 or more sessions "
            f"({table['repeat_items']['count']} items; threshold {threshold * 100:.0f} %)")


def _time_measure(table, cfg):
    return table['minutes_per_landed_item'] or 0.0


def _time_excess(table, measured, threshold, cfg):
    return (measured - threshold) * table['landed_items'] / 60 * _usd_per_hour(table)


def _time_sentence(table, measured, threshold, cfg):
    return f"a landed item takes {measured:.1f} minutes of session time (threshold {threshold:g})"


def _premium_measure(table, cfg):
    cell = premium_cell(table, cfg.premium_models)
    return cell[2].hours if cell else 0.0


def _premium_excess(table, measured, threshold, cfg):
    cell = premium_cell(table, cfg.premium_models)
    return (measured - threshold) * cell[2].usd_per_hour if cell else 0.0


def _premium_sentence(table, measured, threshold, cfg):
    cell = premium_cell(table, cfg.premium_models)
    where = f'{cell[0]} × {cell[1]}' if cell else 'no premium model'
    return f"{where}: {measured:.1f} h (threshold {threshold:g} h)"


def _non_landing_measure(table, cfg):
    return table['non_landing_share']


def _non_landing_excess(table, measured, threshold, cfg):
    return (measured - threshold) * table['usd']


def _non_landing_sentence(table, measured, threshold, cfg):
    return f"{measured * 100:.0f} % of session time landed nothing (threshold {threshold * 100:.0f} %)"


Class = collections.namedtuple('Class', 'name threshold measure sentence excess closes')

CLASSES = (
    Class(REPEAT_SESSIONS, 0.33, _repeat_measure, _repeat_sentence, _repeat_excess,
          lambda t: f'repeat_items usd_share below {t}, measured over a full week'),
    Class(TIME_PER_LANDED_ITEM, 120, _time_measure, _time_sentence, _time_excess,
          lambda t: f'minutes_per_landed_item below {t:g}, measured over a full week'),
    Class(PREMIUM_MODEL_IN_KIND, 8.0, _premium_measure, _premium_sentence, _premium_excess,
          lambda t: f'hours of the largest premium-model kind below {t:g}, measured over a full week'),
    Class(NON_LANDING_SESSIONS, 0.25, _non_landing_measure, _non_landing_sentence,
          _non_landing_excess,
          lambda t: f'non_landing_share below {t}, measured over a full week'),
)


def excess_usd(name, table, conv_improve=None):
    """The spend a class's excess over its threshold covers — ``0.0`` when it is not over."""
    for c in CLASSES:
        if c.name == name:
            cfg = settings(conv_improve)
            measured, threshold = c.measure(table, cfg), cfg.thresholds[name]
            return round(max(0.0, c.excess(table, measured, threshold, cfg)), 2) if measured > threshold else 0.0
    raise KeyError(name)


def evaluate(table, conv_improve=None):
    """One record per class, ``excess_usd`` descending (ties keep the catalogue's order): ``name,
    measured, threshold, over, excess_usd, sentence, closes, rank``. ``over`` is strictly greater
    than the threshold; ``rank`` is the position × 10."""
    cfg = settings(conv_improve)
    records = []
    for c in CLASSES:
        measured, threshold = c.measure(table, cfg), cfg.thresholds[c.name]
        over = measured > threshold
        records.append({
            'name': c.name, 'measured': measured, 'threshold': threshold, 'over': over,
            'excess_usd': round(max(0.0, c.excess(table, measured, threshold, cfg)), 2) if over else 0.0,
            'sentence': c.sentence(table, measured, threshold, cfg), 'closes': c.closes(threshold)})
    records.sort(key=lambda r: -r['excess_usd'])
    for i, r in enumerate(records, 1):
        r['rank'] = i * 10
    return records
