---
name: tracked-review
description: Stateful PR review with severity-classified findings, specialist agents, /wontfix, /ticket, and /fix commands. Invoked by the claude-pr-review.yml workflow.
allowed-tools: Agent, Bash, Read, Edit
---

# Tracked PR Review

Read the `TRIGGER_TYPE` environment variable and branch accordingly.

- `push` → full review flow
- `wontfix` → mark finding won't-fix
- `ticket` → file a GitHub issue for a finding
- `fix` → apply safe auto-fixes

All env vars set by the workflow: `PR_NUMBER`, `PR_AUTHOR`, `TRIGGER_TYPE`, `WONTFIX_COMMENT`, `COMMIT_SHA`, `GITHUB_REPOSITORY`, `PR_BASE_REF`.

`COMMIT_SHA` semantics by trigger: for `push` events it is the pushed commit SHA. For comment-triggered flows (`wontfix`, `ticket`, `fix`) it is the PR's current head SHA at the time the comment was posted. `PR_AUTHOR` for comment-triggered flows is the commenter's login (set by the workflow's check-trigger job), not necessarily the original PR author.

---

## Finding IDs and Severity

IDs use a severity prefix + sequential integer shared across all severities:

| Code | Severity | Meaning |
|------|----------|---------|
| `/C` | Critical | Security hole, data loss, crash — must fix before merge |
| `/S` | Severe | Significant bug or logic error — should fix before merge |
| `/W` | Warning | Quality issue, risky pattern, code smell — worth fixing |
| `/I` | Informational | Suggestion, style, nice-to-have |

Examples: `/C1`, `/S4`, `/W12`, `/I7`

Sequential counter `next_id` is shared — IDs never repeat within a PR regardless of severity.

---

## State Format

Stored as a JSON block inside the tracking comment:

```
<!-- review-state
{ ... }
-->
```

Schema:
```json
{
  "next_id": 5,
  "run_count": 3,
  "tracking_comment_id": 1234567890,
  "last_run_open": [2, 4],
  "open": [
    {
      "id": 4,
      "severity": "W",
      "summary": "One-line description",
      "file": "path/to/file.py",
      "line": 42,
      "detail": "Full finding text",
      "raised": "abc1234",
      "fixable": true,
      "patch": "optional unified diff string for safe auto-fix"
    }
  ],
  "wontfix": [
    {
      "id": 2,
      "severity": "I",
      "summary": "...",
      "reason": "Developer's reason text",
      "by": "github-username",
      "at": "def5678"
    }
  ],
  "ticketed": [
    {
      "id": 3,
      "severity": "S",
      "summary": "...",
      "issue_url": "https://github.com/.../issues/42",
      "by": "github-username",
      "at": "def5678"
    }
  ],
  "closed": [
    {
      "id": 1,
      "severity": "C",
      "summary": "...",
      "at": "ghi9012"
    }
  ]
}
```

`fixable`: true when finding meets auto-fix criteria (single block, one file, no behavioural risk). Always false for files in `.github/workflows/` (GITHUB_TOKEN cannot push workflow changes).
`patch`: stored unified diff — applied verbatim on `/fix`. Re-verified before apply.

---

## Tracking Comment Format

```markdown
## PR Review Tracker

| ID | Status | Severity | Summary | Location |
|----|--------|----------|---------|----------|
| /C1 | ✅ | Critical | Short summary | `file.py:42` |
| /S3 | 🎫 | Severe | Short summary | `file.py:10` |
| /W4 | 🔴 💡 | Warning | Short summary | `file.py:167` |
| /I2 | 🚫 | Informational | Short summary | `file.py` |

**Commands:** `/wontfix /W4 reason` — dismiss · `/ticket /S3` — file GitHub issue · `/fix /W4 /W6` — auto-apply safe fixes

<!-- review-state
{ ... JSON state ... }
-->
```

The table includes ALL findings across ALL state buckets — `open`, `closed`, `wontfix`, and `ticketed`. Never omit a row because an item is no longer open; the full history must remain visible. Rows sorted by severity (C → S → W → I), then by ID ascending within each severity. The 💡 after the status emoji means a safe auto-fix is available.
Status emojis: 🔴 open, ✅ fixed, 🚫 won't fix, 🎫 ticketed.

---

## Update Comment Format (posted each push run)

```markdown
<!-- claude-tracked-review-update -->
@{PR_AUTHOR}

**Review run {n}** — commit `{short_sha}`

| | Items |
|---|---|
| ✅ Fixed since last run | /C1 /S3 |
| 🚫 Won't fix auto-closed (now fixed) | /I2 |
| 🎫 Fixed before ticket resolved | /W9 |
| 🔴 Still open | /W4 /W5 |
| 🆕 New findings | /W6 /I7 |
| 💡 Safe auto-fix available | /W4 /W6 /I8 |

**New findings:**

| ID | Severity | Summary | Location |
|----|----------|---------|----------|
| /W6 | Warning | Short summary of finding | `file.py:42` |
| /I7 | Informational | Short summary of finding | `other.py:10` |

See the [tracking comment](https://github.com/{GITHUB_REPOSITORY}/pull/{PR_NUMBER}#issuecomment-{tracking_comment_id}) for full details.
```

Omit the "New findings" table and its header if no new findings this run. Omit rows with zero items. The 💡 row lists ALL open fixable items across the entire state (not just new ones this run) — omit only if none exist.

`last_run_open` is the set of open finding IDs from the end of the previous run. Use it to drive the update comment buckets for previously-open items:
- In `last_run_open` and still open → "Still open"
- In `last_run_open` but no longer open → "Fixed since last run"
- New from Step 4 agents (not previously tracked) → "New findings"

Wontfix-auto-closed and ticketed-now-fixed items (from Step 3) are NOT in `last_run_open` and are NOT "New findings" — they belong to their own rows ('Won't fix auto-closed' and 'Fixed before ticket resolved') built directly from the Step 3 processing results.

If all items fixed and no new findings: "No open issues — PR is clear."

---

## PUSH TRIGGER FLOW

### Step 1 — Read state

```bash
COMMENTS=$(gh api "repos/$GITHUB_REPOSITORY/issues/$PR_NUMBER/comments" \
  --jq '[.[] | select(.body | contains("<!-- review-state"))]')
```

Parse JSON block between `<!-- review-state` and `-->`. If none, use fresh state:
```json
{
  "next_id": 1, "run_count": 0, "tracking_comment_id": null,
  "last_run_open": [], "open": [], "wontfix": [], "ticketed": [], "closed": []
}
```

### Step 2 — Re-verify open items (parallel agents)

For each item in `state.open`, spawn an Agent in parallel:

```
Verify whether issue /{severity}{id} still exists.

Finding: {summary}
File: {file}, around line {line}
Detail: {detail}

Read the file at {file}. Is this exact issue still present?
Return ONLY valid JSON:
{"still_present": true, "evidence": "brief quote showing the problem"}
OR
{"still_present": false, "evidence": "what changed / how it was fixed"}
```

Items where `still_present == false` → newly closed.

### Step 3 — Re-verify wontfix and ticketed items (silent)

Use the same verify prompt for each item in `state.wontfix` AND `state.ticketed`.

For `wontfix` items:
- Still present → no change.
- Now fixed → move to `closed`, note in update comment as "won't fix auto-closed (now fixed)".

For `ticketed` items:
- Still present → no change.
- Now fixed → move to `closed`, note in update comment as "fixed before ticket resolved".

### Step 4 — Fresh-findings pass (parallel specialist agents)

Spawn all applicable agents simultaneously in a single parallel batch — do not wait for one to finish before starting the next.

Determine changed files:
```bash
CHANGED_FILES=$(git diff --name-only origin/$PR_BASE_REF...HEAD 2>/dev/null || \
  git diff --name-only HEAD~1...HEAD 2>/dev/null)
if [ -z "$CHANGED_FILES" ]; then
  echo "::error::Could not determine changed files — aborting fresh-findings pass"
  exit 1
fi
```

Get the diff:
```bash
DIFF=$(git diff origin/$PR_BASE_REF...HEAD 2>/dev/null || \
  git diff HEAD~1...HEAD 2>/dev/null)
if [ -z "$DIFF" ]; then
  echo "No diff detected — skipping fresh-findings pass."
  # Skip agent spawning; proceed to Step 5 with no new findings.
fi
```

Build the known-findings list from `state.open + state.wontfix + state.ticketed` (format: `/{severity}{id}: {summary}` per line). Pass this list to each agent.

**Always spawn:**
- **code-reviewer** — correctness, bugs, CLAUDE.md rule compliance (SQL safety, breaking changes, binary files, destructive utilities, silent data corruption, config validation, argument conflicts, DataFrame mutation, display column whitelists)
- **silent-failure-hunter** — swallowed exceptions, bare `except`, missing error guards, silent fallbacks

**Spawn if test files changed** (file matches `*test*`, `*spec*`, `Test*.py` etc.):
- **pr-test-analyzer** — test coverage quality, missing edge cases, assertions vs. mocks

**Spawn if comments or docs changed** (`.md`, docstrings, inline comments added/modified):
- **comment-analyzer** — comment accuracy, rot, doc completeness

**Spawn if new types/classes/interfaces added:**
- **type-design-analyzer** — invariant expression, encapsulation, enforcement

After all parallel Step 4 agents have returned their findings, run a second sequential pass:

**Step 4b — Patch polish (sequential, after all Step 4 agents complete):**
- **code-simplifier** — polish/refine auto-fix patches; may update `patch` field for cleaner diffs

Agent prompt template:
```
You are reviewing a pull request for NEW issues only. Prior findings are tracked separately —
do not repeat issues already known from before this commit.

Your role: {agent-role}

Known findings (do not re-report):
{known_findings_list}

Changed files: {file list}
Diff:
{git diff output}

For each new finding, return JSON:
{
  "findings": [
    {
      "severity": "C|S|W|I",
      "summary": "One-line description (max 80 chars)",
      "file": "path/to/file",
      "line": 42,
      "detail": "Full explanation and recommended fix",
      "fixable": true,
      "patch": "unified diff string if fixable, else null"
    }
  ]
}

Severity guide: C=Critical(security/crash/data-loss), S=Severe(significant bug),
W=Warning(quality/risk), I=Informational(suggestion/style).

fixable=true only when: single contiguous block in one file, no behavioural risk,
pure refactor/syntax/typo/obvious guard. Include the patch as a unified diff.

fixable=false always when: file is in `.github/workflows/` — GITHUB_TOKEN cannot
push workflow file changes (blocked server-side); offering auto-fix would always fail.

Return {"findings": []} if no new issues. Return ONLY the JSON object.
```

Deduplicate across agents: same file + line + topic = one finding (keep highest severity).

### Step 5 — Update state

1. Newly-closed open items → `closed` (record `COMMIT_SHA[0:7]` as `at`).
2. Wontfix-auto-closed → `closed`.
3. Ticketed items now fixed → `closed` with note.
4. Assign IDs to fresh findings: current `next_id`, `next_id+1`, ...
5. Append to `open`.
6. Update `last_run_open` to current `open` IDs.
7. Increment `next_id` by count of new findings.
8. Increment `run_count` by 1 (treat as 0 if absent from existing state).

### Step 6 — Edit or create tracking comment

Build full tracking comment (table + JSON state block).

If `tracking_comment_id` set, edit in place:
```bash
gh api "repos/$GITHUB_REPOSITORY/issues/comments/$TRACKING_ID" \
  --method PATCH -f body="$TRACKING_BODY"
```

If null (first run), create and capture ID:
```bash
TRACKING_ID=$(gh api "repos/$GITHUB_REPOSITORY/issues/$PR_NUMBER/comments" \
  -f body="$TRACKING_BODY" --jq '.id') \
  || { echo "::error::Failed to create tracking comment"; exit 1; }
```

### Step 7 — Post update comment

Build update comment using `tracking_comment_id` from state to construct the issuecomment anchor URL. Post:

```bash
gh pr comment "$PR_NUMBER" --body "$UPDATE_BODY"
```

---

## WONTFIX TRIGGER FLOW

### Step 1 — Parse comment

Extract from `WONTFIX_COMMENT`:
- Pattern: `/wontfix /C3 reason text` or `/wontfix /W12 reason text`
- ID: integer after the severity letter
- Reason: everything after the ID token (trimmed)

If malformed:
```bash
gh pr comment "$PR_NUMBER" --body "Could not parse /wontfix. Usage: \`/wontfix /W3 reason text\`"
```

### Step 2 — Read state

If no tracking comment:
```bash
gh pr comment "$PR_NUMBER" --body "No tracking comment found. Push a commit to trigger the first review."
```

### Step 3 — Validate

ID must be in `state.open`. If not:
```bash
gh pr comment "$PR_NUMBER" --body "Item /X{N} is not an open finding."
```

### Step 4 — Update state

Move from `open` → `wontfix`:
```json
{"id": N, "severity": "X", "summary": "...", "reason": "...", "by": "<PR_AUTHOR env var>", "at": "COMMIT_SHA[0:7]"}
```
Remove N from `last_run_open`.

(`PR_AUTHOR` env var holds the commenter's login for comment-triggered flows.)

### Step 5 — Edit tracking comment and post acknowledgement

Rebuild and patch tracking comment.

```bash
gh pr comment "$PR_NUMBER" --body "Item /X{N} marked won't fix by @${PR_AUTHOR}: \"${reason}\""
```

---

## TICKET TRIGGER FLOW

### Step 1 — Parse comment

From `WONTFIX_COMMENT` (reused env var — workflow passes the comment body here):
- Pattern: `/ticket /C3` or `/ticket /W12`
- ID: integer after severity letter

If malformed, post usage and stop.

### Step 2 — Read state and validate

ID must be in `state.open`. If not, post error and stop.

### Step 3 — File GitHub issue

Create the `tracked-review` label first (idempotent):
```bash
gh label create "tracked-review" --color "0075ca" --description "Filed by tracked-review bot" 2>/dev/null || true
```

Then create the issue with that label:
```bash
ISSUE_URL=$(gh issue create \
  --title "{summary}" \
  --body "**Finding /X{N} from PR #{PR_NUMBER}**

{detail}

**File:** \`{file}:{line}\`
**Severity:** {severity}
**Raised at commit:** \`{raised}\`

Filed via tracked-review from [PR #{PR_NUMBER}](https://github.com/{GITHUB_REPOSITORY}/pull/{PR_NUMBER})." \
  --label "tracked-review" \
  --jq '.url')
[ -n "$ISSUE_URL" ] || { gh pr comment "$PR_NUMBER" --body "Failed to create issue for /X{N} — check gh permissions."; exit 1; }
```

### Step 4 — Update state

Move from `open` → `ticketed`:
```json
{"id": N, "severity": "X", "summary": "...", "issue_url": "...", "by": "<PR_AUTHOR env var>", "at": "COMMIT_SHA[0:7]"}
```
Remove N from `last_run_open`.

### Step 5 — Edit tracking comment and post acknowledgement

Rebuild and patch tracking comment (status 🎫).

```bash
gh pr comment "$PR_NUMBER" --body "Item /X{N} filed as issue: ${ISSUE_URL}"
```

---

## FIX TRIGGER FLOW

### Step 1 — Parse comment

From `WONTFIX_COMMENT`:
- Pattern: `/fix /W2 /W6 /I3`
- Extract all IDs and their severity letters.

If malformed, post usage and stop.

### Step 2 — Read state

### Step 3 — Validate and apply

Initialize tracking lists: `patched_files=[]`, `applied=[]`, `skipped_already_fixed=[]`, `skipped_unverifiable=[]`, `skipped_not_eligible=[]`, `skipped_apply_failed=[]`.

For each requested ID in order:
1. If ID not in `state.open` or `fixable != true` → add to `skipped_not_eligible`, continue.
2. Re-verify the item is still present (spawn Agent with verify prompt from Step 2 of PUSH flow).
   - If `still_present == false`: add to `skipped_already_fixed`, continue.
   - If malformed response: add to `skipped_unverifiable`, continue.
3. Apply the stored patch via Edit tool. If patch fails to apply: add to `skipped_apply_failed`, continue.
4. Record the patched file path in `patched_files`. Add ID to `applied`.

### Step 4 — Commit and push

If `applied` is empty, post a differentiated outcome comment and stop:
```bash
gh pr comment "$PR_NUMBER" --body "No patches applied.
$([ -n "{skipped_already_fixed}" ] && echo "Already fixed: {skipped_already_fixed}.")
$([ -n "{skipped_not_eligible}" ] && echo "Not eligible (not open or not fixable): {skipped_not_eligible}.")
$([ -n "{skipped_unverifiable}" ] && echo "Could not verify: {skipped_unverifiable}.")
$([ -n "{skipped_apply_failed}" ] && echo "Patch failed to apply: {skipped_apply_failed}.")"
```

Otherwise:
```bash
# Stage only the specific files touched by the applied patches.
git add -- {patched_files}

if git diff --cached --quiet; then
  # Belt-and-suspenders: patches applied but nothing staged.
  gh pr comment "$PR_NUMBER" --body "Patches applied locally but nothing staged — please report this as a bug."
  exit 1
fi

# The workflow checks out the branch ref (not SHA) for fix triggers, so git push has a target.
git commit -m "Apply tracked-review auto-fixes: {applied id list}"
NEW_SHA=$(git rev-parse --short HEAD)
git push || {
  gh pr comment "$PR_NUMBER" \
    --body "Fixes committed locally (SHA: \`$NEW_SHA\`) but push failed. Check branch protection rules or push manually."
  exit 1
}
```

### Step 5 — Update state and tracking comment

For each applied fix: move item from `open` → `closed`, recording `NEW_SHA` (the fix-commit SHA captured above, not the workflow's `COMMIT_SHA`) as `at`.
Rebuild and patch tracking comment with updated table and JSON state.

If tracking comment update fails, post a recovery comment:
```bash
gh pr comment "$PR_NUMBER" --body "Fixes pushed (SHA: \`$NEW_SHA\`) but failed to update tracking comment. Items closed: {applied list}."
```

### Step 6 — Post result comment

```bash
gh pr comment "$PR_NUMBER" --body "Applied fixes: {applied list}. Commit: \`{NEW_SHA}\`.
$([ -n "{skipped_already_fixed}" ] && echo "Already fixed (skipped): {skipped_already_fixed}.")
$([ -n "{skipped_not_eligible}" ] && echo "Not eligible: {skipped_not_eligible}.")
$([ -n "{skipped_unverifiable}" ] && echo "Could not verify (skipped): {skipped_unverifiable}.")
$([ -n "{skipped_apply_failed}" ] && echo "Patch failed to apply: {skipped_apply_failed}.")"
```

Do NOT re-run the full review. The tracking comment is already up to date.

---

## Error handling

- Any `gh api` failure → print error, exit 1.
- Re-verify agent returns malformed JSON → treat as `still_present: true` (safe default).
- Fresh-findings agent returns malformed JSON → log warning, continue (don't fail the run).
- Fix patch fails to apply → log error for that item, skip it, continue.
- Never leave tracking comment partially updated: build full body in memory before patching.
