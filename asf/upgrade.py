"""asf.upgrade — ``asf upgrade``: ``pipx upgrade`` the package, then the schema check for every
product, one table: product · record schema · package · action."""
import os
import subprocess

from asf import __version__, env, schema

PACKAGE_NAME = 'asf-factory'


def upgrade_command(package=PACKAGE_NAME):
    return ['pipx', 'upgrade', package]


def products():
    d = os.path.join(env.ASF_HOME, 'products')
    if not os.path.isdir(d):
        return []
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith('.yaml'))


def row(name):
    """``(product, record schema, package, action)`` for one product."""
    try:
        product = env.load_product(name)
    except env.ConfigError as e:
        return (name, '?', str(schema.SCHEMA_VERSION), f'fix the config: {e}')
    versions = [schema.record_version(d) for _label, d in schema.record_dirs(product)]
    versions = [v for v in versions if v is not None]
    record = ','.join(str(v) for v in sorted(set(versions))) or 'none'
    ok, _detail = schema.check(product)
    if ok:
        action = 'none'
    elif any(v > schema.SCHEMA_VERSION for v in versions):
        action = 'record is newer than this package: pipx install the newer asf'
    else:
        action = f'asf schema-migrate --product {name}'
    return (name, record, str(schema.SCHEMA_VERSION), action)


def render(rows):
    head = ('product', 'record schema', 'package', 'action')
    out = ['| ' + ' | '.join(head) + ' |', '| ' + ' | '.join('---' for _ in head) + ' |']
    out += ['| ' + ' | '.join(r) + ' |' for r in rows]
    return '\n'.join(out) + '\n'


def cmd_upgrade(args, run=subprocess.run):
    if not args.skip_pipx:
        cmd = upgrade_command()
        print('upgrade: ' + ' '.join(cmd))
        try:
            rc = run(cmd).returncode
        except OSError:
            print(f'NEEDS OPERATOR: pipx is not on PATH — install pipx, then pipx install {PACKAGE_NAME}')
            return 2
        if rc != 0:
            return rc
    print(f'upgrade: package {__version__}, schema {schema.SCHEMA_VERSION}')
    print(render([row(n) for n in products()]), end='')
    return 0


def register(subparsers):
    p = subparsers.add_parser('upgrade', help='pipx upgrade asf, then the schema check for every product')
    p.add_argument('--skip-pipx', action='store_true', help='only the schema table')
    p.set_defaults(run=cmd_upgrade)
    return p
