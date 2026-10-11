#!/usr/bin/env python3
"""Smoke test for the kit. Prints PASS or FAIL per check; exits non-zero on any FAIL.

Usage:
  python smoke.py             run every check
  python smoke.py --selftest  also run checks that must fail (an injected failure, and parse_output
                              on a deliberately broken sample), to show the run exits non-zero

Everything runs in temporary directories; nothing is written into this repo
and nothing goes over the network (the gateway tests use loopback stubs only). Python standard library only.
"""
import importlib.util
import json
import ntpath
import os
import shlex
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
GATE = os.path.join(ROOT, "tools", "runbook_gate.py")
LINEAR = os.path.join(ROOT, "tools", "linear_client.py")
REVIEW = os.path.join(ROOT, "tools", "review.py")
DISPATCH = os.path.join(ROOT, "dispatcher", "dispatch.py")
CLASSES = os.path.join(ROOT, "dispatcher", "task-classes.example.json")
PACK = os.path.join(ROOT, "dispatcher", "fixtures", "pack.example.json")
GATEWAY_TESTS = os.path.join(ROOT, "gateway", "test_gateway.py")
MEMORY_OVERRIDES = ["features.memories=false", "memories.generate_memories=false", "memories.use_memories=false"]
GIT_QUIET = ["-c", "user.name=smoke", "-c", "user.email=smoke@example.invalid", "-c", "commit.gpgsign=false"]

ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")


def run(args, env=None, cwd=None):
    r = subprocess.run(args, capture_output=True, text=True, env=env or ENV, cwd=cwd, timeout=120)
    return r.returncode, r.stdout + r.stderr


def dispatch(*extra):
    return run([PY, "-B", DISPATCH, "--config", CLASSES, "--pack", PACK, "--allow-fictional"] + list(extra))


def check_gate_selftest():
    rc, out = run([PY, "-B", GATE, "--selftest"])
    ok = rc == 0 and "SELFTEST PASS" in out and out.count("as expected") >= 3
    return ok, "exit %d; %s" % (rc, out.strip().splitlines()[-1] if out.strip() else "no output")


def check_dispatch_pick():
    rc, out = dispatch("--class", "implementation", "--data-class", "general", "--now", "2026-01-20T00:00:00Z")
    lines = out.strip().splitlines()
    ok = rc == 0 and len(lines) == 1 and lines[0].startswith("DISPATCH implementation -> ") and "runner-up" in lines[0]
    return ok, "exit %d; %s" % (rc, out.strip())


def check_dispatch_stale():
    rc, out = dispatch("--class", "implementation", "--data-class", "general", "--now", "2026-06-01T00:00:00Z")
    ok = rc == 1 and out.startswith("REFUSED: stale benchmark pack")
    return ok, "exit %d; %s" % (rc, out.strip())


def check_dispatch_sensitive():
    rc, out = dispatch("--class", "implementation", "--data-class", "client-data", "--now", "2026-01-20T00:00:00Z")
    ok = rc == 1 and out.startswith("REFUSED: sensitive data class")
    return ok, "exit %d; %s" % (rc, out.strip())


NO_NETWORK_PROBE = r"""
import socket, sys
attempts = []
def blocked(*a, **k):
    attempts.append(a)
    raise OSError("network blocked by smoke test")
socket.socket.connect = blocked
socket.create_connection = blocked
socket.getaddrinfo = blocked
sys.path.insert(0, sys.argv[1])
import linear_client
rc = linear_client.main(["--check"])
print("NETWORK_ATTEMPTS=%d" % len(attempts))
sys.exit(rc)
"""


def check_linear_no_key():
    env = {k: v for k, v in ENV.items() if k != "LINEAR_API_KEY"}
    rc1, out1 = run([PY, "-B", LINEAR, "--check"], env=env)
    rc2, out2 = run([PY, "-B", "-c", NO_NETWORK_PROBE, os.path.dirname(LINEAR)], env=env)
    ok = (rc1 != 0 and "LINEAR_API_KEY is not set" in out1
          and rc2 != 0 and "NETWORK_ATTEMPTS=0" in out2)
    return ok, "cli exit %d; probe exit %d; %s; %s" % (
        rc1, rc2, out1.strip(), [l for l in out2.splitlines() if l.startswith("NETWORK_")])


def make_scratch_repo(tmp):
    repo = os.path.join(tmp, "scratch")
    os.makedirs(repo)
    git = ["git", "-C", repo] + GIT_QUIET
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init", "-q", repo], check=True, capture_output=True)
    with open(os.path.join(repo, "app.py"), "w", encoding="utf-8") as f:
        f.write("print('one')\n")
    subprocess.run(git + ["add", "app.py"], check=True, capture_output=True)
    subprocess.run(git + ["commit", "-q", "-m", "base"], check=True, capture_output=True)
    subprocess.run(git + ["checkout", "-q", "-b", "work"], check=True, capture_output=True)
    with open(os.path.join(repo, "app.py"), "a", encoding="utf-8") as f:
        f.write("print('two')\n")
    subprocess.run(git + ["commit", "-q", "-am", "change"], check=True, capture_output=True)
    ctx = os.path.join(tmp, "context.md")
    with open(ctx, "w", encoding="utf-8") as f:
        f.write("## Scope\nPrint a second line.\n\n## Pinned decisions\nStandard library only.\n\n"
                "## Not-mine\nAnything outside the Scope.\n")
    return repo, ctx


