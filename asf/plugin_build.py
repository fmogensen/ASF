"""asf.plugin_build — the Claude Code plugin is generated from the CLI, never written by hand.

``asf plugin build`` writes one ``plugin/skills/<name>/SKILL.md`` per operator view — the
commands in :data:`VIEWS` — from one template, with the command's own ``help`` as the skill's
description; ``groom`` gets the answer loop of :data:`DIALOGUES`. ``--check`` exits 1 when the
tree on disk differs from what would be generated (the plugin test and CI run it). The CLI is
the source; a skill that names a command the CLI lacks, or a view without a skill, cannot exist.
"""
import argparse
import json
import os
import sys

from asf import __version__

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(REPO_ROOT, 'plugin')

# Operator views: print the table verbatim and stop.
VIEWS = ('status', 'next', 'backlog', 'roadmap', 'parity', 'prod', 'sessions', 'doctor')

# Operator dialogues: print, then a follow-up the skill carries out with the operator.
DIALOGUES = {
    'groom': (
        "Then read `<backlog_dir>/groom/<today UTC>.md` (`backlog_dir` is in "
        "`~/.ASF/products/<product>.yaml`; the product is `--product` if given, else `$ASF_PRODUCT`, "
        "else `default_product` in `~/.ASF/config.yaml`) and show its open questions "
        "(`→ answer: ____`) as one table: item, question, your recommended answer with a one-line "
        "reason. Wait for the operator's answers (`yes` / `no` / `rank 2` / `parent E-nnnn` / `S1` / "
        "`duplicate of B-nnnn` / `all as recommended`); write each into that line's `answer:` slot, "
        "run `asf groom --apply <same args>` and print its output verbatim. Never write "
        "`decided: true` anywhere except through an answer."
    ),
}

PREAMBLE = ("Print the output below **verbatim** in a fenced code block{stop}. The product is "
            "`$ASF_PRODUCT`, else `default_product` in `~/.ASF/config.yaml`; arguments after the "
            "command are passed through.")
COMMAND = ('!`PATH="$HOME/.local/bin:$PATH"; if command -v asf >/dev/null 2>&1; then asf {name} '
           '$ARGUMENTS 2>&1; else echo "asf is not installed: pipx install -e <ASF checkout>"; fi`')


def command_help():
    """``{name: help}`` from the CLI's own parser — the single source of the descriptions."""
    from asf.cli import build_parser
    sub = [a for a in build_parser()._actions if a.dest == 'command'][0]
    return {a.dest: a.help for a in sub._choices_actions}


def skill_names():
    return tuple(VIEWS) + tuple(DIALOGUES)


def render_skill(name, help_text):
    tools = 'Bash' if name in VIEWS else 'Bash, Read, Edit'
    stop = ' and stop. Do not summarise, reorder or comment on it unless asked' if name in VIEWS else ''
    body = PREAMBLE.format(stop=stop)
    if name in DIALOGUES:
        body += ' ' + DIALOGUES[name]
    desc = help_text.replace('"', "'")
    return (f'---\nname: {name}\ndescription: "ASF: {desc}"\nallowed-tools: {tools}\n---\n\n'
            f'{body}\n\n{COMMAND.format(name=name)}\n')


def render_plugin_json():
    return json.dumps({
        'name': 'asf',
        'description': "ASF — Autonomous Software Factory: the factory's tables in the console — "
                       + ', '.join(f'/asf:{n}' for n in skill_names()),
        'version': __version__,
        'author': {'name': 'ASF contributors'},
    }, indent=2, ensure_ascii=False) + '\n'


def render_marketplace_json():
    return json.dumps({
        'name': 'asf',
        'owner': {'name': 'ASF contributors'},
        'description': 'ASF — Autonomous Software Factory: the factory\'s tables and grooming in the Claude Code console',
        'plugins': [{'name': 'asf', 'source': './plugin',
                     'description': 'ASF — Autonomous Software Factory: '
                                    + ', '.join(f'/asf:{n}' for n in skill_names())}],
    }, indent=2, ensure_ascii=False) + '\n'


def expected_files(plugin_dir=PLUGIN_DIR):
    helps = command_help()
    files = {os.path.join(plugin_dir, 'skills', n, 'SKILL.md'): render_skill(n, helps[n])
             for n in skill_names()}
    files[os.path.join(plugin_dir, '.claude-plugin', 'plugin.json')] = render_plugin_json()
    root = os.path.dirname(plugin_dir)
    files[os.path.join(root, '.claude-plugin', 'marketplace.json')] = render_marketplace_json()
    return files


def stray_skills(plugin_dir=PLUGIN_DIR):
    """Skill directories on disk that no view or dialogue names."""
    d = os.path.join(plugin_dir, 'skills')
    if not os.path.isdir(d):
        return []
    return sorted(n for n in os.listdir(d) if os.path.isdir(os.path.join(d, n))
                  and n not in skill_names())


def diff(plugin_dir=PLUGIN_DIR):
    """Paths whose content differs from the generated one (missing counts), plus strays."""
    out = []
    for path, text in expected_files(plugin_dir).items():
        try:
            with open(path, encoding='utf-8') as f:
                current = f.read()
        except FileNotFoundError:
            current = None
        if current != text:
            out.append(os.path.relpath(path, os.path.dirname(plugin_dir)))
    out += [f'plugin/skills/{n} (stray)' for n in stray_skills(plugin_dir)]
    return out


def build(plugin_dir=PLUGIN_DIR, out=print):
    for path, text in expected_files(plugin_dir).items():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
    for n in stray_skills(plugin_dir):
        out(f'plugin: stray skill {n} — remove it (no command named {n})')
    out(f'plugin: {len(skill_names())} skills, plugin.json, marketplace.json written from the CLI')
    return 0


def check(plugin_dir=PLUGIN_DIR, out=print):
    d = diff(plugin_dir)
    if d:
        for p in d:
            out(f'plugin: stale — {p}')
        out('plugin: run `asf plugin build`')
        return 1
    out('plugin: up to date with the CLI')
    return 0


def register(sub):
    p = sub.add_parser('plugin', help='the Claude Code plugin, generated from the CLI: build | check')
    p.add_argument('action', choices=['build', 'check'])
    p.add_argument('--dir', default=PLUGIN_DIR, help='the plugin directory (default: <repo>/plugin)')
    p.set_defaults(run=lambda args: (build if args.action == 'build' else check)(args.dir))


def main(argv=None):
    ap = argparse.ArgumentParser(prog='asf plugin')
    ap.add_argument('action', choices=['build', 'check'])
    ap.add_argument('--dir', default=PLUGIN_DIR)
    a = ap.parse_args(argv)
    return (build if a.action == 'build' else check)(a.dir)


if __name__ == '__main__':
    sys.exit(main())
