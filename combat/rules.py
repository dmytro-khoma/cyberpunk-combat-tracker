"""
Числа и броски из «Cyberpunk НРИ 1.0». Здесь нет Django и нет состояния боя:
только таблицы и чистые функции, которые удобно проверять с подставными
кубиками. Номера разделов в комментариях — как в книге правил.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

SQUARE_M = 2              # одна клетка карты = 2 м
MOVE_M = 8                # §3: перемещение до 8 м за ход
BASE_DV = 12
AIM_PENALTY = 3           # §4: прицельная атака −3
AIM_PENALTY_OPTICS = 1    # улучшенная оптика: −1 вместо −3
HIT_BONUS_CAP = 4         # §4: класс + оружие + импланты не выше +4
PARTIAL_COVER_DV = 2
DODGE_MIN_REF = 8         # §4: от пули уклоняется только REF ≥ 8
ARMOR_BASE_SP = 3         # §5: базовый SP; TECH-зарядка его игнорирует
MEDKIT_LIMIT = 2          # §6: не больше двух аптечек на цель за бой
GRENADE_RANGE_M = 20


# --- §4. Дальность ----------------------------------------------------------

RANGE_EDGES = (6, 12, 25, 50, 100, 200, 400, 800)

# Модификатор к DV для каждого диапазона; None — «—», стрелять нельзя.
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
    "pistol": "Пистолет",
    "revolver": "Револьвер",
    "smg": "ПП",
    "assault": "Автомат",
    "rifle": "Винтовка",
    "shotgun": "Дробовик",
    "sniper": "Снайперская",
}
CATEGORY_CHOICES = list(CATEGORY_NAMES.items())

# §7: автоматическое оружие этих классов тратит патроны на каждый кубик урона.
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


# --- §7–9. Оружие -----------------------------------------------------------

@dataclass(frozen=True)
class Gun:
    name: str
    category: str
    kind: str              # "power" (обычное), "smart" или "tech"
    auto: bool
    dice: int              # число d6
    overclock: bool
    magazine: int          # значение по умолчанию; ДМ меняет в карточке бойца
    overclock_dv: int | None = None
    arrow: bool = False    # «Вентиль Стрелы»: зарядка обязательна + взрыв

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
    sp_divisor: int = 2       # §5: половина SP, моно-катана — четверть
    ignore_sp: int = 0
    hit_bonus: int = 0
    reach_m: int = SQUARE_M   # соседняя клетка
    min_body: int = 0
    note: str = ""

    is_melee = True
    kind = "melee"
    category = "melee"
    overclock = False
    arrow = False
    magazine = 0


# Магазины и DV разгона в книге не заданы (п. 18) — ниже только стартовые
# значения, их меняют в карточке бойца перед боем.
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
    "arrow":      Gun("Вентиль Стрелы", "rifle", "tech", False, 8, False, 2, arrow=True),
}

BLADES: dict[str, Blade] = {
    "knife":        Blade("Нож", 3),
    "tanto":        Blade("Танто", 2, note="из скрытности +2d6"),
    "club":         Blade("Дубинка", 3, note="крит. травма оглушает"),
    "machete":      Blade("Мачете", 3, note="+1d6 по цели без SP"),
    "katana":       Blade("Катана", 4, hit_bonus=1),
    "mono_katana":  Blade("Моно-катана", 4, sp_divisor=4),
    "sledge":       Blade("Кувалда", 5, ignore_sp=2, min_body=6),
    "axe":          Blade("Боевой топор", 4, note="пробитие снимает 3 SP"),
    "satori":       Blade("Сатори", 4, note="крит. травма +2d6"),
    "byakko":       Blade("Бякко", 4, note="после убийства +4 м"),
    "jinchu":       Blade("Дзинтю-Мару", 4, note="+2 по элите и боссу"),
    "cocktail":     Blade("Коктейльная палочка", 3, note="первый удар из скрытности +2d6"),
    "sting":        Blade("Жало", 3, reach_m=6, note="метание, кровотечение при пробитии"),
    "monowire":     Blade("Моноструна", 3, ignore_sp=2, reach_m=3),
    "mantis":       Blade("Клинки богомола", 4, note="после 6 м движения +2"),
    "gorilla":      Blade("Руки гориллы", 4),
}

WEAPONS: dict[str, Gun | Blade] = {**GUNS, **BLADES}

WEAPON_CHOICES = [
    ("Огнестрельное", [(k, f"{g.name} · {CATEGORY_NAMES[g.category].lower()}, {g.dice}d6")
                       for k, g in GUNS.items()]),
    ("Ближний бой", [(k, f"{b.name} · {b.dice}d6") for k, b in BLADES.items()]),
]


# --- §10. Гранаты -------------------------------------------------------------

@dataclass(frozen=True)
class Grenade:
    name: str
    dice: int
    radius_m: int


GRENADES = {
    "frag":  Grenade("Осколочная", 4, 3),
    "emp":   Grenade("ЭМИ", 2, 3),
    "flash": Grenade("Светошумовая", 0, 4),
    "fire":  Grenade("Зажигательная", 3, 3),
}


# --- §11. Кибердеки и квикхаки --------------------------------------------------

DECKS = {
    "":         ("Нет", 0),
    "basic":    ("Базовая", 5),
    "arasaka":  ("Arasaka", 6),
    "militech": ("Militech", 7),
    "raven":    ("Raven Microcyber", 8),
    "netwatch": ("NetWatch Netdriver", 9),
}
DECK_CHOICES = [(k, name if not ram else f"{name} · {ram} RAM") for k, (name, ram) in DECKS.items()]

QUICKHACKS = {
    "ping":      ("Ping", 1),
    "glitch":    ("Сбой оружия", 2),
    "optics":    ("Перезагрузка оптики", 2),
    "cripple":   ("Ограничение движения", 3),
    "short":     ("Короткое замыкание", 3),
    "overheat":  ("Перегрев", 4),
}
DAMAGE_HACKS = {"short", "overheat"}

HACK_DV = {"regular": 12, "elite": 14, "boss": 16}


# --- §1. Проверки -------------------------------------------------------------

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
    """Натуральная 10 добавляет ещё d10, натуральная 1 вычитает. Без цепочек."""
    first = rng.randint(1, 10)
    check = Check(stat, mods, [first], first)
    if first in (1, 10):
        extra = rng.randint(1, 10)
        check.rolls.append(extra)
        check.die = first + extra if first == 10 else first - extra
    return check


def roll_d6(count: int, rng: random.Random) -> list[int]:
    return [rng.randint(1, 6) for _ in range(count)]


# --- §5. HP и SP ----------------------------------------------------------------

def max_hp(body: int, cool: int) -> int:
    return 10 + 5 * math.ceil((body + cool) / 2)


def chrome_limit(cool: int) -> int:
    return 10 + 2 * cool


def melee_sp(sp: int, divisor: int, ignore: int) -> int:
    """Ближний бой: доля SP с округлением вверх, потом «игнорирует N»."""
    return max(0, math.ceil(sp / divisor) - ignore)


def is_critical(dice: list[int], hp_loss: int) -> bool:
    return dice.count(6) >= 2 and hp_loss >= 1


# §6: d6 по таблице критических травм.
def critical_kind(roll: int) -> str:
    if roll <= 2:
        return "concussion"
    if roll == 3:
        return "arm"
    if roll == 4:
        return "leg"
    return "bleeding"


CRITICAL_NAMES = {
    "concussion": "сотрясение (−2 ко всем проверкам на 1 ход)",
    "arm": "травма руки (рукой пользоваться нельзя до лечения)",
    "leg": "травма ноги (скорость вдвое до лечения)",
    "bleeding": "кровотечение (−2 HP в начале своего хода)",
}


def distance(ax: int, ay: int, bx: int, by: int) -> int:
    """Клетки по сетке, диагональ считается за одну."""
    return max(abs(ax - bx), abs(ay - by))


# --- Стены ----------------------------------------------------------------
# Ту же логику повторяет board.js, чтобы подсветка совпадала с сервером.

def line_cells(ax: int, ay: int, bx: int, by: int) -> list[tuple[int, int]]:
    """Клетки, через которые идёт линия между центрами, без концов."""
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
    Кратчайший путь в клетках в обход стен (BFS, диагональ — один шаг,
    срезать угол между двумя стенами нельзя). None — не дойти за limit шагов.
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
