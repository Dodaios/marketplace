#!/usr/bin/env python3
"""Render the storefront: the pages a person reads at dodaios.ai/marketplace.

``build_index.py`` compiles the feed and then calls :func:`build` here with
that same feed, so the storefront can never show an index other than the one
the app downloads. Every publish rebuilds both.

Output, inside the site folder:

- ``index.html``            the store: search, kind switch, every entry as a row
- ``<kind>s/<name>/``       one page per entry: what it reaches, how to install
                            it, and every published file, readable in place
- ``publish/``              how to publish
- ``assets/``               the stylesheet and the script (copied from site/)
- ``sitemap.xml``

Everything an entry carries is text somebody else wrote, and it is merged with
no human review. So nothing from the feed reaches a page unescaped: this file
builds HTML only from escaped strings, the Markdown renderer escapes before it
formats, and a link is only ever made from an ``http(s)`` address.

Stdlib only, like every script in this repository.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "site"

SITE_URL = "https://dodaios.ai"
STORE_URL = f"{SITE_URL}/marketplace/"
REGISTRY_URL = "https://github.com/Dodaios/marketplace"
APP_REPO_URL = "https://github.com/Dodaios/Dodaios"
PRODUCT = "Dodaios"

#: The manifest block the app reads its own fields from, current name first.
EXTENSION_NAMESPACES = ("io.github.dodaios", "io.github.personaljarvis")

KINDS = ("plugin", "skill", "agent", "look")
KIND_LABEL = {"plugin": "Plugin", "skill": "Skill", "agent": "Agent", "look": "Look"}
KIND_PLURAL = {"plugin": "Plugins", "skill": "Skills", "agent": "Agents", "look": "Looks"}
KIND_HINT = {
    "plugin": "Connect an outside service and its tools",
    "skill": "Instructions that teach a new way of working",
    "agent": "Ready-made teammates with a job and their own instructions",
    "look": "Visuals that take the voice orb's place",
}
#: The only address a look's preview picture is shown from: this registry's
#: own Pages site, where the publish build put the re-encoded PNG.
LOOK_PREVIEW_RE = re.compile(
    r"https://dodaios\.github\.io/marketplace/looks/[a-z0-9-]+/preview-[0-9a-f]{12}\.png"
)
AUTH_LABEL = {
    "pat_paste": "Personal access token",
    "oauth_device_flow": "Device sign-in",
    "hosted_mcp_oauth_dcr": "OAuth sign-in",
    "oauth_pkce_loopback": "OAuth sign-in",
    "hosted_mcp_allowlist": "Account allowlist",
}

SVG_SEARCH = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/>'
    '<path d="m20 20-3.5-3.5"/></svg>'
)
SVG_CHEVRON = (
    '<svg class="row__chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="m9 6 6 6-6 6"/></svg>'
)
SVG_SHIELD = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M12 3 5 6v5c0 4.5 3 8 7 10 4-2 7-5.5 7-10V6z"/>'
    '<path d="M12 8v5"/><path d="M12 16.5v.01"/></svg>'
)
# The GitHub mark, the same path the website draws.
_GITHUB_PATH = (
    "M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577"
    " 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3."
    "633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 "
    "2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-"
    "1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.2"
    "67 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1."
    "653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36"
    ".81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24"
    " 17.592 24 12.297c0-6.627-5.373-12-12-12"
)
SVG_UPLOAD = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M12 16V4"/><path d="m7 9 5-5 5 5"/>'
    '<path d="M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/></svg>'
)
SVG_GITHUB = (
    '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
    f'<path d="{_GITHUB_PATH}"/></svg>'
)


def esc(value: object) -> str:
    """Text made safe for HTML content and for a double-quoted attribute."""
    return html.escape("" if value is None else str(value), quote=True)


# ----------------------------------------------------------------------------
# Markdown, a readable subset
# ----------------------------------------------------------------------------

_CODE_SPAN_RE = re.compile(r"(`+)(.+?)\1")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BARE_URL_RE = re.compile(r"(?<![\"'>=/\w])(https?://[^\s<]+[^\s<.,;:!?)\]])")
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])")
_ITEM_RE = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
_RULE_RE = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_TABLE_RULE_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_FRONTMATTER_RE = re.compile(r"\A﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


def _format(text: str) -> str:
    """Escape ``text``, then apply links, bold and italic to the escaped form."""
    out = esc(text)

    def link(match: re.Match[str]) -> str:
        label, target = match.group(1), html.unescape(match.group(2))
        if not re.match(r"https?://", target, re.IGNORECASE):
            return label  # a relative or scripted target: keep the words only
        return f'<a href="{esc(target)}" rel="noopener nofollow">{label}</a>'

    out = _LINK_RE.sub(link, out)
    out = _BARE_URL_RE.sub(
        lambda m: f'<a href="{m.group(1)}" rel="noopener nofollow">{m.group(1)}</a>', out
    )
    out = _BOLD_RE.sub(lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", out)
    return _ITALIC_RE.sub(r"<em>\1</em>", out)


def _inline(text: str) -> str:
    """Inline Markdown. Code spans are taken out first: nothing formats in them."""
    parts: list[str] = []
    position = 0
    for match in _CODE_SPAN_RE.finditer(text):
        parts.append(_format(text[position : match.start()]))
        parts.append(f"<code>{esc(match.group(2).strip())}</code>")
        position = match.end()
    parts.append(_format(text[position:]))
    return "".join(parts)


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _list(lines: list[str], start: int) -> tuple[str, int]:
    """One list starting at ``lines[start]``; returns its HTML and the next line."""
    first = _ITEM_RE.match(lines[start])
    assert first is not None
    base = len(first.group(1).expandtabs(4))
    ordered = first.group(2)[0].isdigit()
    items: list[tuple[str, list[str]]] = []
    index = start
    while index < len(lines):
        line = lines[index]
        match = _ITEM_RE.match(line)
        indent = len(line) - len(line.lstrip(" \t"))
        if match and len(match.group(1).expandtabs(4)) <= base + 1:
            if match.group(2)[0].isdigit() != ordered:
                break  # a different kind of list starts here
            items.append((match.group(3), []))
        elif not line.strip():
            following = lines[index + 1] if index + 1 < len(lines) else ""
            follows_item = _ITEM_RE.match(following)
            deeper = following.strip() and len(following) - len(following.lstrip(" \t")) > base
            if not (follows_item or deeper):
                break
            if items and items[-1][1]:
                items[-1][1].append("")
        elif indent > base and items:
            items[-1][1].append(line[min(indent, base + 2) :])
        elif items and not (_HEADING_RE.match(line.strip()) or _FENCE_RE.match(line)):
            # A lazy continuation: the same sentence, wrapped without an indent.
            text, sub = items[-1]
            items[-1] = (f"{text} {line.strip()}", sub)
        else:
            break
        index += 1
    tag = "ol" if ordered else "ul"
    body = "".join(
        f"<li>{_inline(text)}{render_markdown(chr(10).join(sub)) if sub else ''}</li>"
        for text, sub in items
    )
    return f"<{tag}>{body}</{tag}>", index


def render_markdown(text: str) -> str:
    """Markdown to HTML for the subset a SKILL.md or a README uses.

    Headings, paragraphs, lists (nested), fenced code, quotes, rules, tables,
    and inline code, bold, italic and links. A page already owns the ``<h1>``,
    so headings are moved one level down. Anything this does not know stays
    visible as the text it is, escaped.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            out.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        fence = _FENCE_RE.match(line)
        if fence:
            flush()
            marker = fence.group(1)[:3]
            index += 1
            block: list[str] = []
            while index < len(lines) and not lines[index].strip().startswith(marker):
                block.append(lines[index])
                index += 1
            index += 1  # the closing fence, or the end of the file
            out.append(f"<pre><code>{esc(chr(10).join(block))}</code></pre>")
            continue

        if not stripped:
            flush()
            index += 1
            continue

        heading = _HEADING_RE.match(stripped)
        if heading:
            flush()
            level = min(len(heading.group(1)) + 1, 6)
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            index += 1
            continue

        if _RULE_RE.match(stripped):
            flush()
            out.append("<hr>")
            index += 1
            continue

        if stripped.startswith(">"):
            flush()
            quote: list[str] = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[index]))
                index += 1
            out.append(f"<blockquote>{render_markdown(chr(10).join(quote))}</blockquote>")
            continue

        if "|" in line and index + 1 < len(lines) and _TABLE_RULE_RE.match(lines[index + 1]):
            flush()
            head = _cells(line)
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(_cells(lines[index]))
                index += 1
            thead = "".join(f"<th>{_inline(cell)}</th>" for cell in head)
            tbody = "".join(
                "<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in row) + "</tr>"
                for row in rows
            )
            out.append(f"<table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table>")
            continue

        if _ITEM_RE.match(line):
            flush()
            rendered, index = _list(lines, index)
            out.append(rendered)
            continue

        paragraph.append(stripped)
        index += 1

    flush()
    return "\n".join(out)


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """A document's leading YAML block, and the prose after it."""
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return None, text
    return match.group(1), text[match.end() :]


