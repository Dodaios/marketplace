#!/usr/bin/env python3
"""Regression test for the storefront and the listing text. Stdlib only.

The storefront prints what strangers published, merged with no human review.
So the rules that matter are pinned here:

- nothing from the feed reaches a page as markup: a title, a description, a
  file or a Markdown link cannot open a tag or run a script;
- a link is only made from an http(s) address;
- every entry gets a page, with the install line and every published file;
- a skill whose listing text says nothing is listed under what its own
  SKILL.md says, in every YAML form a description can be written in;
- an entry is the same colour here as in the app (the hue is a shared rule).

Usage:
    python scripts/test_site.py
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


HOSTILE = '<img src=x onerror=alert(1)>"\'</script><script>alert(2)</script>'

SKILL_MD = """---
name: brief
description: >-
  Turns a topic into three bullets.
  Use it for a short version.
---

# Brief

Write **three** bullets. See [the guide](https://example.com/guide) and
[a trap](javascript:alert(1)).

1. First
   - nested point
2. Second

| Field | Meaning |
| --- | --- |
| a | `code <b>` |

```
<script>alert(3)</script>
```
"""


def index() -> dict:
    return {
        "revision": 1,
        "generated_at": "2026-11-10T09:00:00Z",
        "plugins": [
            {
                "name": "todo-fox",
                "publisher": "octocat",
                "publisher_id": 583231,
                "version": "1.2.0",
                "published_at": "2026-10-01T10:00:00Z",
                "source_url": "https://github.com/Dodaios/marketplace/tree/main/plugins/todo-fox",
                "plugin_json": {
                    "name": "todo-fox",
                    "description": f"Tasks {HOSTILE}",
                    "extensions": {
                        "io.github.dodaios": {
                            "display_name": f"TodoFox {HOSTILE}",
                            "category": "Lists",
                            "logo_slug": "todoist",
                            "logo_color": "E44332",
                            "auth": {"mode": "pat_paste"},
                        }
                    },
                },
                "mcp_json": {
                    "mcpServers": {
                        "todo-fox": {"type": "stdio", "command": "npx", "args": ["-y", "fox@1.0.0"]}
                    }
                },
                "files": [{"path": "plugin.json", "size": 20, "text": '{"name": "<b>"}'}],
            }
        ],
        "skills": [
            {
                "name": "brief",
                "title": "Brief",
                "description": "Turns a topic into three bullets.",
                "publisher": "octocat",
                "version": "1.0.0",
                "published_at": "2026-11-01T10:00:00Z",
                "categories": ["writing"],
                "flavor": "portable",
                "compatible_agents": [],
                "source_url": "javascript:alert(4)",
                "files": [
                    {"path": "SKILL.md", "size": len(SKILL_MD), "text": SKILL_MD},
                    {"path": "logo.png", "size": 2048, "text": None},
                ],
            }
        ],
        "agents": [
            {
                "name": "release-captain",
                "title": "Release Captain",
                "description": "Cuts a release.",
                "publisher": "octocat",
                "agent": {"name": "Release Captain", "require_approval": ["shell"]},
                "files": [],
            }
        ],
    }


def main() -> int:
    site = load("build_site")
    build_index = load("build_index")
    failures: list[str] = []

    def check(what: str, condition: bool) -> None:
        if not condition:
            failures.append(what)

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        pages = site.build(index(), out)
        check("one page per entry, plus the store and the publish page", pages == 5)
        for path in (
            "index.html",
            "publish/index.html",
            "plugins/todo-fox/index.html",
            "skills/brief/index.html",
            "agents/release-captain/index.html",
            "assets/store.css",
            "assets/store.js",
            "assets/publish.js",
            "sitemap.xml",
        ):
            check(f"{path} is written", (out / path).is_file())

        home = (out / "index.html").read_text(encoding="utf-8")
        plugin = (out / "plugins/todo-fox/index.html").read_text(encoding="utf-8")
        skill = (out / "skills/brief/index.html").read_text(encoding="utf-8")
        agent = (out / "agents/release-captain/index.html").read_text(encoding="utf-8")
        publish = (out / "publish/index.html").read_text(encoding="utf-8")

        # --- nothing from the feed is markup -------------------------------
        for name, page in (("home", home), ("plugin", plugin), ("skill", skill)):
            check(f"{name}: no script tag from the feed", "<script>alert" not in page)
            check(f"{name}: no injected image tag", "<img src=x" not in page)
        check("a hostile title is shown as text", "&lt;img src=x onerror=alert(1)&gt;" in plugin)
        check("a file's text is escaped", "&lt;b&gt;" in plugin)
        check("fenced code is escaped", "&lt;script&gt;alert(3)&lt;/script&gt;" in skill)
        check("inline code is escaped", "<code>code &lt;b&gt;</code>" in skill)

        # --- links ------------------------------------------------------------
        check("an https link becomes a link", '<a href="https://example.com/guide"' in skill)
        check("a javascript link keeps its words only", "javascript:alert(1)" not in skill)
        check("a trap link still shows its label", "a trap" in skill)
        check("a non-https source address is not linked", "javascript:alert(4)" not in skill)

        # --- publishing ------------------------------------------------------
        check("the store has a Publish button", '<a class="button" href="publish/">Publish</a>' in home)
        check("every page has Publish in its nav", 'nav__publish" href="../../publish/">Publish</a>' in skill)
        check("the publish page is a form", "data-publish-form" in publish)
        check("the form has a Publish button", 'type="submit">Publish</button>' in publish)
        check("the form knows where the registry is", 'data-registry="https://github.com/' in publish)
        check("the publish page loads its script", 'src="../assets/publish.js"' in publish)
        check("only the publish page loads it", "publish.js" not in home and "publish.js" not in skill)
        check("without JavaScript it says how to publish by hand", "<noscript>" in publish)

        # --- what a page carries --------------------------------------------
        check("the store links every entry", 'href="skills/brief/"' in home)
        check("the install line is on the page", "dodaios marketplace install brief" in skill)
        check(
            "a portable skill also shows the open installer",
            "npx skills add Dodaios/marketplace --skill brief" in skill,
        )
        check(
            "a plugin does not offer the skills installer",
            "npx skills add" not in plugin,
        )
        check("a local plugin shows the command it runs", "npx -y fox@1.0.0" in plugin)
        check("the sign-in method is named", "Personal access token" in plugin)
        check("an agent says what it asks before", "Always asks before" in agent)
        check("the frontmatter is shown apart from the prose", 'class="frontmatter"' in skill)
        check("a file that is not text is listed, not dropped", "logo.png" in skill)
        check("every page warns that nobody reviewed it", "Nobody reviewed it by hand" in skill)
        check("no script rides in the markup", 'onerror="' not in plugin)
        check("a brand glyph has its letters ready", "data-mark-glyph" in plugin)

    # --- Markdown ------------------------------------------------------------
    rendered = site.render_markdown(SKILL_MD.split("---\n", 2)[2])
    check("a heading moves one level down", "<h2>Brief</h2>" in rendered)
    check("bold renders", "<strong>three</strong>" in rendered)
    check("an ordered list renders", "<ol><li>First" in rendered)
    check("a nested list stays inside its item", "<ul><li>nested point</li></ul></li>" in rendered)
    check("a table renders", "<th>Field</th>" in rendered and "<td>a</td>" in rendered)
    plain = site.render_markdown("<b>x</b>")
    check("unknown markup stays text", plain == "<p>&lt;b&gt;x&lt;/b&gt;</p>")
    check("an unclosed fence does not swallow the build", "<pre>" in site.render_markdown("```\nx"))

    # --- marks ---------------------------------------------------------------
    check("two letters from a title", site.monogram("Three Bullet Brief") == "TB")
    check("one word, one letter", site.monogram("humanizer") == "H")
    check("the hue is a multiple of 30", site.name_hue("humanizer") % 30 == 0)
    check("the same name, the same hue", site.name_hue("brief") == site.name_hue("brief"))

    # --- the listing text ---------------------------------------------------
    describe = build_index.frontmatter_description
    check(
        "a folded block scalar is read",
        describe(SKILL_MD) == "Turns a topic into three bullets. Use it for a short version.",
    )
    check(
        "a literal block scalar is read",
        describe("---\ndescription: |\n  Line one.\n  Line two.\nlicense: MIT\n---\n")
        == "Line one. Line two.",
    )
    check("a plain value is read", describe("---\ndescription: Plain text\n---\n") == "Plain text")
    quoted = describe('---\ndescription: "Quoted"\n---\n')
    check("a quoted value loses its quotes", quoted == "Quoted")
    check("no frontmatter, no description", describe("# Title") == "")
    listed = build_index.listed_description
    check(
        "a leaked block marker falls back to the file",
        listed({"description": "|"}, SKILL_MD).startswith("Turns a topic"),
    )
    check(
        "a real listing text wins",
        listed({"description": "Mine."}, SKILL_MD) == "Mine.",
    )
    long_md = "---\ndescription: >-\n  " + "A sentence that goes on. " * 40 + "\n---\n"
    check("a long fallback is cut", len(listed({"description": ""}, long_md)) <= 280)

    if failures:
        print("FAIL:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("storefront: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
