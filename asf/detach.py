"""asf.detach — start a process the caller never waits on and never leaves ``<defunct>``.

A plain ``Popen(..., start_new_session=True)`` that is never waited on stays the caller's
child: when it exits before the caller does it sits as a zombie until the caller ends (a tick
showed a defunct groom session among its children). :func:`spawn` double-forks instead: a short
launcher starts the real command in its own session (``setsid`` — its pid is its process group,
which is what :mod:`asf.workers.lifecycle` signals) and exits at once; the caller reaps the
launcher, and the command is reparented to init, which reaps it when it ends. Python 3 stdlib
only.
"""
import os
import subprocess
import sys

#: The launcher: start ``argv[2:]`` in a new session, write its pid to fd ``argv[1]``, exit.
#: Its ``Popen`` closes every other fd, so the pid pipe's write end dies with the launcher.
LAUNCHER = ('import os, subprocess, sys\n'
            'p = subprocess.Popen(sys.argv[2:], start_new_session=True)\n'
            'os.write(int(sys.argv[1]), str(p.pid).encode())\n')


def spawn(argv, cwd=None, env=None, stdin=None, stdout=None, stderr=None):
    """Start ``argv`` detached (new session, not the caller's child); returns its pid. ``stdin``,
    ``stdout`` and ``stderr`` are as :class:`subprocess.Popen` takes them and reach the command
    itself. Raises OSError when the command could not be started."""
    r, w = os.pipe()
    try:
        launcher = subprocess.Popen([sys.executable, '-I', '-S', '-c', LAUNCHER, str(w), *argv],
                                    cwd=cwd, env=env, stdin=stdin, stdout=stdout, stderr=stderr,
                                    pass_fds=(w,))
        os.close(w)
        w = None
        data = b''
        while True:
            chunk = os.read(r, 64)
            if not chunk:
                break
            data += chunk
        rc = launcher.wait()
    finally:
        if w is not None:
            os.close(w)
        os.close(r)
    if rc != 0 or not data.strip():
        raise OSError(f'detach: could not start {argv[0]} (launcher exited {rc})')
    return int(data)
