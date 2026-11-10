#!/usr/bin/env python3
"""Validate marketplace submission files. Stdlib only — no dependencies.

This is the automated half of the registry's trust model: submissions that
pass merge WITHOUT human review, so every rule here is load-bearing. The
Personal Jarvis app re-enforces the same rules at install time
(jarvis/marketplace/agent_plugins_loader.py) — keep the two in sync.

Usage:
    python scripts/validate.py [FILE...]           # default: submissions/*.json
    python scripts/validate.py --base-ref origin/main FILE...

With --base-ref, a submission that already exists at that git ref must keep
its publisher and strictly increase its version (the anti-hijack rule).

A look (kind=look) is the one kind checked over the network: its archive is
downloaded from the GitHub release it names, hashed, and read in memory as a
zip. That happens only for a look this change adds or edits (an unchanged
file at --base-ref is not fetched again), and ``--no-fetch`` turns it off.

Ownership is keyed on ``publisher_id`` — the numeric GitHub account id —
whenever the existing entry carries one. GitHub logins can be renamed, and
the freed name can then be claimed by anyone, who would inherit every entry
published under it; the numeric id never changes. Entries predating the
field fall back to the login comparison until their next update.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import io
import json
import re
import stat
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
EXTENSION_NAMESPACE = "io.github.personaljarvis"
PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"

KINDS = ("plugin", "skill", "agent", "look")

MAX_FILE_BYTES = 128 * 1024
MAX_SKILL_BYTES = 64 * 1024
MAX_BUNDLED_SKILLS = 10
MAX_DESCRIPTION_CHARS = 500

# Agent templates (kind=agent). An agent's standing instructions are its own
# AGENTS.md; the app caps them at 20 000 characters, and the cap here is the
# same number in bytes so a template the app exported always fits.
MAX_AGENT_INSTRUCTIONS_BYTES = 20_000
MAX_AGENT_NAME_CHARS = 40
MAX_AGENT_TITLE_CHARS = 120
MAX_AGENT_CAPABILITIES = 60

# Agent Plugins v1.0.0 name rules.
NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?$")
GITHUB_LOGIN_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
ENV_PLACEHOLDER_RE = re.compile(r"^\$(?:plugin_[a-z0-9_.-]+|\{plugin_[a-z0-9_.-]+\})$")
HEADER_PLACEHOLDER_RE = re.compile(r"\$(?:plugin_[a-z0-9_.-]+|\{plugin_[a-z0-9_.-]+\})")
TOKEN_LITERAL_RE = re.compile(r"[A-Za-z0-9_\-]{20,}")

STDIO_LAUNCHERS = ("npx", "uvx", "docker")

# The launcher name alone decides nothing: every one of the three allowed
# launchers has options that turn it back into "run whatever I say".
# `npx -p x@1.0.0 -c "curl … | sh"` runs a shell one-liner, `uvx --with evil`
# installs a second package nobody reviewed, and `docker run -v /:/host`
# hands the container the user's whole disk. So the ARGUMENTS are an
# allowlist too, per launcher, and the package a launcher fetches must be a
# pinned name from its own registry — never a git ref, a URL, or a path.
#
# The rule is positional: everything up to and including the package
# specification belongs to the LAUNCHER and is checked; everything after it
# is passed to the server the publisher wrote anyway, so checking it would
# buy nothing.
NPX_FLAGS = frozenset({"-y", "--yes"})
UVX_FLAGS = frozenset({"--from"})  # takes a value, itself pinned
DOCKER_FLAGS = frozenset({"-i", "--interactive", "--rm"})
DOCKER_VALUE_FLAGS = frozenset({"-e", "--env"})  # NAME only — see below

# npm: `pkg@1.2.3` or `@scope/pkg@1.2.3`. No `github:`, no `git+https://`,
# no `file:`, no tarball URL — those are not registry packages and carry no
# version anyone can pin.
NPM_PIN_RE = re.compile(
    r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*@\d+\.\d+\.\d+[0-9A-Za-z.+-]*$"
)
# PyPI: `pkg==1.2.3` (uv's own spelling) or `pkg@1.2.3`.
PYPI_PIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:==|@)\d+\.\d+\.\d+[0-9A-Za-z.+-]*$")
# A container image with an explicit version tag or a digest. `:latest` and a
# bare name are both "whatever the publisher pushes next".
IMAGE_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*:[0-9][0-9A-Za-z._-]*$")
IMAGE_DIGEST_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$")
# Only a bare variable NAME may be handed to `docker -e`. `-e PATH=/evil`
# would rewrite the container's environment from the manifest; the values a
# plugin legitimately needs travel through the env block, which is checked.
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Variables that decide what runs, before the plugin's own first line does.
# A manifest configures its own server; it does not get to re-point the
# dynamic linker, the interpreter, or the PATH the launcher is resolved on.
RESERVED_ENV_NAMES = frozenset(
    {
        "PATH",
        "PATHEXT",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONHOME",
        "NODE_OPTIONS",
        "NODE_PATH",
        "NPM_CONFIG_REGISTRY",
        "UV_INDEX_URL",
        "UV_EXTRA_INDEX_URL",
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
        "DOCKER_HOST",
        "COMSPEC",
        "SYSTEMROOT",
        "WINDIR",
    }
)

AUTH_MODES = (
    "pat_paste",
    "oauth_device_flow",
    "hosted_mcp_oauth_dcr",
    "oauth_pkce_loopback",
    "hosted_mcp_allowlist",
)

# Names the shipped app already uses. A community submission must never
# claim one of these: the app refuses the install, so the listing would only
# mislead. Includes the spec-conformant renames.
RESERVED_PLUGIN_IDS = frozenset(
    {
        "github", "vercel", "supabase", "notion", "slack", "linear", "stripe",
        "cloudflare", "discord", "telegram", "asana", "gmail", "todoist",
        "clickup", "dropbox", "canva", "airtable",
        "google_drive", "google-drive", "google_calendar", "google-calendar",
        "cal_com", "cal-com", "home_assistant", "home-assistant",
    }
)
RESERVED_SKILL_NAMES = frozenset(
    {
        "cli-gcloud", "control-api", "deep-work-mode", "jarvis-doc-author",
        "memory-save", "morning-routine", "skill-creator",
    }
    | {f"plugin-{p}" for p in RESERVED_PLUGIN_IDS}
)

# Obvious credential shapes. Matched against the RAW submission text so a
# token can not hide anywhere (headers, markdown, nested manifest fields).
SECRET_PATTERNS = (
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gho_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9\-_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z\-_]{30,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{20,}\.eyJ[A-Za-z0-9_\-]{20,}"),  # JWT
)


class Errors:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, path: Path, message: str) -> None:
        self.items.append(f"{path.as_posix()}: {message}")


def reject_http_urls(value, where: str, errors: Errors, path: Path) -> None:
    if isinstance(value, str):
        if value.lstrip().lower().startswith("http://"):
            errors.add(path, f"{where}: non-https URL {value!r}")
    elif isinstance(value, dict):
        for key, sub in value.items():
            reject_http_urls(sub, f"{where}.{key}", errors, path)
    elif isinstance(value, list):
        for index, sub in enumerate(value):
            reject_http_urls(sub, f"{where}[{index}]", errors, path)


def validate_stdio_args(launcher: str, args: list[str]) -> str | None:
    """Check the launcher half of an stdio argv against its own allowlist.

    Returns the first problem as a sentence naming the offending argument, or
    ``None`` when the argv is acceptable. The message ends up in the pull
    request check and in the app's install dialog, so it says what to change
    rather than that something was wrong.

    Mirrors ``_validate_stdio_args`` in the app's agent_plugins_loader.py and
    ``validateStdioArgs`` in the storefront's functions/_lib/validate.ts.
    Change one, change all three.
    """
    if launcher == "npx":
        index = 0
        while index < len(args) and args[index].startswith("-"):
            if args[index] not in NPX_FLAGS:
                return (
                    f"npx option {args[index]!r} is not allowed — a community "
                    "package may only be launched as `npx -y <name>@<version>` "
                    "(options like -p/-c can run arbitrary commands)"
                )
            index += 1
        if index >= len(args):
            return "npx needs a package to run, e.g. `npx -y my-mcp@1.2.0`"
        if not NPM_PIN_RE.fullmatch(args[index]):
            return (
                f"{args[index]!r} is not a pinned npm package — community stdio "
                "packages must name a registry package with an exact version "
                "(`my-mcp@1.2.0`), never a git ref, URL or path"
            )
        return None

    if launcher == "uvx":
        index = 0
        while index < len(args) and args[index].startswith("-"):
            flag, sep, inline = args[index].partition("=")
            if flag not in UVX_FLAGS:
                return (
                    f"uvx option {flag!r} is not allowed — a community package "
                    "may only be launched as `uvx <name>==<version>` "
                    "(options like --with install code nobody reviewed)"
                )
            if sep:
                value, index = inline, index + 1
            else:
                if index + 1 >= len(args):
                    return f"uvx option {flag!r} is missing its value"
                value, index = args[index + 1], index + 2
            if not PYPI_PIN_RE.fullmatch(value):
                return (
                    f"{value!r} is not a pinned PyPI package — use "
                    "`--from my-mcp==1.2.0`, never a git ref, URL or path"
                )
            # `--from` names the package; what follows is the entry point.
            return None
        if index >= len(args):
            return "uvx needs a package to run, e.g. `uvx my-mcp==1.2.0`"
        if not PYPI_PIN_RE.fullmatch(args[index]):
            return (
                f"{args[index]!r} is not a pinned PyPI package — community stdio "
                "packages must name a package with an exact version "
                "(`my-mcp==1.2.0`), never a git ref, URL or path"
            )
        return None

    # docker
    if not args or args[0] != "run":
        return "the only allowed docker subcommand is `run`"
    index = 1
    while index < len(args) and args[index].startswith("-"):
        flag, sep, inline = args[index].partition("=")
        if flag in DOCKER_VALUE_FLAGS:
            if sep:
                value, index = inline, index + 1
            else:
                if index + 1 >= len(args):
                    return f"docker option {flag!r} is missing its value"
                value, index = args[index + 1], index + 2
            if not ENV_NAME_RE.fullmatch(value):
                return (
                    f"docker {flag} {value!r} must name a variable and nothing "
                    "else — a plugin's own values belong in the env block, "
                    "which is checked for smuggled credentials"
                )
            continue
        if flag not in DOCKER_FLAGS:
            return (
                f"docker option {flag!r} is not allowed — a community container "
                "runs as `docker run -i --rm <image>:<version>`; options like "
                "-v/--mount/--privileged/--network would open the user's machine "
                "to it"
            )
        index += 1
    if index >= len(args):
        return "docker needs an image to run, e.g. `docker run -i --rm my/mcp:1.2`"
    image = args[index]
    if not (IMAGE_TAG_RE.fullmatch(image) or IMAGE_DIGEST_RE.fullmatch(image)):
        return (
            f"{image!r} is not a pinned image — name an explicit version tag "
            "(`my/mcp:1.2`) or a digest, never `latest` or a bare name"
        )
    return None


def validate_mcp_json(name: str, mcp: dict, errors: Errors, path: Path) -> None:
    servers = mcp.get("mcpServers")
    if not isinstance(servers, dict) or not servers:
        errors.add(path, "mcp_json.mcpServers must be a non-empty object")
        return
    if name in servers:
        server = servers[name]
    elif len(servers) == 1:
        server = next(iter(servers.values()))
    else:
        errors.add(path, "mcp_json: ship exactly one server (or name it after the plugin)")
        return
    if not isinstance(server, dict):
        errors.add(path, "mcp_json: server must be an object")
        return
    server_type = server.get("type")
    if server_type == "streamable-http":
        url = server.get("url")
        if not isinstance(url, str) or not url.lower().startswith("https://"):
            errors.add(path, f"mcp_json: url must be https:// (got {url!r})")
        if server.get("headers"):
            errors.add(
                path,
                "mcp_json: headers are not allowed (token injection is a client concern)",
            )
    elif server_type == "stdio":
        command = server.get("command", "")
        launcher = str(command).replace("\\", "/").rsplit("/", 1)[-1].lower()
        launcher = launcher.removesuffix(".exe").removesuffix(".cmd")
        if launcher not in STDIO_LAUNCHERS:
            errors.add(path, f"mcp_json: launcher {command!r} not in allowlist {STDIO_LAUNCHERS}")
            launcher = ""
        if server.get("url"):
            # One server entry, one transport. A stdio entry that also carries
            # a url reads as a hosted server to anything that checks the url
            # first and runs the command anyway — the two halves must never
            # disagree about what this entry is.
            errors.add(
                path,
                "mcp_json: a stdio server may not also declare a url — "
                "publish either a local command or a hosted endpoint",
            )
        args = server.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            errors.add(path, "mcp_json: args must be strings")
            args = []
        if launcher:
            problem = validate_stdio_args(launcher, list(args))
            if problem:
                errors.add(path, f"mcp_json: {problem}")
        env = server.get("env", {})
        if not isinstance(env, dict):
            errors.add(path, "mcp_json: env must be an object")
            env = {}
        for key, value in env.items():
            name_text = str(key)
            if not ENV_NAME_RE.fullmatch(name_text) or name_text.upper() in RESERVED_ENV_NAMES:
                # A manifest sets variables for ITS server, never for the
                # machinery that starts it: PATH decides which binary `npx`
                # even is, and the loader hooks run code before the server's
                # first line does.
                errors.add(
                    path,
                    f"mcp_json: env {name_text!r} is not a plugin variable — "
                    "PATH and the loader/runtime hooks are off limits",
                )
            if not isinstance(value, str) or not ENV_PLACEHOLDER_RE.fullmatch(value):
                errors.add(path, f"mcp_json: env {key!r} must be a $plugin_… placeholder")
    elif server_type == "sse":
        errors.add(path, "mcp_json: deprecated 'sse' transport — publish streamable-http")
    else:
        errors.add(path, f"mcp_json: unknown server type {server_type!r}")


# Top-level frontmatter keys a marketplace skill may not declare.
#
# risk_policy is a privilege boundary: the app evaluates a skill's tools
# against the SKILL'S declared tier rather than the tool's own, so an
# auto-merged author could mark a dangerous tool "safe" and skip the
# confirmation it was given. Mirrors
# jarvis/marketplace/agent_plugins_loader.py — a rule enforced on only one
# of the two publishing routes is a rule authors route around.
FORBIDDEN_SKILL_KEYS = ("risk_policy",)
TOP_LEVEL_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:", re.MULTILINE)

# Executable payloads inside a skill. The standard allows scripts/; community
# packages ship instructions, and the index embeds SKILL.md only.
FORBIDDEN_SKILL_DIRS = ("scripts",)


def frontmatter_block(skill_md: str) -> str:
    text = skill_md.lstrip()
    if not text.startswith("---"):
        return ""
    _, _, rest = text.partition("---")
    block, sep, _ = rest.partition("\n---")
    return block if sep else ""


def validate_skill_document(
    skill_md: object, name: str, errors: Errors, path: Path, where: str
) -> None:
    """Every rule a SKILL.md must satisfy, standalone or bundled."""
    if not isinstance(skill_md, str) or not skill_md.strip():
        errors.add(path, f"{where}: the full SKILL.md text is required")
        return
    if len(skill_md.encode("utf-8")) > MAX_SKILL_BYTES:
        errors.add(path, f"{where}: larger than {MAX_SKILL_BYTES} bytes")
    block = frontmatter_block(skill_md)
    if not block.strip():
        errors.add(path, f"{where}: must start with YAML frontmatter (---)")
        return
    declared = {m.group(1) for m in TOP_LEVEL_KEY_RE.finditer(block)}
    for key in FORBIDDEN_SKILL_KEYS:
        if key in declared:
            errors.add(
                path,
                f"{where}: {key!r} may not be declared — it governs which "
                "tools run without confirmation; the built-in default applies",
            )
    for required in ("name", "description"):
        if required not in declared:
            errors.add(path, f"{where}: frontmatter is missing {required!r}")
    if f"name: {name}" not in block:
        errors.add(path, f"{where}: frontmatter name must equal {name!r}")


def validate_bundled_skills(doc: dict, name: str, errors: Errors, path: Path) -> int:
    """The optional skills block on a plugin submission. Returns the count."""
    raw = doc.get("skills")
    if raw is None:
        return 0
    if not isinstance(raw, list):
        errors.add(path, "skills must be a list")
        return 0
    if len(raw) > MAX_BUNDLED_SKILLS:
        errors.add(path, f"at most {MAX_BUNDLED_SKILLS} bundled skills are accepted")
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            errors.add(path, "each bundled skill must be an object")
            continue
        skill_name = item.get("name")
        if not isinstance(skill_name, str) or not NAME_RE.fullmatch(skill_name):
            errors.add(path, f"bundled skill name {skill_name!r} violates the name rules")
            continue
        if "--" in skill_name or ".." in skill_name:
            errors.add(path, f"bundled skill name {skill_name!r} violates the name rules")
            continue
        if skill_name in seen:
            errors.add(path, f"bundled skill {skill_name!r} appears twice")
            continue
        seen.add(skill_name)
        for key in item:
            if key in FORBIDDEN_SKILL_DIRS:
                errors.add(path, f"bundled skill {skill_name!r}: {key}/ may not be published")
        validate_skill_document(
            item.get("skill_md"), skill_name, errors, path, f"skills[{skill_name}]"
        )
    return len(seen)


def validate_plugin(doc: dict, name: str, errors: Errors, path: Path) -> None:
    plugin_json = doc.get("plugin_json")
    if not isinstance(plugin_json, dict):
        errors.add(path, "plugin_json (object) is required for kind=plugin")
        return
    if plugin_json.get("$schema") != PLUGIN_SCHEMA:
        errors.add(path, f"plugin_json.$schema must be {PLUGIN_SCHEMA}")
    if plugin_json.get("name") != name:
        errors.add(path, "plugin_json.name must equal the submission name")
    description = plugin_json.get("description")
    if not isinstance(description, str) or not description.strip():
        errors.add(path, "plugin_json.description is required")
    elif len(description) > MAX_DESCRIPTION_CHARS:
        errors.add(path, f"plugin_json.description longer than {MAX_DESCRIPTION_CHARS} chars")
    extensions = plugin_json.get("extensions")
    extension = extensions.get(EXTENSION_NAMESPACE) if isinstance(extensions, dict) else None
    if not isinstance(extension, dict):
        errors.add(path, f'plugin_json.extensions["{EXTENSION_NAMESPACE}"] is required')
        return
    if extension.get("native_tool"):
        errors.add(path, "native_tool is repo-contributed code — not publishable here")
    auth = extension.get("auth")
    if not isinstance(auth, dict) or auth.get("mode") not in AUTH_MODES:
        errors.add(path, f"extension auth.mode must be one of {AUTH_MODES}")
    template = extension.get("mcp_auth_header_template")
    if template is not None:
        if not isinstance(template, str) or not HEADER_PLACEHOLDER_RE.search(template):
            errors.add(path, "mcp_auth_header_template must use a $plugin_… placeholder")
        elif TOKEN_LITERAL_RE.search(HEADER_PLACEHOLDER_RE.sub(" ", template)):
            errors.add(path, "mcp_auth_header_template may not embed literal credentials")
    mcp_json = doc.get("mcp_json")
    if mcp_json is not None:
        if not isinstance(mcp_json, dict):
            errors.add(path, "mcp_json must be an object or null")
        else:
            validate_mcp_json(name, mcp_json, errors, path)
    usage_card = doc.get("usage_card")
    if usage_card is not None and not isinstance(usage_card, str):
        errors.add(path, "usage_card must be a string or null")
    skill_count = validate_bundled_skills(doc, name, errors, path)
    # A package must carry something that works. Native bindings are rejected
    # above, so without an MCP server, a hosted MCP auth URL, or skills, the
    # store would show a card that collects a token and offers nothing.
    has_auth_mcp = isinstance(auth, dict) and bool(auth.get("mcp_url"))
    if mcp_json is None and not has_auth_mcp and skill_count == 0:
        errors.add(
            path,
            "package has no components — needs an mcp_json, a hosted MCP auth "
            "mode, or bundled skills",
        )
    reject_http_urls(doc, "submission", errors, path)


def validate_skill(doc: dict, name: str, errors: Errors, path: Path) -> None:
    validate_skill_document(doc.get("skill_md"), name, errors, path, "skill_md")
    title = doc.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.add(path, "title is required for kind=skill")
    description = doc.get("description")
    if not isinstance(description, str) or not description.strip():
        errors.add(path, "description is required for kind=skill")
    elif len(description) > MAX_DESCRIPTION_CHARS:
        errors.add(path, f"description longer than {MAX_DESCRIPTION_CHARS} chars")
    categories = doc.get("categories", [])
    if not isinstance(categories, list) or not all(isinstance(c, str) for c in categories):
        errors.add(path, "categories must be a list of strings")
    validate_flavor(doc, errors, path)


# Which frontmatter a skill declares. Optional and free to omit: build_index.py
# derives it from the file itself, so an author who never heard of the field
# still gets the right mark. Stating it is for the case where the derivation
# would be wrong — a Jarvis-flavored file whose author also maintains it for
# other agents, say. Mirrors SKILL_FLAVORS in
# jarvis/marketplace/community_source.py and build_index.py.
SKILL_FLAVORS = ("jarvis", "portable")

# The publisher's "also runs in" list. Display-only text that lands in the
# store UI, so it is bounded HERE too: the feed's own cleanup would silently
# truncate, and a submitter deserves to be told instead.
MAX_COMPATIBLE_AGENTS = 8
MAX_AGENT_NAME_CHARS = 32


def validate_flavor(doc: dict, errors: Errors, path: Path) -> None:
    """The optional portability fields on a skill submission."""
    flavor = doc.get("flavor")
    if flavor is not None and (
        not isinstance(flavor, str) or flavor not in SKILL_FLAVORS
    ):
        errors.add(path, f"flavor must be one of {list(SKILL_FLAVORS)} when set")

    agents = doc.get("compatible_agents")
    if agents is None:
        return
    if not isinstance(agents, list) or not all(isinstance(a, str) for a in agents):
        errors.add(path, "compatible_agents must be a list of strings")
        return
    if len(agents) > MAX_COMPATIBLE_AGENTS:
        errors.add(path, f"compatible_agents: at most {MAX_COMPATIBLE_AGENTS} entries")
    for agent in agents:
        if len(agent) > MAX_AGENT_NAME_CHARS:
            errors.add(
                path,
                f"compatible_agents: {agent[:20]!r}… longer than "
                f"{MAX_AGENT_NAME_CHARS} chars",
            )


# ---------------------------------------------------------------------------
# kind=agent — a shareable agent template
# ---------------------------------------------------------------------------
#
# An agent template is the DESIGN of one Personal Jarvis agent: its name, job
# line, standing instructions, look, and which tools it reaches for first. It
# is installed on a stranger's machine, so the rule that matters is what it
# may NOT carry. Everything that names the author's machine (accounts, paths,
# connected computers) or widens what the agent may do without asking (the
# permission ceiling, an approval bypass, always-allow rules, a spending
# budget, browser attach mode) is refused outright, by an allowlist: a field
# travels because it is named below, never because it happened to exist.
# Mirrors jarvis/society/agent_template.py in the app — change both.

AGENT_TEMPLATE_SCHEMA = 1
AGENT_KEYS = frozenset(
    {
        "schema",
        "name",
        "title",
        "instructions",
        "tier",
        "effort",
        "focus",
        "grant_mode",
        "grants",
        "denies",
        "skills",
        "require_approval",
        "knowledge_scope",
        "avatar",
    }
)
# Named so the error says WHY, not just "unknown field".
AGENT_FORBIDDEN_KEYS = {
    "account_id": "names the author's subscription seat",
    "provider": "the installer picks their own model",
    "model": "the installer picks their own model",
    "workspace_dir": "a folder on the author's machine",
    "wiki_namespace": "derived on the installing machine",
    "computer_id": "a machine only the author has",
    "parent_agent_id": "an id from the author's team",
    "permission_ceiling": "a template may not raise what runs without asking",
    "approval_mode": "a template may not switch approvals off",
    "approval_rules": "only require_approval travels — never always_allow",
    "always_allow": "a template may not pre-approve tools",
    "daily_budget_usd": "the installer's own budget applies",
    "max_concurrent_runs": "the installer's own limit applies",
    "browser_mode": "attach mode drives the installer's own browser",
    "browser_allowed_domains": "the installer's own browser settings apply",
}
AGENT_TIERS = ("specialist", "orchestrator")
AGENT_GRANT_MODES = ("all", "allowlist")
AGENT_KNOWLEDGE_SCOPES = ("shared", "own")
AGENT_EFFORT_RE = re.compile(r"^[a-z]{0,16}$")
# Roster rule: 1-40 chars, none of / \ : @ # < > " ' ` and no control chars.
AGENT_NAME_RE = re.compile(r"^[^/\\:@#<>\"'`\x00-\x1f]{1,40}$")
# Capability ids as the app writes them: plugin:gmail, core:browser,
# skill:daily-brief, plugin:gmail:send …
CAPABILITY_ID_RE = re.compile(r"^[a-z]+:[A-Za-z0-9_.:-]{1,80}$")
SKILL_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
# The figure recipe: catalog ids and colours only. `model` (an imported GLB)
# is a URL on the author's machine and never travels.
AVATAR_STRING_KEYS = ("archetype", "base", "style", "hairStyle", "outfit", "eyewear")
AVATAR_KEYS = frozenset(
    {"contract", "parts", "palette", "heightM", "companion", "inner", *AVATAR_STRING_KEYS}
)
AVATAR_ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
HEX_COLOUR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
COMPANION_KEYS = frozenset({"shape", "color", "eyes", "enabled", "sizeM", "followDistanceM"})
# A home folder in instructions is the author's machine talking — and usually
# their real name. Credentials are caught by SECRET_PATTERNS on the raw file.
HOME_PATH_RE = re.compile(
    r"(?:\b[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s]+|/Users/[^/\s]+|/home/[^/\s]+)",
    re.IGNORECASE,
)


def _string_list(value, where: str, errors: Errors, path: Path, pattern) -> None:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors.add(path, f"{where} must be a list of strings")
        return
    if len(value) > MAX_AGENT_CAPABILITIES:
        errors.add(path, f"{where}: at most {MAX_AGENT_CAPABILITIES} entries")
    for item in value:
        if not pattern.fullmatch(item):
            errors.add(path, f"{where}: {item[:40]!r} is not a valid id")


def _id_map(value, values_re) -> bool:
    return (
        isinstance(value, dict)
        and len(value) <= 32
        and all(
            isinstance(k, str)
            and AVATAR_ID_RE.fullmatch(k)
            and isinstance(v, str)
            and values_re.fullmatch(v)
            for k, v in value.items()
        )
    )


def validate_avatar(avatar, errors: Errors, path: Path) -> None:
    if not isinstance(avatar, dict):
        errors.add(path, "agent.avatar must be an object")
        return
    for key in avatar:
        if key not in AVATAR_KEYS:
            errors.add(path, f"agent.avatar.{key} may not be published (catalog look only)")
    for key in AVATAR_STRING_KEYS:
        value = avatar.get(key)
        if value is not None and (not isinstance(value, str) or not AVATAR_ID_RE.fullmatch(value)):
            errors.add(path, f"agent.avatar.{key} must be a catalog id")
    if "contract" in avatar and avatar["contract"] != 1:
        errors.add(path, "agent.avatar.contract must be 1")
    if "parts" in avatar and not _id_map(avatar["parts"], AVATAR_ID_RE):
        errors.add(path, "agent.avatar.parts must map slot ids to part ids")
    if "palette" in avatar and not _id_map(avatar["palette"], HEX_COLOUR_RE):
        errors.add(path, "agent.avatar.palette must map cell names to #rrggbb colours")
    inner = avatar.get("inner")
    if inner is not None and (not isinstance(inner, str) or not HEX_COLOUR_RE.fullmatch(inner)):
        errors.add(path, "agent.avatar.inner must be a #rrggbb colour")
    height = avatar.get("heightM")
    if height is not None and (
        isinstance(height, bool) or not isinstance(height, (int, float)) or not 0.2 <= height <= 4
    ):
        errors.add(path, "agent.avatar.heightM must be a number between 0.2 and 4")
    companion = avatar.get("companion")
    if companion is not None:
        if not isinstance(companion, dict) or any(k not in COMPANION_KEYS for k in companion):
            errors.add(path, "agent.avatar.companion carries unknown fields")
        elif "color" in companion and not (
            isinstance(companion["color"], str) and HEX_COLOUR_RE.fullmatch(companion["color"])
        ):
            errors.add(path, "agent.avatar.companion.color must be a #rrggbb colour")


def validate_agent(doc: dict, name: str, errors: Errors, path: Path) -> None:
    title = doc.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.add(path, "title is required for kind=agent")
    description = doc.get("description")
    if not isinstance(description, str) or not description.strip():
        errors.add(path, "description is required for kind=agent")
    elif len(description) > MAX_DESCRIPTION_CHARS:
        errors.add(path, f"description longer than {MAX_DESCRIPTION_CHARS} chars")
    categories = doc.get("categories", [])
    if not isinstance(categories, list) or not all(isinstance(c, str) for c in categories):
        errors.add(path, "categories must be a list of strings")

    agent = doc.get("agent")
    if not isinstance(agent, dict):
        errors.add(path, "agent (object) is required for kind=agent")
        return
    for key in agent:
        if key in AGENT_FORBIDDEN_KEYS:
            errors.add(path, f"agent.{key} may not be published — {AGENT_FORBIDDEN_KEYS[key]}")
        elif key not in AGENT_KEYS:
            errors.add(path, f"agent.{key} is not a template field")
    if agent.get("schema") != AGENT_TEMPLATE_SCHEMA:
        errors.add(path, f"agent.schema must be {AGENT_TEMPLATE_SCHEMA}")

    agent_name = agent.get("name")
    if not isinstance(agent_name, str) or not AGENT_NAME_RE.fullmatch(agent_name.strip() or "/"):
        errors.add(
            path,
            f"agent.name must be 1-{MAX_AGENT_NAME_CHARS} characters without "
            "/ \\ : @ # < > or quotes",
        )
    elif agent_name.strip().lower() == "jarvis":
        errors.add(path, "agent.name 'Jarvis' is the lead every install already has")
    agent_title = agent.get("title", "")
    if not isinstance(agent_title, str) or len(agent_title) > MAX_AGENT_TITLE_CHARS:
        errors.add(path, f"agent.title must be a string of at most {MAX_AGENT_TITLE_CHARS} chars")
    instructions = agent.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        errors.add(path, "agent.instructions (the standing instructions) are required")
    else:
        if len(instructions.encode("utf-8")) > MAX_AGENT_INSTRUCTIONS_BYTES:
            errors.add(
                path, f"agent.instructions larger than {MAX_AGENT_INSTRUCTIONS_BYTES} bytes"
            )
        home = HOME_PATH_RE.search(instructions)
        if home:
            errors.add(
                path,
                f"agent.instructions name a folder on your machine ({home.group(0)!r}) "
                "— describe it instead",
            )

    if agent.get("tier", "specialist") not in AGENT_TIERS:
        errors.add(path, f"agent.tier must be one of {list(AGENT_TIERS)}")
    if agent.get("grant_mode", "all") not in AGENT_GRANT_MODES:
        errors.add(path, f"agent.grant_mode must be one of {list(AGENT_GRANT_MODES)}")
    if agent.get("knowledge_scope", "shared") not in AGENT_KNOWLEDGE_SCOPES:
        errors.add(path, f"agent.knowledge_scope must be one of {list(AGENT_KNOWLEDGE_SCOPES)}")
    effort = agent.get("effort", "")
    if not isinstance(effort, str) or not AGENT_EFFORT_RE.fullmatch(effort):
        errors.add(path, "agent.effort must be a short lowercase word or empty")
    for key in ("focus", "grants", "denies", "require_approval"):
        if key in agent:
            _string_list(agent[key], f"agent.{key}", errors, path, CAPABILITY_ID_RE)
    if agent.get("skills") is not None:
        _string_list(agent["skills"], "agent.skills", errors, path, SKILL_SLUG_RE)
    if "avatar" in agent:
        validate_avatar(agent["avatar"], errors, path)
    reject_http_urls(doc, "submission", errors, path)


# ---------------------------------------------------------------------------
# kind=look — a visual that takes the voice orb's place
# ---------------------------------------------------------------------------
#
# A look is a folder pack (look.json + an HTML entry page + a preview + the
# files the page loads: scripts, Three.js, .glb models). Packs are too large
# to live in this repository, so the publisher hosts the zip as a GitHub
# release asset and the submission pins its SHA-256. This check downloads the
# archive as DATA, recomputes the digest, and reads the zip in memory with the
# same rules the app applies when it installs it. Nothing is unpacked to disk
# and nothing in the pack is executed.
#
# Keep in sync with the app: dodaios/ui/looks/manifest.py (format, id,
# version, license, file kinds, limits), dodaios/ui/looks/loader.py (pack
# files), dodaios/ui/looks/install.py (archive rules, ALLOWED_HOSTS,
# MAX_ARCHIVE_BYTES) and CommunityLookEntry in
# dodaios/marketplace/community_source.py (the feed entry).

LOOK_FORMAT = "dodaios-look/1"
LOOK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")
# manifest.RESERVED_IDS: "orb" is the built-in voice orb.
RESERVED_LOOK_IDS = frozenset({"orb", "none", "default"})
# The looks the app ships (dodaios/ui/looks/builtin/). A built-in look wins
# over an installed one, and the app refuses to install over it.
BUILTIN_LOOK_IDS = frozenset({"pulse-ring"})
_LOOK_SEGMENT = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}"
LOOK_REL_PATH_RE = re.compile(rf"^(?:{_LOOK_SEGMENT}/){{0,4}}{_LOOK_SEGMENT}$")
LOOK_VERSION_RE = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
LOOK_LICENSE_RE = re.compile(r"^[A-Za-z0-9.+-]{2,40}(?: (?:AND|OR) [A-Za-z0-9.+-]{2,40}){0,3}$")
# manifest.ALLOWED_TYPES, by lowercase suffix.
LOOK_ALLOWED_SUFFIXES = (
    ".html", ".js", ".mjs", ".css", ".json", ".txt", ".md",
    ".png", ".jpg", ".jpeg", ".webp", ".svg",
    ".glb", ".gltf", ".bin", ".ktx2", ".hdr", ".woff2", ".wasm",
)
LOOK_PREVIEW_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".svg")
MAX_LOOK_PACK_BYTES = 25 * 1024 * 1024
MAX_LOOK_PACK_FILES = 300
MAX_LOOK_MANIFEST_BYTES = 16 * 1024
# install.MAX_ARCHIVE_BYTES: the pack limit plus slack for the zip's headers.
MAX_LOOK_ARCHIVE_BYTES = MAX_LOOK_PACK_BYTES + 2 * 1024 * 1024
MAX_LOOK_TITLE_CHARS = 40  # manifest MAX_NAME_CHARS
MAX_LOOK_DESCRIPTION_CHARS = 200  # manifest MAX_DESCRIPTION_CHARS
MAX_LOOK_AUTHOR_CHARS = 60
MAX_LOOK_ATTRIBUTION_CHARS = 200
MAX_LOOK_HOMEPAGE_CHARS = 200
MAX_LOOK_URL_CHARS = 400
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# A GitHub release asset, and nothing else: the one place a publisher can host
# a 25 MB zip that the app's ALLOWED_HOSTS also covers.
LOOK_ARCHIVE_URL_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}"
    r"/releases/download/[A-Za-z0-9._+-]{1,128}/[A-Za-z0-9._-]{1,128}\.zip$"
)
# Where a release download may go. github.com answers with a redirect to one
# of the two asset hosts; any other hop is refused before it is followed.
LOOK_DOWNLOAD_HOSTS = frozenset(
    {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
)
LOOK_KEYS = frozenset(
    {
        "kind", "name", "publisher", "publisher_id", "version", "title", "description",
        "license", "attribution", "source_url", "archive_url", "sha256",
    }
)
# "Jarvis" is another company's trademark; no look may be listed under it.
TRADEMARK_RE = re.compile(r"jarvis", re.IGNORECASE)
# The bytes each preview format starts with. The app does not look; the
# registry does, so a preview that is not the picture it claims is refused
# here instead of turning into a listing without a picture.
_PREVIEW_MAGIC = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
}

#: Download archives while validating. ``--no-fetch`` turns it off for the
#: push-to-main run, where every archive was already checked at merge time.
FETCH_ARCHIVES = True


class LookError(ValueError):
    """A look archive or its look.json breaks a rule; the message says which."""


def look_path_ok(value: object) -> bool:
    """manifest.is_safe_rel_path: a relative pack path such as ``assets/a.glb``."""
    if not isinstance(value, str) or not LOOK_REL_PATH_RE.match(value):
        return False
    return all(part not in {".", ".."} for part in value.split("/"))


def look_suffix(rel_path: str) -> str:
    dot = rel_path.rfind(".")
    return rel_path[dot:].lower() if dot > rel_path.rfind("/") else ""


def _plain_text(value: object, where: str, limit: int, *, required: bool) -> str:
    """manifest._text: stripped text, bounded, without control characters."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise LookError(f"{where} must be text")
    value = value.strip()
    if required and not value:
        raise LookError(f"{where} is required")
    if len(value) > limit:
        raise LookError(f"{where} is longer than {limit} characters")
    if any(ord(ch) < 32 for ch in value):
        raise LookError(f"{where} contains control characters")
    return value


