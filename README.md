# Dodaios Marketplace

The community registry for [Dodaios](https://github.com/Dodaios/Dodaios)
plugins, skills and **agent templates**. Anyone can publish; every submission
that passes the automated checks is listed — there is no human review queue.
Every entry's page shows what it reaches and every published file before
anything is installed.

- **Browse:** on the web at <https://dodaios.ai/marketplace/>, or in the app
  under **Marketplace**. Both show the same feed.
- **Publish from the app:** open **Marketplace**, press **Publish yours**,
  sign in with GitHub and add your package. To share an agent, open it and
  press **Share**. The app files the submission as an issue on this
  repository under your account; the checks run and publish it within a
  minute or two.
- **Publish from the browser:** open a
  [Publish to the marketplace](../../issues/new?template=publish.yml) issue
  and paste the submission JSON. Same checks, same result.
- **Publish with a pull request:** add one `submissions/<name>.json`
  (see [Submission format](#submission-format)).
- **Feed:** the compiled [`index.json`](https://dodaios.github.io/marketplace/index.json)
  is what the app reads. The storefront is rendered from the same file on
  every publish (`scripts/build_site.py`), so the two cannot disagree.

## How publishing works

```
submissions/<name>.json  ──PR──►  automated checks  ──green──►  auto-merge
                                                                    │
plugins/<name>/…  skills/<name>/SKILL.md  registry.json  ◄── expansion (bot)
                                                                    │
                        GitHub Pages: index.json  ◄── compile + deploy
```

1. A pull request adds or updates **one** file: `submissions/<name>.json`.
2. `validate` checks it: naming rules, reserved names, https-only URLs,
   stdio launcher allowlist with pinned versions, no credentials anywhere,
   size limits (see `scripts/validate.py` — the app re-enforces the same
   rules at install time).

   The stdio allowlist covers the **arguments**, not just the launcher name,
   because each allowed launcher has options that run anything at all. In
   practice a stdio server must look like one of these, and the package it
   names must be a pinned name from its own registry — never a git ref, a
   URL, or a path:

   ```
   npx -y my-mcp@1.2.0
   uvx my-mcp==1.2.0                 # or: uvx --from my-mcp==1.2.0 my-server
   docker run -i --rm my/mcp:1.2     # -e passes a variable NAME, no value
   ```

   Arguments after the package belong to your server and are not checked.
   `env` keys must be your own variables (`PATH`, `LD_PRELOAD`, `NODE_OPTIONS`
   and friends are refused) with `$plugin_…` placeholders as their values.
3. The `automerge` gate (trusted code, never executes PR content) verifies
   the PR changes exactly that one file and that the publisher is proven —
   either because you opened the pull request yourself (`publisher` and
   `publisher_id` must be yours), or because the submission came through
   the upload form, whose GitHub App pushed the branch into this repo after
   verifying your sign-in. Then it merges. Everything else waits for a
   maintainer.
4. On main, the bot expands the submission into an
   [Agent Plugins v1.0.0](https://agent-plugins.org/) package under
   `plugins/` (or `skills/<name>/SKILL.md`), records ownership in
   `registry.json`, compiles `index.json`, and deploys it to Pages.

## Publishing through an issue

`scripts/intake.py` (workflow `intake.yml`) handles issues whose title starts
with `[publish]`. It reads the first fenced JSON block in the issue body,
**replaces `publisher` and `publisher_id` with the account that opened the
issue** — GitHub authenticated that account, the JSON proves nothing — and
runs the same `validate.py` every pull request meets, ownership and version
rules included. Green: the bot commits `submissions/<name>.json` to `main`,
starts `publish.yml`, answers with the install line and closes the issue.
Red: it answers with what to change and labels the issue `needs-changes`;
editing the issue runs the checks again. Issue bodies are data: nothing in
them is executed, and the workflow always runs from `main`.

## Ownership and updates

The first merged submission of a name claims it. `registry.json` records
two things about the publisher: `publisher_id`, your **numeric GitHub
account id**, and `publisher`, your login — shown to humans, never used to
decide anything.

That split is deliberate. A GitHub login can be renamed, and the freed name
can then be registered by a stranger; if ownership hung on the login string,
that stranger would inherit every entry published under it. Your account id
never changes, so it is what the gate compares.

Updates auto-merge only from the account that holds the entry and must
increase `version`. Once an entry records a `publisher_id`, every later
update must carry the same one — leaving the field out is rejected, not
waved through. Entries published before the field existed still compare
logins until their next update. Name changes are new submissions.

Find your account id at `https://api.github.com/users/<your-login>` — the
submit form fills it in for you.

## Submission format

One JSON file per item — see [`schemas/submission.schema.json`](schemas/submission.schema.json)
and the live examples in [`submissions/`](submissions/).

The limits, patterns, reserved names and allowlists are generated from the
validator into [`rules.json`](rules.json) and published at
<https://dodaios.github.io/marketplace/rules.json>. Anything that
checks a submission before it reaches CI — the upload form, the app — reads
that file instead of retyping the values, and CI fails if the two drift
apart. `scripts/validate.py` stays the authority for the rules that are
logic rather than data.

**Plugin** (`kind: "plugin"`): an embedded Agent Plugins v1.0.0
`plugin_json` (Dodaios specifics under the `io.github.personaljarvis`
extension: auth mode, branding, category), an optional `mcp_json`
(one `streamable-http` or pinned `stdio` server), an optional `skills`
array, and an optional `usage_card` (keywords that help Dodaios offer the
plugin on relevant turns). A package must carry at least one working
component — an entry that only collects a token is refused.

**Skill** (`kind: "skill"`): `title`, `description`, `categories`, and the
full `skill_md` (a `SKILL.md` with YAML frontmatter — the app validates it
on install and shows it to the user before it can run).

A skill does **not** have to be written for Dodaios. The frontmatter
only has to carry `name` and `description`; anything beyond that may be your
agent's own vocabulary (`allowed-tools`, `model`, …). Such an entry is marked
**portable**, and the store shows the open installer beside our own:

```
dodaios marketplace install <name>                    # Dodaios
npx skills add Dodaios/marketplace --skill <name>     # any other agent
```

The mark is derived from your file — a SKILL.md using none of Jarvis' own
keys (`schema_version`, `triggers`, `execution`, …) is portable — so there is
nothing to declare. Two optional fields override or enrich it:
`"flavor": "jarvis" | "portable"` and `"compatible_agents": ["Claude Code",
"Cursor"]` (up to 8 names, shown on the card).

**Agent** (`kind: "agent"`): `title`, `description` (the one-paragraph
summary on the store card), `categories`, and `agent` — the template of one
Dodaios agent:

```json
"agent": {
  "schema": 1,
  "name": "Inbox Butler",
  "title": "Keeps the inbox at zero",
  "instructions": "You own the inbox. Every morning …",
  "tier": "specialist",
  "effort": "medium",
  "focus": ["plugin:gmail", "core:browser"],
  "grant_mode": "all",
  "require_approval": ["plugin:gmail:send"],
  "knowledge_scope": "shared",
  "avatar": { "contract": 1, "archetype": "biped", "base": "rogue", "parts": {} }
}
```

A template is the agent's **design**, never its operation. The fields above
are an allowlist and anything else is refused. These are refused by name,
because they would either point at the author's machine or let a stranger's
template decide what runs without asking on yours: `account_id`, `provider`,
`model`, `workspace_dir`, `computer_id`, `permission_ceiling`,
`approval_mode`, `approval_rules` / `always_allow`, `daily_budget_usd`,
`max_concurrent_runs`, `browser_mode`, `browser_allowed_domains`, and an
imported figure (`avatar.model`). Instructions may not name a home folder
(`C:\Users\…`, `/Users/…`, `/home/…`), and the credential scan covers the
whole file. Installing a template creates a new agent on the installer's own
model with the app's default permissions; `require_approval` can only add
confirmations, never remove them.

### Bundling skills with a plugin

A plugin may ship the instructions for using it, which is what the Agent
Plugins standard is for: tools and guidance installed together. Add them to
the submission as

```json
"skills": [{ "name": "sentry-triage", "skill_md": "---\nname: sentry-triage\n..." }]
```

and the expansion writes a real `plugins/<name>/skills/<skill>/SKILL.md`, so
the published directory is a package any client implementing the standard
can read. Installing the plugin writes those skills into the user's skills
folder — named on the consent dialog beforehand — and removing the plugin
takes them away again.

Two limits, enforced by CI and again by the app:

* **No `scripts/`.** The standard allows a skill to ship executables; this
  registry publishes instructions and reference text only.
* **No `risk_policy` in the frontmatter.** That field decides which tools run
  without asking the user, and nothing here is reviewed by a human — the
  built-in default applies instead.

## Trust model, stated plainly

Nothing here is reviewed by a human before it is listed. The automated
checks stop credential smuggling, plaintext endpoints, unpinned code
execution, and name hijacking — they cannot judge whether a service is
trustworthy. The app therefore shows every community plugin and skill as
unreviewed and displays the exact endpoint or command before installing.

What replaces prior review is speed afterwards: report a listing by opening
an issue and it is delisted within minutes of being confirmed.
