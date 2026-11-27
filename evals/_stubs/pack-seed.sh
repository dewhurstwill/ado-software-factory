#!/usr/bin/env bash
# Shared scaffold for the review and QA evals (review-remembers, ponytail-overengineering,
# test-gap): installs the pack's scripts in .claude/bin and a minimal GitHub super-board config,
# so the lane commands the skills give (python3 .claude/bin/super-board-forge.py --config <cfg>
# <verb>) run offline against the gh stub. The install is excluded from git, so the agent's
# `git status` stays clean.
# Usage from a seed, after `git init`:  source "$(dirname "$(command -v gh)")/../pack-seed.sh"
#                                       pack_seed <repo name>
pack_seed() {
  local name="$1" stubs pack
  stubs="$(cd "$(dirname "$(command -v gh)")" && pwd)"
  pack="$(cd "$stubs/../../.." && pwd)"
  [ -f "$pack/scripts/super-board-forge.py" ] || { echo "seed: $pack has no scripts/super-board-forge.py" >&2; return 1; }
  mkdir -p .claude/bin .claude/super-board/configs .claude/skills/super-board
  cp "$pack"/scripts/*.sh "$pack"/scripts/*.py .claude/bin/
  cp -R "$pack/skills/super-board/references" .claude/skills/super-board/
  echo "$name" > .claude/super-board/active
  jq -n --arg name "$name" '{
    version: 1,
    project: {owner: "example", number: 1, title: "Shop"},
    repo: {path: ".", remote: ("https://github.com/example/" + $name + ".git")},
    base_branch: "main"}' > ".claude/super-board/configs/$name.json"
  printf '.claude/\n' >> .git/info/exclude
}
