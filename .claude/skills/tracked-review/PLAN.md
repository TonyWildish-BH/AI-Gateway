# tracked-review skill upgrade plan

## Status legend
- [ ] pending
- [x] done
- [~] in progress
- [!] blocked — see QUESTIONS.md

---

## Tasks

### 1. Permissions [x]
- Added `Bash(git push*)` to `.claude/settings.local.json` project allow list.
- `git add` and `git commit` already globally allowed.
- `git push` was in global `ask` list; project allow overrides it.

---

### 2. Replace `#N` marker with `/N` [x]
- GitHub treats `#N` as a link to issue/PR N — breaks in comments.
- Replacing with `/N` prefix: `/C1`, `/S4`, `/W12`, `/I7`.
- Format: severity letter + sequential ID, no `#`.
- Commit and push after this item.

---

### 3. Specialist agents — match pr-review-toolkit depth [ ]
pr-review-toolkit spawns these agents:
- **code-reviewer**: correctness, bugs, CLAUDE.md compliance
- **silent-failure-hunter**: error handling, swallowed exceptions, missing guards
- **comment-analyzer**: comment accuracy, rot, doc completeness
- **pr-test-analyzer**: test coverage quality and completeness
- **type-design-analyzer**: type invariants, encapsulation (if new types added)
- **code-simplifier**: polish/refine after passing review

Decision: replicate all 6 in the tracked-review PUSH flow.
- Always spawn: code-reviewer, silent-failure-hunter
- If test files changed: pr-test-analyzer
- If comments/docs changed: comment-analyzer
- If types added/modified: type-design-analyzer
- After findings collected (not a review agent): code-simplifier pass on safe-fix candidates

Agents run in parallel per step 4 of the existing PUSH flow.
Commit and push after this item.

---

### 4. Severity classification [ ]
Four levels:
- **C** = Critical — security hole, data loss, crash, must-fix before merge
- **S** = Severe — significant bug or logic error, should fix before merge
- **W** = Warning — quality issue, code smell, risky pattern, worth fixing
- **I** = Informational — suggestion, style, nice-to-have

ID is sequential across all severities (shared counter, not per-severity).
Labels: `/C1`, `/S4`, `/W12`, `/I7`

Tracking comment table gains a Severity column.
State schema gains `severity` field on each finding.
Commit and push after this item.

---

### 5. /ticket command [ ]
Pattern: `/ticket /C3` (or `/ticket /S3` etc.)
Flow:
1. Parse ID from comment.
2. Validate item is open.
3. File a GitHub issue via `gh issue create`:
   - Title: finding summary
   - Body: full detail + link back to PR + "Filed from PR #N finding /X{id}"
   - Label: `tracked-review` (create if absent)
4. Move item from `open` → `ticketed` in state (new bucket).
5. Update tracking comment — new status emoji 🎫.
6. Post acknowledgement comment with issue link.
7. On subsequent push reviews, skip re-verification of ticketed items (treat like wontfix for this PR).
8. On push review: if ticketed item is now fixed, move to `closed` with note "fixed before ticket resolved".

Tradeoff: could link issue as sub-issue of PR — decided against it; GraphQL sub-issues API is flaky (see CLAUDE.md). Simple link in body is enough.
Commit and push after this item.

---

### 6. /fix command [ ]
Pattern: `/fix /W2 /W6 /I3`

Eligibility criteria (assessed during PUSH flow, stored in state):
- Single contiguous block of code in one file
- No behavioral change risk (pure refactor, syntax fix, typo, obvious missing guard)
- Assessed by code-reviewer agent returning `{"fixable": true, "patch": "..."}` for eligible items

Fix flow (triggered by /fix comment):
1. Parse IDs from comment.
2. Validate each is open and marked fixable in state.
3. For each ID, apply the stored patch via Edit tool.
4. Commit all fixes in one commit: "Apply tracked-review auto-fixes: /W2 /W6 /I3"
5. Push.
6. Post comment: "Fixed /W2 /W6 /I3 — pushing commit {sha}. Re-running review..."
7. Immediately run a fresh PUSH review pass (re-verify all open items against new code).

Tradeoff: storing patch in state risks stale patches if developer pushes between review and /fix. 
Safe default: re-verify the item is still present before applying. If not present, skip and note.

Offer line added to update comment: "💡 Safe auto-fix available: /W2 /W6 — reply `/fix /W2 /W6` to apply."
Commit and push after this item.

---

## Decisions log

| Decision | Choice | Rationale |
|---|---|---|
| Marker character | `/` not `#` | `#N` is GitHub shortlink syntax |
| Severity count | 4 (C/S/W/I) | Matches industry norms; finer than critical/suggestion |
| Ticket linking | body link not sub-issue | GraphQL sub-issues API unreliable (CLAUDE.md) |
| Fix patch storage | stored in state at review time | Avoids re-running full analysis on /fix; re-verify before apply |
| Wontfix auto-close | keep existing behaviour | Already tested, works |
| Agents | replicate pr-review-toolkit set | Battle-tested set; conditional spawn keeps CI cost down |

---

## Questions (unresolved)

See QUESTIONS.md if created.
