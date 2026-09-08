#!/usr/bin/env python3
"""
effect_check — verify that a scheduled run had an effect, instead of asking it
whether it succeeded.

Four incidents in ../incidents/ produced four check types. Each is here because
something broke; none was invented to round the set out.

    effect      Did the action change anything, or do I only have a return value?
    freshness   Is this observation from now, or from then?
    diversity   Is the picture complete, or am I seeing one corner of it?
    invariant   Did something change that was not supposed to?

A fifth rule governs all four:

    EVERY RUN REPORTS ITS COVERAGE, NOT ONLY ITS FINDINGS.

A probe that cannot be read produces a SKIP. Never a pass. Skips are counted,
named, and make the run incomplete rather than green.

WARNING — A SPEC FILE IS A PROGRAM. The `command` probe executes what the spec
tells it to: as a shell line when given a string, without a shell when given a
list. Never run a spec you have not read, exactly as you would not run a
downloaded shell script.

USAGE

    effect_check.py snapshot spec.json --state run.json
    <the job runs>
    effect_check.py verify   spec.json --state run.json

    effect_check.py run spec.json --state run.json -- python nightly_ingest.py

    effect_check.py selftest

EXIT CODES

    0   every assertion evaluated, every assertion passed
    1   at least one assertion failed
    2   coverage incomplete — something could not be evaluated
    3   usage, specification, or internal error — NOTHING was evaluated

The exit code is a summary, not the evidence. Read the coverage line.

Standard library only. Python 3.7+.
"""

from __future__ import annotations

import argparse
import fnmatch
import glob as globmod
import hashlib
import json
import math
import os
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

VERSION = "1.1"

# How stale a recorded baseline may be before verify refuses to trust it.
# Without this, a snapshot step that quietly stopped running leaves yesterday's
# baseline in place, and today's do-nothing job "grows" against it. A green
# result for a job that did nothing is the exact failure this tool exists to
# catch, reproduced inside the tool.
DEFAULT_MAX_BASELINE_AGE_H = 24.0

# Files larger than this are digested by size and mtime instead of content.
# Hashing is what makes the invariant honest; a cap is what keeps it usable.
DIGEST_CONTENT_LIMIT = 8 * 1024 * 1024


class Unreadable(Exception):
    """The probe could not obtain a value. Not a failure — an absence of evidence."""


class SpecError(Exception):
    """The specification itself is wrong. Loud, immediate, and never a finding."""


class Missing:
    """A target that is absent, carried out of band.

    An earlier version signalled absence by returning a timestamp of 0.0. A real
    file whose mtime is the epoch then produced "target does not exist" — a false
    failure with a lying message. A sentinel inside the value space is a bug
    waiting for the value to occur.
    """
    __slots__ = ("path",)

    def __init__(self, path):
        self.path = path

    def __repr__(self):
        return "<missing %s>" % self.path


# --------------------------------------------------------------------------
# Probes
#
# `phase` is "snapshot" or "verify", and it matters. At snapshot time a missing
# file legitimately means zero — the job may be about to create it. At verify
# time a missing file is an absence of evidence, and reporting it as zero once
# allowed `expect: {equals: 0}` to pass against an unmounted volume.
# --------------------------------------------------------------------------

def _need(cfg, *keys):
    for k in keys:
        if k not in cfg:
            raise SpecError("probe %r requires %r" % (cfg.get("kind"), k))
    return [cfg[k] for k in keys]


def _absent(cfg, path, phase, zero_at_snapshot=False):
    """One place decides what a missing target means, so that all probes agree.

    They did not agree before: `must_exist` was honoured by file_mtime, ignored
    by file_text, and meaningless for file_size, which reported a missing file
    as the number 0.
    """
    if phase == "snapshot" and zero_at_snapshot:
        # Before the run, a missing file is legitimately zero — the job may be
        # about to create it. `must_exist` is a statement about the OUTCOME, so
        # enforcing it here would refuse every job whose whole purpose is to
        # produce the file, and the assertion would be skipped for the one case
        # it was written for.
        return 0
    if cfg.get("must_exist"):
        return Missing(path)
    raise Unreadable("file not found: %s" % path)


def probe_file_size(cfg, phase):
    (path,) = _need(cfg, "path")
    if not os.path.isfile(path):
        return _absent(cfg, path, phase, zero_at_snapshot=True)
    try:
        return os.path.getsize(path)
    except OSError as e:
        raise Unreadable("cannot stat %s: %s" % (path, e))


def probe_file_mtime(cfg, phase):
    (path,) = _need(cfg, "path")
    if not os.path.isfile(path):
        return _absent(cfg, path, phase)
    try:
        return os.path.getmtime(path)
    except OSError as e:
        raise Unreadable("cannot stat %s: %s" % (path, e))


def probe_glob_count(cfg, phase):
    (pattern,) = _need(cfg, "pattern")
    return len(globmod.glob(pattern, recursive=True))


def probe_file_text(cfg, phase):
    (path,) = _need(cfg, "path")
    limit = int(cfg.get("max_bytes", 4 * 1024 * 1024))
    if not os.path.isfile(path):
        return _absent(cfg, path, phase)
    try:
        with open(path, encoding=cfg.get("encoding", "utf-8"), errors="replace") as f:
            return f.read(limit)
    except OSError as e:
        raise Unreadable("cannot read %s: %s" % (path, e))


def _is_reparse(path):
    """Symlink, or on Windows a junction.

    os.walk does not follow symlinks, but it does follow junctions, and a
    junction loop turned one real file into thirty-eight entries across
    forty-five levels — an invariant that reported phantom changes.
    """
    try:
        if os.path.islink(path):
            return True
        st = os.lstat(path)
    except OSError:
        return True
    return bool(getattr(st, "st_reparse_tag", 0))


def probe_tree_digest(cfg, phase):
    """A digest over everything in a tree EXCEPT the working set.

    Content is hashed, not merely sized. Size plus a whole-second mtime missed
    an in-place edit of equal length within the same second, and missed any edit
    whose mtime was restored afterwards. Directories are recorded too, because
    creating or removing one is a change to the tree.
    """
    (root,) = _need(cfg, "root")
    if not os.path.isdir(root):
        raise Unreadable("directory not found: %s" % root)
    excludes = cfg.get("exclude", [])
    limit = int(cfg.get("content_limit", DIGEST_CONTENT_LIMIT))
    entries, hashed, sized = [], 0, 0

    def excluded(rel):
        # fnmatchcase, not fnmatch: fnmatch is case-insensitive on Windows and
        # case-sensitive elsewhere, so an exclude tuned on one platform silently
        # stops excluding on another.
        return any(fnmatch.fnmatchcase(rel, pat) for pat in excludes)

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if not _is_reparse(os.path.join(dirpath, d)))
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        if rel_dir != "." and not excluded(rel_dir):
            entries.append("D|" + rel_dir)
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if excluded(rel):
                continue
            try:
                st = os.stat(full)
            except OSError:
                entries.append("F|%s|GONE" % rel)
                continue
            if st.st_size <= limit:
                h = hashlib.sha256()
                try:
                    with open(full, "rb") as f:
                        for block in iter(lambda: f.read(1 << 20), b""):
                            h.update(block)
                except OSError:
                    entries.append("F|%s|UNREADABLE" % rel)
                    continue
                entries.append("F|%s|%d|%s" % (rel, st.st_size, h.hexdigest()))
                hashed += 1
            else:
                # Too large to hash on every run. Recorded honestly as the weaker
                # observation it is, and counted separately in the report.
                entries.append("F|%s|%d|%r|SIZE-ONLY" % (rel, st.st_size, st.st_mtime))
                sized += 1

    digest = hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()
    return {"digest": digest, "entries": len(entries),
            "hashed": hashed, "size_only": sized}


