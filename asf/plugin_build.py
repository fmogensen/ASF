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
import shutil
import sys

from asf import env

#: The plugin manifest's own version. The manifest is generated and committed (``asf plugin
#: check``), so it cannot carry the per-merge package version (:mod:`asf.version`).
PLUGIN_VERSION = '0.1.0'

PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_plugin_dir(cwd=None):
    """The plugin directory is a property of the CHECKOUT, not the package (B-0047): under a
    plain install the package sits in a venv with no ``plugin/`` beside it. A checkout is
    recognised by its ``.claude-plugin/marketplace.json``; the cwd wins, then the package's own
    parent (the editable install), else None."""
    for root in (os.path.abspath(cwd or os.getcwd()), PACKAGE_ROOT):
        if os.path.isfile(os.path.join(root, '.claude-plugin', 'marketplace.json')) \
                or os.path.isdir(os.path.join(root, 'plugin', 'skills')):
            return os.path.join(root, 'plugin')
    return None


PLUGIN_DIR = default_plugin_dir() or os.path.join(PACKAGE_ROOT, 'plugin')


def installed_plugin_dir():
    """Where ``asf plugin install`` writes (B-0047): under ``<ASF_HOME>``, never the runtime's
    own configuration directory (PD15, D7) — a plain ``pipx`` install has no checkout to find."""
    return os.path.join(env.ASF_HOME, 'plugin')

# Operator views: print the table verbatim and stop.
VIEWS = ('status', 'next', 'backlog', 'roadmap', 'parity', 'prod', 'sessions', 'doctor', 'capacity',
         'scorecard', 'release-readiness')

# Operator dialogues: print, then a follow-up the skill carries out with the operator.
DIALOGUES = {
    'groom': (
        "Then read `<backlog_dir>/groom/<today UTC>.md` (`backlog_dir` is in "
        "`~/.ASF/products/<product>.yaml`; the product is `--product` if given, else `$ASF_PRODUCT`, "
        "else the product whose repo or record holds the working directory, else `default_product` "
        "in `~/.ASF/config.yaml`) and show its open questions "
        "(`→ answer: ____`) as one table: item, question, your recommended answer with a one-line "
        "reason. Wait for the operator's answers (`yes` / `no` / `rank 2` / `parent E-nnnn` / `S1` / "
        "`duplicate of B-nnnn` / `all as recommended`); write each into that line's `answer:` slot, "
        "run `asf groom --apply <same args>` and print its output verbatim. Never write "
        "`decided: true` anywhere except through an answer."
    ),
}

PREAMBLE = ("Print the output below **verbatim** in a fenced code block{stop}. The product is "
            "`--product` if you give one, else `$ASF_PRODUCT`, else the product whose repo or "
            "record holds the working directory, else `default_product` in `~/.ASF/config.yaml`; "
            "arguments after the command are passed through.")
# ASF_TABLES=box: the output is captured through a pipe, yet read in a console (asf.tables).
# `|| true`: a RED exit code is the table's verdict, not a failure — Claude Code refuses to show
# the output of a `!` command that exits non-zero.
# `asf` is the pinned release INSTALL_TOOL puts on PATH (one per machine).
INSTALL_TOOL = os.path.join('tools', 'install' + '.sh')
COMMAND = ('!`PATH="$HOME/.local/bin:$PATH"; ASF_BIN=$(command -v asf); '
           'if [ -n "$ASF_BIN" ]; then ASF_TABLES=box "$ASF_BIN" {name} '
           '$ARGUMENTS 2>&1 || true; else echo "asf is not installed: bash ' + INSTALL_TOOL + ' <product>"; fi`')


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


#: The operator's console rules (B-0090): before this, they lived only in whatever an operator
#: happened to paste into their own memory — the plugin shipped no hook that carried them.
#: Paraphrased from docs/CONSTITUTION.md §5 ("never send a human to do an agent's job") and
#: R-0113 ("delegate long work and keep the controller responsive"), the single console-facing
#: statement of both. No apostrophes or quotes: it is embedded in a single-quoted shell command.
CONSOLE_RULES = (
    'ASF console rules: this session is the operator orchestrator. Delegate long or uncertain '
    'work instead of doing it inline, and never block on a running task; background work keeps '
    'the line moving. Act on a decision you can justify instead of asking permission for it. '
    'Park only what truly needs a person, and let the rest continue.'
)

