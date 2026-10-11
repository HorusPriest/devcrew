#!/usr/bin/env python3
"""Refuse a runbook unless every field in templates/runbook.md is present and non-empty.

Usage:
  python tools/runbook_gate.py RUNBOOK_FILE [--template PATH]
  python tools/runbook_gate.py --selftest

The field list is read from the template, so adding a field to the template
adds it to the gate. A field passes when its line is present and its value
(the rest of the line plus any non-blank lines directly under it, up to a
blank line or a code fence) is not empty and is not the template's own
placeholder text. The command line at the
top (for example `/run-ticket root=... team=...`) must carry every key the
template's command line carries, each with a value.

Exit codes: 0 accepted, 1 refused, 2 usage or file error.
Python standard library only.
"""
import argparse
import contextlib
import io
import os
import re
import shlex
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TEMPLATE = os.path.join(HERE, os.pardir, "templates", "runbook.md")

FIELD_RE = re.compile(r"^([A-Z][A-Z0-9 ,\-]*[A-Z0-9]):(.*)$")


def parse_template(text):
    """Return (command_name, {key: placeholder}, [(field, placeholder), ...])."""
    command, keys, fields = None, {}, []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if command is None and s.startswith("/"):
            tokens = shlex.split(s)
            command = tokens[0]
            for tok in tokens[1:]:
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    keys[k] = v.strip()
            continue
        m = FIELD_RE.match(s)
        if m:
            fields.append((m.group(1), m.group(2).strip()))
    if not fields:
        raise ValueError("template has no fields")
    return command, keys, fields


def _field_start(line, names):
    """If the line starts a known field, return (name, rest_of_line)."""
    for name in names:
        if line.startswith(name):
            rest = line[len(name):]
            if rest == "":
                return name, ""
            if rest.startswith(":"):
                return name, rest[1:]
            if rest.startswith("\t") or rest.startswith("  "):
                return name, rest
    return None


def _fixed_only(value, placeholder):
    """True when the value holds only the template's fixed text, with every <...> part removed."""
    fixed = re.sub(r"<[^<>]*>", "", placeholder).strip(" ,;:")
    return value.strip(" ,;:") == fixed


def check(runbook_text, template_text):
    """Return a list of problems; an empty list means the runbook is accepted."""
    command, keys, fields = parse_template(template_text)
    placeholders = dict(fields)
    names = sorted(placeholders, key=len, reverse=True)
    values = {}
    current = None
    command_line = None
    for raw in runbook_text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith(("```", "~~~")):
            current = None  # a blank line or a code fence ends a field
            continue
        if command and command_line is None and line.strip().startswith(command):
            command_line = line.strip()
            current = None
            continue
        hit = _field_start(line.strip(), names)
        if hit:
            current = hit[0]
            values.setdefault(current, []).append(hit[1].strip())
            continue
        if current is not None:
            values[current].append(line.strip())

    problems = []
    if command:
        if command_line is None:
            problems.append("missing command line: %s" % command)
        else:
            try:
                tokens = shlex.split(command_line)
            except ValueError as e:
                tokens = []
                problems.append("command line does not parse: %s" % e)
            given = {}
            for tok in tokens[1:]:
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    given[k] = v.strip()
            for k, placeholder in keys.items():
                if k not in given:
                    problems.append("missing command key: %s=" % k)
                elif not given[k]:
                    problems.append("empty command key: %s=" % k)
                elif given[k] == placeholder:
                    problems.append("unfilled command key: %s= (still the template placeholder)" % k)
    for name, placeholder in fields:
        if name not in values:
            problems.append("missing field: %s" % name)
            continue
        value = " ".join(v for v in values[name] if v).strip()
        if not value:
            problems.append("empty field: %s" % name)
        elif "<" in placeholder and (value == placeholder or _fixed_only(value, placeholder)):
            problems.append("unfilled field: %s (still the template placeholder)" % name)
    return problems


def run(path, template_path):
    try:
        with open(template_path, encoding="utf-8") as f:
            template_text = f.read()
        with open(path, encoding="utf-8") as f:
            runbook_text = f.read()
    except OSError as e:
        print("ERROR: %s" % e)
        return 2
    try:
        problems = check(runbook_text, template_text)
    except ValueError as e:
        print("ERROR: %s: %s" % (template_path, e))
        return 2
    if problems:
        for p in problems:
            print("REFUSED: %s: %s" % (path, p))
        print("REFUSED: %s: %d problem(s); fix the runbook before launch" % (path, len(problems)))
        return 1
    _, keys, fields = parse_template(template_text)
    print("ACCEPTED: %s: all %d fields and %d command keys present and non-empty"
          % (path, len(fields), len(keys)))
    return 0


def selftest(template_path):
    with open(template_path, encoding="utf-8") as f:
        template_text = f.read()
    complete = re.sub(r"<[^<>]*>", "filled in", template_text)
    lines = complete.splitlines()
    _, _, fields = parse_template(template_text)
    drop = fields[1][0]
    blank = fields[2][0]
    missing = "\n".join(l for l in lines if not l.startswith(drop + ":")) + "\n"
    empty = "\n".join((blank + ":") if l.startswith(blank + ":") else l for l in lines) + "\n"
    cases = [
        ("missing field %s" % drop, missing, 1, "missing field: %s" % drop),
        ("empty field %s" % blank, empty, 1, "empty field: %s" % blank),
        ("unfilled template", template_text, 1, "still the template placeholder"),
        ("complete", complete, 0, "ACCEPTED"),
    ]
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        for i, (label, text, want_rc, want_text) in enumerate(cases):
            p = os.path.join(tmp, "fixture-%d.md" % i)
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
            print("-- case: %s (expect %s)" % (label, "RED" if want_rc else "GREEN"))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = run(p, template_path)
            out = buf.getvalue()
            sys.stdout.write(out.replace(p, "<fixture>"))
            good = rc == want_rc and want_text in out
            print("   -> %s (exit %d) %s" % ("RED" if rc else "GREEN", rc, "as expected" if good else "UNEXPECTED"))
            ok = ok and good
    print("SELFTEST %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("runbook", nargs="?", help="runbook file to check")
    ap.add_argument("--template", default=DEFAULT_TEMPLATE, help="runbook template (default: templates/runbook.md)")
    ap.add_argument("--selftest", action="store_true", help="show the gate RED on bad fixtures and GREEN on a good one")
    a = ap.parse_args(argv)
    template = os.path.normpath(a.template)
    if a.selftest:
        return selftest(template)
    if not a.runbook:
        ap.error("give a runbook file, or --selftest")
    return run(a.runbook, template)


if __name__ == "__main__":
    sys.exit(main())
