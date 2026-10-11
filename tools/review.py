#!/usr/bin/env python3
"""Run one independent Codex review round over a committed diff.

Usage:
  python tools/review.py --repo PATH --paths P [P ...] --context FILE --rounds FILE
                         [--base main] [--head HEAD] [--model M] [--config FILE]
                         [--codex-bin codex] [--effort high] [--timeout 3600] [--dry-run]

What it does:
  1. Diffs BASE..HEAD limited to the positive list of --paths and pastes that
     diff into the prompt, recording its byte count.
  2. Adds the ticket's Scope, Pinned decisions and Not-mine from --context, a
     Markdown file with exactly those three headings and nothing else.
  3. Carries every earlier round's findings, and how each was handled, from
     --rounds (a JSON file this tool appends to). It refuses to start while an
     earlier finding has no disposition written in that file.
  4. Runs, with the prompt on STDIN:
       codex exec --skip-git-repo-check -C <repo> --ignore-user-config
         -c features.memories=false -c memories.generate_memories=false
         -c memories.use_memories=false -c model_reasoning_effort="high"
         -s read-only [-m <model>] -
     The model comes from --model, else from the config file, else Codex's
     own default; no model id is written in this tool.
  5. Runs `git status --porcelain` before the round (it must be clean: commit
     first) and after it, also after a timeout, and refuses to continue if the
     round left files behind. Like git status, this does not see files under
     gitignored paths.

--dry-run prints the command and the byte counts without running anything.
Keep --context, --rounds and every round's output OUTSIDE the repo; the tool
refuses paths inside it.

Config file (optional, JSON; default tools/review.config.json if present):
  {"reviewer_model": "", "reasoning_effort": "high", "codex_bin": "codex"}

Exit codes: 0 no blocking finding, 1 blocking finding(s), 2 usage or input
error, 3 dirty tree before or after the round, 4 Codex failed or its verdict
could not be read. Python standard library only.
"""
import argparse
import datetime
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "review.config.json")
SECTIONS = ["Scope", "Pinned decisions", "Not-mine"]
MEMORY_OVERRIDES = [
    "features.memories=false",
    "memories.generate_memories=false",
    "memories.use_memories=false",
]
FINDING_RE = re.compile(r"^[\s>*+\-\d.)`]*FINDING\s*\|\s*(NON-BLOCKING|BLOCKING)\s*\|\s*([^|]*)\|\s*(.+?)\s*$", re.I)
VERDICT_RE = re.compile(r"^[\s>*+\-`]*VERDICT:\s*(NO BLOCKING|BLOCKING)\s*$", re.I)
MODEL_RE = re.compile(r"^\s*model:\s*(\S+)", re.I | re.M)
PLAIN_RE = re.compile(r"^[A-Za-z0-9._:/-]+\Z")


class Refusal(Exception):
    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code


def git(repo, *args):
    r = subprocess.run(["git", "-C", repo] + list(args), capture_output=True)
    if r.returncode != 0:
        raise Refusal("git %s failed: %s" % (" ".join(args), r.stderr.decode("utf-8", errors="replace").strip()))
    return r.stdout


def porcelain(repo):
    return git(repo, "status", "--porcelain", "--untracked-files=all").decode("utf-8", errors="replace")


def inside(repo, path, pathmod=os.path):
    """True when path is the repo or under it. pathmod lets a test pass ntpath on any OS."""
    repo_real = pathmod.normcase(pathmod.realpath(repo))
    p = pathmod.normcase(pathmod.realpath(path))
    if pathmod.splitdrive(repo_real)[0] != pathmod.splitdrive(p)[0]:
        return False  # another drive is never inside the repo (commonpath would raise here)
    return pathmod.commonpath([repo_real, p]) == repo_real


def whole_repo(path):
    """True when a --paths value names the repo root itself, such as '.' or './'."""
    return posixpath.normpath(path.replace("\\", "/")) == "."


def plain_value(label, value):
    """Refuse a --model or --effort value that carries shell metacharacters."""
    # on Windows codex.cmd runs through cmd.exe, which re-reads its arguments, so only plain characters pass
    if value is not None and not PLAIN_RE.match(value):
        raise Refusal("%s may hold only letters, digits and . _ : / - characters: %r" % (label, value))
    return value


