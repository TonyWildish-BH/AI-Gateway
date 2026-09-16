---
name: tracked-review
description: Stateful PR review that assigns unique IDs to findings and tracks them to closure across runs. Handles both commit-push reviews and /wontfix comment processing. Invoked by the claude-pr-review.yml workflow.
---

# Tracked PR Review

Read the `TRIGGER_TYPE` environment variable and branch accordingly.

- `push` → full review flow
- `wontfix` → state-update-only flow

All env vars are set by the workflow: `PR_NUMBER`, `PR_AUTHOR`, `TRIGGER_TYPE`, `WONTFIX_COMMENT`, `COMMIT_SHA`, `GITHUB_REPOSITORY`.

---

## State Format

State is stored as a JSON block inside the tracking comment:

```
<!-- review-state
{ ... }
-->
```

Schema:
```json
{
  "next_id": 5,
  "tracking_comment_id": 1234567890,
  "last_run_open": [2, 4],
  "open": [
    {
      "id": 4,
      "summary": "One-line description",
      "file": "path/to/file.py",
      "line": 42,
      "severity": "critical|important|suggestion",
      "detail": "Full finding text",
      "raised": "abc1234"
    }
  ],
  "wontfix": [
    {
      "id": 2,
      "summary": "...",
      "reason": "Developer's reason text",
      "by": "github-username",
      "at": "def5678"
    }
  ],
  "closed": [
    {
      "id": 1,
      "summary": "...",
      "at": "ghi9012"
    }
  ]
}
```

`last_run_open` = IDs that were open at end of the previous run. Used to determine "newly closed
since last run" — items in `last_run_open` that are now gone from `open`.

---

## Tracking Comment Format

```markdown
## PR Review Tracker

| # | Status | Severity | Summary | Location | Raised |
|---|--------|----------|---------|----------|--------|
| 1 | ✅ fixed | critical | Short summary | `file.py:42` | `abc1234` |
| 2 | 🚫 won't fix | suggestion | Short summary | `file.py` | `def5678` — @user: "reason" |
| 4 | 🔴 open | important | Short summary | `file.py:167` | `ghi9012` |

<!-- review-state
{ ... JSON state ... }
-->
```

Rows sorted by ID ascending. Severity emojis: critical = 🔴, important = 🟡, suggestion = 🔵.
Status emojis: open = 🔴, fixed = ✅, won't fix = 🚫.

---

## Update Comment Format (posted each push run)

```markdown
<!-- claude-tracked-review-update -->
@{PR_AUTHOR}

**Review run {n}** — commit `{short_sha}`

| | Items |
|---|---|
| ✅ Fixed since last run | #1 #3 |
| 🚫 Won't fix auto-closed (now fixed) | #2 |
| 🔴 Still open | #4 #5 |
| 🆕 New findings | #6 #7 |

See the [tracking comment](#) for full details.
```

Omit rows with zero items. If all items are fixed and no new findings: write "No open issues — PR is clear." instead of the table.

---

## PUSH TRIGGER FLOW

### Step 1 — Read state

```bash
# Find the tracking comment (contains the marker)
COMMENTS=$(gh api "repos/$GITHUB_REPOSITORY/issues/$PR_NUMBER/comments" \
  --jq '[.[] | select(.body | contains("<!-- review-state"))]')
```

Extract the most recent tracking comment's body and parse the JSON block between
`<!-- review-state` and `-->`. If none found, use a fresh state:
```json
{
  "next_id": 1,
  "tracking_comment_id": null,
  "last_run_open": [],
  "open": [],
  "wontfix": [],
  "closed": []
}
```

### Step 2 — Re-verify open items

For each item in `state.open`, spawn an Agent (in parallel) with this prompt:

```
You are verifying whether a specific code issue still exists.

Issue #{id} ({severity}): {summary}
File: {file}, around line {line}
Original finding: {detail}

Read the current file at {file}. Determine whether this exact issue is still present.
Return ONLY valid JSON:
{"still_present": true, "evidence": "brief quote or line showing the problem"}
OR
{"still_present": false, "evidence": "brief explanation of what changed / how it was fixed"}
```

Collect results. Items where `still_present == false` → newly closed.

### Step 3 — Re-verify wontfix items (silent pass)

For each item in `state.wontfix`, spawn an Agent with the same re-verify prompt.
- Still present → no output, no state change (stays in wontfix).
- Now fixed → move to `closed`, note in update comment as "won't fix auto-closed (now fixed)".

### Step 4 — Fresh-findings pass

