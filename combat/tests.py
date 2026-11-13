"""
Проверки правил на подставных кубиках. Где можно, числа взяты из примеров
книги (§4, §5, §16).
"""

import json
import random

from django.test import TestCase
from django.urls import reverse

from . import rules, services
from .models import Combatant, Encounter, LogEntry


class Dice(random.Random):
    """Отдаёт заранее заданные числа по порядку."""

    def __init__(self, *values: int):
        super().__init__()
        self.values = list(values)

    def randint(self, a: int, b: int) -> int:
        value = self.values.pop(0)
        assert a <= value <= b, f"{value} не может выпасть на {a}..{b}"
        return value


def make(encounter, name, side="crew", **kw):
    c = Combatant(encounter=encounter, name=name, side=side, **kw)
    c.armor_sp_max = c.armor_sp
    c.luck_left = c.luck
    c.refill()
    c.save()
    return c


class RulesTests(TestCase):
    def test_natural_ten_adds_and_one_subtracts(self):
        self.assertEqual(rules.roll_check(5, 0, Dice(10, 4)).total, 19)
        self.assertEqual(rules.roll_check(5, 0, Dice(1, 6)).total, 0)

    def test_max_hp_example(self):
        self.assertEqual(rules.max_hp(5, 5), 35)            # §5: BODY5 COOL5 → 35

    def test_range_table(self):
        self.assertEqual(rules.range_mod("assault", 20), 0)  # §4: автомат на 20 м
        self.assertEqual(rules.range_mod("sniper", 4), 8)
        self.assertIsNone(rules.range_mod("shotgun", 120))

    def test_melee_sp(self):
        self.assertEqual(rules.melee_sp(5, 2, 0), 3)        # половина вверх
        self.assertEqual(rules.melee_sp(5, 4, 0), 2)        # моно-катана
        self.assertEqual(rules.melee_sp(5, 2, 2), 1)        # кувалда

    def test_ammo_per_die(self):
        self.assertEqual(rules.GUNS["ajax"].ammo_for(4), 8)
        self.assertEqual(rules.GUNS["pozhar"].ammo_for(4), 1)
        self.assertEqual(rules.GUNS["unity"].ammo_for(3), 1)

    def test_critical_needs_penetration(self):
        self.assertTrue(rules.is_critical([6, 6, 1], 1))
        self.assertFalse(rules.is_critical([6, 6, 1], 0))


class FightTests(TestCase):
    def setUp(self):
        self.e = Encounter.objects.create(name="Тест", width=20, height=10)
        # Стрелок REF 8 с Ajax, как в примере §16.
        self.shooter = make(self.e, "Вэкс", ref=8, dex=5, weapon="ajax", x=0, y=0)
        self.enemy = make(self.e, "Враг", "hostile", ref=4, dex=4, hp_override=30,
                          armor_sp=4, weapon="unity", x=10, y=0, cover="partial")
        services.start(self.e, Dice())       # REF 8 и 4 — без d6
        self.reload_all()

    def reload_all(self):
        for obj in (self.e, self.shooter, self.enemy):
            obj.refresh_from_db()

    def test_initiative_by_ref(self):
        self.assertEqual(self.e.active_combatant(), self.shooter)

    def test_example_from_section_16(self):
        # 20 м, частичное укрытие: DV 14. d10=7 → 15. Урон 6,6,3,2 = 17 − 4 = 13.
        # Две шестёрки и пробитие → крит. d6=5 → кровотечение.
        services.attack(self.e, self.shooter, self.enemy, Dice(7, 6, 6, 3, 2, 5))
        self.reload_all()
        self.assertEqual(self.enemy.hp, 17)
        self.assertEqual(self.enemy.armor_sp, 3)
        self.assertIn("bleeding", self.enemy.statuses)
        self.assertEqual(self.shooter.ammo, 32 - 8)

    def test_roll_equal_to_dv_hits(self):
        services.attack(self.e, self.shooter, self.enemy, Dice(6, 1, 1, 1, 1))  # 8+6 = 14
        self.reload_all()
        self.assertEqual(self.enemy.hp, 30)          # 4 урона не пробили SP 4
        self.assertEqual(self.enemy.armor_sp, 4)
        self.assertTrue(LogEntry.objects.filter(text__contains="попадание").exists())

    def test_headshot_doubles_after_sp(self):
        # 8 − 3 + 9 = 14 ≥ 14. Урон 5,5,4,2 = 16 − 4 = 12, ×2 = 24.
        services.attack(self.e, self.shooter, self.enemy, Dice(9, 5, 5, 4, 2), aim="head")
        self.reload_all()
        self.assertEqual(self.enemy.hp, 6)

    def test_one_action_per_turn(self):
        services.attack(self.e, self.shooter, self.enemy, Dice(1, 1))
        with self.assertRaises(services.CombatError):
            services.reload(self.e, self.shooter)

    def test_full_cover_blocks_shot(self):
        self.enemy.cover = "full"
        self.enemy.save()
        with self.assertRaises(services.CombatError):
            services.attack(self.e, self.shooter, self.enemy, Dice())

    def test_movement_is_8_metres(self):
        services.move(self.e, self.shooter, 2, 0)        # 4 м
        services.move(self.e, self.shooter, 4, 0)        # ещё 4 м
        with self.assertRaises(services.CombatError):
            services.move(self.e, self.shooter, 5, 0)

    def test_zero_hp_and_death_check(self):
        second = make(self.e, "Второй", "hostile", ref=2, x=15, y=5)
        second.initiative = 20                      # чтобы бой не кончился сразу
        second.save()
        self.enemy.hp = 0
        self.enemy.save()
        services.end_turn(self.e, self.shooter, Dice(3))   # BODY 5 + 3 = 8 < 12
        self.enemy.refresh_from_db()
        self.assertTrue(self.enemy.is_dead)

    def test_medkit_limit(self):
        ally = make(self.e, "Док", ref=2, x=0, y=1)
        ally.hp = 5
        ally.medkits_received = 2
        ally.save()
        with self.assertRaises(services.CombatError):
            services.medkit(self.e, self.shooter, ally, Dice(3, 3))


