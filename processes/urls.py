from django.urls import path

from . import views

urlpatterns = [
    path("", views.process_list, name="process_list"),
    path("processes/new/", views.process_create, name="process_create"),
    path("processes/<int:pk>/", views.process_detail, name="process_detail"),
    path("processes/<int:pk>/edit/", views.process_edit, name="process_edit"),
    path("processes/<int:pk>/delete/", views.process_delete, name="process_delete"),
    path(
        "processes/<int:process_id>/operations/new/",
        views.operation_create,
        name="operation_create",
    ),
    path("operations/<int:pk>/", views.operation_detail, name="operation_detail"),
    path("operations/<int:pk>/edit/", views.operation_edit, name="operation_edit"),
    path("operations/<int:pk>/delete/", views.operation_delete, name="operation_delete"),
    path(
        "operations/<int:pk>/move/<str:direction>/",
        views.operation_move,
        name="operation_move",
    ),
    path(
        "operations/<int:operation_id>/activities/new/",
        views.activity_create,
        name="activity_create",
    ),
    path("activities/<int:pk>/edit/", views.activity_edit, name="activity_edit"),
    path("activities/<int:pk>/delete/", views.activity_delete, name="activity_delete"),
    path("videos/upload/", views.video_upload, name="video_upload"),
    path(
        "operations/<int:operation_id>/videos/upload/",
        views.video_upload,
        name="operation_video_upload",
    ),
    path("videos/<int:pk>/review/", views.video_review, name="video_review"),
    path(
        "videos/<int:pk>/approve-and-analyze/",
        views.video_approve_and_analyze,
        name="video_approve_and_analyze",
    ),
    path("analyses/<int:pk>/", views.analysis_detail, name="analysis_detail"),
    path(
        "analyses/<int:analysis_pk>/segments/<int:segment_pk>/",
        views.segment_update,
        name="segment_update",
    ),
]
