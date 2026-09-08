# The dashboard was full. The source had been dead for seven weeks.

*A post-mortem on a silent ingest failure. Status: unresolved.*

---

## What was supposed to happen

A scheduled job runs nightly against a public sector open-data API, pulls newly published documents, chunks them, embeds them, and writes them into a vector store. A second job crawls the same institution's HTML pages as a fallback for anything the API misses. A monitoring view shows, per source, how much data exists and how fresh it is.

Three layers: primary API, HTML fallback, freshness dashboard. On paper, no single failure should be able to take the source offline without someone noticing.

## What actually happened

Between the morning of 17 July and the small hours of 18 July 2026, the upstream open-data interface stopped delivering — both crawlers flipped inside that window. The endpoints answer plainly, and they say why:

```
/oparl/system          → HTTP 503
/oparl/1.0/system      → HTTP 503
/public/oparl/system   → HTTP 500
```

And the response phrase states the reason outright: *the module is deactivated.* Not a timeout, not a rate limit, not a move — the interface says what happened to it. Reproduced with a browser user-agent, with `Accept: application/json`, and against `/system`; the web interface itself still returns `200`, so it is the API that is off and not the system behind it.

The ingest kept running every night. It has been running ever since.

Here is what arrived in the store, counted per publication month directly against the vector database, filtered to the affected document type:

| Month | Chunks ingested |
|---|---|
| 2026-03 | 10,005 |
| 2026-04 | 2,933 |
| 2026-05 | 3,030 |
| 2026-06 | 1,975 |
| **2026-07** | **2** |
| **2026-08** | **4** |
| **2026-09** | **6** |

Two chunks in July. The source did not degrade. It stopped.

**Two things are visible in that table, and I want to separate them honestly.** The cliff between June and July is the incident: the API was switched off. The decline from March to June is something else — I have not established whether it reflects genuinely lower publication volume, a seasonal pattern, or an earlier problem I also missed. I do not know, so I am not claiming it.

## How long it went unnoticed, and why

Seven weeks. And the reason is the part worth reading.

It was not that nobody looked. The freshness dashboard existed precisely for this, and it was consulted. **The dashboard showed data arriving, because data was arriving — from a different source.** A second ingest, feeding press material into the same store, ran normally throughout. Monthly totals looked populated. Nothing was red.

The question anyone would naturally ask — *is data coming in?* — returned **yes**. It was the wrong question, and it had a comforting answer.

The right question was: *is data coming in **from this specific source**, at the rate this source has historically produced?* The dashboard could not answer that, because it aggregated.

There was a second reason, and it is more embarrassing: the dashboard tracked document volume but **not the calendar of the events that generate those documents**. Sessions kept happening. Documents kept being published on the institution's website. The system had no expectation to compare reality against — it only counted what it had.

## What the monitoring said at the time

Green, and worse than green.

Two of the three scheduled jobs **reported success**. The wrapper script did not pass the Python process's exit code through to the scheduler, and then unconditionally wrote a `DONE_<date>` marker to the log. The crawler was aborting nightly with a JSON decode error — it was receiving an error page where it expected an API response — and the scheduler recorded a clean run every single time.

The third job showed status `Ready` in the task scheduler while its last result was `SCHED_S_TASK_TERMINATED`. Status field and outcome field were decoupled, and only one of them was being read.

The HTML fallback, which existed for exactly this scenario, did not catch it either. It has its own gaps and did not compensate for the missing API.

### The safeguard was the thing that hid it

One job had a guard clause, and it is the detail I find hardest to argue with.

```python
if not sessions:
    return          # don't overwrite good data with an empty result
```

The intent is sound and I would write it again. A parser that returns nothing should not be allowed to wipe a working dataset; failing to write is safer than writing garbage.

What it did in practice: when the API went dark, the crawler received an error page, parsed zero items, hit the guard and returned. **The previous data stayed in place and continued to be served as current.** A dashboard fed from that store showed a calendar that looked entirely normal — and looked normal for a specific reason that no one could have predicted from the code: the frozen snapshot covered a summer recess, so its sparseness was exactly what a correct result would have looked like anyway.

Every cancellation, rescheduling and new entry after that date is missing from it. Nothing indicates that.

> A guard against writing bad data is also a guard against *noticing* that no data arrived.

I am not proposing to remove it. Both failure modes are real, and destroying a good dataset is worse than staling one. The point is that the safeguard **converted a loud failure into a quiet one**, and that this consequence was invisible at the point where the decision was made — the clause is three lines, obviously correct, and reviewed by anyone in about a second.

The fix is not to drop the guard. It is that **declining to write is an event**, and has to be reported as one. A skipped write is currently indistinguishable from a successful one in every log I keep. It should be the loudest line in the file.

## Root cause

There is a technical root cause and a design root cause, and only the second one is interesting.

**Technical:** the interface reported, in its own error message, that the module was disabled. That much is not inference — it is what the server says, unchanged on every endpoint seven weeks later. What was never established is **who disabled it, whether deliberately or as a side effect of a nightly update, and whether it was announced anywhere I did not look.**