# ----------------------------------------------------------------------------
# Entries
# ----------------------------------------------------------------------------


def _readable(text: object) -> str:
    """``text`` when it says something, else an empty string."""
    value = str(text or "").strip()
    return value if re.search(r"\w", value) else ""


def _extension(plugin_json: dict) -> dict:
    extensions = plugin_json.get("extensions")
    if isinstance(extensions, dict):
        for namespace in EXTENSION_NAMESPACES:
            block = extensions.get(namespace)
            if isinstance(block, dict):
                return block
    return {}


def _mcp_server(mcp_json: object) -> dict:
    servers = mcp_json.get("mcpServers") if isinstance(mcp_json, dict) else None
    if isinstance(servers, dict):
        for server in servers.values():
            if isinstance(server, dict):
                return server
    return {}


def entries_of(index: dict) -> list[dict]:
    """Every entry of the feed in the one flat shape the pages draw."""
    entries: list[dict] = []
    for plugin in index.get("plugins", []):
        manifest = plugin.get("plugin_json") if isinstance(plugin.get("plugin_json"), dict) else {}
        ext = _extension(manifest)
        server = _mcp_server(plugin.get("mcp_json"))
        auth = ext.get("auth") if isinstance(ext.get("auth"), dict) else {}
        category = _readable(ext.get("category"))
        entries.append(
            {
                "kind": "plugin",
                "name": plugin["name"],
                "title": _readable(ext.get("display_name")) or plugin["name"],
                "description": _readable(manifest.get("description")),
                "categories": [category] if category else [],
                "logo_slug": _readable(ext.get("logo_slug")),
                "logo_color": _readable(ext.get("logo_color")),
                "server": server,
                "auth_mode": _readable(auth.get("mode")),
                "source": plugin,
            }
        )
    for kind, key in (("skill", "skills"), ("agent", "agents"), ("look", "looks")):
        for item in index.get(key, []):
            categories = item.get("categories")
            entries.append(
                {
                    "kind": kind,
                    "name": item["name"],
                    "title": _readable(item.get("title")) or item["name"],
                    "description": _readable(item.get("description")),
                    "categories": [
                        c for c in (categories if isinstance(categories, list) else []) if c
                    ],
                    "source": item,
                }
            )
            if kind == "look":
                preview = str(item.get("preview_url") or "")
                entries[-1]["preview_url"] = preview if LOOK_PREVIEW_RE.fullmatch(preview) else ""
    for entry in entries:
        source = entry["source"]
        entry["publisher"] = _readable(source.get("publisher"))
        entry["publisher_id"] = source.get("publisher_id")
        entry["version"] = _readable(source.get("version"))
        entry["published_at"] = _readable(source.get("published_at"))
        entry["source_url"] = _readable(source.get("source_url"))
        entry["featured"] = bool(source.get("featured"))
        entry["href"] = f"{entry['kind']}s/{entry['name']}/"
    # The registry's picks first, then the newest publication, then by title.
    entries.sort(key=lambda e: e["title"].lower())
    entries.sort(key=lambda e: e["published_at"], reverse=True)
    entries.sort(key=lambda e: not e["featured"])
    return entries


