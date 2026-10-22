---
description: Execute the runbook pasted after this command, field by field
---

Read the runbook's model fields first. If its builder belongs to another model family, act as
the launcher using `<kit>/.claude/commands/launch-builder.md`. Otherwise you are the builder:
carry out the runbook below as written rather than redesigning it.

$ARGUMENTS

How to work:

Use `<kit>` for the absolute folder where the user cloned this kit; replace that placeholder when installing this command. The project's own repository need not contain the kit's tools.

- Read the whole runbook before you change anything. If a field is missing, contradicts another field, or asks for something you cannot do, stop and say so instead of guessing.
- Measure the starting state yourself: branch, HEAD, and whether the tree is clean. Do not trust what the runbook says it was.
- Perform START CHECK before editing. Stop without changes if the ticket's branch or worktree exists, unless the ticket records your own builder id as its holder or a human has authorized your takeover.
- Work each field as written: the model and effort, what to read first, the rules for this job, the success criterion, what must never be removed, and what is forbidden.
- Change only what the runbook asks for. Anything else you notice goes into your report as a suggestion, not into the code.
- Stage only the files you changed, by name. Never stage everything at once.
- For a Claude builder, use `<kit>/tools/review.py` for independent Codex review with the declared data class. A builder launched on another family commits in its separate branch and worktree, delivers to its launcher and ends its invocation; it never dispatches a reviewer of its own family. Review follows TIER: rounds until no blocking finding at Tier 2, one round at Tier 1. At either tier, stop and ask the human if the same blocking finding remains after two rounds. Record every finding's disposition.
- If you cannot proceed, post a STOP on the ticket. A launched builder continues through its recorded session or thread id after the decision; a second builder must not be started while the first holds the ticket, except for a human-authorized takeover.
- Never merge. MERGE must name the human or a planner session the human has explicitly authorized.
- Prove each check you rely on by watching it fail once before you trust it passing.
- Commit with a clear message that says what changed and why.
- Report last: what shipped, with the commit hash; what you checked and how; every finding, how serious it was, and what happened to it; and anything left undone, with the reason.
