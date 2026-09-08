# effect_check

Verify that a scheduled run had an **effect**, instead of asking it whether it succeeded.

Python 3.7+, standard library only, one file, no dependencies.

> ⚠️ **A spec file is a program.** The `command` probe executes what the spec tells it to — as a shell line when given a string, without a shell when given a list. Never run a spec you have not read, exactly as you would not run a downloaded shell script.

---

## Getting started in five minutes

Nothing to install. Copy `effect_check.py` next to the job you want to check.

**1. See it work, and see it fail.** Two commands, in [`../examples/minimal/`](../examples/minimal/): the same check against a job that works, and against one that does nothing while printing the same log line and exiting zero.

Or run the self-test, which builds a temporary directory, breaks things in it on purpose, and checks that the tool notices:

```bash
python effect_check.py selftest
```

**2. Write your first spec.** Say what the job is supposed to *do to the world*, not what it is supposed to return. Save as `nightly.json`:

```json
{
  "name": "nightly-report",
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

**3. Wrap the job.** The tool measures before, runs your command, and measures after:

```bash
python effect_check.py run nightly.json --state nightly.state.json -- python make_report.py
```

**4. Read the first line, not the exit code.**

```
COVERAGE: 2 of 2 assertions evaluated  —  nothing skipped

[nightly-report]  passed 2  failed 0  skipped 0
```

**5. Prove it can fail.** Point the same spec at a job that does nothing (`-- python -c "pass"`) and run it again. It must say `FAILED … unchanged … the run produced no effect here`. A checker you have never seen fail is a checker you have no reason to trust.

**In a scheduled task or cron job** the wrapper form is usually enough. Where the job is started by something you do not control, use the two-phase form:

```bash
python effect_check.py snapshot nightly.json --state nightly.state.json
# … the job runs, by whatever means …
python effect_check.py verify   nightly.json --state nightly.state.json
```

Add `nightly.state.json` to `.gitignore`. It holds measured values, which for a `file_text` probe means file contents.

---

## Why these four checks

Four incidents in [`../incidents/`](../incidents/) produced four check types. None was invented to round the set out — the rule is **no check type without an incident**.

| Primitive | The question it answers |
|---|---|
| `effect` | Did the action change anything, or do I only have a return value? |
| `freshness` | Is this observation from now, or from then? |
| `diversity` | Is the picture complete, or am I seeing one corner of it? |
| `invariant` | Did something change that was not supposed to? |

And a fifth rule that governs all four:

> **Every run reports its coverage, not only its findings.**

A probe that cannot be read produces a **skip**. Never a pass. Skips are counted, named, and make the run *incomplete* rather than green. A checker that evaluates eleven assertions, silently skips five hundred and reports "no problems" has reproduced the first incident in this archive inside the tool meant to prevent it.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | every assertion evaluated, every assertion passed |
| `1` | at least one assertion failed |
| `2` | **coverage incomplete** — something could not be evaluated |
| `3` | usage, specification, or internal error — **nothing** was evaluated |

Exit code `2` exists to distinguish incomplete coverage from an evaluated failure. A run that fails nothing but could not evaluate half its assertions is not a good run.

The exit code is a summary, not the evidence. Read the coverage line.

### Under `run`, the wrapped command is treated asymmetrically

A **zero** exit from the wrapped job proves nothing, so it never counts as success — the assertions decide. A **non-zero** exit is the job telling you it did not finish, and that is not swallowed: the run comes out failed even if every assertion holds.

An earlier version returned only the verify result. A reviewer refused to wrap a nightly task with it for exactly that reason — the job could crash, the assertions could be green, and the task would report success. That is the wrapper from the first incident in this archive, one layer up, inside the tool written about it.

## Spec reference

Keys beginning with `_` are ignored, so a spec can carry its own reasoning. Any **other** unrecognised `expect` key is an error: a misspelt one used to be ignored, which quietly turned a stated threshold into "it grew by at least one".

### Probes

| Probe | Measures |
|---|---|
| `file_size` | bytes; `path` |
| `file_mtime` | modification time; `path` |
| `file_text` | contents; `path`, optional `encoding`, `max_bytes` |
| `glob_count` | number of matches; `pattern` |
| `tree_digest` | a content hash over a directory; `root`, optional `exclude`, `content_limit` |
| `group_counts` | counts **per group**; `source` = `json` \| `glob` \| `sqlite`; optional `only`, `value_key` |
| `sqlite_scalar` | one value; `db`, `sql` |
| `http_json` | a response, or a value inside it; `url`, optional `pointer`, `headers`, `timeout`, `max_bytes` |
| `command` | a command's **output**; `cmd`, optional `timeout` |

Two options on `group_counts` came from wiring it to a live status feed, and both exist for the same reason:

- **`only`** names the groups the assertion is about. Everything else is ignored. Without it, one contributor that is not wired up yet — reporting `null` — makes the whole probe unreadable, and a genuine shortfall in a working contributor can never be reported.
- **`value_key`** reads one field out of each group's object, because a status feed usually reports an object per contributor rather than a bare number. Naming the field beats reshaping the feed, which would mean a second copy of the truth.

A `null` in a group the spec **did** name stays unreadable, and that is the point: a missing number means the instrument is broken, which is a different finding from a contributor that has gone quiet. Confusing those two is what cost seven weeks in the first incident here.

Every probe that reads a path also accepts `must_exist` (see below). `command` takes the process's output as the measurement and ignores its return code, on purpose.

### Expectations

| Key | Type | Meaning |
|---|---|---|
| `delta_min` | `effect` | grew by at least this much since the snapshot |
| `delta_max` | `effect` | grew by at most this much |
| `unchanged` | `effect` | must not have moved at all |
| `min` / `max` | `effect` | absolute bound; needs no snapshot |
| `contains` / `equals` | `effect` | the result must contain / equal this |
| `not_contains` | `effect` | the result must **not** contain this — the failure marker |
| `max_age_hours` | `freshness` | how old the observation may be |
| `each_min` | `diversity` | every group must reach this — **not** the total |
| `min_ratio_of_median` | `diversity` | every group must reach this fraction of the median across groups |
| `min_groups`, `require_groups` | `diversity` | how many groups, and which ones by name |
| `allow_empty` | `diversity` | permit a result with no groups at all |

**All stated expectations must hold.** An earlier version stopped at the first one it recognised, so `{"min": 5, "delta_min": 10}` passed on the minimum while the job did nothing at all.


### The substring trap

**A substring is only a valid success criterion if the failure output is known not to contain it.**

A reader described a monitoring probe that matched on `Cipher is` to decide a TLS handshake had succeeded. A *failed* handshake prints:

```
New, (NONE), Cipher is (NONE)
```

which contains it. The probe reported major sites as compliant for weeks. No amount of care in choosing the needle separates those two cases, because the success token is a substring of the error line — the vocabularies overlap.

This tool had the same hole and no warning about it. `not_contains` is the answer: name the **failure** marker alongside the success one.

```json
{ "contains": "Cipher is", "not_contains": "(NONE)" }
```

The habit worth taking from it is not the key but the question: **when you write a check, look at what the failure output actually says.** Almost nobody does, which is why this class survives so long.

`each_min` and `min_ratio_of_median` answer different questions and belong together.

The **ratio** catches a single contributor that has fallen behind its peers, even when nobody remembered to maintain a threshold. It came from a search path that returned 8 candidates where its peers returned 55: no total would have shown it, because the peers carried the sum, and any absolute floor set below 8 would have stayed quiet.

The **floor** catches the case the ratio cannot see. If every contributor collapses together, the ratio stays perfectly healthy while the whole thing fails. The self-test keeps that case as a passing test on the ratio and a failing one on the floor, so the limit is not forgotten.

Where the median is taken over fewer than three groups, the result says so rather than passing quietly.

An `effect` assertion stated as a change needs a snapshot; one stated absolutely does not.

## What it deliberately does not do

**It does not guess what a missing target means.** An absent file may mean the job never ran, the path is wrong, or the volume is not mounted. The tool will not choose between those, so absence is a skip. If you *know* the target must exist, set `"must_exist": true` on the probe and absence becomes a failure — one you declared, not one it inferred.

**It does not interpret meaning.** It evaluates declarative assertions against measured state. A checker that must decide what a sentence means produces false positives, and a checker with too many false positives becomes easy to ignore — at which point it is worse than none, because it consumes the attention it was meant to direct.

**It does not trust an old baseline.** A snapshot older than 24 hours (`--max-baseline-age`), one belonging to a different spec, or one already used by a previous `verify` is refused. Without that, a snapshot step that quietly stopped running leaves yesterday's baseline in place, and today's do-nothing job "grows" against it.

## Known limits, stated rather than discovered

- **`tree_digest` hashes contents up to `content_limit` (8 MB by default).** Larger files are compared by size and modification time only, and the report says how many. That is a weaker observation, and it is labelled as one.
- **`exclude` patterns are case-sensitive on every platform** (`fnmatchcase`). Plain `fnmatch` is case-insensitive on Windows, so an exclude tuned there would silently stop excluding on Linux.
- **`exclude` wildcards cross `/`.** `incoming/*` also matches `incoming/sub/deep.txt`.
- **A string `cmd` runs under the platform's shell** — `cmd.exe` or `/bin/sh`. Use the list form for specs that must work on both.
- **`sqlite_scalar` opens the database read-only and enforces it** with a quoted URI, `PRAGMA query_only`, and an authorizer that refuses `ATTACH`. The first version did none of that, and a spec could `DROP` a table through it while the tool reported "query returned no row" — the third incident in this archive, inside the tool built to detect it. Found by a reviewer, not by me, and not by the self-test.
- **`http_json` accepts `http` and `https` only.** `urlopen` would otherwise also accept `file:` and `data:`.
- **A timestamp in the future fails** rather than reading as maximally fresh, because a skewed clock on a network mount would otherwise make stale files look new.

## The self-test

`selftest` runs **57 cases** against a temporary directory: 46 that assert a run outcome, and 11 that assert a property of the tool itself.

Of those 46, only **12 may come out green**. The other 34 must not — 24 failures, 7 coverage gaps, 3 rejected specifications — and the self-test fails if any of them passes. Among them:

- a job that wrote *only a header row*, which a size threshold alone would wave through
- a source that died while the **total stayed high** because others covered for it
- **every** source dead, so there is no group left to be below a minimum
- a same-length edit with the modification time restored afterwards
- a neighbouring record overwritten, one deleted, and a new empty directory
- a 72-hour-old baseline, a baseline from another spec, and one already consumed
- `DROP TABLE` through three different read-only bypasses, and an `ATTACH` that must not create a file
- a probe that cannot be read, which must skip and must not pass
- one green assertion beside one unreadable one, which must come out **incomplete**

A suite that only demonstrates that green is reachable demonstrates nothing.

Nearly all of these exist because the behaviour was once wrong in exactly that way. Most were found by independent reviewers reading the first version — which passed its own twelve-case self-test.

## Licence

[Apache 2.0](../LICENSE).
