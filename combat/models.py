from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse

from . import rules


def stat_field(default: int, verbose: str) -> models.PositiveSmallIntegerField:
    # §1: на создании 2–8, после бонусов максимум 10.
    return models.PositiveSmallIntegerField(
        verbose, default=default, validators=[MinValueValidator(2), MaxValueValidator(10)]
    )


def count_field(verbose: str, default: int = 0, high: int = 20) -> models.PositiveSmallIntegerField:
    return models.PositiveSmallIntegerField(
        verbose, default=default, validators=[MaxValueValidator(high)]
    )


class Encounter(models.Model):
    name = models.CharField("Название", max_length=80)
    width = models.PositiveSmallIntegerField(
        "Ширина", default=16, validators=[MinValueValidator(6), MaxValueValidator(30)]
    )
    height = models.PositiveSmallIntegerField(
        "Высота", default=10, validators=[MinValueValidator(6), MaxValueValidator(30)]
    )
    walls = models.JSONField(default=list, blank=True)   # [[x, y], ...]
    round = models.PositiveIntegerField(default=0)       # 0 — бой не начат
    turn_index = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return reverse("combat:encounter", args=[self.pk])

    @property
    def started(self) -> bool:
        return self.round > 0

    @property
    def wall_set(self) -> set[tuple[int, int]]:
        return {(x, y) for x, y in self.walls}

    def turn_order(self) -> list["Combatant"]:
        return list(self.combatants.order_by("-initiative", "pk"))

    def active_combatant(self) -> "Combatant | None":
        if not self.started:
            return None
        order = self.turn_order()
        return order[self.turn_index] if self.turn_index < len(order) else None


