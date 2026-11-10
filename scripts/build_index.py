#!/usr/bin/env python3
"""Compile the published registry into the static site the app consumes.

Output (default ``_site/``):
- ``index.json``  — the single feed the Dodaios app fetches (community
  plugins with embedded manifests, skills with their text and raw download
  URLs, and agent templates with the template embedded).
- ``rules.json``  — the generated submission rules, published so the upload
  endpoint and the app can read the limits instead of retyping them
  (scripts/export_rules.py).
- the storefront — ``index.html``, one page per entry, ``publish/`` and
  ``assets/``, rendered from that same feed by scripts/build_site.py and served
  at dodaios.ai/marketplace.

Keep the wire shape in sync with dodaios/marketplace/community_source.py in the
app repository (CommunityIndex / CommunityPluginEntry / CommunitySkillEntry /
CommunityAgentEntry).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTENSION_DIR = "io.github.personaljarvis"

# Largest file the feed embeds verbatim. Same number as validate.py's
# per-file submission limit (rules.json limits.max_file_bytes), so a file
# that passed the gate is always readable in the feed; anything bigger could
# only have arrived before that limit existed.
MAX_EMBED_BYTES = 131_072

REPO = os.environ.get("GITHUB_REPOSITORY", "Dodaios/marketplace")
BRANCH = "main"
TREE_URL = f"https://github.com/{REPO}/tree/{BRANCH}"
RAW_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}"


# The two frontmatter profiles a published skill can carry. "jarvis" is the
# app's own schema (triggers, execution mode, plugin coupling); "portable" is a
# plain Agent Skill in the open format — the file `npx skills add` installs
# into Claude Code, Cursor, Codex and the rest. Mirrors SKILL_FLAVORS in
# jarvis/marketplace/community_source.py.
SKILL_FLAVORS = ("jarvis", "portable")

# Frontmatter keys only Personal Jarvis defines. A file that uses one of them
# was written FOR Jarvis; a file that uses none runs anywhere a SKILL.md runs.
# `description` and `name` are deliberately absent — every skill has those.
JARVIS_ONLY_KEYS = frozenset(
    {
        "schema_version",
        "triggers",
        "requires_tools",
        "risk_policy",
        "execution",
        "auto_fire",
        "state",
        "config",
        "token_budget_estimate",
        "plugin_id",
        "intent_verbs",
        "intent_objects",
    }
)

TOP_LEVEL_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:", re.MULTILINE)

# What a compatibility name may be. Publisher-written free text that lands in
# the store UI, so the feed bounds it rather than the reader having to.
MAX_COMPATIBLE_AGENTS = 8
MAX_AGENT_NAME_CHARS = 32


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def frontmatter_keys(skill_md: str) -> set[str]:
    """The top-level keys of a SKILL.md's YAML frontmatter.

    Regex rather than a YAML parse for the same reason validate.py does it
    that way: this script runs on stdlib alone, and the question here is which
    keys were declared, not what they mean.
    """
    text = skill_md.lstrip()
    if not text.startswith("---"):
        return set()
    block, sep, _ = text[3:].partition("\n---")
    if not sep:
        return set()
    return {m.group(1) for m in TOP_LEVEL_KEY_RE.finditer(block)}


FRONTMATTER_RE = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
DESCRIPTION_KEY_RE = re.compile(r"^description[ \t]*:[ \t]*(.*)$")
# What YAML writes after the colon when the value follows on the next lines.
BLOCK_SCALAR_RE = re.compile(r"^[|>][+-]?\d*$")
MAX_LISTED_DESCRIPTION = 280


def frontmatter_description(skill_md: str) -> str:
    """The ``description`` a SKILL.md states about itself, as one paragraph.

    Stdlib only, so this reads the one key it needs instead of parsing YAML:
    a plain or quoted value on the key's own line, or a block scalar
    (``|``, ``>``, ``>-`` …) whose text is the indented lines that follow.
    """
    match = FRONTMATTER_RE.match(skill_md)
    if match is None:
        return ""
    lines = match.group(1).splitlines()
    for position, line in enumerate(lines):
        key = DESCRIPTION_KEY_RE.match(line)
        if key is None:
            continue
        value = key.group(1).strip()
        parts = [] if BLOCK_SCALAR_RE.match(value) or not value else [value]
        for continuation in lines[position + 1 :]:
            if continuation.strip() and not continuation.startswith((" ", "\t")):
                break  # the next top-level key
            parts.append(continuation.strip())
        text = " ".join(part for part in parts if part)
        if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
            text = text[1:-1]
        return text.strip()
    return ""


def listed_description(submission: dict, skill_md: str) -> str:
    """The text a skill is listed under.

    The submission form's text, unless it says nothing. It has: a publisher
    whose description was a YAML block scalar submitted the marker itself
    (``"|"``), and the store listed the skill as "No description yet." while
    the file beside it described it in full. The file is the fallback.
    """
    listed = str(submission.get("description") or "").strip()
    if re.search(r"\w", listed):
        return listed
    derived = " ".join(frontmatter_description(skill_md).split())
    if len(derived) <= MAX_LISTED_DESCRIPTION:
        return derived
    cut = derived[:MAX_LISTED_DESCRIPTION]
    stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if stop >= MAX_LISTED_DESCRIPTION // 2:
        return cut[: stop + 1]
    return cut.rsplit(" ", 1)[0].rstrip(",;:") + "…"


def skill_flavor(submission: dict, skill_md: str) -> str:
    """Which frontmatter this skill carries — declared, else derived.

    A publisher may state it outright. Most will not, so the default is read
    off the file itself: a SKILL.md that uses none of Jarvis' own keys is one
    that runs in any agent, and saying so is the whole point of the mark.
    Deriving beats demanding — a required field would turn every existing
    submission into a rejected one.
    """
    declared = submission.get("flavor")
    if isinstance(declared, str) and declared.strip().lower() in SKILL_FLAVORS:
        return declared.strip().lower()
    return "jarvis" if frontmatter_keys(skill_md) & JARVIS_ONLY_KEYS else "portable"


def compatible_agents(submission: dict) -> list[str]:
    """The publisher's "also runs in" list, cleaned and bounded."""
    raw = submission.get("compatible_agents")
    if not isinstance(raw, list):
        return []
    cleaned: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        name = " ".join(item.split())[:MAX_AGENT_NAME_CHARS].strip()
        if name and name not in cleaned:
            cleaned.append(name)
        if len(cleaned) >= MAX_COMPATIBLE_AGENTS:
            break
    return cleaned


