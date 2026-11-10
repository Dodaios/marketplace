#!/usr/bin/env python3
"""Regression test for publishing from the website. Stdlib + Node.

The publish page builds a submission in the visitor's browser and hands it to
GitHub as a filled-in issue. Nothing on our side sees it before the intake
does, so the two halves have to agree. This test runs the page's own logic
(site/publish.js, in Node) and feeds what it produces to the real intake and
the real validator:

- a dropped SKILL.md, and plain text with no frontmatter at all, both become
  submissions the validator accepts once the intake has stamped the publisher;
- the address the page opens carries a body the intake reads back unchanged;
- a submission too large for an address travels on the clipboard instead, to
  the registry's own issue form;
- the page stops what a person can fix before they leave it (a taken name, a
  version that does not go up, a pasted credential);
- the page reads a submission's state from the issue list the way the intake
  labels it.

Usage:
    python scripts/test_publish_flow.py
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = "https://github.com/Dodaios/marketplace"

NODE_SCRIPT = r"""
const p = require(process.argv[2]);
const registry = process.argv[3];
const out = {};

const skillMd = [
  "---", "name: Demo Skill", "description: >-", "  Does a thing.", "  On two lines.",
  "metadata:", '  version: "2.1.0"', "---", "", "# Demo Skill", "", "Do the thing.", "",
].join("\n");
const dropped = p.draftFromFiles([{ path: "demo/SKILL.md", text: skillMd }], "skill");
out.droppedFields = dropped.fields;
out.dropped = p.buildSubmission(dropped.kind, dropped.fields, dropped.parts);
out.droppedHandoff = p.handoff(out.dropped, registry);

out.plain = p.buildSubmission(
  "skill",
  { name: "plain-notes", version: "1.0.0", title: "Plain Notes", description: 'Says "hi": always.' },
  { skill_md: "Just take notes.\n" },
);

const big = p.buildSubmission(
  "skill",
  { name: "big-one", version: "1.0.0", title: "Big One", description: "A long skill." },
  { skill_md: "# Big\n\n" + "A line of instructions that goes on.\n".repeat(400) },
);
out.bigHandoff = p.handoff(big, registry);

const exported = { kind: "agent", name: "release-captain", version: "1.0.0", title: "Release Captain",
  description: "Cuts a release.", categories: ["team"],
  agent: { schema: 1, name: "Release Captain", instructions: "Cut the release." } };
const agentDraft = p.draftFromFiles([{ path: "release-captain.json", text: JSON.stringify(exported) }], "skill");
out.agent = p.buildSubmission(agentDraft.kind, agentDraft.fields, agentDraft.parts);

const plugin = p.draftFromFiles([
  { path: "fox/plugin.json", text: JSON.stringify({ name: "todo-fox", version: "1.2.0", description: "Tasks.",
      extensions: { "io.github.personaljarvis": { display_name: "TodoFox" } } }) },
  { path: "fox/mcp.json", text: JSON.stringify({ mcpServers: {} }) },
  { path: "fox/skills/triage/SKILL.md", text: "---\nname: triage\ndescription: x\n---\nbody" },
], "skill");
out.pluginKind = plugin.kind;
out.pluginFields = plugin.fields;
out.pluginSkills = (plugin.parts.skills || []).map((s) => s.name);

const context = {
  rules: { reserved: { plugin_ids: ["sentry"], skill_names: [] }, secret_patterns: ["ghp_[A-Za-z0-9]{20,}"] },
  published: { brief: { kind: "skill", version: "1.2.0", publisher: "octocat" } },
};
const fields = (o) => Object.assign({ name: "x", version: "1.0.0", title: "T", description: "D" }, o);
const check = (kind, f, parts) =>
  p.problemsOf(p.buildSubmission(kind, fields(f), parts || { skill_md: "Do it." }), context).map((q) => q.field);
out.problems = {
  ok: check("skill", {}),
  badName: check("skill", { name: "Not A Name" }),
  doubleDash: check("skill", { name: "a--b" }),
  badVersion: check("skill", { version: "1.0" }),
  sameVersion: check("skill", { name: "brief", version: "1.2.0" }),
  nextVersion: check("skill", { name: "brief", version: "1.2.1" }),
  takenByOtherKind: check("agent", { name: "brief" }, { agent: { instructions: "x" } }),
  reserved: check("plugin", { name: "sentry" }, { plugin_json: { extensions: {} } }),
  noDescription: check("skill", { description: " " }),
  noFile: check("skill", {}, { skill_md: "" }),
  secret: check("skill", {}, { skill_md: "token ghp_" + "a".repeat(30) }),
};

const now = Date.parse("2026-11-10T12:00:00Z");
const issue = (title, labels, created) => ({ title, html_url: "u", number: 7,
  labels: labels.map((name) => ({ name })), created_at: created || "2026-11-10T12:00:30Z" });