class _GitHubOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only while it stays on https and a GitHub asset host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        if not _download_url_ok(newurl):
            raise LookError(f"the download was redirected off GitHub ({urlsplit(newurl).hostname})")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_url_ok(url: str) -> bool:
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and (parts.hostname or "").lower() in LOOK_DOWNLOAD_HOSTS
        and not parts.username
        and not parts.password
        and parts.port in (None, 443)
    )


def fetch_look_archive(url: str) -> bytes:
    """Download a look archive: https on GitHub only, never more than the cap.

    Tests replace this function; nothing else in this file touches the network.
    """
    if not LOOK_ARCHIVE_URL_RE.fullmatch(url):
        raise LookError("archive_url is not a GitHub release asset")
    opener = urllib.request.build_opener(_GitHubOnlyRedirects())
    # The scheme is fixed to https by LOOK_ARCHIVE_URL_RE above.
    request = urllib.request.Request(  # noqa: S310
        url, headers={"User-Agent": "dodaios-marketplace-validate"}
    )
    try:
        with opener.open(request, timeout=60) as response:
            if not _download_url_ok(response.geturl()):
                raise LookError("the download ended on a host that is not GitHub")
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > MAX_LOOK_ARCHIVE_BYTES:
                raise LookError(f"the archive is larger than {MAX_LOOK_ARCHIVE_BYTES} bytes")
            buf = io.BytesIO()
            while chunk := response.read(64 * 1024):
                buf.write(chunk)
                if buf.tell() > MAX_LOOK_ARCHIVE_BYTES:
                    raise LookError(f"the archive is larger than {MAX_LOOK_ARCHIVE_BYTES} bytes")
            return buf.getvalue()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LookError(f"the archive could not be downloaded: {exc}") from exc


