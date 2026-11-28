---
type: tool_used
tool: Bash
input_match: '(super-board-forge\.py[^"]* pr comments|gh (pr view|api)[^"]*comments)'
min: 1
weight: 1
---

The reviewer fetched the PR's comments to load `prior_report` (`forge pr comments <PR>`, or a
direct `gh` read).