def _coerce_counts(pairs, only=None):
    """Turn (name, value) pairs into counts.

    `only` names the groups this assertion is about. Everything else is
    ignored. That distinction came from a real source: a status feed reported
    numbers for some contributors and null for others that were not wired up
    yet. Without `only`, the first null made the whole probe unreadable, so a
    genuine shortfall in a working contributor could never be reported.

    A null in a group the spec DID name stays unreadable, and that is the
    point: an absent number means the instrument is broken, which is a
    different finding from a contributor that has gone quiet. Confusing those
    two is what cost seven weeks in the first incident here.
    """
    out = {}
    for k, v in pairs:
        name = str(k)
        if only is not None and name not in only:
            continue
        try:
            out[name] = int(v)
        except (TypeError, ValueError):
            raise Unreadable(
                "group %r has no usable number (%r) — the measurement is broken, "
                "which is not the same as the group having gone quiet" % (name, v))
    if only is not None:
        fehlt = [g for g in only if g not in out]
        if fehlt:
            raise Unreadable("the measurement does not report on: %s" % ", ".join(sorted(fehlt)))
    return out


def probe_group_counts(cfg, phase):
    """Counts per group, for diversity. Never a total."""
    kind = cfg.get("source", "json")
    if kind == "json":
        path, pointer = _need(cfg, "path", "pointer")
        if not os.path.isfile(path):
            raise Unreadable("file not found: %s" % path)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise Unreadable("cannot parse %s: %s" % (path, e))
        for part in [p for p in pointer.split("/") if p]:
            if not isinstance(data, dict) or part not in data:
                raise Unreadable("pointer %r not found in %s" % (pointer, path))
            data = data[part]
        if not isinstance(data, dict):
            raise Unreadable("pointer %r is not an object of counts" % pointer)
        schluessel = cfg.get("value_key")
        if schluessel:
            # A status feed usually reports an OBJECT per contributor, not a bare
            # number. Naming the field here beats reshaping the feed, which would
            # mean a second copy of the truth.
            paare = []
            for name, eintrag in data.items():
                if not isinstance(eintrag, dict):
                    raise Unreadable("group %r is not an object, so %r cannot be read"
                                     % (name, schluessel))
                paare.append((name, eintrag.get(schluessel)))
            return _coerce_counts(paare, cfg.get("only"))
        return _coerce_counts(data.items(), cfg.get("only"))
    if kind == "glob":
        (groups,) = _need(cfg, "groups")
        if not isinstance(groups, dict):
            raise SpecError("group_counts source 'glob' needs an object of name -> pattern")
        return _coerce_counts(((name, len(globmod.glob(pat, recursive=True)))
                               for name, pat in groups.items()), cfg.get("only"))
    if kind == "sqlite":
        db, sql = _need(cfg, "db", "sql")
        try:
            con = _readonly_connect(db)
            rows = con.execute(sql).fetchall()
            con.close()
        except sqlite3.Error as e:
            raise Unreadable("sqlite: %s" % e)
        return _coerce_counts(((r[0], r[1]) for r in rows), cfg.get("only"))
    raise SpecError("unknown group source %r" % kind)


def _readonly_connect(db):
    """Open a database read-only, and mean it.

    Three things were needed, and an independent reviewer found the first two
    after the naive version had already been written and self-tested green:

    1. The path must be quoted. Interpolated straight into a URI, a '#' in the
       path swallows the query string and an appended '?mode=rwc&' overrides it.
       Either way the connection silently opens read-write.
    2. PRAGMA query_only — read-only in SQLite is a property of an attached
       database, not of the connection, so statements could still write.
    3. An authorizer refusing ATTACH. Without it, query_only still permits
       creating an empty file at the attach target.

    Before this, a spec could DROP a table through this function while the tool
    reported "query returned no row". That is the third incident in this archive
    occurring inside the tool written to detect it — and neither I nor the
    self-test found it, because both shared the assumption that naming a
    connection read-only made it so.
    """
    uri = "file:" + urllib.parse.quote(
        os.path.abspath(db).replace(os.sep, "/"), safe="/:") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.execute("PRAGMA query_only=ON")
    con.set_authorizer(
        lambda action, *a: sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_ATTACH else sqlite3.SQLITE_OK)
    return con


def probe_sqlite_scalar(cfg, phase):
    db, sql = _need(cfg, "db", "sql")
    try:
        con = _readonly_connect(db)
        row = con.execute(sql).fetchone()
        con.close()
    except sqlite3.Error as e:
        raise Unreadable("sqlite: %s" % e)
    if row is None:
        raise Unreadable("query returned no row")
    return row[0]


def probe_http_json(cfg, phase):
    (url,) = _need(cfg, "url")
    if not str(url).lower().startswith(("http://", "https://")):
        # urlopen also accepts file:, data: and ftp:. Without this check a spec
        # could read local files, or reach a metadata endpoint, through a probe
        # documented as an HTTP check.
        raise SpecError("http_json requires an http(s) URL, got %r" % url)
    limit = int(cfg.get("max_bytes", 10000000))
    req = urllib.request.Request(url, headers=cfg.get("headers", {}))
    try:
        with urllib.request.urlopen(req, timeout=cfg.get("timeout", 30)) as r:
            raw = r.read(limit + 1)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise Unreadable("http: %s" % e)
    if len(raw) > limit:
        raise Unreadable("response exceeds max_bytes (%d)" % limit)
    body = raw.decode("utf-8", "replace")
    if not cfg.get("pointer"):
        return body
    try:
        data = json.loads(body)
    except ValueError as e:
        raise Unreadable("response is not JSON: %s" % e)
    for part in [p for p in cfg["pointer"].split("/") if p]:
        if isinstance(data, list):
            try:
                data = data[int(part)]
            except (ValueError, IndexError):
                raise Unreadable("pointer element %r not in list" % part)
        elif isinstance(data, dict) and part in data:
            data = data[part]
        else:
            raise Unreadable("pointer %r not found" % cfg["pointer"])
    return data


