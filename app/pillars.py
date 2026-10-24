"""Niche + content pillars (pillars.toml): which pillar the next idea belongs to.

Weighted rotation with one hard rule: never the same pillar twice in a row, counting
the last pillar he actually drafted from (stored in settings as LAST_PILLAR)."""

from __future__ import annotations

import random
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")

PILLARS_FILE = Path(__file__).resolve().parent.parent / "pillars.toml"
LAST_PILLAR = "last_pillar"  # settings key


@dataclass(frozen=True)
class Pillar:
    key: str
    name: str
    weight: float
    about: str
    examples: tuple[str, ...] = ()

    @property
    def tag(self) -> str:
        return f"[{self.name}]"


@dataclass(frozen=True)
class Pillars:
    niche: str
    pillars: tuple[Pillar, ...]

    def get(self, key: str | None) -> Pillar | None:
        return next((p for p in self.pillars if p.key == key), None)

    def prompt_text(self) -> str:
        """The pillar list as the models see it."""
        total = sum(p.weight for p in self.pillars)
        return "\n".join(
            f"- {p.key} ({p.name}, {100 * p.weight / total:.0f}%): {p.about} e.g. {'; '.join(p.examples)}" for p in self.pillars
        )


@cache  # read once per process: edit pillars.toml, then restart the bot
def load_pillars(path: Path = PILLARS_FILE) -> Pillars:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    niche = str(data.get("niche") or "").strip()
    pillars = tuple(
        Pillar(str(p["key"]), str(p["name"]), float(p["weight"]), str(p.get("about") or ""), tuple(map(str, p.get("examples") or ())))
        for p in data.get("pillar") or []
    )
    if not niche or not pillars:
        raise ValueError(f"{path.name} needs a niche and at least one [[pillar]]")
    if len({p.key for p in pillars}) != len(pillars) or any(p.weight <= 0 for p in pillars):
        raise ValueError(f"{path.name}: pillar keys must be unique and weights positive")
    return Pillars(niche, pillars)


def next_pillar(pillars: Pillars, last: str | None, rng: random.Random | None = None) -> Pillar:
    """A weighted pick that is never `last` (unless it's the only pillar)."""
    pool = [p for p in pillars.pillars if p.key != last] or list(pillars.pillars)
    return (rng or random).choices(pool, weights=[p.weight for p in pool])[0]


def plan_pillars(pillars: Pillars, n: int, last: str | None, rng: random.Random | None = None) -> list[Pillar]:
    """n pillars in order, no two neighbours alike, and the first isn't `last`."""
    plan: list[Pillar] = []
    for _ in range(n):
        plan.append(next_pillar(pillars, plan[-1].key if plan else last, rng))
    return plan


def no_repeats(items: list[T], key: Callable[[T], str], last: str | None) -> list[T]:
    """Reorder so no two neighbours share a pillar and the first isn't `last`; an item
    that can't be placed without a repeat is dropped (never shown back to back)."""
    left, out, prev = list(items), [], last
    while left:
        pick = next((x for x in left if key(x) != prev), None)
        if pick is None:
            break
        left.remove(pick)
        out.append(pick)
        prev = key(pick)
    return out
