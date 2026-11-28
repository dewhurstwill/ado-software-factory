"""Shared by the Python suites (not a suite itself): `import sbtest` from tests/.

BASH        Git Bash on Windows (never System32's WSL bash.exe, which CreateProcess
            finds first for a bare 'bash'); plain 'bash' elsewhere.
fake_tool(dir, name, body)  writes an executable fake tool. Windows Python callers
            start `#!` fakes through super-board-compat.py; nothing else is needed.
Importing pins host git settings for test repos through GIT_CONFIG_* (see sbtest.sh).
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('sb_compat', ROOT / 'scripts' / 'super-board-compat.py')
compat = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compat)

WINDOWS = compat.WINDOWS
BASH = (compat.resolve('bash') or ['bash'])[0]

GIT_SETTINGS = {
    'core.autocrlf': 'false', 'core.safecrlf': 'false', 'core.longpaths': 'true',
    'init.defaultBranch': 'main', 'commit.gpgsign': 'false', 'tag.gpgsign': 'false',
    'user.name': 'super-board-test', 'user.email': 'super-board-test@example.invalid',
}


def git_env(env: dict | None = None) -> dict:
    """env (default os.environ) with the test git settings appended to GIT_CONFIG_*."""
    env = dict(os.environ if env is None else env)
    if env.get('SBTEST_GIT_ENV'):
        return env
    n = int(env.get('GIT_CONFIG_COUNT') or 0)
    for key, value in GIT_SETTINGS.items():
        env[f'GIT_CONFIG_KEY_{n}'], env[f'GIT_CONFIG_VALUE_{n}'] = key, value
        n += 1
    env['GIT_CONFIG_COUNT'] = str(n)
    env['SBTEST_GIT_ENV'] = '1'
    return env


os.environ.update(git_env())


def fake_tool(folder, name: str, body: str) -> Path:
    path = Path(folder) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding='utf-8', newline='\n')
    path.chmod(0o755)
    return path