def probe_command(cfg, phase):
    """Runs a command and takes its OUTPUT as the measurement.

    The return code is deliberately ignored: consulting it is the habit this
    program exists to break. Note that this probe executes arbitrary code — see
    the warning at the top of this file.
    """
    (cmd,) = _need(cfg, "cmd")
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=cfg.get("timeout", 120))
    except (OSError, subprocess.SubprocessError) as e:
        raise Unreadable("command: %s" % e)
    out = p.stdout.strip()
    if not out and not cfg.get("allow_empty"):
        # A command that died produces empty output, and an empty measurement
        # compared against an empty baseline reads as "unchanged". That is a
        # false pass built out of nothing at all. An empty result must be
        # declared by the spec, not inferred here.
        hinweis = (p.stderr or "").strip().splitlines()
        hinweis = (" — stderr: " + hinweis[-1][:160]) if hinweis else ""
        raise Unreadable("the command produced no output, so there is no "
                         "measurement (exit %s)%s" % (p.returncode, hinweis))
    return out


PROBES = {
    "file_size": probe_file_size,
    "file_mtime": probe_file_mtime,
    "file_text": probe_file_text,
    "glob_count": probe_glob_count,
    "tree_digest": probe_tree_digest,
    "group_counts": probe_group_counts,
    "sqlite_scalar": probe_sqlite_scalar,
    "http_json": probe_http_json,
    "command": probe_command,
}


def measure(cfg, phase):
    kind = cfg.get("kind")
    if kind not in PROBES:
        raise SpecError("unknown probe kind %r; known: %s"
                        % (kind, ", ".join(sorted(PROBES))))
    return PROBES[kind](cfg, phase)


# --------------------------------------------------------------------------
# Assertions
# --------------------------------------------------------------------------

class Result:
    __slots__ = ("cid", "ctype", "status", "detail")

    def __init__(self, cid, ctype, status, detail):
        self.cid, self.ctype, self.status, self.detail = cid, ctype, status, detail


EXPECT_KEYS = {
    "effect": {"delta_min", "delta_max", "unchanged", "min", "max",
               "contains", "not_contains", "equals"},
    "freshness": {"max_age_hours"},
    "diversity": {"each_min", "min_groups", "require_groups", "allow_empty"},
    "invariant": set(),
}

DELTA_KEYS = {"delta_min", "delta_max", "unchanged"}
ABSOLUTE_KEYS = {"min", "max", "contains", "not_contains", "equals"}


def needs_baseline(chk):
    if chk["type"] == "invariant":
        return True
    if chk["type"] != "effect":
        return False
    exp = chk.get("expect", {})
    if DELTA_KEYS & set(exp):
        return True
    if ABSOLUTE_KEYS & set(exp):
        return False        # answerable from the after-state alone
    return True             # nothing stated: the default reading is "it grew"


def _number(v, label):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        try:
            v = float(str(v).strip())
        except (TypeError, ValueError):
            raise Unreadable("%s is not numeric: %r" % (label, v))
    if isinstance(v, float) and not math.isfinite(v):
        raise Unreadable("%s is not a finite number: %r" % (label, v))
    return v


def _short(v, n=200):
    s = repr(v)
    return s if len(s) <= n else s[:n] + "... (%d chars)" % len(s)


def _same(a, b):
    """Equality that does not treat True as 1. `equals: 1` matching a measured
    True was a false pass hiding behind Python's numeric tower."""
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    return a == b


def check_effect(chk, before, after):
    """Did the action change anything?

    EVERY stated expectation must hold. An earlier version returned on the first
    key it recognised, so {"min": 5, "delta_min": 10} passed on the minimum
    while the job did nothing at all.
    """
    exp = chk.get("expect") or {"delta_min": 1}
    if isinstance(after, Missing):
        return "fail", "target does not exist, and the spec declares that it must: %s" % after.path
    failures, passes = [], []

    if "contains" in exp:
        needle = exp["contains"]
        text = after if isinstance(after, str) else str(after)
        if needle in text:
            passes.append("contains %r" % needle)
        else:
            failures.append("expected %r in the result; not present" % needle)
    if "not_contains" in exp:
        # A substring is only a valid success criterion if the FAILURE output is
        # known not to contain it. A reader described a probe that matched on
        # "Cipher is" to decide a TLS handshake had succeeded — a failed
        # handshake prints "New, (NONE), Cipher is (NONE)", which contains it.
        # It reported major sites as compliant for weeks. The success token was
        # a substring of the error line, so no amount of care in choosing the
        # needle could separate the two cases; only naming the failure marker
        # can. This tool had the same hole and no warning about it.
        needle = exp["not_contains"]
        text = after if isinstance(after, str) else str(after)
        if needle in text:
            failures.append("found %r, which marks the failure case" % needle)
        else:
            passes.append("does not contain %r" % needle)
    if "equals" in exp:
        if _same(after, exp["equals"]):
            passes.append("equals %r" % (exp["equals"],))
        else:
            failures.append("expected %r, measured %s" % (exp["equals"], _short(after)))
    if "min" in exp:
        a = _number(after, "measured value")
        if a >= exp["min"]:
            passes.append("%g >= %s" % (a, exp["min"]))
        else:
            failures.append("%g is below the required minimum %s" % (a, exp["min"]))
    if "max" in exp:
        a = _number(after, "measured value")
        if a <= exp["max"]:
            passes.append("%g <= %s" % (a, exp["max"]))
        else:
            failures.append("%g exceeds the permitted maximum %s" % (a, exp["max"]))

    if (DELTA_KEYS & set(exp)) or not (ABSOLUTE_KEYS & set(exp)):
        if before is None:
            raise Unreadable("no baseline recorded for this assertion")
        if isinstance(before, Missing):
            raise Unreadable("the baseline target did not exist")
        b = _number(before, "baseline")
        a = _number(after, "measured value")
        delta = a - b
        if exp.get("unchanged"):
            if delta == 0:
                passes.append("unchanged at %g, as required" % a)
            else:
                failures.append("changed by %+g, but was required to stay unchanged" % delta)
        else:
            need = exp.get("delta_min", 1)
            if delta >= need:
                passes.append("grew by %g (needed %g)" % (delta, need))
            elif delta == 0:
                failures.append("unchanged at %g — the run produced no effect here" % a)
            else:
                failures.append("changed by %+g, needed at least %g" % (delta, need))
            if "delta_max" in exp:
                if delta <= exp["delta_max"]:
                    passes.append("delta %g <= %s" % (delta, exp["delta_max"]))
                else:
                    failures.append("grew by %g, more than the permitted %s"
                                    % (delta, exp["delta_max"]))

    if failures:
        return "fail", "; ".join(failures)
    return "pass", "; ".join(passes)


