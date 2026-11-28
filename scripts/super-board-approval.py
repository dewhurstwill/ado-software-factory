#!/usr/bin/env python3
"""Read-only, head-bound human approval shared by the planner and merge gate.

A trusted request pins the code and human steps BEFORE a human replies `done`.
Labels only describe UI state. They never authorize a merge. No forge writes.

Provider: --config <file> (key "provider", GitHub when absent; read once, may be a FIFO).
GitHub runs the same `gh` reads through super-board-github-read.py as it always did
(argv built by super-board-forge.py); trust is the commenter's current collaborator
permission. Azure DevOps reads through the forge's backend; trust is the config allow-list
`ado.approvers`. Rules and the Azure DevOps mapping: references/providers.md ("Human approval").
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from datetime import datetime
import hashlib
import json
import re
import subprocess
import sys


def _load(name, filename):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # the forge binds its backends to sys.modules[__name__]
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


forge = _load("super_board_forge", "super-board-forge.py")
github_read = forge.github_read
GH = forge.GitHubBackend

PREFIX = "approval-request: "
SHA = re.compile(r"[0-9a-f]{40}\Z")
SCOPE = re.compile(r"[0-9a-f]{64}\Z")
BLOCK = re.compile(r"Reason tag:[^\n]*🙋|^approval-request:", re.M)
DONE = re.compile(r"\s*done[.!]?\s*\Z", re.I)
# Azure DevOps' rich-text work item editor stores a typed `done` as one HTML element.
HTML_DONE = re.compile(r"\s*<(div|p)>\s*(done[.!]?)\s*(?:<br\s*/?>\s*)?</\1>\s*\Z", re.I)
CLOSES = forge.CLOSES
WRITE = ("write", "maintain", "admin")
# The request's `repo`: GitHub owner/name; Azure DevOps <org>/<project>/<repository> (names may hold spaces).
REPO_FORMS = {"github": re.compile(r"[^/\s]+/[^/\s]+"),
              "azure-devops": re.compile(r"[^/\n]+/[^/\n]+/[^/\n]+")}
UNVERIFIABLE = (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError,
                forge.ForgeError, forge.Usage, forge.Unavailable)


def body_links(body, repo):
    """Same-repo issues a PR body closes; other-repo references are ignored."""
    return GH.body_links(body, repo)


def scope_for(plan):
    # Exclude UI-only `done`; a label change must not change what was approved.
    relevant = {key: plan.get(key, []) for key in ("human", "migrations", "run", "needs_you")}
    return hashlib.sha256(json.dumps(relevant, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def request_for(repo, pr, head, scope):
    return {"repo": repo.lower(), "pr": int(pr), "head": head, "scope": scope}


def marker(request):
    return PREFIX + json.dumps(request, sort_keys=True, separators=(",", ":"))


def parse_request(comment, repo_form=None):
    lines = [line for line in (comment.get("body") or "").splitlines() if line.startswith(PREFIX)]
    if len(lines) != 1:
        return None
    try:
        value = json.loads(lines[0][len(PREFIX):])
        if (not isinstance(value, dict) or not isinstance(value.get("repo"), str)
                or not (repo_form or REPO_FORMS["github"]).fullmatch(value["repo"])
                or type(value.get("pr")) is not int or value["pr"] < 1
                or not SHA.fullmatch(value.get("head", ""))
                or not SCOPE.fullmatch(value.get("scope", ""))):
            return None
        return value
    except (ValueError, TypeError):
        return None


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("missing comment timestamp")
    # Azure DevOps sends 1 to 7 fraction digits; older Pythons parse only 3 or 6. Truncating can
    # only make two times equal (a tie holds), never reorder them.
    value = re.sub(r"\.(\d+)(?=(?:Z|[+-]\d\d:\d\d)\Z)", lambda m: "." + (m.group(1) + "000000")[:6], value)
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("comment timestamp lacks timezone")
    return result


def trusted_blocks(comments, permission):
    result = []
    for block in comments:
        if not BLOCK.search(block.get("body") or ""):
            continue
        user = block.get("user") or {}
        if not user.get("login") or user.get("type") not in ("User", "Bot"):
            raise ValueError("requester identity could not be verified")
        if permission(user["login"]) in WRITE:
            result.append(block)
    return result


def validate(comments, expected, permission, approver=None, repo_form=None):
    """Pure verdict; permission(login) is a current repository permission lookup: who may post a
    request or a block. approver(login) -> bool, when given, decides who may reply `done`
    (Azure DevOps: the allow-list); otherwise permission decides both (GitHub). A comment with
    `edited` set was changed after posting, whatever its timestamps say."""
    def hold(why):
        return {"approved": False, "why": why}

    def may_approve(login):
        return approver(login) if approver is not None else permission(login) in WRITE

    if not SHA.fullmatch(expected.get("head", "")):
        return hold("current PR head is unavailable")
    try:
        blocks = trusted_blocks(comments, permission)
        if not blocks:
            return hold("no request from a trusted repository collaborator")
        # Any later human block supersedes the old request, including one with
        # missing/malformed metadata. Equal-second requests are ambiguous: hold.
        latest_time = max(timestamp(c.get("created_at")) for c in blocks)
        latest = [c for c in blocks if timestamp(c.get("created_at")) == latest_time]
        if len(latest) != 1:
            return hold("ambiguous newest human request")
        request_comment = latest[0]
        request = parse_request(request_comment, repo_form)
        if request is None or any(request.get(k) != v for k, v in expected.items()):
            return hold("the latest request does not cover the current code and human steps")
        if request_comment.get("edited") or timestamp(request_comment.get("updated_at")) != latest_time:
            return hold("approval request was edited; post a fresh request")
        requester = request_comment.get("user") or {}
        if not requester.get("login") or permission(requester["login"]) not in WRITE:
            return hold("requester authority could not be verified")
        source = request_comment.get("source")
        if isinstance(source, bool) or not isinstance(source, (int, str)) or source == "" or not request_comment.get("id"):
            return hold("request identity is missing")
        for comment in comments:
            if (not comment.get("id") or not DONE.fullmatch(comment.get("body") or "")
                    or comment.get("source") != source or comment.get("edited")):
                continue
            created = timestamp(comment.get("created_at"))
            if created <= latest_time or timestamp(comment.get("updated_at")) != created:
                continue
            author = comment.get("user") or {}
            if author.get("type") == "User" and author.get("login") and may_approve(author["login"]):
                return {"approved": True, "why": "trusted human confirmed the current request",
                        "request": request, "requestId": request_comment.get("id"),
                        "approvalId": comment.get("id"), "approver": author["login"]}
        return hold("waiting for a trusted human to comment done on the current request")
    except (ValueError, TypeError, KeyError):
        return hold("approval evidence is incomplete or unreadable")


class GitHub:
    """Comments on the PR and on the issues it closes (one number space); trust = the
    commenter's current write, maintain or admin permission."""
    repo_form = REPO_FORMS["github"]

    def __init__(self, repo):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("missing or invalid repository")
        self.repo = repo.lower()
        self.permissions = {}

    @staticmethod
    def read(*args):
        kind = ("approval" if "graphql" in args else "comments" if "--paginate" in args
                else "permission" if args[-1].endswith("/permission") else "json")
        return github_read.read(list(args), kind)[1]

    def permission(self, login):
        if not forge.GITHUB_LOGIN.fullmatch(login):
            return None
        if login not in self.permissions:
            value = self.read(*GH.permission_argv(self.repo, login))
            self.permissions[login] = value.get("permission")
        return self.permissions[login]

    def comments(self, number):
        pages = self.read(*GH.comments_argv(self.repo, number))
        if not isinstance(pages, list) or not pages or any(not isinstance(p, list) for p in pages):
            raise ValueError("unreadable comments")
        if any(not isinstance(c, dict) for page in pages for c in page):
            raise ValueError("unreadable comment")
        return [dict(comment, source=number) for page in pages for comment in page]

    issue_comments = comments

    def pr(self, number):
        snapshot = GH.approval_snapshot(self.read(*GH.approval_argv(self.repo, number)), self.repo)
        if snapshot["state"] != "OPEN":
            raise ValueError("PR is not open")
        return snapshot["headRefOid"], set(snapshot["closes"])

    def check(self, number, head=None, scope=None, issue=None):
        current_head, issues = self.pr(number)
        if issue is not None and issue not in issues:
            return {"approved": False, "why": "request issue is not linked to this PR"}
        if head is not None and current_head != head:
            return {"approved": False, "headChanged": True, "why": "PR head changed after review"}
        comments = []
        for source in sorted(issues | {number}):
            comments.extend(self.comments(source))
        expected = {"repo": self.repo, "pr": number, "head": current_head}
        if scope is not None:
            expected["scope"] = scope
        return validate(comments, expected, self.permission)


