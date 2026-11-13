"""
Combat flow: "is this allowed" checks, model changes and log entries.
Views call only the functions in this module. Any rules violation raises
CombatError with a message for the player.
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
    "blind": "blinded",
    "concussion": "concussion",
    "stunned": "stunned",
    "immobile": "immobilised",
    "glitch": "weapon glitch",
    "implants_off": "implants off",
    "slowed": "half speed",
    "burning": "burning",
    "overheat": "overheating",
    "bleeding": "bleeding",
    "arm": "arm injury",
    "leg": "leg injury",
    "minus2": "−2 to next check",
}

# Persistent conditions last until treated; the others trigger at turn start.
PERSISTENT = {"bleeding", "arm", "leg"}
ON_TURN_START = {"burning", "overheat", "stunned"}
UNTIL_NEXT_CHECK = {"minus2"}


# --- helpers ----------------------------------------------------------------------

def _log(encounter: Encounter, text: str, kind: str = Kind.INFO) -> None:
    LogEntry.objects.create(encounter=encounter, round=encounter.round, kind=kind, text=text)


def _metres(a: Combatant, b: Combatant) -> int:
    return rules.distance(a.x, a.y, b.x, b.y) * rules.SQUARE_M


def _add_status(c: Combatant, name: str, turns: int | None = 1, **extra) -> bool:
    """Applying the same condition again refreshes it but does not stack."""
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


WINNER_TEXT = {"crew": "The crew wins.", "hostile": "The hostiles win.",
               "nobody": "Nobody is left standing."}


def _require_turn(encounter: Encounter, c: Combatant, *, action: bool = False) -> None:
    if not encounter.started:
        raise CombatError("Start the fight first.")
    if winner(encounter):
        raise CombatError("The fight is over.")
    active = encounter.active_combatant()
    if active is None or active.pk != c.pk:
        raise CombatError(f"It is not {c.name}'s turn.")
    if not c.conscious:
        raise CombatError(f"{c.name} is unconscious.")
    if action and c.has_acted:
        raise CombatError(f"{c.name} has already used the action this turn.")


def _check_mods(c: Combatant, *, attack: bool) -> tuple[int, list[str]]:
    """Condition penalties for a check. "−2 to next check" is used up here."""
    mods, notes = 0, []
    if "blind" in c.statuses:
        mods -= 4 if attack else 2
        notes.append("blinded")
    if "concussion" in c.statuses:
        mods -= 2
        notes.append("concussion")
    if c.statuses.pop("minus2", None) is not None:
        mods -= 2
        notes.append("−2 from injury")
    return mods, notes


def _spend_luck(c: Combatant, luck: int) -> int:
    if not 0 <= luck <= 3:
        raise CombatError("Luck is spent 1–3 points per check.")
    if luck > c.luck_left:
        raise CombatError(f"{c.name} has only {c.luck_left} luck left.")
    c.luck_left -= luck
    return luck


def _hit_bonus(a: Combatant, t: Combatant, weapon) -> int:
    """§4: class + weapon + implants, +4 at most in total."""
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
        return 0                                     # §8: a lock removes the −3
    if a.optics and a.implants_on:
        return rules.AIM_PENALTY_OPTICS
    return rules.AIM_PENALTY


def _dodged(encounter, t: Combatant, attack_total: int, rng: random.Random, *, bullet: bool) -> bool:
    """§4: one attempt per round, shared by bullets and melee; ties go to the defender."""
    if t.dodge_used or not t.conscious:
        return False
    if bullet and t.effective_ref < rules.DODGE_MIN_REF:
        return False
    t.dodge_used = True
    mods, _ = _check_mods(t, attack=False)
    roll = rules.roll_check(t.dex, mods, rng)
    if roll.total >= attack_total:
        _log(encounter, f"{t.name} dodges: DEX {roll.describe()} ≥ {attack_total}.", Kind.MISS)
        return True
    _log(encounter, f"{t.name} tries to dodge: DEX {roll.describe()}, too slow.")
    return False


# --- damage ---------------------------------------------------------------------------

def _apply_critical(c: Combatant, kind: str, lines: list[str]) -> None:
    if c.is_boss and kind == "arm":
        _add_status(c, "minus2", None)
        lines.append(f"Critical injury: {c.name} is a boss, so instead of an arm injury −2 to the next check.")
        return
    turns = 1 if kind == "concussion" else None
    fresh = _add_status(c, kind, turns)
    tail = "" if fresh else " Already has this injury, it does not stack."
    lines.append(f"Critical injury: {rules.CRITICAL_NAMES[kind]}.{tail}")


def _stun(c: Combatant, lines: list[str]) -> None:
    if c.is_boss:
        _add_status(c, "minus2", None)
        lines.append(f"{c.name} keeps the action (boss): −2 to the next check.")
    else:
        _add_status(c, "stunned", None)
        lines.append(f"{c.name} is stunned and will lose the next action.")


def _knock_down(encounter: Encounter, c: Combatant, was_up: bool) -> bool:
    if was_up and c.hp <= 0 and not c.is_dead:
        _log(encounter, f"{c.name} falls unconscious (0 HP).", Kind.DOWN)
        return True
    return False


def _strike(encounter, a, t, rng, *, dice, weapon, aim="body", charged=False, bonus_dice=0):
    """
    §5–6. Damage roll for one attack: SP, armour wear, headshot, critical injury.
    Returns the HP lost.
    """
    lines: list[str] = []
    rolled = rules.roll_d6(dice + bonus_dice, rng)

    sp = t.armor_sp + t.implant_sp + (t.head_sp if aim == "head" else 0)
    sp_note = []
    if charged:
        ignored = min(rules.ARMOR_BASE_SP, t.armor_sp)
        sp -= ignored
        sp_note.append(f"charge −{ignored}")
    if weapon.is_melee:
        sp = rules.melee_sp(sp, weapon.sp_divisor, weapon.ignore_sp)
        sp_note.append("¼ SP" if weapon.sp_divisor == 4 else "½ SP")
        if weapon.ignore_sp:
            sp_note.append(f"ignores {weapon.ignore_sp}")

    raw = sum(rolled)
    loss = max(0, raw - sp)
    crit = rules.is_critical(rolled, loss)
    note = f" ({', '.join(sp_note)})" if sp_note else ""
    text = f"Damage {len(rolled)}d6{rolled} = {raw} − SP {sp}{note} → {loss}"
    base = loss

    if crit and a.weapon == "satori":
        extra = rules.roll_d6(2, rng)
        loss += sum(extra)
        text += f", Satori +2d6{extra}"
    if aim == "head" and loss:
        loss *= 2
        text += ", ×2 to the head"
    lines.append(text + (f" = {loss} HP." if loss != base else " HP."))

    if loss >= 1 and t.armor_sp > 0:
        drop = 3 if a.weapon == "axe" else 1
        t.armor_sp = max(0, t.armor_sp - drop)
        lines.append(f"Target armour −{drop} SP, {t.armor_sp} left.")

    was_up = t.hp > 0
    t.hp = max(0, t.hp - loss)

    if loss >= 1:
        if aim == "arm":
            lines.append(f"{t.name} drops whatever was in that hand.")
        elif aim == "leg":
            _add_status(t, "slowed", 1)
            lines.append(f"{t.name}: half speed next turn.")
        if charged and aim == "head":
            _tech_headshot(t, weapon, lines)
        if a.weapon == "sting":
            _add_status(t, "bleeding", None)
            lines.append(f"Sting: {t.name} is bleeding.")

    if crit:
        kind = rules.critical_kind(rng.randint(1, 6))
        _apply_critical(t, kind, lines)
        if a.weapon == "club":
            _stun(t, lines)

    _log(encounter, " ".join(lines), Kind.HIT)
    _knock_down(encounter, t, was_up)
    return loss


def _tech_headshot(t: Combatant, gun, lines: list[str]) -> None:
    """§5: a charged TECH shot to the head that gets through armour."""
    lethal = gun.category in ("shotgun", "sniper")
    if lethal and not t.is_boss:
        t.hp, t.is_dead = 0, True
        lines.append(f"Charged headshot: {t.name} is killed outright.")
        return
    if gun.category in ("pistol", "smg", "assault") or lethal:
        if t.skull and t.implants_on:
            lines.append("The armoured skull prevents blinding.")
        else:
            _add_status(t, "blind", 1)
            lines.append(f"{t.name} is blinded for 1 turn.")


# --- initiative and turns ---------------------------------------------------------

def _break_ties(group: list[Combatant], rng: random.Random) -> list[tuple[Combatant, int]]:
    """§3: equal REF rolls a d6, ties are rerolled."""
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
        raise CombatError("You need at least one crew member and one hostile.")

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
    _log(encounter, "Initiative by REF: " + ", ".join(parts) + ".")
    _begin_turn(encounter, rng)


def _end_of_turn(c: Combatant) -> None:
    """Conditions count the character's own turns: "1 turn" lasts until the end of their next turn."""
    for name, data in list(c.statuses.items()):
        if data.get("left") is not None and data.get("seen"):
            data["left"] -= 1
            if data["left"] <= 0:
                del c.statuses[name]


