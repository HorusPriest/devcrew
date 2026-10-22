/run-ticket root=<repo path> team=<LINEAR TEAM> project="<project>" tickets=<ID-1,ID-2>

If your client has no `/run-ticket` command, treat this first line as the job's coordinates.
Read this runbook and the named tickets in full, check which role its model fields assign you,
then carry out that role. Report an incomplete or contradictory instruction before changing files.

MODEL: <model, and the dispatcher's one-line reason>
REVIEWER: <independent reviewer model>
PROVIDER: <builder's model family>
TIER: <1 or 2; the consequence tier determines review rounds>
EFFORT: <low | medium | high>
MERGE: <who merges: the human, or a planner session the human has said may; never the builder>
STAGING: <explicit-path: stage named files only; never "add all">
CONCURRENCY: <what else must not run at the same time, and why>
CODEX: <who reviews every diff: the Codex review command when the builder is Claude, or "launcher" when the builder is of another family>
COMMIT: <when to commit, and what every message must and must not name>
DONE-WHEN: <read on the ticket itself, including comments>

START CHECK: <verify the base, branch and worktree; STOP on an existing ticket branch or worktree unless this builder's ownership or the human's takeover instruction is on the ticket>
TREE AT CUT: <branch, HEAD, clean or dirty>, "re-measure, do not trust"
READ FIRST: <project rules file; the tickets IN FULL including comments; key docs>
EXECUTION PINS: <the specific rules for this job, including when to STOP and ask>
SUCCESS CRITERION: <numbered outcomes, each reachable on its own; say which landed>
CLOSE, IN ORDER: <commit, verify, report on the ticket, clean up, debrief LAST>
NEVER REMOVE: <records, data, other sessions' work>
FORBIDDEN: <out-of-scope tickets, pushes, public actions, anything irreversible>

<!-- devcrew template runbook.md, release 2 -->