def monogram(title: str) -> str:
    """Up to two letters that stand for a title: "Three Bullet Brief" is "TB"."""
    words = [word for word in re.split(r"[\s._-]+", title.strip()) if word]
    if not words:
        return "?"
    return (words[0][0] + (words[1][0] if len(words) > 1 else "")).upper()


def name_hue(name: str) -> int:
    """A hue that belongs to a name. The app computes the same number, so an
    entry is the same colour on the website and in the store."""
    value = 0
    for char in name:
        value = (value * 31 + ord(char)) % 360
    # JavaScript's Math.round, which rounds a half up (Python's rounds to even).
    return int(value / 30 + 0.5) * 30


def _ink_on(hex_colour: str) -> str:
    """Black or white, whichever reads on the given brand colour."""
    red, green, blue = (int(hex_colour[i : i + 2], 16) / 255 for i in (0, 2, 4))

    def linear(channel: float) -> float:
        return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4

    luminance = 0.2126 * linear(red) + 0.7152 * linear(green) + 0.0722 * linear(blue)
    return "111111" if luminance > 0.6 else "ffffff"


def mark(entry: dict, *, page: bool = False) -> str:
    """The face of an entry: a brand tile for a plugin, a look's own preview,
    a monogram otherwise."""
    size = " mark--page" if page else ""
    if entry["kind"] == "look" and entry.get("preview_url"):
        return (
            f'<span class="mark mark--look{size}" aria-hidden="true">'
            f'<img src="{esc(entry["preview_url"])}" alt="" loading="lazy"></span>'
        )
    letters = esc(monogram(entry["title"]))
    colour = entry.get("logo_color", "")
    slug = entry.get("logo_slug", "")
    if entry["kind"] == "plugin" and re.fullmatch(r"[0-9a-fA-F]{6}", colour):
        ink = _ink_on(colour)
        inner = letters
        if re.fullmatch(r"[a-z0-9]+", slug):
            # The glyph stands in for the letters. If it does not load, store.js
            # takes it out and shows them again; no script rides in the markup.
            inner = (
                f'<img src="https://cdn.simpleicons.org/{slug}/{ink}" alt="" data-mark-glyph>'
                f"<span hidden>{letters}</span>"
            )
        return (
            f'<span class="mark mark--brand{size}" style="--brand:#{colour};--brand-ink:#{ink}" '
            f'aria-hidden="true">{inner}</span>'
        )
    return (
        f'<span class="mark{size}" style="--h:{name_hue(entry["name"])}" '
        f'aria-hidden="true">{letters}</span>'
    )


