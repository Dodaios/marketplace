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

  var KINDS = ["skill", "plugin", "agent"];
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
    var draft = { kind: preferredKind || "skill", fields: {}, parts: {}, problem: null };
    var byName = {};
    files.forEach(function (file) {
      byName[baseName(file.path)] = file;
    });

    var ready = null;
    files.forEach(function (file) {
      if (ready || !/\.json$/i.test(file.path)) return;
      var doc = parseJson(file.text);
      if (doc && KINDS.indexOf(doc.kind) !== -1 && typeof doc.name === "string") ready = doc;
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
      if (byName["mcp.json"]) {
        var mcp = parseJson(byName["mcp.json"].text);
        if (!mcp) {
          draft.problem = "mcp.json is not valid JSON.";
          return draft;
        }
        draft.parts.mcp_json = mcp;
      }
      if (byName["usage-card.md"]) draft.parts.usage_card = byName["usage-card.md"].text;
      var bundled = [];
      files.forEach(function (file) {
        var hit = /(?:^|[\\/])skills[\\/]([^\\/]+)[\\/]skill\.md$/i.exec(file.path);
        if (hit) bundled.push({ name: hit[1], skill_md: file.text });
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
      if (doc && typeof doc.instructions === "string" && typeof doc.name === "string") template = doc;
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
      "That is not something this page can publish. Drop a SKILL.md, a plugin folder with a plugin.json, or an agent file exported from the app.";
    return draft;
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
    [["plugin", "plugins"], ["skill", "skills"], ["agent", "agents"]].forEach(function (pair) {
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
  function problemsOf(submission, context) {
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

    if (!name) add("name", "Give it a name.");
    else if (!NAME_RE.test(name) || name.indexOf("--") !== -1 || name.indexOf("..") !== -1) {
      add("name", "Use lowercase letters, digits, hyphens and dots only, starting and ending with a letter or digit.");
    } else if (kind === "plugin" && (reserved.plugin_ids || []).indexOf(name) !== -1) {
      add("name", "This name belongs to a plugin that ships with the app. Pick another.");
    } else if (kind === "skill" && (reserved.skill_names || []).indexOf(name) !== -1) {
      add("name", "This name belongs to a skill that ships with the app. Pick another.");
    }

    if (!SEMVER_RE.test(submission.version || "")) add("version", "Use three numbers, like 1.0.0.");

    var taken = name ? published[name] : null;
    if (taken) {
      if (taken.kind !== kind) {
        add("name", "This name is already a published " + taken.kind + ". Pick another.");
      } else if (SEMVER_RE.test(submission.version || "") && !versionAbove(submission.version, taken.version)) {
        add(
          "version",
          "This name is published as " + taken.version + " by @" + taken.publisher +
            ". If that is you, raise the version. If not, pick another name.",
        );
      }
    }

    var description = kind === "plugin" ? (submission.plugin_json || {}).description : submission.description;
    if (!description || !String(description).trim()) add("description", "Say in a sentence what it does.");
    else if (String(description).length > (limits.max_description_chars || 500)) {
      add("description", "Keep the description under " + (limits.max_description_chars || 500) + " characters.");
    }

    if (kind !== "plugin" && !String(submission.title || "").trim()) add("title", "Give it a title.");

    if (kind === "skill") {
      var body = splitFrontmatter(submission.skill_md || "").body;
      if (!body.trim()) add("file", "Add the skill's instructions: drop a SKILL.md or write them.");
      else if (byteLength(submission.skill_md) > (limits.max_skill_bytes || 65536)) {
        add("file", "The SKILL.md is larger than the registry accepts.");
      }
    } else if (kind === "plugin") {
      if (!submission.plugin_json || !Object.keys(submission.plugin_json).length || !submission.plugin_json.extensions) {
        add("file", "Drop the plugin's folder, or at least its plugin.json.");
      }
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
  var $ = function (selector) {
    return form.querySelector(selector);
  };
  var input = {
    name: $("[name=name]"),
    version: $("[name=version]"),
    title: $("[name=title]"),
    description: $("[name=description]"),
    categories: $("[name=categories]"),
    text: $("[name=text]"),
  };
  var state = { kind: "skill", parts: {}, origin: "", nameTouched: false, tried: false };
  var context = { rules: {}, published: {} };

  var DROP_COPY = {
    skill: ["Drop your SKILL.md here", "A Markdown file with the instructions. Plain text is fine too."],
    plugin: ["Drop your plugin folder here", "The folder with plugin.json, and mcp.json if it has a server."],
    agent: ["Drop your agent file here", "In the app, open the agent and press Share to export it."],
  };

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
    render();
  }, function () {});

  function fields() {
    return {
      name: input.name.value,
      version: input.version.value,
      title: input.title.value,
      description: input.description.value,
      categories: input.categories.value,
    };
  }

  function currentParts() {
    if (state.kind === "skill" && !state.parts.skill_md && input.text.value.trim()) {
      return { skill_md: input.text.value };
    }
    return state.parts;
  }

  function submission() {
    return buildSubmission(state.kind, fields(), currentParts());
  }

  function setKind(kind) {
    state.kind = kind;
    form.querySelectorAll("[data-kind]").forEach(function (tab) {
      tab.setAttribute("aria-selected", tab.getAttribute("data-kind") === kind ? "true" : "false");
    });
    $("[data-drop-title]").textContent = DROP_COPY[kind][0];
    $("[data-drop-hint]").textContent = DROP_COPY[kind][1];
    $("[data-write]").hidden = kind !== "skill" || Boolean(state.parts.skill_md);
    $("[data-row-title]").hidden = kind === "plugin";
    $("[data-row-categories]").hidden = kind === "plugin";
  }

  function render() {
    var problems = problemsOf(submission(), context);
    var byField = {};
    problems.forEach(function (problem) {
      if (!byField[problem.field]) byField[problem.field] = problem.message;
    });
    form.querySelectorAll("[data-problem]").forEach(function (slot) {
      var message = state.tried ? byField[slot.getAttribute("data-problem")] : "";
      slot.textContent = message || "";
      slot.hidden = !message;
    });
    $("[data-install-line]").textContent =
      "dodaios marketplace install " + (input.name.value.trim() || "your-name");
    $("[data-count]").textContent = String(input.description.value.length);
    var loaded = $("[data-loaded]");
    loaded.hidden = !state.origin;
    $("[data-loaded-name]").textContent = state.origin;
    $("[data-drop]").hidden = Boolean(state.origin);
    $("[data-write]").hidden = state.kind !== "skill" || Boolean(state.origin);
    return problems;
  }

  function apply(draft, origin) {
    if (draft.problem) {
      var slot = $("[data-problem=file]");
      slot.textContent = draft.problem;
      slot.hidden = false;
      return;
    }
    state.parts = draft.parts;
    state.origin = origin;
    state.nameTouched = Boolean(draft.fields.name);
    setKind(draft.kind);
    Object.keys(draft.fields).forEach(function (key) {
      if (input[key] && draft.fields[key]) input[key].value = draft.fields[key];
    });
    if (!input.version.value) input.version.value = "1.0.0";
    var taken = context.published[input.name.value];
    if (taken && taken.kind === draft.kind && !/[1-9]/.test(draft.fields.version || "")) {
      /* An update: suggest the next patch version instead of failing on it. */
      var parts = taken.version.split(".");
      if (parts.length === 3) input.version.value = parts[0] + "." + parts[1] + "." + (parseInt(parts[2], 10) + 1);
    }
    render();
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

  function load(promise, origin) {
    promise.then(function (files) {
      files = files.filter(Boolean).slice(0, 60);
      if (!files.length) return;
      apply(draftFromFiles(files, state.kind), origin || baseName(files[0].path));
    });
  }

  var drop = $("[data-drop]");
  drop.addEventListener("dragover", function (event) {
    event.preventDefault();
    drop.setAttribute("data-over", "");
  });
  drop.addEventListener("dragleave", function () {
    drop.removeAttribute("data-over");
  });
  drop.addEventListener("drop", function (event) {
    event.preventDefault();
    drop.removeAttribute("data-over");
    var items = Array.prototype.slice.call(event.dataTransfer.items || []);
    var entries = items
      .map(function (item) {
        return item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
      })
      .filter(Boolean);
    if (entries.length) {
      var label = entries.length === 1 ? entries[0].name : entries.length + " files";
      load(Promise.all(entries.map(function (entry) {
        return walk(entry, "");
      })).then(function (lists) {
        return [].concat.apply([], lists);
      }), label);
      return;
    }
    var dropped = Array.prototype.slice.call(event.dataTransfer.files || []);
    load(Promise.all(dropped.map(function (file) {
      return readFile(file);
    })));
  });
  form.querySelectorAll("[data-pick]").forEach(function (picker) {
    picker.addEventListener("change", function () {
      var picked = Array.prototype.slice.call(picker.files || []);
      var label = picked.length === 1 ? picked[0].name : picked.length + " files";
      load(Promise.all(picked.map(function (file) {
        return readFile(file);
      })), label);
      picker.value = "";
    });
  });
  form.querySelectorAll("[data-open]").forEach(function (button) {
    button.addEventListener("click", function () {
      $("[data-pick=" + button.getAttribute("data-open") + "]").click();
    });
  });
  $("[data-clear]").addEventListener("click", function () {
    state.parts = {};
    state.origin = "";
    render();
  });

  form.querySelectorAll("[data-kind]").forEach(function (tab) {
    tab.addEventListener("click", function () {
      setKind(tab.getAttribute("data-kind"));
      render();
    });
  });

  input.title.addEventListener("input", function () {
    /* Until somebody types a name of their own, the title names the entry. */
    if (!state.nameTouched) input.name.value = slug(input.title.value);
  });
  input.name.addEventListener("input", function () {
    state.nameTouched = Boolean(input.name.value);
  });
  input.text.addEventListener("input", function () {
    if (state.nameTouched || input.title.value) return;
    var read = readSkill(input.text.value);
    if (read.title) {
      input.title.value = read.title;
      input.name.value = read.name || slug(read.title);
    }
    if (read.description && !input.description.value) input.description.value = read.description;
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
          show(
            "The checks found something to fix. The answer is on your submission: edit it there and the checks run again.",
            found.url,
            "Open your submission",
          );
        } else if (found.state === "published") {
          show("Checks passed. It will be in the store in a minute.", found.url, "Your submission");
        } else {
          show("GitHub has your submission. The checks are running.", found.url, "Your submission");
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
      show("Not live yet. Your submission on GitHub says where it stands.", search, "Find your submission");
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
        ? "GitHub opened in a new tab. Your submission is on the clipboard."
        : "GitHub opened in a new tab with everything filled in. Sign in if it asks, then press Create.",
      how.url,
      "Open GitHub again",
    );
    status.scrollIntoView({ block: "center", behavior: "smooth" });
    watch(job);
  });

  setKind("skill");
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
