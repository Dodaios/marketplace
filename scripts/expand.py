#!/usr/bin/env python3
"""Expand validated submissions into the published registry layout.

Runs on main after every merge (publish workflow). For each
``submissions/<name>.json``:

- kind=plugin → ``plugins/<name>/plugin.json`` (+ ``mcp.json``,
  ``io.github.personaljarvis/usage-card.md``) per the Agent Plugins v1.0.0
  packaging standard.
- kind=skill  → ``skills/<name>/SKILL.md`` (the file the app downloads).
- kind=agent  → ``agents/<name>/agent.json`` (the agent template the app
  installs as a new teammate).
- kind=look   → ``looks/<name>/archive.json`` (the facts of the pinned
  archive: digest, size, manifest, file listing) and
  ``looks/<name>/preview-<sha12>.png`` (the pack's preview, re-encoded by
  Pillow). The archive itself never enters the repository; the publisher
  hosts it as a GitHub release asset.

``registry.json`` records name → {kind, publisher, version, published_at};
it is the ownership ledger the auto-merge gate enforces. ``published_at`` is
stamped on first publish and refreshed when the version changes.

Idempotent: a second run with no new submissions changes nothing, so the
publish workflow cannot loop on its own expansion commit.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import warnings
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTENSION_DIR = "io.github.personaljarvis"

#: The longest edge of a look's published preview, in pixels.
PREVIEW_MAX_EDGE = 512
#: Pillow refuses to decode anything larger: a preview is a picture, not a
#: decompression bomb. 64 megapixels is far above any real preview.
PREVIEW_MAX_PIXELS = 64_000_000


def write_if_changed(path: Path, content: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return True


def dump(document: object) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def load_validator():
    spec = importlib.util.spec_from_file_location("validate", ROOT / "scripts" / "validate.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def preview_file_name(sha256: str) -> str:
    """The published preview's name: a new archive gets a new address."""
    return f"preview-{sha256[:12]}.png"


def render_preview(raw: bytes) -> bytes:
    """Re-encode a pack's preview as a PNG of at most 512 px on its long edge.

    The bytes the publisher shipped are never served: Pillow decodes them and
    writes a fresh PNG, so metadata, an appended payload or a forged header
    does not reach the site. Raises on anything that is not a PNG, JPEG or
    WebP that Pillow can decode within the pixel cap.
    """
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = PREVIEW_MAX_PIXELS
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(raw), formats=("PNG", "JPEG", "WEBP")) as image:
            image.seek(0)  # the first frame of an animated picture
            image.load()
            picture = image.convert("RGBA")
    picture.thumbnail((PREVIEW_MAX_EDGE, PREVIEW_MAX_EDGE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    picture.save(out, "PNG", optimize=True)
    return out.getvalue()


def expand_look(doc: dict, validator) -> bool:
    """Derive ``looks/<name>/`` for one look. Returns whether anything changed.

    Runs once per archive: when ``archive.json`` already records the pinned
    digest, nothing is downloaded. Any failure (the release is gone, the
    asset was swapped, Pillow is missing) writes no facts for this digest, so
    build_index.py does not list the look and the next run tries again. One
    broken look never stops the others from publishing.
    """
    name, sha256 = doc["name"], doc["sha256"]
    target = ROOT / "looks" / name
    facts_path = target / "archive.json"
    if facts_path.exists():
        try:
            known = json.loads(facts_path.read_text(encoding="utf-8"))
            if known.get("sha256") == sha256 and known.get("archive_url") == doc["archive_url"]:
                return False
        except ValueError:
            pass

    try:
        pack = validator.check_look_archive(doc)
    except validator.LookError as exc:
        print(f"WARNING: look {name}: {exc} — not listed", file=sys.stderr)
        return False

    preview_name = None
    manifest = pack["manifest"]
    if not manifest["preview"].lower().endswith(".svg"):
        # An SVG is a document that can carry script, so it gets no picture.
        try:
            png = render_preview(pack["preview_bytes"])
        except ImportError:
            print(f"WARNING: look {name}: Pillow is missing — not listed", file=sys.stderr)
            return False
        except Exception as exc:  # noqa: BLE001 - any decode failure means no picture
            print(f"WARNING: look {name}: the preview does not decode ({exc})", file=sys.stderr)
        else:
            preview_name = preview_file_name(sha256)
            target.mkdir(parents=True, exist_ok=True)
            (target / preview_name).write_bytes(png)
    if target.is_dir():
        for stale in target.glob("preview-*.png"):
            if stale.name != preview_name:
                stale.unlink()

    facts = {
        "sha256": sha256,
        "size_bytes": pack["size_bytes"],
        "archive_url": doc["archive_url"],
        "preview": preview_name,
        "manifest": manifest,
        "files": pack["files"],
    }
    write_if_changed(facts_path, dump(facts))
    return True


def main() -> int:
    registry_path = ROOT / "registry.json"
    registry: dict[str, dict] = {}
    if registry_path.exists():
        registry = json.loads(registry_path.read_text(encoding="utf-8"))

    changed = False
    validator = None
    for path in sorted((ROOT / "submissions").glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        name, kind = doc["name"], doc["kind"]

        if kind == "plugin":
            target = ROOT / "plugins" / name
            changed |= write_if_changed(target / "plugin.json", dump(doc["plugin_json"]))
            if doc.get("mcp_json"):
                changed |= write_if_changed(target / "mcp.json", dump(doc["mcp_json"]))
            # `skills/<name>/SKILL.md` — the layout the Agent Plugins standard
            # defines, so the published directory is a real package another
            # client can read, not a shape only this registry understands.
            # Names (and path traversal) are settled by validate.py before
            # anything reaches here.
            for skill in doc.get("skills") or []:
                changed |= write_if_changed(
                    target / "skills" / skill["name"] / "SKILL.md", skill["skill_md"]
                )
            if doc.get("usage_card"):
                changed |= write_if_changed(
                    target / EXTENSION_DIR / "usage-card.md", doc["usage_card"]
                )
        elif kind == "agent":
            changed |= write_if_changed(ROOT / "agents" / name / "agent.json", dump(doc["agent"]))
        elif kind == "look":
            if validator is None:
                validator = load_validator()
            changed |= expand_look(doc, validator)
        else:
            changed |= write_if_changed(ROOT / "skills" / name / "SKILL.md", doc["skill_md"])

        previous = registry.get(name) or {}
        entry = {"kind": kind, "publisher": doc["publisher"], "version": doc["version"]}
        if doc.get("publisher_id") is not None:
            entry["publisher_id"] = doc["publisher_id"]
        # published_at marks the release, so it only moves when the version
        # does — backfilling publisher_id must not restamp a live entry.
        if previous.get("version") == doc["version"] and previous.get("published_at"):
            entry["published_at"] = previous["published_at"]
        else:
            entry["published_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if previous != entry:
            registry[name] = entry
            changed = True

    changed |= write_if_changed(registry_path, dump(registry))
    print("expanded: changes written" if changed else "expanded: nothing to do")
    return 0


if __name__ == "__main__":
    sys.exit(main())
