from django.urls import path

from trips import views

urlpatterns = [
    path("", views.map_page, name="map"),
    path("api/routes/", views.plan_route, name="plan-route"),
    path("api/health/", views.health, name="health"),
]
