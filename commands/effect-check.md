---
description: Run effect_check against a spec and report coverage, not just the exit code
argument-hint: [path/to/spec.json] [snapshot|verify|selftest]
allowed-tools: Bash, Read, Glob
---

Run the bundled checker and report what it actually established.

The tool is at `${CLAUDE_PLUGIN_ROOT}/tool/effect_check.py`. It needs Python 3.7+ and nothing else.

**Arguments given:** `$ARGUMENTS`

Work out what was asked:

- **No arguments** — look for `*.json` spec files near the current directory (a spec has a top-level `name` and `checks`). If exactly one exists, verify it. If several do, list them and ask which. If none, run `python "${CLAUDE_PLUGIN_ROOT}/tool/effect_check.py" selftest` instead and report the result, so the user at least sees the tool work.
- **A spec path only** — run `verify` against it. If it needs a baseline (any `delta_min`, `delta_max` or `unchanged` expectation) and no state file exists, say so and offer `snapshot` first rather than producing a spurious skip.
- **A spec path and a subcommand** — run that subcommand.
- **`selftest`** — run it and report the tallies.

Then report in this order:

1. **The coverage line first.** `COVERAGE: n of m assertions evaluated`. If anything was skipped, name it and say why it is a skip and not a pass — a probe that could not be read establishes nothing.
2. **Each failed assertion**, with the measured value against the expected one.
3. **The exit code last**, and only as a summary: `0` all evaluated and passed, `1` something failed, `2` coverage incomplete, `3` nothing was evaluated at all.

Do not report a run as healthy on the strength of exit code `0` alone if the coverage line shows skips — that combination cannot occur, and if you ever see it, the tool is wrong and that is the finding.

If the user seems to be writing or reviewing a spec rather than running one, the `writing-effect-checks` skill in this plugin covers the check types and the traps.
