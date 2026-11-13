"""
python manage.py seed_demo

Готовая сцена: трое из команды против четырёх бандитов на складе.
Каждый запуск создаёт новую копию.
"""

from django.core.management.base import BaseCommand

from combat.models import Combatant, Encounter

CREW = [
    dict(name="Вэкс", role="gunner", gunner_category="assault",
         intel=4, ref=8, dex=6, tech=5, cool=5, body=5, luck=2,
         weapon="ajax", subdermal=True, optics=True, medkits=2, frag=1, x=1, y=3),
    dict(name="Кира", role="assassin",
         intel=4, ref=7, dex=8, tech=3, cool=5, body=6, luck=2,
         weapon="katana", reflexes=True, medkits=1, flash=1, x=2, y=6),
    dict(name="Шёпот", role="netrunner", deck="raven",
         intel=8, ref=5, dex=5, tech=6, cool=6, body=3, luck=2,
         weapon="kappa", medkits=1, x=0, y=5),
]

HOSTILES = [
    dict(name="Бритва", tier="elite", ref=6, dex=6, body=7, cool=5, hp_override=45,
         armor_sp=6, weapon="carnage", x=12, y=3, cover="partial"),
    dict(name="Шпана", ref=5, dex=5, body=4, cool=4, hp_override=20,
         weapon="saratoga", x=13, y=6),
    dict(name="Кирпич", ref=4, dex=5, body=8, cool=4, hp_override=30, armor_sp=4,
         weapon="sledge", x=10, y=7),
    dict(name="Глаз", ref=6, dex=5, body=4, cool=5, hp_override=30, armor_sp=4,
         weapon="nekomata", x=15, y=1, cover="partial"),
]


class Command(BaseCommand):
    help = "Создать демо-сцену с семью бойцами."

    def handle(self, *args, **options):
        # Ящики и колонны: часть линий огня открыта, часть перекрыта.
        walls = [[7, 1], [7, 2], [4, 4], [10, 4], [11, 4], [5, 8], [6, 8],
                 [9, 8], [9, 9], [12, 0], [13, 0]]
        encounter = Encounter.objects.create(name="Склад в Уотсоне", width=16, height=10, walls=walls)
        for side, people in ((Combatant.Side.CREW, CREW), (Combatant.Side.HOSTILE, HOSTILES)):
            for data in people:
                fighter = Combatant(encounter=encounter, side=side, **data)
                fighter.armor_sp_max = fighter.armor_sp
                fighter.luck_left = fighter.luck
                fighter.refill()
                fighter.save()

        self.stdout.write(self.style.SUCCESS(
            f"Сцена «{encounter.name}» (id {encounter.pk}) готова."
        ))
