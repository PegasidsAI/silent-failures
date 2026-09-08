#!/usr/bin/env python3
"""Break the plugin manifest on purpose and confirm the checker notices.

check_plugin_manifest.py passing on a healthy repository establishes almost
nothing: a script that printed "all good" unconditionally would do the same.
What matters is the set of things it refuses.

So this copies the repository to a temporary directory, breaks one thing at a
time, and asserts the exit code. Most cases must NOT come out green — including
the one that mattered most, and was green until it was tested: deleting every
skill and command, which left a plugin that installs cleanly and provides
nothing while the checker reported no problems.

The tallies are printed at the end and deliberately not repeated here. Stating
them in prose is how the counts in this repository drifted before, and a number
written twice is a number that will disagree with itself.

Run from the repository root:
    python .github/check_plugin_manifest_selftest.py
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

WURZEL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NICHT_KOPIEREN = shutil.ignore_patterns(".git", "__pycache__", "*.state.json", "out")


def lies(pfad):
    return io.open(pfad, encoding="utf-8").read()


def schreib(pfad, text):
    io.open(pfad, "w", encoding="utf-8", newline="\n").write(text)


def lauf(wurzel):
    """Run the checker inside a copy and return (exit code, output)."""
    p = subprocess.run(
        [sys.executable, "-X", "utf8", os.path.join(wurzel, ".github", "check_plugin_manifest.py")],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def json_ohne(pfad, schluessel):
    d = json.load(io.open(pfad, encoding="utf-8"))
    d.pop(schluessel, None)
    schreib(pfad, json.dumps(d, indent=2))


def json_mit(pfad, schluessel, wert):
    d = json.load(io.open(pfad, encoding="utf-8"))
    d[schluessel] = wert
    schreib(pfad, json.dumps(d, indent=2))


# Each case: a name, the exit code it must produce, and what to break.
# 0 = everything evaluated and passed, 1 = something failed,
# 3 = nothing could be evaluated at all.
FAELLE = [
    ("an untouched copy passes", 0, None),

    ("a command naming a script that is not there", 1, lambda r, p: schreib(
        p["cmd"], lies(p["cmd"]).replace("tool/effect_check.py", "tool/not_here.py"))),

    ("a source pointing at a directory without a plugin.json", 1, lambda r, p: schreib(
        p["markt"], lies(p["markt"]).replace('"source": "./"', '"source": "./tool"'))),

    ("a source leaving the marketplace root", 1, lambda r, p: schreib(
        p["markt"], lies(p["markt"]).replace('"source": "./"', '"source": "../elsewhere"'))),

    ("a plugin name that differs from the manifest it points at", 1, lambda r, p: schreib(
        p["plugin"], lies(p["plugin"]).replace('"name": "effect-check"', '"name": "effect-checker"'))),

    ("a name that is not kebab-case", 1, lambda r, p: schreib(
        p["plugin"], lies(p["plugin"]).replace('"name": "effect-check"', '"name": "Effect_Check"'))),

    ("a marketplace without an owner", 1, lambda r, p: json_ohne(p["markt"], "owner")),

    ("a skill with no description, which would never be selected", 1, lambda r, p: schreib(
        p["skill"], lies(p["skill"]).replace("description:", "summary:", 1))),

    ("a skill directory renamed without its frontmatter", 1, lambda r, p: os.rename(
        p["skill_dir"], p["skill_dir"] + "-renamed")),

    # The one this file exists for. Every loop in the checker iterates over what
    # it finds; with nothing to find, every assertion held and the summary read
    # "nothing skipped, nothing failed" for a plugin that provides nothing.
    ("a plugin with no skill and no command left", 1, lambda r, p: [
        shutil.rmtree(os.path.join(r, "skills")),
        shutil.rmtree(os.path.join(r, "commands"))]),

    ("a manifest naming a command directory that does not exist", 1,
     lambda r, p: json_mit(p["plugin"], "commands", "./nowhere")),

    # A parse error is not a failed assertion: nothing was evaluated, and the
    # exit code has to say so rather than blend into "something failed".
    ("an unreadable marketplace manifest reports nothing-evaluated, not failure", 3,
     lambda r, p: schreib(p["markt"], "{ broken")),
]


def main():
    ergebnisse = []
    for titel, erwartet, brechen in FAELLE:
        arbeit = tempfile.mkdtemp(prefix="manifest-selftest-")
        kopie = os.path.join(arbeit, "repo")
        try:
            shutil.copytree(WURZEL, kopie, ignore=NICHT_KOPIEREN)
            pfade = {
                "markt": os.path.join(kopie, ".claude-plugin", "marketplace.json"),
                "plugin": os.path.join(kopie, ".claude-plugin", "plugin.json"),
                "cmd": os.path.join(kopie, "commands", "effect-check.md"),
                "skill_dir": os.path.join(kopie, "skills", "writing-effect-checks"),
                "skill": os.path.join(kopie, "skills", "writing-effect-checks", "SKILL.md"),
            }
            if brechen is not None:
                brechen(kopie, pfade)
            code, ausgabe = lauf(kopie)
        finally:
            shutil.rmtree(arbeit, ignore_errors=True)

        ok = code == erwartet
        ergebnisse.append(ok)
        marke = "[ok ]" if ok else "[BAD]"
        print("%s %-62s exit %d, expected %d" % (marke, titel, code, erwartet))
        if not ok:
            for zeile in ausgabe.splitlines():
                print("        " + zeile)

    gruen = sum(1 for (_, e, _) in FAELLE if e == 0)
    print()
    if all(ergebnisse):
        print("MANIFEST SELF-TEST PASSED — %d cases, %d of which must not come out green."
              % (len(FAELLE), len(FAELLE) - gruen))
        return 0
    print("MANIFEST SELF-TEST FAILED — %d of %d cases did not behave as stated."
          % (len(ergebnisse) - sum(ergebnisse), len(ergebnisse)))
    return 1


if __name__ == "__main__":
    sys.exit(main())
