#!/usr/bin/env python3
"""Check that the case counts stated in the READMEs match a real self-test run.

Why this exists: the numbers in README.md and tool/README.md drifted within
hours of being written. Cases were added to the self-test and the prose was
not updated. Nothing failed, nothing turned red, and both files went on
describing a state that no longer held.

That is the second incident in this archive, occurring in its own
documentation. So the counts are measured here rather than trusted.

Run from the repository root:  python .github/check_documented_counts.py
"""

import collections
import pathlib
import re
import subprocess
import sys

WURZEL = pathlib.Path(__file__).resolve().parent.parent


def wirklich():
    """Run the self-test and count what it actually did."""
    p = subprocess.run([sys.executable, "tool/effect_check.py", "selftest"],
                       cwd=WURZEL, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        print("The self-test itself did not pass, so its counts cannot be trusted.")
        print(p.stdout[-2000:])
        raise SystemExit(1)

    faelle = [z for z in p.stdout.splitlines() if "[ok ]" in z or "[BAD]" in z]
    mit_ergebnis = [z for z in faelle if "expected" in z]
    zaehler = collections.Counter(
        re.search(r"expected (\w+)", z).group(1) for z in mit_ergebnis)
    return {
        "gesamt": len(faelle),
        "mit_ergebnis": len(mit_ergebnis),
        "gruen": zaehler["pass"],
        "nicht_gruen": zaehler["fail"] + zaehler["skip"] + zaehler["specerror"],
        "fail": zaehler["fail"],
        "skip": zaehler["skip"],
        "specerror": zaehler["specerror"],
    }


def behauptet():
    """Pull every number the documentation states about the self-test."""
    gefunden = {}

    text = (WURZEL / "README.md").read_text(encoding="utf-8")
    m = re.search(r"selftest` runs (\d+) cases\. Of the (\d+) that assert a run outcome, "
                  r"only (\d+) may come out green; the other (\d+) must not", text)
    if not m:
        print("README.md no longer contains the sentence stating the counts. "
              "Either restore it or update this check.")
        raise SystemExit(1)
    gefunden["README.md"] = {"gesamt": int(m.group(1)), "mit_ergebnis": int(m.group(2)),
                             "gruen": int(m.group(3)), "nicht_gruen": int(m.group(4))}

    text = (WURZEL / "tool" / "README.md").read_text(encoding="utf-8")
    m = re.search(r"runs \*\*(\d+) cases\*\* against a temporary directory: (\d+) that "
                  r"assert a run outcome, and (\d+) that assert a property", text)
    if not m:
        print("tool/README.md no longer contains the sentence stating the counts.")
        raise SystemExit(1)
    a = {"gesamt": int(m.group(1)), "mit_ergebnis": int(m.group(2))}

    m = re.search(r"Of those (\d+), only \*\*(\d+) may come out green\*\*\. The other "
                  r"(\d+) must not — (\d+) failures, (\d+) coverage gaps, "
                  r"(\d+) rejected specifications", text)
    if not m:
        print("tool/README.md no longer contains the sentence breaking down the counts.")
        raise SystemExit(1)
    a.update({"gruen": int(m.group(2)), "nicht_gruen": int(m.group(3)),
              "fail": int(m.group(4)), "skip": int(m.group(5)),
              "specerror": int(m.group(6))})
    gefunden["tool/README.md"] = a
    return gefunden


def main():
    ist = wirklich()
    print("Measured from an actual run:")
    for k in ("gesamt", "mit_ergebnis", "gruen", "nicht_gruen", "fail", "skip", "specerror"):
        print("  %-13s %d" % (k, ist[k]))
    print()

    abweichungen = []
    for datei, werte in behauptet().items():
        for schluessel, behauptung in werte.items():
            if behauptung != ist[schluessel]:
                abweichungen.append((datei, schluessel, behauptung, ist[schluessel]))

    if abweichungen:
        print("The documentation states numbers the code does not produce:\n")
        for datei, schluessel, sagt, ist_wert in abweichungen:
            print("  %-16s %-13s states %d, actual %d" % (datei, schluessel, sagt, ist_wert))
        print("\nUpdate the prose, or the claim is false the moment someone checks it.")
        return 1

    print("Every documented count matches the run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
