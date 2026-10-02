"""Build talents.json: fully-resolved talent tree text for every hero.

Data sources (all fetched over HTTP):
  - OpenDota constants hero_abilities  -> talent structure per hero
  - OpenDota constants abilities       -> talent display templates (dname)
  - OpenDota constants heroes          -> localized hero names
  - dotabuff/d2vpkr per-hero VPK files -> numeric talent values

Run:  dota-assistant build-talents [--out PATH]

By default this writes cache/talents.json in your data directory. The copy the
dashboard actually serves ships inside the package (src/dota_assistant/data/);
refreshing that for a new patch is a maintainer step: run this with
`--out src/dota_assistant/data/talents.json` from a source checkout.
"""

import json
import re
import sys

import requests

from . import config

HERO_ABILITIES_URL = "https://api.opendota.com/api/constants/hero_abilities"
ABILITIES_URL = "https://api.opendota.com/api/constants/abilities"
HEROES_URL = "https://api.opendota.com/api/constants/heroes"
VPK_URL = (
    "https://raw.githubusercontent.com/dotabuff/d2vpkr/master/"
    "dota/scripts/npc/heroes/{npc}.txt"
)

TOKEN_RE = re.compile(r"\{[svdf]:[^}]*\}")


def _out_path() -> str:
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--out" and i + 1 < len(argv):
            return argv[i + 1]
    return str(config.cache_dir() / "talents.json")


def fetch_json(url):
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.json()


def fetch_text(url):
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text


def fmt_number(raw):
    """Strip sign/multiplier markers and drop a trailing .0 (keep real decimals)."""
    s = raw.strip().lstrip("+-")
    # Some VPK values carry a literal multiplier marker, e.g. "x1.5"; the dname
    # template already supplies the "x", so drop a leading/trailing x/X.
    s = s.strip("xX")
    try:
        f = float(s)
    except ValueError:
        return s
    if f == int(f):
        return str(int(f))
    return ("%f" % f).rstrip("0").rstrip(".")


def collect_values(name, hero_text):
    """Find all `"<name>"  "value"` KV lines and return magnitude numbers."""
    values = []
    if not hero_text:
        return values
    pat = re.compile(r'"' + re.escape(name) + r'"\s+"([^"]+)"')
    for m in pat.finditer(hero_text):
        for part in m.group(1).split():
            values.append(fmt_number(part))
    return values


def resolve_talent(name, abilities, hero_text):
    """Resolve a single talent ability name to its final display text."""
    dname = ""
    entry = abilities.get(name)
    if isinstance(entry, dict):
        dname = entry.get("dname") or ""
    if not dname:
        # No template available; fall back to the raw name (best effort).
        return name

    tokens = TOKEN_RE.findall(dname)
    if not tokens:
        return dname.strip()

    values = collect_values(name, hero_text)

    result = dname
    for i, tok in enumerate(tokens):
        if values:
            val = values[i] if i < len(values) else values[-1]
        else:
            val = ""
        # Replace the first remaining occurrence of this exact token text.
        result = result.replace(tok, val, 1)

    # Clean up doubled spaces / stray spacing left by empty replacements.
    result = re.sub(r"\s{2,}", " ", result).strip()
    # Also tidy spaces before punctuation like "% " sequences that doubled.
    result = result.replace(" %", "%").replace(" /", "/").replace("/ ", "/")
    return result


def main():
    print("Fetching OpenDota constants ...")
    hero_abilities = fetch_json(HERO_ABILITIES_URL)
    abilities = fetch_json(ABILITIES_URL)
    heroes = fetch_json(HEROES_URL)

    # Build npcname -> localized_name map.
    localized = {}
    for _id, hero in heroes.items():
        if isinstance(hero, dict) and hero.get("name"):
            localized[hero["name"]] = hero.get("localized_name") or hero["name"]

    def pretty_name(npc):
        if npc in localized:
            return localized[npc]
        stripped = npc.replace("npc_dota_hero_", "")
        return stripped.replace("_", " ").title()

    # Cache of fetched hero VPK text keyed by npc name.
    hero_text_cache = {}

    def get_hero_text(npc):
        if npc in hero_text_cache:
            return hero_text_cache[npc]
        try:
            text = fetch_text(VPK_URL.format(npc=npc))
        except Exception as exc:  # noqa: BLE001
            print("  WARN: failed to fetch VPK for %s: %s" % (npc, exc))
            text = ""
        hero_text_cache[npc] = text
        return text

    # ---- Sanity checks (windrunner) ----
    print("Running sanity checks ...")
    wr_text = get_hero_text("npc_dota_hero_windrunner")
    check1 = resolve_talent(
        "special_bonus_unique_windranger_powershot_slow", abilities, wr_text
    )
    check2 = resolve_talent(
        "special_bonus_unique_windranger_3", abilities, wr_text
    )
    print("  sanity 1: special_bonus_unique_windranger_powershot_slow -> %r" % check1)
    print("  sanity 2: special_bonus_unique_windranger_3             -> %r" % check2)
    if check1 != "+1s Powershot Slow Duration":
        print("  !! sanity check 1 mismatch (expected '+1s Powershot Slow Duration')")
    if check2 != "-15% Powershot Reduction":
        print("  !! sanity check 2 mismatch (expected '-15% Powershot Reduction')")

    # ---- Build output ----
    result = {}
    heroes_written = 0
    unresolved = 0

    npc_names = sorted(hero_abilities.keys())
    total = len(npc_names)
    for idx, npc in enumerate(npc_names, 1):
        entry = hero_abilities[npc]
        if not isinstance(entry, dict):
            continue
        talents = entry.get("talents")
        if not isinstance(talents, list) or len(talents) != 8:
            continue
        if npc in ("npc_dota_hero_base",):
            continue

        hero_text = get_hero_text(npc)

        # Resolve each of the 8 talents; slot = index+1.
        resolved = []  # list of (slot, level, text)
        for i, t in enumerate(talents):
            if not isinstance(t, dict) or not t.get("name"):
                continue
            slot = i + 1
            game_level = {1: 10, 2: 15, 3: 20, 4: 25}.get(t.get("level"), 0)
            text = resolve_talent(t["name"], abilities, hero_text)
            if "{" in text:
                unresolved += 1
            resolved.append((slot, game_level, text))

        if len(resolved) != 8:
            continue

        # Group into tiers by game level, ordered 25 -> 10.
        tiers = []
        for lvl in (25, 20, 15, 10):
            options = [
                {"slot": slot, "text": text}
                for (slot, glvl, text) in resolved
                if glvl == lvl
            ]
            options.sort(key=lambda o: o["slot"])
            tiers.append({"level": lvl, "options": options})

        result[npc] = {"name": pretty_name(npc), "tiers": tiers}
        heroes_written += 1
        if idx % 20 == 0 or idx == total:
            print("  processed %d/%d heroes ..." % (idx, total))

    out_path = _out_path()
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print()
    print("=== Summary ===")
    print("Heroes written:            %d" % heroes_written)
    print("Talent texts w/ '{' left:  %d (unresolved)" % unresolved)
    print("Sanity 1: %r" % check1)
    print("Sanity 2: %r" % check2)
    print("Wrote: %s" % out_path)


if __name__ == "__main__":
    main()
