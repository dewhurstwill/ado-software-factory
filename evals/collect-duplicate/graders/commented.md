---
type: regex
pattern: 'issue comment 301|-X POST repos/[^ ]+/issues/301/comments'
match: contains
target:
  source: file
  path: .git/gh-calls.log
weight: 1
---

The duplicate got a comment on the open ticket #301 (the GitHub filer runs `gh issue comment`;
a forge `ticket comment` sends the REST POST).