def check_freshness(chk, before, after):
    """Is this from now, or from then?"""
    exp = chk.get("expect", {})
    if "max_age_hours" not in exp:
        raise SpecError("%s: freshness needs expect.max_age_hours" % chk["id"])
    if isinstance(after, Missing):
        return "fail", "target does not exist, and the spec declares that it must: %s" % after.path
    ts = _number(after, "timestamp")
    age_h = (time.time() - ts) / 3600.0
    limit = exp["max_age_hours"]
    if age_h < -1.0:
        # A timestamp in the future is a clock problem, not freshness. Passing it
        # would let a skewed network mount make stale files look new.
        return "fail", "timestamp is %.1f h in the future — check the clock, not the age" % -age_h
    if age_h <= limit:
        return "pass", "%.1f h old (limit %s h)" % (age_h, limit)
    return "fail", "%.1f h old, limit is %s h — stale, not absent" % (age_h, limit)


def check_diversity(chk, before, after):
    """Is the picture complete, or is a total covering for a dead contributor?"""
    exp = chk.get("expect", {})
    if not isinstance(after, dict):
        raise Unreadable("diversity needs per-group counts, got a single value")

    if not after:
        # No groups at all. An earlier version passed this: with nothing to
        # iterate there was nothing below the minimum, so the case where EVERY
        # contributor had died read as healthy. That is the first incident in
        # this archive, inside the check written for it.
        if exp.get("allow_empty"):
            return "pass", "no groups, and the spec permits that"
        return "fail", ("no groups at all — either every contributor stopped, or the "
                        "query no longer matches. Set expect.allow_empty if an empty "
                        "result is genuinely acceptable here.")

    each_min = exp.get("each_min", 1)
    problems = []
    starved = sorted(g for g, n in after.items() if n < each_min)
    if starved:
        problems.append("below %s: %s" % (each_min, ", ".join(starved)))
    if "min_groups" in exp and len(after) < exp["min_groups"]:
        problems.append("only %d groups, expected %s" % (len(after), exp["min_groups"]))
    for required in exp.get("require_groups", []):
        if required not in after:
            problems.append("group %r absent entirely" % required)
    total = sum(after.values())
    if problems:
        # Only say the total looks healthy when it actually would. The phrase
        # exists to name the trap — a comfortable sum hiding a dead
        # contributor — not to be printed over a sum that is plainly bad.
        deckt_zu = total >= each_min * max(len(after), 1)
        vorspann = ("total %d looks healthy, but " % total if deckt_zu
                    else "measured total %d; " % total)
        return "fail", vorspann + "; ".join(problems)
    return "pass", "%d groups, each >= %s (total %d)" % (len(after), each_min, total)


def check_invariant(chk, before, after):
    """Did something change that was not supposed to?"""
    if before is None:
        raise Unreadable("no baseline recorded for this assertion")
    if before == after:
        if isinstance(after, dict):
            n = after.get("entries", 0)
            noun = "entry" if n == 1 else "entries"
            extra = ""
            if after.get("size_only"):
                k = after["size_only"]
                extra = (", %d %s compared by size and mtime only, being above the "
                         "content limit" % (k, "file" if k == 1 else "files"))
            return "pass", "unchanged (%d %s outside the working set%s)" % (n, noun, extra)
        return "pass", "unchanged"
    if isinstance(before, dict) and isinstance(after, dict):
        d = after.get("entries", 0) - before.get("entries", 0)
        moved = "" if d == 0 else ", entry count %+d" % d
        return "fail", "changed outside the working set%s" % moved
    return "fail", "changed: %s -> %s" % (_short(before), _short(after))


CHECKERS = {
    "effect": check_effect,
    "freshness": check_freshness,
    "diversity": check_diversity,
    "invariant": check_invariant,
}


# --------------------------------------------------------------------------
# Spec loading — strict on purpose
# --------------------------------------------------------------------------

def load_spec(path):
    try:
        with open(path, encoding="utf-8") as f:
            spec = json.load(f)
    except OSError as e:
        raise SpecError("cannot read spec: %s" % e)
    except ValueError as e:
        raise SpecError("spec is not valid JSON: %s" % e)
    return validate_spec(spec)


def validate_spec(spec):
    """The checks a spec must survive, independent of where it came from.

    This lived inside load_spec, so a spec built in memory — by the self-test,
    or by any caller using this as a library — was never validated. Two of the
    validation rules were therefore untested, and both were broken.
    """
    if not isinstance(spec, dict):
        raise SpecError("spec must be a JSON object")
    checks = spec.get("checks")
    if not isinstance(checks, list) or not checks:
        raise SpecError("spec needs a non-empty 'checks' list")

    seen = set()
    for c in checks:
        if not isinstance(c, dict):
            raise SpecError("check is not an object: %r" % (c,))
        for key in ("id", "type", "probe"):
            if key not in c:
                raise SpecError("check is missing %r: %r" % (key, c))
        ctype = c["type"]
        if ctype not in CHECKERS:
            raise SpecError("%s: unknown type %r" % (c["id"], ctype))
        if c["id"] in seen:
            raise SpecError("duplicate check id %r" % c["id"])
        seen.add(c["id"])
        if not isinstance(c["probe"], dict):
            raise SpecError("%s: 'probe' must be an object" % c["id"])
        if c["probe"].get("kind") not in PROBES:
            raise SpecError("%s: unknown probe kind %r" % (c["id"], c["probe"].get("kind")))

        exp = c.get("expect", {})
        if not isinstance(exp, dict):
            raise SpecError("%s: 'expect' must be an object" % c["id"])
        # A mistyped key used to be ignored, and the check quietly became "it
        # grew by at least one" — passing while the intended threshold was never
        # tested. Unknown keys are an error now, not a shrug.
        unknown = set(exp) - EXPECT_KEYS[ctype]
        if unknown:
            raise SpecError(
                "%s: unknown expect key(s) for type %r: %s. Known: %s"
                % (c["id"], ctype, ", ".join(sorted(unknown)),
                   ", ".join(sorted(EXPECT_KEYS[ctype])) or "(none)"))
        for schluessel in ("contains", "not_contains"):
            if exp.get(schluessel) == "":
                raise SpecError("%s: expect.%s is empty, which matches everything"
                                % (c["id"], schluessel))
        if "contains" in exp and exp.get("contains") == exp.get("not_contains"):
            raise SpecError("%s: contains and not_contains are the same string, "
                            "which can never both hold" % c["id"])
        if "unchanged" in exp and (DELTA_KEYS & set(exp) - {"unchanged"}):
            raise SpecError("%s: 'unchanged' cannot be combined with a delta bound" % c["id"])
    return spec


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------