def read_context(path):
    """Parse the Scope / Pinned decisions / Not-mine file. Refuses anything else."""
    with open(path, encoding="utf-8-sig") as f:
        text = f.read()
    wanted = {s.lower(): s for s in SECTIONS}
    found, current, preamble = {}, None, []
    fence = None  # (character, length) of the open code fence, if any
    for line in text.splitlines():
        fm = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if fm:
            run = fm.group(1)
            if fence is None:
                fence = (run[0], len(run))
            elif run[0] == fence[0] and len(run) >= fence[1] and not fm.group(2).strip():
                fence = None  # only a bare fence of the same character, at least as long, closes it
        m = None if fence else re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if m:
            name = m.group(1).strip().lower()
            if name not in wanted:
                raise Refusal("context file has an unexpected section %r; allowed: %s"
                              % (m.group(1), ", ".join(SECTIONS)))
            current = wanted[name]
            if current in found:
                raise Refusal("context file repeats section %r" % current)
            found[current] = []
            continue
        if current is None:
            preamble.append(line)
        else:
            found[current].append(line)
    if "".join(preamble).strip():
        raise Refusal("context file has text before its first section")
    for s in SECTIONS:
        if s not in found or not "\n".join(found[s]).strip():
            raise Refusal("context file is missing or has an empty section: %s" % s)
    return {s: "\n".join(found[s]).strip() for s in SECTIONS}


def load_rounds(path):
    if not path or not os.path.exists(path):
        return {"rounds": []}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("rounds"), list):
        raise Refusal("rounds file must be a JSON object with a 'rounds' list")
    return data


def undisposed(rounds):
    out = []
    for r in rounds["rounds"]:
        for f in r.get("findings", []):
            if not str(f.get("disposition", "")).strip():
                out.append(f.get("id", "?"))
    return out


def load_config(path):
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise Refusal("config file must be a JSON object")
        return cfg
    return {}


def build_command(codex_bin, repo, effort, model):
    cmd = [codex_bin, "exec", "--skip-git-repo-check", "-C", repo, "--ignore-user-config"]
    for override in MEMORY_OVERRIDES:
        cmd += ["-c", override]
    cmd += ["-c", 'model_reasoning_effort="%s"' % effort, "-s", "read-only"]
    if model:
        cmd += ["-m", model]
    cmd.append("-")  # read the prompt from stdin
    return cmd


def fence_for(text):
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def build_prompt(context, rounds, paths, base, head_sha, diff_text, diff_len):
    lines = [
        "You are an independent code reviewer. Review only: do not modify, create or delete any file.",
        "",
        "Review the change against the intent below, not against taste. A finding is BLOCKING only when the",
        "change breaks the Scope, contradicts a Pinned decision, is incorrect, or is unsafe. Everything else is",
        "NON-BLOCKING. Anything under Not-mine is out of scope and is never BLOCKING.",
        "",
    ]
    for s in SECTIONS:
        lines += ["## %s" % s, context[s], ""]
    lines.append("## Earlier rounds (findings and how each was handled)")
    if not rounds["rounds"]:
        lines.append("None: this is the first round.")
    for r in rounds["rounds"]:
        lines.append("Round %s on %s: %s" % (r.get("round"), str(r.get("reviewed_sha", "?"))[:12], r.get("verdict")))
        for f in r.get("findings", []):
            lines.append("- %s [%s] %s: %s -> disposition: %s" % (
                f.get("id"), f.get("severity"), f.get("where", "").strip(), f.get("summary"), f.get("disposition")))
    lines += ["", "## Paths in scope"] + ["- %s" % p for p in paths]
    fence = fence_for(diff_text)
    lines += [
        "",
        "## Diff: git diff %s..%s, %d bytes" % (base, head_sha[:12], diff_len),
        fence + "diff",
        diff_text.rstrip("\n"),
        fence,
        "",
        "## Output format",
        "One line per finding, exactly:",
        "FINDING | BLOCKING or NON-BLOCKING | <path:line> | <one-sentence summary>",
        "Then one final line, exactly one of:",
        "VERDICT: BLOCKING",
        "VERDICT: NO BLOCKING",
        "",
    ]
    return "\n".join(lines)


