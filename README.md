# devcrew: running a small AI engineering team in Claude Code

A practical guide to a setup that splits work across Claude Code sessions with clear roles. Linear is the record, a dispatcher picks the model for each task from public benchmarks, and a model from another family reviews each code change. Codex reviews changes built by Claude; a launcher reviews a builder from another family. Everything here is a pattern you rebuild in your own environment; nothing depends on anyone else's accounts.

## 1. The shape

| Role | What it does | What it never does |
|---|---|---|
| **Lane planner** (one Claude Code session per project) | Turns goals into Linear tickets, writes runbooks, reviews what builders report, keeps the project's rules current; merges only when the human has authorized it | Builds anything itself, or assumes merge permission |
| **Builder** (a fresh session per runbook) | Executes one runbook: works the tickets, gets every code change reviewed, commits, reports back | Works outside its tickets, skips review, or merges |
| **Master planner** (optional, once you run 3+ projects) | Owns only what crosses projects: shared data contracts, priority order across projects, a second review of planner decisions | Designs or diagnoses inside a project |
| **Codex** | Reviews Claude's diffs; can also build under a launcher from another family | Reviews work from its own builder family |

The rule that makes it work: **planners plan, builders build, and the record lives in Linear, not in chat.** A replacement session can resume authorized work from the tickets, using its own approved access.

## 2. Linear as the system of record

- **One team per project (lane).** Use projects and milestones inside it for phases.
- **Every ticket carries five sections:** Scope, Done-when (a checkable condition), Proof (what evidence closes it), Not-mine (what is explicitly out), and Pinned decisions.
- **Comments are the handoff.** Every decision, ruling and state change is written as a ticket comment the moment it happens. If a session dies, nothing is lost.
- **Planners post a state digest at each reconciliation:** SHIPPED, NEXT, BLOCKED-ON, OFFERS (data this project could share), ASKS (data it needs).
- **A builder never marks its own work Done on a summary.** It closes against the ticket's own Done-when, quoting the commit hash.

A small Python client for Linear's GraphQL API (key from an environment variable, never printed) is all the tooling you need to start.

## 3. The runbook (launch packet)

The planner writes one block; you paste it into a new Claude Code session. It builds when the runbook names a Claude builder, or launches and reviews the builder when the runbook names another family. Fields:

```
/run-ticket root=<repo path> team=<LINEAR TEAM> project="<project>" tickets=<ID-1,ID-2>

MODEL: <model, and the dispatcher's one-line reason>
REVIEWER: <independent reviewer model>
PROVIDER: <builder's model family>
TIER: <1 or 2; determines review rounds>
EFFORT: <low | medium | high>
MERGE: <the human, or a planner session the human has authorized; never the builder>
STAGING: explicit-path (stage named files only; never "add all")
CONCURRENCY: <what else must not run at the same time, and why>
CODEX: <Codex review command for a Claude builder, or "launcher" for another family>
COMMIT: before close; every message names the ticket ids
DONE-WHEN: read on the ticket itself, including comments

START CHECK: <base, branch and worktree checks; existing ticket branch/worktree means STOP unless own recorded ownership or a human's takeover instruction permits it>
TREE AT CUT: <branch, HEAD, clean or dirty>, "re-measure, do not trust"
READ FIRST: <project rules file; the tickets IN FULL including comments; key docs>
EXECUTION PINS: <the specific rules for this job, including when to STOP and ask>
SUCCESS CRITERION: <numbered outcomes, each reachable on its own; say which landed>
CLOSE, IN ORDER: commit, verify, report on the ticket, clean up, debrief LAST
NEVER REMOVE: <records, data, other sessions' work>
FORBIDDEN: <out-of-scope tickets, pushes, public actions, anything irreversible>
```

`/run-ticket` ships in `.claude/commands/run-ticket.md`. It reads the runbook's model fields to select the builder or launcher role. A Claude builder uses `<kit>/tools/review.py` for Codex review; a builder from another family delivers to its launcher. Tier 2 repeats review until no blocking finding remains; Tier 1 gets one round. At either tier, the same blocker still open after two rounds goes to the human. The builder commits and reports, and never merges. Rename the command or adapt it to your process.

## A builder on another model family