def _zip_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    """install._members + loader.check_pack_files, on the zip's own listing.

    Returns pack path -> ZipInfo. The pack sits at the zip's root or inside
    one top folder, exactly as the app accepts it.
    """
    infos = [info for info in archive.infolist() if not info.is_dir()]
    if not infos:
        raise LookError("the archive is empty")
    if len(infos) > MAX_LOOK_PACK_FILES:
        raise LookError(f"a look may hold at most {MAX_LOOK_PACK_FILES} files")
    names = [info.filename.replace("\\", "/") for info in infos]
    tops = {name.split("/", 1)[0] for name in names}
    strip = ""
    if len(tops) == 1 and all("/" in name for name in names):
        strip = next(iter(tops)) + "/"

    total = 0
    members: dict[str, zipfile.ZipInfo] = {}
    folded: set[str] = set()
    for info, name in zip(infos, names, strict=True):
        mode = (info.external_attr >> 16) & 0xFFFF
        if mode and stat.S_ISLNK(mode):
            raise LookError(f"the archive contains a link: {name}")
        if info.flag_bits & 0x1:
            raise LookError("the archive is encrypted")
        rel = name[len(strip):] if strip else name
        if not look_path_ok(rel):
            raise LookError(f"the archive contains a file name that is not allowed: {name}")
        if look_suffix(rel) not in LOOK_ALLOWED_SUFFIXES:
            raise LookError(f"the file kind of {rel!r} is not allowed in a look")
        # Stricter than the app, which would crash on these while unpacking:
        # the same path twice, or two paths that collide on a case-insensitive
        # disk (Windows, macOS).
        if rel.lower() in folded:
            raise LookError(f"the archive holds {rel!r} twice (names are compared ignoring case)")
        folded.add(rel.lower())
        total += info.file_size
        if total > MAX_LOOK_PACK_BYTES:
            mb = MAX_LOOK_PACK_BYTES // (1024 * 1024)
            raise LookError(f"a look may be at most {mb} MB unpacked")
        members[rel] = info
    # A file whose path is also another file's folder cannot be unpacked.
    for rel in members:
        parts = rel.lower().split("/")
        for depth in range(1, len(parts)):
            if "/".join(parts[:depth]) in folded:
                raise LookError(f"{rel!r} sits inside a path that is also a file")
    return members


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    """One member's bytes, never more than its header claims (CRC checked)."""
    buf = io.BytesIO()
    with archive.open(info) as src:
        while chunk := src.read(64 * 1024):
            buf.write(chunk)
            # The header's size is a claim; the stream is the truth.
            if buf.tell() > info.file_size:
                raise LookError("the archive's sizes do not match its contents")
    return buf.getvalue()


