---
name: writing-effect-checks
description: Write and review assertions that verify what a job did to the world, rather than what it returned. Use when adding monitoring to a scheduled task, cron job, pipeline or unattended agent; when a job reports success but the data looks wrong; when asked whether a check is any good; or when someone proposes to trust an exit code, a log line, a status field or a dashboard as evidence that work happened.
---

# Writing checks that can actually fail

An unattended job that fails loudly is a solved problem. The expensive case is the one that keeps reporting success: the source went dark, the file stopped growing, the task never started, and every indicator stayed green because every indicator was asking the job about itself.

The move is always the same. **Stop asking the run whether it worked. Measure what it was supposed to change.**

## Before writing anything, ask what the failure looks like

Not what success looks like — what the failure *prints*. Most bad checks are written by people who only ever saw the happy path, and the failure output turns out to contain the success token.

A real one: a probe matched `Cipher is` to decide a TLS handshake had succeeded. A failed handshake prints `New, (NONE), Cipher is (NONE)`, which contains it. The probe reported major sites as compliant for weeks. No care in choosing the needle fixes this, because the vocabularies overlap.

**A substring is a valid success criterion only if the failure output is known not to contain it.** When in doubt, name the failure marker too:

```json
{ "contains": "Cipher is", "not_contains": "(NONE)" }
```

## The four check types

Each exists because an incident produced it. The rule for adding a fifth is the same: **no check type without an incident.**

| Type | The question it answers |
|---|---|
| `effect` | Did the action change anything, or do I only have a return value? |
| `freshness` | Is this observation from now, or from then? |
| `diversity` | Is the picture complete, or am I seeing one corner of it? |
| `invariant` | Did something change that was not supposed to? |

`diversity` is the one people skip and then need. A total of 110 looks healthy while one contributor of five has quietly dropped to 8 and the others carry the sum. Use `each_min` for an absolute floor and `min_ratio_of_median` to catch a contributor falling behind its peers — they answer different questions, and the ratio goes blind exactly when everything collapses together.

## A spec, and what each part is for

```json
{
  "name": "nightly-report",
  "_why": "The fee calculator reads this file. If the run dies, the result is not an error but an empty set.",
  "checks": [
    {
      "id": "report-grew",
      "type": "effect",
      "probe": { "kind": "file_size", "path": "out/report.csv" },
      "expect": { "delta_min": 100 }
    },
    {
      "id": "report-is-recent",
      "type": "freshness",
      "probe": { "kind": "file_mtime", "path": "out/report.csv", "must_exist": true },
      "expect": { "max_age_hours": 26 }
    }
  ]
}
```

Keys starting with `_` are ignored, so the spec can carry its own reasoning. Write down *why* the check exists — the next person to see it fail will need that sentence more than the threshold.

## Running it

```bash
# wrap the job: measure, run, measure
python effect_check.py run nightly.json --state nightly.state.json -- python make_report.py

# or two-phase, when something else starts the job
python effect_check.py snapshot nightly.json --state nightly.state.json
python effect_check.py verify   nightly.json --state nightly.state.json
```

Add the state file to `.gitignore`; it holds measured values, which for a text probe means file contents.

## Read the coverage line, not the exit code

```
COVERAGE: 2 of 2 assertions evaluated  —  nothing skipped
```

| Exit | Meaning |
|---|---|
| `0` | everything evaluated, everything passed |
| `1` | at least one assertion failed |
| `2` | **coverage incomplete** — something could not be evaluated |
| `3` | usage or spec error — **nothing** was evaluated |

A checker that evaluates eleven assertions, silently skips five hundred and reports "no problems" has reproduced the failure it was built to prevent. **A probe that cannot be read is a skip, never a pass.**

Under `run` the wrapped job is treated asymmetrically: a zero exit proves nothing and never counts as success, but a non-zero exit is not swallowed. Otherwise the job can crash, the assertions can be green, and the scheduler reports success.

## Traps worth knowing before you hit them

- **All stated expectations must hold.** `{"min": 5, "delta_min": 10}` means both, not the first one recognised.
- **A misspelt `expect` key is an error, not a default.** A typo used to turn a stated threshold into "it grew by at least one".
- **Absence is a skip, not a failure** — a missing file may mean the job never ran, or the path is wrong, or the volume is not mounted. If you *know* it must exist, say `"must_exist": true` and make it a failure you declared. `must_exist` is a statement about the outcome, so it is enforced when verifying, not when taking the baseline — a job whose whole purpose is to create the file must still be checkable.
- **Absent is not zero.** If your source only lists active items, a contributor that disappears is missing rather than low, and a floor will never see it. Name the groups you expect (`require_groups`) and how many there should be (`min_groups`).
- **A stale baseline is refused**, along with one from another spec and one already consumed. Without that, a snapshot step that quietly stopped running leaves yesterday's baseline in place and today's do-nothing job "grows" against it.

## The last step, which is not optional

**Point the check at a job that does nothing and confirm it goes red.**

```bash
python effect_check.py run nightly.json --state nightly.state.json -- python -c "pass"
```

A checker you have never seen fail is a checker you have no reason to trust. The first version of this tool passed its own twelve-case self-test and was broken in nine separate ways, including a "read-only" database path that could drop a table while reporting "query returned no row". Independent reviewers found that. The self-test did not, because the same person wrote both and they shared assumptions.

Full reference: `tool/README.md`. The four incidents that produced the check types: `incidents/`.