def format_date(iso: str) -> str:
    try:
        parsed = datetime.strptime(iso[:10], "%Y-%m-%d")
    except ValueError:
        return iso
    return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}"


def format_size(size: int) -> str:
    return f"{size} B" if size < 1024 else f"{size / 1024:.1f} kB"


def byline(entry: dict, *, with_kind: bool) -> str:
    parts = [
        KIND_LABEL[entry["kind"]] if with_kind else "",
        f"@{entry['publisher']}" if entry["publisher"] else "",
        entry["categories"][0].replace("-", " ").replace("_", " ").capitalize()
        if entry["categories"]
        else "",
    ]
    return " · ".join(part for part in parts if part)


def install_commands(entry: dict) -> list[tuple[str, str, str]]:
    """``(heading, command, note)`` for every way this entry installs."""
    name = entry["name"]
    if entry["kind"] == "look":
        # A look installs from the app's Marketplace; the command line does
        # not list looks.
        return []
    commands = [
        (
            f"Install in {PRODUCT}",
            f"dodaios marketplace install {name}",
            f"Or open Marketplace in the {PRODUCT} app and search for “{entry['title']}”.",
        )
    ]
    if entry["kind"] == "skill" and entry["source"].get("flavor") == "portable":
        agents = entry["source"].get("compatible_agents") or []
        where = (
            ", ".join(str(agent) for agent in agents)
            if agents
            else "any agent that reads SKILL.md"
        )
        commands.append(
            (
                "Install in another agent",
                f"npx skills add Dodaios/marketplace --skill {name}",
                f"A portable skill. Runs in {where}.",
            )
        )
    return commands


# ----------------------------------------------------------------------------
# Pages
# ----------------------------------------------------------------------------


def asset(name: str) -> str:
    """An asset's file name with a fingerprint of its content.

    The host lets browsers keep a stylesheet for hours. Without the
    fingerprint a visitor gets a new page drawn with the old styles.
    """
    digest = hashlib.sha256((ASSETS / name).read_bytes()).hexdigest()[:10]
    return f"{name}?v={digest}"


