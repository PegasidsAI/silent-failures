# Ready, but it never ran

*A post-mortem on a scheduled job that failed every night for weeks, a status field that said nothing about it, and a manual test that could not have caught it.*

---

## What was supposed to happen

A nightly job on a Windows machine: run a script, write a log line, write a CSV. Unattended, no user logged in, triggered by the operating system's task scheduler.

I registered it, ran it once by hand to confirm it worked, and moved on. The scheduler's status column read **Ready**.

## What actually happened

The script never executed. Not once. Every scheduled run failed immediately, before a single line of it was interpreted. No log, no CSV, no partial output — nothing at all was produced for weeks.

Throughout, the status column continued to read **Ready**.

## The status field was not lying

This is the part worth being precise about, because "Ready" looks like a health indicator and is not one.

In this scheduler, `Status` has three values: **Running**, **Disabled**, **Ready**. `Ready` means *enabled and waiting for its next trigger*. It is a statement about the schedule, not about any run. A task that has failed a thousand consecutive times sits at `Ready` between failures, and it is correct to do so.

The outcome of the last run lives in a different field entirely — `LastTaskResult` — which is not shown in the same view and which nobody reads unless they already suspect something.

So the failure mode is not a false signal. It is **a true signal answering a question nobody asked.** I looked at the field that was visible and read it as the field I wanted.

## Why it failed

Two mechanisms, and the second is the one that made it invisible.

**1. The interpreter was blocked before the script ran.**

This machine had two PowerShell implementations installed: Windows PowerShell 5.1, which comes with the operating system, and PowerShell 7, which is a separate product installed alongside it. They keep their execution policies in **separate registry locations**:

```
HKLM\SOFTWARE\Microsoft\PowerShell\1\ShellIds\Microsoft.PowerShell   ← 5.1
HKLM\SOFTWARE\Microsoft\PowerShell\3\ShellIds\Microsoft.PowerShell   ← 7
```

An absent key here means one specific thing and not more: the `LocalMachine` scope is unset for that implementation. Execution policy resolves across several scopes in priority order, and a higher-priority scope can override what is or is not written here. Only when no scope is set anywhere does a Windows client fall back to `Restricted`.

The concrete finding on the incident machine: **the 5.1 `LocalMachine` policy was unset, and in the scheduled context the script was refused before execution.** The task invoked 5.1 as the system account, `LastTaskResult` was `1`, and no line of the script ran.

I am stating it that way deliberately, because I did not measure the effective policy *inside* the scheduler's context. The clean evidence would have been a task whose only job is to write `Get-ExecutionPolicy -List` and its environment to a file. I did not run it. What I measured is consistent with the explanation and does not prove it end to end.

The values are worth reading in full, because I checked a second machine while writing this and found **the mirror image**:

| | Machine A (the incident) | Machine B |
|---|---|---|
| 5.1 branch | *(key absent — LocalMachine unset)* | `RemoteSigned` |
| 7 branch | `RemoteSigned` | *(key absent — LocalMachine unset)* |
| Task account | system | user |

Same operating system, same intent, opposite configuration. Anything I concluded from machine A alone about "which interpreter is safe to use" was a statement about machine A.

**2. My manual test ran with a policy override the scheduled run did not have.**

My interactive session had a process-scoped policy override. When I invoked the 5.1 executable from that session to test, the child process **inherited the override through an environment variable**. It ran perfectly.

This is directly observable rather than inferred. In a session of the kind I was testing from:

```
Get-ExecutionPolicy -List  →  Process = Bypass
$env:PSExecutionPolicyPreference  →  Bypass
```

That environment variable is what a child process reads. The scheduler starts the same executable with the same arguments and does not set it.

> One difference stands out: my interactive session supplied a policy override that the scheduler did not.

The two environments differ in other ways too — account, profile, working directory, privileges — and I am not claiming they were otherwise identical. This is the difference that best explains the observed failure, and my test removed it. I did not measure the scheduler context end to end, so I am stating it as the best explanation rather than as the established cause.

## What the monitoring said

Nothing, and this time the reason is uncomfortable: the machine was monitored for *services being down*. A watchdog checked that the long-running processes were listening on their ports and restarted them when they were not.