const sub = { name: "brief", version: "1.2.1" };
out.states = {
  none: p.issueState([issue("[publish] other 1.0.0", [])], sub, now),
  checking: p.issueState([issue("[publish] brief 1.2.1", [])], sub, now).state,
  published: p.issueState([issue("[Publish] brief 1.2.1", ["published"])], sub, now).state,
  needsChanges: p.issueState([issue("[publish] brief 1.2.1", ["needs-changes"])], sub, now).state,
  old: p.issueState([issue("[publish] brief 1.2.1", ["published"], "2026-11-09T12:00:00Z")], sub, now),
  prefixOnly: p.issueState([issue("[publish] briefing 1.0.0", [])], sub, now),
};
process.stdout.write(JSON.stringify(out));
"""


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main() -> int:
    node = shutil.which("node")
    if node is None:
        print("publish flow: SKIPPED (node is not installed)")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "run.cjs"
        script.write_text(NODE_SCRIPT, encoding="utf-8")
        proc = subprocess.run(
            [node, str(script), str(ROOT / "site" / "publish.js"), REGISTRY],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    if proc.returncode != 0:
        print("FAIL: the page logic did not run in Node:\n" + proc.stderr)
        return 1
    out = json.loads(proc.stdout)

    intake = load("intake")
    validate = load("validate")
    failures: list[str] = []

    def check(what: str, condition: bool) -> None:
        if not condition:
            failures.append(what)

    def accepted(doc: dict) -> list[str]:
        """Stamp a publisher like the intake does, then run the real validator."""
        stamped = intake.stamp_publisher(doc, {"type": "User", "login": "octocat", "id": 583231})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / f"{stamped['name']}.json"
            path.write_text(json.dumps(stamped, indent=2, ensure_ascii=False), encoding="utf-8")
            errors = validate.Errors()
            validate.validate_file(path, errors, None)
            return errors.items

    # --- a dropped SKILL.md --------------------------------------------------
    fields = out["droppedFields"]
    check("the name is read from the frontmatter", fields["name"] == "demo-skill")
    check("the title is read from the heading", fields["title"] == "Demo Skill")
    check("a folded description is read", fields["description"] == "Does a thing. On two lines.")
    check("a nested version is read", fields["version"] == "2.1.0")
    check("the frontmatter names the entry as the form does", "name: demo-skill" in out["dropped"]["skill_md"])
    check("a dropped SKILL.md passes the validator", accepted(out["dropped"]) == [])

    # --- plain text ----------------------------------------------------------
    check("plain text gets a frontmatter", out["plain"]["skill_md"].startswith("---\nname: plain-notes\n"))
    check("plain text passes the validator", accepted(out["plain"]) == [])

    # --- an exported agent ---------------------------------------------------
    check("an exported agent keeps its kind", out["agent"]["kind"] == "agent")
    check("an exported agent passes the validator", accepted(out["agent"]) == [])

    # --- a plugin folder -----------------------------------------------------
    check("a folder with plugin.json is a plugin", out["pluginKind"] == "plugin")
    check("the plugin's title comes from its manifest", out["pluginFields"]["title"] == "TodoFox")
    check("bundled skills travel with the plugin", out["pluginSkills"] == ["triage"])

    # --- the handoff ---------------------------------------------------------
    handoff = out["droppedHandoff"]
    check("a small submission is filled in for the visitor", handoff["mode"] == "prefill")
    query = parse_qs(urlsplit(handoff["url"]).query)
    issue = {"title": query["title"][0], "body": query["body"][0]}
    check("the address opens the registry's new-issue page", handoff["url"].startswith(REGISTRY + "/issues/new?"))
    check("the intake recognises the issue", intake.is_submission(issue))
    check("the intake reads the submission back unchanged", intake.extract_submission(issue["body"]) == out["dropped"])
    big = out["bigHandoff"]
    check("a large submission travels on the clipboard", big["mode"] == "paste")
    check("it goes to the registry's own form", "template=publish.yml" in big["url"])
    check("the address stays short", len(big["url"]) < 400)
    check("the clipboard holds the submission", json.loads(big["clipboard"])["name"] == "big-one")

    # --- what the page stops -------------------------------------------------
    problems = out["problems"]
    check("a good submission has no problems", problems["ok"] == [])
    check("a name with capitals and spaces is stopped", problems["badName"] == ["name"])
    check("a double hyphen is stopped", problems["doubleDash"] == ["name"])
    check("a two-part version is stopped", problems["badVersion"] == ["version"])
    check("a version that does not go up is stopped", problems["sameVersion"] == ["version"])
    check("the next version of a published name is fine", problems["nextVersion"] == [])
    check("a name published as another kind is stopped", problems["takenByOtherKind"] == ["name"])
    check("a built-in plugin's name is stopped", problems["reserved"] == ["name"])
    check("an empty description is stopped", problems["noDescription"] == ["description"])
    check("a skill without instructions is stopped", problems["noFile"] == ["file"])
    check("a pasted credential is stopped", problems["secret"] == ["file"])

    # --- reading the issue ---------------------------------------------------
    states = out["states"]
    check("no matching issue is no state", states["none"] is None)
    check("an unlabelled issue is being checked", states["checking"] == "checking")
    check("a published label is published", states["published"] == "published")
    check("a needs-changes label is a problem", states["needsChanges"] == "needs-changes")
    check("yesterday's issue is not this submission", states["old"] is None)
    check("a longer name is not this submission", states["prefixOnly"] is None)

    if failures:
        print("FAIL:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("publish flow: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
