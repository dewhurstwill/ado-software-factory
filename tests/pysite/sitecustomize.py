"""Test harness only: on PYTHONPATH for the suites on Windows (tests/sbtest.sh, run-safety.sh).

1. Fake tools written in Python stand in for gh, codex and friends. The real tools
   write '\\n'; a Windows Python writes '\\r\\n' to a pipe. Every Python in a suite
   writes '\\n' as UTF-8, so a fake behaves like the tool it replaces.
2. Suites start scripts with subprocess.run(['bash', ...]). For a bare 'bash'
   CreateProcess searches System32 before PATH and finds WSL's bash.exe there. Point
   'bash' at Git Bash (SB_BASH, exported by scripts/super-board-compat.sh). Product
   code never relies on this: it resolves bash with super-board-compat.py tool('bash').
"""
import os
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(newline='\n', encoding='utf-8', errors='replace' if _stream is sys.stderr else 'strict')
    except (AttributeError, ValueError):
        pass

if os.name == 'nt':
    import importlib.util
    import subprocess

    _spec = importlib.util.spec_from_file_location(
        'sb_compat', os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'scripts', 'super-board-compat.py'))
    _compat = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_compat)
    _popen_init = subprocess.Popen.__init__

    def _git_bash_init(self, args, *rest, **kwargs):
        # 3. A suite that runs a script by path (scripts/x.sh, a #! fixture) gets it
        #    started the way super-board-compat.py starts one: through Git Bash.
        if isinstance(args, (list, tuple)) and args:
            first = str(args[0])
            if first.lower() in ('bash', 'bash.exe') or (
                    os.path.isfile(first) and os.path.splitext(first)[1].lower() not in ('.exe', '.com', '.bat', '.cmd')):
                found = _compat.resolve(first)
                if found:
                    args = [*found, *args[1:]]
            # 4. Any MSYS program (Git Bash by its full path too) gets every word quoted.
            args = _compat.command_line([str(a) for a in args])
        _popen_init(self, args, *rest, **kwargs)

    subprocess.Popen.__init__ = _git_bash_init
