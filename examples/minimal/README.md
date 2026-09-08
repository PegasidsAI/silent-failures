# Two minutes, two runs

Run this from inside `examples/minimal/`. Nothing to install.

## 1. A job that does something

```bash
python ../../tool/effect_check.py run minimal.json -- python job.py
```

```
report written
COVERAGE: 2 of 2 assertions evaluated  —  nothing skipped

[minimal-example]  passed 2  failed 0  skipped 0

  ok    report-grew [effect] — grew by 24 (needed 5)
  ok    report-is-recent [freshness] — 0.0 h old (limit 1 h)

RESULT: PASSED
```

Exit code `0`.

## 2. The same check, against a job that does nothing

```bash
python ../../tool/effect_check.py run minimal.json -- python broken_job.py
```

```
report written
COVERAGE: 2 of 2 assertions evaluated  —  nothing skipped

[minimal-example]  passed 1  failed 1  skipped 0

  FAIL  report-grew [effect] — unchanged at 24 — the run produced no effect here
  ok    report-is-recent [freshness] — 0.0 h old (limit 1 h)

RESULT: FAILED
```

Exit code `1`.

## What just happened

Look at `broken_job.py`. It is eleven lines and it is not a strawman:

```python
rows = []                       # the upstream source returned nothing
if not rows:
    pass                        # nothing to write; do not clobber good data

print("report written")         # the log line still appears
```

That guard clause is *correct*. A parser that returns nothing should not be allowed to wipe a working dataset. It is also the shape of the first incident in this archive, where the same reasoning kept a stale calendar alive for seven weeks.

So on run 2:

- the job **printed the same log line** as on run 1
- the job **exited 0**
- a scheduler would record a clean run
- and nothing was produced

Everything that reports on the machinery said yes. The only thing that said no was the assertion about the world: *the report must gain at least one row.*

## One more thing worth noticing

The freshness check **passed on both runs**, and it should have. The file genuinely was recent — it had been written moments earlier, by the *previous* run. A single check answers a single question, and picking the wrong question is the failure mode this whole archive is about.

That is why `effect` and `freshness` are separate primitives, and why the report lists every assertion by name instead of collapsing them into one verdict.

## Try breaking it further

- Delete `out/report.csv` and run the broken job. Freshness now fails too, and says the target does not exist rather than pretending it is old.
- Change `delta_min` to `1000`. The real job now fails, because 24 bytes is not enough — the assertion is about *your* expectation, not about the file existing.
- Point the spec at a path that does not exist and remove `must_exist`. The run comes out **INCOMPLETE**, exit `2`, not green. An unreadable probe is never a pass.