def snapshot(spec, state_path):
    base, skipped = {}, {}
    for chk in spec["checks"]:
        if not needs_baseline(chk):
            continue
        try:
            v = measure(chk["probe"], "snapshot")
            if isinstance(v, Missing):
                skipped[chk["id"]] = "target absent at snapshot: %s" % v.path
            else:
                base[chk["id"]] = v
        except Unreadable as e:
            skipped[chk["id"]] = str(e)
        except SpecError:
            raise
        except Exception as e:
            skipped[chk["id"]] = "probe raised %s: %s" % (type(e).__name__, e)
    payload = {"tool": VERSION, "started": time.time(),
               "spec_name": spec.get("name", ""), "baseline": base,
               "baseline_unreadable": skipped, "consumed": False}
    tmp = state_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, state_path)      # atomic: never a half-written baseline
    except OSError as e:
        raise SpecError("cannot write the baseline to %s: %s" % (state_path, e))
    return payload


def _load_state(spec, state_path, max_age_h):
    """Return (state, reason_it_cannot_be_used)."""
    if not state_path or not os.path.isfile(state_path):
        return {}, "no snapshot was taken before the run"
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError) as e:
        return {}, "snapshot file unreadable: %s" % e
    if not isinstance(state, dict):
        return {}, "snapshot file is not an object"
    if state.get("spec_name", "") != spec.get("name", ""):
        return {}, ("snapshot belongs to spec %r, not %r"
                    % (state.get("spec_name"), spec.get("name")))
    started = state.get("started")
    if not isinstance(started, (int, float)) or isinstance(started, bool):
        return {}, "snapshot has no usable timestamp"
    age_h = (time.time() - started) / 3600.0
    if age_h > max_age_h:
        return {}, ("snapshot is %.1f h old (limit %s h) — a stale baseline makes a "
                    "do-nothing run look like growth" % (age_h, max_age_h))
    if state.get("consumed"):
        return {}, ("snapshot was already used by an earlier verify; take a new one "
                    "before the next run")
    return state, None


def _mark_consumed(state_path):
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
        state["consumed"] = True
        tmp = state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, state_path)
    except (OSError, ValueError):
        pass


def verify(spec, state_path, max_age_h=DEFAULT_MAX_BASELINE_AGE_H):
    state, unusable = _load_state(spec, state_path, max_age_h)
    baseline = {} if unusable else state.get("baseline", {})
    missing_baseline = {} if unusable else state.get("baseline_unreadable", {})

    results = []
    for chk in spec["checks"]:
        cid, ctype = chk["id"], chk["type"]
        if needs_baseline(chk) and cid not in baseline:
            why = missing_baseline.get(cid) or unusable or "no baseline recorded"
            results.append(Result(cid, ctype, "skip", why))
            continue
        try:
            after = measure(chk["probe"], "verify")
            status, detail = CHECKERS[ctype](chk, baseline.get(cid), after)
        except Unreadable as e:
            results.append(Result(cid, ctype, "skip", str(e)))
            continue
        except SpecError:
            raise
        except Exception as e:
            # An unexpected exception used to escape here and kill the run with
            # exit 1 — indistinguishable, to a caller, from "an assertion
            # failed" — and no coverage line was printed at all.
            results.append(Result(cid, ctype, "skip",
                                  "probe or check raised %s: %s" % (type(e).__name__, e)))
            continue
        results.append(Result(cid, ctype, status, detail))

    if state_path and not unusable and os.path.isfile(state_path):
        _mark_consumed(state_path)
    return results, state


def report(spec, results, state):
    total = len(results)
    passed = [r for r in results if r.status == "pass"]
    failed = [r for r in results if r.status == "fail"]
    skipped = [r for r in results if r.status == "skip"]
    evaluated = len(passed) + len(failed)

    lines = ["COVERAGE: %d of %d assertions evaluated%s"
             % (evaluated, total,
                ("  —  %d NOT EVALUATED" % len(skipped)) if skipped
                else "  —  nothing skipped")]
    if skipped:
        lines.append("   A skipped assertion is not a passing one. This run is incomplete.")
    lines.append("")
    lines.append("[%s]  passed %d  failed %d  skipped %d"
                 % (spec.get("name", "unnamed"), len(passed), len(failed), len(skipped)))
    if state.get("started"):
        lines.append("   baseline taken %.1f min ago" % ((time.time() - state["started"]) / 60))
    lines.append("")
    for r in skipped:
        lines.append("  SKIP  %s [%s] — %s" % (r.cid, r.ctype, r.detail))
    for r in failed:
        lines.append("  FAIL  %s [%s] — %s" % (r.cid, r.ctype, r.detail))
    for r in passed:
        lines.append("  ok    %s [%s] — %s" % (r.cid, r.ctype, r.detail))
    lines.append("")

    if failed:
        verdict, code = "FAILED", 1
    elif skipped:
        verdict, code = "INCOMPLETE — coverage gap, not a clean run", 2
    else:
        verdict, code = "PASSED", 0
    lines.append("RESULT: " + verdict)
    return "\n".join(lines), code


# --------------------------------------------------------------------------
# Self-test
#
# Every case here that must NOT come out green exists because that exact false
# result was once produced — by the first draft, or by a reviewer who broke it.
# No test case without an incident either.
# --------------------------------------------------------------------------