def layout(
    *, title: str, description: str, path: str, depth: int, body: str, scripts: tuple[str, ...] = ()
) -> str:
    """One whole document. ``depth`` is how many folders ``path`` sits below
    the store's root, so links stay relative and the site works at any address."""
    up = "../" * depth
    extra = "".join(
        f'<script src="{up}assets/{asset(name)}" defer></script>\n' for name in scripts
    )
    canonical = STORE_URL + path
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
<link rel="canonical" href="{esc(canonical)}">
<link rel="icon" type="image/svg+xml" href="{SITE_URL}/favicon.svg">
<meta property="og:site_name" content="{PRODUCT}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:type" content="website">
<meta property="og:url" content="{esc(canonical)}">
<link rel="stylesheet" href="{up}assets/{asset("store.css")}">
</head>
<body>
<header class="nav">
<div class="nav__row">
<a class="nav__mark" href="{SITE_URL}/">{PRODUCT}</a>
<nav class="nav__links" aria-label="Sections">
<a class="nav__link" href="{SITE_URL}/#features">Plugins</a>
<a class="nav__link" href="{SITE_URL}/#skills">Skills</a>
<a class="nav__link" href="{SITE_URL}/#clis">CLIs</a>
<a class="nav__link" href="{SITE_URL}/#agents">Agents</a>
<a class="nav__link" href="{up or './'}" aria-current="true">Marketplace</a>
<a class="nav__link" href="{SITE_URL}/blog/">Blog</a>
</nav>
<div class="nav__actions">
<a class="nav__github" href="{APP_REPO_URL}" aria-label="{PRODUCT} on GitHub">{SVG_GITHUB}</a>
<a class="button button--quiet nav__publish" href="{up}publish/">Publish</a>
<a class="button" href="{SITE_URL}/#install">Get {PRODUCT}</a>
</div>
</div>
</header>
<main class="column">
{body}
</main>
<footer class="footer">
<div class="column footer__row">
<p>Community content. Checked automatically, not reviewed by a person.</p>
<nav class="footer__links" aria-label="Marketplace">
<a href="{up}publish/">Publish yours</a>
<a href="{REGISTRY_URL}">Registry on GitHub</a>
<a href="{REGISTRY_URL}/issues/new">Report an entry</a>
<a href="{up}index.json">Feed</a>
</nav>
</div>
</footer>
<script src="{up}assets/{asset("store.js")}" defer></script>
{extra}</body>
</html>
"""


def row(entry: dict, *, with_kind: bool = False) -> str:
    haystack = " ".join(
        [entry["title"], entry["name"], entry["description"], entry["publisher"]]
        + entry["categories"]
    ).lower()
    description = entry["description"] or "No description yet."
    return f"""<li data-search="{esc(haystack)}">
<a class="row" href="{esc(entry["href"])}">
{mark(entry)}
<span class="row__text">
<span class="row__title">{esc(entry["title"])}</span>
<span class="row__description">{esc(description)}</span>
<span class="row__byline">{esc(byline(entry, with_kind=with_kind))}</span>
</span>
{SVG_CHEVRON}
</a>
</li>"""


def home_page(entries: list[dict]) -> str:
    counts = {kind: sum(1 for e in entries if e["kind"] == kind) for kind in KINDS}
    tabs = [
        f'<a class="tab" href="#top" role="tab" aria-selected="true" data-kind-tab="all">All '
        f'<span class="tab__count" data-count>{len(entries)}</span></a>'
    ]
    groups = []
    for kind in KINDS:
        tabs.append(
            f'<a class="tab" href="#{kind}s" role="tab" aria-selected="false" '
            f'data-kind-tab="{kind}">{KIND_PLURAL[kind]} '
            f'<span class="tab__count" data-count>{counts[kind]}</span></a>'
        )
        rows = [row(entry) for entry in entries if entry["kind"] == kind]
        if not rows:
            continue
        groups.append(
            f"""<section class="group" id="{kind}s" data-kind-group="{kind}">
<div class="group__head">
<h2>{KIND_PLURAL[kind]}</h2>
<p class="group__hint">{KIND_HINT[kind]}</p>
</div>
<ul class="rows">
{chr(10).join(rows)}
</ul>
</section>"""
        )
    nothing = (
        ""
        if entries
        else '<div class="empty"><strong>Nothing is published yet.</strong>'
        "<p>The first entry could be yours.</p></div>"
    )
    body = f"""<section class="hero" id="top">
<p class="eyebrow">Community</p>
<h1>Marketplace</h1>
<p class="standfirst">Plugins, skills, agents and looks for {PRODUCT}, made by the people
who use it.
Open one, read every file, then install it with one line.</p>
<div class="search" role="search">
{SVG_SEARCH}
<input type="search" placeholder="Search plugins, skills, agents and looks" autocomplete="off"
 aria-label="Search plugins, skills, agents and looks" data-store-search>