def _begin_turn(encounter: Encounter, rng: random.Random) -> None:
    order = encounter.turn_order()

    for _ in range(len(order) * 3):
        if (side := winner(encounter)):
            _log(encounter, f"The fight is over. {WINNER_TEXT[side]}", Kind.DOWN)
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

        line = f"Round {encounter.round}. {c.name}'s turn."
        if c.has_acted:
            line += " Stunned: no main action this turn."
        _log(encounter, line)
        return


def _turn_start_damage(encounter: Encounter, c: Combatant, rng: random.Random) -> None:
    was_up = c.hp > 0
    lines = []
    if "bleeding" in c.statuses:
        c.hp = max(0, c.hp - 2)
        lines.append("bleeding −2 HP")
    for name, label in (("burning", "burning"), ("overheat", "overheat")):
        data = c.statuses.pop(name, None)
        if data:
            rolled = rules.roll_d6(data.get("dice", 1), rng)
            c.hp = max(0, c.hp - sum(rolled))
            lines.append(f"{label} {len(rolled)}d6{rolled} −{sum(rolled)} HP")
    if lines:
        _log(encounter, f"{c.name}: " + ", ".join(lines) + f". {c.hp} HP left.", Kind.HIT)
        _knock_down(encounter, c, was_up)


def _death_check(encounter: Encounter, c: Combatant, rng: random.Random) -> None:
    """§6: BODY + d10 against DV 12 at the start of own turn; the first failure is death."""
    roll = rules.roll_check(c.body, 0, rng)
    if roll.total >= rules.BASE_DV:
        _log(encounter, f"{c.name} holds on: BODY {roll.describe()} ≥ 12.")
    else:
        c.is_dead = True
        _log(encounter, f"{c.name} dies: BODY {roll.describe()} < 12.", Kind.DOWN)


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


