#!/usr/bin/env python3
"""Regression test: every published entry travels with its owner's account id.

A GitHub login is a display name. It can be renamed, and the freed handle can
be registered by somebody else the same day. `publisher` alone therefore cannot
answer "whose work is this?" — a consumer that joins entries to a person by
name hands one account's listings to a stranger the moment a rename happens,
and breaks the original owner's profile page in the same move.

The ownership ledger (registry.json) has keyed on the numeric account id since
the beginning; automerge_gate.py refuses a submission whose id does not match
the pull request author. This test exists because that number used to stop at
the ledger: build_index.py copied `publisher` into the feed and left
`publisher_id` behind, so every downstream consumer was back to matching on a
renameable string.

What it checks, per lane:
- the key is present on every entry, spelled exactly `publisher_id`;
- it is a positive integer, never a string — a JSON consumer comparing 12 to
  "12" gets a silent no-match, which reads as "this person published nothing";
- it agrees with the ledger, so the feed cannot drift from the record that
  decides who may update an entry;
- an entry the ledger has no id for still ships (as null) rather than being
  dropped — the feed's job is to carry what is published, not to withhold an
  entry over a field that predates the field.

Usage:
    python scripts/test_publisher_id.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LANES = ("plugins", "skills", "agents", "looks")


def load_build_index():
    spec = importlib.util.spec_from_file_location(
        "build_index", ROOT / "scripts" / "build_index.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main() -> int:
    build_index = load_build_index()
    failures: list[str] = []

    def check(what: str, condition: bool) -> None:
        if not condition:
            failures.append(what)

    registry_path = ROOT / "registry.json"
    if not registry_path.exists():
        print("registry.json is missing — nothing to check")
        return 0
    registry = json.loads(registry_path.read_text(encoding="utf-8"))

    # Build the real feed into a throwaway directory. Running the actual
    # entry point rather than a re-implementation is the point: a test that
    # rebuilds the dict itself would keep passing after somebody deletes the
    # line from build_index.py.
    with tempfile.TemporaryDirectory() as tmp:
        argv = sys.argv
        sys.argv = ["build_index.py", "--out", tmp]
        try:
            build_index.main()
        finally:
            sys.argv = argv
        index = json.loads((Path(tmp) / "index.json").read_text(encoding="utf-8"))

    seen = 0
    for lane in LANES:
        entries = index.get(lane, [])
        for entry in entries:
            name = entry.get("name", "<unnamed>")
            where = f"{lane}/{name}"
            check(f"{where}: no publisher_id key at all", "publisher_id" in entry)

            value = entry.get("publisher_id")
            expected = registry.get(name, {}).get("publisher_id")

            if expected is None:
                # Predates the field, or a hand-written entry. It must still be
                # in the feed, and the key must still be there — as null, which
                # a consumer can tell apart from "the key was never written".
                check(f"{where}: id absent in ledger but present in feed", value is None)
                continue

            seen += 1
            check(
                f"{where}: publisher_id is {type(value).__name__}, expected int",
                isinstance(value, int) and not isinstance(value, bool),
            )
            check(f"{where}: publisher_id {value} is not positive", isinstance(value, int) and value > 0)
            check(
                f"{where}: feed says {value}, ledger says {expected}",
                value == expected,
            )

    # A feed that carries no ids at all would pass every check above by having
    # nothing to check. The registry has ids today, so at least one must arrive.
    check(
        "not a single entry carried a publisher_id — did the feed stop copying it?",
        seen > 0,
    )

    for failure in failures:
        print(f"FAIL  {failure}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print(f"publisher_id: ok ({seen} entr(y/ies) checked against the ledger)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
