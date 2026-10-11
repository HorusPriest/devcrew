#!/usr/bin/env python3
"""A small client for Linear's GraphQL API. Python standard library only.

The API key is read from the LINEAR_API_KEY environment variable. It is sent
only in the Authorization header and is never printed, logged or written.

Usage:
  python tools/linear_client.py --check
      Prints only the viewer name and the organisation name.
  python tools/linear_client.py comment ISSUE --body-file FILE
      Adds a comment (Markdown, read from FILE) to ISSUE (id or identifier).
  python tools/linear_client.py create-issue --team-id TEAM_ID --title TITLE --body-file FILE [--project-id ID]
      Creates an issue and prints its identifier and URL.

Bodies are read from a file, never passed on the command line, so prose with
quotes and apostrophes survives intact.

As a library:
  from linear_client import graphql, create_issue, add_comment

Exit codes: 0 ok, 1 API or network error, 2 key missing or usage error.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API_URL = "https://api.linear.app/graphql"
KEY_ENV = "LINEAR_API_KEY"
TIMEOUT_SECONDS = 30


class LinearError(Exception):
    """An API, network or configuration error. Its message never contains the key."""


def _api_key():
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        raise LinearError("%s is not set; export your Linear personal API key in that variable "
                          "(nothing was sent over the network)" % KEY_ENV)
    if any(c.isspace() or not c.isprintable() for c in key):
        raise LinearError("%s contains whitespace or control characters; set it to the key alone "
                          "(the value is not shown; nothing was sent over the network)" % KEY_ENV)
    return key


def _scrub(text, key):
    """Remove the key, and its escaped forms, from a message."""
    if not key:
        return text
    for form in (key, repr(key)[1:-1], repr(key.encode("utf-8"))[2:-1]):
        if form:
            text = text.replace(form, "[redacted]")
    return text


def graphql(query, variables=None):
    """Run a GraphQL query or mutation and return its `data` object."""
    key = _api_key()
    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": key,
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise LinearError(_scrub("HTTP %d from Linear: %s" % (e.code, detail), key)) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise LinearError(_scrub("request to Linear failed: %s" % e, key)) from None
    if payload.get("errors"):
        msgs = "; ".join(str(err.get("message", err)) for err in payload["errors"])
        raise LinearError(_scrub("Linear returned errors: %s" % msgs, key))
    return payload.get("data") or {}


def viewer_and_org():
    data = graphql("query { viewer { name } organization { name } }")
    return data["viewer"]["name"], data["organization"]["name"]


def create_issue(team_id, title, description="", project_id=None):
    """Create an issue. Returns {id, identifier, url}."""
    issue_input = {"teamId": team_id, "title": title, "description": description}
    if project_id:
        issue_input["projectId"] = project_id
    data = graphql(
        "mutation($input: IssueCreateInput!) { issueCreate(input: $input) "
        "{ success issue { id identifier url } } }",
        {"input": issue_input},
    )
    result = data.get("issueCreate") or {}
    if not result.get("success"):
        raise LinearError("issueCreate did not succeed")
    return result["issue"]


def add_comment(issue_id, body):
    """Add a Markdown comment to an issue (id or identifier). Returns {id, url}."""
    data = graphql(
        "mutation($input: CommentCreateInput!) { commentCreate(input: $input) "
        "{ success comment { id url } } }",
        {"input": {"issueId": issue_id, "body": body}},
    )
    result = data.get("commentCreate") or {}
    if not result.get("success"):
        raise LinearError("commentCreate did not succeed")
    return result["comment"]


def _read_body(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Small Linear GraphQL client (key from %s)." % KEY_ENV)
    ap.add_argument("--check", action="store_true", help="print the viewer name and organisation name only")
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("comment", help="add a comment to an issue")
    c.add_argument("issue", help="issue id or identifier")
    c.add_argument("--body-file", required=True)
    i = sub.add_parser("create-issue", help="create an issue")
    i.add_argument("--team-id", required=True)
    i.add_argument("--title", required=True)
    i.add_argument("--body-file", required=True)
    i.add_argument("--project-id")
    a = ap.parse_args(argv)
    if not a.check and not a.cmd:
        ap.print_help()
        return 2
    try:
        _api_key()  # fail before any network use when the key is absent
    except LinearError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 2
    try:
        if a.check:
            viewer, org = viewer_and_org()
            print("viewer: %s" % viewer)
            print("organization: %s" % org)
        elif a.cmd == "comment":
            comment = add_comment(a.issue, _read_body(a.body_file))
            print("comment added: %s" % comment.get("url", comment.get("id")))
        elif a.cmd == "create-issue":
            issue = create_issue(a.team_id, a.title, _read_body(a.body_file), a.project_id)
            print("issue created: %s %s" % (issue["identifier"], issue["url"]))
    except (LinearError, OSError) as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