def parse_output(text):
    findings, verdict = [], None
    for line in text.splitlines():
        m = FINDING_RE.match(line)
        if m:
            findings.append({"severity": m.group(1).upper(), "where": m.group(2).strip(), "summary": m.group(3)})
            continue
        v = VERDICT_RE.match(line)
        if v:
            verdict = v.group(1).upper()
    if verdict and any(f["severity"] == "BLOCKING" for f in findings):
        verdict = "BLOCKING"
    return findings, verdict


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run one Codex review round over a committed diff.")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--paths", nargs="+", required=True, help="positive list of paths to review")
    ap.add_argument("--context", required=True, help="file with Scope, Pinned decisions and Not-mine")
    ap.add_argument("--rounds", help="JSON rounds file (required unless --dry-run)")
    ap.add_argument("--base", default="main")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--model", help="reviewer model; default: config, else Codex's own default")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--codex-bin")
    ap.add_argument("--effort")
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--dry-run", action="store_true", help="print the command; run nothing")
    a = ap.parse_args(argv)
    try:
        return run(a)
    except Refusal as e:
        print("REFUSED: %s" % e)
        return e.code
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as e:
        print("REFUSED: bad input: %s" % e)
        return 2


def run(a):
    repo = os.path.realpath(a.repo)
    if not os.path.isdir(repo):
        raise Refusal("repo not found: %s" % a.repo)
    top = git(repo, "rev-parse", "--show-toplevel").decode().strip()
    repo = os.path.realpath(top)
    for label, p in (("context", a.context), ("rounds", a.rounds)):
        if p and inside(repo, p):
            raise Refusal("%s file must live outside the repo: %s" % (label, p))
    for p in a.paths:
        if os.path.isabs(p) or ".." in p.replace("\\", "/").split("/"):
            raise Refusal("paths must be relative to the repo and stay inside it: %s" % p)
        if not p.strip() or p.startswith(":"):
            raise Refusal("paths must be plain paths, not empty and not pathspec magic such as :(exclude): %r" % p)
        if whole_repo(p):
            raise Refusal("--paths %s names the whole repo; list the paths the change touches" % p)
    cfg = load_config(a.config)
    model = plain_value("--model", a.model or str(cfg.get("reviewer_model", "")).strip() or None)
    effort = plain_value("--effort", a.effort or str(cfg.get("reasoning_effort", "")).strip() or "high")
    codex_bin = a.codex_bin or str(cfg.get("codex_bin", "")).strip() or "codex"
    context = read_context(a.context)
    rounds = load_rounds(a.rounds)

    head_sha = git(repo, "rev-parse", "--verify", a.head + "^{commit}").decode().strip()
    git(repo, "rev-parse", "--verify", a.base + "^{commit}")
    # literal pathspecs: each path names itself; no globs or magic widen the positive list
    diff_bytes = git(repo, "--literal-pathspecs", "diff", "--no-color", "--no-ext-diff", "%s..%s" % (a.base, head_sha), "--", *a.paths)
    diff_text = diff_bytes.decode("utf-8", errors="replace")
    if not diff_text.strip():
        raise Refusal("the diff %s..%s over the given paths is empty; nothing to review" % (a.base, a.head))
    prompt = build_prompt(context, rounds, a.paths, a.base, head_sha, diff_text, len(diff_bytes))
    cmd = build_command(codex_bin, repo, effort, model)
    model_label = model if model else "Codex default (no -m passed)"
    round_no = len(rounds["rounds"]) + 1

    print("repo: %s" % repo)
    print("range: %s..%s (%s)" % (a.base, a.head, head_sha))
    print("paths: %s" % " ".join(a.paths))
    print("diff bytes pasted: %d" % len(diff_bytes))
    print("prompt bytes: %d" % len(prompt.encode("utf-8")))
    print("earlier rounds carried: %d" % len(rounds["rounds"]))
    print("model: %s" % model_label)
    print("command (prompt on stdin):")
    print("  " + shlex.join(cmd))
    if a.dry_run:
        missing = undisposed(rounds)
        if missing:
            print("WARNING: earlier findings without a disposition: %s" % ", ".join(missing))
        print("DRY RUN: nothing was run")
        return 0

    if not a.rounds:
        raise Refusal("--rounds is required for a real round")
    missing = undisposed(rounds)
    if missing:
        raise Refusal("write a disposition for every earlier finding first: %s" % ", ".join(missing))
    before = porcelain(repo)
    if before.strip():
        raise Refusal("the repo has uncommitted or untracked files; commit before a review round:\n%s"
                      % before.rstrip(), 3)

    out_dir = os.path.dirname(os.path.abspath(a.rounds))
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.join(out_dir, "round-%d" % round_no)
    with open(stem + ".prompt.md", "w", encoding="utf-8") as f:
        f.write(prompt)
    started = datetime.datetime.now(datetime.timezone.utc)
    timed_out = False
    try:
        # shutil.which honours PATHEXT, so an npm-installed codex.cmd is found on Windows
        exe = [shutil.which(cmd[0]) or cmd[0]] + cmd[1:]
        proc = subprocess.run(exe, input=prompt.encode("utf-8"), capture_output=True, timeout=a.timeout)
        out_b, err_b, rc = proc.stdout, proc.stderr, proc.returncode
    except FileNotFoundError:
        raise Refusal("codex not found: %s" % codex_bin, 4)
    except subprocess.TimeoutExpired as e:
        timed_out = True
        out_b, err_b, rc = e.stdout or b"", e.stderr or b"", -1
    stdout = out_b.decode("utf-8", errors="replace")
    stderr = err_b.decode("utf-8", errors="replace")
    with open(stem + ".out.txt", "w", encoding="utf-8") as f:
        f.write(stdout)
    with open(stem + ".err.txt", "w", encoding="utf-8") as f:
        f.write(stderr)

    findings, verdict = parse_output(stdout)
    for n, f in enumerate(findings, 1):
        f["id"] = "R%d-F%d" % (round_no, n)
        f["disposition"] = ""
    reported = MODEL_RE.search(stderr) or MODEL_RE.search(stdout)
    after = porcelain(repo)
    contaminated = bool(after.strip())
    if contaminated:
        status = "REFUSED: round left files behind"
    elif timed_out:
        status = "CODEX TIMED OUT after %d seconds" % a.timeout
    elif rc != 0:
        status = "CODEX FAILED (exit %d)" % rc
    else:
        status = verdict or "UNPARSED"
    rounds["rounds"].append({
        "round": round_no,
        "started_at": started.isoformat(timespec="seconds"),
        "base": a.base,
        "reviewed_sha": head_sha,
        "paths": a.paths,
        "model_flag_passed": bool(model),
        "model_requested": model_label,
        "model_reported": reported.group(1) if reported else "not reported",
        "diff_bytes": len(diff_bytes),
        "prompt_bytes": len(prompt.encode("utf-8")),
        "codex_exit": rc,
        "verdict": status,
        "findings": findings,
        "output_file": stem + ".out.txt",
    })
    with open(a.rounds, "w", encoding="utf-8") as f:
        json.dump(rounds, f, indent=2)
        f.write("\n")

    print("round %d on %s: %s (%d finding(s); reviewer model: %s)"
          % (round_no, head_sha[:12], status, len(findings), rounds["rounds"][-1]["model_reported"]))
    for f in findings:
        print("  %s [%s] %s %s" % (f["id"], f["severity"], f["where"], f["summary"]))
    if contaminated:
        print("REFUSED: the review round left files behind; do not commit them:\n%s" % after.rstrip())
        return 3
    if timed_out or rc != 0 or not verdict:
        print("REFUSED: could not read a verdict; see %s.err.txt" % stem)
        return 4
    if findings:
        print("write a disposition for each finding in %s before the next round" % a.rounds)
    return 1 if verdict == "BLOCKING" else 0


if __name__ == "__main__":
    sys.exit(main())