def parse_look_manifest(data: object) -> dict:
    """manifest.parse_manifest: the checked look.json fields, or LookError."""
    if not isinstance(data, dict):
        raise LookError("look.json must hold one JSON object")
    if data.get("format") != LOOK_FORMAT:
        raise LookError(f'look.json must say "format": "{LOOK_FORMAT}"')
    look_id = data.get("id")
    if not isinstance(look_id, str) or not LOOK_ID_RE.match(look_id):
        raise LookError(
            "look.json id must be 2 to 48 lowercase letters, digits or dashes, "
            "starting with a letter or digit"
        )
    if look_id in RESERVED_LOOK_IDS:
        raise LookError(f"the look id {look_id!r} is reserved")
    version = _plain_text(data.get("version", ""), "look.json version", 14, required=True)
    if not LOOK_VERSION_RE.match(version):
        raise LookError("look.json version must look like 1.0.0")
    license_id = _plain_text(data.get("license", ""), "look.json license", 120, required=True)
    if not LOOK_LICENSE_RE.match(license_id):
        raise LookError("look.json license must be an SPDX license id such as Apache-2.0")
    entry = data.get("entry", "index.html")
    if not look_path_ok(entry) or look_suffix(entry) != ".html":
        raise LookError("look.json entry must be a relative path to an .html file in the pack")
    preview = data.get("preview")
    if not look_path_ok(preview) or look_suffix(preview) not in LOOK_PREVIEW_SUFFIXES:
        raise LookError(
            "look.json preview must be a relative path to a .png, .jpg, .webp or .svg file"
        )
    homepage = _plain_text(data.get("homepage"), "look.json homepage", MAX_LOOK_HOMEPAGE_CHARS,
                           required=False)
    if homepage and not homepage.startswith("https://"):
        raise LookError("look.json homepage must start with https://")
    return {
        "id": look_id,
        "name": _plain_text(
            data.get("name"), "look.json name", MAX_LOOK_TITLE_CHARS, required=True
        ),
        "description": _plain_text(data.get("description"), "look.json description",
                                   MAX_LOOK_DESCRIPTION_CHARS, required=False),
        "author": _plain_text(data.get("author"), "look.json author", MAX_LOOK_AUTHOR_CHARS,
                              required=True),
        "version": version,
        "license": license_id,
        "entry": entry,
        "preview": preview,
        "attribution": _plain_text(data.get("attribution"), "look.json attribution",
                                   MAX_LOOK_ATTRIBUTION_CHARS, required=False),
        "homepage": homepage,
    }