I am spelling out that boundary because I got it wrong twice, in both directions. The version I nearly published said "decommissioned without notice," which assigns a decision and a failure to announce it to someone else on evidence I do not have. Told about that, I over-corrected and removed the part that *is* documented — the server's own statement — and replaced it with a hedge covering possibilities the evidence already excludes.

Both errors have the same shape: I described the state of the evidence from memory instead of reading it. **The correction is not "be more cautious." It is "look at what you actually have," which sometimes means claiming more.**

The lesson survives either way. The source went quiet and my monitoring did not notice. Who silenced it changes nothing about the second half.

**Design:** every layer of monitoring I had built measured *whether my process ran*, and none measured *whether my process had an effect*. Exit code, task status, aggregate row count — all three are properties of the machinery, not of the outcome. A pipeline that faithfully processes zero records is indistinguishable, by those measures, from one processing thousands.

## The damage was downstream, and that is the part worth copying

I originally wrote this up as "the dashboard was green while the source was dead." That is true and it is the weaker version.

The real cost showed up weeks later, while this very post-mortem was being reviewed. A downstream check — one that searches the archive for prior items overlapping a new one — reported **no overlap**. The answer was well-formed, confident and consistent with everything the system could see. It was also searching a corpus that effectively ended in June, because nothing had arrived since.

Nothing in the output said so. There is no field on a "no overlap" result that reads *based on data through June*. The check was not broken; it answered the question it was given, over the data it had.

> A silent input failure does not stay silent. It becomes a confident downstream answer that no one can identify as incomplete by looking at it.

That is why the check I now want is per-source rather than per-pipeline. An answer computed over stale inputs is not a smaller answer. It is a wrong one wearing the shape of a right one.

### The cheap half of the fix is not monitoring at all

A colleague reviewing this went looking for the same defect in outgoing documents rather than in pipelines, and found a countermeasure I would not have arrived at from the monitoring side.

You cannot attach a freshness field to a "no overlap" result. **You can put the date inside the claim.**

| Fragile | Durable |
|---|---|
| "no comparable item exists" | "no comparable item is known to us" |
| "no date has been set" | "as of 17 February, no date had been given" |
| "this has not been submitted" | "it has not reached us" |

The left column asserts a present state of the world, and goes false the moment the world moves — silently, with nothing in the sentence to warn a reader. The right column states a dated observation or the limit of one's own knowledge. It stays true regardless of what happened afterwards.

Their count: in two documents, most sentences were already immune because observations had been dated as a matter of habit. Three were not, and all three were rewritten in minutes at **no cost to the argument** — the qualified versions were, if anything, harder to rebut, because a stated limit of knowledge invites an answer while a false assertion of fact invites a correction that discredits everything around it.

> Where the freshness of your data cannot be guaranteed, write dated observations rather than present-tense states.

This is not a monitoring recommendation. It is a writing rule for anything an automated system derives from a corpus whose currency it does not itself guarantee, and it costs nothing to adopt. It also converges with something a second colleague arrived at independently while classifying notes: a claim about a *state* is worthless without the date it was checked, whereas a claim about a *property* does not need one. Same conclusion, one from drafting documents, one from building a checker.

## Current status: unresolved

I am not writing this from the comfortable side of a fix. As of 8 September 2026 the endpoints still return 503 and 500, and the gap is over two months wide.

What has changed: the two jobs that reported false success now fail with a real exit code, corrected on 4 September. That does not restore the data. It means the *next* outage will be visible on day one instead of day fifty.

The larger consequence surfaced only during the review of this write-up: work that relied on this corpus has, for two months, been running on data that ends in June. Nobody knew, because nothing indicated it. That is the actual cost of the incident, and it is larger than the missing rows.

## The rule I take from this

> **Success is an effect, not a return value.**

Concretely, three checks that would each have caught this within a day, and none of which I had:

**Per-source freshness, never aggregate.** A single dashboard number across sources hides the death of any one of them. Each source needs its own threshold, and the threshold belongs to the source's own rhythm — press material daily, documents weekly, event-driven material before each event. A global threshold is either too loud to be read or too quiet to be useful.

**An expectation to compare against.** Counting what arrived tells you nothing unless you know what should have arrived. Where the real world provides a calendar — sessions, publication schedules, filing deadlines — that calendar is the assertion. Without it, "we ingested six documents" is a number with no verdict attached.

**A dead-man's switch per source.** Not "did the job run" but "has this specific source produced anything within its own expected window". If a source that reliably yields hundreds of items a month yields two, that is an alert, even when every process exited zero.

## What I still do not know

Whether the decline from March to June has an explanation separate from the July cliff. Whether the HTML fallback would have worked had it been maintained, or whether it was structurally unable to substitute for the API. And whether there are other sources in the same system currently in the same condition — because the honest answer is that I would not necessarily know.

That last one is why the tool in this repository exists instead of another dashboard.
