"""asf.connectors.launchd — the default ``scheduler`` connector: launchd user agents (macOS).

The only module that runs ``launchctl``: :func:`launchctl` is the one process call, and
:func:`list_argv` the argv a caller with its own injected runner (doctor, upgrade) sends. The
launchd job logic itself — the plist, the durable pause, ``launchctl print`` parsing — is
:mod:`asf.scheduler`'s, which calls :func:`launchctl` through ``asf.scheduler._launchctl``;
:class:`LaunchdScheduler` is that logic seen through the :class:`asf.connectors.protocols.
Scheduler` interface, each method a call into :mod:`asf.scheduler` at call time.

``cron`` and ``none`` are the same label operations (a machine that schedules by cron may still
hold launchd jobs, and the doctor reads them as before); they differ only in what an install of
a job does: cron prints the crontab line, none prints the command to run.
"""
import os
import subprocess

#: The executable this connector drives.
LAUNCHCTL = 'launchctl'


def launchctl(args, timeout=30):
    """``launchctl <args>`` → ``(rc, stdout, stderr)``; 127 when it is not installed."""
    try:
        p = subprocess.run([LAUNCHCTL] + list(args), capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or ''), (p.stderr or '')
    except FileNotFoundError:
        return 127, '', 'launchctl not found'
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, '', str(e)


def list_argv(label=None):
    """``launchctl list [<label>]`` as an argv, for a caller that runs it with its own runner."""
    return [LAUNCHCTL, 'list'] + ([label] if label else [])


def bootout_hint(label):
    """The command an operator runs to unload ``label`` by hand."""
    return f'{LAUNCHCTL} bootout gui/$(id -u)/{label}'


class LaunchdScheduler:
    name = 'launchd'

    def __init__(self, cfg=None):
        self.cfg = cfg

    @staticmethod
    def _s():
        from asf import scheduler
        return scheduler

    def render(self, job, workdir, env_vars):
        """Fill ``job`` (from :func:`asf.scheduler.render`) with its plist and path."""
        label, log = job['label'], job['log']
        plist = {
            'Label': label,
            'ProgramArguments': job['argv'],
            'WorkingDirectory': workdir,
            'EnvironmentVariables': env_vars,
            'StandardOutPath': log,
            'StandardErrorPath': log,
        }
        if job.get('at'):
            # a timed clock fires only at its declared time — RunAtLoad would also run it the
            # moment install loads the job, hours outside that window (B-0115)
            plist['StartCalendarInterval'] = dict(job['at'])
        else:
            plist['RunAtLoad'] = True
            plist['StartInterval'] = int(job['every_s'])
        job['plist'] = plist
        job['path'] = self.definition_path(label)
        return job

    def definition_path(self, label):
        return self._s().plist_path(label)

    def installed_labels(self, pattern):
        """The labels matching the glob ``pattern`` whose definition is on disk, sorted."""
        import glob
        s = self._s()
        return sorted(os.path.basename(p)[:-len('.plist')]
                      for p in glob.glob(os.path.join(s.launch_agents_dir(), f'{pattern}.plist')))

    def install(self, job):
        return self._s()._launchd_install(job)

    def uninstall(self, label, remove_definition=True):
        return self._s()._launchd_uninstall(label, remove_definition)

    def load(self, path):
        s = self._s()
        rc, _out, err = s._launchctl(['bootstrap', f'gui/{s._uid()}', path])
        return rc == 0, (err.strip() if rc else '')

    def stop(self, label):
        s = self._s()
        rc, _out, _err = s._launchctl(['bootout', f'gui/{s._uid()}/{label}'])
        return rc == 0

    def status(self, label):
        return self._s()._launchd_status(label)

    def loaded_labels(self):
        rc, out, _err = self._s()._launchctl(['list'])
        return self._s().parse_list(out) if rc == 0 else None

    def locate(self, label):
        """``(path, {'ProgramArguments', 'WorkingDirectory'})`` of a loaded label's definition."""
        s = self._s()
        path = s._plist_for(label)
        return path, (s._read_plist(path) if path else None)


class CronScheduler(LaunchdScheduler):
    name = 'cron'

    def render(self, job, workdir, env_vars):
        return job  # asf.scheduler.render writes the crontab line itself

    def install(self, job):
        return self._s().install(job)  # prints the line: nothing here installs a crontab


class NoScheduler(CronScheduler):
    name = 'none'
