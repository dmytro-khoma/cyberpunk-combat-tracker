"""
Ход боя: проверки «можно ли», изменение моделей и запись в журнал.
Вьюхи вызывают только функции отсюда. Любое нарушение правил —
CombatError с текстом для игрока.
"""

from __future__ import annotations

import math
import random

from django.db import transaction

from . import rules
from .models import Combatant, Encounter, LogEntry

Kind = LogEntry.Kind


class CombatError(Exception):
    pass


STATUS_NAMES = {
    "blind": "ослеплён",
    "concussion": "сотрясение",
    "stunned": "оглушён",
    "immobile": "обездвижен",
    "glitch": "сбой оружия",
    "implants_off": "импланты отключены",
    "slowed": "скорость вдвое",
    "burning": "горит",
    "overheat": "перегрев",
    "bleeding": "кровотечение",
    "arm": "травма руки",
    "leg": "травма ноги",
    "minus2": "−2 к следующей проверке",
}

# Персистентные состояния живут до лечения; разовые срабатывают в начале хода.
PERSISTENT = {"bleeding", "arm", "leg"}
ON_TURN_START = {"burning", "overheat", "stunned"}
UNTIL_NEXT_CHECK = {"minus2"}


# --- мелочи ---------------------------------------------------------------

def _log(encounter: Encounter, text: str, kind: str = Kind.INFO) -> None:
    LogEntry.objects.create(encounter=encounter, round=encounter.round, kind=kind, text=text)


def _metres(a: Combatant, b: Combatant) -> int:
    return rules.distance(a.x, a.y, b.x, b.y) * rules.SQUARE_M


def _add_status(c: Combatant, name: str, turns: int | None = 1, **extra) -> bool:
    """Повторное наложение обновляет длительность, но не складывается."""
    fresh = name not in c.statuses
    c.statuses[name] = {"left": turns, "seen": False, **extra}
    return fresh


def winner(encounter: Encounter) -> str | None:
    if not encounter.started:
        return None
    standing = {c.side for c in encounter.combatants.all() if c.conscious}
    if len(standing) == 1:
        return standing.pop()
    return "nobody" if not standing else None


WINNER_TEXT = {"crew": "Победила команда.", "hostile": "Победили противники.",
               "nobody": "На ногах не осталось никого."}


def _require_turn(encounter: Encounter, c: Combatant, *, action: bool = False) -> None:
    if not encounter.started:
        raise CombatError("Сначала начните бой.")
    if winner(encounter):
        raise CombatError("Бой окончен.")
    active = encounter.active_combatant()
    if active is None or active.pk != c.pk:
        raise CombatError(f"Сейчас не ход {c.name}.")
    if not c.conscious:
        raise CombatError(f"{c.name} без сознания.")
    if action and c.has_acted:
        raise CombatError(f"{c.name} уже потратил действие в этом ходу.")


def _check_mods(c: Combatant, *, attack: bool) -> tuple[int, list[str]]:
    """Штрафы состояний к проверке. «−2 к следующей» снимается здесь же."""
    mods, notes = 0, []
    if "blind" in c.statuses:
        mods -= 4 if attack else 2
        notes.append("ослеплён")
    if "concussion" in c.statuses:
        mods -= 2
        notes.append("сотрясение")
    if c.statuses.pop("minus2", None) is not None:
        mods -= 2
        notes.append("−2 от травмы")
    return mods, notes


def _spend_luck(c: Combatant, luck: int) -> int:
    if not 0 <= luck <= 3:
        raise CombatError("Удачу тратят по 1–3 очка на проверку.")
    if luck > c.luck_left:
        raise CombatError(f"У {c.name} осталось {c.luck_left} очк. удачи.")
    c.luck_left -= luck
    return luck


def _hit_bonus(a: Combatant, t: Combatant, weapon) -> int:
    """§4: класс + оружие + импланты, всё вместе не выше +4."""
    bonus = 0
    if (a.role == Combatant.Role.GUNNER and not weapon.is_melee
            and a.gunner_category == weapon.category):
        bonus += 2
    if weapon.is_melee:
        bonus += weapon.hit_bonus
        if a.weapon == "jinchu" and t.tier != Combatant.Tier.REGULAR:
            bonus += 2
        if a.weapon == "mantis" and a.moved_m >= 6:
            bonus += 2
    if a.adrenaline and a.implants_on and a.hp <= a.hp_max / 2:
        bonus += 2
    return min(bonus, rules.HIT_BONUS_CAP)


def _aim_penalty(a: Combatant, t: Combatant, weapon, aim: str) -> int:
    if aim == "body":
        return 0
    if (weapon.kind == "smart" and a.lock_target_id == t.pk and a.lock_ready):
        return 0                                     # §8: захват снимает −3
    if a.optics and a.implants_on:
        return rules.AIM_PENALTY_OPTICS
    return rules.AIM_PENALTY


def _dodged(encounter, t: Combatant, attack_total: int, rng: random.Random, *, bullet: bool) -> bool:
    """§4: одна попытка за раунд, общая для пуль и ближнего боя; ничья — защитнику."""
    if t.dodge_used or not t.conscious:
        return False
    if bullet and t.effective_ref < rules.DODGE_MIN_REF:
        return False
    t.dodge_used = True
    mods, _ = _check_mods(t, attack=False)
    roll = rules.roll_check(t.dex, mods, rng)
    if roll.total >= attack_total:
        _log(encounter, f"{t.name} уклоняется: DEX {roll.describe()} ≥ {attack_total}.", Kind.MISS)
        return True
    _log(encounter, f"{t.name} пытается уклониться: DEX {roll.describe()} — не успел.")
    return False


# --- урон --------------------------------------------------------------------