class AzureDevOps:
    """PR thread comments plus the comments of the work items the PR links (two number spaces,
    so a comment's source is "pr:<id>" or "workitem:<id>"). Requests and 🙋 blocks count from an
    approver or from the token's own identity (the pack posts them); `done` counts only from an
    approver in config ado.approvers, the token's own identity included when it is listed (a solo
    owner, as on GitHub). File-anchored PR threads are ignored, as GitHub review comments are."""
    repo_form = REPO_FORMS["azure-devops"]

    def __init__(self, config):
        self.backend = forge.AzureDevOpsBackend(config, None)
        forge.LABEL["name"] = "azure-devops"
        self.repo = self.backend.resolve_repo().lower()
        if not self.repo_form.fullmatch(self.repo):
            raise ValueError("missing or invalid Azure DevOps repository")

    def approver(self, login):
        return self.backend.may_approve(login)

    def permission(self, login):
        if self.approver(login):
            return "write"
        me = self.backend.whoami_unique()
        return "write" if me and isinstance(login, str) and login.casefold() == me.casefold() else None

    def pr_comments(self, number):
        return [{"id": row["id"], "body": row["body"], "user": {"login": row["uniqueName"], "type": "User"},
                 "created_at": row["created_at"], "updated_at": row["contentUpdated"], "source": f"pr:{number}"}
                for row in self.backend.pr_comment_rows(number) if not row["inline"]]

    def issue_comments(self, number):
        out = []
        for row in self.backend.ticket_comment_rows(number, strict=True):
            body = row["body"]
            found = HTML_DONE.fullmatch(body or "")
            out.append({"id": row["id"], "body": found.group(2) if found else body,
                        "user": {"login": row["uniqueName"], "type": "User"},
                        "created_at": row["created_at"], "updated_at": row["updated_at"],
                        # version 1 = never edited; anything else (or none) counts as edited
                        "edited": not (type(row.get("version")) is int and row["version"] == 1),
                        "source": f"workitem:{number}"})
        return out

    def pr(self, number):
        snapshot = self.backend.approval_snapshot(number)
        if snapshot["state"] != "OPEN":
            raise ValueError("PR is not open")
        if not SHA.fullmatch(snapshot["headRefOid"]):
            raise ValueError("unreadable PR head")
        return snapshot["headRefOid"], set(snapshot["closes"])

    def check(self, number, head=None, scope=None, issue=None):
        current_head, issues = self.pr(number)
        if issue is not None and issue not in issues:
            return {"approved": False, "why": "request issue is not linked to this PR"}
        if head is not None and current_head != head:
            return {"approved": False, "headChanged": True, "why": "PR head changed after review"}
        comments = self.pr_comments(number)
        for source in sorted(issues):
            comments.extend(self.issue_comments(source))
        # Two reads, not one snapshot: the PR must not have moved while its comments were read.
        if self.pr(number) != (current_head, issues):
            raise ValueError("PR changed during the approval read")
        expected = {"repo": self.repo, "pr": number, "head": current_head}
        if scope is not None:
            expected["scope"] = scope
        return validate(comments, expected, self.permission, approver=self.approver, repo_form=self.repo_form)


