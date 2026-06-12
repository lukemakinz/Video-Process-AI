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
    path(
        "operations/<int:operation_id>/activities/ai-field/",
        views.activity_ai_field,
        name="activity_ai_field",
    ),
    path("activities/<int:pk>/edit/", views.activity_edit, name="activity_edit"),
    path("activities/<int:pk>/delete/", views.activity_delete, name="activity_delete"),
    path(
        "activities/<int:pk>/move/<str:direction>/",
        views.activity_move,
        name="activity_move",
    ),
    path("videos/upload/", views.video_upload, name="video_upload"),
    path(
        "operations/<int:operation_id>/videos/upload/",
        views.video_upload,
        name="operation_video_upload",
    ),
    path("videos/<int:pk>/review/", views.video_review, name="video_review"),
    path(
        "videos/<int:pk>/reanonymize/",
        views.video_reanonymize,
        name="video_reanonymize",
    ),
    path(
        "videos/<int:pk>/analysis-status/",
        views.analysis_status,
        name="analysis_status",
    ),
    path(
        "videos/<int:pk>/approve-and-analyze/",
        views.video_approve_and_analyze,
        name="video_approve_and_analyze",
    ),
    path("analyses/<int:pk>/", views.analysis_detail, name="analysis_detail"),
    path(
        "analyses/<int:pk>/approve-all/",
        views.analysis_approve_all,
        name="analysis_approve_all",
    ),
    path(
        "analyses/<int:pk>/export.csv",
        views.analysis_export_csv,
        name="analysis_export_csv",
    ),
    path(
        "analyses/<int:analysis_pk>/segments/<int:segment_pk>/",
        views.segment_update,
        name="segment_update",
    ),
    path(
        "analyses/<int:analysis_pk>/segments/<int:segment_pk>/reassign/",
        views.segment_reassign,
        name="segment_reassign",
    ),
    path(
        "analyses/<int:analysis_pk>/segments/<int:segment_pk>/approve/",
        views.segment_approve,
        name="segment_approve",
    ),
    path(
        "analyses/<int:analysis_pk>/segments/<int:segment_pk>/feedback/",
        views.segment_feedback,
        name="segment_feedback",
    ),
    path("hints/<int:pk>/toggle/", views.hint_toggle, name="hint_toggle"),
    path("hints/<int:pk>/delete/", views.hint_delete, name="hint_delete"),
]
