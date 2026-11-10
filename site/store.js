/*
 * The storefront's two behaviours: the search with its kind switch, and the
 * copy buttons. Every page reads fine without this file; it only saves the
 * visitor a scroll or a selection.
 */
(function () {
  "use strict";

  // --- brand glyphs --------------------------------------------------------
  // A plugin's glyph comes from an icon host. When it does not arrive, the
  // tile falls back to the letters that are already in the markup.
  document.querySelectorAll("img[data-mark-glyph]").forEach(function (glyph) {
    var fallback = function () {
      var letters = glyph.nextElementSibling;
      glyph.remove();
      if (letters) letters.hidden = false;
    };
    if (glyph.complete && glyph.naturalWidth === 0) fallback();
    else glyph.addEventListener("error", fallback);
  });

  // --- copy buttons -------------------------------------------------------
  document.querySelectorAll("[data-copy]").forEach(function (button) {
    button.addEventListener("click", function () {
      var text = button.getAttribute("data-copy") || "";
      var done = function () {
        var label = button.textContent;
        button.textContent = button.getAttribute("data-copied") || "Copied";
        window.setTimeout(function () {
          button.textContent = label;
        }, 1600);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () {});
      }
    });
  });

  // --- search and kind switch ----------------------------------------------
  var input = document.querySelector("[data-store-search]");
  if (!input) return;

  var tabs = Array.prototype.slice.call(document.querySelectorAll("[data-kind-tab]"));
  var groups = Array.prototype.slice.call(document.querySelectorAll("[data-kind-group]"));
  var empty = document.querySelector("[data-store-empty]");
  var emptyQuery = document.querySelector("[data-store-empty-query]");
  var kind = "all";

  function words(value) {
    return value.toLowerCase().split(/\s+/).filter(Boolean);
  }

  function apply() {
    var needle = words(input.value);
    var counts = { all: 0 };
    var shown = 0;
    groups.forEach(function (group) {
      var groupKind = group.getAttribute("data-kind-group");
      var visible = 0;
      group.querySelectorAll("[data-search]").forEach(function (row) {
        var haystack = row.getAttribute("data-search") || "";
        var match = needle.every(function (word) {
          return haystack.indexOf(word) !== -1;
        });
        // The counts answer "how many would I see there", so they follow the
        // search and ignore the kind that happens to be selected.
        if (match) {
          counts.all += 1;
          counts[groupKind] = (counts[groupKind] || 0) + 1;
        }
        var show = match && (kind === "all" || kind === groupKind);
        row.hidden = !show;
        if (show) visible += 1;
      });
      group.hidden = visible === 0;
      shown += visible;
    });
    tabs.forEach(function (tab) {
      var tabKind = tab.getAttribute("data-kind-tab");
      tab.setAttribute("aria-selected", tabKind === kind ? "true" : "false");
      var count = tab.querySelector("[data-count]");
      if (count) count.textContent = String(counts[tabKind] || 0);
    });
    if (empty) {
      empty.hidden = shown !== 0;
      if (emptyQuery) emptyQuery.textContent = input.value.trim();
    }
  }

  tabs.forEach(function (tab) {
    tab.addEventListener("click", function (event) {
      event.preventDefault();
      kind = tab.getAttribute("data-kind-tab") || "all";
      apply();
    });
  });

  input.addEventListener("input", apply);
  input.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && input.value) {
      input.value = "";
      apply();
    }
  });

  // "/" jumps into the search, unless the visitor is already typing.
  document.addEventListener("keydown", function (event) {
    if (event.key !== "/" || event.ctrlKey || event.metaKey || event.altKey) return;
    var target = event.target;
    if (target instanceof Element && target.closest("input, textarea, [contenteditable='true']")) {
      return;
    }
    event.preventDefault();
    input.focus();
  });

  // A shared link can carry a search: ?q=sentry
  var initial = new URLSearchParams(window.location.search).get("q");
  if (initial) input.value = initial;
  apply();
})();
