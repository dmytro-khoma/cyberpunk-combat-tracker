import json
import random

from django.contrib import messages
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import services
from .forms import CombatantForm, EncounterForm
from .models import Combatant, Encounter

# Настоящие кубики. Тесты передают в services свой генератор.
DICE = random.SystemRandom()


@require_http_methods(["GET", "POST"])
def encounter_list(request: HttpRequest) -> HttpResponse:
    form = EncounterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        encounter = form.save()
        messages.success(request, "Сцена создана. Добавьте бойцов.")
        return redirect("combat:combatant_add", pk=encounter.pk)

    return render(request, "combat/encounter_list.html", {
        "encounters": Encounter.objects.all(),
        "form": form,
    })


@require_GET
def encounter_detail(request: HttpRequest, pk: int) -> HttpResponse:
    encounter = get_object_or_404(Encounter, pk=pk)
    return render(request, "combat/encounter_detail.html", {
        "encounter": encounter,
        "state": services.state(encounter),
    })


@require_POST
def encounter_delete(request: HttpRequest, pk: int) -> HttpResponse:
    encounter = get_object_or_404(Encounter, pk=pk)
    encounter.delete()
    messages.success(request, f"Сцена «{encounter.name}» удалена.")
    return redirect("combat:list")


@require_http_methods(["GET", "POST"])
def combatant_form(request: HttpRequest, pk: int, combatant_pk: int | None = None) -> HttpResponse:
    encounter = get_object_or_404(Encounter, pk=pk)
    combatant = (
        get_object_or_404(Combatant, pk=combatant_pk, encounter=encounter) if combatant_pk else None
    )
    if encounter.started:
        messages.error(request, "Бойцов меняют до начала боя. Сначала сбросьте бой.")
        return redirect(encounter)

    form = CombatantForm(request.POST or None, instance=combatant, encounter=encounter)
    if request.method == "POST" and form.is_valid():
        fighter = form.save(commit=False)
        fighter.encounter = encounter
        fighter.armor_sp_max = fighter.armor_sp
        fighter.luck_left = fighter.luck
        fighter.refill()
        fighter.save()
        messages.success(request, f"{fighter.name} сохранён.")
        if "add_another" in request.POST:
            return redirect("combat:combatant_add", pk=encounter.pk)
        return redirect(encounter)

    return render(request, "combat/combatant_form.html", {
        "encounter": encounter,
        "form": form,
        "combatant": combatant,
    })


@require_POST
def combatant_delete(request: HttpRequest, pk: int, combatant_pk: int) -> HttpResponse:
    encounter = get_object_or_404(Encounter, pk=pk)
    combatant = get_object_or_404(Combatant, pk=combatant_pk, encounter=encounter)
    if encounter.started:
        messages.error(request, "Во время боя бойцов не убирают. Сначала сбросьте бой.")
    else:
        combatant.delete()
        messages.success(request, f"{combatant.name} убран из сцены.")
    return redirect(encounter)


@require_POST
def action(request: HttpRequest, pk: int) -> JsonResponse:
    """
    JSON для карты. Тело: {"type": ..., "actor": id, "target": id, "x", "y", ...}.
    Ответ — новое состояние сцены или {"error": "..."} с кодом 400.
    """
    encounter = get_object_or_404(Encounter, pk=pk)

    try:
        data = json.loads(request.body or b"{}")
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        return JsonResponse({"error": "Ожидался JSON-объект."}, status=400)

    def fighter(key: str) -> Combatant:
        try:
            return encounter.combatants.get(pk=int(data.get(key)))
        except (TypeError, ValueError, Combatant.DoesNotExist):
            raise services.CombatError("Нет такого бойца.") from None

    def number(key: str, default: int | None = None) -> int:
        try:
            return int(data.get(key, default))
        except (TypeError, ValueError):
            raise services.CombatError("Неверное число в запросе.") from None

    kind = data.get("type")
    try:
        if kind == "start":
            services.start(encounter, DICE)
        elif kind == "reset":
            services.reset(encounter)
        elif kind == "end_turn":
            services.end_turn(encounter, fighter("actor"), DICE)
        elif kind == "move":
            services.move(encounter, fighter("actor"), number("x"), number("y"))
        elif kind == "attack":
            services.attack(
                encounter, fighter("actor"), fighter("target"), DICE,
                aim=str(data.get("aim") or "body"), luck=number("luck", 0),
                overclock=bool(data.get("overclock")), stealth=bool(data.get("stealth")),
            )
        elif kind == "grenade":
            services.throw_grenade(encounter, fighter("actor"), number("x"), number("y"),
                                   str(data.get("grenade")), DICE, luck=number("luck", 0))
        elif kind == "hack":
            services.quickhack(encounter, fighter("actor"), fighter("target"),
                               str(data.get("hack")), DICE, luck=number("luck", 0))
        elif kind == "drain":
            services.drain_ram(encounter, fighter("actor"), fighter("target"))
        elif kind == "reload":
            services.reload(encounter, fighter("actor"))
        elif kind == "unjam":
            services.unjam(encounter, fighter("actor"))
        elif kind == "charge":
            services.charge(encounter, fighter("actor"))
        elif kind == "lock":
            services.lock(encounter, fighter("actor"), fighter("target"))
        elif kind == "medkit":
            services.medkit(encounter, fighter("actor"), fighter("target"), DICE)
        elif kind == "stabilize":
            services.stabilize(encounter, fighter("actor"), fighter("target"), DICE)
        elif kind == "stop_bleeding":
            services.stop_bleeding(encounter, fighter("actor"), fighter("target"), DICE)
        elif kind == "walls":
            cells = data.get("cells")
            if not isinstance(cells, list):
                raise services.CombatError("Ожидался список клеток.")
            services.toggle_walls(encounter, cells, bool(data.get("build")))
        elif kind == "clear_walls":
            services.clear_walls(encounter)
        elif kind == "cover":
            services.set_cover(encounter, fighter("target"), str(data.get("cover") or ""))
        else:
            return JsonResponse({"error": "Неизвестное действие."}, status=400)
    except services.CombatError as error:
        return JsonResponse({"error": str(error)}, status=400)

    encounter.refresh_from_db()
    return JsonResponse(services.state(encounter))   # fx (место взрыва) переживает refresh
