# The read path was a write path

*A post-mortem on a diagnostic that destroyed the evidence it was called to examine.*

---

## What was supposed to happen

A small internal service keeps a list of open items, one list per user, in a single JSON store. A `GET` endpoint returns the caller's list. A `POST` adds an item. Ordinary, boring, the kind of thing nobody audits.

`GET` reads. That is the entire contract, and it is so obvious that it is never written down.

## What actually happened

The `GET` handler loaded the store, applied a maintenance rule — drop completed items older than twenty-four hours — and then **wrote the whole store back to disk.** Every read. For every caller.

Two consequences, and the visible one was the smaller.

**The symptom people noticed:** completed items disappeared. Someone marked a task done, came back the next day, and it was gone. Annoying, explicable as a feature nobody remembered agreeing to.

**The part that actually mattered:** every read rewrote the *entire* file, including the lists belonging to everyone else. A read by one user was a full write across all users' data. Any concurrent modification, any partial write, any stale in-memory copy — all of it now had a blast radius covering the whole store, triggered by the most frequent and most innocuous operation in the service.

Nothing catastrophic happened. That is luck, not design.

## How it was found, and why that is the interesting part

Someone went looking for a missing entry.

The natural way to check whether an item is still in the list is to call the endpoint that returns the list. So they did — and that call silently applied the cleanup rule and wrote the result back.

**The diagnostic action was the damaging action.** Checking whether something was missing could make something else missing. A second investigation would no longer have been observing the same state as the first — and a fault that shifts between observations reads as intermittent rather than self-inflicted.

I want to be precise about the epistemics here, because this is the whole point: **any measurement taken through that endpoint was taken after the endpoint had already changed the thing being measured.** Not a race condition — an ordering guarantee, running the wrong way.

## What the monitoring said

Nothing, correctly. The endpoint returned `200`. It did exactly what its code said. There was no error, no exception, no failed write, no anomaly of any kind to detect.

This is the uncomfortable variant of the pattern that runs through this repository. In the other incidents, a signal claimed success while the effect was absent. Here **none of the signals being monitored was false.** The endpoint reported truthfully on the thing it had been asked about — whether the request completed. Nobody was measuring the thing that mattered, because nobody had asked: *does reading change anything?*

(An endpoint that mutates while presenting itself as a read is of course failing in a deeper sense. The point is narrower: no *operational* signal was wrong, so no amount of watching the operational signals would have surfaced it.)

## Root cause

**A maintenance operation was placed inside a read handler.**

The reasoning behind it is easy to reconstruct and entirely reasonable in isolation: the cleanup has to run somewhere, a scheduled job is extra machinery, and the read path executes often, so the cleanup will happen reliably. Every step of that is sensible. The result violates the one property of `GET` that the entire rest of the system depends on without checking.

Nobody decided that reads should mutate state. It emerged from a convenience.

## What was fixed

The automatic deletion was removed from the read path and the change committed. Reads now read.

What has not been fixed, and is honestly harder: there is no mechanism preventing the same thing from being reintroduced somewhere else. The change was found by inspection, not by a test.

## The rule I take from this

> **Before diagnosing a system, verify that the diagnostic does not write.**

This sounds like paranoia until it has cost you something. A useful first pass is one search: grep the read handlers for calls that persist state. It takes a minute and it is now the first step of any investigation I run — with the caveat that a write can hide behind a helper, an ORM hook, a cache layer or an event handler, so a clean grep is reassurance rather than proof.

Two further consequences, and the second one is the reason the tool exists at all:

**Read paths get an explicit contract.** If an endpoint mutates, it is not a read endpoint, whatever the HTTP verb says. Where a maintenance operation genuinely has to be opportunistic, it belongs behind an explicit call, not attached to whatever runs most frequently.

**Check what was *not* supposed to change.** This is the invariant I would have wanted, and the one I keep coming back to across incidents. Counting the records you touched tells you the operation ran. **An invariant over the untouched set would have caught this overwrite** — and it catches a class of failure that checking only the intended working set will miss by construction. Other approaches exist and are better in other ways: audit logs, content hashes, snapshot comparison, transactional guarantees. The invariant's advantage is that it is cheap enough to attach to every operation. A separate incident in a different system — a vector store where an identifier collision overwrote several thousand records while returning `200` — was found the same way: by someone counting the holdings *beside* the working set, not inside it.

That check is not expensive. It was not the check I instinctively wrote, because the natural instinct is to verify that the intended thing happened rather than that the unintended thing did not.

## What I still do not know

Whether the same pattern exists elsewhere in the same service. The fix was applied where the fault was found; no systematic sweep has been done.

How long it had been that way. The behaviour was noticed because it was annoying, not because it was detected, and I cannot reconstruct when the cleanup was first attached to the read path.

And whether any data was lost that nobody missed — which is, by construction, unanswerable.