def _apply_critical(c: Combatant, kind: str, lines: list[str]) -> None:
    if c.is_boss and kind == "arm":
        _add_status(c, "minus2", None)
        lines.append(f"Крит. травма: {c.name} — босс, вместо травмы руки −2 к следующей проверке.")
        return
    turns = 1 if kind == "concussion" else None
    fresh = _add_status(c, kind, turns)
    tail = "" if fresh else " Такая травма уже есть, не складывается."
    lines.append(f"Крит. травма: {rules.CRITICAL_NAMES[kind]}.{tail}")


def _stun(c: Combatant, lines: list[str]) -> None:
    if c.is_boss:
        _add_status(c, "minus2", None)
        lines.append(f"{c.name} не теряет действие (босс): −2 к следующей проверке.")
    else:
        _add_status(c, "stunned", None)
        lines.append(f"{c.name} оглушён и пропустит следующее действие.")


def _knock_down(encounter: Encounter, c: Combatant, was_up: bool) -> bool:
    if was_up and c.hp <= 0 and not c.is_dead:
        _log(encounter, f"{c.name} падает без сознания (0 HP).", Kind.DOWN)
        return True
    return False


def _strike(encounter, a, t, rng, *, dice, weapon, aim="body", charged=False, bonus_dice=0):
    """
    §5–6. Бросок урона одной атаки: SP, пробитие, хедшот, крит. травма.
    Возвращает (строки журнала, потеря HP).
    """
    lines: list[str] = []
    rolled = rules.roll_d6(dice + bonus_dice, rng)

    sp = t.armor_sp + t.implant_sp + (t.head_sp if aim == "head" else 0)
    sp_note = []
    if charged:
        ignored = min(rules.ARMOR_BASE_SP, t.armor_sp)
        sp -= ignored
        sp_note.append(f"зарядка −{ignored}")
    if weapon.is_melee:
        sp = rules.melee_sp(sp, weapon.sp_divisor, weapon.ignore_sp)
        sp_note.append("¼ SP" if weapon.sp_divisor == 4 else "½ SP")
        if weapon.ignore_sp:
            sp_note.append(f"игнор {weapon.ignore_sp}")

    raw = sum(rolled)
    loss = max(0, raw - sp)
    crit = rules.is_critical(rolled, loss)
    note = f" ({', '.join(sp_note)})" if sp_note else ""
    text = f"Урон {len(rolled)}d6{rolled} = {raw} − SP {sp}{note} → {loss}"
    base = loss

    if crit and a.weapon == "satori":
        extra = rules.roll_d6(2, rng)
        loss += sum(extra)
        text += f", Сатори +2d6{extra}"
    if aim == "head" and loss:
        loss *= 2
        text += ", ×2 в голову"
    lines.append(text + (f" = {loss} HP." if loss != base else " HP."))

    if loss >= 1 and t.armor_sp > 0:
        drop = 3 if a.weapon == "axe" else 1
        t.armor_sp = max(0, t.armor_sp - drop)
        lines.append(f"SP брони цели −{drop}, осталось {t.armor_sp}.")

    was_up = t.hp > 0
    t.hp = max(0, t.hp - loss)

    if loss >= 1:
        if aim == "arm":
            lines.append(f"{t.name} роняет то, что держал в руке.")
        elif aim == "leg":
            _add_status(t, "slowed", 1)
            lines.append(f"{t.name}: скорость вдвое на следующий ход.")
        if charged and aim == "head":
            _tech_headshot(t, weapon, lines)
        if a.weapon == "sting":
            _add_status(t, "bleeding", None)
            lines.append(f"Жало: у {t.name} кровотечение.")

    if crit:
        kind = rules.critical_kind(rng.randint(1, 6))
        _apply_critical(t, kind, lines)
        if a.weapon == "club":
            _stun(t, lines)

    _log(encounter, " ".join(lines), Kind.HIT)
    _knock_down(encounter, t, was_up)
    return loss


def _tech_headshot(t: Combatant, gun, lines: list[str]) -> None:
    """§5: заряженный TECH-выстрел в голову с пробитием."""
    lethal = gun.category in ("shotgun", "sniper")
    if lethal and not t.is_boss:
        t.hp, t.is_dead = 0, True
        lines.append(f"Заряженный выстрел в голову: {t.name} убит на месте.")
        return
    if gun.category in ("pistol", "smg", "assault") or lethal:
        if t.skull and t.implants_on:
            lines.append("Бронированный череп защищает от ослепления.")
        else:
            _add_status(t, "blind", 1)
            lines.append(f"{t.name} ослеплён на 1 ход.")


# --- инициатива и ходы ----------------------------------------------------------

def _break_ties(group: list[Combatant], rng: random.Random) -> list[tuple[Combatant, int]]:
    """§3: при равном REF — d6, ничьи перебрасываются."""
    rolls = {c.pk: rng.randint(1, 6) for c in group}
    ordered = []
    for value in sorted(set(rolls.values()), reverse=True):
        same = [c for c in group if rolls[c.pk] == value]
        ordered.extend([(same[0], value)] if len(same) == 1 else _break_ties(same, rng))
    return ordered


@transaction.atomic
def start(encounter: Encounter, rng: random.Random) -> None:
    everyone = list(encounter.combatants.all())
    if {c.side for c in everyone if c.conscious} != {Combatant.Side.CREW, Combatant.Side.HOSTILE}:
        raise CombatError("Нужен хотя бы один боец в команде и один противник.")

    by_ref: dict[int, list[Combatant]] = {}
    for c in everyone:
        by_ref.setdefault(c.effective_ref, []).append(c)

    parts = []
    for ref in sorted(by_ref, reverse=True):
        group = by_ref[ref]
        ranked = [(group[0], None)] if len(group) == 1 else _break_ties(group, rng)
        for place, (c, d6) in enumerate(ranked):
            c.initiative = ref * 10 + (len(ranked) - place)
            c.init_roll = d6
            c.dodge_used = False
            c.save()
            parts.append(f"{c.name} {ref}" + (f" (d6: {d6})" if d6 else ""))

    encounter.round = 1
    encounter.turn_index = 0
    encounter.save()
    _log(encounter, "Инициатива по REF: " + ", ".join(parts) + ".")
    _begin_turn(encounter, rng)


