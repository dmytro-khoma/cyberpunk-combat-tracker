from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse

from . import rules


def stat_field(default: int, verbose: str) -> models.PositiveSmallIntegerField:
    # §1: 2–8 at creation, 10 at most after bonuses.
    return models.PositiveSmallIntegerField(
        verbose, default=default, validators=[MinValueValidator(2), MaxValueValidator(10)]
    )


def count_field(verbose: str, default: int = 0, high: int = 20) -> models.PositiveSmallIntegerField:
    return models.PositiveSmallIntegerField(
        verbose, default=default, validators=[MaxValueValidator(high)]
    )


class Encounter(models.Model):
    name = models.CharField("Name", max_length=80)
    width = models.PositiveSmallIntegerField(
        "Width", default=16, validators=[MinValueValidator(6), MaxValueValidator(30)]
    )
    height = models.PositiveSmallIntegerField(
        "Height", default=10, validators=[MinValueValidator(6), MaxValueValidator(30)]
    )
    walls = models.JSONField(default=list, blank=True)   # [[x, y], ...]
    round = models.PositiveIntegerField(default=0)       # 0 = the fight has not started
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
        CREW = "crew", "Crew"
        HOSTILE = "hostile", "Hostiles"

    class Tier(models.TextChoices):
        REGULAR = "regular", "Regular"
        ELITE = "elite", "Elite"
        BOSS = "boss", "Boss"

    class Role(models.TextChoices):
        NONE = "", "No class"
        GUNNER = "gunner", "Gunner"
        ASSASSIN = "assassin", "Cold-blooded killer"
        NETRUNNER = "netrunner", "Netrunner"

    class Cover(models.TextChoices):
        NONE = "", "None"
        PARTIAL = "partial", "Partial"
        FULL = "full", "Full"

    encounter = models.ForeignKey(Encounter, on_delete=models.CASCADE, related_name="combatants")
    name = models.CharField("Name", max_length=40)
    side = models.CharField("Side", max_length=10, choices=Side.choices, default=Side.CREW)
    tier = models.CharField("Tier", max_length=10, choices=Tier.choices, default=Tier.REGULAR)
    role = models.CharField("Class", max_length=10, choices=Role.choices, blank=True, default="")
    gunner_category = models.CharField(
        "Gunner category", max_length=10, choices=rules.CATEGORY_CHOICES, blank=True, default="",
        help_text="Gunner only: +2 to hit with this category.",
    )

    # §1. Stats
    intel = stat_field(5, "INT")
    ref = stat_field(5, "REF")
    dex = stat_field(5, "DEX")
    tech = stat_field(5, "TECH")
    cool = stat_field(5, "COOL")
    body = stat_field(5, "BODY")
    luck = stat_field(5, "LUCK")

    # §15. Passive implants the tracker applies by itself
    subdermal = models.BooleanField("Subdermal armour (+2 SP)", default=False)
    skull = models.BooleanField("Armoured skull (+3 head SP)", default=False)
    bioplastic = models.BooleanField("Bioplastic blood vessels (+10 HP)", default=False)
    optics = models.BooleanField("Enhanced optics (aim −1)", default=False)
    reflexes = models.BooleanField("Boosted reflexes (+1 REF)", default=False)
    adrenaline = models.BooleanField("Adrenaline booster", default=False)
    cyberized = models.BooleanField("Cyberized", default=True,
                                    help_text="Needed for EMP and for taking RAM from a body.")
    deck = models.CharField("Cyberdeck", max_length=10, choices=rules.DECK_CHOICES,
                            blank=True, default="")

    # Gear
    weapon = models.CharField("Weapon", max_length=20, choices=rules.WEAPON_CHOICES, default="unity")
    magazine = count_field("Magazine", 0, 99)
    overclock_dv = models.PositiveSmallIntegerField("Overclock DV", null=True, blank=True,
                                                    validators=[MaxValueValidator(30)])
    armor_sp = count_field("Armour SP", rules.ARMOR_BASE_SP, 20)
    armor_sp_max = models.PositiveSmallIntegerField(default=rules.ARMOR_BASE_SP)
    hp_override = models.PositiveSmallIntegerField(
        "Max HP (manual)", null=True, blank=True, validators=[MaxValueValidator(300)],
        help_text="Leave empty to use the formula. Enemy templates: weak 20, regular 30, elite 45.",
    )
    medkits = count_field("Medkits", 2)
    frag = count_field("Frag grenades", 0)
    emp = count_field("EMP grenades", 0)
    flash = count_field("Flashbangs", 0)
    fire = count_field("Incendiary grenades", 0)
    cover = models.CharField("Cover", max_length=10, choices=Cover.choices, blank=True, default="")

    # Combat state
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
    memory = models.JSONField(default=dict, blank=True)   # one-off effects per fight / turn

    x = models.PositiveSmallIntegerField("Column", default=0)
    y = models.PositiveSmallIntegerField("Row", default=0)
    initiative = models.SmallIntegerField(null=True, blank=True)
    init_roll = models.PositiveSmallIntegerField(null=True, blank=True)
    move_left = models.PositiveSmallIntegerField(default=0)    # metres
    moved_m = models.PositiveSmallIntegerField(default=0)
    has_acted = models.BooleanField(default=False)

    class Meta:
        ordering = ["pk"]

    def __str__(self) -> str:
        return self.name

    def refill(self) -> None:
        """Full HP, magazine and RAM. Called on create and on edit."""
        self.hp_max = self.hp_override or (
            rules.max_hp(self.body, self.cool) + (10 if self.bioplastic else 0)
        )
        self.hp = self.hp_max
        self.ammo = self.mag_size
        self.ram = self.ram_max

    # --- derived values -----------------------------------------------------

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
        INFO = "info", "Info"
        HIT = "hit", "Hit"
        MISS = "miss", "Miss"
        DOWN = "down", "Down"

    encounter = models.ForeignKey(Encounter, on_delete=models.CASCADE, related_name="log")
    round = models.PositiveIntegerField(default=0)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.INFO)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-pk"]

    def __str__(self) -> str:
        return self.text[:60]