# --- movement --------------------------------------------------------------------------

@transaction.atomic
def move(encounter: Encounter, c: Combatant, x: int, y: int) -> None:
    _require_turn(encounter, c)
    if not (0 <= x < encounter.width and 0 <= y < encounter.height):
        raise CombatError("That square is outside the map.")
    if (x, y) == (c.x, c.y):
        raise CombatError("Pick a different square.")
    if "immobile" in c.statuses:
        raise CombatError(f"{c.name} is immobilised.")
    if (x, y) in encounter.wall_set:
        raise CombatError("There is a wall there.")
    if encounter.combatants.filter(x=x, y=y, is_dead=False).exists():
        raise CombatError("That square is taken.")
    budget = c.move_left // rules.SQUARE_M
    steps = rules.path_steps(encounter.width, encounter.height, encounter.wall_set,
                             (c.x, c.y), (x, y), budget)
    if steps is None:
        straight = rules.distance(c.x, c.y, x, y) * rules.SQUARE_M
        if straight > c.move_left:
            raise CombatError(f"Too far: the square is {straight} m away, {c.name} has {c.move_left} m left.")
        raise CombatError(f"Can't get there: the way around the walls is longer than {c.move_left} m.")
    cost = steps * rules.SQUARE_M

    c.x, c.y = x, y
    c.move_left -= cost
    c.moved_m += cost
    c.save()
    _log(encounter, f"{c.name} moves {cost} m ({c.move_left} m left).")


# --- attacks ----------------------------------------------------------------------------

AIMS = {"body": "", "head": " (head)", "arm": " (arm)", "leg": " (leg)"}