def read_bundled_skills(plugin_dir: Path) -> list[dict]:
    """The package's skills/<name>/SKILL.md files, embedded verbatim.

    Only SKILL.md travels. A skill may reference other files in the standard,
    but community packages ship instructions, not payloads — see the
    submission rules.
    """
    skills_dir = plugin_dir / "skills"
    if not skills_dir.is_dir():
        return []
    bundled = []
    for skill_dir in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        skill_md = skill_dir / "SKILL.md"
        if skill_md.exists():
            bundled.append(
                {"name": skill_dir.name, "skill_md": skill_md.read_text(encoding="utf-8")}
            )
    return bundled


def collect_files(entry_dir: Path) -> list[dict]:
    """Every file of one published entry, path + size + verbatim text.

    This is the entry's whole folder, not a curated selection: the storefront
    renders it as a file browser so a visitor can read exactly what they are
    about to install, without a GitHub account and without trusting our
    summary of it. Embedded rather than linked for the same reason the skill
    body is (see the skill branch below) — a link inherits the availability of
    whatever host serves it.

    ``text`` is null for anything that is not UTF-8 text, or that exceeds the
    per-file submission limit (rules.json ``limits.max_file_bytes``); such a
    file still appears in the tree with its size, so the listing stays honest
    about what it carries. Nothing here can grow unbounded: validate.py caps
    both the size and the number of files a submission may contain.

    Root files come first, then deeper paths, each level alphabetical — the
    order a `tree` listing reads in.
    """
    files = []
    for path in entry_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(entry_dir).as_posix()
        raw = path.read_bytes()
        text: str | None = None
        if len(raw) <= MAX_EMBED_BYTES:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = None
        files.append({"path": rel, "size": len(raw), "text": text})
    files.sort(key=lambda f: (f["path"].count("/"), f["path"]))
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="_site")
    args = parser.parse_args()
    out = ROOT / args.out

    registry: dict[str, dict] = {}
    registry_path = ROOT / "registry.json"
    if registry_path.exists():
        registry = read_json(registry_path)

    plugins, skills, agents = [], [], []
    for name, meta in sorted(registry.items()):
        if meta.get("kind") == "plugin":
            plugin_dir = ROOT / "plugins" / name
            plugin_json_path = plugin_dir / "plugin.json"
            if not plugin_json_path.exists():
                continue
            mcp_path = plugin_dir / "mcp.json"
            card_path = plugin_dir / EXTENSION_DIR / "usage-card.md"
            plugins.append(
                {
                    "name": name,
                    "publisher": meta.get("publisher"),
                    # The account, not the name it currently goes by. A GitHub
                    # login can be renamed and the freed handle re-registered
                    # by a stranger, so a consumer that joins entries to a
                    # person by `publisher` alone hands one account's work to
                    # another the day somebody renames. The ledger keys
                    # ownership on this number (automerge_gate.py).
                    "publisher_id": meta.get("publisher_id"),
                    "version": meta.get("version"),
                    "published_at": meta.get("published_at"),
                    "source_url": f"{TREE_URL}/plugins/{name}",
                    "plugin_json": read_json(plugin_json_path),
                    "mcp_json": read_json(mcp_path) if mcp_path.exists() else None,
                    "skills": read_bundled_skills(plugin_dir),
                    "usage_card": (
                        card_path.read_text(encoding="utf-8") if card_path.exists() else None
                    ),
                    # The package as it was published, file by file.
                    "files": collect_files(plugin_dir),
                }
            )
        elif meta.get("kind") == "skill":
            skill_path = ROOT / "skills" / name / "SKILL.md"
            if not skill_path.exists():
                continue
            submission = read_json(ROOT / "submissions" / f"{name}.json")
            skill_md = skill_path.read_text(encoding="utf-8")
            skills.append(
                {
                    "name": name,
                    "title": submission.get("title", name),
                    "description": listed_description(submission, skill_md),
                    "publisher": meta.get("publisher"),
                    # See the plugin branch: the stable half of the identity.
                    "publisher_id": meta.get("publisher_id"),
                    "version": meta.get("version"),
                    "published_at": meta.get("published_at"),
                    "categories": submission.get("categories", []),
                    "source_url": f"{TREE_URL}/skills/{name}",
                    # Which frontmatter the file carries, and where else the
                    # publisher says it runs. A portable skill is installable
                    # with `npx skills add` into any agent that reads
                    # SKILL.md, and the store shows that command beside ours.
                    "flavor": skill_flavor(submission, skill_md),
                    "compatible_agents": compatible_agents(submission),
                    # The instructions themselves, not a link to them. A
                    # linked file inherits the availability of whatever host
                    # serves it: on 2026-08-14 this repo went private while
                    # Pages kept serving this index, and every raw_url in a
                    # live feed answered 404 — the store listed skills it
                    # could not install. raw_url stays for older clients.
                    "skill_md": skill_md,
                    "raw_url": f"{RAW_URL}/skills/{name}/SKILL.md",
                    # A skill is usually one file, but the folder is what was
                    # published — so the folder is what the feed carries.
                    "files": collect_files(ROOT / "skills" / name),
                }
            )

        elif meta.get("kind") == "agent":
            agent_path = ROOT / "agents" / name / "agent.json"
            if not agent_path.exists():
                continue
            submission = read_json(ROOT / "submissions" / f"{name}.json")
            agents.append(
                {
                    "name": name,
                    "title": submission.get("title", name),
                    "description": submission.get("description", ""),
                    "publisher": meta.get("publisher"),
                    # See the plugin branch: the stable half of the identity.
                    "publisher_id": meta.get("publisher_id"),
                    "version": meta.get("version"),
                    "published_at": meta.get("published_at"),
                    "categories": submission.get("categories", []),
                    "source_url": f"{TREE_URL}/agents/{name}",
                    # The template itself, embedded for the same reason the
                    # skill body is: installing must not depend on a second
                    # host being up.
                    "agent": read_json(agent_path),
                    "files": collect_files(ROOT / "agents" / name),
                }
            )

    index = {
        # Monotonic enough for cache-busting: the workflow run number, or a
        # timestamp when built locally.
        "revision": int(os.environ.get("GITHUB_RUN_NUMBER", 0))
        or int(datetime.now(UTC).timestamp()),
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "plugins": plugins,
        "skills": skills,
        "agents": agents,
    }

    out.mkdir(parents=True, exist_ok=True)
    (out / "index.json").write_text(
        json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    # The storefront, rendered from the feed that was just written: the pages
    # a person reads can never be about a different index than the app's.
    # (The path line is for callers that load this file by path, not from scripts/.)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import build_site

    pages = build_site.build(index, out)

    # Ship the generated rules beside the feed so the upload endpoint and the
    # app read the limits rather than retyping them. The index still deploys
    # without it — a missing feed is worse than a missing rule sheet.
    rules_path = ROOT / "rules.json"
    if rules_path.exists():
        (out / "rules.json").write_text(rules_path.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        print("WARNING: rules.json missing — run scripts/export_rules.py", file=sys.stderr)

    print(
        f"index: {len(plugins)} plugin(s), {len(skills)} skill(s), "
        f"{len(agents)} agent(s) -> {out / 'index.json'}; storefront: {pages} page(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