Install `.claude/commands/launch-builder.md` alongside the run-ticket command when you use
a builder from another family. The session you open becomes its launcher and reviewer.
The command includes a Codex CLI example; the selected model and effort come from the runbook.

1. The launcher reads the runbook and starts the selected builder. A launched builder is not started separately by another session.
2. A builder does not send work to a reviewer from its own family. A launched builder uses its own branch and worktree, commits, delivers the result to its launcher, and ends that invocation.
3. Review comes from another model family: Codex for a Claude builder, or the launcher for a builder it started on another family.
4. TIER sets the review work: Tier 2 repeats until no blocking finding remains; Tier 1 gets one round. At either tier, the reviewer stops and asks the human when the same blocking finding is still open after two rounds.
5. A builder that cannot proceed posts a STOP on the ticket. Its launcher continues it by its recorded session or thread id; it does not start another builder while that holder exists. A human's takeover note can authorize a replacement.
6. A ticket has one launcher. Every builder runs START CHECK: an existing ticket branch or worktree stops the build without edits, except for its own ownership recorded under its id or a takeover recorded by the human.
7. Builders do not merge. MERGE names the human, or a planner the human has said may merge.

Two checks before any runbook is launched:
1. **A gate script** that refuses a runbook missing any field, and names which one.
2. **A fresh-context read:** a separate session that never saw the planning reads the runbook whole, looking for contradictions, stale facts and instructions the builder cannot actually complete.

Builders end with a **debrief** on the project: what shipped, what was hard, what tooling broke, and what rule should change. The planner mines debriefs into revised tickets and sharper skills. That loop is where the setup improves itself.

## 4. The model dispatcher

Picks the model for each task instead of defaulting to one.