def check_review_dry_run():
    with tempfile.TemporaryDirectory() as tmp:
        repo, ctx = make_scratch_repo(tmp)
        empty_cfg = os.path.join(tmp, "no-config.json")
        rc, out = run([PY, "-B", REVIEW, "--repo", repo, "--paths", "app.py", "--context", ctx,
                       "--config", empty_cfg, "--dry-run"])
        lines = out.splitlines()
        cmd = []
        for i, line in enumerate(lines):
            if line.startswith("command (prompt on stdin)") and i + 1 < len(lines):
                cmd = shlex.split(lines[i + 1])
        pairs = set(zip(cmd, cmd[1:]))
        has_memories = all(("-c", o) in pairs for o in MEMORY_OVERRIDES)
        ok = (rc == 0 and cmd[:2] == ["codex", "exec"] and has_memories and cmd[-1] == "-"
              and ("-s", "read-only") in pairs and "-m" not in cmd and "DRY RUN" in out)
        return ok, "exit %d; memories-off=%s; stdin=%s; command: %s" % (
            rc, has_memories, bool(cmd) and cmd[-1] == "-", shlex.join(cmd))


FAKE_CODEX = r"""
import sys, os
prompt = sys.stdin.read()
repo = sys.argv[sys.argv.index("-C") + 1]
with open(os.path.join(repo, "left-behind.txt"), "w") as f:
    f.write("prompt bytes on stdin: %d\n" % len(prompt.encode("utf-8")))
print("model: fake-reviewer", file=sys.stderr)
print("VERDICT: NO BLOCKING")
"""