def read_look_pack(data: bytes) -> dict:
    """Check a downloaded archive as a look pack, in memory, without trusting it.

    Every member is decompressed (and its CRC checked) but never written
    anywhere. Returns ``{"manifest", "files", "preview_bytes"}`` where
    ``files`` is the pack's ``[{path, size}]`` listing.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError) as exc:
        raise LookError("the archive is not a valid zip file") from exc
    with archive:
        members = _zip_members(archive)
        contents: dict[str, bytes] = {}
        try:
            for rel, info in members.items():
                contents[rel] = _read_member(archive, info)
        except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, OSError,
                EOFError, RuntimeError) as exc:
            # RuntimeError: an encrypted member zipfile notices only now.
            raise LookError(f"the archive could not be read: {exc}") from exc

    raw = contents.get("look.json")
    if raw is None:
        raise LookError("the pack has no look.json")
    if len(raw) > MAX_LOOK_MANIFEST_BYTES:
        raise LookError("look.json is too large")
    try:
        decoded = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise LookError("look.json could not be read as JSON") from exc
    manifest = parse_look_manifest(decoded)
    for rel in (manifest["entry"], manifest["preview"]):
        if rel not in contents:
            raise LookError(f"the pack has no file {rel!r}")
    preview_bytes = contents[manifest["preview"]]
    suffix = look_suffix(manifest["preview"])
    magic = _PREVIEW_MAGIC.get(suffix)
    if magic and not preview_bytes.startswith(magic):
        raise LookError(
            f"the preview {manifest['preview']!r} is not a {suffix[1:].upper()} picture"
        )
    if suffix == ".webp" and not (preview_bytes[:4] == b"RIFF" and preview_bytes[8:12] == b"WEBP"):
        raise LookError(f"the preview {manifest['preview']!r} is not a WEBP picture")
    files = sorted(
        ({"path": rel, "size": len(body)} for rel, body in contents.items()),
        key=lambda f: (f["path"].count("/"), f["path"]),
    )
    return {"manifest": manifest, "files": files, "preview_bytes": preview_bytes}


def check_look_archive(doc: dict) -> dict:
    """Download the submission's archive and check it against the submission.

    Returns what :func:`read_look_pack` returns plus ``size_bytes``; raises
    LookError with the first problem.
    """
    data = fetch_look_archive(doc["archive_url"])
    digest = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(digest, doc["sha256"]):
        raise LookError(
            f"the archive's SHA-256 is {digest}, not the submitted {doc['sha256']} — "
            "recompute it from the file you uploaded to the release"
        )
    pack = read_look_pack(data)
    manifest = pack["manifest"]
    if manifest["id"] != doc["name"]:
        raise LookError(
            f"look.json id {manifest['id']!r} must equal the submission name {doc['name']!r}"
        )
    if manifest["version"] != doc["version"]:
        raise LookError(
            f"look.json version {manifest['version']!r} must equal the submission version "
            f"{doc['version']!r}"
        )
    if manifest["license"] != doc["license"]:
        raise LookError(
            f"look.json license {manifest['license']!r} must equal the submission license "
            f"{doc['license']!r}"
        )
    if TRADEMARK_RE.search(manifest["name"]):
        raise LookError("look.json name may not contain 'Jarvis' (a trademark of another company)")
    pack["size_bytes"] = len(data)
    return pack


def validate_look(doc: dict, name: str, errors: Errors, path: Path, *, fetch: bool) -> None:
    before = len(errors.items)
    for key in doc:
        if key not in LOOK_KEYS:
            errors.add(path, f"{key} is not a look submission field")
    if not LOOK_ID_RE.fullmatch(name):
        errors.add(path, "a look name must be 2 to 48 lowercase letters, digits or dashes")
    if name in RESERVED_LOOK_IDS:
        errors.add(path, f"{name!r} is reserved (the app's own orb setting)")
    if name in BUILTIN_LOOK_IDS:
        errors.add(path, f"{name!r} is a look the app ships — reserved")
    if TRADEMARK_RE.search(name):
        errors.add(path, "a look name may not contain 'jarvis' (a trademark of another company)")

    for key, limit, required in (
        ("title", MAX_LOOK_TITLE_CHARS, True),
        ("description", MAX_LOOK_DESCRIPTION_CHARS, True),
        ("attribution", MAX_LOOK_ATTRIBUTION_CHARS, False),
    ):
        try:
            value = _plain_text(doc.get(key), key, limit, required=required)
        except LookError as exc:
            errors.add(path, f"{exc} (kind=look)")
            continue
        if key == "title" and TRADEMARK_RE.search(value):
            errors.add(
                path, "a look title may not contain 'Jarvis' (a trademark of another company)"
            )

    version = doc.get("version")
    if isinstance(version, str) and not LOOK_VERSION_RE.fullmatch(version):
        errors.add(path, "a look version must be MAJOR.MINOR.PATCH with at most 4 digits each")
    license_id = doc.get("license")
    if not isinstance(license_id, str) or not LOOK_LICENSE_RE.fullmatch(license_id):
        errors.add(path, "license must be an SPDX id such as Apache-2.0, or 'A AND B'")
    source_url = doc.get("source_url")
    if (
        not isinstance(source_url, str)
        or not source_url.startswith("https://")
        or len(source_url) > MAX_LOOK_URL_CHARS
        or any(ch.isspace() or ord(ch) < 32 for ch in source_url)
    ):
        errors.add(path, "source_url must be an https:// link to the look's source")
    archive_url = doc.get("archive_url")
    if not isinstance(archive_url, str) or not LOOK_ARCHIVE_URL_RE.fullmatch(archive_url):
        errors.add(
            path,
            "archive_url must be a GitHub release asset: "
            "https://github.com/<owner>/<repo>/releases/download/<tag>/<file>.zip",
        )
    sha256 = doc.get("sha256")
    if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
        errors.add(path, "sha256 must be the archive's SHA-256 as 64 lowercase hex digits")
    reject_http_urls(doc, "submission", errors, path)

    # The download is the expensive half; it runs only on a submission that is
    # otherwise sound.
    if not fetch or len(errors.items) > before:
        return
    if not isinstance(version, str) or not SEMVER_RE.fullmatch(version):
        return
    try:
        check_look_archive(doc)
    except LookError as exc:
        errors.add(path, f"archive: {exc}")


def read_base_version(path: Path, base_ref: str) -> dict | None:
    """The submission as it exists at base_ref, or None when new."""
    rel = path.resolve().relative_to(ROOT).as_posix()
    proc = subprocess.run(
        ["git", "show", f"{base_ref}:{rel}"],
        capture_output=True,
        # Submissions are UTF-8; the runner's locale (cp1252 on Windows) is not.
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
    )
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def account_id(doc: dict) -> int | None:
    """The submission's numeric GitHub account id, or None when absent/invalid.

    ``bool`` is a subclass of ``int`` in Python, so ``true`` would otherwise
    pass as the id ``1``.
    """
    value = doc.get("publisher_id")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def validate_file(path: Path, errors: Errors, base_ref: str | None) -> None:
    raw = path.read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        errors.add(path, f"file larger than {MAX_FILE_BYTES} bytes")
        return
    text = raw.decode("utf-8", errors="replace")
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            errors.add(
                path, f"matches credential pattern {pattern.pattern!r} — never submit secrets"
            )
    try:
        doc = json.loads(text)
    except ValueError as exc:
        errors.add(path, f"not valid JSON: {exc}")
        return
    if not isinstance(doc, dict):
        errors.add(path, "submission must be a JSON object")
        return

    name = doc.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or "--" in name or ".." in name:
        errors.add(path, f"name {name!r} violates the naming rules (a-z 0-9 - . / no '--' '..')")
        return
    if path.name != f"{name}.json":
        errors.add(path, f"file must be named {name}.json")
    kind = doc.get("kind")
    if kind not in KINDS:
        errors.add(path, "kind must be 'plugin', 'skill', 'agent' or 'look'")
        return
    if kind == "plugin" and name in RESERVED_PLUGIN_IDS:
        errors.add(path, f"{name!r} is a built-in Personal Jarvis plugin id — reserved")
    if kind == "skill" and name in RESERVED_SKILL_NAMES:
        errors.add(path, f"{name!r} is a built-in Personal Jarvis skill name — reserved")

    publisher = doc.get("publisher")
    if not isinstance(publisher, str) or not GITHUB_LOGIN_RE.fullmatch(publisher):
        errors.add(path, "publisher must be the author's GitHub username")
    publisher_id = account_id(doc)
    if doc.get("publisher_id") is not None and publisher_id is None:
        errors.add(path, "publisher_id must be a positive integer (your numeric GitHub account id)")
    version = doc.get("version")
    if not isinstance(version, str) or not SEMVER_RE.fullmatch(version):
        errors.add(path, "version must be SemVer (MAJOR.MINOR.PATCH)")

    if kind == "plugin":
        validate_plugin(doc, name, errors, path)
    elif kind == "agent":
        validate_agent(doc, name, errors, path)
    elif kind == "look":
        # The archive is downloaded only for a submission this change adds or
        # edits. The workflow validates the whole catalogue on every PR, and
        # an unchanged look was checked when it was merged.
        unchanged = bool(base_ref) and read_base_version(path, base_ref) == doc
        validate_look(doc, name, errors, path, fetch=FETCH_ARCHIVES and not unchanged)
    else:
        validate_skill(doc, name, errors, path)

    if base_ref and isinstance(version, str) and SEMVER_RE.fullmatch(version):
        base = read_base_version(path, base_ref)
        if base == doc:
            # The workflow validates EVERY submission, not only the ones a
            # PR touches. A file identical to its base version is untouched
            # in this change — the update rules (ownership, version bump)
            # must not fire for it, or every PR fails on the back catalog.
            base = None
        if base is not None:
            base_id = account_id(base)
            if base_id is not None:
                # Once an entry records an id, that id is the ONLY ownership
                # key. Dropping the field must not fall back to the login
                # comparison — omitting it would otherwise be the bypass.
                if publisher_id is None:
                    errors.add(
                        path,
                        "publisher_id is required to update this entry — it is owned by "
                        f"account id {base_id}. Add your numeric GitHub account id.",
                    )
                elif publisher_id != base_id:
                    errors.add(
                        path,
                        f"publisher_id may not change on an update (ownership rule): "
                        f"this entry belongs to account id {base_id}",
                    )
            elif base.get("publisher") != publisher:
                errors.add(path, "publisher may not change on an update (ownership rule)")
            if base.get("kind") != kind:
                errors.add(
                    path,
                    f"kind may not change on an update (this name is a {base.get('kind')!r})",
                )
            base_version = str(base.get("version", "0.0.0"))
            base_ok = SEMVER_RE.fullmatch(base_version)
            if base_ok and version_tuple(version) <= version_tuple(base_version):
                errors.add(path, f"version must increase (base has {base_version})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", help="submission files (default: submissions/*.json)")
    parser.add_argument("--base-ref", default=None, help="git ref for ownership/version checks")
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="do not download look archives (they were checked when they were merged)",
    )
    args = parser.parse_args()
    global FETCH_ARCHIVES
    FETCH_ARCHIVES = not args.no_fetch

    files = [Path(f) for f in args.files] or sorted((ROOT / "submissions").glob("*.json"))
    errors = Errors()
    for path in files:
        if not path.exists():
            errors.add(path, "file does not exist")
            continue
        validate_file(path, errors, args.base_ref)

    if errors.items:
        print(f"FAIL — {len(errors.items)} problem(s):")
        for item in errors.items:
            print(f"  - {item}")
        return 1
    print(f"OK — {len(files)} submission(s) valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