<kbd aria-hidden="true">/</kbd>
</div>
<div class="tabs" role="tablist" aria-label="Kind">
{chr(10).join(tabs)}
</div>
</section>
{chr(10).join(groups)}
{nothing}
<div class="empty" data-store-empty hidden>
<strong>Nothing matches “<span data-store-empty-query></span>”.</strong>
<p>The community catalogue is still small. {PRODUCT} itself ships with many more plugins
ready to connect.</p>
</div>
<section class="invite">
<div class="invite__text">
<h2>Publish yours</h2>
<p>Drop your file, press Publish, confirm on GitHub. It is live a minute or two later, under
your GitHub name.</p>
</div>
<a class="button" href="publish/">Publish</a>
</section>"""
    return layout(
        title=f"Marketplace — {PRODUCT}",
        description=(
            f"Plugins, skills, agents and looks for {PRODUCT}, published by the community. "
            "Read every file before you install."
        ),
        path="",
        depth=0,
        body=body,
    )


def _reach(entry: dict) -> str:
    """What installing would reach: an address, a command, or instructions."""
    blocks: list[str] = []
    if entry["kind"] == "plugin":
        server = entry["server"]
        command = server.get("command")
        if command:
            arguments = server.get("args") if isinstance(server.get("args"), list) else []
            line = " ".join([str(command), *[str(a) for a in arguments]])
            blocks.append(
                '<p class="label">Once connected, this command runs on your computer:</p>'
                f'<div class="terminal"><code>{esc(line)}</code></div>'
            )
        elif server.get("url"):
            blocks.append(
                '<p class="label">Once connected, requests and your access token go to:</p>'
                f'<div class="terminal terminal--plain"><code>{esc(server["url"])}</code></div>'
            )
        else:
            blocks.append(
                '<p class="label">Metadata only. This adds no server and runs no command.</p>'
            )
        auth = AUTH_LABEL.get(entry["auth_mode"])
        if auth:
            blocks.append(f'<p class="label">Sign-in method</p><p class="value">{esc(auth)}</p>')
        heading = "Where your data goes"
    elif entry["kind"] == "look":
        look = entry["source"]
        blocks.append(
            '<p class="label">A look can only draw. It runs in a sandboxed frame with no '
            "network, no access to the app or your data, no cookies and no storage. It gets "
            "nothing but the voice state and the volume.</p>"
        )
        for label, key in (("License", "license"), ("Credits", "attribution")):
            value = _readable(look.get(key))
            if value:
                blocks.append(f'<p class="label">{label}</p><p class="value">{esc(value)}</p>')
        digest = str(look.get("sha256") or "")
        if re.fullmatch(r"[0-9a-f]{64}", digest):
            blocks.append(
                '<p class="label">Archive SHA-256, checked again by the app before it '
                f'unpacks anything</p><div class="terminal terminal--plain"><code>{esc(digest)}'
                "</code></div>"
            )
        heading = "What it can do"
    elif entry["kind"] == "agent":
        template = entry["source"].get("agent")
        template = template if isinstance(template, dict) else {}
        blocks.append(
            '<p class="label">Runs on your own model. Nothing of the publisher’s accounts or '
            "settings comes with it.</p>"
        )
        focus = [str(item) for item in template.get("focus") or []]
        if focus:
            blocks.append(
                '<p class="label">Reaches first for</p>'
                f'<p class="value">{esc(", ".join(focus))}</p>'
            )
        asks = [str(item) for item in template.get("require_approval") or []]
        blocks.append(
            '<p class="label">Always asks before</p><p class="value">'
            + esc(", ".join(asks) if asks else "It asks you before it acts until you change that.")
            + "</p>"
        )
        heading = "Where it runs"
    else:
        return ""
    return f'<section class="section"><h2>{heading}</h2>{"".join(blocks)}</section>'


def _file(item: dict, *, opened: bool, archived: bool = False) -> str:
    path = str(item.get("path") or "")
    text = item.get("text")
    size = item.get("size") if isinstance(item.get("size"), int) else 0
    if not isinstance(text, str) and archived:
        body = "<pre>Inside the archive. Download it to read this file.</pre>"
    elif not isinstance(text, str):
        body = "<pre>Not shown: this file is not text, or is too large to embed.</pre>"
    elif path.lower().endswith((".md", ".markdown")):
        frontmatter, prose = split_frontmatter(text)
        head = f'<pre class="frontmatter">{esc(frontmatter)}</pre>' if frontmatter else ""
        body = f'{head}<div class="prose">{render_markdown(prose)}</div>'
    else:
        body = f"<pre>{esc(text)}</pre>"
    return (
        f'<details class="file"{" open" if opened else ""}><summary>{esc(path)}'
        f'<span class="file__size">{esc(format_size(size))}</span></summary>'
        f'<div class="file__body">{body}</div></details>'
    )


def entry_page(entry: dict) -> str:
    source = entry["source"]
    kind = entry["kind"]
    publisher = entry["publisher"]
    avatar = ""
    if isinstance(entry["publisher_id"], int) and entry["publisher_id"] > 0:
        avatar = (
            f'<img src="https://avatars.githubusercontent.com/u/{entry["publisher_id"]}?s=64" '
            'alt="" loading="lazy">'
        )
    who = (
        f'<span aria-hidden="true">·</span>{avatar}'
        f'<a href="https://github.com/{esc(publisher)}" rel="noopener">@{esc(publisher)}</a>'
        if publisher
        else ""
    )

    installs = (
        f'<p class="note">Open Marketplace in the {PRODUCT} app, find “{esc(entry["title"])}” '
        "under Looks and press Install. Then pick it as your look in Settings.</p>"
        if kind == "look"
        else ""
    ) + "".join(
        f'<p class="label">{esc(heading)}</p>'
        f'<div class="terminal"><code>{esc(command)}</code>'
        f'<button class="copy" type="button" data-copy="{esc(command)}">Copy</button></div>'
        f'<p class="note">{esc(note)}</p>'
        for heading, command, note in install_commands(entry)
    )

    facts = []
    if entry["version"]:
        facts.append(("Version", entry["version"]))
    if entry["published_at"]:
        facts.append(("Published", format_date(entry["published_at"])))
    if kind == "look" and isinstance(source.get("size_bytes"), int):
        facts.append(("Download", f"{source['size_bytes'] / (1024 * 1024):.1f} MB"))
    if entry["categories"]:
        labels = [c.replace("-", " ").replace("_", " ").capitalize() for c in entry["categories"]]
        facts.append(("Category", ", ".join(labels)))
    facts_html = (
        '<dl class="facts">'
        + "".join(f"<div><dt>{esc(k)}</dt><dd>{esc(v)}</dd></div>" for k, v in facts)
        + "</dl>"
        if facts
        else ""
    )

    files = [item for item in source.get("files") or [] if isinstance(item, dict)]
    files_html = (
        "".join(
            _file(
                item,
                opened=position == 0 and isinstance(item.get("text"), str),
                archived=kind == "look",
            )
            for position, item in enumerate(files)
        )
        or '<p class="note">This entry publishes no readable files.</p>'
    )
    archive = str(source.get("archive_url") or "") if kind == "look" else ""
    if archive.startswith("https://github.com/"):
        files_html = (
            f'<p class="note"><a class="link" href="{esc(archive)}" rel="noopener">'
            "Download the archive</a> to read every file before you install it.</p>"
        ) + files_html
    source_link = (
        f'<p class="note"><a class="link" href="{esc(entry["source_url"])}" rel="noopener">'
        f"{'Open the source' if kind == 'look' else 'Open the source on GitHub'}</a></p>"
        if entry["source_url"].startswith("https://")
        else ""
    )
    lead = (
        f'<p class="lead">{esc(entry["description"])}</p>'
        if entry["description"]
        else '<p class="lead lead--missing">No description yet.</p>'
    )

    preview_html = (
        f'<figure class="look-preview"><img src="{esc(entry["preview_url"])}" '
        f'alt="Preview of {esc(entry["title"])}" loading="lazy"></figure>'
        if kind == "look" and entry.get("preview_url")
        else ""
    )

    body = f"""<nav class="crumbs" aria-label="Breadcrumb">