def _end_of_turn(c: Combatant) -> None:
    """Состояния считаются в ходах персонажа: «1 ход» — до конца следующего его хода."""
    for name, data in list(c.statuses.items()):
        if data.get("left") is not None and data.get("seen"):
            data["left"] -= 1
            if data["left"] <= 0:
                del c.statuses[name]


def _begin_turn(encounter: Encounter, rng: random.Random) -> None:
    order = encounter.turn_order()

    for _ in range(len(order) * 3):
        if (side := winner(encounter)):
            _log(encounter, f"Бой окончен. {WINNER_TEXT[side]}", Kind.DOWN)
            return

        c = Combatant.objects.get(pk=order[encounter.turn_index].pk)
        if c.is_dead:
            _advance(encounter)
            continue

        for data in c.statuses.values():
            data["seen"] = True

        if c.hp > 0:
            _turn_start_damage(encounter, c, rng)

        if c.hp <= 0:
            if not c.is_dead and not c.stabilized:
                _death_check(encounter, c, rng)
            _end_of_turn(c)
            c.save()
            _advance(encounter)
            continue

        c.has_acted = c.statuses.pop("stunned", None) is not None
        c.move_left = 0 if "immobile" in c.statuses else c.speed_m
        c.moved_m = 0
        c.lock_ready = c.lock_target_id is not None
        for key in ("reroll", "nr_discount"):
            c.memory.pop(key, None)
        c.save()

        line = f"Раунд {encounter.round}. Ходит {c.name}."
        if c.has_acted:
            line += " Оглушение: основного действия в этот ход нет."
        _log(encounter, line)
        return


def _turn_start_damage(encounter: Encounter, c: Combatant, rng: random.Random) -> None:
    was_up = c.hp > 0
    lines = []
    if "bleeding" in c.statuses:
        c.hp = max(0, c.hp - 2)
        lines.append("кровотечение −2 HP")
    for name, label in (("burning", "горение"), ("overheat", "перегрев")):
        data = c.statuses.pop(name, None)
        if data:
            rolled = rules.roll_d6(data.get("dice", 1), rng)
            c.hp = max(0, c.hp - sum(rolled))
            lines.append(f"{label} {len(rolled)}d6{rolled} −{sum(rolled)} HP")
    if lines:
        _log(encounter, f"{c.name}: " + ", ".join(lines) + f". Осталось {c.hp} HP.", Kind.HIT)
        _knock_down(encounter, c, was_up)


def _death_check(encounter: Encounter, c: Combatant, rng: random.Random) -> None:
    """§6: BODY + d10 против DV 12 в начале своего хода; первый провал — смерть."""
    roll = rules.roll_check(c.body, 0, rng)
    if roll.total >= rules.BASE_DV:
        _log(encounter, f"{c.name} держится: BODY {roll.describe()} ≥ 12.")
    else:
        c.is_dead = True
        _log(encounter, f"{c.name} умирает: BODY {roll.describe()} < 12.", Kind.DOWN)


def _advance(encounter: Encounter) -> None:
    encounter.turn_index += 1
    if encounter.turn_index >= encounter.combatants.count():
        encounter.turn_index = 0
        encounter.round += 1
        encounter.combatants.update(dodge_used=False)
    encounter.save(update_fields=["turn_index", "round"])


@transaction.atomic
def end_turn(encounter: Encounter, c: Combatant, rng: random.Random) -> None:
    _require_turn(encounter, c)
    _end_of_turn(c)
    c.save()
    _advance(encounter)
    _begin_turn(encounter, rng)


# --- движение -------------------------------------------------------------------

@transaction.atomic
def move(encounter: Encounter, c: Combatant, x: int, y: int) -> None:
    _require_turn(encounter, c)
    if not (0 <= x < encounter.width and 0 <= y < encounter.height):
        raise CombatError("Клетка за пределами карты.")
    if (x, y) == (c.x, c.y):
        raise CombatError("Выберите другую клетку.")
    if "immobile" in c.statuses:
        raise CombatError(f"{c.name} обездвижен.")
    if (x, y) in encounter.wall_set:
        raise CombatError("Там стена.")
    if encounter.combatants.filter(x=x, y=y, is_dead=False).exists():
        raise CombatError("Клетка занята.")
    budget = c.move_left // rules.SQUARE_M
    steps = rules.path_steps(encounter.width, encounter.height, encounter.wall_set,
                             (c.x, c.y), (x, y), budget)
    if steps is None:
        straight = rules.distance(c.x, c.y, x, y) * rules.SQUARE_M
        if straight > c.move_left:
            raise CombatError(f"Далеко: до клетки {straight} м, у {c.name} осталось {c.move_left} м.")
        raise CombatError(f"Не дойти: в обход стен дальше, чем {c.move_left} м.")
    cost = steps * rules.SQUARE_M

    c.x, c.y = x, y
    c.move_left -= cost
    c.moved_m += cost
    c.save()
    _log(encounter, f"{c.name} проходит {cost} м (осталось {c.move_left} м).")


# --- атаки ------------------------------------------------------------------------

AIMS = {"body": "", "head": " в голову", "arm": " в руку", "leg": " в ногу"}


