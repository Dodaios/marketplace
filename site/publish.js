/*
 * Publishing from the website.
 *
 * A static page cannot hold a login, so it does not try to. It builds the
 * submission in the browser, then hands it to GitHub as an issue that is
 * already filled in. GitHub asks the visitor to sign in if they are not, they
 * press Create, and the registry's intake takes the publisher from the account
 * that opened the issue. This page then watches the feed until the entry is
 * live, and the issue for what the checks said.
 *
 * The first half of this file is pure (no DOM): what a dropped file is, what
 * the submission looks like, what is wrong with it, and the address that
 * carries it. `scripts/test_publish_flow.py` runs that half in Node and feeds
 * the result to the real intake and validator. The second half wires the page.
 */
(function (root) {
  "use strict";

  var KINDS = ["skill", "plugin", "agent", "look"];
  var NAME_RE = /^[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?$/;
  var SEMVER_RE = /^\d+\.\d+\.\d+$/;
  var TITLE_TAG = "[publish]";
  var BODY_MARKER = "<!-- jarvis-marketplace-submission -->";
  /* GitHub answers "URI too long" a little past 8,000 characters. */
  var MAX_PREFILL_URL = 7600;
  var DEFAULT_LIMITS = { max_description_chars: 500, max_skill_bytes: 65536 };

  // ------------------------------------------------------------------ text

  function byteLength(text) {
    if (typeof TextEncoder !== "undefined") return new TextEncoder().encode(text).length;
    return unescape(encodeURIComponent(text)).length;
  }

  /** "My Skill!" becomes "my-skill": what a name may look like. */
  function slug(text) {
    return String(text || "")
      .toLowerCase()
      .replace(/[^a-z0-9.]+/g, "-")
      .replace(/-{2,}/g, "-")
      .replace(/\.{2,}/g, ".")
      .replace(/^[-.]+|[-.]+$/g, "")
      .slice(0, 64)
      .replace(/[-.]+$/, "");
  }

  /** "three-bullet-brief" becomes "Three Bullet Brief". */
  function titleFromName(name) {
    return String(name || "")
      .split(/[-._\s]+/)
      .filter(Boolean)
      .map(function (word) {
        return word.charAt(0).toUpperCase() + word.slice(1);
      })
      .join(" ");
  }

  var FRONTMATTER_RE = /^﻿?---[ \t]*\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)/;

  function splitFrontmatter(text) {
    var match = FRONTMATTER_RE.exec(text);
    if (!match) return { block: null, body: text };
    return { block: match[1], body: text.slice(match[0].length) };
  }

  /**
   * One top-level value of a YAML block, read without a YAML parser: a plain
   * or quoted value on the key's line, or a block scalar (| or >) whose text
   * is the indented lines that follow.
   */
  function frontmatterValue(block, key) {
    if (!block) return "";
    var lines = block.split(/\r?\n/);
    var start = new RegExp("^" + key + "[ \\t]*:[ \\t]*(.*)$");
    for (var i = 0; i < lines.length; i++) {
      var hit = start.exec(lines[i]);
      if (!hit) continue;
      var value = hit[1].trim();
      var parts = /^[|>][+-]?\d*$/.test(value) || !value ? [] : [value];
      for (var j = i + 1; j < lines.length; j++) {
        if (lines[j].trim() && !/^[ \t]/.test(lines[j])) break;
        parts.push(lines[j].trim());
      }
      var text = parts.filter(Boolean).join(" ").trim();
      if (text.length >= 2 && text[0] === text[text.length - 1] && /["']/.test(text[0])) {
        text = text.slice(1, -1);
      }
      return text;
    }
    return "";
  }

  /** What a SKILL.md says about itself, for the form to start from. */
  function readSkill(text) {
    var parts = splitFrontmatter(text);
    var name = slug(frontmatterValue(parts.block, "name"));
    var heading = /^#\s+(.+?)\s*#*\s*$/m.exec(parts.body);
    var version = frontmatterValue(parts.block, "version");
    if (!SEMVER_RE.test(version)) {
      var nested = parts.block ? /^[ \t]+version:[ \t]*["']?(\d+\.\d+\.\d+)/m.exec(parts.block) : null;
      version = nested ? nested[1] : "";
    }
    return {
      name: name,
      title: heading ? heading[1].replace(/[*_`]/g, "").trim() : titleFromName(name),
      description: frontmatterValue(parts.block, "description"),
      version: version,
    };
  }

  /** A double-quoted YAML scalar on one line. */
  function yamlQuote(text) {
    return '"' + String(text).replace(/\s+/g, " ").trim().replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"';
  }

  /**
   * The SKILL.md as it will be published: its frontmatter names the entry the
   * way the form does, and states a description. The registry requires both,
   * and somebody who dropped a plain Markdown file should not have to know.
   */
  function normalizeSkill(text, name, description) {
    var parts = splitFrontmatter(String(text || "").replace(/\r\n/g, "\n"));
    if (parts.block === null) {
      return "---\nname: " + name + "\ndescription: " + yamlQuote(description) + "\n---\n\n" + parts.body.replace(/^\n+/, "");
    }
    var lines = parts.block.split("\n");
    var hasName = false;
    var hasDescription = false;
    for (var i = 0; i < lines.length; i++) {
      if (/^name[ \t]*:/.test(lines[i])) {
        lines[i] = "name: " + name;
        hasName = true;
      } else if (/^description[ \t]*:/.test(lines[i])) {
        hasDescription = true;
      }
    }
    if (!hasName) lines.unshift("name: " + name);
    if (!hasDescription) lines.push("description: " + yamlQuote(description));
    return "---\n" + lines.join("\n") + "\n---\n" + parts.body;
  }

  // ----------------------------------------------------------------- files

  function baseName(path) {
    return String(path).split(/[\\/]/).pop().toLowerCase();
  }

  function parseJson(text) {
    try {
      var doc = JSON.parse(text);
      return doc && typeof doc === "object" && !Array.isArray(doc) ? doc : null;
    } catch (err) {
      return null;
    }
  }

  function extensionOf(pluginJson) {
    var extensions = pluginJson && pluginJson.extensions;
    if (!extensions || typeof extensions !== "object") return {};
    return extensions["io.github.dodaios"] || extensions["io.github.personaljarvis"] || {};
  }

  /**
   * Turn what was dropped into a draft: `{kind, fields, parts, problem}`.
   *
   * `files` is `[{path, text}]`. A JSON file that already is a submission (the
   * app exports agents that way) is taken whole. Otherwise the file names say
   * what it is: a Markdown file is a skill, a plugin.json with its neighbours
   * is a plugin, a bare agent template is an agent.
   */
  function draftFromFiles(files, preferredKind) {
    /* `files` on the draft is what goes into the package, by path. */
    var draft = { kind: preferredKind || "skill", fields: {}, parts: {}, files: [], problem: null };
    var byName = {};
    files.forEach(function (file) {
      byName[baseName(file.path)] = file;
    });

    var ready = null;
    files.forEach(function (file) {
      if (ready || !/\.json$/i.test(file.path)) return;
      var doc = parseJson(file.text);
      if (doc && KINDS.indexOf(doc.kind) !== -1 && typeof doc.name === "string") {
        ready = doc;
        draft.files = [file.path];
      }
    });
    if (ready) {
      draft.kind = ready.kind;
      draft.parts = ready;
      draft.fields = {
        name: ready.name,
        version: ready.version || "",
        title: ready.title || titleFromName(ready.name),
        description: ready.description || (ready.plugin_json && ready.plugin_json.description) || "",
        categories: Array.isArray(ready.categories) ? ready.categories.join(", ") : "",
      };
      if (ready.kind === "look") {
        /* A finished look submission: its digest was computed elsewhere. */
        draft.parts = {
          look: {
            manifest: null,
            sha256: ready.sha256 || "",
            size: null,
            license: ready.license || "",
            attribution: ready.attribution || "",
          },
        };
        draft.fields.archive_url = ready.archive_url || "";
      }
      return draft;
    }

    if (byName["look.json"]) {
      /* A look's folder: it travels as a zip on a release, not as files. */
      draft.kind = "look";
      draft.problem = LOOK_HOW_TO;
      return draft;
    }

    if (byName["plugin.json"]) {
      var plugin = parseJson(byName["plugin.json"].text);
      if (!plugin) {
        draft.problem = "plugin.json is not valid JSON.";
        return draft;
      }
      draft.kind = "plugin";
      draft.parts = { plugin_json: plugin };
      draft.files = [byName["plugin.json"].path];
      if (byName["mcp.json"]) {
        var mcp = parseJson(byName["mcp.json"].text);
        if (!mcp) {
          draft.problem = "mcp.json is not valid JSON.";
          return draft;
        }
        draft.parts.mcp_json = mcp;
        draft.files.push(byName["mcp.json"].path);
      }
      if (byName["usage-card.md"]) {
        draft.parts.usage_card = byName["usage-card.md"].text;
        draft.files.push(byName["usage-card.md"].path);
      }
      var bundled = [];
      files.forEach(function (file) {
        var hit = /(?:^|[\\/])skills[\\/]([^\\/]+)[\\/]skill\.md$/i.exec(file.path);
        if (hit) {
          bundled.push({ name: hit[1], skill_md: file.text });
          draft.files.push(file.path);
        }
      });
      if (bundled.length) draft.parts.skills = bundled;
      draft.fields = {
        name: slug(plugin.name),
        version: SEMVER_RE.test(plugin.version) ? plugin.version : "",
        title: extensionOf(plugin).display_name || titleFromName(plugin.name),
        description: plugin.description || "",
        categories: "",
      };
      return draft;
    }

    var markdown = null;
    files.forEach(function (file) {
      if (!markdown && /\.(md|markdown|txt)$/i.test(file.path)) markdown = file;
    });
    if (byName["skill.md"]) markdown = byName["skill.md"];
    if (markdown) {
      var skill = readSkill(markdown.text);
      draft.kind = "skill";
      draft.parts = { skill_md: markdown.text };
      draft.files = [markdown.path];
      draft.fields = {
        name: skill.name || slug(baseName(markdown.path).replace(/\.[^.]+$/, "").replace(/^skill$/, "")),
        version: skill.version,
        title: skill.title,
        description: skill.description,
        categories: "",
      };
      return draft;
    }

    var template = null;
    files.forEach(function (file) {
      if (template || !/\.json$/i.test(file.path)) return;
      var doc = parseJson(file.text);
      if (doc && typeof doc.instructions === "string" && typeof doc.name === "string") {
        template = doc;
        draft.files = [file.path];
      }
    });
    if (template) {
      draft.kind = "agent";
      draft.parts = { agent: template };
      draft.fields = {
        name: slug(template.name),
        version: "",
        title: template.name,
        description: template.title || "",
        categories: "",
      };
      return draft;
    }

    draft.problem =
      "This cannot be published. Drop a SKILL.md, a plugin folder with a plugin.json, an agent file exported from the app, or a look's zip.";
    return draft;
  }

  // ----------------------------------------------------------------- looks
  //
  // A look is published as a zip attached to a GitHub release. This page
  // cannot upload the release or download it (release downloads send no CORS
  // headers), so it reads the zip the person dropped: the same bytes they
  // attach. It runs the registry's pack checks as early feedback, computes
  // the SHA-256 and asks for the release link. scripts/validate.py downloads
  // the archive again and stays the authority. Keep these rules in sync with
  // the look section of validate.py (and so with dodaios/ui/looks/ in the app).

  var LOOK_FORMAT = "dodaios-look/1";
  var LOOK_ID_RE = /^[a-z0-9][a-z0-9-]{1,47}$/;
  var LOOK_VERSION_RE = /^\d{1,4}\.\d{1,4}\.\d{1,4}$/;
  var LOOK_LICENSE_RE = /^[A-Za-z0-9.+-]{2,40}(?: (?:AND|OR) [A-Za-z0-9.+-]{2,40}){0,3}$/;
  var LOOK_REL_PATH_RE = /^(?:[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\/){0,4}[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$/;
  var LOOK_ARCHIVE_URL_RE =
    /^https:\/\/github\.com\/[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\/[A-Za-z0-9._-]{1,100}\/releases\/download\/[A-Za-z0-9._+-]{1,128}\/[A-Za-z0-9._-]{1,128}\.zip$/;
  var LOOK_SUFFIXES = [
    ".html", ".js", ".mjs", ".css", ".json", ".txt", ".md", ".png", ".jpg", ".jpeg", ".webp",
    ".svg", ".glb", ".gltf", ".bin", ".ktx2", ".hdr", ".woff2", ".wasm",
  ];
  var LOOK_PREVIEW_SUFFIXES = [".png", ".jpg", ".jpeg", ".webp", ".svg"];
  var LOOK_RESERVED = ["default", "none", "orb", "pulse-ring"];
  var LOOK_LIMITS = {
    archive: 25 * 1024 * 1024 + 2 * 1024 * 1024,
    pack: 25 * 1024 * 1024,
    files: 300,
    manifest: 16 * 1024,
    title: 40,
    description: 200,
    attribution: 200,
  };
  var TRADEMARK_RE = /jarvis/i;
  var LOOK_HOW_TO =
    "A look is published as a zip attached to a GitHub release. Zip the look's folder, " +
    "create a release on your GitHub repository, attach the zip to it, then drop that same zip here.";

  function lookPathOk(path) {
    if (typeof path !== "string" || !LOOK_REL_PATH_RE.test(path)) return false;
    return path.split("/").every(function (part) {
      return part !== "." && part !== "..";
    });
  }

  function lookSuffix(path) {
    var dot = path.lastIndexOf(".");
    return dot > path.lastIndexOf("/") ? path.slice(dot).toLowerCase() : "";
  }

  function u16(bytes, at) {
    return bytes[at] | (bytes[at + 1] << 8);
  }

  function u32(bytes, at) {
    return (bytes[at] | (bytes[at + 1] << 8) | (bytes[at + 2] << 16) | (bytes[at + 3] << 24)) >>> 0;
  }

  /**
   * The zip's central directory as `[{name, method, flags, size, compressedSize,
   * offset, mode, isDir}]`. Throws an Error whose message a person can read.
   */
  function zipDirectory(bytes) {
    var end = -1;
    for (var at = bytes.length - 22; at >= Math.max(0, bytes.length - 65557); at--) {
      if (u32(bytes, at) === 0x06054b50) {
        end = at;
        break;
      }
    }
    if (end < 0) throw new Error("This is not a zip file.");
    var count = u16(bytes, end + 10);
    var offset = u32(bytes, end + 16);
    if (count === 0xffff || offset === 0xffffffff) throw new Error("This zip is too large for a look.");
    var decoder = new TextDecoder("utf-8");
    var entries = [];
    for (var i = 0; i < count; i++) {
      if (offset + 46 > bytes.length || u32(bytes, offset) !== 0x02014b50) {
        throw new Error("This zip file is damaged.");
      }
      var nameLength = u16(bytes, offset + 28);
      var name = decoder.decode(bytes.subarray(offset + 46, offset + 46 + nameLength));
      entries.push({
        name: name,
        flags: u16(bytes, offset + 8),
        method: u16(bytes, offset + 10),
        compressedSize: u32(bytes, offset + 20),
        size: u32(bytes, offset + 24),
        mode: (u32(bytes, offset + 38) >>> 16) & 0xffff,
        offset: u32(bytes, offset + 42),
        isDir: name.charAt(name.length - 1) === "/",
      });
      offset += 46 + nameLength + u16(bytes, offset + 30) + u16(bytes, offset + 32);
    }
    return entries;
  }

  /** One entry's bytes: stored as is, deflated through DecompressionStream. */
  function zipEntryData(bytes, entry) {
    var at = entry.offset;
    if (u32(bytes, at) !== 0x04034b50) return Promise.reject(new Error("This zip file is damaged."));
    var start = at + 30 + u16(bytes, at + 26) + u16(bytes, at + 28);
    var raw = bytes.subarray(start, start + entry.compressedSize);
    if (entry.method === 0) return Promise.resolve(raw);
    if (entry.method !== 8 || typeof DecompressionStream === "undefined") {
      return Promise.reject(new Error("This browser cannot read " + entry.name + " in the zip."));
    }
    var stream = new Blob([raw]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
    return new Response(stream).arrayBuffer().then(function (buffer) {
      if (buffer.byteLength !== entry.size) throw new Error("The zip's sizes do not match its contents.");
      return new Uint8Array(buffer);
    });
  }

  /**
   * The pack's files by path, after the registry's archive rules
   * (validate.py _zip_members). Returns `{problem}` or `{members}`.
   */
  function lookMembers(entries) {
    var files = entries.filter(function (entry) {
      return !entry.isDir;
    });
    if (!files.length) return { problem: "The zip is empty." };
    if (files.length > LOOK_LIMITS.files) return { problem: "A look may hold at most " + LOOK_LIMITS.files + " files." };
    var names = files.map(function (entry) {
      return entry.name.replace(/\\/g, "/");
    });
    var tops = {};
    names.forEach(function (name) {
      tops[name.split("/")[0]] = true;
    });
    var strip = "";
    if (Object.keys(tops).length === 1 && names.every(function (name) {
      return name.indexOf("/") !== -1;
    })) {
      strip = Object.keys(tops)[0] + "/";
    }
    var members = {};
    var folded = {};
    var total = 0;
    for (var i = 0; i < files.length; i++) {
      var entry = files[i];
      var name = names[i];
      if (entry.mode && (entry.mode & 0xf000) === 0xa000) return { problem: "The zip contains a link: " + name + "." };
      if (entry.flags & 0x1) return { problem: "The zip is encrypted." };
      var rel = strip ? name.slice(strip.length) : name;
      if (!lookPathOk(rel)) return { problem: "The zip contains a file name that is not allowed: " + name + "." };
      if (LOOK_SUFFIXES.indexOf(lookSuffix(rel)) === -1) {
        return { problem: "A look cannot hold " + rel + ": that kind of file is not allowed." };
      }
      if (folded[rel.toLowerCase()]) return { problem: "The zip holds " + rel + " twice." };
      folded[rel.toLowerCase()] = true;
      total += entry.size;
      if (total > LOOK_LIMITS.pack) return { problem: "A look may be at most 25 MB unpacked." };
      members[rel] = entry;
    }
    var clash = Object.keys(members).filter(function (rel) {
      var parts = rel.toLowerCase().split("/");
      for (var depth = 1; depth < parts.length; depth++) {
        if (folded[parts.slice(0, depth).join("/")]) return true;
      }
      return false;
    });
    if (clash.length) return { problem: clash[0] + " sits inside a path that is also a file." };
    return { members: members };
  }

  function plainText(value, label, limit, required) {
    if (value === undefined || value === null) value = "";
    if (typeof value !== "string") return { problem: "look.json: " + label + " must be text." };
    value = value.trim();
    if (required && !value) return { problem: "look.json needs a " + label + "." };
    if (value.length > limit) return { problem: "look.json: " + label + " is longer than " + limit + " characters." };
    if (/[\u0000-\u001f]/.test(value)) return { problem: "look.json: " + label + " contains control characters." };
    return { value: value };
  }

  /** look.json, checked like the app checks it. Returns `{problem}` or `{manifest}`. */
  function parseLookManifest(doc) {
    if (!doc || typeof doc !== "object" || Array.isArray(doc)) return { problem: "look.json must hold one JSON object." };
    if (doc.format !== LOOK_FORMAT) return { problem: 'look.json must say "format": "' + LOOK_FORMAT + '".' };
    if (typeof doc.id !== "string" || !LOOK_ID_RE.test(doc.id)) {
      return { problem: "look.json: id must be 2 to 48 lowercase letters, digits or hyphens." };
    }
    var out = { id: doc.id };
    var texts = [
      ["name", "name", LOOK_LIMITS.title, true],
      ["description", "description", LOOK_LIMITS.description, false],
      ["author", "author", 60, true],
      ["version", "version", 14, true],
      ["license", "license", 120, true],
      ["attribution", "attribution", LOOK_LIMITS.attribution, false],
      ["homepage", "homepage", 200, false],
    ];
    for (var i = 0; i < texts.length; i++) {
      var read = plainText(doc[texts[i][0]], texts[i][1], texts[i][2], texts[i][3]);
      if (read.problem) return read;
      out[texts[i][0]] = read.value;
    }
    if (!LOOK_VERSION_RE.test(out.version)) return { problem: "look.json: version must look like 1.0.0." };
    if (!LOOK_LICENSE_RE.test(out.license)) {
      return { problem: "look.json: license must be an SPDX id such as Apache-2.0." };
    }
    if (out.homepage && out.homepage.indexOf("https://") !== 0) {
      return { problem: "look.json: homepage must start with https://." };
    }
    out.entry = doc.entry === undefined ? "index.html" : doc.entry;
    if (!lookPathOk(out.entry) || lookSuffix(out.entry) !== ".html") {
      return { problem: "look.json: entry must be the path of an .html file in the pack." };
    }
    out.preview = doc.preview;
    if (!lookPathOk(out.preview) || LOOK_PREVIEW_SUFFIXES.indexOf(lookSuffix(out.preview)) === -1) {
      return { problem: "look.json: preview must be the path of a .png, .jpg, .webp or .svg file." };
    }
    return { manifest: out };
  }

  function previewMatches(suffix, bytes) {
    var starts = function (list) {
      return list.every(function (value, index) {
        return bytes[index] === value;
      });
    };
    if (suffix === ".png") return starts([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
    if (suffix === ".jpg" || suffix === ".jpeg") return starts([0xff, 0xd8, 0xff]);
    if (suffix === ".webp") {
      return starts([0x52, 0x49, 0x46, 0x46]) && bytes[8] === 0x57 && bytes[9] === 0x45 && bytes[10] === 0x42 && bytes[11] === 0x50;
    }
    return true;
  }

  function hex(buffer) {
    return Array.prototype.map.call(new Uint8Array(buffer), function (value) {
      return ("0" + value.toString(16)).slice(-2);
    }).join("");
  }

  /**
   * Read a dropped look zip into a draft. Resolves (never rejects) with
   * `{kind: "look", fields, parts: {look}, files, problem}`.
   */
  function readLookZip(bytes) {
    var draft = { kind: "look", fields: {}, parts: {}, files: [], problem: null };
    var fail = function (message) {
      draft.problem = message;
      return draft;
    };
    if (bytes.length > LOOK_LIMITS.archive) return Promise.resolve(fail("A look's zip may be at most 27 MB."));
    var members;
    try {
      var checked = lookMembers(zipDirectory(bytes));
      if (checked.problem) return Promise.resolve(fail(checked.problem));
      members = checked.members;
    } catch (err) {
      return Promise.resolve(fail(err.message));
    }
    if (!members["look.json"]) return Promise.resolve(fail("The zip has no look.json. " + LOOK_HOW_TO));
    if (members["look.json"].size > LOOK_LIMITS.manifest) return Promise.resolve(fail("look.json is too large."));
    var manifest;
    return zipEntryData(bytes, members["look.json"])
      .then(function (raw) {
        var doc = parseJson(new TextDecoder("utf-8").decode(raw).replace(/^﻿/, ""));
        if (!doc) throw new Error("look.json could not be read as JSON.");
        var parsed = parseLookManifest(doc);
        if (parsed.problem) throw new Error(parsed.problem);
        manifest = parsed.manifest;
        if (!members[manifest.entry]) throw new Error("The pack has no file " + manifest.entry + ".");
        if (!members[manifest.preview]) throw new Error("The pack has no file " + manifest.preview + ".");
        return zipEntryData(bytes, members[manifest.preview]);
      })
      .then(function (preview) {
        var suffix = lookSuffix(manifest.preview);
        if (!previewMatches(suffix, preview)) {
          throw new Error("The preview " + manifest.preview + " is not a " + suffix.slice(1).toUpperCase() + " picture.");
        }
        return crypto.subtle.digest("SHA-256", bytes);
      })
      .then(function (digest) {
        draft.files = Object.keys(members);
        draft.parts = {
          look: {
            manifest: manifest,
            sha256: hex(digest),
            size: bytes.length,
            license: manifest.license,
            attribution: manifest.attribution,
          },
        };
        draft.fields = {
          name: manifest.id,
          version: manifest.version,
          title: manifest.name,
          description: manifest.description,
          categories: "",
          archive_url: "",
        };
        return draft;
      })
      .catch(function (err) {
        return fail(err && err.message ? err.message : "The zip could not be read.");
      });
  }

  /** The repository a release asset belongs to: what the look's source is. */
  function repositoryOf(archiveUrl) {
    var hit = /^(https:\/\/github\.com\/[^/]+\/[^/]+)\/releases\/download\//.exec(String(archiveUrl || ""));
    return hit ? hit[1] : "";
  }

  /** Up to two letters that stand for a title: "Three Bullet Brief" is "TB". */
  function monogram(title) {
    var words = String(title || "").trim().split(/[\s._-]+/).filter(Boolean);
    if (!words.length) return "?";
    return (words[0].charAt(0) + (words.length > 1 ? words[1].charAt(0) : "")).toUpperCase();
  }

  /** The hue that belongs to a name: the same number the store draws it with. */
  function nameHue(name) {
    var value = 0;
    Array.from(String(name || "")).forEach(function (char) {
      value = (value * 31 + char.codePointAt(0)) % 360;
    });
    return Math.round(value / 30) * 30;
  }

  /**
   * The version to publish `name` as, when the person did not choose one:
   * what the file says, or 1.0.0, or one patch above what is published.
   */
  function nextVersion(fromFile, taken) {
    var base = SEMVER_RE.test(fromFile || "") ? fromFile : "1.0.0";
    if (!taken || !SEMVER_RE.test(taken.version || "") || versionAbove(base, taken.version)) return base;
    var parts = versionTuple(taken.version);
    return parts[0] + "." + parts[1] + "." + (parts[2] + 1);
  }

  // ------------------------------------------------------------ submission

  function categoriesOf(text) {
    var seen = [];
    String(text || "")
      .split(",")
      .forEach(function (item) {
        var value = slug(item);
        if (value && seen.indexOf(value) === -1) seen.push(value);
      });
    return seen.slice(0, 6);
  }

  /** The submission the registry reads, from the form and the dropped parts. */
  function buildSubmission(kind, fields, parts) {
    var name = String(fields.name || "").trim();
    var version = String(fields.version || "").trim();
    var title = String(fields.title || "").trim();
    var description = String(fields.description || "").replace(/\s+/g, " ").trim();
    var out = { kind: kind, name: name, version: version };
    if (kind === "plugin") {
      var manifest = JSON.parse(JSON.stringify(parts.plugin_json || {}));
      manifest.name = name;
      manifest.version = version;
      if (description) manifest.description = description;
      out.plugin_json = manifest;
      if (parts.mcp_json) out.mcp_json = parts.mcp_json;
      if (parts.usage_card) out.usage_card = parts.usage_card;
      if (parts.skills && parts.skills.length) out.skills = parts.skills;
      return out;
    }
    out.title = title;
    out.description = description;
    if (kind === "look") {
      var look = parts.look || {};
      var archive = String(fields.archive_url || "").trim();
      out.license = look.license || "";
      if (look.attribution) out.attribution = look.attribution;
      out.source_url = repositoryOf(archive);
      out.archive_url = archive;
      out.sha256 = look.sha256 || "";
      return out;
    }
    out.categories = categoriesOf(fields.categories);
    if (kind === "agent") {
      out.agent = parts.agent || {};
      return out;
    }
    out.skill_md = normalizeSkill(parts.skill_md || "", name, description);
    if (parts.flavor) out.flavor = parts.flavor;
    if (parts.compatible_agents) out.compatible_agents = parts.compatible_agents;
    return out;
  }

  function versionTuple(version) {
    return String(version)
      .split(".")
      .map(function (part) {
        return parseInt(part, 10) || 0;
      });
  }

  function versionAbove(candidate, base) {
    var a = versionTuple(candidate);
    var b = versionTuple(base);
    for (var i = 0; i < 3; i++) {
      if ((a[i] || 0) !== (b[i] || 0)) return (a[i] || 0) > (b[i] || 0);
    }
    return false;
  }

  /** Every published entry of the feed as `{name: {kind, version, publisher}}`. */
  function publishedNames(index) {
    var out = {};
    [["plugin", "plugins"], ["skill", "skills"], ["agent", "agents"], ["look", "looks"]].forEach(function (pair) {
      ((index && index[pair[1]]) || []).forEach(function (entry) {
        if (entry && typeof entry.name === "string") {
          out[entry.name] = { kind: pair[0], version: entry.version || "", publisher: entry.publisher || "" };
        }
      });
    });
    return out;
  }

  /**
   * What would stop this submission, as `[{field, message}]`.
   *
   * The registry's validator stays the authority and runs again on GitHub.
   * This catches what a person can fix before they leave the page.
   */
  function problemsOf(submission, context, parts) {
    var rules = (context && context.rules) || {};
    var limits = rules.limits || DEFAULT_LIMITS;
    var reserved = rules.reserved || {};
    var published = (context && context.published) || {};
    var problems = [];
    var add = function (field, message) {
      problems.push({ field: field, message: message });
    };
    var name = submission.name;
    var kind = submission.kind;

    if (!name) add("name", "Give it an ID.");
    else if (!NAME_RE.test(name) || name.indexOf("--") !== -1 || name.indexOf("..") !== -1) {
      add("name", "Use lowercase letters, digits, hyphens and dots only, starting and ending with a letter or digit.");
    } else if (kind === "plugin" && (reserved.plugin_ids || []).indexOf(name) !== -1) {
      add("name", "This ID belongs to a plugin that ships with the app. Pick another.");
    } else if (kind === "skill" && (reserved.skill_names || []).indexOf(name) !== -1) {
      add("name", "This ID belongs to a skill that ships with the app. Pick another.");
    } else if (kind === "look" && !LOOK_ID_RE.test(name)) {
      add("name", "A look's ID is 2 to 48 lowercase letters, digits and hyphens.");
    } else if (kind === "look" && (reserved.look_ids || LOOK_RESERVED).indexOf(name) !== -1) {
      add("name", "This ID belongs to the app's own looks. Pick another.");
    } else if (kind === "look" && TRADEMARK_RE.test(name)) {
      add("name", "Pick an ID without “Jarvis”: it is another company's trademark.");
    }

    if (!SEMVER_RE.test(submission.version || "")) add("version", "Use three numbers, like 1.0.0.");
    else if (kind === "look" && !LOOK_VERSION_RE.test(submission.version)) {
      add("version", "Use three numbers of at most four digits, like 1.0.0.");
    }

    if (kind === "look") {
      var look = (parts && parts.look) || {};
      var manifest = look.manifest;
      /* The zip is what gets checked, so the form may not disagree with it. */
      if (manifest && name && name !== manifest.id) {
        add("name", "look.json says the ID is " + manifest.id + ". Change it there and zip again.");
      }
      if (manifest && submission.version && submission.version !== manifest.version) {
        add("version", "look.json says the version is " + manifest.version + ". Change it there and zip again.");
      }
      var lookTitle = String(submission.title || "").trim();
      if (lookTitle.length > LOOK_LIMITS.title) add("title", "Keep the name under " + LOOK_LIMITS.title + " characters.");
      else if (TRADEMARK_RE.test(lookTitle)) add("title", "Pick a name without “Jarvis”: it is another company's trademark.");
      if (String(submission.description || "").length > LOOK_LIMITS.description) {
        add("description", "Keep the description under " + LOOK_LIMITS.description + " characters.");
      }
      if (!/^[0-9a-f]{64}$/.test(submission.sha256 || "")) add("file", "Drop the look's zip.");
      else if (!LOOK_LICENSE_RE.test(submission.license || "")) {
        add("file", "look.json needs a license, an SPDX id such as Apache-2.0.");
      } else if (manifest && TRADEMARK_RE.test(manifest.name)) {
        add("file", "look.json's name may not contain “Jarvis”: it is another company's trademark.");
      }
      var archive = submission.archive_url || "";
      if (!archive) add("archive", "Paste the link of this zip on your GitHub release.");
      else if (!LOOK_ARCHIVE_URL_RE.test(archive)) {
        add(
          "archive",
          "Use the zip's download link from a GitHub release: " +
            "https://github.com/you/repo/releases/download/v1.0.0/look.zip",
        );
      }
    }

    var taken = name ? published[name] : null;
    if (taken) {
      if (taken.kind !== kind) {
        add("name", "This ID is already a published " + taken.kind + ". Pick another.");
      } else if (SEMVER_RE.test(submission.version || "") && !versionAbove(submission.version, taken.version)) {
        add(
          "version",
          "This ID is published as " + taken.version + " by @" + taken.publisher +
            ". If that is you, raise the version. If not, pick another ID.",
        );
      }
    }

    var description = kind === "plugin" ? (submission.plugin_json || {}).description : submission.description;
    if (!description || !String(description).trim()) add("description", "Say in a sentence what it does.");
    else if (String(description).length > (limits.max_description_chars || 500)) {
      add("description", "Keep the description under " + (limits.max_description_chars || 500) + " characters.");
    }

    if (kind !== "plugin" && !String(submission.title || "").trim()) add("title", "Give it a name.");

    if (kind === "skill") {
      var body = splitFrontmatter(submission.skill_md || "").body;
      if (!body.trim()) add("file", "Drop a SKILL.md with the skill's instructions in it.");
      else if (byteLength(submission.skill_md) > (limits.max_skill_bytes || 65536)) {
        add("file", "The SKILL.md is larger than the registry accepts.");
      }
    } else if (kind === "plugin") {
      if (!submission.plugin_json || !Object.keys(submission.plugin_json).length || !submission.plugin_json.extensions) {
        add("file", "Drop the plugin's folder, or at least its plugin.json.");
      }
    } else if (kind === "look") {
      /* Checked above. */
    } else if (!submission.agent || typeof submission.agent.instructions !== "string") {
      add("file", "Drop the agent file the app exported (open the agent, press Share).");
    }

    var text = JSON.stringify(submission);
    (rules.secret_patterns || []).some(function (pattern) {
      try {
        if (new RegExp(pattern).test(text)) {
          add("file", "This looks like it contains a password or an API key. Remove it before you publish.");
          return true;
        }
      } catch (err) {
        /* A pattern this engine cannot read is the registry's to enforce. */
      }
      return false;
    });
    return problems;
  }

  // ---------------------------------------------------------------- handoff

  function issueTitle(submission) {
    return TITLE_TAG + " " + submission.name + " " + submission.version;
  }

  function issueBody(submission) {
    return BODY_MARKER + "\n```json\n" + JSON.stringify(submission, null, 1) + "\n```\n";
  }

  /**
   * How the submission reaches GitHub: `{mode, url, clipboard}`.
   *
   * "prefill" carries the whole submission in the address, so the visitor
   * only presses Create. When it is too long for an address, "paste" opens the
   * registry's own form and the submission travels on the clipboard instead.
   */
  function handoff(submission, registryUrl) {
    var title = encodeURIComponent(issueTitle(submission));
    /* The one sentence the visitor reads on GitHub, above the data. */
    var compact =
      BODY_MARKER +
      "\nPress **Create** below to publish this to the Dodaios marketplace.\n\n```json\n" +
      JSON.stringify(submission) +
      "\n```\n";
    var url = registryUrl + "/issues/new?title=" + title + "&body=" + encodeURIComponent(compact);
    if (url.length <= MAX_PREFILL_URL) return { mode: "prefill", url: url, clipboard: null };
    return {
      mode: "paste",
      url: registryUrl + "/issues/new?template=publish.yml&title=" + title,
      clipboard: JSON.stringify(submission, null, 1),
    };
  }

  /** What an issue from the registry's list says about this submission. */
  function issueState(issues, submission, sinceMs) {
    var wanted = (TITLE_TAG + " " + submission.name).toLowerCase();
    for (var i = 0; i < (issues || []).length; i++) {
      var issue = issues[i];
      if (!issue || issue.pull_request) continue;
      var title = String(issue.title || "").trim().toLowerCase();
      if (title !== wanted && title.indexOf(wanted + " ") !== 0) continue;
      if (Date.parse(issue.created_at) < sinceMs - 120000) continue;
      var labels = (issue.labels || []).map(function (label) {
        return label && label.name;
      });
      var state = labels.indexOf("published") !== -1
        ? "published"
        : labels.indexOf("needs-changes") !== -1
          ? "needs-changes"
          : "checking";
      return { state: state, url: issue.html_url, number: issue.number };
    }
    return null;
  }

  var core = {
    KINDS: KINDS,
    slug: slug,
    titleFromName: titleFromName,
    splitFrontmatter: splitFrontmatter,
    frontmatterValue: frontmatterValue,
    readSkill: readSkill,
    normalizeSkill: normalizeSkill,
    draftFromFiles: draftFromFiles,
    zipDirectory: zipDirectory,
    lookMembers: lookMembers,
    parseLookManifest: parseLookManifest,
    readLookZip: readLookZip,
    repositoryOf: repositoryOf,
    LOOK_HOW_TO: LOOK_HOW_TO,
    monogram: monogram,
    nameHue: nameHue,
    nextVersion: nextVersion,
    buildSubmission: buildSubmission,
    publishedNames: publishedNames,
    problemsOf: problemsOf,
    issueTitle: issueTitle,
    issueBody: issueBody,
    handoff: handoff,
    issueState: issueState,
    MAX_PREFILL_URL: MAX_PREFILL_URL,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  root.DodaiosPublish = core;

  // =========================================================================
  // The page
  // =========================================================================

  if (typeof document === "undefined") return;
  var form = document.querySelector("[data-publish-form]");
  if (!form) return;

  var REGISTRY = form.getAttribute("data-registry");
  var API = form.getAttribute("data-api");
  var WATCH_KEY = "dodaios-publish-watch";
  var KIND_LABEL = { skill: "Skill", plugin: "Plugin", agent: "Agent", look: "Look" };
  var $ = function (selector) {
    return form.querySelector(selector);
  };
  var input = {
    name: $("[name=name]"),
    version: $("[name=version]"),
    title: $("[name=title]"),
    description: $("[name=description]"),
    archive: $("[name=archive_url]"),
  };
  /*
   * `origin` is what was dropped, `files` what of it is packed. The ID and the
   * version follow the name and the feed until the person types their own.
   */
  var state = {
    kind: "skill",
    parts: {},
    origin: "",
    files: [],
    categories: "",
    fileVersion: "",
    dropProblem: "",
    nameTouched: false,
    versionTouched: false,
    tried: false,
  };
  var context = { rules: {}, published: {} };

  function fetchJson(url) {
    return fetch(url, { cache: "no-store" }).then(function (response) {
      if (!response.ok) throw new Error(String(response.status));
      return response.json();
    });
  }

  fetchJson("../rules.json").then(function (rules) {
    context.rules = rules;
    render();
  }, function () {});
  fetchJson("../index.json").then(function (index) {
    context.published = publishedNames(index);
    follow();
    render();
  }, function () {});

  function fields() {
    return {
      name: input.name.value,
      version: input.version.value,
      title: input.title.value,
      description: input.description.value,
      categories: state.categories,
      archive_url: input.archive.value,
    };
  }

  function submission() {
    return buildSubmission(state.kind, fields(), state.parts);
  }

  /** Keep the ID and the version in step with the name, until they are typed. */
  function follow() {
    if (!state.nameTouched) input.name.value = slug(input.title.value);
    if (!state.versionTouched) {
      var taken = context.published[input.name.value.trim()];
      input.version.value = nextVersion(state.fileVersion, taken && taken.kind === state.kind ? taken : null);
    }
  }

  function render() {
    var problems = problemsOf(submission(), context, state.parts);
    var byField = {};
    problems.forEach(function (problem) {
      if (!byField[problem.field]) byField[problem.field] = problem.message;
    });
    if (!state.origin) byField.file = "Drop a file or folder first.";
    form.querySelectorAll("[data-problem]").forEach(function (slot) {
      var field = slot.getAttribute("data-problem");
      var message = state.tried ? byField[field] : "";
      /* What was wrong with a drop is said at once, not after a Publish. */
      if (field === "file" && state.dropProblem) message = state.dropProblem;
      slot.textContent = message || "";
      slot.hidden = !message;
    });
    /* A problem with the ID or the version must not hide in a closed section. */
    var named = Boolean(input.title.value.trim()) || state.kind === "plugin";
    if (state.tried && (byField.version || (byField.name && named))) $("[data-more]").open = true;

    var loaded = Boolean(state.origin);
    $("[data-drop]").hidden = loaded;
    $("[data-loaded]").hidden = !loaded;
    $("[data-row-title]").hidden = state.kind === "plugin";
    $("[data-row-archive]").hidden = !(loaded && state.kind === "look");
    if (loaded) {
      var title = input.title.value.trim() || input.name.value.trim() || state.origin;
      var mark = $("[data-pack-mark]");
      mark.textContent = monogram(title);
      mark.style.setProperty("--h", String(nameHue(input.name.value.trim())));
      /* A plugin has no Name field: its manifest names it, so the pack does. */
      $("[data-pack-title]").textContent = state.kind === "plugin" ? title : state.origin;
      var names = state.files.map(baseName);
      var listed = names.slice(0, 3).join(", ") + (names.length > 3 ? " +" + (names.length - 3) : "");
      /* Somebody about to update an entry should see whose it is. */
      var taken = context.published[input.name.value.trim()];
      var look = state.kind === "look" ? state.parts.look || {} : null;
      var detail = taken && taken.kind === state.kind
        ? "Update to " + input.version.value.trim() + " · published by @" + taken.publisher
        : look && look.size
          ? (look.size < 1024 * 1024
            ? Math.max(1, Math.round(look.size / 1024)) + " kB"
            : (look.size / (1024 * 1024)).toFixed(1) + " MB") + " · " + names.length + " files"
          : names.length > 1 || names[0] !== state.origin.toLowerCase() ? listed : "";
      $("[data-pack-meta]").textContent = KIND_LABEL[state.kind] + (detail ? " · " + detail : "");
    }
    return problems.concat(loaded ? [] : [{ field: "file", message: byField.file }]);
  }

  function apply(draft, origin) {
    if (draft.problem) {
      state.dropProblem = draft.problem;
      render();
      return;
    }
    state.dropProblem = "";
    state.kind = draft.kind;
    state.parts = draft.parts;
    state.files = draft.files || [];
    state.origin = origin;
    state.categories = draft.fields.categories || "";
    state.fileVersion = draft.fields.version || "";
    state.nameTouched = Boolean(draft.fields.name);
    /* A look's version is the one in its look.json, never a suggestion. */
    state.versionTouched = draft.kind === "look";
    input.title.value = draft.fields.title || "";
    input.archive.value = draft.fields.archive_url || "";
    input.description.value = draft.fields.description || "";
    input.name.value = draft.fields.name || "";
    follow();
    if (draft.kind === "look") input.version.value = draft.fields.version || "";
    render();
    if (state.kind === "look" && input.title.value && input.description.value) input.archive.focus();
    else (state.kind === "plugin" || input.title.value ? input.description : input.title).focus();
  }

  // --- reading what was dropped or chosen ----------------------------------

  function readFile(file, path) {
    return new Promise(function (resolve) {
      if (file.size > 300000) return resolve(null);
      var reader = new FileReader();
      reader.onload = function () {
        resolve({ path: path || file.webkitRelativePath || file.name, text: String(reader.result) });
      };
      reader.onerror = function () {
        resolve(null);
      };
      reader.readAsText(file);
    });
  }

  function walk(entry, prefix) {
    if (entry.isFile) {
      return new Promise(function (resolve) {
        entry.file(function (file) {
          readFile(file, prefix + entry.name).then(function (read) {
            resolve(read ? [read] : []);
          });
        }, function () {
          resolve([]);
        });
      });
    }
    if (!entry.isDirectory || /^(\.git|node_modules)$/.test(entry.name)) return Promise.resolve([]);
    return new Promise(function (resolve) {
      var reader = entry.createReader();
      var all = [];
      var next = function () {
        reader.readEntries(function (batch) {
          if (!batch.length) {
            Promise.all(all.map(function (child) {
              return walk(child, prefix + entry.name + "/");
            })).then(function (lists) {
              resolve([].concat.apply([], lists));
            });
            return;
          }
          all = all.concat(Array.prototype.slice.call(batch));
          next();
        }, function () {
          resolve([]);
        });
      };
      next();
    });
  }

  /** A dropped zip is a look: read its bytes, not its text. */
  function loadZip(file) {
    if (file.size > 27 * 1024 * 1024) {
      apply({ problem: "A look's zip may be at most 27 MB." }, "");
      return;
    }
    file.arrayBuffer().then(function (buffer) {
      return readLookZip(new Uint8Array(buffer));
    }).then(function (draft) {
      apply(draft, file.name);
    }, function () {
      apply({ problem: "The zip could not be read." }, "");
    });
  }

  function isZip(file) {
    return Boolean(file) && /\.zip$/i.test(file.name || "");
  }

  function load(promise, origin) {
    promise.then(function (files) {
      files = files.filter(Boolean).slice(0, 60);
      if (!files.length) {
        apply({ problem: "Nothing in there could be read as text." }, "");
        return;
      }
      apply(draftFromFiles(files, state.kind), origin || files[0].path.split(/[\\/]/).pop());
    });
  }

  /* The whole page takes the drop, so nobody has to aim. */
  var drop = $("[data-drop]");
  var carriesFiles = function (event) {
    var types = event.dataTransfer && event.dataTransfer.types;
    return Boolean(types) && Array.prototype.indexOf.call(types, "Files") !== -1;
  };
  document.addEventListener("dragover", function (event) {
    if (!carriesFiles(event)) return;
    event.preventDefault();
    form.setAttribute("data-over", "");
  });
  document.addEventListener("dragleave", function (event) {
    if (!event.relatedTarget) form.removeAttribute("data-over");
  });
  document.addEventListener("drop", function (event) {
    if (!carriesFiles(event)) return;
    event.preventDefault();
    form.removeAttribute("data-over");
    /* Both lists are only readable while the event is being handled. */
    var dropped = Array.prototype.slice.call(event.dataTransfer.files || []);
    if (dropped.length === 1 && isZip(dropped[0])) {
      loadZip(dropped[0]);
      return;
    }
    var items = Array.prototype.slice.call(event.dataTransfer.items || []);
    var entries = items
      .map(function (item) {
        return item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
      })
      .filter(Boolean);
    var plain = function () {
      return Promise.all(dropped.map(function (file) {
        return readFile(file);
      }));
    };
    if (!entries.length) {
      load(plain());
      return;
    }
    var label = entries.length === 1 ? entries[0].name : entries.length + " files";
    load(Promise.all(entries.map(function (entry) {
      return walk(entry, "");
    })).then(function (lists) {
      var walked = [].concat.apply([], lists);
      /* A browser that hands out entries it cannot open still has the files. */
      return walked.length ? walked : plain();
    }), label);
  });
  form.querySelectorAll("[data-pick]").forEach(function (picker) {
    picker.addEventListener("change", function () {
      var picked = Array.prototype.slice.call(picker.files || []);
      if (!picked.length) return;
      if (picked.length === 1 && isZip(picked[0])) {
        loadZip(picked[0]);
        picker.value = "";
        return;
      }
      var folder = picker.hasAttribute("webkitdirectory") && picked[0].webkitRelativePath;
      var label = folder
        ? picked[0].webkitRelativePath.split("/")[0]
        : picked.length === 1 ? picked[0].name : picked.length + " files";
      load(Promise.all(picked.map(function (file) {
        return readFile(file);
      })), label);
      picker.value = "";
    });
  });
  form.querySelectorAll("[data-open]").forEach(function (button) {
    button.addEventListener("click", function (event) {
      event.stopPropagation();
      $("[data-pick=" + button.getAttribute("data-open") + "]").click();
    });
  });
  drop.addEventListener("click", function (event) {
    if (event.target instanceof Element && event.target.closest("button, input")) return;
    $("[data-pick=file]").click();
  });
  $("[data-clear]").addEventListener("click", function () {
    state.parts = {};
    state.files = [];
    state.origin = "";
    state.fileVersion = "";
    state.dropProblem = "";
    input.archive.value = "";
    render();
  });

  input.title.addEventListener("input", follow);
  input.name.addEventListener("input", function () {
    state.nameTouched = Boolean(input.name.value);
    follow();
  });
  input.version.addEventListener("input", function () {
    state.versionTouched = Boolean(input.version.value);
  });
  form.addEventListener("input", render);

  // --- publish -------------------------------------------------------------

  var status = document.querySelector("[data-publish-status]");
  var statusText = status.querySelector("[data-status-text]");
  var statusLink = status.querySelector("[data-status-link]");
  var pasteHelp = status.querySelector("[data-paste-help]");

  function show(text, link, linkLabel) {
    status.hidden = false;
    statusText.textContent = text;
    statusLink.hidden = !link;
    if (link) {
      statusLink.href = link;
      statusLink.textContent = linkLabel;
    }
  }

  function watch(job) {
    var started = Date.now();
    var submissionRef = { name: job.name, version: job.version };
    var done = false;
    var search = REGISTRY + "/issues?q=" + encodeURIComponent("is:issue in:title " + TITLE_TAG + " " + job.name);
    var finish = function () {
      done = true;
      try {
        sessionStorage.removeItem(WATCH_KEY);
      } catch (err) {
        /* Private mode: nothing was stored. */
      }
    };
    var checkFeed = function () {
      if (done) return;
      fetchJson("../index.json?t=" + Date.now()).then(function (index) {
        var entry = publishedNames(index)[job.name];
        if (entry && entry.version === job.version) {
          finish();
          pasteHelp.hidden = true;
          status.setAttribute("data-state", "live");
          show(job.name + " " + job.version + " is live.", "../" + job.kind + "s/" + job.name + "/", "Open its page");
        }
      }, function () {});
    };
    var checkIssue = function () {
      if (done) return;
      fetchJson(API + "/issues?state=all&per_page=15&sort=created&direction=desc").then(function (issues) {
        if (done) return;
        var found = issueState(issues, submissionRef, job.since);
        if (!found) return;
        pasteHelp.hidden = true;
        if (found.state === "needs-changes") {
          status.setAttribute("data-state", "problem");
          show("The checks found something to fix.", found.url, "See what");
        } else if (found.state === "published") {
          show("Checks passed. Live in a minute.", found.url, "Your submission");
        } else {
          show("Checks are running.", found.url, "Your submission");
        }
      }, function () {});
    };
    var feedTimer = setInterval(checkFeed, 8000);
    var issueTimer = setInterval(checkIssue, 20000);
    setTimeout(checkIssue, 6000);
    setTimeout(function () {
      clearInterval(feedTimer);
      clearInterval(issueTimer);
      if (done) return;
      show("Not live yet.", search, "Find your submission");
    }, Math.max(0, 12 * 60000 - (started - job.since)));
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    state.tried = true;
    var problems = render();
    if (problems.length) {
      var first = form.querySelector("[data-problem]:not([hidden])");
      if (first) first.scrollIntoView({ block: "center", behavior: "smooth" });
      return;
    }
    var ready = submission();
    var how = handoff(ready, REGISTRY);
    if (how.mode === "paste" && navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(how.clipboard).then(null, function () {});
    }
    window.open(how.url, "_blank", "noopener");
    var job = { name: ready.name, version: ready.version, kind: ready.kind, since: Date.now() };
    try {
      sessionStorage.setItem(WATCH_KEY, JSON.stringify(job));
    } catch (err) {
      /* Private mode: the page still watches until it is closed. */
    }
    status.removeAttribute("data-state");
    pasteHelp.hidden = how.mode !== "paste";
    status.querySelector("[data-copy]").setAttribute("data-copy", how.clipboard || "");
    show(
      how.mode === "paste"
        ? "GitHub is open. Your submission is on the clipboard."
        : "GitHub is open. Press Create there.",
      how.url,
      "Open GitHub again",
    );
    status.scrollIntoView({ block: "nearest", behavior: "smooth" });
    watch(job);
  });

  render();
  try {
    var saved = JSON.parse(sessionStorage.getItem(WATCH_KEY) || "null");
    if (saved && Date.now() - saved.since < 12 * 60000) {
      show("Still watching for " + saved.name + " " + saved.version + ".", null, "");
      watch(saved);
    }
  } catch (err) {
    /* Nothing to resume. */
  }
})(typeof window !== "undefined" ? window : globalThis);
