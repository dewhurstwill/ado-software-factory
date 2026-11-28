#!/usr/bin/env python3
"""Cross-platform helpers for super-board's Python files. Stdlib only, Python 3.9+.

Load it the way the pack loads its other helpers (the file name has dashes):

    spec = importlib.util.spec_from_file_location('sb_compat', Path(__file__).with_name('super-board-compat.py'))
    compat = importlib.util.module_from_spec(spec); spec.loader.exec_module(compat)

tool(name)      argv prefix that runs `name`, a bare command or a path. On Windows,
                CreateProcess only starts a bare `gh` when it is gh.exe; this also
                finds gh.cmd / az.cmd / codex.cmd through PATHEXT, and runs a
                shell-script tool (a `#!` file with no extension, as Git Bash users
                and the test fakes have) through Git Bash. Raises ToolMissing, a
                FileNotFoundError naming the tool, when there is none.
run(argv, ...)  subprocess.run with argv[0] resolved by tool().
popen(argv, ...) subprocess.Popen likewise.
utf8_stdio()    UTF-8 stdout/stderr with \\n line ends. A piped Windows stdout
                otherwise uses the ANSI code page (ticket titles carry emoji) and
                writes \\r\\n, which bash keeps in every $(…) value.

On macOS and Linux tool() is shutil.which() and nothing else changes.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

WINDOWS = os.name == 'nt'

class ToolMissing(FileNotFoundError):
    """The named tool is not on PATH (or the given path does not exist)."""

    def __init__(self, name: str):
        super().__init__(2, f'{name} not found on PATH; install it or add it to PATH', name)
        self.tool = name


def _shebang(path: str) -> list[str] | None:
    """The interpreter words of a `#!` first line, or None when there is none."""
    try:
        with open(path, 'rb') as handle:
            first = handle.readline(512)
    except OSError:
        return None
    if not first.startswith(b'#!'):
        return None
    words = first[2:].decode('utf-8', 'replace').split()
    if words and os.path.basename(words[0]) == 'env':
        words = [w for w in words[1:] if not w.startswith('-')]
    return words or None


def git_bash() -> str | None:
    """Git for Windows' bash.exe. Never C:\\Windows\\System32\\bash.exe (that is WSL)."""
    found = os.environ.get('SB_BASH')
    if found and os.path.isfile(found):
        return found
    for folder in os.environ.get('PATH', '').split(os.pathsep):
        candidate = os.path.join(folder, 'bash.exe')
        if folder and os.path.isfile(candidate) and 'system32' not in candidate.lower():
            return candidate
    git = shutil.which('git')
    if git:
        root = os.path.dirname(os.path.dirname(os.path.realpath(git)))
        for rel in ('bin', os.path.join('usr', 'bin'), os.path.join('..', 'bin'), os.path.join('..', 'usr', 'bin')):
            candidate = os.path.normpath(os.path.join(root, rel, 'bash.exe'))
            if os.path.isfile(candidate):
                return candidate
    return None


# CreateProcess runs these directly (a .bat/.cmd through cmd.exe).
_DIRECT = ('.exe', '.com', '.bat', '.cmd')


def _script_argv(path: str) -> list[str] | None:
    """argv prefix for a `#!` script, which CreateProcess cannot start on its own."""
    words = _shebang(path)
    if not words:
        return None
    name = os.path.basename(words[0]).lower()
    if name.startswith('python'):
        return [sys.executable, path]
    bash = git_bash()
    if not bash:
        return None
    if name in ('sh', 'bash', 'dash'):
        return [bash, path]
    # Any other interpreter: let Git Bash read the #! line itself.
    return [bash, '-c', 'exec "$0" "$@"', path]


def _windows_file(path: str, direct_only: bool = False) -> list[str] | None:
    """argv prefix for an existing file on Windows, or None if it cannot be run."""
    if not os.path.isfile(path):
        return None
    if os.path.splitext(path)[1].lower() in _DIRECT:
        return [path]
    return None if direct_only else _script_argv(path)


