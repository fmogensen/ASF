"""asf.connectors.systemd — the ``scheduler`` connector for Linux: systemd user timers.

Selected by ``connectors.scheduler: systemd`` (or the older ``scheduler.kind: systemd``). Each
clock is two user units in :func:`unit_dir` (``~/.config/systemd/user`` unless
``scheduler.systemd_unit_dir`` names another): ``<label>.service`` — a oneshot running the job's
argv in its working directory and environment, appending stdout and stderr to the clock's log —
and ``<label>.timer``, which fires it every ``every:`` (and once right after it is loaded, as
launchd's ``RunAtLoad`` does) or daily at ``at:`` (``Persistent=true``: a run missed while the
machine was off happens at the next boot). The only module that runs ``systemctl``.

Install writes both units, ``daemon-reload``s and ``enable --now``s the timer; a paused clock is
written but not enabled. Uninstall is ``disable --now``, the units removed, ``daemon-reload``.
"""
import os
import shlex
import subprocess

#: The executable this connector drives (always with ``--user``).
SYSTEMCTL = 'systemctl'
#: Where user units live when ``scheduler.systemd_unit_dir`` is unset.
DEFAULT_UNIT_DIR = '~/.config/systemd/user'


def unit_dir(cfg=None):
    d = ((cfg or {}).get('scheduler') or {}).get('systemd_unit_dir') or DEFAULT_UNIT_DIR
    return os.path.expanduser(str(d))


def _quote(arg):
    """One ExecStart word: systemd's own quoting (double quotes, backslash escapes, ``%%``)."""
    arg = str(arg).replace('%', '%%')
    if arg and not any(c in arg for c in ' \t"\'\\;$'):
        return arg
    return '"' + arg.replace('\\', '\\\\').replace('"', '\\"').replace('$', '$$') + '"'


def _env_line(k, v):
    value = f'{k}={v}'.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    return f'Environment="{value}"'


def service_text(job, workdir, env_vars):
    lines = ['[Unit]', f"Description=ASF clock {job['label']}", '', '[Service]',
             'Type=oneshot', f'WorkingDirectory={workdir}']
    lines += [_env_line(k, v) for k, v in env_vars.items()]
    lines += ['ExecStart=' + ' '.join(_quote(a) for a in job['argv']),
              f"StandardOutput=append:{job['log']}", f"StandardError=append:{job['log']}", '']
    return '\n'.join(lines)


def timer_text(job):
    lines = ['[Unit]', f"Description=ASF clock {job['label']}", '', '[Timer]']
    if job.get('at'):
        at = job['at']
        lines.append(f"OnCalendar=*-*-* {int(at['Hour']):02d}:{int(at['Minute']):02d}:00")
    else:
        lines += ['OnActiveSec=1s', f"OnUnitActiveSec={int(job['every_s'])}s"]
    lines += ['Persistent=true', f"Unit={job['label']}.service", '', '[Install]',
              'WantedBy=timers.target', '']
    return '\n'.join(lines)


def parse_show(text):
    """``systemctl show`` output → ``{Key: value}``."""
    out = {}
    for line in (text or '').splitlines():
        k, sep, v = line.partition('=')
        if sep:
            out[k.strip()] = v.strip()
    return out


def parse_exec_start(text):
    """The argv of a unit file's ``ExecStart=`` line, and its ``WorkingDirectory=``."""
    argv, cwd = [], None
    for line in (text or '').splitlines():
        if line.startswith('ExecStart='):
            try:
                argv = [a.replace('%%', '%') for a in shlex.split(line[len('ExecStart='):])]
            except ValueError:
                argv = []
        elif line.startswith('WorkingDirectory='):
            cwd = line[len('WorkingDirectory='):]
    return argv, cwd


