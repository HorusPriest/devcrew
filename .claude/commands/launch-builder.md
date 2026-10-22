---
description: Start a runbook's builder on another model family and review its deliveries
---

Act as the launch session for the runbook below. Read its MODEL, PROVIDER, REVIEWER and TIER
before starting anything. Your model family must differ from the builder's, and your model
must match the reviewer the runbook names. If it does not, report the mismatch to the human.

$ARGUMENTS

Read the complete runbook and its tickets, including comments. Check for an existing launch
record first: one ticket has one launcher. An existing builder is continued by its recorded
session or thread id, never by launching another copy. A human's recorded takeover instruction
can authorize a replacement after the previous holder has ended.

The builder must carry out the runbook's START CHECK before editing. An existing ticket branch
or worktree means stop without changing it, unless the ticket records this same builder's
ownership or the human has authorized its takeover. For a launched build, require a separate
branch and worktree, then let the builder make and test its changes there.

Start the selected client's builder in the background, passing the complete runbook as input.
Retain its output and write the session id, selected model, root and launcher identity on the
ticket. Wait for that invocation to end; a quiet output stream does not justify another launch.

For a Codex builder, this is one Bash command shape. Replace the placeholders with the
runbook's values. The runbook file must already contain the entire validated runbook.
The calling tool must retain the JSON output while the command runs in the background.

Both Codex examples below use `danger-full-access`: the builder runs without a sandbox,
with the invoking user's access to files, the network and credentials, including files
outside the project. Use an account or environment that keeps unrelated private files,
secrets and unneeded credentials out of the builder's reach.

```bash
codex exec --skip-git-repo-check -C "<project-root>" -m "<builder-model>" \
  -s danger-full-access --json -c model_reasoning_effort="<effort>" - < "<runbook-file>"
```

Read the `thread.started` event's `thread_id` from that output and record it on the ticket.
The final `-` reads stdin; the file redirect supplies input to completion without waiting for
interactive typing. Check the installed client's help when adapting the example.

Continue that same Codex thread from the project root, using its recorded id and the builder's
original model. Put the next instruction in the reply file. This is the corresponding Bash form:

```bash
codex exec resume "<thread-id>" --skip-git-repo-check -m "<builder-model>" \
  -c 'sandbox_mode="danger-full-access"' -c model_reasoning_effort="<effort>" --json - < "<reply-file>"
```

Other clients use their own documented start and resume interfaces; the ticket still carries
the stable identifier. Never resume an invocation that has not ended.

On a delivery, read the committed change against the tickets and their pinned decisions. The
builder does not launch its own review: you are its reviewer, from another model family. Read
the named commit and its proof results, and record each finding and verdict on the ticket.
Resume the same builder to address findings and receive another committed delivery as needed.

Follow TIER: Tier 2 repeats review until no blocking finding remains; Tier 1 gets one review
round. At either tier, if the same blocking finding remains after two rounds, stop and ask the
human. Do not silently waive a blocker or add rounds beyond the runbook's tier.

A builder that cannot continue writes a STOP on the ticket. Obtain the human's decision there
and send that decision to the same builder id. Do not create a replacement to get around a STOP.
When review is complete, resume the builder to report its completion. Builders never merge:
the human merges, or a planner the human has explicitly authorized in MERGE.
