# PR author notes

Make each changed file understandable in the PR's changed-files view (GitHub **Files changed**, Azure DevOps **Files**). Use the
**Purpose / What changed / Why it matters** format in [writing-standard.md](writing-standard.md#author-notes-on-a-pr).
Keep each label to one short sentence. Describe what the file does before its details.

## When to write them

The PR author posts notes after opening a PR, including a partial draft, and refreshes
them after each push. The lane that changes code owns the refresh for its changed files;
it also checks that every file in the current PR has a summary. This includes QA fixes
and UI-refine PRs. Do it before the lane handoff. No new setup question is needed.

Write one native file-level comment per changed file. Add only a few inline comments
where a reader needs the reason behind a critical change. Both use the three labels.
Tests explain what behavior they protect; scripts explain when they run; config and docs
explain who follows the changed rule. Do not edit source files just to add these notes.

## Read before writing

1. Read the PR's repository, base and full current head SHA. Fetch the matching diff and
   all pages of changed files and review comments, including replies. Confirm that the
   local code you inspect matches that head. A failed or incomplete read is not an empty
   list: stop posting and report the missing read.
2. Draft notes from that diff and the file's actual job. Use only paths on the current PR.
   For inline notes, choose a line or small range in the current diff, with the correct
   old/new side. Never guess line numbers from an earlier checkout.
3. Match existing author notes by path, subject type and stable marker key. Only change
   notes whose author is the current authenticated identity and whose marker identifies
   this workflow's note. Preserve all other comments, including human edits and replies;
   a marker by itself does not prove ownership. If ownership or an edit is unclear, leave
   it intact and report the conflict instead of creating a second copy.
4. Re-read the head before writing. If it changed, discard the draft anchors and start
   from the new diff. Follow the existing quota and halt rules. After posting,
   read back the notes and head to confirm what actually appeared.

## Write notes with the forge

Every read and write below is `python3 .claude/bin/super-board-forge.py --config <cfg> <verb>` (written
`forge <verb>`): the same verbs on GitHub and Azure DevOps ([providers.md](providers.md) → "Pull request
comments, review notes and threads"). Write every body to a file and pass `--body-file`. Keep prose
out of shell interpolation.

- Existing notes: `forge pr threads <PR>` (file threads carry `path`); your identity is `forge whoami`'s
  `login`. Only a note whose `author.login` is that login can be yours.
- File summary: `forge pr note <PR> --file <path> --body-file F --head <sha>`, no `--line`. Do not attach
  a fake line-1 comment to stand in for a whole-file note.
- Critical inline note: `forge pr note <PR> --file <path> --line L --side right|left --body-file F --head <sha>`.
  Use `left` for a deleted line and `right` for an added line. Add `--end-line M` (L first, M last)
  only for a range that the current diff supports.
- Several notes at once: `forge pr notes <PR> --notes-file F --head <sha>`, `F` = JSON
  `[{file,line?,end_line?,side?,body}]`; it prints `[{index,id}]`, and an `id` of null was written but
  not read back. On GitHub the line notes go in ONE review with `event: COMMENT`, never `APPROVE` or
  `REQUEST_CHANGES`, and file summaries as their own review comments; on Azure DevOps each note is a
  thread. Direct review comments do not grant approval.
- `--head` is the full head SHA you read. When the PR moved, the forge exits 6 and writes nothing:
  re-read and redraft.

## Refresh without noise

- Same note, still true at a valid current anchor: leave it alone. Do not publish one
  copy per lane, polling pass or commit.
- Changed explanation, same valid anchor: edit only the owned note's body with
  `forge pr note-edit <PR> <id> --body-file F`. Keep its marker/key. An edit cannot
  move a note to another line or commit.
- Outdated anchor, removed file or renamed path: preserve the old discussion. Mark only
  an owned, unedited note as superseded, and link to its replacement when one is needed
  on a current changed file. Never delete a thread or resolve human replies to tidy up.
- Timeout or uncertain write result: read the comments again before doing anything else.
  Match the intended marker, path, anchor and body. If its outcome is still unknown, stop
  and report it. Never blindly replay a create or submit request.

Generated files, vendored files and binary assets still need useful context. Say what
produced or changed them, with only the details you can verify. If the provider cannot accept
a file comment, use one clearly marked fallback PR comment (`forge pr comment`) covering the affected paths
with the same three labels and the reason. Reuse that owned fallback on later runs.
Do not invent inline anchors for files with no visible diff or silently skip them.

## Handoff and review

Report the confirmed number of file notes and inline notes, plus any grouped fallback
or failed note. If posting fails, keep the PR draft (or leave an existing PR's state
unchanged), preserve the work, and report the incomplete handoff. Do not claim notes
were posted or move the card forward as though the handoff finished.

The Reviewer reads the code before these author explanations. A note by itself is not
a defect finding and must not cause a rebuild or count as approval. Read its replies:
a human question or change request still follows the normal review/blocking flow.
Never exempt a whole thread because its first comment has the author-note marker.
Do not auto-resolve these conversations or bypass the provider's conversation-resolution rules.
