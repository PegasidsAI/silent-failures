# Silent Failures

**How does an unattended system know that its picture of reality is correct?**

[![self-test](https://github.com/PegasidsAI/silent-failures/actions/workflows/self-test.yml/badge.svg)](https://github.com/PegasidsAI/silent-failures/actions/workflows/self-test.yml)

---

I run a small fleet of AI agents unattended — scheduled jobs, around the clock, on my own hardware, with a watchdog and an error channel. This repository is what I learned when they were confidently wrong.

Not spectacular failures. Quiet ones. The kind where every indicator stays green.

| What reported success | What was actually happening |
|---|---|
| Exit code 0, scheduler reported "success" | The upstream source had been switched off. **Seven weeks.** |
| **HTTP 200** on a `GET` | The diagnostic had already modified the state it was called to inspect |
| Task status: **"Ready"** | The script had never run. It produced nothing, every night, for weeks |

Three systems, three different ways of signalling success. **None of the three answered the question I actually needed answered.**

And then a fourth, which is not a machine at all:

> **My own documentation** described a fault that no longer existed. Two people acted on it before anyone measured.

---

## Four incidents, four different ways of being wrong

It would be tidier if these were four instances of one mistake. They are not, and the differences are the useful part.

Each incident contains more than one error. The table names the one I found most instructive, not the only one present.

| Incident | How the belief was wrong |
|---|---|
| Dead source, green dashboard | The signal was **true, but not about the thing in question**. Data was arriving — from somewhere else, and the total hid it. |
| Documentation reported success | The belief was **true at a different time**. A past measurement read as a present fact. |
| The read path was a write path | **Forming the belief changed the fact.** The instrument mutated what it was consulted about. |
| Ready, but never ran | **The validation excluded the failure mode**; the visible status field answered a different question. |

## The claim

> **Success is an effect, not a return value.**

That is the version that fits on a sticker, and it answers exactly one question: *did my action do anything?* Exit codes, HTTP status, task state and aggregate counts can all be accurate while answering a narrower question than the one you care about. A pipeline that faithfully processes zero records is indistinguishable, by every one of those measures, from one processing thousands.

**Process observability** tells you about the machinery. **Outcome observability** tells you whether the intended state of the world was reached. That distinction explains the first class of failures in this archive, but not all of them: in the read-path incident every operational signal was accurate, and in the documentation incident there was no live signal at all — only a remembered one. Which is why the question at the top of this page is the wider one, and the sticker is its first special case.

## The part I find harder to accept

> **A check is only useful to the extent that it is independent of the failure mode it is meant to detect.**

When the check and the work share an assumption, the check can confirm the same error. That is what happened in every incident here: the exit code shared the job's assumption that running meant producing; the documentation was the system's own account of itself; the list was read by the code path that changed it; and the manual test inherited the very override whose absence made the scheduled run fail.

This is *not* the claim that a system cannot check itself. It can — redundant sensors, separate implementations, checksums, cross-validation. The requirement is not externality. It is independence from the specific way the thing fails, and that is testable rather than philosophical.

The uncomfortable version showed up while preparing this repository. Three separate errors of mine were caught by someone other than me: an outdated success rate I was about to publish, an anonymisation that did not anonymise, and an unsourced statistic — inside the write-up about unsourced claims. **I caught none of those three myself.** A reviewer sharing your model may share your blind spot; what helped here was a different vantage point.

A fourth I did catch, and the reason is the point rather than the credit. My one-line summary of the fourth incident described the wrong mechanism — and not one I had invented. I had taken a real detail from the *first* incident, where a wrapper script genuinely did swallow an exit code, and attached it to the fourth from memory. That is why it survived several readings: it was not implausible, it was true somewhere else.

I found it only because writing the long form forced a line-by-line comparison against the original record. The check worked because it was independent of the failure: the failure was memory, and the check was the source.

It is also why, in my own setup, an unattended check reports *outward* — to a phone — rather than into a file that something would have to remember to read. That part is not in this repository; what is here is the checker, which prints and sets an exit code. **A system that grades its own homework has produced another return value.**

### The four, in reading order

1. [The dashboard was full. The source had been dead for seven weeks.](incidents/01-dead-source-green-dashboard.md) — *unresolved*
2. [My documentation reported success.](incidents/02-my-documentation-reported-success.md)
3. [The read path was a write path.](incidents/03-the-read-path-was-a-write-path.md)
4. [Ready, but it never ran.](incidents/04-ready-but-never-ran.md)

## Who this is for

You run agents or automations that nobody watches. Your dashboard is green. You have not verified in a while whether green means anything.

If you have never discovered that a green job had quietly stopped producing anything useful, you may not need this repository yet.

## What is in here

- **[`tool/`](tool/)** — [`effect_check.py`](tool/effect_check.py). Standard library only, no dependencies. `effect_check.py selftest` runs 49 cases. Of the 38 that assert a run outcome, only 9 may come out green; the other 29 must not, and the self-test fails if any of them passes. A suite that only demonstrates that green is reachable demonstrates nothing.
- **[`examples/minimal/`](examples/minimal/)** — two commands, two minutes. The same check against a job that works and a job that does nothing while printing the same log line and exiting zero.
- **[`incidents/`](incidents/)** — four post-mortems, one per failure. Fixed structure: what should have happened, what did, how long it went unnoticed, what the monitoring said at the time, root cause, the generalisable detection rule, and what I still do not know.

**Not here yet:** `measurement/`, the evaluation harness behind the two numbers further down. Those numbers are stated because I have measured them; the harness is absent because I have not packaged it, and it will appear when it exists rather than being described in advance.

That sentence is here because an earlier draft of this section described `tool/` and `measurement/` in the present tense when neither existed — the second incident in this archive, occurring on its own front page. The tool has since been written, which is why it has moved up one paragraph.

The checker has four primitives. Each is there because something broke:

> **No check type without an incident.**

It is the rule that keeps this from turning into a feature list — four real failures, four checks, nothing invented to round the set out.

| Primitive | The question it answers | Evidenced by |
|---|---|---|
| `effect` | Did my action change anything, or do I only have a return value? | Ready, but never ran; also dead source |
| `freshness` | Is this belief from now, or from then? | Dead source; documentation reported success |
| `diversity` | Is my picture complete, or am I seeing one corner and calling it the whole? | Documentation reported success |
| `invariant` | Did I change something I was not supposed to touch? | The read path was a write path |

That column is deliberately untidy. An earlier draft of it was a clean one-to-one mapping, four incidents to four checks, and it was clean because I had arranged it that way afterwards — the dead-source write-up argues for per-source freshness at least as strongly as for diversity, and the documentation write-up is where "measure diversity, not only volume" was actually first stated. The principle does not require a bijection. It only requires that no check is here without an incident behind it.

A fifth rule sits alongside the four, and it came from a colleague reviewing this: **every run reports its coverage, not only its findings.** A checker that verifies eleven assertions, silently skips five hundred and reports "no problems" has reproduced the first incident in this archive inside the tool meant to prevent it. A clean run with most of the work skipped is a red result, not a green one.

The last one is the rarest and the one I would fight for. Counting the records you touched tells you the operation ran; confirming that everything *outside* the working set is unchanged catches a class of failure the first check misses by construction.

## The numbers, both of them

The grounding layer I use — a system that must cite a source for every claim or explicitly stay silent — scores **94 % on the easy evaluation set** (46 questions) and **55 % on the adversarial one** (12 questions: false premises, conflict cases, answers distributed across documents, deliberately misleading context). Five runs each, on one build of the system.

**The second number is the meaningful one.** A repository arguing against demo theatre does not get to report only its flattering figure.

The run count is stated because I nearly published a different pair. The two figures I first wrote down came from **two different builds** — the flattering one from after a change, the adversarial one from before it. Quoted together they would have been a comparison of nothing. At n = 12, one question is more than eight percentage points, so 55 % is a coarse number and the spread across runs was 6 to 7 correct.

## What I am not claiming

I did not catch any of these on the day they happened. Several are still unresolved as of publication — where that is the case, it says so in the write-up.

Two candidate incidents were removed during review: one because the only evidence was a note in my own documentation rather than a measurement, one because describing the defence would have exposed a bypass that is not yet closed. **That review process is described too**, because it is the more useful part.

None of this generalises further than the evidence behind it. Four incidents in one small operating setup is not a study. Where I state a rule, it is a rule I now follow, not a finding about software in general.

## Back to the question

*How does an unattended system know that its picture of reality is correct?*

After four write-ups, my answer is: **it cannot certify that its picture is correct.** What it can do is test specific assumptions, through checks that are independent of the failure modes they are meant to detect.

That is smaller than certainty and it is worth having. A system can enumerate the ways it has already been wrong and test for those. That is all this repository is: four incidents, four checks. Each check catches the failure that produced it. **I make no claim about failure modes these checks have not been tested against** — a check may well surface something I have never seen, but its coverage there is unvalidated, and I would not treat a clean result as evidence about them.

So the four primitives are not a theory of correctness. They are a list of scars, and the list grows the way scars do.

Two things follow, and they are why I would rather have this than a monitoring product.

**A check has to be independent of the failure it is looking for.** Not external — independent. A second implementation, a different vantage point, a measurement taken by something that does not share the assumption under test. Where that independence is missing, the check will confirm the error rather than find it, and it will do so with the same confidence it would report a real pass.

**Something sufficiently independent has to close the loop.** In this archive, that was consistently a person. Three of my four errors were caught by someone else; the fourth by forcing a comparison against a record I had not written that day. None of them by the system reasoning about itself.

If there is a fifth incident somewhere in here, I have not found it. Given the record above, that is not evidence that there is none.

## Licence

[Apache 2.0](LICENSE). No production code, no client data, no third-party material. Everything rewritten from scratch.

*Maximilian Adams · adams@pegasids.ai*
