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

def verzeichnisse(schluessel, standard):
    """Where a capability actually lives, honouring the manifest's overrides.

    `skills` adds to the default directory; `commands` and `agents` replace it.
    Scanning the default regardless would check files the plugin does not load
    and miss the ones it does.
    """
    wert = plugin.get(schluessel)
    if wert is None:
        # The default directory is optional: not every plugin has skills.
        return [os.path.join(ROOT, standard)]
    if isinstance(wert, str):
        wert = [wert]
    pfade = [os.path.normpath(os.path.join(ROOT, w)) for w in wert if isinstance(w, str)]
    # An explicitly named directory is a claim, and a claim that points at
    # nothing is a failure rather than an empty result. Without this, pointing
    # `commands` at a misspelt path loads no commands and reports no problem.
    for p in pfade:
        check(os.path.isdir(p),
              "plugin.json: %s names %s, which is not a directory" % (schluessel, rel(p)))
    if schluessel == "skills":
        pfade.insert(0, os.path.join(ROOT, standard))
    return pfade


def rel(pfad):
    return os.path.relpath(pfad, ROOT).replace(os.sep, "/")


# --- skills: directory name, frontmatter, and a description that can trigger -
anzahl_skills = 0
for skills in verzeichnisse("skills", "skills"):
    if not os.path.isdir(skills):
        continue
    for eintrag in sorted(os.listdir(skills)):
        ordner = os.path.join(skills, eintrag)
        if not os.path.isdir(ordner):
            continue
        anzahl_skills += 1
        datei = os.path.join(ordner, "SKILL.md")
        check(os.path.exists(datei), "%s/%s: no SKILL.md" % (rel(skills), eintrag))
        if not os.path.exists(datei):
            continue
        fm = frontmatter(datei)
        check(fm is not None, "%s/%s/SKILL.md: no frontmatter" % (rel(skills), eintrag))
        if fm is None:
            continue
        check(fm.get("name") == eintrag,
              "%s/%s/SKILL.md: name %r does not match the directory"
              % (rel(skills), eintrag, fm.get("name")))
        check(bool(fm.get("description")),
              "%s/%s/SKILL.md: no description — it would never be selected"
              % (rel(skills), eintrag))

# --- commands: frontmatter, and any script path they name must exist ---------
anzahl_befehle = 0
for befehle in verzeichnisse("commands", "commands"):
    if not os.path.isdir(befehle):
        continue
    for eintrag in sorted(os.listdir(befehle)):
        if not eintrag.endswith(".md"):
            continue
        anzahl_befehle += 1
        datei = os.path.join(befehle, eintrag)
        fm = frontmatter(datei)
        check(fm is not None and bool(fm.get("description")),
              "%s/%s: no description in frontmatter" % (rel(befehle), eintrag))
        with open(datei, encoding="utf-8") as f:
            text = f.read()
        # The claim that costs the most when wrong: a command that points at a
        # script which is not there installs cleanly and fails at first use.
        for treffer in set(re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}/([\w./\-]+)", text)):
            check(os.path.exists(os.path.join(ROOT, treffer)),
                  "%s/%s: refers to %s, which does not exist"
                  % (rel(befehle), eintrag, treffer))

# --- and the check without which all of the above is vacuous -----------------
#
# Everything above iterates over what it finds. Delete skills/ and commands/
# and every loop runs zero times, every assertion holds, and the summary reads
# "nothing skipped, nothing failed" for a plugin that installs cleanly and
# provides nothing. That is the first incident in this archive, occurring in
# the checker written for the plugin that ships it. A count that can silently
# become zero is not a denominator.
anzahl_agenten = sum(len([d for d in os.listdir(v) if d.endswith(".md")])
                     for v in verzeichnisse("agents", "agents") if os.path.isdir(v))
sonstiges = [s for s, p in (("hooks", "hooks/hooks.json"), ("mcpServers", ".mcp.json"))
             if plugin.get(s) or os.path.exists(os.path.join(ROOT, p))]

bestand = "%d skill(s), %d command(s), %d agent(s)%s" % (
    anzahl_skills, anzahl_befehle, anzahl_agenten,
    "".join(", " + s for s in sonstiges))
check(anzahl_skills + anzahl_befehle + anzahl_agenten + len(sonstiges) > 0,
      "the plugin provides nothing at all — no skill, command, agent, hook or MCP server")

# The inventory is printed either way. "No complaints" and "nothing to
# complain about" read identically otherwise, and telling those two apart is
# the whole point of the archive this ships with.
print("INVENTORY: %s" % bestand)

if problems:
    print("PLUGIN MANIFEST: %d of %d checks failed\n" % (len(problems), checked))
    for p in problems:
        print("  FAIL  %s" % p)
    sys.exit(1)

print("PLUGIN MANIFEST: %d of %d checks evaluated — nothing skipped, nothing failed" % (checked, checked))
sys.exit(0)
