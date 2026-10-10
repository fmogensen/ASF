"""``asf record-repair``: rewrite every card a value of which an older writer split over lines
(a CI log or a review finding with a raw newline inside a quoted list element) back on one line.

The parser reads that shape (:func:`asf.record.frontmatter._extent`) and the writer no longer
makes it (:func:`asf.record.frontmatter._quote` escapes line breaks), so this is the one-off
migration of the cards already on disk: each is rewritten by
:func:`asf.record.frontmatter.normalize` — only the split values change, every other line stays
byte for byte — through the card writer, and the console command commits them as one record
commit. A dry run unless ``--apply``; idempotent."""
import os
import sys

from asf.record import core, frontmatter, writer


def _cards(root):
    for folder in core.ITEM_FOLDERS:
        d = os.path.join(root, folder)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.endswith('.md'):
                yield os.path.join(folder, name)


def _fixed(root, rel):
    """``(old text, new text)`` of card ``rel``; equal when it needs nothing (or is no card)."""
    with open(os.path.join(root, rel), encoding='utf-8') as f:
        text = f.read()
    if not text.startswith('---\n'):
        return text, text
    try:
        return text, frontmatter.normalize(text, path=rel)
    except frontmatter.FrontmatterError:
        return text, text  # unreadable for another reason: `asf check` names it


def plan(root):
    """The record-relative paths of the cards :func:`repair` would rewrite."""
    return [rel for rel in _cards(root) if len(set(_fixed(root, rel))) == 2]


def repair(root):
    """Rewrite every card in :func:`plan`; returns their paths."""
    done = []
    for rel in _cards(root):
        old, new = _fixed(root, rel)
        if new != old:
            writer.write_card(os.path.join(root, rel), new)
            done.append(rel)
    return done


def run(root, apply=False, out=print):
    rels = repair(root) if apply else plan(root)
    for rel in rels:
        out(f"{'rewrote' if apply else 'would rewrite'} {rel}")
    out(f"record-repair: {len(rels)} card(s) "
        f"{'rewritten on one line' if apply else 'to rewrite (dry run: add --apply)'}")
    return 0


def cmd_record_repair(args, root):
    return run(root, apply=bool(args.apply))


def register(sub):
    """``asf record-repair --product P [--apply]``."""
    p = sub.add_parser('record-repair',
                       help='rewrite cards whose values an older writer split over lines back '
                            'on one line (a dry run unless --apply)')
    p.add_argument('--apply', action='store_true', help='write the cards (else a dry run)')
    p.add_argument('--product')
    return p


if __name__ == '__main__':  # pragma: no cover
    sys.exit(run(sys.argv[1], apply='--apply' in sys.argv))
