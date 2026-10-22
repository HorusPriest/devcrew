#!/usr/bin/env python3
"""Report release freshness without updating the kit or its copied templates."""
import argparse
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAMP = re.compile(r"<!-- devcrew template ([^,<>]+), release ([0-9]+) -->")
TAG = re.compile(r"\S+\s+refs/tags/release-([0-9]+)")


def git_read(*args):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
    return subprocess.run(["git", "-C", str(ROOT)] + list(args), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=15, env=env, check=True).stdout


def release_status(local, remote):
    try:
        if remote is None:
            remote = git_read("remote", "get-url", "origin").strip()
            if not remote:
                return "UNKNOWN: origin has no URL"
        tags = git_read("ls-remote", "--tags", "--refs", "--", remote)
    except subprocess.TimeoutExpired:
        return "UNKNOWN: release tag lookup timed out"
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN: could not read release tags"
    releases = [int(match.group(1)) for line in tags.splitlines() if (match := TAG.fullmatch(line.strip()))]
    if not releases:
        return "UNKNOWN: no release tags found"
    newest = max(releases)
    if local == newest:
        return "CURRENT release %d" % local
    if local < newest:
        return "BEHIND by %d releases: local %d, newest %d" % (newest - local, local, newest)
    return "UNKNOWN: local release %d is newer than the newest tag %d" % (local, newest)


def copied_reports(paths):
    history = json.loads((ROOT / "templates" / "HISTORY.json").read_text(encoding="utf-8"))
    if not isinstance(history, dict):
        raise ValueError("invalid template history")
    changes = {}
    for release, names in history.items():
        if (not re.fullmatch(r"[0-9]+", release) or not isinstance(names, list)
                or any(not isinstance(name, str) for name in names)):
            raise ValueError("invalid template history")
        changes[int(release)] = names
    known = {name for names in changes.values() for name in names}
    reports = []
    for path in paths:
        lines = [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
        stamp = STAMP.fullmatch(lines[-1]) if lines else None
        if stamp is None:
            reports.append("UNSTAMPED %s" % path)
            continue
        template, release = stamp.group(1), int(stamp.group(2))
        if template not in known:
            reports.append("UNKNOWN TEMPLATE %s" % path)
            continue
        later = [number for number, names in changes.items() if number > release and template in names]
        if later:
            reports.append("STALE %s: %s copied from release %d, changed in release %d"
                           % (path, template, release, max(later)))
    return reports


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--remote", help="release repository URL or path (default: local origin URL)")
    ap.add_argument("--copied", nargs="+", metavar="FILE", help="copies of stamped kit templates to inspect")
    args = ap.parse_args(argv)
    local, reports = "unknown", []
    try:
        value = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[0-9]+", value):
            raise ValueError("invalid VERSION")
        local = int(value)
        status = release_status(local, args.remote)
        if args.copied:
            reports = copied_reports(args.copied)
    except Exception as error:
        status = "UNKNOWN: local release or template information could not be read (%s)" % type(error).__name__
    print(status)
    if status.startswith("BEHIND"):
        print("update: " + shlex.join(["git", "-C", str(ROOT), "pull", "--ff-only"]))
    for report in reports:
        print(report)
    if args.copied and not status.startswith("CURRENT"):
        print("INCOMPLETE: template history is local release %s; update the kit and run again" % local)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
