"""ASF — Autonomous Software Factory."""


def _version():
    """The package version, derived from the ``v<x.y.z>`` git tag — never hand-edited: the version
    the build stamped (``setup.py`` writes ``asf/_build.py`` into the wheel), else the checkout's
    own ``git describe``, else the installed distribution's metadata, else ``0.0.0``."""
    try:
        from asf import _build
        stamped = getattr(_build, 'VERSION', '')
        if isinstance(stamped, str) and stamped:
            return stamped
    except ImportError:
        pass
    import os
    import subprocess
    from asf import version
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.exists(os.path.join(root, '.git')):
        try:
            p = subprocess.run(['git', '-C', root, 'describe', '--tags', '--match', 'v[0-9]*'],  # client-exempt: the package version, read before any client module can import
                               capture_output=True, text=True, timeout=5)
            if p.returncode == 0 and version.DESCRIBE_RE.match(p.stdout.strip()):
                return version.pep440(p.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        from importlib import metadata
        return metadata.version('asf-factory')
    except Exception:  # noqa: BLE001 — not installed: no version recorded
        return version.FALLBACK


def __getattr__(name):
    # PEP 562: ``from asf import __version__`` derives it on first use, then caches it
    if name == '__version__':
        value = _version()
        globals()['__version__'] = value
        return value
    raise AttributeError(f"module 'asf' has no attribute {name!r}")
