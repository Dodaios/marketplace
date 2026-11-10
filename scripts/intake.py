#!/usr/bin/env python3
"""Publish a submission that arrived as an issue. Stdlib + the gh CLI.

This is the registry's upload door for people who have no fork, no pull
request and no idea what either is: the Personal Jarvis app (and the issue
form on this repo) opens an issue whose body carries the submission as one
fenced JSON code block. This script runs from TRUSTED default-branch code on
the ``issues`` event and turns that issue into a published entry:

1. Read the issue from the event payload. The body is DATA: it is parsed as
   JSON, never executed, never interpolated into a shell.
2. Take the publisher from the issue's AUTHOR — the account GitHub itself
   authenticated to open the issue. Whatever ``publisher`` / ``publisher_id``
   the JSON claims is overwritten, so the identity proof is exactly as strong
   as the fork path's "you opened the pull request yourself".
3. Write ``submissions/<name>.json`` and run scripts/validate.py with the
   pre-change tree as the base, so naming, the secret scan, ownership
   (numeric account id) and the version bump are the same rules every pull
   request meets.
4. On success: commit to main as the bot, start the publish workflow, answer
   on the issue with the install line, label it and close it.
   On failure: answer with the validator's findings and label the issue
   ``needs-changes``; editing the issue runs this again.

No human approves anything (the maintainer's standing decision): the checks
are the gate, and a report after the fact delists.

Environment: GH_TOKEN (contents + issues write), REPO, GITHUB_EVENT_PATH.
``--dry-run`` prints the decision instead of touching git or the issue.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: An issue is a submission when its title starts with this tag (the issue
#: form and the app both set it) or when its body carries the marker. Every
#: other issue — a bug report, a delisting request — is left alone.
TITLE_TAG = "[publish]"
BODY_MARKER = "<!-- jarvis-marketplace-submission -->"

LABEL_PUBLISHED = "published"
LABEL_NEEDS_CHANGES = "needs-changes"

# The first fenced block tagged json (or untagged), the way the issue form
# renders a `render: json` textarea and the way the app writes its body.
FENCE_RE = re.compile(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.DOTALL)
NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?$")
MAX_BODY_BYTES = 65_536


class Refusal(Exception):
    """A submission problem the author can fix; the message is shown to them."""


def is_submission(issue: dict) -> bool:
    title = str(issue.get("title") or "").strip().lower()
    body = str(issue.get("body") or "")
    return title.startswith(TITLE_TAG) or BODY_MARKER in body


def extract_submission(body: str) -> dict:
    """The submission object from an issue body, or a Refusal saying why not."""
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise Refusal("the issue body is larger than GitHub keeps — shorten the submission")
    match = FENCE_RE.search(body)
    if match is None:
        raise Refusal(
            "no JSON code block found — paste the submission between ```json and ``` fences"
        )
    try:
        doc = json.loads(match.group(1))
    except ValueError as exc:
        raise Refusal(f"the code block is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise Refusal("the code block must hold one JSON object")
    return doc


def stamp_publisher(doc: dict, user: dict) -> dict:
    """Bind the submission to the account that opened the issue.

    The issue author is proven by GitHub; the JSON is not. So the author
    wins, always — a submission cannot publish under somebody else's name by
    writing it into the file.
    """
    if str(user.get("type") or "") != "User":
        raise Refusal("submissions are accepted from personal GitHub accounts only")
    login = user.get("login")
    account = user.get("id")
    if not isinstance(login, str) or isinstance(account, bool) or not isinstance(account, int):
        raise Refusal("the issue author could not be read from the event")
    stamped = dict(doc)
    stamped["publisher"] = login
    stamped["publisher_id"] = account
    return stamped


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    # UTF-8 both ways: the validator prints non-ASCII, and a runner's locale
    # (cp1252 on Windows) must not turn a report into a decode crash.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=check, env=env,
    )


def gh(*args: str) -> None:
    run("gh", *args, check=False)


def install_line(doc: dict) -> str:
    return f"dodaios marketplace install {doc['name']}"


def install_hint(doc: dict) -> str:
    """How the published entry installs, as the answer on the issue says it."""
    if doc.get("kind") == "look":
        # Looks install from the app's Marketplace; the command line does not
        # list them.
        return (
            "Install it in the app: open **Marketplace**, find it under **Looks** and "
            "press **Install**. Keep the release asset in place: the app downloads it "
            "from there and checks it against the pinned SHA-256."
        )
    return f"Install it with:\n\n```\n{install_line(doc)}\n```"


def answer(repo: str, number: int, body: str) -> None:
    gh("issue", "comment", str(number), "--repo", repo, "--body", body)


def ensure_labels(repo: str) -> None:
    gh("label", "create", LABEL_PUBLISHED, "--repo", repo, "--color", "0e8a16",
       "--description", "Submission published to the marketplace", "--force")
    gh("label", "create", LABEL_NEEDS_CHANGES, "--repo", repo, "--color", "d93f0b",
       "--description", "Submission did not pass the automated checks", "--force")


def validate(path: Path) -> tuple[bool, str]:
    """Run the authoritative validator with the pre-change tree as the base."""
    proc = run(
        sys.executable,
        str(ROOT / "scripts" / "validate.py"),
        "--base-ref",
        "HEAD",
        str(path),
        check=False,
    )
    # A crash is a failure too, and its traceback is the report.
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def restore(path: Path) -> None:
    """Leave the checkout as it was: the published version back, or no file."""
    rel = path.relative_to(ROOT).as_posix()
    if run("git", "ls-files", "--error-unmatch", "--", rel, check=False).returncode == 0:
        run("git", "checkout", "--", rel, check=False)
    else:
        path.unlink(missing_ok=True)


def commit_and_push(path: Path, doc: dict, number: int) -> bool:
    """Commit the submission to main; False when it changes nothing."""
    rel = path.relative_to(ROOT).as_posix()
    run("git", "add", "--", rel)
    if run("git", "diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    run("git", "config", "user.name", "marketplace-bot")
    run("git", "config", "user.email", "actions@github.com")
    run(
        "git",
        "commit",
        "-m",
        f"publish: {rel} by @{doc['publisher']} (#{number})",
    )
    for _ in range(3):
        if run("git", "push", "origin", "HEAD:main", check=False).returncode == 0:
            return True
        # Another publish landed in between: replay on top of it and retry.
        run("git", "pull", "--rebase", "origin", "main")
    raise RuntimeError("could not push the submission after three attempts")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_PATH", ""))
    args = parser.parse_args()

    event = json.loads(Path(args.event).read_text(encoding="utf-8"))
    issue = event.get("issue") or {}
    repo = os.environ.get("REPO") or (event.get("repository") or {}).get("full_name", "")
    number = int(issue.get("number") or 0)

    if issue.get("pull_request") or not is_submission(issue):
        print("not a submission issue — nothing to do")
        return 0
    if str(issue.get("state") or "open") != "open":
        print("issue is closed — nothing to do")
        return 0
    labels = {str(label.get("name")) for label in issue.get("labels") or []}
    if LABEL_PUBLISHED in labels:
        print("already published from this issue — open a new one for the next version")
        return 0

    try:
        submitted = extract_submission(str(issue.get("body") or ""))
        doc = stamp_publisher(submitted, issue.get("user") or {})
        name = doc.get("name")
        # The name becomes a file path below, so it is checked HERE, before
        # validate.py ever sees the file.
        if not isinstance(name, str) or not NAME_RE.fullmatch(name) or ".." in name or "--" in name:
            raise Refusal(f"name {name!r} breaks the naming rules (a-z 0-9 - . , max 64)")
    except Refusal as exc:
        print(f"refused: {exc}")
        if not args.dry_run:
            ensure_labels(repo)
            answer(repo, number, f"This submission could not be read: **{exc}**.\n\n"
                   "Edit the issue to fix it — the check runs again on every edit.")
            gh("issue", "edit", str(number), "--repo", repo, "--add-label", LABEL_NEEDS_CHANGES)
        return 0

    path = ROOT / "submissions" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
                    newline="\n")
    ok, report = validate(path)
    print(report)
    if not ok or args.dry_run:
        restore(path)
    if not ok:
        if not args.dry_run:
            ensure_labels(repo)
            answer(repo, number, "The automated checks found problems — edit the issue to fix "
                   "them and the check runs again:\n\n```\n" + report + "\n```")
            gh("issue", "edit", str(number), "--repo", repo, "--add-label", LABEL_NEEDS_CHANGES)
        return 0

    if args.dry_run:
        print(f"would publish {name}@{doc.get('version')} for @{doc['publisher']}")
        return 0

    changed = commit_and_push(path, doc, number)
    ensure_labels(repo)
    if changed:
        gh("workflow", "run", "publish.yml", "--repo", repo, "--ref", "main")
        message = (
            f"Published **{name}** {doc.get('version')} as @{doc['publisher']}. "
            "It reaches the marketplace feed within a few minutes.\n\n"
            f"{install_hint(doc)}\n\n"
            "To publish an update, open a new submission with a higher version."
        )
    else:
        message = (
            f"**{name}** {doc.get('version')} is already published exactly like this — "
            "nothing changed. Raise the version to publish an update."
        )
    answer(repo, number, message)
    gh("issue", "edit", str(number), "--repo", repo, "--add-label", LABEL_PUBLISHED,
       "--remove-label", LABEL_NEEDS_CHANGES)
    gh("issue", "close", str(number), "--repo", repo, "--reason", "completed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
