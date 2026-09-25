"""asf.views.capacity — the ``CAPACITY`` table (``asf capacity``) and its ``--json`` twin.

One row per product, each built from :func:`asf.capacity.resolve` — the resolver is the only
reader of the raw ``capacity:`` keys (spec f-0079 §2.2); this module only formats what it
returns. ``?`` marks an unknown count (an unconfigured ceiling, or an unreadable CI source);
``sessions`` is the effective ceiling — bounded by the product's fair share of the usable pool when
more than one product's wave is active (``bound by`` then names the share); ``free`` is
``max(0, ceiling - inflight)``, ``?`` when either side is unknown. ``ci from`` names where the CI
ceiling came from: ``product`` (``capacity.ci``), ``ci.pool: heavy 12, light 7`` (the declared
runner pool's slots), ``product (overrides ci.pool 19)``, ``operator default`` or ``operator total``. The table is
script-generated (R-0109), never hand-typed.
"""
import json

from asf import capacity, env

HEADERS = ('product', 'sessions', 'in flight', 'free', 'bound by', 'ci', 'runs', 'free', 'ci from',
           'batch')


def _fmt(n):
    return '?' if n is None else str(n)


def _free(ceiling, inflight):
    return None if ceiling is None or inflight is None else max(0, ceiling - inflight)


def _batch_cell(batch):
    """``<per_run> per run × <parallel> parallel on <runners> runners`` — a key ``batch`` does
    not carry drops its clause; an empty ``batch`` prints ``—``."""
    parts = []
    if 'per_run' in batch:
        parts.append(f"{batch['per_run']} per run")
    if 'parallel' in batch:
        parts.append(f"{batch['parallel']} parallel")
    text = ' × '.join(parts)
    if 'runners' in batch:
        clause = f"on {batch['runners']} runners"
        text = f"{text} {clause}" if text else clause
    return text or '—'


def _row(product, cfg):
    r = capacity.resolve(product, cfg)
    inflight = capacity.inflight_sessions(product.name)
    row = {
        'product': product.name,
        'sessions': {'ceiling': r.sessions, 'inflight': inflight,
                     'free': _free(r.sessions, inflight), 'bound_by': r.sessions_bound},
        'ci': {'ceiling': r.ci, 'inflight': r.ci_inflight,
               'free': _free(r.ci, r.ci_inflight), 'bound_by': r.ci_bound},
        'batch': r.batch,
    }
    if r.fair_share is not None:  # only when the share bounds the ceiling: the shape is stable
        row['sessions'].update(configured=r.ceiling, fair_share=r.fair_share, usable=r.usable,
                               active_products=r.active)
    return row


def _table(headers, rows):
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) if rows else len(str(h))
              for i, h in enumerate(headers)]

    def fmt(cells):
        last = len(cells) - 1
        return '  '.join(str(c) if i == last else str(c).ljust(widths[i]) for i, c in enumerate(cells))

    return [fmt(headers)] + [fmt(r) for r in rows]


def render(products, cfg):
    dep = capacity.deprecations(cfg)
    header = (f"CAPACITY — operator total: sessions {_fmt(capacity.total_sessions(cfg))}, "
             f"ci {_fmt(capacity.total_ci(cfg))}")
    rows = []
    for p in products:
        row = _row(p, cfg)
        s, ci = row['sessions'], row['ci']
        bound = s['bound_by']
        if s.get('fair_share') is not None:
            bound = (f"fair share of {s['usable']} usable / {s['active_products']} products "
                     f"(configured {s['configured']})")
        rows.append((row['product'], s['ceiling'], s['inflight'], _fmt(s['free']), bound,
                    _fmt(ci['ceiling']), _fmt(ci['inflight']), _fmt(ci['free']),
                    ci['bound_by'] or '—', _batch_cell(row['batch'])))
    lines = [header, ''] + _table(HEADERS, rows)
    if dep:
        lines.append('')
        lines += [f'deprecated: {d}' for d in dep]
    return '\n'.join(lines) + '\n'


def as_json(products, cfg):
    dep = capacity.deprecations(cfg)
    return [dict(_row(p, cfg), deprecated=dep) for p in products]


def _products_for(args, cfg):
    if getattr(args, 'all', False):
        from asf import schema
        return [env.load_product(name) for name in schema._all_products()]
    return [env.load_product(getattr(args, 'product', None))]


def cmd_capacity(args):
    cfg = env.load_config()
    products = _products_for(args, cfg)
    if getattr(args, 'json', False):
        print(json.dumps(as_json(products, cfg), indent=2, ensure_ascii=False))
    else:
        print(render(products, cfg), end='')
    return 0