A task that fails instantly does not go down. It has no port, no process to find missing, no unhealthy state to observe. It is *absent*, and absence is what the monitoring was structurally incapable of noticing — the watchdog answered "is this thing broken?", never "did this thing happen?".

## Root cause

**The verification shared an assumption with the failure.**

The assumption was: *my session and the scheduler's session are the same kind of environment.* Everything else follows from it. The manual test was not weak, or rushed, or careless. It was **not independent of the failure mode it was meant to detect**, and a check that is not independent can confirm the very error it is looking for.

That is the general form. The specific form is narrower and easier to remember: a green test from your own shell is evidence about your shell.

## What was fixed

The task was changed to invoke PowerShell 7, whose policy on this machine permits the script.

**What was deliberately not done:** passing an execution-policy bypass flag in the task's arguments. It would have worked. The reason to avoid it is narrower than it first appears: it overrides a machine-level setting rather than working within it, so the task keeps running only because it is exempt from a rule the rest of the machine follows. That is a maintenance liability, not a security finding.

I want to correct myself here, because my first draft of this paragraph claimed more. I wrote that the bypass flag is "the exact command shape that endpoint protection scores as hostile." Checking my own incident log, that is not what was measured. What was measured, on a different day and a different machine, was a block on this shape:

```
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command <inline code enumerating scheduled tasks with a name filter>
```

Scripts invoked as `-ExecutionPolicy Bypass -File <path>` on the same machine, the same day, on the same subject matter were **not** flagged.

What that evidence supports is narrower than a new explanation: **the flag alone was not sufficient to trigger the block.** It does not isolate which part of the surrounding shape was decisive — several things differed between the two invocations at once, and I never varied them one at a time. Replacing a wrong cause with a second plausible one would be the same mistake at a lower volume, so I am leaving it open.

**And a correction to my own conclusion.** Having found this, I wrote down the rule "never use 5.1 in a scheduled task." The table above is what happened when I checked it against a second machine: the rule was wrong. Several tasks there invoke 5.1 and succeed, because they run **as a user rather than as the system account** and that machine's 5.1 branch does have a policy set. The trap needs both conditions together. Applied as written, my rule would have caused rebuilds on machines where nothing was broken — and on machine B it would have pushed the task towards the interpreter that is actually unconfigured there.

I mention it because it is the same error as the incident, one level up: I formed a belief from a single observation and was about to act on it without an independent check.

## The rule I take from this

> **Register a job, then measure the artefact it is supposed to produce. Note the file's size before, compare after.**

Concretely, three steps. All three are evidence; only the third is evidence of the intended effect:

1. Read `LastTaskResult`, not `Status`. Different fields, different questions.
2. Trigger the task through the scheduler, never from your own shell. The scheduler is the environment under test.
3. Check that the output file exists, grew, and contains what it should.

Step 3 is the decisive evidence — **provided the assertion is specific to this run and its expected outcome.** Left vague, it is not decisive at all: the file may have existed already, been written by something else, grown without gaining anything useful, or survived unchanged from an earlier run. "The file is there" is a return value wearing a different hat.

## Why this incident produced the `effect` primitive

Of the four checks in the accompanying tool, this incident is the reason the first one is there — and the purest case for the archive's claim that success is an effect rather than a return value.

`effect` takes a declarative assertion about the world — *this file exists and grew by at least n bytes since the run started*, *this table gained rows*, *this endpoint now returns the new value* — and evaluates it **after** the scheduled run, in a process that is not the run. Nothing about the exit code, the status field, or the scheduler's own opinion enters into it.

It is also the reason `effect` takes an assertion instead of just checking file size. A job that writes a CSV header and then fails would grow the file and pass a size check. The assertion has to name the outcome, not a proxy for it.

## What I still do not know

**How many other tasks are in the same state.** I found this one because I happened to check its output. Both machines carry a number of scheduled jobs registered at different times; no systematic sweep has been done. The honest position is that I know of one instance, not that there was one instance.

**How long it had been failing.** The task was registered weeks before anyone looked at its output, and the scheduler's history was not retained far enough back to establish the first failure.

**Whether anything downstream had quietly adapted** to the missing output — read a stale file, skipped a step, defaulted to an empty set — in the way the dead-source incident did. I have not traced the consumers.