class DodgeAndMeleeTests(TestCase):
    def setUp(self):
        self.e = Encounter.objects.create(name="Ближний", width=10, height=10)
        self.a = make(self.e, "Кира", ref=9, dex=8, weapon="katana", x=0, y=0)
        self.t = make(self.e, "Громила", "hostile", ref=3, dex=5, armor_sp=5,
                      hp_override=30, weapon="knife", x=1, y=0)
        services.start(self.e, Dice())
        self.a.refresh_from_db()
        self.t.refresh_from_db()

    def test_melee_uses_half_sp_and_anyone_can_dodge(self):
        # 8 + 1 (катана) + 5 = 14; уклонение 5 + 4 = 9 — мимо. Урон 4,4,4,4 = 16 − 3 = 13.
        services.attack(self.e, self.a, self.t, Dice(5, 4, 4, 4, 4, 4))
        self.t.refresh_from_db()
        self.assertEqual(self.t.hp, 17)
        self.assertTrue(self.t.dodge_used)

    def test_successful_dodge(self):
        services.attack(self.e, self.a, self.t, Dice(3, 9))   # 12 против 5 + 9 = 14
        self.t.refresh_from_db()
        self.assertEqual(self.t.hp, 30)


class GrenadeTests(TestCase):
    def test_grenade_hits_everyone_in_radius_without_crits(self):
        e = Encounter.objects.create(name="Граната", width=12, height=12)
        a = make(e, "Метатель", ref=8, dex=6, frag=1, x=0, y=0)
        t1 = make(e, "Один", "hostile", ref=3, hp_override=30, x=5, y=5)
        t2 = make(e, "Два", "hostile", ref=2, hp_override=30, x=6, y=5)
        services.start(e, Dice())
        a.refresh_from_db()
        # Бросок 6 + 6 = 12. Потом по отдельному набору 4d6 на каждого.
        services.throw_grenade(e, a, 5, 5, "frag", Dice(6, 6, 6, 1, 1, 6, 6, 6, 6))
        t1.refresh_from_db()
        t2.refresh_from_db()
        self.assertEqual(t1.hp, 30 - (14 - 3))
        self.assertEqual(t2.hp, 30 - (24 - 3))
        self.assertEqual(t2.statuses, {})               # две шестёрки, но крита нет


class WallTests(TestCase):
    def setUp(self):
        self.e = Encounter.objects.create(name="Стены", width=12, height=8,
                                          walls=[[3, 0], [3, 1], [3, 2], [3, 3], [3, 4]])
        self.a = make(self.e, "Стрелок", ref=8, dex=6, weapon="unity", frag=1, x=1, y=2)
        self.t = make(self.e, "Цель", "hostile", ref=2, hp_override=30, x=5, y=2)
        services.start(self.e, Dice())
        self.a.refresh_from_db()

    def test_line_cells(self):
        self.assertEqual(rules.line_cells(0, 0, 3, 0), [(1, 0), (2, 0)])
        self.assertEqual(rules.walls_between({(3, 2)}, 1, 2, 5, 2), 1)

    def test_wall_blocks_shot(self):
        with self.assertRaises(services.CombatError):
            services.attack(self.e, self.a, self.t, Dice())

    def test_move_goes_around_or_fails(self):
        # Через стену напрямую 2 клетки, в обход по ряду 5 — 4+ шагов: не хватает 8 м.
        with self.assertRaises(services.CombatError):
            services.move(self.e, self.a, 4, 2)
        with self.assertRaises(services.CombatError):
            services.move(self.e, self.a, 3, 2)          # в саму стену

    def test_wall_shields_from_blast(self):
        # Граната падает рядом со стрелком, цель за стеной от центра взрыва.
        self.t.x, self.t.y = 4, 2
        self.t.save()
        services.throw_grenade(self.e, self.a, 2, 2, "frag", Dice(6, 6, 6, 6, 6))
        self.t.refresh_from_db()
        self.assertEqual(self.t.hp, 30)

    def test_toggle_walls_skips_occupied(self):
        services.toggle_walls(self.e, [[1, 2], [6, 6]], True)
        self.assertNotIn([1, 2], self.e.walls)
        self.assertIn([6, 6], self.e.walls)
        services.toggle_walls(self.e, [[6, 6]], False)
        self.assertNotIn([6, 6], self.e.walls)


class ApiTests(TestCase):
    def setUp(self):
        self.e = Encounter.objects.create(name="Api", width=10, height=10)
        make(self.e, "A", x=0, y=0)
        make(self.e, "B", "hostile", x=5, y=5)
        self.url = reverse("combat:action", args=[self.e.pk])

    def post(self, data):
        return self.client.post(self.url, json.dumps(data), content_type="application/json")

    def test_start_returns_state(self):
        response = self.post({"type": "start"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["started"])

    def test_errors_are_json(self):
        response = self.post({"type": "move", "actor": 999, "x": 1, "y": 1})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_pages_render(self):
        self.assertContains(self.client.get(reverse("combat:list")), "Api")
        self.assertContains(self.client.get(self.e.get_absolute_url()), "initial-state")
        self.assertEqual(self.client.get(reverse("combat:combatant_add", args=[self.e.pk])).status_code, 200)