class Combatant(models.Model):
    class Side(models.TextChoices):
        CREW = "crew", "Команда"
        HOSTILE = "hostile", "Противники"

    class Tier(models.TextChoices):
        REGULAR = "regular", "Обычный"
        ELITE = "elite", "Элита"
        BOSS = "boss", "Босс"

    class Role(models.TextChoices):
        NONE = "", "Без класса"
        GUNNER = "gunner", "Стрелок"
        ASSASSIN = "assassin", "Хладнокровный убийца"
        NETRUNNER = "netrunner", "Нетраннер"

    class Cover(models.TextChoices):
        NONE = "", "Нет"
        PARTIAL = "partial", "Частичное"
        FULL = "full", "Полное"

    encounter = models.ForeignKey(Encounter, on_delete=models.CASCADE, related_name="combatants")
    name = models.CharField("Имя", max_length=40)
    side = models.CharField("Сторона", max_length=10, choices=Side.choices, default=Side.CREW)
    tier = models.CharField("Уровень", max_length=10, choices=Tier.choices, default=Tier.REGULAR)
    role = models.CharField("Класс", max_length=10, choices=Role.choices, blank=True, default="")
    gunner_category = models.CharField(
        "Категория стрелка", max_length=10, choices=rules.CATEGORY_CHOICES, blank=True, default="",
        help_text="Только для Стрелка: +2 к попаданию из этой категории.",
    )

    # §1. Характеристики
    intel = stat_field(5, "INT")
    ref = stat_field(5, "REF")
    dex = stat_field(5, "DEX")
    tech = stat_field(5, "TECH")
    cool = stat_field(5, "COOL")
    body = stat_field(5, "BODY")
    luck = stat_field(5, "LUCK")

    # §15. Пассивные импланты, которые трекер считает сам
    subdermal = models.BooleanField("Подкожная броня (+2 SP)", default=False)
    skull = models.BooleanField("Бронированный череп (+3 SP головы)", default=False)
    bioplastic = models.BooleanField("Биопластиковые сосуды (+10 HP)", default=False)
    optics = models.BooleanField("Улучшенная оптика (прицел −1)", default=False)
    reflexes = models.BooleanField("Усиленные рефлексы (+1 REF)", default=False)
    adrenaline = models.BooleanField("Адреналиновый ускоритель", default=False)
    cyberized = models.BooleanField("Кибернизирован", default=True,
                                    help_text="Нужно для ЭМИ и сбора RAM с тела.")
    deck = models.CharField("Кибердека", max_length=10, choices=rules.DECK_CHOICES,
                            blank=True, default="")

    # Снаряжение
    weapon = models.CharField("Оружие", max_length=20, choices=rules.WEAPON_CHOICES, default="unity")
    magazine = count_field("Магазин", 0, 99)
    overclock_dv = models.PositiveSmallIntegerField("DV разгона", null=True, blank=True,
                                                    validators=[MaxValueValidator(30)])
    armor_sp = count_field("SP брони", rules.ARMOR_BASE_SP, 20)
    armor_sp_max = models.PositiveSmallIntegerField(default=rules.ARMOR_BASE_SP)
    hp_override = models.PositiveSmallIntegerField(
        "Макс. HP вручную", null=True, blank=True, validators=[MaxValueValidator(300)],
        help_text="Пусто — по формуле. Шаблоны врагов: слабый 20, обычный 30, элита 45.",
    )
    medkits = count_field("Аптечки", 2)
    frag = count_field("Осколочные", 0)
    emp = count_field("ЭМИ", 0)
    flash = count_field("Светошумовые", 0)
    fire = count_field("Зажигательные", 0)
    cover = models.CharField("Укрытие", max_length=10, choices=Cover.choices, blank=True, default="")

    # Состояние в бою
    hp_max = models.PositiveSmallIntegerField(default=0)
    hp = models.SmallIntegerField(default=0)
    is_dead = models.BooleanField(default=False)
    stabilized = models.BooleanField(default=False)
    ammo = models.PositiveSmallIntegerField(default=0)
    jammed = models.BooleanField(default=False)
    charged = models.BooleanField(default=False)
    lock_target = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    lock_ready = models.BooleanField(default=False)
    ram = models.PositiveSmallIntegerField(default=0)
    luck_left = models.PositiveSmallIntegerField(default=0)
    medkits_received = models.PositiveSmallIntegerField(default=0)
    dodge_used = models.BooleanField(default=False)
    statuses = models.JSONField(default=dict, blank=True)
    memory = models.JSONField(default=dict, blank=True)   # разовые эффекты за бой/ход

    x = models.PositiveSmallIntegerField("Столбец", default=0)
    y = models.PositiveSmallIntegerField("Ряд", default=0)
    initiative = models.SmallIntegerField(null=True, blank=True)
    init_roll = models.PositiveSmallIntegerField(null=True, blank=True)
    move_left = models.PositiveSmallIntegerField(default=0)    # метры
    moved_m = models.PositiveSmallIntegerField(default=0)
    has_acted = models.BooleanField(default=False)

    class Meta:
        ordering = ["pk"]

    def __str__(self) -> str:
        return self.name

    def refill(self) -> None:
        """Полные HP, магазин и RAM. Вызывается при создании и правке."""
        self.hp_max = self.hp_override or (
            rules.max_hp(self.body, self.cool) + (10 if self.bioplastic else 0)
        )
        self.hp = self.hp_max
        self.ammo = self.mag_size
        self.ram = self.ram_max

    # --- производные значения -------------------------------------------

    def get_weapon(self):
        return rules.WEAPONS[self.weapon]

    @property
    def mag_size(self) -> int:
        weapon = self.get_weapon()
        if weapon.is_melee:
            return 0
        return self.magazine or weapon.magazine

    @property
    def effective_ref(self) -> int:
        return min(10, self.ref + (1 if self.reflexes else 0))

    @property
    def ram_max(self) -> int:
        ram = rules.DECKS[self.deck][1]
        if ram and self.role == self.Role.NETRUNNER:
            ram += 2
        return ram

    @property
    def implants_on(self) -> bool:
        return "implants_off" not in self.statuses

    @property
    def implant_sp(self) -> int:
        return 2 if self.subdermal and self.implants_on else 0

    @property
    def head_sp(self) -> int:
        return 3 if self.skull and self.implants_on else 0

    @property
    def is_boss(self) -> bool:
        return self.tier == self.Tier.BOSS

    @property
    def conscious(self) -> bool:
        return not self.is_dead and self.hp > 0

    @property
    def condition(self) -> str:
        if self.is_dead:
            return "dead"
        if self.hp <= 0:
            return "stable" if self.stabilized else "down"
        return "ok"

    @property
    def speed_m(self) -> int:
        slow = "leg" in self.statuses or "slowed" in self.statuses
        return rules.MOVE_M // 2 if slow else rules.MOVE_M


class LogEntry(models.Model):
    class Kind(models.TextChoices):
        INFO = "info", "Инфо"
        HIT = "hit", "Попадание"
        MISS = "miss", "Промах"
        DOWN = "down", "Выбыл"

    encounter = models.ForeignKey(Encounter, on_delete=models.CASCADE, related_name="log")
    round = models.PositiveIntegerField(default=0)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.INFO)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-pk"]

    def __str__(self) -> str:
        return self.text[:60]