def fake_codex(tmp):
    script = os.path.join(tmp, "fake_codex.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(FAKE_CODEX)
    if os.name == "nt":
        wrapper = os.path.join(tmp, "fake_codex.cmd")
        with open(wrapper, "w", encoding="utf-8") as f:
            f.write('@"%s" "%s" %%*\n' % (PY, script))
        return wrapper
    wrapper = os.path.join(tmp, "fake_codex")
    with open(wrapper, "w", encoding="utf-8") as f:
        f.write("#!%s\n" % PY)
        f.write(FAKE_CODEX)
    os.chmod(wrapper, 0o755)
    return wrapper


def check_review_contamination():
    with tempfile.TemporaryDirectory() as tmp:
        repo, ctx = make_scratch_repo(tmp)
        rounds_dir = os.path.join(tmp, "review")
        os.makedirs(rounds_dir)
        rounds = os.path.join(rounds_dir, "rounds.json")
        codex = fake_codex(tmp)
        base = [PY, "-B", REVIEW, "--repo", repo, "--paths", "app.py", "--context", ctx, "--rounds", rounds,
                "--config", os.path.join(tmp, "no-config.json"), "--codex-bin", codex]
        # a) a stray file already in the tree: refuse before running anything
        stray = os.path.join(repo, "stray.txt")
        with open(stray, "w", encoding="utf-8") as f:
            f.write("stray\n")
        rc_a, out_a = run(base)
        os.remove(stray)
        ok_a = rc_a == 3 and "commit before a review round" in out_a and not os.path.exists(rounds)
        # b) the round itself leaves a file behind: refuse to continue
        rc_b, out_b = run(base)
        left = os.path.join(repo, "left-behind.txt")
        got_stdin = False
        if os.path.exists(left):
            with open(left, encoding="utf-8") as f:
                got_stdin = not f.read().strip().endswith(": 0")
        ok_b = rc_b == 3 and "round left files behind" in out_b and got_stdin
        return ok_a and ok_b, "pre-dirty exit %d; post-round exit %d; prompt arrived on stdin=%s" % (
            rc_a, rc_b, got_stdin)


def load_review():
    spec = importlib.util.spec_from_file_location("review_under_test", REVIEW)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_dispatch_null_sensitive():
    with tempfile.TemporaryDirectory() as tmp:
        with open(CLASSES, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["data_policy"]["data_classes"]["unset"] = {"sensitive": None}
        cfg["data_policy"]["allowed_providers"] = {}
        path = os.path.join(tmp, "classes.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        rc, out = run([PY, "-B", DISPATCH, "--config", path, "--pack", PACK, "--allow-fictional",
                       "--class", "implementation", "--data-class", "unset", "--now", "2026-01-20T00:00:00Z"])
    ok = rc == 1 and out.startswith("REFUSED: sensitive data class")
    return ok, "exit %d; %s" % (rc, out.strip())


def review_refusal(paths, *extra):
    with tempfile.TemporaryDirectory() as tmp:
        repo, ctx = make_scratch_repo(tmp)
        return run([PY, "-B", REVIEW, "--repo", repo, "--paths"] + paths + ["--context", ctx,
                   "--config", os.path.join(tmp, "no-config.json"), "--dry-run"] + list(extra))


def check_review_pathspec_magic():
    rc, out = review_refusal([":(exclude)app.py"])
    ok = rc == 2 and "pathspec magic" in out and "DRY RUN" not in out
    return ok, "exit %d; %s" % (rc, out.strip().splitlines()[-1] if out.strip() else "no output")


def check_review_whole_repo():
    rc, out = review_refusal(["."])
    ok = rc == 2 and "whole repo" in out and "DRY RUN" not in out
    return ok, "exit %d; %s" % (rc, out.strip().splitlines()[-1] if out.strip() else "no output")


def check_review_metacharacters():
    results = [review_refusal(["app.py"], "--model", "x&calc") , review_refusal(["app.py"], "--effort", "high|x")]
    ok = all(rc == 2 and "may hold only" in out for rc, out in results)
    return ok, "; ".join("exit %d" % rc for rc, _ in results)


NESTED_FENCE_CONTEXT = """## Scope
Change the parser.

````markdown
```
## Not a section inside a nested fence
```
## Still inside the outer fence
````

## Pinned decisions
Standard library only.

## Not-mine
Anything else.
"""


def check_review_nested_fence():
    review = load_review()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "context.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(NESTED_FENCE_CONTEXT)
        ctx = review.read_context(path)
    ok = (sorted(ctx) == sorted(review.SECTIONS) and "## Still inside the outer fence" in ctx["Scope"]
          and ctx["Pinned decisions"] == "Standard library only.")
    return ok, "sections: %s" % sorted(ctx)


def check_review_other_drive():
    review = load_review()
    try:
        other = review.inside("C:\\work\\repo", "D:\\notes\\context.md", ntpath)
        same = review.inside("C:\\work\\repo", "c:\\WORK\\repo\\context.md", ntpath)
    except Exception as e:  # the old check raised "Paths don't have the same drive"
        return False, "raised %s: %s" % (type(e).__name__, e)
    return other is False and same is True, "D: outside=%s; same-drive inside=%s" % (not other, same)


PARSE_SAMPLE = """Some preamble the reviewer wrote.
FINDING | BLOCKING | tools/review.py:42 | the drive check raises instead of refusing
FINDING | NON-BLOCKING | README.md:7 | a sentence could be shorter
VERDICT: NO BLOCKING
"""
BROKEN_PARSE_SAMPLE = """FINDING - BLOCKING - tools/review.py:42 - the separators are wrong
VERDICT: NO BLOCKING
"""


def check_review_parse_output(sample=PARSE_SAMPLE):
    review = load_review()
    findings, verdict = review.parse_output(sample)
    want = [{"severity": "BLOCKING", "where": "tools/review.py:42",
             "summary": "the drive check raises instead of refusing"},
            {"severity": "NON-BLOCKING", "where": "README.md:7", "summary": "a sentence could be shorter"}]
    ok = findings == want and verdict == "BLOCKING"
    return ok, "%d finding(s) %s; verdict %s" % (
        len(findings), [(f["severity"], f["where"]) for f in findings], verdict)


def check_gateway_tests():
    rc, out = run([PY, "-B", GATEWAY_TESTS])
    tail = [l for l in out.strip().splitlines() if l.startswith(("Ran ", "OK", "FAILED"))]
    ok = rc == 0 and out.rstrip().endswith("OK")
    return ok, "exit %d; %s" % (rc, " ".join(tail))


def check_injected_failure():
    return False, "deliberately failing check injected by --selftest"


CHECKS = [
    ("runbook gate selftest", check_gate_selftest),
    ("dispatcher valid pick", check_dispatch_pick),
    ("dispatcher stale-pack refusal", check_dispatch_stale),
    ("dispatcher sensitive-data refusal", check_dispatch_sensitive),
    ("linear --check without key, no network", check_linear_no_key),
    ("review --dry-run command", check_review_dry_run),
    ("review contamination refusal", check_review_contamination),
    ("dispatcher refuses a null-sensitivity class", check_dispatch_null_sensitive),
    ("review refuses pathspec magic", check_review_pathspec_magic),
    ("review refuses --paths . (whole repo)", check_review_whole_repo),
    ("review refuses shell metacharacters", check_review_metacharacters),
    ("review reads a nested code fence", check_review_nested_fence),
    ("review: another drive is outside (ntpath)", check_review_other_drive),
    ("review parse_output reads a FINDING line", check_review_parse_output),
    ("gateway tests", check_gateway_tests),
]


def main(argv):
    checks = list(CHECKS)
    if "--selftest" in argv:
        checks.append(("injected failure (selftest)", check_injected_failure))
        checks.append(("parse_output on a broken sample (selftest)",
                       lambda: check_review_parse_output(BROKEN_PARSE_SAMPLE)))
    failed = 0
    for name, fn in checks:
        try:
            ok, detail = fn()
        except Exception as e:  # a crashing check is a failing check
            ok, detail = False, "crashed: %r" % e
        print("%s  %s :: %s" % ("PASS" if ok else "FAIL", name, detail))
        failed += 0 if ok else 1
    print("SMOKE: %d passed, %d failed" % (len(checks) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
