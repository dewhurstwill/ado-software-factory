#!/usr/bin/env python3
"""A scripted `gh` for the forge tests. Rules come from the JSON file $FAKE_GH_RULES:

    [{"match": ["issue view", "--json"],   # every substring must occur in the space-joined argv
      "out": {...} | "text",               # stdout (JSON-encoded unless a string)
      "outs": [..],                        # or one answer per call of this rule (the last one repeats)
      "rc": 0, "err": "",                  # exit status and stderr
      "stdin": "substring"}]               # optional: must occur in stdin as well

The first matching rule answers; no match prints an error and exits 1 (a test that did not expect the call).
Every call is appended to $FAKE_GH_LOG as {"argv", "stdin", "gh_repo"}; per-rule counters live beside the log.
"""
import json
import os
import sys

stdin = '' if sys.stdin.isatty() else sys.stdin.read()
argv = sys.argv[1:]
with open(os.environ['FAKE_GH_LOG'], 'a', encoding='utf-8') as log:
    log.write(json.dumps({'argv': argv, 'stdin': stdin, 'gh_repo': os.environ.get('GH_REPO')}) + '\n')
rules = json.load(open(os.environ['FAKE_GH_RULES'], encoding='utf-8'))
line = ' '.join(argv)
counts_path = os.environ['FAKE_GH_LOG'] + '.counts'
try:
    counts = json.load(open(counts_path, encoding='utf-8'))
except (OSError, ValueError):
    counts = {}
for index, rule in enumerate(rules):
    if all(m in line for m in rule['match']) and rule.get('stdin', '') in stdin:
        hit = counts.get(str(index), 0)
        counts[str(index)] = hit + 1
        json.dump(counts, open(counts_path, 'w', encoding='utf-8'))
        out = rule['outs'][min(hit, len(rule['outs']) - 1)] if 'outs' in rule else rule.get('out', '')
        sys.stdout.write(out if isinstance(out, str) else json.dumps(out))
        if rule.get('err'):
            print(rule['err'], file=sys.stderr)
        sys.exit(rule.get('rc', 0))
print(f'fake gh: no rule for: {line}', file=sys.stderr)
sys.exit(1)
