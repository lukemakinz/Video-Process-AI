from django.contrib import admin

from .models import (
    Activity,
    ActivityHint,
    Analysis,
    AnalysisSegment,
    Operation,
    Process,
    Video,
)


class OperationInline(admin.TabularInline):
    model = Operation
    extra = 0
    fields = ("order", "name")


class ActivityInline(admin.TabularInline):
    model = Activity
    extra = 0
    fields = ("name", "performed_by", "minimum_duration_seconds")


class AnalysisSegmentInline(admin.TabularInline):
    model = AnalysisSegment
    extra = 0
    fields = (
        "start_seconds",
        "end_seconds",
        "activity",
        "activity_name",
        "confidence",
        "is_approved",
    )


@admin.register(Process)
class ProcessAdmin(admin.ModelAdmin):
    list_display = ("name", "created_at", "updated_at")
    search_fields = ("name", "description")
    inlines = [OperationInline]


@admin.register(Operation)
class OperationAdmin(admin.ModelAdmin):
    list_display = ("name", "process", "order", "created_at")
    list_filter = ("process",)
    search_fields = ("name", "description", "process__name")
    inlines = [ActivityInline]


@admin.register(Activity)
class ActivityAdmin(admin.ModelAdmin):
    list_display = ("name", "operation", "order", "performed_by", "minimum_duration_seconds")
    list_filter = ("performed_by", "operation__process")
    search_fields = ("name", "description", "recognition_rules", "exclusion_rules")


@admin.register(Video)
class VideoAdmin(admin.ModelAdmin):
    list_display = ("original_filename", "operation", "duration_seconds", "status", "created_at")
    list_filter = ("status", "operation__process")
    search_fields = ("original_filename", "operation__name")


@admin.register(Analysis)
class AnalysisAdmin(admin.ModelAdmin):
    list_display = ("id", "video", "status", "model_name", "started_at", "completed_at")
    list_filter = ("status", "model_name")
    readonly_fields = ("prompt", "raw_response")
    inlines = [AnalysisSegmentInline]


@admin.register(AnalysisSegment)
class AnalysisSegmentAdmin(admin.ModelAdmin):
    list_display = (
        "analysis",
        "activity_name",
        "start_seconds",
        "end_seconds",
        "confidence",
        "is_approved",
    )
    list_filter = ("is_approved", "activity__performed_by")


@admin.register(ActivityHint)
class ActivityHintAdmin(admin.ModelAdmin):
    list_display = ("activity", "text", "confused_with", "is_active", "created_at")
    list_filter = ("is_active", "activity__operation__process")
    search_fields = ("text", "activity__name")