def load_config(path):
    if not path:
        return {}
    return forge.load_config(path)


def source_for(config, repo):
    provider = config.get("provider", "github")
    if provider == "azure-devops":
        return AzureDevOps(config)
    if provider == "github":
        return GitHub(repo or GH(config, None).explicit_repo())
    raise ValueError(f"unknown provider {provider!r}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default="", help="GitHub owner/name (else config repo.remote); "
                    "with --deps, empty means offline")
    ap.add_argument("--config", help="project config; selects the provider (GitHub when absent)")
    ap.add_argument("--pr", type=int)
    ap.add_argument("--head")
    ap.add_argument("--plan", help="JSON plan from merge-policy")
    ap.add_argument("--request", action="store_true", help="print a pinned request; no network or writes")
    ap.add_argument("--deps", action="store_true", help="enrich the dependency graph on stdin")
    args = ap.parse_args()
    try:
        config = load_config(args.config)
    except forge.ForgeError as error:
        config, config_error = None, str(error)
    else:
        config_error = ""
    if args.request:
        if not args.pr or not args.head or not SHA.fullmatch(args.head) or not args.plan:
            ap.error("request requires --pr, full --head and --plan")
        if config is None:
            ap.error(config_error)
        if config.get("provider", "github") == "github":
            repo = args.repo or GH(config, None).explicit_repo()
            if not repo:
                ap.error("request requires --repo (or config repo.remote)")
        else:
            try:
                repo = source_for(config, args.repo).repo
            except UNVERIFIABLE as error:
                ap.error(str(error))
        print(marker(request_for(repo, args.pr, args.head, scope_for(json.loads(args.plan)))))
        return
    if args.deps:
        graph = json.load(sys.stdin)
        source = None
        if args.repo and config is not None:
            try:
                source = source_for(config, args.repo)
            except UNVERIFIABLE:
                source = None
        for entry in graph.values():
            entry["needsYouDone"] = False
            entry["approvalRefresh"] = False
            if not entry.get("needsYou") or not args.repo:
                continue
            if source is None:
                entry["approvalWhy"] = "approval evidence could not be verified"
                continue
            try:
                comments = source.issue_comments(entry["number"])
                blocks = trusted_blocks(comments, source.permission)
                latest = max(blocks, key=lambda c: timestamp(c.get("created_at"))) if blocks else {}
                request = parse_request(latest, source.repo_form)
                if request is None or request["repo"] != source.repo:
                    continue
                result = source.check(request["pr"], head=request["head"], issue=entry["number"])
                entry["needsYouDone"] = result["approved"]
                entry["approvalRefresh"] = result.get("headChanged", False)
                entry["approvalWhy"] = result["why"]
            except UNVERIFIABLE:
                entry["approvalWhy"] = "approval evidence could not be verified"
        print(json.dumps(graph))
        return
    if not args.pr or not args.head or not args.plan:
        ap.error("check requires --pr, --head and --plan")
    try:
        if config is None:
            raise ValueError(config_error)
        result = source_for(config, args.repo).check(args.pr, args.head, scope_for(json.loads(args.plan)))
    except UNVERIFIABLE:
        result = {"approved": False, "why": "approval evidence could not be verified"}
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except github_read.ReadHalted as error:
        print(f"{'Azure DevOps' if forge.LABEL['name'] == 'azure-devops' else 'GitHub'} read halt: {error}",
              file=sys.stderr)
        sys.exit(github_read.HALTED)