<a href="../../">Marketplace</a><span aria-hidden="true">/</span>
<a href="../../#{kind}s">{KIND_PLURAL[kind]}</a><span aria-hidden="true">/</span>
<span aria-current="page">{esc(entry["title"])}</span>
</nav>
<header class="entry__head">
{mark(entry, page=True)}
<div>
<h1>{esc(entry["title"])}</h1>
<p class="entry__byline"><span>{KIND_LABEL[kind]}</span>{who}</p>
</div>
</header>
{lead}
{preview_html}
{facts_html}
<section class="section">
<h2>Install</h2>
{installs}
</section>
{_reach(entry)}
<section class="section">
<h2>Before you install</h2>
<p class="trust">{SVG_SHIELD}<span>Published by a community member and merged by automated
checks. Nobody reviewed it by hand. The files below are exactly what gets installed.</span></p>
{source_link}
</section>
<section class="section">
<h2>What is inside</h2>
{files_html}
</section>"""
    description = entry["description"] or f"A community {KIND_LABEL[kind].lower()} for {PRODUCT}."
    return layout(
        title=f"{entry['title']} — {PRODUCT} Marketplace",
        description=description[:300],
        path=entry["href"],
        depth=2,
        body=body,
    )


def publish_page() -> str:
    """The page that publishes: one small window.

    Drop a file or a folder, give it a name and a description, press Publish.
    The page works out what was dropped and packs the submission in the
    browser (site/publish.js), then hands it to GitHub as a filled-in issue,
    where the visitor signs in and confirms. Without JavaScript the window
    cannot pack anything, so the page then says how to publish by hand.
    """
    issue_form = f"{REGISTRY_URL}/issues/new?template=publish.yml"
    body = f"""<section class="pubwin">