Determine which files changed:
```bash
git diff --name-only origin/$PR_BASE_REF...HEAD 2>/dev/null || \
  git diff --name-only HEAD~1...HEAD
```

Spawn review agents in parallel based on changed file types. Always include:
- **code-reviewer**: general correctness, bugs, CLAUDE.md compliance
- **silent-failure-hunter**: error handling, silent failures, missing guards

Add if relevant:
- **comment-analyzer**: if comments or docs were added/modified
- **pr-test-analyzer**: if test files changed

Agent prompt template:
```
You are reviewing a pull request for new issues. Prior findings are tracked separately —
do not repeat issues that were already known before this commit.

Changed files: {file list}
Diff:
{git diff output}

Find NEW issues introduced in this diff. For each finding return JSON in this exact format:
{
  "findings": [
    {
      "summary": "One-line description (max 80 chars)",
      "file": "path/to/file",
      "line": 42,
      "severity": "critical|important|suggestion",
      "detail": "Full explanation of the problem and recommended fix"
    }
  ]
}

Return {"findings": []} if no new issues found. Return ONLY the JSON object, nothing else.
```

Deduplicate findings across agents (same file+line+topic = one finding).

### Step 5 — Update state

1. Move newly-closed items from `open` → `closed` (record `COMMIT_SHA[0:7]` as `at`).
2. Move wontfix-auto-closed items from `wontfix` → `closed`.
3. Assign new IDs to fresh findings (`next_id`, `next_id+1`, ...).
4. Append new findings to `open`.
5. Update `last_run_open` to current `open` IDs.
6. Increment `next_id` by count of new findings.

### Step 6 — Edit or create tracking comment

Build the full tracking comment body (table + JSON state block).

If `state.tracking_comment_id` is set, edit in place:
```bash
gh api "repos/$GITHUB_REPOSITORY/issues/comments/$TRACKING_ID" \
  --method PATCH \
  -f body="$TRACKING_BODY"
```

If null (first run), create it and capture the ID:
```bash
TRACKING_ID=$(gh api "repos/$GITHUB_REPOSITORY/issues/$PR_NUMBER/comments" \
  -f body="$TRACKING_BODY" --jq '.id')
```

Store the new `tracking_comment_id` in state before writing (it's part of the JSON block in the comment body).

### Step 7 — Post update comment

Build the update comment (see format above). Count the run number as
`len(state.closed) + len(state.wontfix) + len(state.open)` at start of run + 1, or simply
increment a `run_count` field in state.

Post as a new comment:
```bash
gh pr comment "$PR_NUMBER" --body "$UPDATE_BODY"
```

---

## WONTFIX TRIGGER FLOW

### Step 1 — Parse the comment

Extract ID and reason from `WONTFIX_COMMENT` (env var):
- Pattern: `/wontfix #<N> <reason text>`
- ID: integer N
- Reason: everything after `#N ` (trimmed)

If malformed (no ID found), post a reply and stop:
```bash
gh pr comment "$PR_NUMBER" --body "Could not parse /wontfix command. Usage: \`/wontfix #N reason text\`"
```

### Step 2 — Read state

Same as push Step 1. If no tracking comment exists, post reply and stop:
```
No tracking comment found on this PR. Run the review first by pushing a commit.
```

### Step 3 — Validate

Check that ID N is in `state.open`. If not (already closed, already wontfix, or doesn't exist):
```bash
gh pr comment "$PR_NUMBER" --body "Item #N is not an open finding (already closed, already marked won't fix, or not found)."
```
Stop.

### Step 4 — Update state

Move item from `open` → `wontfix`:
```json
{
  "id": N,
  "summary": "<existing summary>",
  "reason": "<extracted reason>",
  "by": "<WONTFIX_COMMENT author from env PR_AUTHOR>",
  "at": "<COMMIT_SHA[0:7]>"
}
```

Remove N from `last_run_open` so it doesn't show as "newly closed" next push run.

### Step 5 — Edit tracking comment

Rebuild and patch the tracking comment (same as push Step 6).

### Step 6 — Post acknowledgement

```bash
gh pr comment "$PR_NUMBER" --body \
  "Item #N marked won't fix by @${PR_AUTHOR}: \"${reason}\""
```

---

## Error handling

- If any `gh api` call fails, print the error and exit 1 (the workflow captures stderr).
- If a re-verify agent returns malformed JSON, treat that item as `still_present: true`
  (safe default: don't close an issue you couldn't verify).
- If a fresh-findings agent returns malformed JSON, log a warning and continue — don't fail the
  whole run over one agent's bad output.
- Never leave the tracking comment in a partially-updated state: build the full new body in
  memory before patching.
