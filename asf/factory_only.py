"""asf.factory_only — the opt-in factory-only merge rule, as a CI check.

With ``conventions.merge.factory_only: true`` (default false) only the factory's own branches
land on the trunk through a PR: a PR into the trunk whose head branch is under none of the
product's factory prefixes (``branch_prefixes``, legacy ones included — :meth:`asf.conventions.
Conventions.branch_kind`) is refused. Two escapes stand whatever the branch:

* a release tag — a run on ``refs/tags/…`` is no PR into the trunk;
* a PR that touches nothing but ``merge.bot_paths`` (default ``CHANGELOG.md``, the file the
  release bot writes).

A PR into any other branch, and a product with the rule off, pass. The product's CI runs it on
every pull request; GitHub's own variables supply the defaults::

    - run: python3 -m asf.factory_only --product-file .asf/product.yaml

``--product-file`` names a product file (the ``conventions:`` block is read); ``--product`` a
product by name from ``~/.ASF/products``. ``--head``, ``--base`` and ``--ref`` default to
``GITHUB_HEAD_REF``, ``GITHUB_BASE_REF`` and ``GITHUB_REF``; ``--files`` to the PR's own diff
(``git diff --name-only origin/<base>...HEAD``). Exit 0 with one ``ok`` line, or 1 with the reason.
"""
import argparse
import fnmatch
import os
import sys

TAG_REF = 'refs/tags/'


def verdict(conv, trunk, head, base, files=(), ref=''):
    """``(ok, why)`` for one PR (or one run on ``ref``) under ``conv``'s factory-only rule."""
    if not getattr(conv, 'merge_factory_only', False):
        return True, 'merge.factory_only is off'
    if (ref or '').startswith(TAG_REF):
        return True, f'release tag {ref[len(TAG_REF):]}'
    if not base:
        return True, 'not a pull request'
    if base != trunk:
        return True, f'a PR into {base}, not the trunk {trunk}'
    kind = conv.branch_kind(head or '')
    if kind:
        return True, f'{head} is a factory branch ({kind})'
    bot = list(getattr(conv, 'merge_bot_paths', None) or ())
    files = [f for f in files or () if f]
    if files and bot and all(any(fnmatch.fnmatch(f, g) for g in bot) for f in files):
        return True, f'touches only bot paths ({", ".join(sorted(set(files)))})'
    prefixes = sorted({conv.prefix(k) for k in conv.kinds()} | set(conv.legacy_prefixes()))
    return False, (f'merge.factory_only: {head or "(no head branch)"} is not a factory branch '
                   f'({", ".join(prefixes)}) and touches more than {", ".join(bot) or "no"} '
                   f'bot paths — land it through the factory')


def _diff_files(base, cwd=None):
    from asf import gitops
    p = gitops.git(['diff', '--name-only', f'origin/{base}...HEAD'], cwd or os.getcwd())
    if not p.ok:
        return None
    return [ln.strip() for ln in p.stdout.splitlines() if ln.strip()]


def _load(args):
    from asf import env
    if args.product_file:
        data = env.load_file(args.product_file) or {}
        return env.Product(os.path.splitext(os.path.basename(args.product_file))[0], data)
    return env.load_product(args.product)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='python3 -m asf.factory_only', description=__doc__.split('\n')[0])
    ap.add_argument('--product-file')
    ap.add_argument('--product')
    ap.add_argument('--head', default=os.environ.get('GITHUB_HEAD_REF', ''))
    ap.add_argument('--base', default=os.environ.get('GITHUB_BASE_REF', ''))
    ap.add_argument('--ref', default=os.environ.get('GITHUB_REF', ''))
    ap.add_argument('--files', nargs='*')
    args = ap.parse_args(argv)
    product = _load(args)
    conv = product.conventions
    files = args.files
    if files is None and conv.merge_factory_only and args.base and args.head:
        files = _diff_files(args.base)
        if files is None:
            print(f'factory-only: cannot read the diff against origin/{args.base} — '
                  f'fetch it (actions/checkout fetch-depth: 0)', file=sys.stderr)
            return 1
    ok, why = verdict(conv, product.main, args.head, args.base, files or (), args.ref)
    print(f"factory-only: {'ok' if ok else 'refused'} — {why}")
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
