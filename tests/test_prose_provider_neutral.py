#!/usr/bin/env python3
"""Lane instructions stay provider-neutral: no raw `gh`/`az` ticket, board or PR commands.

Every lane operation is one `python3 .claude/bin/super-board-forge.py --config <cfg> <verb>` that behaves
the same on GitHub and Azure DevOps (skills/super-board/references/providers.md). A raw `gh issue|pr|
project|api|label` or `az boards|repos` command in an instruction an agent follows is a regression: it
silently breaks every Azure DevOps run. Setup and reference material that is provider-specific by nature
(sign-in, account administration, the GraphQL price list) may name them, but only under a markdown
heading that says which provider it is for ("GitHub" or "Azure DevOps" in the heading or one of its
parents), or under a heading on the explicit ALLOWED list below. Prompt files (.js/.sh) get no exception.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = re.compile(r'\bgh (?:issue|pr|project|api|label)\b|\baz (?:boards|repos)\b')
PROVIDER_HEADING = re.compile(r'GitHub|Azure DevOps')

# Files an agent follows. providers.md is the verb reference itself (its "Replaces" columns name the
# old commands on purpose) and stays out.
SCOPE = [
    *sorted(ROOT.glob('skills/*/SKILL.md')),
    *sorted(ROOT.glob('skills/*/README.md')),
    *sorted(p for p in ROOT.glob('skills/*/references/*.md') if p.name != 'providers.md'),
    ROOT / 'workflows/super-board-wave.js',
    ROOT / 'scripts/super-board-lane.sh',
    ROOT / 'scripts/super-board-codex-wave.sh',
    ROOT / 'docs/super-board/README.md',
    ROOT / 'docs/super-board/routing.md',
    ROOT / 'docs/hosts.md',
]

# (file, heading) pairs whose section may name raw setup commands although the heading does not name
# the provider: each line there is labelled GitHub in the text itself.
ALLOWED = {
    ('skills/super-board/references/onboard.md', 'Step by step'),                 # 3. BOARD (GitHub): gh project create
    ('skills/super-board/references/onboard.md', 'Error recovery during onboard'),  # (GitHub) rows
    ('skills/super-board/references/onboard.md', 'Worker self-check (mandatory before the 🎉 screen)'),
}

HEADING = re.compile(r'^(#{1,6})\s+(.*?)\s*#*\s*$')
FENCE = re.compile(r'^\s*(```|~~~)')


def violations(path):
    rel = path.relative_to(ROOT).as_posix()
    text = path.read_text(encoding='utf-8')
    if path.suffix != '.md':
        return [(rel, n, line.strip()) for n, line in enumerate(text.splitlines(), 1) if RAW.search(line)]
    found, chain, fenced = [], [], False
    for n, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            m = HEADING.match(line)
            if m:
                level = len(m.group(1))
                chain = [h for h in chain if h[0] < level] + [(level, m.group(2))]
                continue
        if not RAW.search(line):
            continue
        headings = [h for _, h in chain]
        if any(PROVIDER_HEADING.search(h) for h in headings) or any((rel, h) in ALLOWED for h in headings):
            continue
        found.append((rel, n, line.strip()))
    return found


def main():
    missing = [p for p in SCOPE if not p.exists()]
    bad = [v for p in SCOPE if p.exists() for v in violations(p)]
    stale = [a for a in ALLOWED if not (ROOT / a[0]).exists()
             or not re.search(rf'^#+\s+{re.escape(a[1])}\s*$', (ROOT / a[0]).read_text(encoding='utf-8'), re.M)]
    for p in missing:
        print(f'FAIL: in-scope file missing: {p.relative_to(ROOT)}', file=sys.stderr)
    for rel, n, line in bad:
        print(f'FAIL: {rel}:{n}: raw provider command in lane instructions (use the forge verb, '
              f'or move it under a GitHub/Azure DevOps heading): {line[:160]}', file=sys.stderr)
    for rel, heading in stale:
        print(f'FAIL: ALLOWED names a heading that no longer exists: {rel} -> {heading}', file=sys.stderr)
    if missing or bad or stale:
        sys.exit(1)
    print(f'PASS: test_prose_provider_neutral.py ({len(SCOPE)} files, no raw gh/az lane commands)')


if __name__ == '__main__':
    main()