@transaction.atomic
def attack(encounter, a, t, rng, *, aim="body", luck=0, overclock=False, stealth=False):
    _require_turn(encounter, a, action=True)
    if aim not in AIMS:
        raise CombatError("Неизвестная часть тела.")
    if t.pk == a.pk:
        raise CombatError("Выберите другую цель.")
    if t.is_dead:
        raise CombatError(f"{t.name} уже мёртв.")
    if "glitch" in a.statuses:
        raise CombatError(f"У {a.name} сбой оружия до конца следующего хода.")

    weapon = a.get_weapon()
    if weapon.is_melee:
        _melee(encounter, a, t, weapon, rng, aim=aim, luck=luck, stealth=stealth)
    else:
        _shoot(encounter, a, t, weapon, rng, aim=aim, luck=luck, overclock=overclock)

    a.has_acted = True
    a.save()
    t.save()
    if (side := winner(encounter)):
        _log(encounter, f"Бой окончен. {WINNER_TEXT[side]}", Kind.DOWN)


def _shoot(encounter, a, t, gun, rng, *, aim, luck, overclock):
    if a.jammed:
        raise CombatError("Оружие заклинило. Устранение — 1 действие.")
    if gun.arrow and not a.charged:
        raise CombatError("«Вентиль Стрелы» стреляет только после зарядки.")

    metres = _metres(a, t)
    mod = rules.range_mod(gun.category, metres)
    if mod is None:
        raise CombatError(f"{metres} м — слишком далеко: {rules.CATEGORY_NAMES[gun.category].lower()} не достаёт.")

    charged = gun.kind == "tech" and a.charged
    walls = rules.walls_between(encounter.wall_set, a.x, a.y, t.x, t.y)
    if walls > (1 if charged else 0):
        raise CombatError("Стена на линии огня." + ("" if gun.kind != "tech" else
                          " Заряженный TECH пробивает только одну стену."))
    if walls:
        _log(encounter, "Заряженный выстрел идёт сквозь стену.")
    cover = 0
    if t.cover == Combatant.Cover.FULL and not charged:
        raise CombatError(f"{t.name} в полном укрытии: прямой выстрел невозможен.")
    if t.cover == Combatant.Cover.PARTIAL and not charged:
        cover = rules.PARTIAL_COVER_DV
    dv = rules.BASE_DV + mod + cover

    per_die = gun.ammo_per_die
    if per_die and a.ammo < per_die:
        raise CombatError(f"Нужно минимум {per_die} патр. на кубик, в магазине {a.ammo}. Перезарядитесь.")
    if not per_die and a.ammo < 1:
        raise CombatError("Магазин пуст. Перезарядитесь.")

    attacks, jam = 1, False
    if overclock:
        if not gun.overclock:
            raise CombatError(f"{gun.name} не умеет разгон.")
        oc_dv = a.overclock_dv or gun.overclock_dv
        mods, _ = _check_mods(a, attack=False)
        roll = rules.roll_check(a.tech, mods, rng)
        if roll.total >= oc_dv:
            attacks = 2
            _log(encounter, f"{a.name}: разгон удался, TECH {roll.describe()} ≥ {oc_dv}. Две атаки.")
        else:
            jam = True
            _log(encounter, f"{a.name}: разгон сорвался, TECH {roll.describe()} < {oc_dv}. "
                            "Одна атака, потом заклинивание.", Kind.MISS)

    bonus = _hit_bonus(a, t, gun)
    aim_pen = _aim_penalty(a, t, gun, aim)
    luck = _spend_luck(a, luck)
    state_mods, notes = _check_mods(a, attack=True)

    for number in range(attacks):
        if t.is_dead:
            break
        dice = min(gun.dice, a.ammo // per_die) if per_die else gun.dice
        if dice < 1:
            _log(encounter, "На вторую атаку патронов не хватило.")
            break
        a.ammo -= gun.ammo_for(dice)

        mods = bonus - aim_pen + state_mods + (luck if number == 0 else 0)
        roll = rules.roll_check(a.effective_ref, mods, rng)
        head = f"{a.name} → {t.name}{AIMS[aim]}, {gun.name} ({metres} м, DV {dv})"
        if notes:
            head += f" [{', '.join(notes)}]"

        if roll.total < dv and a.role == Combatant.Role.GUNNER and not a.memory.get("reroll"):
            a.memory["reroll"] = True
            _log(encounter, f"{head}: REF {roll.describe()} — промах, Стрелок перебрасывает.", Kind.MISS)
            roll = rules.roll_check(a.effective_ref, mods, rng)

        if roll.total < dv:
            _log(encounter, f"{head}: REF {roll.describe()} — промах.", Kind.MISS)
            continue

        _log(encounter, f"{head}: REF {roll.describe()} — попадание.", Kind.HIT)
        if _dodged(encounter, t, roll.total, rng, bullet=True):
            continue

        _strike(encounter, a, t, rng, dice=dice, weapon=gun, aim=aim, charged=charged)
        if gun.arrow:
            _arrow_blast(encounter, a, t, rng)

    if charged:
        a.charged = False
    if jam:
        a.jammed = True


def _arrow_blast(encounter, a, t, rng):
    """§9: взрыв 3d6 радиусом 2 м, SP считается отдельно и полностью."""
    for victim in _in_radius(encounter, a, t.x, t.y, 1, also=t):
        if victim.is_dead:
            continue
        _explosion(encounter, victim, rng, "Взрыв «Вентиля Стрелы»", dice=3)
        if victim not in (a, t):
            victim.save()


def _melee(encounter, a, t, blade, rng, *, aim, luck, stealth):
    if _metres(a, t) > max(blade.reach_m, rules.SQUARE_M):
        reach = "до соседней клетки" if blade.reach_m < 4 else f"на {blade.reach_m} м"
        raise CombatError(f"{blade.name} достаёт только {reach}.")
    if rules.walls_between(encounter.wall_set, a.x, a.y, t.x, t.y):
        raise CombatError("Между вами стена.")

    mods = _hit_bonus(a, t, blade) - _aim_penalty(a, t, blade, aim)
    if blade.min_body and a.body < blade.min_body:
        mods -= 4
    mods += _spend_luck(a, luck)
    state_mods, notes = _check_mods(a, attack=True)
    mods += state_mods

    roll = rules.roll_check(a.dex, mods, rng)
    head = f"{a.name} → {t.name}{AIMS[aim]}, {blade.name} (DV 12)"
    if notes:
        head += f" [{', '.join(notes)}]"
    if roll.total < rules.BASE_DV:
        _log(encounter, f"{head}: DEX {roll.describe()} — промах.", Kind.MISS)
        return
    _log(encounter, f"{head}: DEX {roll.describe()} — попадание.", Kind.HIT)

    if _dodged(encounter, t, roll.total, rng, bullet=False):
        return

    bonus, why = 0, []
    if stealth and a.weapon == "tanto":
        bonus += 2
        why.append("танто из скрытности +2d6")
    if stealth and a.weapon == "cocktail" and not a.memory.get("cocktail"):
        a.memory["cocktail"] = True
        bonus += 2
        why.append("палочка из скрытности +2d6")
    if a.weapon == "machete" and t.armor_sp + t.implant_sp == 0:
        bonus += 1
        why.append("мачете по цели без SP +1d6")
    hit_before = a.memory.setdefault("hit", [])
    if t.pk not in hit_before:
        hit_before.append(t.pk)
        if a.role == Combatant.Role.ASSASSIN:
            bonus += 1
            why.append("первый удар убийцы по цели +1d6")
    if why:
        _log(encounter, "Бонус: " + ", ".join(why) + ".")

    was_up = t.hp > 0
    loss = _strike(encounter, a, t, rng, dice=blade.dice, weapon=blade, aim=aim, bonus_dice=bonus)

    if a.weapon == "tanto" and stealth and loss:
        t.dodge_used = True
    if was_up and t.hp <= 0:
        _kill_bonus(encounter, a)


def _kill_bonus(encounter: Encounter, a: Combatant) -> None:
    extra = 0
    if a.role == Combatant.Role.ASSASSIN and a.memory.get("kill_round") != encounter.round:
        a.memory["kill_round"] = encounter.round
        extra += 4
    if a.weapon == "byakko":
        extra += 4
    if extra:
        a.move_left += extra
        _log(encounter, f"{a.name} получает +{extra} м свободного движения после убийства.")


# --- гранаты -----------------------------------------------------------------------

DIRECTIONS = [(0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1)]
DIRECTION_NAMES = ["север", "северо-восток", "восток", "юго-восток", "юг", "юго-запад", "запад", "северо-запад"]


def _in_radius(encounter, actor, x, y, squares, also=None):
    people = []
    for c in encounter.combatants.filter(is_dead=False):
        if actor and c.pk == actor.pk:
            c = actor
        elif also and c.pk == also.pk:
            c = also
        if rules.distance(c.x, c.y, x, y) <= squares:
            c.behind_wall = bool(rules.walls_between(encounter.wall_set, x, y, c.x, c.y))
            people.append(c)
    return people


def _shielded(c: Combatant) -> bool:
    """§10: полное укрытие (в том числе стена) между центром взрыва и целью."""
    return c.cover == Combatant.Cover.FULL or getattr(c, "behind_wall", False)


def _explosion(encounter, c, rng, label, *, dice, ignore_armor=False, sp_note=""):
    """Взрывы: без уклонения и без крит. травм (§10). Полное укрытие спасает."""
    if _shielded(c):
        _log(encounter, f"{label}: {c.name} за укрытием, не задет.")
        return None
    return _flat_damage(encounter, c, rng, label, dice=dice, ignore_armor=ignore_armor,
                        sp_note=sp_note)


def _flat_damage(encounter, c, rng, label, *, dice, ignore_armor=False, sp_note=""):
    """Урон без попадания по части тела и без крит. травм: взрывы и квикхаки."""
    rolled = rules.roll_d6(dice, rng)
    sp = c.implant_sp if ignore_armor else c.armor_sp + c.implant_sp
    loss = max(0, sum(rolled) - sp)
    text = f"{label}: {c.name} — {dice}d6{rolled} = {sum(rolled)} − SP {sp}{sp_note} → {loss} HP."
    if loss and not ignore_armor and c.armor_sp:
        c.armor_sp -= 1
        text += f" SP брони −1, осталось {c.armor_sp}."
    was_up = c.hp > 0
    c.hp = max(0, c.hp - loss)
    _log(encounter, text, Kind.HIT if loss else Kind.INFO)
    _knock_down(encounter, c, was_up)
    return loss


@transaction.atomic
def throw_grenade(encounter, a, x, y, kind, rng, *, luck=0):
    _require_turn(encounter, a, action=True)
    if kind not in rules.GRENADES:
        raise CombatError("Неизвестная граната.")
    grenade = rules.GRENADES[kind]
    if getattr(a, kind) < 1:
        raise CombatError(f"У {a.name} нет гранат «{grenade.name}».")
    if not (0 <= x < encounter.width and 0 <= y < encounter.height):
        raise CombatError("Клетка за пределами карты.")
    metres = rules.distance(a.x, a.y, x, y) * rules.SQUARE_M
    if metres > rules.GRENADE_RANGE_M:
        raise CombatError(f"Граната летит до 20 м, а тут {metres} м.")
    if (x, y) in encounter.wall_set:
        raise CombatError("Это клетка стены — целься рядом.")

    mods = _spend_luck(a, luck)
    state_mods, _ = _check_mods(a, attack=True)
    roll = rules.roll_check(a.dex, mods + state_mods, rng)
    setattr(a, kind, getattr(a, kind) - 1)
    a.has_acted = True

    if roll.total >= rules.BASE_DV:
        _log(encounter, f"{a.name} бросает «{grenade.name}» на {metres} м: DEX {roll.describe()} — точно.")
    else:
        d = rng.randint(0, 7)
        x = min(max(x + DIRECTIONS[d][0], 0), encounter.width - 1)
        y = min(max(y + DIRECTIONS[d][1], 0), encounter.height - 1)
        _log(encounter, f"{a.name} бросает «{grenade.name}»: DEX {roll.describe()} — мимо, "
                        f"граната уходит на ≈3 м, {DIRECTION_NAMES[d]} (d8: {d + 1}).", Kind.MISS)

    radius = grenade.radius_m // rules.SQUARE_M
    encounter.fx = {"blast": [x, y, radius, kind]}
    victims = _in_radius(encounter, a, x, y, radius)
    if not victims:
        _log(encounter, "В радиусе взрыва никого.")

    for c in victims:
        if _shielded(c):
            _log(encounter, f"{grenade.name}: {c.name} за укрытием, не задет.")
        elif kind == "frag":
            _flat_damage(encounter, c, rng, "Осколочная", dice=4)
        elif kind == "fire":
            _flat_damage(encounter, c, rng, "Зажигательная", dice=3)
            _add_status(c, "burning", None, dice=1)
        elif kind == "emp" and not c.cyberized:
            _log(encounter, f"ЭМИ: у {c.name} нет имплантов, эффекта нет.")
        elif kind == "emp":
            _flat_damage(encounter, c, rng, "ЭМИ", dice=2, ignore_armor=True,
                         sp_note=" (обычная броня не считается)")
            _add_status(c, "implants_off", 1)
        elif kind == "flash":
            _add_status(c, "blind", 1)
            _log(encounter, f"Светошумовая: {c.name} ослеплён на 1 ход.")
        if c.pk != a.pk:
            c.save()

    a.save()
    if (side := winner(encounter)):
        _log(encounter, f"Бой окончен. {WINNER_TEXT[side]}", Kind.DOWN)


# --- нетраннинг ------------------------------------------------------------------------

@transaction.atomic
def quickhack(encounter, a, t, hack, rng, *, luck=0):
    _require_turn(encounter, a, action=True)
    if not a.deck:
        raise CombatError(f"У {a.name} нет кибердеки.")
    if not a.implants_on:
        raise CombatError("Импланты отключены — дека не работает.")
    if hack not in rules.QUICKHACKS:
        raise CombatError("Неизвестный квикхак.")
    if t.pk == a.pk or t.is_dead:
        raise CombatError("Выберите живую цель.")

    name, cost = rules.QUICKHACKS[hack]
    discounts = []
    if a.role == Combatant.Role.NETRUNNER and not a.memory.get("nr_discount"):
        cost -= 1
        discounts.append("нетраннер −1")
    if a.deck == "raven" and not a.memory.get("raven"):
        cost -= 2
        discounts.append("Raven −2")
    cost = max(1, cost)
    if a.ram < cost:
        raise CombatError(f"«{name}» стоит {cost} RAM, у {a.name} {a.ram}.")

    # §11: RAM уходит при попытке, даже если взлом не прошёл.
    a.ram -= cost
    if "нетраннер −1" in discounts:
        a.memory["nr_discount"] = True
    if "Raven −2" in discounts:
        a.memory["raven"] = True
    a.has_acted = True

    dv = 16 if t.deck else rules.HACK_DV[t.tier]
    mods = _spend_luck(a, luck)
    state_mods, _ = _check_mods(a, attack=False)
    roll = rules.roll_check(a.intel, mods + state_mods, rng)
    price = f"{cost} RAM" + (f", {', '.join(discounts)}" if discounts else "")
    head = f"{a.name}: «{name}» на {t.name} ({price}), INT {roll.describe()} против DV {dv}"

    if roll.total < dv:
        _log(encounter, head + " — взлом не прошёл.", Kind.MISS)
        a.save()
        return

    _log(encounter, head + " — успех.", Kind.HIT)
    if hack == "ping":
        _log(encounter, "Ping: ведущий показывает подключённые устройства и цели в сети.")
    elif hack == "glitch":
        _add_status(t, "glitch", 1)
        _log(encounter, f"{t.name} не может пользоваться оружием 1 ход.")
    elif hack == "optics":
        _add_status(t, "blind", 1)
        _log(encounter, f"{t.name} ослеплён 1 ход.")
    elif hack == "cripple":
        _add_status(t, "immobile", 1)
        _log(encounter, f"{t.name} не сможет двигаться в следующий ход.")
    else:
        dice = 3 if hack == "short" else 2
        if a.deck == "militech" and not a.memory.get("militech"):
            a.memory["militech"] = True
            dice += 1
            _log(encounter, "Militech: +1d6 к первому атакующему квикхаку.")
        _flat_damage(encounter, t, rng, name, dice=dice, ignore_armor=True,
                     sp_note=" (обычная броня не защищает)")
        if hack == "overheat":
            _add_status(t, "overheat", None, dice=2)
            _log(encounter, f"В начале следующего хода {t.name} получит ещё 2d6.")

    t.save()
    a.save()
    if (side := winner(encounter)):
        _log(encounter, f"Бой окончен. {WINNER_TEXT[side]}", Kind.DOWN)


@transaction.atomic
def drain_ram(encounter, a, t):
    """§11: 1 действие у выведенного из строя кибернизированного врага."""
    _require_turn(encounter, a, action=True)
    if not a.deck:
        raise CombatError(f"У {a.name} нет кибердеки.")
    if _metres(a, t) > rules.SQUARE_M:
        raise CombatError("Нужно стоять вплотную к телу.")
    if t.conscious or not t.cyberized:
        raise CombatError("RAM снимают только с выведенного из строя кибернизированного врага.")
    if t.memory.get("drained"):
        raise CombatError(f"С {t.name} RAM уже сняли.")
    gain = 4 if (t.deck and t.tier != Combatant.Tier.REGULAR) else 3
    before = a.ram
    a.ram = min(a.ram_max, a.ram + gain)
    a.has_acted = True
    t.memory["drained"] = True
    a.save()
    t.save()
    _log(encounter, f"{a.name} снимает RAM с {t.name}: +{a.ram - before} (теперь {a.ram}/{a.ram_max}).")


# --- прочие действия --------------------------------------------------------------------

def _gun(c: Combatant):
    weapon = c.get_weapon()
    if weapon.is_melee:
        raise CombatError(f"{weapon.name} — холодное оружие.")
    return weapon


@transaction.atomic
def reload(encounter, c):
    _require_turn(encounter, c, action=True)
    gun = _gun(c)
    if c.ammo >= c.mag_size:
        raise CombatError("Магазин и так полный.")
    c.ammo = c.mag_size
    c.has_acted = True
    c.save()
    _log(encounter, f"{c.name} перезаряжает {gun.name} ({c.ammo}).")


@transaction.atomic
def unjam(encounter, c):
    _require_turn(encounter, c, action=True)
    if not c.jammed:
        raise CombatError("Оружие не заклинило.")
    c.jammed = False
    c.has_acted = True
    c.save()
    _log(encounter, f"{c.name} устраняет заклинивание.")


@transaction.atomic
def charge(encounter, c):
    _require_turn(encounter, c, action=True)
    gun = _gun(c)
    if gun.kind != "tech":
        raise CombatError(f"{gun.name} — не TECH-оружие.")
    if c.charged:
        raise CombatError("Уже заряжено, несколько зарядок не копятся.")
    c.charged = True
    c.has_acted = True
    c.save()
    _log(encounter, f"{c.name} заряжает {gun.name}. Выстрел — следующим действием.")


@transaction.atomic
def lock(encounter, c, t):
    _require_turn(encounter, c, action=True)
    gun = _gun(c)
    if gun.kind != "smart":
        raise CombatError(f"{gun.name} — не Smart-оружие.")
    if t.pk == c.pk or t.is_dead:
        raise CombatError("Выберите другую цель.")
    c.lock_target = t
    c.lock_ready = False
    c.has_acted = True
    c.save()
    _log(encounter, f"{c.name} берёт {t.name} в захват. Со следующего хода прицельные "
                    "выстрелы по нему без −3.")


def _near(a: Combatant, t: Combatant) -> None:
    if t.pk != a.pk and _metres(a, t) > rules.SQUARE_M:
        raise CombatError(f"{t.name} слишком далеко, нужно стоять рядом.")


@transaction.atomic
def medkit(encounter, a, t, rng):
    _require_turn(encounter, a, action=True)
    _near(a, t)
    if t.pk == a.pk:
        t = a
    if a.medkits < 1:
        raise CombatError(f"У {a.name} нет аптечек.")
    if t.is_dead:
        raise CombatError(f"{t.name} мёртв.")
    if t.medkits_received >= rules.MEDKIT_LIMIT:
        raise CombatError(f"На {t.name} уже ушло две аптечки за бой.")
    if t.hp >= t.hp_max and "bleeding" not in t.statuses:
        raise CombatError(f"{t.name} не ранен.")

    rolled = rules.roll_d6(2, rng)
    before = t.hp
    t.hp = min(t.hp_max, t.hp + sum(rolled))
    t.medkits_received += 1
    a.medkits -= 1
    a.has_acted = True
    text = f"{a.name} применяет аптечку на {t.name}: 2d6{rolled} → {before}→{t.hp} HP."
    if t.statuses.pop("bleeding", None):
        text += " Кровотечение остановлено."
    if before <= 0 < t.hp:
        t.stabilized = False
        text += f" {t.name} приходит в себя."
    t.save()
    if t is not a:
        a.save()
    _log(encounter, text)


@transaction.atomic
def stabilize(encounter, a, t, rng):
    _require_turn(encounter, a, action=True)
    _near(a, t)
    if t.pk == a.pk or t.hp > 0 or t.is_dead or t.stabilized:
        raise CombatError("Стабилизировать можно только союзника без сознания.")
    mods, _ = _check_mods(a, attack=False)
    roll = rules.roll_check(a.tech, mods, rng)
    a.has_acted = True
    if roll.total >= rules.BASE_DV:
        t.stabilized = True
        _log(encounter, f"{a.name} стабилизирует {t.name}: TECH {roll.describe()}. "
                        "Проверок смерти больше нет, HP остаётся 0.")
    else:
        _log(encounter, f"{a.name} пытается стабилизировать {t.name}: TECH {roll.describe()} — не вышло.",
             Kind.MISS)
    t.save()
    a.save()


@transaction.atomic
def stop_bleeding(encounter, a, t, rng):
    _require_turn(encounter, a, action=True)
    _near(a, t)
    target = a if t.pk == a.pk else t
    if "bleeding" not in target.statuses:
        raise CombatError(f"У {target.name} нет кровотечения.")
    mods, _ = _check_mods(a, attack=False)
    roll = rules.roll_check(a.tech, mods, rng)
    a.has_acted = True
    if roll.total >= rules.BASE_DV:
        target.statuses.pop("bleeding")
        _log(encounter, f"{a.name} останавливает кровотечение у {target.name}: TECH {roll.describe()}.")
    else:
        _log(encounter, f"{a.name} не смог остановить кровотечение: TECH {roll.describe()}.", Kind.MISS)
    if target is not a:
        target.save()
    a.save()


@transaction.atomic
def set_cover(encounter, c, cover):
    """Инструмент ведущего: укрытие меняют в любой момент, действие не тратится."""
    if cover not in Combatant.Cover.values:
        raise CombatError("Неизвестное укрытие.")
    c.cover = cover
    c.save(update_fields=["cover"])
    label = dict(Combatant.Cover.choices)[cover].lower()
    _log(encounter, f"Укрытие {c.name}: {label}.")


@transaction.atomic
def toggle_walls(encounter: Encounter, cells: list, build: bool) -> None:
    """
    Инструмент ведущего: поставить или снять стены. Работает и во время боя,
    действие не тратится. Клетку, где кто-то стоит, застроить нельзя.
    """
    walls = encounter.wall_set
    taken = {(c.x, c.y) for c in encounter.combatants.filter(is_dead=False)}
    for raw in cells[:400]:
        try:
            x, y = int(raw[0]), int(raw[1])
        except (TypeError, ValueError, IndexError):
            raise CombatError("Неверная клетка.") from None
        if not (0 <= x < encounter.width and 0 <= y < encounter.height):
            continue
        if build and (x, y) not in taken:
            walls.add((x, y))
        elif not build:
            walls.discard((x, y))
    encounter.walls = sorted([x, y] for x, y in walls)
    encounter.save(update_fields=["walls"])


@transaction.atomic
def clear_walls(encounter: Encounter) -> None:
    encounter.walls = []
    encounter.save(update_fields=["walls"])


@transaction.atomic
def reset(encounter: Encounter) -> None:
    """
    Назад к расстановке. §5: после боя возвращается половина потерянного SP
    (вверх). Аптечки, гранаты и удача не возвращаются — это расходники и
    запас на сессию; их правят в карточке бойца.
    """
    encounter.round = 0
    encounter.turn_index = 0
    encounter.save()
    for c in encounter.combatants.all():
        lost = c.armor_sp_max - c.armor_sp
        c.armor_sp += math.ceil(lost / 2)
        c.hp = c.hp_max
        c.ammo = c.mag_size
        c.ram = c.ram_max
        c.is_dead = c.stabilized = c.jammed = c.charged = c.lock_ready = False
        c.lock_target = None
        c.dodge_used = False
        c.statuses, c.memory = {}, {}
        c.medkits_received = 0
        c.initiative = c.init_roll = None
        c.move_left = c.moved_m = 0
        c.has_acted = False
        c.save()
    encounter.log.all().delete()


# --- состояние для карты ---------------------------------------------------------------

def _status_list(c: Combatant) -> list[str]:
    names = []
    for key, data in c.statuses.items():
        label = STATUS_NAMES.get(key, key)
        if data.get("left"):
            label += f" ({data['left']} х.)"
        names.append(label)
    return names


def state(encounter: Encounter) -> dict:
    active = encounter.active_combatant()
    order = encounter.turn_order() if encounter.started else list(encounter.combatants.all())
    side = winner(encounter)

    fighters = []
    for c in order:
        weapon = c.get_weapon()
        reach = (max(weapon.reach_m, rules.SQUARE_M) if weapon.is_melee
                 else rules.max_range_m(weapon.category))
        fighters.append({
            "id": c.pk,
            "name": c.name,
            "side": c.side,
            "tier": c.tier,
            "tierLabel": c.get_tier_display(),
            "role": c.get_role_display() if c.role else "",
            "x": c.x,
            "y": c.y,
            "hp": c.hp,
            "hpMax": c.hp_max,
            "sp": c.armor_sp + c.implant_sp,
            "spHead": c.armor_sp + c.implant_sp + c.head_sp,
            "weapon": weapon.name,
            "weaponKind": weapon.kind,
            "melee": weapon.is_melee,
            "reachSquares": reach // rules.SQUARE_M,
            "overclock": weapon.overclock,
            "stealthWeapon": c.weapon in ("tanto", "cocktail"),
            "ammo": c.ammo,
            "magazine": c.mag_size,
            "jammed": c.jammed,
            "charged": c.charged,
            "lockTarget": c.lock_target_id,
            "lockReady": c.lock_ready,
            "ram": c.ram,
            "ramMax": c.ram_max,
            "luck": c.luck_left,
            "medkits": c.medkits,
            "grenades": {k: getattr(c, k) for k in rules.GRENADES},
            "cover": c.cover,
            "initiative": c.effective_ref if c.initiative is not None else None,
            "initRoll": c.init_roll,
            "condition": c.condition,
            "statuses": _status_list(c),
            "bleeding": "bleeding" in c.statuses,
            "moveLeft": c.move_left,
            "hasActed": c.has_acted,
            "dodgeUsed": c.dodge_used,
        })

    return {
        "id": encounter.pk,
        "width": encounter.width,
        "height": encounter.height,
        "round": encounter.round,
        "started": encounter.started,
        "walls": encounter.walls,
        "fx": getattr(encounter, "fx", None),
        "activeId": active.pk if active and not side else None,
        "winner": side,
        "winnerText": WINNER_TEXT.get(side, ""),
        "combatants": fighters,
        "grenadeNames": {k: g.name for k, g in rules.GRENADES.items()},
        "hacks": [{"key": k, "name": n, "ram": r} for k, (n, r) in rules.QUICKHACKS.items()],
        "log": [
            {"id": e.pk, "round": e.round, "kind": e.kind, "text": e.text}
            for e in encounter.log.all()[:60]
        ],
    }