class SystemdScheduler:
    name = 'systemd'

    def __init__(self, cfg=None, run=None):
        self.cfg = cfg
        self._run = run

    # ---- the one process call ---------------------------------------------------------

    def systemctl(self, args, timeout=30):
        """``systemctl --user <args>`` → ``(rc, stdout, stderr)``; 127 when not installed."""
        try:
            p = (self._run or subprocess.run)([SYSTEMCTL, '--user'] + list(args),
                                              capture_output=True, text=True, timeout=timeout)
            return p.returncode, (p.stdout or ''), (p.stderr or '')
        except FileNotFoundError:
            return 127, '', 'systemctl not found'
        except (OSError, subprocess.TimeoutExpired) as e:
            return 1, '', str(e)

    # ---- the interface ----------------------------------------------------------------

    def definition_path(self, label):
        return os.path.join(unit_dir(self.cfg), f'{label}.timer')

    def service_path(self, label):
        return os.path.join(unit_dir(self.cfg), f'{label}.service')

    def render(self, job, workdir, env_vars):
        job['units'] = {'service': service_text(job, workdir, env_vars), 'timer': timer_text(job)}
        job['path'] = self.definition_path(job['label'])
        return job

    def installed_labels(self, pattern):
        import glob
        return sorted(os.path.basename(p)[:-len('.timer')]
                      for p in glob.glob(os.path.join(unit_dir(self.cfg), f'{pattern}.timer')))

    def install(self, job):
        from asf import scheduler
        label, timer = job['label'], job.get('path') or self.definition_path(job['label'])
        service = os.path.join(os.path.dirname(timer), f'{label}.service')
        os.makedirs(os.path.dirname(timer), exist_ok=True)
        os.makedirs(os.path.dirname(job['log']), exist_ok=True)
        for path, text in ((service, job['units']['service']), (timer, job['units']['timer'])):
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)
        lines = [f'scheduler: wrote {service}', f'scheduler: wrote {timer}']
        self.systemctl(['daemon-reload'])
        record = scheduler.pause_record(label)
        if record is not None:
            lines.append(f'scheduler: {label} {scheduler.pause_text(record)} — not enabled; '
                         f'{scheduler.resume_hint(label)} to start it')
            return lines
        rc, _out, err = self.systemctl(['enable', '--now', f'{label}.timer'])
        lines.append(f'scheduler: enabled {label}.timer' if rc == 0
                     else f'scheduler: enable {label}.timer failed ({err.strip() or rc})')
        return lines

    def uninstall(self, label, remove_definition=True):
        rc, _out, err = self.systemctl(['disable', '--now', f'{label}.timer'])
        lines = [f'scheduler: disabled {label}.timer' if rc == 0
                 else f'scheduler: disable {label}.timer failed ({err.strip() or rc})']
        if remove_definition:
            for path in (self.definition_path(label), self.service_path(label)):
                if os.path.exists(path):
                    os.remove(path)
                    lines.append(f'scheduler: removed {path}')
            self.systemctl(['daemon-reload'])
        return lines

    def load(self, path):
        label = os.path.basename(path)
        for suffix in ('.timer', '.service'):
            if label.endswith(suffix):
                label = label[:-len(suffix)]
        rc, _out, err = self.systemctl(['enable', '--now', f'{label}.timer'])
        return rc == 0, (err.strip() if rc else '')

    def stop(self, label):
        """Stop the timer and any running run of the service (as a launchd ``bootout`` does)."""
        rc, _out, _err = self.systemctl(['disable', '--now', f'{label}.timer'])
        self.systemctl(['stop', f'{label}.service'])
        return rc == 0

    def status(self, label):
        rc, out, err = self.systemctl(['show', f'{label}.timer', '--property',
                                       'ActiveState,LoadState,UnitFileState'])
        timer = parse_show(out) if rc == 0 else {}
        if rc != 0 or timer.get('LoadState') in (None, 'not-found') \
                or timer.get('ActiveState') not in ('active', 'activating'):
            return {'label': label, 'loaded': False,
                    'detail': (err or out).strip().splitlines()[:1]}
        rc, out, _err = self.systemctl(['show', f'{label}.service', '--property',
                                        'ActiveState,ExecMainStatus,NRestarts,FragmentPath,'
                                        'ExecMainExitTimestampMonotonic'])
        svc = parse_show(out) if rc == 0 else {}
        exited = svc.get('ExecMainExitTimestampMonotonic') not in (None, '', '0')
        last = svc.get('ExecMainStatus')
        return {'label': label, 'loaded': True,
                'state': 'running' if svc.get('ActiveState') == 'activating'
                else ('waiting' if exited else 'not running'),
                'runs': None, 'program': None, 'path': self.definition_path(label),
                'last_exit': int(last) if exited and str(last).lstrip('-').isdigit() else None,
                'never_exited': not exited}

    def loaded_labels(self):
        rc, out, _err = self.systemctl(['list-timers', '--all', '--no-legend', '--plain'])
        if rc != 0:
            return None
        labels = []
        for line in out.splitlines():
            for word in line.split():
                if word.endswith('.timer'):
                    labels.append(word[:-len('.timer')])
                    break
        return labels

    def locate(self, label):
        path = self.service_path(label)
        try:
            with open(path, encoding='utf-8') as f:
                argv, cwd = parse_exec_start(f.read())
        except OSError:
            return None, None
        return self.definition_path(label), {'ProgramArguments': argv, 'WorkingDirectory': cwd}
