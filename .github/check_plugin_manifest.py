#!/usr/bin/env python3
"""Check the plugin and marketplace manifests against the repository.

A manifest is a set of claims: this plugin exists at this path, it provides
these commands, this command runs that script. Every one of those claims can
be wrong while the JSON stays perfectly valid, and the failure shows up as a
plugin that installs and then quietly does nothing.

So this re-derives them from the files on disk. It is the same move the tool
itself makes: do not ask the manifest whether it is correct, measure what it
points at.

Exit codes match the tool: 0 all checks evaluated and passed, 1 something
failed, 3 nothing could be evaluated.
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

problems = []
checked = 0


def check(bedingung, meldung):
    global checked
    checked += 1
    if not bedingung:
        problems.append(meldung)


def load(rel):
    pfad = os.path.join(ROOT, rel)
    if not os.path.exists(pfad):
        print("MISSING: %s" % rel, file=sys.stderr)
        sys.exit(3)
    with open(pfad, encoding="utf-8") as f:
        try:
            return json.load(f)
        except ValueError as e:
            print("UNPARSEABLE: %s — %s" % (rel, e), file=sys.stderr)
            sys.exit(3)


def frontmatter(pfad):
    """Return the YAML-ish frontmatter of a markdown file as a dict.

    Deliberately not a YAML parser: these files use flat `key: value` pairs,
    and a real parser would hide a file that is subtly not what it looks like.
    """
    with open(pfad, encoding="utf-8") as f:
        text = f.read()
    if not text.startswith("---"):
        return None
    ende = text.find("\n---", 3)
    if ende == -1:
        return None
    feld = {}
    for zeile in text[3:ende].splitlines():
        if ":" in zeile and not zeile.startswith((" ", "\t", "#")):
            k, _, v = zeile.partition(":")
            feld[k.strip()] = v.strip()
    return feld


plugin = load(".claude-plugin/plugin.json")
markt = load(".claude-plugin/marketplace.json")

# --- the two required fields, per the documented schema ----------------------
check("name" in plugin, "plugin.json: no name")
check(KEBAB.match(plugin.get("name", "")), "plugin.json: name is not kebab-case")
check("name" in markt, "marketplace.json: no name")
check(KEBAB.match(markt.get("name", "")), "marketplace.json: name is not kebab-case")
check(isinstance(markt.get("owner"), dict) and markt["owner"].get("name"),
      "marketplace.json: owner.name is required")
check(isinstance(markt.get("plugins"), list) and markt["plugins"],
      "marketplace.json: plugins must be a non-empty list")

# --- every listed plugin must resolve to a real directory with a manifest ----
namen = set()
for eintrag in markt.get("plugins", []):
    name = eintrag.get("name", "<unnamed>")
    namen.add(name)
    quelle = eintrag.get("source")
    check(isinstance(quelle, str) and quelle.startswith("./"),
          "%s: source must be a relative path starting with ./" % name)
    check(".." not in (quelle or ""),
          "%s: source must not leave the marketplace root" % name)
    if isinstance(quelle, str) and quelle.startswith("./"):
        ziel = os.path.normpath(os.path.join(ROOT, quelle))
        check(os.path.isdir(ziel), "%s: source directory does not exist: %s" % (name, quelle))
        manifest = os.path.join(ziel, ".claude-plugin", "plugin.json")
        check(os.path.exists(manifest),
              "%s: no .claude-plugin/plugin.json under %s" % (name, quelle))
        if os.path.exists(manifest):
            with open(manifest, encoding="utf-8") as f:
                check(json.load(f).get("name") == name,
                      "%s: name differs from the plugin.json it points at" % name)

check(plugin.get("name") in namen,
      "plugin.json name %r is not listed in marketplace.json" % plugin.get("name"))

# --- skills: directory name, frontmatter, and a description that can trigger -
skills = os.path.join(ROOT, "skills")
if os.path.isdir(skills):
    gefunden = 0
    for eintrag in sorted(os.listdir(skills)):
        ordner = os.path.join(skills, eintrag)
        if not os.path.isdir(ordner):
            continue
        gefunden += 1
        datei = os.path.join(ordner, "SKILL.md")
        check(os.path.exists(datei), "skills/%s: no SKILL.md" % eintrag)
        if not os.path.exists(datei):
            continue
        fm = frontmatter(datei)
        check(fm is not None, "skills/%s/SKILL.md: no frontmatter" % eintrag)
        if fm is None:
            continue
        check(fm.get("name") == eintrag,
              "skills/%s/SKILL.md: name %r does not match the directory" % (eintrag, fm.get("name")))
        check(bool(fm.get("description")),
              "skills/%s/SKILL.md: no description — it would never be selected" % eintrag)
    check(gefunden > 0, "skills/ exists but holds no skill")

# --- commands: frontmatter, and any script path they name must exist ---------
befehle = os.path.join(ROOT, "commands")
if os.path.isdir(befehle):
    for eintrag in sorted(os.listdir(befehle)):
        if not eintrag.endswith(".md"):
            continue
        datei = os.path.join(befehle, eintrag)
        fm = frontmatter(datei)
        check(fm is not None and bool(fm.get("description")),
              "commands/%s: no description in frontmatter" % eintrag)
        with open(datei, encoding="utf-8") as f:
            text = f.read()
        # The claim that costs the most when wrong: a command that points at a
        # script which is not there installs cleanly and fails at first use.
        for treffer in set(re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}/([\w./\-]+)", text)):
            check(os.path.exists(os.path.join(ROOT, treffer)),
                  "commands/%s: refers to %s, which does not exist" % (eintrag, treffer))

if problems:
    print("PLUGIN MANIFEST: %d of %d checks failed\n" % (len(problems), checked))
    for p in problems:
        print("  FAIL  %s" % p)
    sys.exit(1)

print("PLUGIN MANIFEST: %d of %d checks evaluated — nothing skipped, nothing failed" % (checked, checked))
sys.exit(0)
