from django.urls import path

from . import views

app_name = "combat"

urlpatterns = [
    path("", views.encounter_list, name="list"),
    path("encounters/<int:pk>/", views.encounter_detail, name="encounter"),
    path("encounters/<int:pk>/delete/", views.encounter_delete, name="encounter_delete"),
    path("encounters/<int:pk>/action/", views.action, name="action"),
    path("encounters/<int:pk>/fighters/new/", views.combatant_form, name="combatant_add"),
    path("encounters/<int:pk>/fighters/<int:combatant_pk>/", views.combatant_form, name="combatant_edit"),
    path("encounters/<int:pk>/fighters/<int:combatant_pk>/delete/", views.combatant_delete,
         name="combatant_delete"),
]
