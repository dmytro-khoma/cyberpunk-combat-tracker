from django.contrib import admin

from .models import Combatant, Encounter, LogEntry


class CombatantInline(admin.TabularInline):
    model = Combatant
    extra = 0
    fields = ["name", "side", "tier", "hp", "hp_max", "armor_sp", "weapon", "ammo", "x", "y", "is_dead"]


@admin.register(Encounter)
class EncounterAdmin(admin.ModelAdmin):
    list_display = ["name", "round", "created_at"]
    inlines = [CombatantInline]


@admin.register(LogEntry)
class LogEntryAdmin(admin.ModelAdmin):
    list_display = ["encounter", "round", "kind", "text"]
    list_filter = ["kind"]
