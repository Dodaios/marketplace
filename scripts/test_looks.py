#!/usr/bin/env python3
"""Regression test: what a look submission and its archive may carry.

A look is a zip from a stranger's GitHub release that the app unpacks and
runs in a sandboxed frame. The registry merges it with no human review, so
the rules that decide it are pinned here, and they are the app's own rules
(dodaios/ui/looks/manifest.py, loader.py, install.py):

- a clean pack passes, at the zip's root or inside one top folder;
- the archive must hash to the submitted SHA-256;
- path tricks, links, encrypted entries, unknown file kinds, too many files,
  too much unpacked data and colliding names are refused;
- look.json must say what the submission says: id, version, license;
- "Jarvis" is refused in the name, the title and the pack's own name;
- the archive must be a GitHub release asset, and a download may only pass
  through GitHub's own hosts;
- an unchanged look is not downloaded again on every pull request;
- expand.py derives the facts and a re-encoded PNG preview once per archive;
- index.json carries exactly the fields the app's CommunityLookEntry reads,
  with the preview on this registry's Pages site;
- the storefront page says what a look can do and stays script-free.

No network: every download is a fake that returns an archive built in memory.

Usage:
    python scripts/test_looks.py
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
import stat
import struct
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent

# The app's install.ALLOWED_HOSTS and MAX_ARCHIVE_BYTES, copied so the feed is
# checked against what the app accepts, not against this registry's opinion.
APP_ALLOWED_HOSTS = frozenset(
    {
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "raw.githubusercontent.com",
        "dodaios.github.io",
    }
)
APP_MAX_ARCHIVE_BYTES = 25 * 1024 * 1024 + 2 * 1024 * 1024
# The fields CommunityLookEntry declares (dodaios/marketplace/community_source.py).
CONTRACT_KEYS = {
    "name", "title", "description", "publisher", "publisher_id", "featured", "version",
    "published_at", "license", "attribution", "source_url", "archive_url", "sha256",
    "size_bytes", "preview_url",
}


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def png(width: int, height: int) -> bytes:
    """A real RGB PNG, stdlib only."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    row = b"\x00" + bytes([200, 160, 40]) * width
    raw = zlib.compress(row * height)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


NAME = "aurora-core"
MANIFEST = {
    "format": "dodaios-look/1",
    "id": NAME,
    "name": "Aurora Core",
    "description": "A slow aurora that brightens while Dodaios talks.",
    "author": "Octo Cat",
    "version": "1.2.0",
    "license": "MIT AND CC-BY-4.0",
    "entry": "index.html",
    "preview": "preview.png",
    "attribution": "Core model by Example Studio, CC-BY-4.0",
}
PREVIEW = png(1024, 600)


def pack_files(manifest: dict | None = None, **extra: bytes) -> dict[str, bytes]:
    files = {
        "look.json": json.dumps(manifest or MANIFEST).encode(),
        "index.html": b"<!doctype html><canvas></canvas>"
        b"<script type='module' src='vendor/three.module.js'></script>",
        "preview.png": PREVIEW,
        "vendor/three.module.js": b"export const REVISION = '170';",
        "models/core.glb": b"glTF" + b"\x00" * 64,
    }
    files.update(extra)
    return files


def make_zip(files: dict[str, bytes], *, top: str = "", links: tuple[str, ...] = (),
             encrypted: tuple[str, ...] = ()) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel, body in files.items():
            info = zipfile.ZipInfo(top + rel)
            info.compress_type = zipfile.ZIP_DEFLATED
            if rel in links:
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, body)
    data = bytearray(buf.getvalue())
    for rel in encrypted:
        # zipfile cannot write an encrypted entry, so the flag is set by hand
        # in the central directory, which is where a reader looks for it.
        name = (top + rel).encode()
        at = data.find(b"PK\x01\x02")
        while at != -1:
            length = int.from_bytes(data[at + 28 : at + 30], "little")
            if bytes(data[at + 46 : at + 46 + length]) == name:
                data[at + 8] |= 0x1
            at = data.find(b"PK\x01\x02", at + 4)
    return bytes(data)


