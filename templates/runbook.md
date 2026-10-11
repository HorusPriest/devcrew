/run-ticket root=<repo path> team=<LINEAR TEAM> project="<project>" tickets=<ID-1,ID-2>

MODEL: <model, and the dispatcher's one-line reason>
REVIEWER: <independent reviewer model>
EFFORT: <low | medium | high>
MERGE: <who merges: executor or planner>
STAGING: <explicit-path: stage named files only; never "add all">
CONCURRENCY: <what else must not run at the same time, and why>
CODEX: <mandatory review of every diff, with the exact command>
COMMIT: <when to commit, and what every message must and must not name>
DONE-WHEN: <read on the ticket itself, including comments>

TREE AT CUT: <branch, HEAD, clean or dirty>, "re-measure, do not trust"
READ FIRST: <project rules file; the tickets IN FULL including comments; key docs>
EXECUTION PINS: <the specific rules for this job, including when to STOP and ask>
SUCCESS CRITERION: <numbered outcomes, each reachable on its own; say which landed>
CLOSE, IN ORDER: <commit, verify, report on the ticket, clean up, debrief LAST>
NEVER REMOVE: <records, data, other sessions' work>
FORBIDDEN: <out-of-scope tickets, pushes, public actions, anything irreversible>
