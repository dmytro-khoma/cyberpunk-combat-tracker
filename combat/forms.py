from django import forms

from .models import Combatant, Encounter

STATS = ["intel", "ref", "dex", "tech", "cool", "body", "luck"]
IMPLANTS = ["subdermal", "skull", "bioplastic", "optics", "reflexes", "adrenaline", "cyberized"]


class EncounterForm(forms.ModelForm):
    class Meta:
        model = Encounter
        fields = ["name", "width", "height"]
        help_texts = {
            "width": "Клеток, 6–30. Клетка — 2 м.",
            "height": "Клеток, 6–30.",
        }


class CombatantForm(forms.ModelForm):
    class Meta:
        model = Combatant
        fields = [
            "name", "side", "tier", "role", "gunner_category",
            *STATS,
            *IMPLANTS, "deck",
            "weapon", "magazine", "overclock_dv", "armor_sp", "hp_override",
            "medkits", "frag", "emp", "flash", "fire",
            "cover", "x", "y",
        ]
        help_texts = {
            "magazine": "0 — значение по умолчанию для модели. Книга оставляет магазины ДМ.",
            "overclock_dv": "Пусто — по умолчанию для модели (у Pozhar 15).",
            "armor_sp": "Базовый SP — 3.",
        }

    def __init__(self, *args, encounter: Encounter, **kwargs):
        super().__init__(*args, **kwargs)
        self.encounter = encounter

    def stat_fields(self):
        return [self[name] for name in STATS]

    def implant_fields(self):
        return [self[name] for name in IMPLANTS]

    def clean(self):
        data = super().clean()

        if data.get("role") == Combatant.Role.GUNNER and not data.get("gunner_category"):
            self.add_error("gunner_category", "Стрелок выбирает категорию оружия.")

        x, y = data.get("x"), data.get("y")
        if x is None or y is None:
            return data
        if x >= self.encounter.width or y >= self.encounter.height:
            raise forms.ValidationError(
                f"Карта {self.encounter.width} × {self.encounter.height}: столбцы 0–"
                f"{self.encounter.width - 1}, ряды 0–{self.encounter.height - 1}."
            )
        if (x, y) in self.encounter.wall_set:
            raise forms.ValidationError("Эта клетка — стена. Выберите другую или уберите стену на карте.")
        taken = self.encounter.combatants.filter(x=x, y=y, is_dead=False)
        if self.instance.pk:
            taken = taken.exclude(pk=self.instance.pk)
        if taken.exists():
            raise forms.ValidationError("На этой клетке уже кто-то стоит.")
        return data