1. **Task classes.** Define a handful, for example: implementation, code review, evidence and measurement, long-form writing, classification.
2. **A benchmark pack.** A sync script reads public leaderboards and pricing pages (honouring each site's robots.txt and terms) and writes one dated file: per model, a quality score per task class and the current price.
3. **The pick.** For the task's class: quality score, weighted by price, and by how much of your subscription cap is left. Print ONE line with the choice, the score and source, the price basis, and the runner-up with why it lost.
4. **Safety rules:**
   - **Fail closed on a stale pack.** If the benchmark file is older than N days, refuse and say so.
   - **Never hard-pin a model id.** Resolve the newest model at run time; a dated suffix in a model id is a release date, not a version you want frozen.
   - **A sensitive-data rule.** Data classes you mark sensitive (client data, financials) may only go to providers you approve. Default to refusing until you configure it.
5. **Record outcomes** (not included in this kit). When a run on a model goes well or badly, log it, so measured results beat benchmarks over time.

## 5. Review across model families

Every code change is reviewed by another model family before it counts as done: Codex for Claude's changes, or the launcher for a builder it starts on another family. Tier 2 repeats until no blocking finding remains; Tier 1 gets one round. At either tier, the reviewer stops and asks the human if the same blocking finding remains after two rounds. Review is not waived.

Use the review tool with a declared data class and a configured policy (see Quick start):

```
python tools/review.py --repo <repo> --paths <paths> --context <file> --rounds <file> --data-class general
```

After the policy check, the tool supplies its prompt on stdin, as in this command shape:

```
codex exec --skip-git-repo-check -C <repo> --ignore-user-config \
  -c features.memories=false -c memories.generate_memories=false -c memories.use_memories=false \
  -c model_reasoning_effort="high" -s read-only - < "<prompt-file>"
```

(Check the flags against `codex exec --help` for your version.)

Rules:
- **Paste the diff into the prompt,** and scope it by a positive list of paths.
- **Give the reviewer the ticket's Scope, Pinned decisions and Not-mine,** so it reviews against intent, not taste.
- **Carry every earlier round's findings, and how each was handled, into the next round.**
- **After each round, run `git status --porcelain`.** Never commit anything the review round itself left behind.
- **Use the runbook's tier.** Tier 2 repeats until no blocking finding remains; Tier 1 gets one round. Stop and ask the human after two rounds with the same blocking finding still open, at either tier. Record each verdict on the ticket.
- Codex uses its own subscription. Run it signed into your own ChatGPT plan.

**As a builder** (optional): the launcher example runs Codex **without a sandbox**, with the invoking user's access to files, the network and credentials, including files outside the project. That access permits creation of the runbook's branch and separate worktree, including a sibling folder. Use an account or environment that keeps unrelated private files, secrets and unneeded credentials out of the builder's reach. Its launcher reviews from another model family, following TIER and the two-round stop for the same unresolved blocker. Set the runbook's scope and forbidden actions before launching it with that access.

## 6. Hard-won rules (the short list)

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

## 7. Start in an afternoon

1. Create a Linear workspace with one team for your first project. Write the ticket template from section 2 into a Linear template.
2. Write a short `CLAUDE.md` in the repo: the project's rules, the hard limits from section 6, and "planners never build."
3. Open one Claude Code session as the planner. Have it write three tickets and one runbook from section 3.
4. Install the Codex CLI, sign in with your own ChatGPT plan, and run one review round by hand on a small diff.
5. Open a second session, paste the runbook, and let it work. Read its debrief, then improve the runbook template from what it found.
6. Add the dispatcher once you are switching models by hand often enough to want a rule for it.

## 8. Running it safely: the full strategy

The sections above describe the parts. This is how they fit together, so that the amount of process matches what is at stake.

### Consequence tiers

Every task gets a tier before it starts. The highest tier that applies wins, and a runbook that names no tier is treated as Tier 2.

- **Tier 2: the full process.** A runbook, the gate, review from another model family until no blocking finding remains, and a fresh read of the runbook before launch. The reviewer stops and asks the human if the same blocker remains after two rounds. Use it for anything that touches production; client, personal or financial data; credentials or security settings; anything public; an agreement between projects; anything irreversible; or a component that has already had a serious defect of the same kind.
- **Tier 1: a light runbook.** The same fields, with "n/a" where a field does not apply, acceptance evidence, and ONE review round from another model family. The same two-round stop applies if a blocker is still open after two rounds. Use it for every other code change.
- **Tier 0: a self-check.** Only for text that states nothing checkable about the system, such as a reworded sentence in a guide.

### What blocks

A blocking finding is a correctness error, data loss or corruption, a security or exposure risk, an unreviewed merge, a missed acceptance criterion, or an instruction that cannot be completed. Everything else is non-blocking and is recorded. At either tier, the reviewer stops and asks the human if the same blocking finding remains after two review rounds. A blocker is never waived to save time.

### The fresh read

Before a runbook launches, a session that never saw the planning reads it whole, looking for contradictions, stale facts and instructions the builder cannot complete. A revised runbook gets a fresh read of what changed.

### Model routing

The dispatcher (section 4) recommends a model for each task. The reviewer belongs to another family than the builder: Codex for a Claude builder, or the launcher for its builder on another family. Review rounds follow TIER, with a stop for the human after two rounds with the same blocker still open. The gateway in `gateway/` lets a Claude Code session send a chosen task to an approved OpenCode model on your own OpenCode account. A request that does not ask for OpenCode goes to the default provider unchanged; a request that asks for OpenCode but fails any check is refused, never sent somewhere else. See [gateway/README.md](gateway/README.md).

### Sensitive data

Mark your data classes as sensitive or not. A sensitive class may reach only the providers you approve for it, and the default is to refuse. The dispatcher, review tool and gateway all apply this rule, and all treat a class as sensitive unless it is explicitly marked `"sensitive": false`. The review tool uses `--data-class` and `--policy` (default `dispatcher/task-classes.json`); its config's `review_provider` defaults to `openai`.

### The record

Linear holds every decision and state change, so that another session, on another account, can resume from the tickets alone. Each builder's debrief lists every finding with its severity, which check caught it, whether it was fixed, and how the fix was verified.

### One machine, many sessions

Before heavy or timing-sensitive work, write a claim file: what is running, how long it should take, and until when. Read everyone else's claim files first. Release yours by marking it CLOSED, never by deleting it.

## What is in this repo

A starter kit for the setup above. Everything is generic: you configure it for your own team, accounts and models.

| Path | What it is |
|---|---|
| `templates/CLAUDE.md` | Project rules file (section 7): your rules, the hard limits from section 6, and "planners never build" |
| `templates/ticket.md` | Ticket with the five sections from section 2 |
| `templates/runbook.md` | The runbook field block from section 3, as a fill-in template |
| `templates/debrief.md` | Builder debrief: shipped, hard, tooling that broke, rule to change |
| `tools/linear_client.py` | Linear GraphQL client, standard library only; key from `LINEAR_API_KEY`, never printed |
| `tools/runbook_gate.py` | Refuses a runbook missing any template field, or with one left empty, and names each one |
| `tools/review.py` | Runs one Codex review round (section 5) over a committed diff, prompt on stdin, memories off |
| `dispatcher/dispatch.py` | Picks a model per task class from your own benchmark pack (section 4) and prints one line |
| `dispatcher/task-classes.example.json` | Example task classes, price weights and the sensitive-data policy (refuses until you configure it) |
| `dispatcher/benchmark-pack.schema.json` | The shape of the dated benchmark pack your sync writes |
| `dispatcher/fixtures/pack.example.json` | A fictional pack (model-a, model-b, model-c) for tests only |
| `.claude/commands/run-ticket.md` | The `/run-ticket` command from section 3; rename or rewrite it freely |
| `.claude/commands/launch-builder.md` | Starts a builder from another model family, reviews its deliveries and resumes the same id |
| `gateway/` | A local gateway that sends chosen Claude Code tasks to an approved OpenCode model (section 8); see `gateway/README.md` |
| `smoke.py` | Checks that every tool above behaves, with no network except the version check's one read of the release tags, which prints UNKNOWN offline and does not fail |
| `VERSION` | The local release number |
| `tools/check_version.py` | Reads release tags and reports whether the kit or a copied template is outdated |
| `templates/HISTORY.json` | Template names changed in each release |

No benchmark sync and no benchmark data ship here. You write your own sync, honouring each site's robots.txt and terms, and have it write `dispatcher/pack.json`, the file the dispatcher reads by default, as a pack that matches `dispatcher/benchmark-pack.schema.json`.

## Releases and copied templates

Each release uses a `release-<N>` tag on the public repository. The local `VERSION` file
contains the release number. Run `python tools/check_version.py` to compare it with the
largest release number among the remote's tags. The check reads your clone's `origin` URL;
`--remote <url-or-path>` selects another source without changing your git configuration.
No update is performed. A behind result prints a `pull --ff-only` command for you to run.
An offline lookup, a failed lookup, missing release tags, or a local version newer than the
tags produces `UNKNOWN`. These advisory results exit zero; argument errors exit nonzero.

Each template has a final stamp naming its original file and release. Keep that last line
when copying or renaming it. Run `python tools/check_version.py --copied <file> <file>` to
find unstamped copies, unknown template names and templates changed since you copied them.
`templates/HISTORY.json` records those changes. A behind or unknown release result also
reports that the local template history is incomplete; update the kit and run the check again.

## Quick start

Requirements: Python 3.9 or newer, and git (`smoke.py` and `tools/review.py` call it). The tools use the Python standard library only.

1. Run `python smoke.py`. Every check should print PASS.
2. Copy `templates/CLAUDE.md` to your project's root as `CLAUDE.md` and fill in the project section.
3. Put `templates/ticket.md` into a Linear template. Export your Linear personal API key as `LINEAR_API_KEY` and run `python tools/linear_client.py --check`.
4. Copy `.claude/commands/run-ticket.md` into your project's `.claude/commands/` folder and replace `<kit>` once with the absolute folder where you cloned the kit. Write a runbook from `templates/runbook.md` and run `python tools/runbook_gate.py my-runbook.md` from the kit folder before launching it.
5. Copy `dispatcher/task-classes.example.json` to `dispatcher/task-classes.json`. Set each class's `sensitive` value and its `allowed_providers` entry under `data_policy` for the providers you approve. Do this before the first review.
6. Install the Codex CLI and sign in with your own ChatGPT plan. Keep a context file (headings `## Scope`, `## Pinned decisions`, `## Not-mine`) and a rounds file outside the project repo, then run `python tools/review.py --repo <project-repo> --paths <paths> --context <file> --rounds <file> --data-class general --dry-run` from the kit folder to see the command, and again without `--dry-run` for a real round. Choose the data class that describes the actual diff and context.
7. When you want the dispatcher, write your sync and run `python dispatcher/dispatch.py --class implementation --data-class general`.

Licensed under the MIT licence; see `LICENSE`.