def submission(data: bytes, **overrides) -> dict:
    doc = {
        "kind": "look",
        "name": NAME,
        "publisher": "octocat",
        "publisher_id": 583231,
        "version": "1.2.0",
        "title": "Aurora Core",
        "description": "A slow aurora that brightens while Dodaios talks.",
        "license": "MIT AND CC-BY-4.0",
        "attribution": "Core model by Example Studio, CC-BY-4.0",
        "source_url": "https://github.com/octocat/aurora-core",
        "archive_url": "https://github.com/octocat/aurora-core/releases/download/v1.2.0/aurora-core.zip",
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    doc.update(overrides)
    return doc


class FakeFetch:
    """Stands in for validate.fetch_look_archive: serves one archive, counts calls."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.calls = 0

    def __call__(self, url: str) -> bytes:
        self.calls += 1
        return self.data


def problems(v, doc: dict, data: bytes, *, base_ref: str | None = None) -> list[str]:
    v.fetch_look_archive = FakeFetch(data)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{doc.get('name')}.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        errors = v.Errors()
        v.validate_file(path, errors, base_ref)
        return errors.items


def main() -> int:
    v = load("validate")
    failures: list[str] = []

    def check(what: str, condition: bool) -> None:
        if not condition:
            failures.append(what)

    def expect_ok(label: str, doc: dict, data: bytes) -> None:
        found = problems(v, doc, data)
        if found:
            failures.append(f"{label}: expected valid, got {found}")

    def expect_refused(label: str, doc: dict, data: bytes, needle: str) -> None:
        found = problems(v, doc, data)
        if not any(needle in item for item in found):
            failures.append(f"{label}: expected a refusal mentioning {needle!r}, got {found}")

    def refused_pack(label: str, data: bytes, needle: str) -> None:
        expect_refused(label, submission(data), data, needle)

    # --- a clean pack ---------------------------------------------------------
    clean = make_zip(pack_files())
    expect_ok("clean pack at the zip root", submission(clean), clean)
    in_folder = make_zip(pack_files(), top=f"{NAME}/")
    expect_ok("clean pack inside one top folder", submission(in_folder), in_folder)
    svg_manifest = {**MANIFEST, "preview": "preview.svg"}
    svg_pack = make_zip(pack_files(svg_manifest, **{"preview.svg": b"<svg/>"}))
    expect_ok("an SVG preview is allowed", submission(svg_pack), svg_pack)
    no_attribution = {k: val for k, val in submission(clean).items() if k != "attribution"}
    expect_ok("attribution is optional", no_attribution, clean)

    # --- the pinned digest ------------------------------------------------------
    expect_refused("sha mismatch", submission(clean, sha256="0" * 64), clean, "SHA-256 is")
    expect_refused("uppercase sha", submission(clean, sha256="A" * 64), clean, "64 lowercase hex")

    # --- what the zip may hold ------------------------------------------------
    for trick in ("../evil.js", "/abs.js", "..\\evil.js", "C:/evil.js", "a/./b.js",
                  "a/b/c/d/e/f/deep.js", ".hidden.js"):
        refused_pack(f"path trick {trick!r}", make_zip(pack_files(**{trick: b"x"})),
                     "file name that is not allowed")
    refused_pack("a link", make_zip(pack_files(), links=("vendor/three.module.js",)),
                 "contains a link")
    refused_pack("an encrypted entry", make_zip(pack_files(), encrypted=("index.html",)),
                 "encrypted")
    for bad in ("run.exe", "tool.py", "page.htm", "noext"):
        refused_pack(f"suffix {bad!r}", make_zip(pack_files(**{bad: b"x"})), "file kind of")
    many = pack_files(**{f"frames/f{i}.png": b"\x89PNG\r\n\x1a\n" for i in range(300)})
    refused_pack("too many files", make_zip(many), "at most 300 files")
    huge = pack_files(**{"models/huge.bin": b"\x00" * (25 * 1024 * 1024)})
    refused_pack("too large unpacked", make_zip(huge), "MB unpacked")
    refused_pack("the same name twice ignoring case",
                 make_zip(pack_files(**{"Index.HTML": b"x"})), "twice")
    refused_pack("a file that is also a folder",
                 make_zip(pack_files(**{"models.bin": b"x", "models.bin/a.glb": b"y"})),
                 "also a file")
    refused_pack("not a zip", b"PK-not-really", "not a valid zip")
    refused_pack("empty zip", make_zip({}), "empty")

    # --- look.json against the submission ----------------------------------
    def with_manifest(**changes) -> bytes:
        return make_zip(pack_files({**MANIFEST, **changes}))

    refused_pack("id mismatch", with_manifest(id="other-look"), "must equal the submission name")
    refused_pack("version mismatch", with_manifest(version="1.3.0"),
                 "must equal the submission version")
    refused_pack("license mismatch", with_manifest(license="MIT"),
                 "must equal the submission license")
    refused_pack("wrong format", with_manifest(format="dodaios-look/2"), "format")
    refused_pack("entry not html", with_manifest(entry="main.js"), "entry must be")
    refused_pack("entry missing", with_manifest(entry="start.html"), "no file 'start.html'")
    refused_pack("preview missing", with_manifest(preview="cover.png"), "no file 'cover.png'")
    fake_png = make_zip(pack_files(**{"preview.png": b"<svg onload=alert(1)>"}))
    refused_pack("preview that is not a PNG", fake_png, "is not a PNG picture")
    no_manifest = pack_files()
    del no_manifest["look.json"]
    refused_pack("no look.json", make_zip(no_manifest), "no look.json")
    refused_pack("look.json not JSON", make_zip(pack_files(**{"look.json": b"{nope"})),
                 "could not be read as JSON")
    refused_pack("http homepage", with_manifest(homepage="http://example.com"), "homepage")
    refused_pack("jarvis in the pack's name", with_manifest(name="Jarvis Glow"), "Jarvis")

    # --- the submission itself ----------------------------------------------
    expect_refused("jarvis in the name", submission(clean, name="jarvis-orb"), clean, "jarvis")
    expect_refused("Jarvis in the title", submission(clean, title="JARVIS core"), clean, "Jarvis")
    expect_refused("reserved id", submission(clean, name="orb"), clean, "reserved")
    expect_refused("built-in look", submission(clean, name="pulse-ring"), clean, "the app ships")
    expect_refused("dot in a look name", submission(clean, name="aurora.core"), clean,
                   "2 to 48 lowercase")
    expect_refused("long version part", submission(clean, version="10000.0.0"), clean,
                   "at most 4 digits")
    expect_refused("bad license", submission(clean, license="see LICENSE"), clean, "SPDX")
    expect_refused("long title", submission(clean, title="x" * 41), clean, "longer than 40")
    expect_refused("long attribution", submission(clean, attribution="x" * 201), clean,
                   "longer than 200")
    expect_refused("unknown field", submission(clean, preview_url="https://x"), clean,
                   "not a look submission field")
    expect_refused("http source", submission(clean, source_url="http://github.com/o/r"), clean,
                   "source_url")
    for url in (
        "https://example.com/aurora-core.zip",
        "http://github.com/octocat/aurora-core/releases/download/v1.2.0/aurora-core.zip",
        "https://github.com/octocat/aurora-core/archive/refs/heads/main.zip",
        "https://raw.githubusercontent.com/octocat/aurora-core/main/aurora-core.zip",
        "https://github.com/octocat/aurora-core/releases/download/v1.2.0/aurora-core.tar.gz",
        "https://github.com/octocat/aurora-core/releases/download/v1/../../x/aurora.zip",
    ):
        expect_refused(f"archive_url {url}", submission(clean, archive_url=url), clean,
                       "GitHub release asset")

    # A refused field stops the download: nothing is fetched for a bad submission.
    fetch = FakeFetch(clean)
    v.fetch_look_archive = fetch
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{NAME}.json"
        path.write_text(json.dumps(submission(clean, license="see LICENSE")), encoding="utf-8")
        v.validate_file(path, v.Errors(), None)
    check("a submission with field errors is not downloaded", fetch.calls == 0)

    # The download guard itself, without the network.
    check("github.com may serve the download", v._download_url_ok(
        "https://github.com/o/r/releases/download/v1/a.zip"))
    check("the release asset host may serve it", v._download_url_ok(
        "https://release-assets.githubusercontent.com/x?sig=1"))
    check("objects.githubusercontent.com may serve it", v._download_url_ok(
        "https://objects.githubusercontent.com/x"))
    check("another host may not", not v._download_url_ok("https://evil.example/x.zip"))
    check("plain http may not", not v._download_url_ok("http://github.com/x"))
    check("a look-alike host may not", not v._download_url_ok("https://github.com.evil.io/x"))
    check("credentials in the URL may not", not v._download_url_ok("https://u:p@github.com/x"))
    check("raw.githubusercontent.com is not a release host",
          not v._download_url_ok("https://raw.githubusercontent.com/x"))
    try:
        load("validate").fetch_look_archive("https://example.com/a.zip")
        check("fetch refuses a non-release URL before any request", False)
    except Exception as exc:  # noqa: BLE001
        check("fetch refuses a non-release URL with LookError", type(exc).__name__ == "LookError")

    # --- ownership, version, and no re-download of an unchanged look ---------
    doc = submission(clean)
    fetch = FakeFetch(clean)
    original = v.read_base_version
    try:
        v.read_base_version = lambda _p, _r: copy.deepcopy(doc)
        v.fetch_look_archive = fetch
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"{NAME}.json"
            path.write_text(json.dumps(doc), encoding="utf-8")
            errors = v.Errors()
            v.validate_file(path, errors, "origin/main")
        check(f"an unchanged look passes ({errors.items})", not errors.items)
        check("an unchanged look is not downloaded again", fetch.calls == 0)

        v.read_base_version = lambda _p, _r: {**doc, "publisher": "mallory", "publisher_id": 7}
        found = problems(v, submission(clean, version="1.2.1"), clean, base_ref="origin/main")
        check(f"another account cannot update a look ({found})",
              any("ownership rule" in item for item in found))
        v.read_base_version = lambda _p, _r: doc
        found = problems(v, submission(clean), clean, base_ref="origin/main")
        check("…and a re-sent identical look is unchanged, not refused", not found)
        newer = submission(clean, title="Aurora Core II")
        found = problems(v, newer, clean, base_ref="origin/main")
        check(f"an update must raise the version ({found})",
              any("version must increase" in item for item in found))
        v.read_base_version = lambda _p, _r: {**doc, "kind": "skill"}
        found = problems(v, submission(clean, version="1.2.1"), clean, base_ref="origin/main")
        check("a skill name cannot become a look", any("kind may not change" in i for i in found))
    finally:
        v.read_base_version = original

    v.FETCH_ARCHIVES = False
    fetch = FakeFetch(b"never read")
    v.fetch_look_archive = fetch
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{NAME}.json"
        path.write_text(json.dumps(submission(clean)), encoding="utf-8")
        errors = v.Errors()
        v.validate_file(path, errors, None)
    check("--no-fetch checks the fields without downloading", not errors.items and fetch.calls == 0)
    v.FETCH_ARCHIVES = True

    # --- expand.py + build_index.py: facts, preview, feed -------------------
    expand = load("expand")
    build_index = load("build_index")
    try:
        import PIL  # noqa: F401

        have_pillow = True
    except ImportError:
        have_pillow = False
        print("  (Pillow is not installed: the preview re-encode is skipped)")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "submissions").mkdir()
        doc = submission(clean)
        (root / "submissions" / f"{NAME}.json").write_text(json.dumps(doc), encoding="utf-8")
        svg_doc = submission(svg_pack, name="line-glow", title="Line Glow",
                             archive_url="https://github.com/octocat/glow/releases/download/v1.2.0/glow.zip")
        svg_files = pack_files({**svg_manifest, "id": "line-glow"}, **{"preview.svg": b"<svg/>"})
        svg_data = make_zip(svg_files)
        svg_doc["sha256"] = hashlib.sha256(svg_data).hexdigest()
        (root / "submissions" / "line-glow.json").write_text(json.dumps(svg_doc), encoding="utf-8")

        archives = {doc["archive_url"]: clean, svg_doc["archive_url"]: svg_data}
        calls: list[str] = []

        def fake_fetch(url: str) -> bytes:
            calls.append(url)
            return archives[url]

        validator = load("validate")
        validator.fetch_look_archive = fake_fetch
        expand.ROOT = root
        expand.load_validator = lambda: validator
        expand.main()
        facts = json.loads((root / "looks" / NAME / "archive.json").read_text(encoding="utf-8"))
        check("expand records the pinned digest", facts.get("sha256") == doc["sha256"])
        check("expand records the archive size", facts.get("size_bytes") == len(clean))
        check("expand lists the pack's files",
              {f["path"] for f in facts.get("files", [])} == set(pack_files()))
        check("the archive never enters the repository",
              not any(p.suffix == ".zip" for p in root.rglob("*")))
        preview_name = f"preview-{doc['sha256'][:12]}.png"
        if have_pillow:
            from PIL import Image

            check("expand names the preview after the digest", facts.get("preview") == preview_name)
            with Image.open(root / "looks" / NAME / preview_name) as picture:
                check("the preview is a PNG", picture.format == "PNG")
                check(f"the preview is at most 512 px ({picture.size})", max(picture.size) <= 512)
        svg_facts = json.loads((root / "looks" / "line-glow" / "archive.json").read_text("utf-8"))
        check("an SVG preview gets no picture", svg_facts.get("preview") is None)

        calls.clear()
        expand.main()
        check("expand does not download an archive it already checked", not calls)

        # A swapped release asset: the facts stay for the old digest only.
        swapped = make_zip(pack_files(**{"extra.txt": b"changed"}))
        archives[doc["archive_url"]] = swapped
        bumped = {**doc, "version": "1.2.1"}  # the submission still pins the old digest
        (root / "submissions" / f"{NAME}.json").write_text(json.dumps(bumped), encoding="utf-8")
        (root / "looks" / NAME / "archive.json").unlink()
        expand.main()
        check("a swapped asset writes no facts",
              not (root / "looks" / NAME / "archive.json").exists())
        archives[doc["archive_url"]] = clean
        (root / "submissions" / f"{NAME}.json").write_text(json.dumps(doc), encoding="utf-8")
        expand.main()

        registry = json.loads((root / "registry.json").read_text(encoding="utf-8"))
        check("the ledger records the look", registry.get(NAME, {}).get("kind") == "look")

        out = root / "_site"
        build_index.ROOT = root
        argv = sys.argv
        sys.argv = ["build_index.py", "--out", str(out)]
        try:
            build_index.main()
        finally:
            sys.argv = argv
        index = json.loads((out / "index.json").read_text(encoding="utf-8"))
        looks = {item["name"]: item for item in index.get("looks", [])}
        check("index.json has a looks section with both looks", set(looks) == {NAME, "line-glow"})
        item = looks.get(NAME, {})
        check(f"a look carries every contract field ({sorted(CONTRACT_KEYS - set(item))})",
              CONTRACT_KEYS <= set(item))
        check(f"nothing beyond the contract but the file listing ({set(item) - CONTRACT_KEYS})",
              set(item) - CONTRACT_KEYS <= {"files"})
        check("name", item.get("name") == NAME)
        check("description is text", isinstance(item.get("description"), str))
        check("featured is false", item.get("featured") is False)
        check("publisher_id is the account number", item.get("publisher_id") == 583231)
        check("sha256 is 64 lowercase hex", item.get("sha256") == doc["sha256"])
        check("size_bytes is the archive's size",
              item.get("size_bytes") == len(clean) and 0 < len(clean) <= APP_MAX_ARCHIVE_BYTES)
        check("archive_url is the release asset", item.get("archive_url") == doc["archive_url"])
        check("license and attribution travel",
              item.get("license") == doc["license"]
              and item.get("attribution") == doc["attribution"])
        for key in ("archive_url", "preview_url"):
            value = item.get(key)
            if value is None and key == "preview_url" and not have_pillow:
                continue
            parts = urlsplit(str(value))
            check(f"{key} is https on a host the app allows ({value})",
                  parts.scheme == "https" and parts.hostname in APP_ALLOWED_HOSTS)
        if have_pillow:
            expected = f"https://dodaios.github.io/marketplace/looks/{NAME}/{preview_name}"
            check(f"preview_url is on this registry's Pages site ({item.get('preview_url')})",
                  item.get("preview_url") == expected)
            check("the preview is in the Pages output",
                  (out / "looks" / NAME / preview_name).is_file())
        check("the SVG look is listed without a preview", looks.get("line-glow", {}).get(
            "preview_url", "missing") is None)

        page = (out / "looks" / NAME / "index.html").read_text(encoding="utf-8")
        check("the look has its own page", "Aurora Core" in page)
        check("the page says a look can only draw", "A look can only draw" in page)
        check("the page says what a look gets",
              "nothing but the voice state and the volume" in page)
        check("the page shows the digest", doc["sha256"] in page)
        check("the page offers no command-line install", "marketplace install" not in page)
        check("the page lists the pack's files", "vendor/three.module.js" in page)
        if have_pillow:
            check("the page shows the preview", f'src="{expected}"' in page)
        home = (out / "index.html").read_text(encoding="utf-8")
        check("the store has a Looks group", 'data-kind-group="look"' in home)
        check("the store links the look", f'href="looks/{NAME}/"' in home)

    # --- the storefront escapes a hostile look -----------------------------
    site = load("build_site")
    hostile = '<img src=x onerror=alert(1)>"\'</script><script>alert(2)</script>'
    feed = {
        "looks": [
            {
                "name": "evil-look",
                "title": f"Evil {hostile}",
                "description": hostile,
                "publisher": "octocat",
                "license": hostile,
                "attribution": hostile,
                "source_url": "javascript:alert(3)",
                "archive_url": "javascript:alert(4)",
                "sha256": "f" * 64,
                "size_bytes": 10,
                "preview_url": "https://evil.example/p.png",
                "files": [{"path": f"x{hostile}.js", "size": 1, "text": None}],
            }
        ]
    }
    with tempfile.TemporaryDirectory() as tmp:
        site.build(feed, Path(tmp))
        page = (Path(tmp) / "looks" / "evil-look" / "index.html").read_text(encoding="utf-8")
        check("no script from a look reaches the page", "<script>alert" not in page)
        check("no injected image tag", "<img src=x" not in page)
        check("a javascript: link is never made", "javascript:" not in page)
        check("a preview from another host is not shown", "evil.example" not in page)

    if failures:
        print("FAIL")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("OK — look rules hold")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("GITHUB_REPOSITORY", "Dodaios/marketplace")
    sys.exit(main())
