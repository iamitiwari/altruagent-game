"""Frozen reference data; never downloads anything during play."""
import json
import re
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def ident(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


# Attack type: super effective, resisted, immune. Generation 6+.
CHART = {
    "normal": ("", "rock steel", "ghost"),
    "fire": ("grass ice bug steel", "fire water rock dragon", ""),
    "water": ("fire ground rock", "water grass dragon", ""),
    "electric": ("water flying", "electric grass dragon", "ground"),
    "grass": ("water ground rock", "fire grass poison flying bug dragon steel", ""),
    "ice": ("grass ground flying dragon", "fire water ice steel", ""),
    "fighting": ("normal ice rock dark steel", "poison flying psychic bug fairy", "ghost"),
    "poison": ("grass fairy", "poison ground rock ghost", "steel"),
    "ground": ("fire electric poison rock steel", "grass bug", "flying"),
    "flying": ("grass fighting bug", "electric rock steel", ""),
    "psychic": ("fighting poison", "psychic steel", "dark"),
    "bug": ("grass psychic dark", "fire fighting poison flying ghost steel fairy", ""),
    "rock": ("fire ice flying bug", "fighting ground steel", ""),
    "ghost": ("psychic ghost", "dark", "normal"),
    "dragon": ("dragon", "steel", "fairy"),
    "dark": ("psychic ghost", "fighting dark fairy", ""),
    "steel": ("ice rock fairy", "fire water electric steel", ""),
    "fairy": ("fighting dragon dark", "fire poison steel", ""),
}


def effectiveness(move_type, types):
    strong, weak, immune = [set(s.split()) for s in CHART.get(ident(move_type), ("", "", ""))]
    score = 1.0
    for t in types or []:
        t = ident(t)
        if t in immune:
            return 0.0
        score *= 2 if t in strong else 0.5 if t in weak else 1
    return score


@lru_cache(maxsize=None)
def _reference_json(path):
    return json.loads(path.read_text()) if path.exists() else {}


class Dex:
    def __init__(self):
        self.species = self._load("pokedex.json")
        self.moves = self._load("moves.json")

    def _load(self, name):
        path = ROOT / "data" / name
        return _reference_json(path)

    def pokemon(self, p):
        if not p:
            return {}
        if isinstance(p, str):
            p = {"species": p}
        static = self.species.get(ident(p.get("species") or p.get("name")), {})
        result = {"types": static.get("types", []), "base_stats": static.get("baseStats", {}), **p}
        if not result.get("types"):
            result["types"] = static.get("types", [])
        if not result.get("base_stats"):
            result["base_stats"] = static.get("baseStats", {})
        return result

    def move(self, m):
        if isinstance(m, str):
            m = {"move_id": m}
        m = dict(m)
        # The action discriminator is not the elemental move type.
        if m.get("type") in {"move", "switch", "pass"}:
            m.pop("type")
        key = ident(m.get("move_id") or m.get("id") or m.get("name"))
        raw = self.moves.get(key, {})
        return {"move_id": key, "base_power": raw.get("basePower", 0),
                "category": raw.get("category", "Status"), "type": raw.get("type", "Normal"),
                "accuracy": raw.get("accuracy", 100), "priority": raw.get("priority", 0),
                "target": raw.get("target", "normal"), "flags": raw.get("flags", {}), **m}