#: B-0121: the console's periodic FACTORY STATUS feed — the console half of B-0087, which built
#: the per-tick digest and `asf watch` but never wired either into a console, leaving every
#: operator to type `/loop 5m /asf:status` by hand. `asf console-feed` decides for itself
#: whether the resolved product's `console.status_every` is off, and its own output
#: (:func:`asf.console_feed.run`) is what tells the console to call it again — so this hook
#: never has to know the interval, and a console with no `asf` on PATH yet runs a no-op.
STATUS_FEED_COMMAND = ('PATH="$HOME/.local/bin:$PATH"; ASF_BIN=$(command -v asf); '
                       'if [ -n "$ASF_BIN" ]; then "$ASF_BIN" console-feed 2>&1 || true; fi')


def render_hooks_json():
    """``plugin/hooks/hooks.json`` — a Claude Code plugin hooks file, auto-loaded by every
    console that installs the plugin. One ``SessionStart`` hook prints :data:`CONSOLE_RULES`; a
    second runs ``asf console-feed`` (:data:`STATUS_FEED_COMMAND`, B-0121) — both folded into
    that session's context the moment it starts (B-0090)."""
    hooks = [{'type': 'command', 'command': f"printf '%s\\n' '{CONSOLE_RULES}'"},
             {'type': 'command', 'command': STATUS_FEED_COMMAND}]
    return json.dumps({
        'hooks': {'SessionStart': [{'hooks': hooks}]},
    }, indent=2, ensure_ascii=False) + '\n'


def render_plugin_json():
    return json.dumps({
        'name': 'asf',
        'description': "ASF — Autonomous Software Factory: the factory's tables in the console — "
                       + ', '.join(f'/asf:{n}' for n in skill_names()),
        'version': PLUGIN_VERSION,
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
    files[os.path.join(plugin_dir, 'hooks', 'hooks.json')] = render_hooks_json()
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


def install(dest=None, out=print):
    """The whole tree under ``<ASF_HOME>`` (or ``dest``), written from the CLI's own parser,
    reading nothing from a checkout — so a plain install still has the ``/asf:*`` skills
    (B-0047). Every file is written whether or not it differs, so the tree is regenerated
    byte-identical on a second run. Strays under the destination's ``plugin/skills`` are
    removed, not merely reported: a skill dropped from the CLI must not linger in an installed
    tree — unlike :func:`build`, which only reports them on a checkout."""
    dest = dest or installed_plugin_dir()
    plugin_dir = os.path.join(dest, 'plugin')
    for path, text in expected_files(plugin_dir).items():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
    for n in stray_skills(plugin_dir):
        shutil.rmtree(os.path.join(plugin_dir, 'skills', n))
        out(f'plugin: removed stray skill {n} (no command named {n})')
    out(f'plugin: {len(skill_names())} skills, plugin.json, marketplace.json written to {dest}')
    return 0


def register(sub):
    p = sub.add_parser('plugin', help='the Claude Code plugin, generated from the CLI: build | check | install')
    p.add_argument('action', choices=['build', 'check', 'install'])
    p.add_argument('--dir', default=None,
                   help='the plugin directory (default for build/check: <checkout>/plugin, found '
                        'from the cwd; default for install: <ASF_HOME>/plugin)')
    p.set_defaults(run=lambda args: run_action(args.action, args.dir))


def run_action(action, plugin_dir=None, out=print, cwd=None):
    if action == 'install':
        return install(plugin_dir, out=out)
    d = plugin_dir or default_plugin_dir(cwd)
    if not d:
        out('plugin: no checkout here (no .claude-plugin/marketplace.json in the cwd) — pass --dir <checkout>/plugin')
        return 2
    return (build if action == 'build' else check)(d, out=out)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='asf plugin')
    ap.add_argument('action', choices=['build', 'check', 'install'])
    ap.add_argument('--dir', default=None)
    a = ap.parse_args(argv)
    return run_action(a.action, a.dir)


if __name__ == '__main__':
    sys.exit(main())