@transaction.atomic
def attack(encounter, a, t, rng, *, aim="body", luck=0, overclock=False, stealth=False):
    _require_turn(encounter, a, action=True)
    if aim not in AIMS:
        raise CombatError("Unknown body part.")
    if t.pk == a.pk:
        raise CombatError("Pick another target.")
    if t.is_dead:
        raise CombatError(f"{t.name} is already dead.")
    if "glitch" in a.statuses:
        raise CombatError(f"{a.name}'s weapon is glitched until the end of their next turn.")

    weapon = a.get_weapon()
    if weapon.is_melee:
        _melee(encounter, a, t, weapon, rng, aim=aim, luck=luck, stealth=stealth)
    else:
        _shoot(encounter, a, t, weapon, rng, aim=aim, luck=luck, overclock=overclock)

    a.has_acted = True
    a.save()
    t.save()
    if (side := winner(encounter)):
        _log(encounter, f"The fight is over. {WINNER_TEXT[side]}", Kind.DOWN)


def _shoot(encounter, a, t, gun, rng, *, aim, luck, overclock):
    if a.jammed:
        raise CombatError("The weapon is jammed. Clearing it takes 1 action.")
    if gun.arrow and not a.charged:
        raise CombatError("The Arrow Valve fires only after charging.")

    metres = _metres(a, t)
    mod = rules.range_mod(gun.category, metres)
    if mod is None:
        raise CombatError(f"{metres} m is too far for a {rules.CATEGORY_NAMES[gun.category].lower()}.")

    charged = gun.kind == "tech" and a.charged
    walls = rules.walls_between(encounter.wall_set, a.x, a.y, t.x, t.y)
    if walls > (1 if charged else 0):
        raise CombatError("A wall blocks the line of fire." + ("" if gun.kind != "tech" else
                          " A charged TECH shot goes through one wall only."))
    if walls:
        _log(encounter, "The charged shot goes through the wall.")
    cover = 0
    if t.cover == Combatant.Cover.FULL and not charged:
        raise CombatError(f"{t.name} is in full cover: no direct shot.")
    if t.cover == Combatant.Cover.PARTIAL and not charged:
        cover = rules.PARTIAL_COVER_DV
    dv = rules.BASE_DV + mod + cover

    per_die = gun.ammo_per_die
    if per_die and a.ammo < per_die:
        raise CombatError(f"Needs at least {per_die} rounds per die, the magazine has {a.ammo}. Reload.")
    if not per_die and a.ammo < 1:
        raise CombatError("The magazine is empty. Reload.")

    attacks, jam = 1, False
    if overclock:
        if not gun.overclock:
            raise CombatError(f"{gun.name} can't be overclocked.")
        oc_dv = a.overclock_dv or gun.overclock_dv
        mods, _ = _check_mods(a, attack=False)
        roll = rules.roll_check(a.tech, mods, rng)
        if roll.total >= oc_dv:
            attacks = 2
            _log(encounter, f"{a.name}: overclock works, TECH {roll.describe()} ≥ {oc_dv}. Two attacks.")
        else:
            jam = True
            _log(encounter, f"{a.name}: overclock fails, TECH {roll.describe()} < {oc_dv}. "
                            "One attack, then the weapon jams.", Kind.MISS)

    bonus = _hit_bonus(a, t, gun)
    aim_pen = _aim_penalty(a, t, gun, aim)
    luck = _spend_luck(a, luck)
    state_mods, notes = _check_mods(a, attack=True)

    for number in range(attacks):
        if t.is_dead:
            break
        dice = min(gun.dice, a.ammo // per_die) if per_die else gun.dice
        if dice < 1:
            _log(encounter, "Not enough ammo for the second attack.")
            break
        a.ammo -= gun.ammo_for(dice)

        mods = bonus - aim_pen + state_mods + (luck if number == 0 else 0)
        roll = rules.roll_check(a.effective_ref, mods, rng)
        head = f"{a.name} → {t.name}{AIMS[aim]}, {gun.name} ({metres} m, DV {dv})"
        if notes:
            head += f" [{', '.join(notes)}]"

        if roll.total < dv and a.role == Combatant.Role.GUNNER and not a.memory.get("reroll"):
            a.memory["reroll"] = True
            _log(encounter, f"{head}: REF {roll.describe()}, miss. The gunner rerolls.", Kind.MISS)
            roll = rules.roll_check(a.effective_ref, mods, rng)

        if roll.total < dv:
            _log(encounter, f"{head}: REF {roll.describe()}, miss.", Kind.MISS)
            continue

        _log(encounter, f"{head}: REF {roll.describe()}, hit.", Kind.HIT)
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
    """§9: a 3d6 blast with a 2 m radius; SP counts separately and in full."""
    for victim in _in_radius(encounter, a, t.x, t.y, 1, also=t):
        if victim.is_dead:
            continue
        _explosion(encounter, victim, rng, "Arrow Valve blast", dice=3)
        if victim not in (a, t):
            victim.save()


def _melee(encounter, a, t, blade, rng, *, aim, luck, stealth):
    if _metres(a, t) > max(blade.reach_m, rules.SQUARE_M):
        reach = "the next square" if blade.reach_m < 4 else f"{blade.reach_m} m"
        raise CombatError(f"{blade.name} only reaches {reach}.")
    if rules.walls_between(encounter.wall_set, a.x, a.y, t.x, t.y):
        raise CombatError("There is a wall between you.")

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
        _log(encounter, f"{head}: DEX {roll.describe()}, miss.", Kind.MISS)
        return
    _log(encounter, f"{head}: DEX {roll.describe()}, hit.", Kind.HIT)

    if _dodged(encounter, t, roll.total, rng, bullet=False):
        return

    bonus, why = 0, []
    if stealth and a.weapon == "tanto":
        bonus += 2
        why.append("tanto from stealth +2d6")
    if stealth and a.weapon == "cocktail" and not a.memory.get("cocktail"):
        a.memory["cocktail"] = True
        bonus += 2
        why.append("cocktail stick from stealth +2d6")
    if a.weapon == "machete" and t.armor_sp + t.implant_sp == 0:
        bonus += 1
        why.append("machete against no SP +1d6")
    hit_before = a.memory.setdefault("hit", [])
    if t.pk not in hit_before:
        hit_before.append(t.pk)
        if a.role == Combatant.Role.ASSASSIN:
            bonus += 1
            why.append("killer's first strike on this target +1d6")
    if why:
        _log(encounter, "Bonus: " + ", ".join(why) + ".")

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
        _log(encounter, f"{a.name} gets +{extra} m of free movement after the kill.")


# --- grenades -------------------------------------------------------------------------------

DIRECTIONS = [(0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1)]
DIRECTION_NAMES = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"]


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
    """§10: full cover (a wall counts too) between the blast centre and the target."""
    return c.cover == Combatant.Cover.FULL or getattr(c, "behind_wall", False)


def _explosion(encounter, c, rng, label, *, dice, ignore_armor=False, sp_note=""):
    """Blasts: no dodging and no critical injuries (§10). Full cover protects."""
    if _shielded(c):
        _log(encounter, f"{label}: {c.name} is behind cover, not hit.")
        return None
    return _flat_damage(encounter, c, rng, label, dice=dice, ignore_armor=ignore_armor,
                        sp_note=sp_note)


def _flat_damage(encounter, c, rng, label, *, dice, ignore_armor=False, sp_note=""):
    """Damage with no body part and no critical injuries: blasts and quickhacks."""
    rolled = rules.roll_d6(dice, rng)
    sp = c.implant_sp if ignore_armor else c.armor_sp + c.implant_sp
    loss = max(0, sum(rolled) - sp)
    text = f"{label}: {c.name}, {dice}d6{rolled} = {sum(rolled)} − SP {sp}{sp_note} → {loss} HP."
    if loss and not ignore_armor and c.armor_sp:
        c.armor_sp -= 1
        text += f" Armour −1 SP, {c.armor_sp} left."
    was_up = c.hp > 0
    c.hp = max(0, c.hp - loss)
    _log(encounter, text, Kind.HIT if loss else Kind.INFO)
    _knock_down(encounter, c, was_up)
    return loss


@transaction.atomic
def throw_grenade(encounter, a, x, y, kind, rng, *, luck=0):
    _require_turn(encounter, a, action=True)
    if kind not in rules.GRENADES:
        raise CombatError("Unknown grenade.")
    grenade = rules.GRENADES[kind]
    if getattr(a, kind) < 1:
        raise CombatError(f"{a.name} has no {grenade.name.lower()} grenades.")
    if not (0 <= x < encounter.width and 0 <= y < encounter.height):
        raise CombatError("That square is outside the map.")
    metres = rules.distance(a.x, a.y, x, y) * rules.SQUARE_M
    if metres > rules.GRENADE_RANGE_M:
        raise CombatError(f"A grenade flies up to 20 m, this is {metres} m.")
    if (x, y) in encounter.wall_set:
        raise CombatError("That square is a wall. Aim next to it.")

    mods = _spend_luck(a, luck)
    state_mods, _ = _check_mods(a, attack=True)
    roll = rules.roll_check(a.dex, mods + state_mods, rng)
    setattr(a, kind, getattr(a, kind) - 1)
    a.has_acted = True

    if roll.total >= rules.BASE_DV:
        _log(encounter, f"{a.name} throws a {grenade.name.lower()} grenade {metres} m: DEX {roll.describe()}, on target.")
    else:
        d = rng.randint(0, 7)
        x = min(max(x + DIRECTIONS[d][0], 0), encounter.width - 1)
        y = min(max(y + DIRECTIONS[d][1], 0), encounter.height - 1)
        _log(encounter, f"{a.name} throws a {grenade.name.lower()} grenade: DEX {roll.describe()}, off target. "
                        f"It lands ≈3 m to the {DIRECTION_NAMES[d]} (d8: {d + 1}).", Kind.MISS)

    radius = grenade.radius_m // rules.SQUARE_M
    encounter.fx = {"blast": [x, y, radius, kind]}
    victims = _in_radius(encounter, a, x, y, radius)
    if not victims:
        _log(encounter, "Nobody in the blast radius.")

    for c in victims:
        if _shielded(c):
            _log(encounter, f"{grenade.name}: {c.name} is behind cover, not hit.")
        elif kind == "frag":
            _flat_damage(encounter, c, rng, "Frag", dice=4)
        elif kind == "fire":
            _flat_damage(encounter, c, rng, "Incendiary", dice=3)
            _add_status(c, "burning", None, dice=1)
        elif kind == "emp" and not c.cyberized:
            _log(encounter, f"EMP: {c.name} has no implants, no effect.")
        elif kind == "emp":
            _flat_damage(encounter, c, rng, "EMP", dice=2, ignore_armor=True,
                         sp_note=" (regular armour doesn't count)")
            _add_status(c, "implants_off", 1)
        elif kind == "flash":
            _add_status(c, "blind", 1)
            _log(encounter, f"Flashbang: {c.name} is blinded for 1 turn.")
        if c.pk != a.pk:
            c.save()

    a.save()
    if (side := winner(encounter)):
        _log(encounter, f"The fight is over. {WINNER_TEXT[side]}", Kind.DOWN)


# --- netrunning --------------------------------------------------------------------------------

@transaction.atomic
def quickhack(encounter, a, t, hack, rng, *, luck=0):
    _require_turn(encounter, a, action=True)
    if not a.deck:
        raise CombatError(f"{a.name} has no cyberdeck.")
    if not a.implants_on:
        raise CombatError("Implants are off, the deck doesn't work.")
    if hack not in rules.QUICKHACKS:
        raise CombatError("Unknown quickhack.")
    if t.pk == a.pk or t.is_dead:
        raise CombatError("Pick a living target.")

    name, cost = rules.QUICKHACKS[hack]
    discounts = []
    netrunner_discount = a.role == Combatant.Role.NETRUNNER and not a.memory.get("nr_discount")
    raven_discount = a.deck == "raven" and not a.memory.get("raven")
    if netrunner_discount:
        cost -= 1
        discounts.append("netrunner −1")
    if raven_discount:
        cost -= 2
        discounts.append("Raven −2")
    cost = max(1, cost)
    if a.ram < cost:
        raise CombatError(f"{name} costs {cost} RAM, {a.name} has {a.ram}.")

    # §11: RAM is spent on the attempt, even if the hack fails.
    a.ram -= cost
    if netrunner_discount:
        a.memory["nr_discount"] = True
    if raven_discount:
        a.memory["raven"] = True
    a.has_acted = True

    dv = 16 if t.deck else rules.HACK_DV[t.tier]
    mods = _spend_luck(a, luck)
    state_mods, _ = _check_mods(a, attack=False)
    roll = rules.roll_check(a.intel, mods + state_mods, rng)
    price = f"{cost} RAM" + (f", {', '.join(discounts)}" if discounts else "")
    head = f"{a.name}: {name} on {t.name} ({price}), INT {roll.describe()} against DV {dv}"

    if roll.total < dv:
        _log(encounter, head + ", the hack fails.", Kind.MISS)
        a.save()
        return

    _log(encounter, head + ", success.", Kind.HIT)
    if hack == "ping":
        _log(encounter, "Ping: the GM reveals connected devices and targets on the network.")
    elif hack == "glitch":
        _add_status(t, "glitch", 1)
        _log(encounter, f"{t.name} can't use their weapon for 1 turn.")
    elif hack == "optics":
        _add_status(t, "blind", 1)
        _log(encounter, f"{t.name} is blinded for 1 turn.")
    elif hack == "cripple":
        _add_status(t, "immobile", 1)
        _log(encounter, f"{t.name} can't move next turn.")
    else:
        dice = 3 if hack == "short" else 2
        if a.deck == "militech" and not a.memory.get("militech"):
            a.memory["militech"] = True
            dice += 1
            _log(encounter, "Militech: +1d6 to the first attacking quickhack.")
        _flat_damage(encounter, t, rng, name, dice=dice, ignore_armor=True,
                     sp_note=" (regular armour doesn't protect)")
        if hack == "overheat":
            _add_status(t, "overheat", None, dice=2)
            _log(encounter, f"At the start of their next turn {t.name} takes another 2d6.")

    t.save()
    a.save()
    if (side := winner(encounter)):
        _log(encounter, f"The fight is over. {WINNER_TEXT[side]}", Kind.DOWN)


@transaction.atomic
def drain_ram(encounter, a, t):
    """§11: 1 action next to a downed cyberized enemy."""
    _require_turn(encounter, a, action=True)
    if not a.deck:
        raise CombatError(f"{a.name} has no cyberdeck.")
    if _metres(a, t) > rules.SQUARE_M:
        raise CombatError("You need to stand right next to the body.")
    if t.conscious or not t.cyberized:
        raise CombatError("RAM can only be taken from a downed cyberized enemy.")
    if t.memory.get("drained"):
        raise CombatError(f"{t.name}'s RAM has already been taken.")
    gain = 4 if (t.deck and t.tier != Combatant.Tier.REGULAR) else 3
    before = a.ram
    a.ram = min(a.ram_max, a.ram + gain)
    a.has_acted = True
    t.memory["drained"] = True
    a.save()
    t.save()
    _log(encounter, f"{a.name} takes RAM from {t.name}: +{a.ram - before} (now {a.ram}/{a.ram_max}).")


# --- other actions -----------------------------------------------------------------------------

def _gun(c: Combatant):
    weapon = c.get_weapon()
    if weapon.is_melee:
        raise CombatError(f"{weapon.name} is a melee weapon.")
    return weapon


@transaction.atomic
def reload(encounter, c):
    _require_turn(encounter, c, action=True)
    gun = _gun(c)
    if c.ammo >= c.mag_size:
        raise CombatError("The magazine is already full.")
    c.ammo = c.mag_size
    c.has_acted = True
    c.save()
    _log(encounter, f"{c.name} reloads the {gun.name} ({c.ammo}).")


@transaction.atomic
def unjam(encounter, c):
    _require_turn(encounter, c, action=True)
    if not c.jammed:
        raise CombatError("The weapon is not jammed.")
    c.jammed = False
    c.has_acted = True
    c.save()
    _log(encounter, f"{c.name} clears the jam.")


@transaction.atomic
def charge(encounter, c):
    _require_turn(encounter, c, action=True)
    gun = _gun(c)
    if gun.kind != "tech":
        raise CombatError(f"{gun.name} is not a TECH weapon.")
    if c.charged:
        raise CombatError("Already charged; charges don't stack.")
    c.charged = True
    c.has_acted = True
    c.save()
    _log(encounter, f"{c.name} charges the {gun.name}. The shot comes with the next action.")


@transaction.atomic
def lock(encounter, c, t):
    _require_turn(encounter, c, action=True)
    gun = _gun(c)
    if gun.kind != "smart":
        raise CombatError(f"{gun.name} is not a Smart weapon.")
    if t.pk == c.pk or t.is_dead:
        raise CombatError("Pick another target.")
    c.lock_target = t
    c.lock_ready = False
    c.has_acted = True
    c.save()
    _log(encounter, f"{c.name} locks on to {t.name}. From next turn, aimed shots at them "
                    "have no −3 penalty.")


def _near(a: Combatant, t: Combatant) -> None:
    if t.pk != a.pk and _metres(a, t) > rules.SQUARE_M:
        raise CombatError(f"{t.name} is too far away, you need to stand next to them.")


@transaction.atomic
def medkit(encounter, a, t, rng):
    _require_turn(encounter, a, action=True)
    _near(a, t)
    if t.pk == a.pk:
        t = a
    if a.medkits < 1:
        raise CombatError(f"{a.name} has no medkits.")
    if t.is_dead:
        raise CombatError(f"{t.name} is dead.")
    if t.medkits_received >= rules.MEDKIT_LIMIT:
        raise CombatError(f"{t.name} has already had two medkits this fight.")
    if t.hp >= t.hp_max and "bleeding" not in t.statuses:
        raise CombatError(f"{t.name} is not hurt.")

    rolled = rules.roll_d6(2, rng)
    before = t.hp
    t.hp = min(t.hp_max, t.hp + sum(rolled))
    t.medkits_received += 1
    a.medkits -= 1
    a.has_acted = True
    text = f"{a.name} uses a medkit on {t.name}: 2d6{rolled} → {before}→{t.hp} HP."
    if t.statuses.pop("bleeding", None):
        text += " Bleeding stopped."
    if before <= 0 < t.hp:
        t.stabilized = False
        text += f" {t.name} comes round."
    t.save()
    if t is not a:
        a.save()
    _log(encounter, text)


@transaction.atomic
def stabilize(encounter, a, t, rng):
    _require_turn(encounter, a, action=True)
    _near(a, t)
    if t.pk == a.pk or t.hp > 0 or t.is_dead or t.stabilized:
        raise CombatError("You can only stabilise an unconscious ally.")
    mods, _ = _check_mods(a, attack=False)
    roll = rules.roll_check(a.tech, mods, rng)
    a.has_acted = True
    if roll.total >= rules.BASE_DV:
        t.stabilized = True
        _log(encounter, f"{a.name} stabilises {t.name}: TECH {roll.describe()}. "
                        "No more death checks, HP stays at 0.")
    else:
        _log(encounter, f"{a.name} tries to stabilise {t.name}: TECH {roll.describe()}, no luck.",
             Kind.MISS)
    t.save()
    a.save()


@transaction.atomic
def stop_bleeding(encounter, a, t, rng):
    _require_turn(encounter, a, action=True)
    _near(a, t)
    target = a if t.pk == a.pk else t
    if "bleeding" not in target.statuses:
        raise CombatError(f"{target.name} is not bleeding.")
    mods, _ = _check_mods(a, attack=False)
    roll = rules.roll_check(a.tech, mods, rng)
    a.has_acted = True
    if roll.total >= rules.BASE_DV:
        target.statuses.pop("bleeding")
        _log(encounter, f"{a.name} stops {target.name}'s bleeding: TECH {roll.describe()}.")
    else:
        _log(encounter, f"{a.name} can't stop the bleeding: TECH {roll.describe()}.", Kind.MISS)
    if target is not a:
        target.save()
    a.save()


@transaction.atomic
def set_cover(encounter, c, cover):
    """GM tool: cover can change at any time and costs no action."""
    if cover not in Combatant.Cover.values:
        raise CombatError("Unknown cover.")
    c.cover = cover
    c.save(update_fields=["cover"])
    label = dict(Combatant.Cover.choices)[cover].lower()
    _log(encounter, f"{c.name}'s cover: {label}.")


@transaction.atomic
def toggle_walls(encounter: Encounter, cells: list, build: bool) -> None:
    """
    GM tool: add or remove walls. Works during a fight too and costs no
    action. A square where someone stands can't become a wall.
    """
    walls = encounter.wall_set
    taken = {(c.x, c.y) for c in encounter.combatants.filter(is_dead=False)}
    for raw in cells[:400]:
        try:
            x, y = int(raw[0]), int(raw[1])
        except (TypeError, ValueError, IndexError):
            raise CombatError("Invalid square.") from None
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
    Back to setup. §5: after a fight half of the lost SP comes back (rounded
    up). Medkits, grenades and luck don't come back: they are consumables and
    a per-session pool; edit them on the fighter sheet.
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


# --- state for the map --------------------------------------------------------------------------

def _status_list(c: Combatant) -> list[str]:
    names = []
    for key, data in c.statuses.items():
        label = STATUS_NAMES.get(key, key)
        if data.get("left"):
            label += f" ({data['left']} t.)"
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