def resolve(name: str) -> list[str] | None:
    """argv prefix that runs `name`, or None when it cannot be found."""
    if not name:
        return None
    if not WINDOWS:
        if os.sep in name:
            return [name] if os.path.isfile(name) else None
        found = shutil.which(name)
        return [found] if found else None
    if name.lower() in ('bash', 'bash.exe'):
        # CreateProcess looks in System32 before PATH, where bash.exe is WSL's.
        bash = git_bash()
        return [bash] if bash else None
    if '/' in name or '\\' in name:
        for candidate in [name] + [name + ext for ext in _DIRECT]:
            argv = _windows_file(candidate)
            if argv:
                return argv
        return None
    # PATH order first, as CreateProcess does. Within one folder: .exe/.com, then a
    # `#!` script (npm puts `codex` beside `codex.cmd`; the script keeps arguments
    # intact, cmd.exe does not), then .bat/.cmd.
    for folder in os.environ.get('PATH', '').split(os.pathsep):
        if not folder:
            continue
        base = os.path.join(folder, name)
        if os.path.splitext(name)[1].lower() in _DIRECT:
            candidates = [(base, True)]
        else:
            candidates = [(base + '.exe', True), (base + '.com', True), (base, False),
                          (base + '.bat', True), (base + '.cmd', True)]
        for candidate, direct_only in candidates:
            argv = _windows_file(candidate, direct_only)
            if argv:
                return argv
    return None


def tool(name: str) -> list[str]:
    argv = resolve(name)
    if argv is None:
        raise ToolMissing(name)
    return argv


def argv_for(argv: list[str]) -> list[str]:
    """argv with its first element resolved by tool()."""
    argv = [str(a) for a in argv]
    return tool(argv[0]) + argv[1:]


def is_msys(exe: str) -> bool:
    """A Git Bash / MSYS2 / Cygwin program (bash.exe, the usr/bin tools), on Windows."""
    if not WINDOWS:
        return False
    if os.path.basename(exe).lower() in ('bash.exe', 'sh.exe', 'bash', 'sh'):
        return True
    folder = os.path.dirname(exe)
    return any(os.path.isfile(os.path.join(folder, dll)) for dll in ('msys-2.0.dll', 'cygwin1.dll'))


def _msys_quote(arg: str) -> str:
    """One argument for an MSYS command line, in double quotes. Inside them MSYS reads a
    backslash as an escape for the next character (C:\\x would lose it), so every
    backslash and every double quote gets one."""
    return '"' + arg.replace('\\', '\\\\').replace('"', '\\"') + '"'


def command_line(argv):
    """What to hand subprocess for argv: the list itself, except for an MSYS program on Windows.

    An MSYS program started by a Windows process parses its own command line and globs
    and brace-expands every word that is not inside double quotes. subprocess quotes only
    words with spaces, so a JSON argument {"a":1,"b":2} would arrive split in two with its
    quotes gone. For those programs every word is quoted; nothing else changes.
    """
    if argv and is_msys(str(argv[0])):
        return ' '.join(_msys_quote(str(a)) for a in argv)
    return argv


def run(argv, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command_line(argv_for(argv)), **kwargs)


def popen(argv, **kwargs) -> subprocess.Popen:
    return subprocess.Popen(command_line(argv_for(argv)), **kwargs)


def write_text(path, text: str) -> None:
    """Path.write_text with UTF-8 and \\n line ends on every OS (bash reads these files:
    a lock file sourced by bash with \\r\\n would give PID=123\\r)."""
    with open(path, 'w', encoding='utf-8', newline='\n') as stream:
        stream.write(text)


def group_kwargs() -> dict:
    """Popen kwargs that start a child as the head of its own process group."""
    if WINDOWS:
        return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
    return {'start_new_session': True}


def kill_group(process, hard: bool = False) -> None:
    """End a child started with group_kwargs() and everything it started.

    POSIX: SIGTERM (or SIGKILL) to the process group. Windows has no SIGTERM for
    another process and no process groups to signal: taskkill /T /F ends the tree
    at once, so a child's own TERM handler does not run there.
    """
    if WINDOWS:
        try:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
        if hard:
            try:
                process.kill()
            except OSError:
                pass
        return
    import signal
    try:
        os.killpg(process.pid, signal.SIGKILL if hard else signal.SIGTERM)
    except ProcessLookupError:
        pass


def utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace' if stream is sys.stderr else 'strict', newline='\n')
        except (AttributeError, ValueError):
            pass


if __name__ == '__main__':
    # `super-board-compat.py which <name>` prints the argv a Python caller would use.
    if len(sys.argv) == 3 and sys.argv[1] == 'which':
        utf8_stdio()
        found = resolve(sys.argv[2])
        if found is None:
            print(f'{sys.argv[2]}: not found', file=sys.stderr)
            sys.exit(1)
        print(' '.join(found))
        sys.exit(0)
    print('usage: super-board-compat.py which <name>', file=sys.stderr)
    sys.exit(64)
