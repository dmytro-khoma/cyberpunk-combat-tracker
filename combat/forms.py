from django import forms

from .models import Combatant, Encounter

STATS = ["intel", "ref", "dex", "tech", "cool", "body", "luck"]
IMPLANTS = ["subdermal", "skull", "bioplastic", "optics", "reflexes", "adrenaline", "cyberized"]


class EncounterForm(forms.ModelForm):
    class Meta:
        model = Encounter
        fields = ["name", "width", "height"]
        help_texts = {
            "width": "Squares, 6–30. One square is 2 m.",
            "height": "Squares, 6–30.",
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
            "magazine": "0 uses the model's default. The rulebook leaves magazines to the GM.",
            "overclock_dv": "Leave empty for the model's default (Pozhar: 15).",
            "armor_sp": "Base SP is 3.",
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
            self.add_error("gunner_category", "A gunner has to pick a weapon category.")

        x, y = data.get("x"), data.get("y")
        if x is None or y is None:
            return data
        if x >= self.encounter.width or y >= self.encounter.height:
            raise forms.ValidationError(
                f"The map is {self.encounter.width} × {self.encounter.height}: columns 0–"
                f"{self.encounter.width - 1}, rows 0–{self.encounter.height - 1}."
            )
        if (x, y) in self.encounter.wall_set:
            raise forms.ValidationError("That square is a wall. Pick another one or remove the wall on the map.")
        taken = self.encounter.combatants.filter(x=x, y=y, is_dead=False)
        if self.instance.pk:
            taken = taken.exclude(pk=self.instance.pk)
        if taken.exists():
            raise forms.ValidationError("Someone is already standing on that square.")
        return data
