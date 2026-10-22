# Project rules

Copy this file to the root of your repo as `CLAUDE.md` and fill in the project section. Keep it short: every session reads it.

## The project

- **What it is:** <one or two sentences>
- **Record:** <your Linear team for this project>. Tickets, decisions and state changes live there, not in chat.
- **Key paths:** <the directories that matter, and anything that must never be touched>
- **How to check work:** <the commands that lint, test and build>

## Roles

- **Session start:** run `python <kit>/tools/check_version.py`, with `<kit>` set to your clone folder, and report its first line.

- **Planners never build.** A planner session writes tickets and runbooks, reviews what builders report and keeps these rules current. It does not edit code.
- **Builders build one runbook.** A builder works only the tickets its runbook names, gets every code change reviewed, commits, and reports back on the ticket.
- **Every diff gets an independent review** by a different model before it counts as done. No skipping, no waiving, no downgrading.
- **A builder never marks its own work Done on a summary.** It closes against the ticket's own Done-when, quoting the commit hash.

## Hard limits

- **Prove a check by watching it fail.** A test, gate or guard never seen red proves nothing when it is green.
- **Measure, never assume.** "UNVERIFIED" is a valid answer; a guess stated as fact is not.
- **Never conclude something is absent from one search.** Name the tool you searched with when you report a zero.
- **Kill processes by the PID you recorded, never by name.** Name matches hit shared infrastructure and the search command itself.
- **Wait on the artifact (the exit code, the file, the last line of the log), never on a clock.**
- **One machine, many sessions:** before a heavy or timing-sensitive run, write a claim file (what, how long, until when), and read everyone else's first. Release it by rewriting it as CLOSED, never by deleting it.
- **Scripts that contain English prose go through a file, not a shell heredoc.** Apostrophes break quoting silently.
- **Change only what was asked.** Out-of-scope findings become new tickets, never inline fixes.
- **Delivered work is immutable.** Corrections are forward-only: a new commit, a new comment.
- **Anything public, irreversible, involving money or credentials comes back to a human as explicit steps.** The AI never enters passwords, sends email, or moves money.

## Project-specific rules

- <add rules here as debriefs teach them; date each one>

<!-- devcrew template CLAUDE.md, release 2 -->
