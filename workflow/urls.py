from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("cycles/new/", views.create_cycle, name="create_cycle"),
    path("brand-brief/", views.brand_brief, name="brand_brief"),
    path("cycles/<int:pk>/", views.cycle_detail, name="cycle_detail"),
    path("cycles/<int:pk>/items/", views.add_items, name="add_items"),
    path("cycles/<int:pk>/items/<int:item_pk>/delete/", views.delete_item, name="delete_item"),
    path("cycles/<int:pk>/stage-1/run/", views.run_stage1, name="run_stage1"),
    path("cycles/<int:pk>/stage-1/decide/", views.decide_brief, name="decide_brief"),
    path("cycles/<int:pk>/stage-2/run/", views.run_stage2, name="run_stage2"),
    path("cycles/<int:pk>/stage-2/select/", views.select_concepts, name="select_concepts"),
]
