"""
Numbers and dice from the "Cyberpunk TTRPG 1.0" rules. No Django and no
combat state here: only tables and pure functions that are easy to test
with fixed dice. Section numbers in comments match the rulebook.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

SQUARE_M = 2              # one map square = 2 m
MOVE_M = 8                # §3: move up to 8 m per turn
BASE_DV = 12
AIM_PENALTY = 3           # §4: aimed attack −3
AIM_PENALTY_OPTICS = 1    # enhanced optics: −1 instead of −3
HIT_BONUS_CAP = 4         # §4: class + weapon + implants, +4 at most
PARTIAL_COVER_DV = 2
DODGE_MIN_REF = 8         # §4: only REF ≥ 8 can dodge bullets
ARMOR_BASE_SP = 3         # §5: base SP; a TECH charge ignores it
MEDKIT_LIMIT = 2          # §6: two medkits per target per fight at most
GRENADE_RANGE_M = 20


# --- §4. Range -----------------------------------------------------------------

RANGE_EDGES = (6, 12, 25, 50, 100, 200, 400, 800)

# DV modifier for each band; None means "—", the weapon can't fire that far.
RANGE_TABLE: dict[str, tuple[int | None, ...]] = {
    "pistol":   (0, 2, 4, 6, 8, 10, None, None),
    "revolver": (2, 0, 2, 4, 6, 8, 10, None),
    "smg":      (0, 0, 2, 4, 6, 8, None, None),
    "assault":  (4, 2, 0, 0, 2, 4, 6, 8),
    "rifle":    (4, 2, 0, 0, 2, 4, 6, 8),
    "shotgun":  (0, 2, 4, 6, 8, None, None, None),
    "sniper":   (8, 6, 4, 2, 0, 0, 2, 4),
}

CATEGORY_NAMES = {
    "pistol": "Pistol",
    "revolver": "Revolver",
    "smg": "SMG",
    "assault": "Assault rifle",
    "rifle": "Rifle",
    "shotgun": "Shotgun",
    "sniper": "Sniper rifle",
}
CATEGORY_CHOICES = list(CATEGORY_NAMES.items())

# §7: automatic weapons of these classes spend ammo for every damage die.
AMMO_PER_DIE = {"pistol": 2, "smg": 3, "assault": 2}


def range_mod(category: str, metres: int) -> int | None:
    for edge, mod in zip(RANGE_EDGES, RANGE_TABLE[category]):
        if metres <= edge:
            return mod
    return None


def max_range_m(category: str) -> int:
    reach = 0
    for edge, mod in zip(RANGE_EDGES, RANGE_TABLE[category]):
        if mod is not None:
            reach = edge
    return reach


# --- §7–9. Weapons ---------------------------------------------------------------

@dataclass(frozen=True)
class Gun:
    name: str
    category: str
    kind: str              # "power" (regular), "smart" or "tech"
    auto: bool
    dice: int              # number of d6
    overclock: bool
    magazine: int          # default; the GM can change it on the fighter sheet
    overclock_dv: int | None = None
    arrow: bool = False    # Arrow Valve: must be charged, then explodes

    is_melee = False

    @property
    def ammo_per_die(self) -> int:
        return AMMO_PER_DIE.get(self.category, 0) if self.auto else 0

    def ammo_for(self, dice: int) -> int:
        return self.ammo_per_die * dice if self.ammo_per_die else 1


@dataclass(frozen=True)
class Blade:
    name: str
    dice: int
    sp_divisor: int = 2       # §5: half SP, a mono-katana uses a quarter
    ignore_sp: int = 0
    hit_bonus: int = 0
    reach_m: int = SQUARE_M   # the next square
    min_body: int = 0
    note: str = ""

    is_melee = True
    kind = "melee"
    category = "melee"
    overclock = False
    arrow = False
    magazine = 0


# The book leaves magazines and overclock DVs to the GM (§18), so these are
# only starting values; change them on the fighter sheet before a fight.
GUNS: dict[str, Gun] = {
    "unity":      Gun("Unity", "pistol", "power", False, 3, False, 12),
    "nue":        Gun("Nue", "pistol", "power", False, 4, False, 8),
    "lexington":  Gun("Lexington", "pistol", "power", True, 2, True, 24, 13),
    "overture":   Gun("Overture", "revolver", "power", False, 4, True, 6, 14),
    "kappa":      Gun("Kappa", "pistol", "smart", True, 2, True, 24, 13),
    "chao":       Gun("Chao", "pistol", "smart", False, 3, False, 12),
    "yukimura":   Gun("Yukimura", "pistol", "smart", False, 3, False, 10),
    "saratoga":   Gun("Saratoga", "smg", "power", True, 2, True, 36, 13),
    "shingen":    Gun("Shingen", "smg", "smart", True, 2, True, 36, 13),
    "dian":       Gun("Dian", "smg", "smart", True, 2, True, 36, 13),
    "senkoh":     Gun("Senkoh LX", "smg", "tech", True, 2, True, 36, 13),
    "copperhead": Gun("Copperhead", "assault", "power", True, 3, True, 30, 14),
    "ajax":       Gun("Ajax", "assault", "power", True, 4, True, 32, 14),
    "kyubi":      Gun("Kyubi", "assault", "power", False, 4, False, 20),
    "sidewinder": Gun("Sidewinder", "assault", "smart", True, 3, True, 30, 14),
    "carnage":    Gun("Carnage", "shotgun", "power", False, 5, False, 4),
    "zhuo":       Gun("Zhuo", "shotgun", "smart", False, 4, False, 6),
    "pozhar":     Gun("Pozhar", "shotgun", "power", True, 4, True, 12, 15),
    "achilles":   Gun("Achilles", "rifle", "tech", False, 4, False, 8),
    "nekomata":   Gun("Nekomata", "sniper", "tech", False, 5, False, 5),
    "ashura":     Gun("Ashura", "sniper", "smart", False, 5, False, 5),
    "grad":       Gun("Grad", "sniper", "power", False, 6, False, 5),
    "arrow":      Gun("Arrow Valve", "rifle", "tech", False, 8, False, 2, arrow=True),
}

BLADES: dict[str, Blade] = {
    "knife":        Blade("Knife", 3),
    "tanto":        Blade("Tanto", 2, note="+2d6 from stealth"),
    "club":         Blade("Club", 3, note="critical injury stuns"),
    "machete":      Blade("Machete", 3, note="+1d6 against a target without SP"),
    "katana":       Blade("Katana", 4, hit_bonus=1),
    "mono_katana":  Blade("Mono-katana", 4, sp_divisor=4),
    "sledge":       Blade("Sledgehammer", 5, ignore_sp=2, min_body=6),
    "axe":          Blade("Battle axe", 4, note="a hit through armour removes 3 SP"),
    "satori":       Blade("Satori", 4, note="critical injury +2d6"),
    "byakko":       Blade("Byakko", 4, note="+4 m after a kill"),
    "jinchu":       Blade("Jinchu-Maru", 4, note="+2 against elites and bosses"),
    "cocktail":     Blade("Cocktail stick", 3, note="first strike from stealth +2d6"),
    "sting":        Blade("Sting", 3, reach_m=6, note="thrown, bleeding on a hit through armour"),
    "monowire":     Blade("Monowire", 3, ignore_sp=2, reach_m=3),
    "mantis":       Blade("Mantis blades", 4, note="+2 after moving 6 m"),
    "gorilla":      Blade("Gorilla arms", 4),
}

WEAPONS: dict[str, Gun | Blade] = {**GUNS, **BLADES}

WEAPON_CHOICES = [
    ("Firearms", [(k, f"{g.name} · {CATEGORY_NAMES[g.category].lower()}, {g.dice}d6")
                  for k, g in GUNS.items()]),
    ("Melee", [(k, f"{b.name} · {b.dice}d6") for k, b in BLADES.items()]),
]


# --- §10. Grenades -----------------------------------------------------------------

@dataclass(frozen=True)
class Grenade:
    name: str
    dice: int
    radius_m: int


GRENADES = {
    "frag":  Grenade("Frag", 4, 3),
    "emp":   Grenade("EMP", 2, 3),
    "flash": Grenade("Flashbang", 0, 4),
    "fire":  Grenade("Incendiary", 3, 3),
}


# --- §11. Cyberdecks and quickhacks ---------------------------------------------

DECKS = {
    "":         ("None", 0),
    "basic":    ("Basic", 5),
    "arasaka":  ("Arasaka", 6),
    "militech": ("Militech", 7),
    "raven":    ("Raven Microcyber", 8),
    "netwatch": ("NetWatch Netdriver", 9),
}
DECK_CHOICES = [(k, name if not ram else f"{name} · {ram} RAM") for k, (name, ram) in DECKS.items()]

QUICKHACKS = {
    "ping":      ("Ping", 1),
    "glitch":    ("Weapon glitch", 2),
    "optics":    ("Reboot optics", 2),
    "cripple":   ("Cripple movement", 3),
    "short":     ("Short circuit", 3),
    "overheat":  ("Overheat", 4),
}
DAMAGE_HACKS = {"short", "overheat"}

HACK_DV = {"regular": 12, "elite": 14, "boss": 16}


# --- §1. Checks --------------------------------------------------------------------

@dataclass
class Check:
    stat: int
    mods: int = 0
    rolls: list[int] = field(default_factory=list)
    die: int = 0

    @property
    def total(self) -> int:
        return self.stat + self.mods + self.die

    def describe(self) -> str:
        if len(self.rolls) == 2:
            sign = "+" if self.rolls[0] == 10 else "−"
            dice = f"d10[{self.rolls[0]}{sign}{self.rolls[1]}]"
        else:
            dice = f"d10[{self.rolls[0]}]"
        mods = f" {self.mods:+d}".replace("-", "−") if self.mods else ""
        return f"{self.stat} + {dice}{mods} = {self.total}"


def roll_check(stat: int, mods: int, rng: random.Random) -> Check:
    """A natural 10 adds another d10, a natural 1 subtracts one. No chains."""
    first = rng.randint(1, 10)
    check = Check(stat, mods, [first], first)
    if first in (1, 10):
        extra = rng.randint(1, 10)
        check.rolls.append(extra)
        check.die = first + extra if first == 10 else first - extra
    return check


def roll_d6(count: int, rng: random.Random) -> list[int]:
    return [rng.randint(1, 6) for _ in range(count)]


# --- §5. HP and SP -----------------------------------------------------------------

def max_hp(body: int, cool: int) -> int:
    return 10 + 5 * math.ceil((body + cool) / 2)


def chrome_limit(cool: int) -> int:
    return 10 + 2 * cool


def melee_sp(sp: int, divisor: int, ignore: int) -> int:
    """Melee: a share of SP rounded up, then "ignores N SP"."""
    return max(0, math.ceil(sp / divisor) - ignore)


def is_critical(dice: list[int], hp_loss: int) -> bool:
    return dice.count(6) >= 2 and hp_loss >= 1


# §6: d6 on the critical injury table.
def critical_kind(roll: int) -> str:
    if roll <= 2:
        return "concussion"
    if roll == 3:
        return "arm"
    if roll == 4:
        return "leg"
    return "bleeding"


CRITICAL_NAMES = {
    "concussion": "concussion (−2 to all checks for 1 turn)",
    "arm": "arm injury (the arm can't be used until treated)",
    "leg": "leg injury (half speed until treated)",
    "bleeding": "bleeding (−2 HP at the start of own turn)",
}


def distance(ax: int, ay: int, bx: int, by: int) -> int:
    """Grid squares; a diagonal step counts as one."""
    return max(abs(ax - bx), abs(ay - by))


# --- Walls -------------------------------------------------------------------------
# board.js repeats this logic so the highlighted targets match the server.

def line_cells(ax: int, ay: int, bx: int, by: int) -> list[tuple[int, int]]:
    """Squares the line between the two centres passes through, ends excluded."""
    steps = max(abs(bx - ax), abs(by - ay)) * 4
    cells: list[tuple[int, int]] = []
    for i in range(1, steps):
        x = math.floor(ax + (bx - ax) * i / steps + 0.5)
        y = math.floor(ay + (by - ay) * i / steps + 0.5)
        if (x, y) not in ((ax, ay), (bx, by)) and (not cells or cells[-1] != (x, y)):
            cells.append((x, y))
    return cells


def walls_between(walls: set, ax: int, ay: int, bx: int, by: int) -> int:
    return len({c for c in line_cells(ax, ay, bx, by) if c in walls})


NEIGHBOURS = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy]


def path_steps(width: int, height: int, walls: set, start: tuple, goal: tuple, limit: int) -> int | None:
    """
    Shortest path in squares around walls (BFS, a diagonal is one step, and
    you can't cut a corner between two walls). None if it takes more than
    `limit` steps.
    """
    if goal in walls:
        return None
    frontier, seen = [start], {start: 0}
    while frontier:
        nxt = []
        for x, y in frontier:
            d = seen[(x, y)]
            if d >= limit:
                continue
            for dx, dy in NEIGHBOURS:
                cell = (x + dx, y + dy)
                if cell in seen or cell in walls:
                    continue
                if not (0 <= cell[0] < width and 0 <= cell[1] < height):
                    continue
                if dx and dy and (x + dx, y) in walls and (x, y + dy) in walls:
                    continue
                if cell == goal:
                    return d + 1
                seen[cell] = d + 1
                nxt.append(cell)
        frontier = nxt
    return None