def selftest():
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="effect_check_")
    faults = []
    ran = [0]

    def write(rel, text):
        p = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return p

    def case(label, spec, mutate, want, prepare=None):
        ran[0] += 1
        state = os.path.join(tmp, "state.json")
        if os.path.exists(state):
            os.remove(state)
        results = []
        try:
            if prepare:
                prepare()
            validate_spec(spec)
            snapshot(spec, state)
            mutate()
            results, st = verify(spec, state)
            _, code = report(spec, results, st)
        except SpecError as e:
            code, results = 3, [Result("-", "-", "specerror", str(e))]
        got = {0: "pass", 1: "fail", 2: "skip", 3: "specerror"}[code]
        ok = got == want
        print("  [%s] %s: expected %s, got %s" % ("ok " if ok else "BAD", label, want, got))
        if not ok:
            faults.append(label)
            for r in results:
                print("          %s %s: %s" % (r.status, r.cid, r.detail))

    def wrapper_fall(label, zeilen, want, must_exist=False):
        """Exercise the whole `run` branch, including what it returns.

        A reviewer refused to use `run` around a nightly job because an earlier
        version returned only the verify result: the job could crash, the
        assertions could be green, and the task reported success. That is the
        wrapper from the first incident in this archive, one layer up.
        """
        ran[0] += 1
        job = os.path.join(tmp, "job_%d.py" % ran[0])
        with open(job, "w", encoding="utf-8", newline=chr(10)) as f:
            f.write(chr(10).join(zeilen) + chr(10))
        ziel = os.path.join(tmp, "wrap_%d.txt" % ran[0])
        spec = {"name": "wrap", "checks": [{
            "id": "grew", "type": "effect",
            "probe": {"kind": "file_size", "path": ziel, "must_exist": must_exist},
            "expect": {"delta_min": 1}}]}
        spec_pfad = os.path.join(tmp, "wrap_%d.json" % ran[0])
        with open(spec_pfad, "w", encoding="utf-8", newline=chr(10)) as f:
            json.dump(spec, f)
        code = main(["run", spec_pfad, "--state", os.path.join(tmp, "wrap.state.json"),
                     "--", sys.executable, job, ziel])
        # Same vocabulary as every other case, so the tallies can be counted in
        # one pass. An earlier version printed "expected exit 1", which put these
        # cases in a bucket of their own and made the documented breakdown wrong
        # — caught by the CI job that compares the prose against a real run.
        wort = {0: "pass", 1: "fail", 2: "skip", 3: "specerror"}
        ok = code == want
        print("  [%s] %s: expected %s, got %s"
              % ("ok " if ok else "BAD", label, wort.get(want, want), wort.get(code, code)))
        if not ok:
            faults.append(label)

    def claim(label, condition):
        ran[0] += 1
        print("  [%s] %s" % ("ok " if condition else "BAD", label))
        if not condition:
            faults.append(label)

    print("effect_check %s — self-test in %s" % (VERSION, tmp))
    try:
        out = write("out/data.csv", "header\n")

        def S(**kw):
            return {"name": "t", "checks": [dict(id="c", **kw)]}

        print("\n effect")
        sp = S(type="effect", probe={"kind": "file_size", "path": out},
               expect={"delta_min": 50})
        case("job wrote rows", sp,
             lambda: write("out/data.csv", "header\n" + "x" * 200), "pass",
             prepare=lambda: write("out/data.csv", "header\n"))
        case("job wrote NOTHING", sp, lambda: None, "fail",
             prepare=lambda: write("out/data.csv", "header\n"))
        case("job wrote only a header — a size proxy alone would wave this through", sp,
             lambda: write("out/data.csv", "header\ncol\n"), "fail",
             prepare=lambda: write("out/data.csv", "header\n"))
        case("every constraint is evaluated, not just the first",
             S(type="effect", probe={"kind": "file_size", "path": out},
               expect={"min": 5, "delta_min": 10}), lambda: None, "fail")
        case("max is enforced, not merely documented",
             S(type="effect", probe={"kind": "file_size", "path": out},
               expect={"min": 5, "max": 10}), lambda: None, "fail")
        case("delta_max catches runaway growth",
             S(type="effect", probe={"kind": "file_size", "path": out},
               expect={"delta_max": 5}),
             lambda: write("out/data.csv", "y" * 900), "fail")
        case("unchanged means unchanged",
             S(type="effect", probe={"kind": "file_size", "path": out},
               expect={"unchanged": True}),
             lambda: write("out/data.csv", "z" * 4000), "fail")
        case("unchanged passes when the value truly did not move",
             S(type="effect", probe={"kind": "file_size", "path": out},
               expect={"unchanged": True}), lambda: None, "pass")
        case("a mistyped expect key is an error, not a shrug",
             S(type="effect", probe={"kind": "file_size", "path": out},
               expect={"minimum": 500}), lambda: None, "specerror")
        case("an empty 'contains' is rejected",
             S(type="effect", probe={"kind": "file_text", "path": out},
               expect={"contains": ""}), lambda: None, "specerror")
        FEHLZEILE = "New, (NONE), Cipher is (NONE)\n"
        ECHTZEILE = "New, TLSv1.3, Cipher is TLS_AES_256_GCM_SHA384\n"
        log = write("out/handshake.log", FEHLZEILE)
        case("a needle that also appears in the failure output passes on its own",
             S(type="effect", probe={"kind": "file_text", "path": log},
               expect={"contains": "Cipher is"}), lambda: None, "pass")
        case("naming the failure marker separates the two cases",
             S(type="effect", probe={"kind": "file_text", "path": log},
               expect={"contains": "Cipher is", "not_contains": "(NONE)"}),
             lambda: None, "fail",
             prepare=lambda: write("out/handshake.log", FEHLZEILE))
        case("and the same assertion passes on a genuine success line",
             S(type="effect", probe={"kind": "file_text", "path": log},
               expect={"contains": "Cipher is", "not_contains": "(NONE)"}),
             lambda: write("out/handshake.log", ECHTZEILE), "pass")
        case("contains and not_contains identical is rejected",
             S(type="effect", probe={"kind": "file_text", "path": log},
               expect={"contains": "x", "not_contains": "x"}), lambda: None, "specerror")
        gone = os.path.join(tmp, "out", "absent.csv")
        case("a missing file is not a measured zero at verify time",
             S(type="effect", probe={"kind": "file_size", "path": gone},
               expect={"equals": 0}), lambda: None, "skip")

        print("\n freshness")
        case("written just now",
             S(type="freshness", probe={"kind": "file_mtime", "path": out},
               expect={"max_age_hours": 24}),
             lambda: write("out/data.csv", "fresh\n"), "pass")
        case("three days old",
             S(type="freshness", probe={"kind": "file_mtime", "path": out},
               expect={"max_age_hours": 24}),
             lambda: os.utime(out, (time.time() - 3 * 86400,) * 2), "fail")
        case("an unreadable target skips, and never passes",
             S(type="freshness", probe={"kind": "file_mtime", "path": gone},
               expect={"max_age_hours": 24}), lambda: None, "skip")
        case("a declared-missing target fails",
             S(type="freshness", probe={"kind": "file_mtime", "path": gone,
                                        "must_exist": True},
               expect={"max_age_hours": 24}), lambda: None, "fail")
        epoch = write("out/epoch.txt", "old")
        case("a real file with an epoch mtime is stale, not 'missing'",
             S(type="freshness", probe={"kind": "file_mtime", "path": epoch,
                                        "must_exist": True},
               expect={"max_age_hours": 24}),
             lambda: os.utime(epoch, (0, 0)), "fail")
        future = write("out/future.txt", "soon")
        case("a timestamp from the future is a clock fault, not freshness",
             S(type="freshness", probe={"kind": "file_mtime", "path": future},
               expect={"max_age_hours": 24}),
             lambda: os.utime(future, (time.time() + 5 * 86400,) * 2), "fail")

        print("\n diversity")
        counts = write("out/sources.json",
                       json.dumps({"per_source": {"a": 90, "b": 40, "c": 30}}))
        spd = S(type="diversity",
                probe={"kind": "group_counts", "source": "json", "path": counts,
                       "pointer": "per_source"},
                expect={"each_min": 5, "min_groups": 3})
        case("all three sources delivering", spd, lambda: None, "pass")
        case("one source dead while the TOTAL stays high", spd,
             lambda: write("out/sources.json",
                           json.dumps({"per_source": {"a": 155, "b": 5, "c": 0}})), "fail",
             prepare=lambda: write("out/sources.json",
                                   json.dumps({"per_source": {"a": 90, "b": 40, "c": 30}})))
        teil = write("out/teilweise.json", json.dumps(
            {"per_source": {"a": 90, "b": 2, "c": None, "d": None}}))
        case("a null in an unnamed group must not poison the named ones",
             S(type="diversity",
               probe={"kind": "group_counts", "source": "json", "path": teil,
                      "pointer": "per_source", "only": ["a", "b"]},
               expect={"each_min": 5}), lambda: None, "fail")
        case("a null in a NAMED group is a broken instrument, not a quiet source",
             S(type="diversity",
               probe={"kind": "group_counts", "source": "json", "path": teil,
                      "pointer": "per_source", "only": ["a", "c"]},
               expect={"each_min": 5}), lambda: None, "skip")
        case("a named group the measurement omits entirely is also a skip",
             S(type="diversity",
               probe={"kind": "group_counts", "source": "json", "path": teil,
                      "pointer": "per_source", "only": ["a", "gibt_es_nicht"]},
               expect={"each_min": 5}), lambda: None, "skip")
        verschachtelt = write("out/status.json", json.dumps({"quellen": {
            "a": {"zuwachs_30d": 90, "ampel": "gruen"},
            "b": {"zuwachs_30d": 2, "ampel": "rot"},
            "c": {"zuwachs_30d": None, "ampel": "rot"}}}))
        case("a value_key reads one field out of each group's object",
             S(type="diversity",
               probe={"kind": "group_counts", "source": "json", "path": verschachtelt,
                      "pointer": "quellen", "value_key": "zuwachs_30d",
                      "only": ["a", "b"]},
               expect={"each_min": 50}), lambda: None, "fail")
        case("EVERY source dead — no groups at all",
             S(type="diversity",
               probe={"kind": "group_counts", "source": "json", "path": counts,
                      "pointer": "per_source"}, expect={"each_min": 5}),
             lambda: write("out/sources.json", json.dumps({"per_source": {}})), "fail")

        print("\n invariant")
        tree = os.path.join(tmp, "store")
        write("store/keep1.txt", "one")
        write("store/keep2.txt", "two")
        write("store/working.txt", "mine")
        spi = S(type="invariant",
                probe={"kind": "tree_digest", "root": tree, "exclude": ["working.txt"]})
        case("only the working file touched", spi,
             lambda: write("store/working.txt", "mine, rewritten at some length"), "pass")
        case("a neighbouring record overwritten", spi,
             lambda: write("store/keep1.txt", "clobbered"), "fail",
             prepare=lambda: write("store/keep1.txt", "one"))
        case("a neighbouring record deleted", spi,
             lambda: os.remove(os.path.join(tree, "keep2.txt")), "fail",
             prepare=lambda: write("store/keep2.txt", "two"))
        case("a same-length edit with the mtime restored", spi,
             lambda: (write("store/keep1.txt", "ONE"),
                      os.utime(os.path.join(tree, "keep1.txt"), (1000, 1000))), "fail",
             prepare=lambda: write("store/keep1.txt", "one"))
        case("a new empty directory is a change too", spi,
             lambda: os.makedirs(os.path.join(tree, "new_dir"), exist_ok=True), "fail",
             prepare=lambda: write("store/keep1.txt", "one"))
        if os.path.isdir(os.path.join(tree, "new_dir")):
            os.rmdir(os.path.join(tree, "new_dir"))

        print("\n other probes")
        db = os.path.join(tmp, "store.db")
        con = sqlite3.connect(db)
        con.execute("create table chunks(source text)")
        con.executemany("insert into chunks values(?)",
                        [("oparl",)] * 9 + [("html",)] * 4)
        con.commit()
        con.close()
        case("sqlite per-source counts, one source starved",
             S(type="diversity",
               probe={"kind": "group_counts", "source": "sqlite", "db": db,
                      "sql": "select source, count(*) from chunks group by source"},
               expect={"each_min": 5, "require_groups": ["oparl", "html"]}),
             lambda: None, "fail")
        case("a command that produces nothing gives no measurement, not an empty one",
             S(type="effect",
               probe={"kind": "command",
                      "cmd": [sys.executable, "-c",
                              "import sys; sys.stderr.write('boom'); sys.exit(3)"]},
               expect={"contains": "rows"}), lambda: None, "skip")
        case("an empty result declared by the spec is accepted",
             S(type="effect",
               probe={"kind": "command", "allow_empty": True,
                      "cmd": [sys.executable, "-c", "pass"]},
               expect={"equals": ""}), lambda: None, "pass")
        case("a command's OUTPUT is the measurement, not its exit code",
             S(type="effect",
               probe={"kind": "command",
                      "cmd": [sys.executable, "-c",
                              "import sys; print('rows=7'); sys.exit(9)"]},
               expect={"contains": "rows=7"}), lambda: None, "pass")

        print("\n coverage and state")
        case("one green beside one unreadable is INCOMPLETE, not green",
             {"name": "t", "checks": [
                 {"id": "grew", "type": "effect",
                  "probe": {"kind": "file_size", "path": out}, "expect": {"delta_min": 1}},
                 {"id": "unreadable", "type": "freshness",
                  "probe": {"kind": "file_mtime", "path": gone},
                  "expect": {"max_age_hours": 24}}]},
             lambda: write("out/data.csv", "x" * 5000), "skip")

        stale = S(type="effect", probe={"kind": "file_size", "path": out},
                  expect={"delta_min": 1})
        st = os.path.join(tmp, "stale.json")
        snapshot(stale, st)
        with open(st, encoding="utf-8") as f:
            s = json.load(f)
        s["started"] = time.time() - 72 * 3600
        s["baseline"]["c"] = 1
        with open(st, "w", encoding="utf-8", newline="\n") as f:
            json.dump(s, f)
        _, code = report(stale, *reversed(list(reversed(verify(stale, st)))))
        claim("a 72 h old baseline is refused rather than trusted", code == 2)

        other = {"name": "someone-else", "checks": stale["checks"]}
        st2 = os.path.join(tmp, "other.json")
        snapshot(other, st2)
        r2, s2 = verify(stale, st2)
        _, code = report(stale, r2, s2)
        claim("a baseline belonging to another spec is refused", code == 2)

        st3 = os.path.join(tmp, "once.json")
        reuse = S(type="effect", probe={"kind": "file_size", "path": out},
                  expect={"delta_min": 1})
        snapshot(reuse, st3)
        write("out/data.csv", "x" * 9000)
        verify(reuse, st3)
        r3, s3 = verify(reuse, st3)
        _, code = report(reuse, r3, s3)
        claim("a baseline already consumed is not silently reused", code == 2)

        print("\n read-only really means read-only")
        vic = os.path.join(tmp, "victim.db")
        con = sqlite3.connect(vic)
        con.execute("create table canary(x)")
        con.execute("insert into canary values(1)")
        con.commit()
        con.close()

        def canary_alive():
            c = sqlite3.connect(vic)
            n = c.execute("select count(*) from sqlite_master "
                          "where name='canary'").fetchone()[0]
            c.close()
            return bool(n)

        for label, dbpath in [("a plain path", vic),
                              ("a '#' fragment", vic + "#"),
                              ("an appended '?mode=rwc&'", vic + "?mode=rwc&")]:
            try:
                probe_sqlite_scalar({"kind": "sqlite_scalar", "db": dbpath,
                                     "sql": "drop table canary"}, "verify")
            except Exception:
                pass
            claim("DROP through %s did not take effect" % label, canary_alive())

        target = os.path.join(tmp, "should_not_exist.db")
        try:
            probe_sqlite_scalar({"kind": "sqlite_scalar", "db": vic,
                                 "sql": "attach '%s' as v" % target.replace("\\", "/")},
                                "verify")
        except Exception:
            pass
        claim("ATTACH did not create a file on disk", not os.path.exists(target))

        print("\n input handling")
        SCHREIBT = ["import sys",
                    "open(sys.argv[1], 'a', encoding='utf-8').write('x' * 50)"]
        wrapper_fall("job works and the assertion holds",
                     SCHREIBT + ["sys.exit(0)"], 0)
        wrapper_fall("job wrote correctly but exited non-zero",
                     SCHREIBT + ["sys.exit(7)"], 1)
        # Two readings of the same run, and the difference is declared by the
        # spec rather than guessed by the tool: without must_exist an absent
        # target is ambiguous (the job may not have run, the path may be wrong,
        # the volume may not be mounted), so it is a coverage gap. With it, the
        # author has said the file must be there, and absence is a failure.
        wrapper_fall("job exited zero and produced nothing — ambiguous, so incomplete",
                     ["import sys", "sys.exit(0)"], 2)
        wrapper_fall("same run, but the spec declares the target must exist",
                     ["import sys", "sys.exit(0)"], 1, must_exist=True)

        try:
            probe_http_json({"kind": "http_json", "url": "file:///etc/hosts"}, "verify")
            claim("a file:// URL is refused", False)
        except SpecError:
            claim("a file:// URL is refused", True)
        for bad, label in [("[]", "a spec that is a JSON list"),
                           ('{"checks":[1]}', "a check that is not an object"),
                           ('{"checks":[]}', "an empty checks list")]:
            p = write("bad.json", bad)
            try:
                load_spec(p)
                claim("%s is rejected" % label, False)
            except SpecError:
                claim("%s is rejected" % label, True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if faults:
        print("SELF-TEST FAILED: %d of %d case(s): %s" % (len(faults), ran[0], ", ".join(faults)))
        return 1
    print("SELF-TEST PASSED — %d cases, including every one that had to fail." % ran[0])
    return 0


# --------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse exits 2 on a usage error, which collides with this tool's
        # exit 2 = "coverage incomplete". A cron wrapper that mistypes the
        # subcommand would otherwise read the typo as an incomplete run.
        self.print_usage(sys.stderr)
        sys.stderr.write("USAGE ERROR: %s\n" % message)
        raise SystemExit(3)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    # argparse.REMAINDER swallows options that follow the spec, so
    #   run spec.json --state s.json -- cmd
    # once put '--state' into the command list and wrote the state elsewhere.
    # It also stripped every '--', turning `git checkout -- file` into a
    # different command. The split happens here instead, on the first bare '--'.
    command = []
    if "--" in argv:
        i = argv.index("--")
        command, argv = argv[i + 1:], argv[:i]

    ap = _Parser(prog="effect_check",
                 description="Verify that a run had an effect, instead of asking "
                             "whether it succeeded.")
    ap.add_argument("--max-baseline-age", type=float, default=DEFAULT_MAX_BASELINE_AGE_H,
                    metavar="H", help="refuse a baseline older than this many hours")
    sub = ap.add_subparsers(dest="cmd")
    for name, helptext in [("snapshot", "record before-values"),
                           ("verify", "evaluate assertions against the recorded values"),
                           ("run", "snapshot, run the command after --, then verify")]:
        p = sub.add_parser(name, help=helptext)
        p.add_argument("spec")
        p.add_argument("--state")
    sub.add_parser("selftest", help="run the built-in cases, including the failing ones")

    args = ap.parse_args(argv)
    if not args.cmd:
        ap.error("a subcommand is required")
    if args.cmd == "selftest":
        return selftest()

    try:
        spec = load_spec(args.spec)
        state_path = args.state or os.path.splitext(args.spec)[0] + ".state.json"

        if args.cmd == "snapshot":
            snapshot(spec, state_path)
            print("baseline written to %s" % state_path)
            return 0

        if args.cmd == "run":
            if not command:
                print("USAGE ERROR: no command given after --", file=sys.stderr)
                return 3
            snapshot(spec, state_path)
            try:
                rc = subprocess.run(command).returncode
            except OSError as e:
                # The job could not even start. Say so, and remove the fresh
                # baseline rather than leave one that would make the next run
                # look like growth.
                try:
                    os.remove(state_path)
                except OSError:
                    pass
                print("COMMAND ERROR: could not start %r: %s" % (command, e), file=sys.stderr)
                return 3
            results, state = verify(spec, state_path, args.max_baseline_age)
            text, code = report(spec, results, state)
            print(text)
            # The asymmetry is the whole point, and an earlier version got it
            # half right. A zero exit proves nothing, so it must not count as
            # success. A NON-zero exit is the job saying it did not finish, and
            # swallowing that turns this wrapper into exactly the thing the
            # first incident here describes: a wrapper that dropped the exit
            # code and reported a clean run regardless. A reviewer refused to
            # use `run` for that reason, and he was right.
            if rc != 0:
                print("\nThe command itself exited %d. Taken as a failure: a zero exit "
                      "proves nothing, but a non-zero one is the job telling you it did "
                      "not finish." % rc)
                if code != 3:
                    code = 1
            else:
                print("\n(the command exited 0 — on its own that is not evidence of "
                      "anything, which is why the assertions above decide)")
            return code

        results, state = verify(spec, state_path, args.max_baseline_age)
        text, code = report(spec, results, state)
        print(text)
        return code

    except SpecError as e:
        print("SPEC ERROR: %s" % e, file=sys.stderr)
        return 3
    except Exception:
        # Exit 3, never 1: nothing was evaluated, and a caller must be able to
        # tell that apart from a failed assertion.
        print("INTERNAL ERROR — nothing was evaluated:", file=sys.stderr)
        traceback.print_exc()
        return 3


if __name__ == "__main__":
    sys.exit(main())