<h1>Publish</h1>

<noscript>
<p class="notice">This needs JavaScript. Without it, open a
<a class="link" href="{issue_form}">publish issue</a> on GitHub and paste your submission
there.</p>
</noscript>

<form class="pubcard" data-publish-form novalidate
 data-registry="{REGISTRY_URL}" data-api="https://api.github.com/repos/Dodaios/marketplace">

<div class="drop" data-drop>
{SVG_UPLOAD}
<strong>Drop a file or folder</strong>
<span class="drop__hint">A skill, a plugin, an agent, or a look's zip</span>
<span class="drop__actions">
<button class="button button--quiet" type="button" data-open="file">Choose file</button>
<button class="button button--quiet" type="button" data-open="folder">Choose folder</button>
</span>
<input type="file" data-pick="file" multiple hidden
 accept=".md,.markdown,.txt,.json,.zip,text/markdown,application/json,application/zip">
<input type="file" data-pick="folder" multiple webkitdirectory hidden>
</div>

<div class="pack" data-loaded hidden>
<span class="mark" data-pack-mark aria-hidden="true"></span>
<span class="pack__text">
<strong data-pack-title></strong>
<span data-pack-meta></span>
</span>
<button class="pack__remove" type="button" data-clear aria-label="Remove">&times;</button>
</div>
<p class="problem" data-problem="file" role="alert" hidden></p>

<label class="field" data-row-title>
<span class="field__label">Name</span>
<input name="title" type="text" maxlength="80" autocomplete="off">
<span class="problem" data-problem="title" role="alert" hidden></span>
</label>

<label class="field">
<span class="field__label">Description</span>
<textarea name="description" rows="3" maxlength="500"></textarea>
<span class="problem" data-problem="description" role="alert" hidden></span>
</label>

<label class="field" data-row-archive hidden>
<span class="field__label">Release link</span>
<input name="archive_url" type="url" autocomplete="off" spellcheck="false"
 placeholder="https://github.com/you/repo/releases/download/v1.0.0/look.zip">
<span class="field__hint">Attach this same zip to a release on your GitHub repository, then copy
the file's link.</span>
<span class="problem" data-problem="archive" role="alert" hidden></span>
</label>

<details class="more" data-more>
<summary>More options</summary>
<div class="more__fields">
<label class="field">
<span class="field__label">ID</span>
<input name="name" type="text" maxlength="64" autocomplete="off" spellcheck="false">
<span class="problem" data-problem="name" role="alert" hidden></span>
</label>
<label class="field">
<span class="field__label">Version</span>
<input name="version" type="text" maxlength="20" autocomplete="off" spellcheck="false"
 value="1.0.0">
<span class="problem" data-problem="version" role="alert" hidden></span>
</label>
</div>
</details>

<button class="button button--large" type="submit">Publish</button>

<div class="status" data-publish-status hidden aria-live="polite">
<p class="status__text" data-status-text></p>
<p class="status__paste" data-paste-help hidden>Paste it into the Submission field.
<button class="copy" type="button" data-copy="" data-copied="Copied">Copy again</button></p>
<a class="link" data-status-link href="{REGISTRY_URL}" target="_blank" rel="noopener" hidden></a>
</div>
</form>
</section>"""
    return layout(
        title=f"Publish to the Marketplace — {PRODUCT}",
        description=(
            f"Publish a plugin, a skill, an agent or a look to the {PRODUCT} marketplace: drop "
            "your file, press Publish, confirm on GitHub."
        ),
        path="publish/",
        depth=1,
        body=body,
        scripts=("publish.js",),
    )


def build(index: dict, out: Path) -> int:
    """Write the whole storefront for ``index`` into ``out``. Returns the page count."""
    entries = entries_of(index)
    pages: dict[str, str] = {"": home_page(entries), "publish/": publish_page()}
    for entry in entries:
        pages[entry["href"]] = entry_page(entry)

    for path, document in pages.items():
        target = out / path / "index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(document, encoding="utf-8", newline="\n")

    assets = out / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    for name in ("store.css", "store.js", "publish.js"):
        shutil.copyfile(ASSETS / name, assets / name)

    urls = "".join(f"<url><loc>{esc(STORE_URL + path)}</loc></url>" for path in pages)
    (out / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>\n',
        encoding="utf-8",
        newline="\n",
    )
    return len(pages)


def main() -> int:
    """Rebuild the storefront from an index that is already on disk."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="_site")
    args = parser.parse_args()
    out = ROOT / args.out
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    print(f"storefront: {build(index, out)} page(s) -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
