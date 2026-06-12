import csv
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.db.models import Count, Max
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import (
    ActivityForm,
    OperationForm,
    ProcessForm,
    SegmentCorrectionForm,
    VideoUploadForm,
)
from .models import Activity, Analysis, Operation, Process, Video
from .services import (
    analysis_summary,
    anonymize_video,
    assist_activity,
    get_video_duration_seconds,
    run_analysis_in_background,
    segments_needing_review,
)


def process_list(request):
    processes = (
        Process.objects.annotate(operation_total=Count("operations"))
        .prefetch_related("operations")
        .order_by("-created_at")
    )
    return render(request, "processes/process_list.html", {"processes": processes})


def process_create(request):
    form = ProcessForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        process = form.save()
        messages.success(request, "Proces został utworzony.")
        return redirect(process)
    return render(
        request,
        "processes/form.html",
        {"form": form, "title": "Dodaj proces", "submit_label": "Zapisz proces"},
    )


def process_detail(request, pk):
    process = get_object_or_404(Process.objects.prefetch_related("operations"), pk=pk)
    return render(request, "processes/process_detail.html", {"process": process})


def process_edit(request, pk):
    process = get_object_or_404(Process, pk=pk)
    form = ProcessForm(request.POST or None, instance=process)
    if request.method == "POST" and form.is_valid():
        process = form.save()
        messages.success(request, "Proces został zaktualizowany.")
        return redirect(process)
    return render(
        request,
        "processes/form.html",
        {"form": form, "title": "Edytuj proces", "submit_label": "Zapisz zmiany"},
    )


def process_delete(request, pk):
    process = get_object_or_404(Process, pk=pk)
    if request.method == "POST":
        process.delete()
        messages.success(request, "Proces został usunięty.")
        return redirect("process_list")
    return render(
        request,
        "processes/confirm_delete.html",
        {"object": process, "title": "Usuń proces", "cancel_url": process.get_absolute_url()},
    )


def operation_create(request, process_id):
    process = get_object_or_404(Process, pk=process_id)
    next_order = (process.operations.aggregate(max_order=Max("order"))["max_order"] or 0) + 1
    form = OperationForm(request.POST or None, initial={"order": next_order})
    if request.method == "POST" and form.is_valid():
        operation = form.save(commit=False)
        operation.process = process
        operation.save()
        messages.success(request, "Operacja została dodana.")
        return redirect(operation)
    return render(
        request,
        "processes/form.html",
        {
            "form": form,
            "title": f"Dodaj operację: {process.name}",
            "submit_label": "Zapisz operację",
            "cancel_url": process.get_absolute_url(),
        },
    )


def operation_detail(request, pk):
    operation = get_object_or_404(
        Operation.objects.select_related("process").prefetch_related("activities"),
        pk=pk,
    )
    videos = operation.videos.order_by("-created_at")[:5]
    return render(
        request,
        "processes/operation_detail.html",
        {"operation": operation, "videos": videos},
    )


def operation_edit(request, pk):
    operation = get_object_or_404(Operation.objects.select_related("process"), pk=pk)
    form = OperationForm(request.POST or None, instance=operation)
    if request.method == "POST" and form.is_valid():
        operation = form.save()
        messages.success(request, "Operacja została zaktualizowana.")
        return redirect(operation)
    return render(
        request,
        "processes/form.html",
        {
            "form": form,
            "title": "Edytuj operację",
            "submit_label": "Zapisz zmiany",
            "cancel_url": operation.get_absolute_url(),
        },
    )


def operation_delete(request, pk):
    operation = get_object_or_404(Operation.objects.select_related("process"), pk=pk)
    cancel_url = operation.get_absolute_url()
    process_url = operation.process.get_absolute_url()
    if request.method == "POST":
        operation.delete()
        messages.success(request, "Operacja została usunięta.")
        return redirect(process_url)
    return render(
        request,
        "processes/confirm_delete.html",
        {"object": operation, "title": "Usuń operację", "cancel_url": cancel_url},
    )


@require_POST
def operation_move(request, pk, direction):
    operation = get_object_or_404(Operation.objects.select_related("process"), pk=pk)
    siblings = list(operation.process.operations.all())
    index = siblings.index(operation)
    target_index = index - 1 if direction == "up" else index + 1
    if 0 <= target_index < len(siblings):
        other = siblings[target_index]
        operation.order, other.order = other.order, operation.order
        operation.save(update_fields=["order", "updated_at"])
        other.save(update_fields=["order", "updated_at"])
    return redirect(operation.process)


def activity_create(request, operation_id):
    operation = get_object_or_404(Operation.objects.select_related("process"), pk=operation_id)
    return _activity_form(request, operation=operation)


def activity_edit(request, pk):
    activity = get_object_or_404(Activity.objects.select_related("operation", "operation__process"), pk=pk)
    return _activity_form(request, operation=activity.operation, activity=activity)


@require_POST
def activity_ai_field(request, operation_id):
    operation = get_object_or_404(Operation.objects.select_related("process"), pk=operation_id)
    target = request.POST.get("target")
    if target not in {"description", "recognition_rules", "exclusion_rules"}:
        return HttpResponse("", status=400)
    if not (request.POST.get("name") or "").strip():
        return HttpResponse("Najpierw wpisz nazwę czynności.", status=400)
    fields = {
        "name": request.POST.get("name", "").strip(),
        "quick_description": request.POST.get("quick_description", ""),
        "description": request.POST.get("description", ""),
        "recognition_rules": request.POST.get("recognition_rules", ""),
        "exclusion_rules": request.POST.get("exclusion_rules", ""),
    }
    mode = "refine" if fields.get(target) else "generate"
    try:
        result = assist_activity(operation, fields, mode=mode, target=target)
        return HttpResponse(result.get(target, ""))
    except Exception as exc:
        return HttpResponse(f"Nie udało się wygenerować pola: {exc}", status=400)


def _activity_form(request, operation, activity=None):
    form = ActivityForm(request.POST or None, instance=activity)
    suggestion = None
    ai_available = bool(settings.OPENAI_API_KEY) or settings.OPENAI_USE_MOCK

    if request.method == "POST" and request.POST.get("action") in {"ai_suggest", "ai_refine"}:
        mode = "refine" if request.POST.get("action") == "ai_refine" else "generate"
        name = (request.POST.get("name") or "").strip()
        if not name:
            form.add_error("name", "Najpierw wpisz nazwę czynności, żeby AI wiedziało, co opisać.")
            messages.error(request, "Wpisz nazwę czynności przed użyciem AI.")
            return render(
                request,
                "processes/activity_form.html",
                {
                    "form": form,
                    "operation": operation,
                    "activity": activity,
                    "suggestion": suggestion,
                    "title": "Edytuj czynność" if activity else "Dodaj czynność",
                    "ai_available": ai_available,
                    "ai_mock_enabled": settings.OPENAI_USE_MOCK,
                },
            )
        try:
            suggestion = assist_activity(
                operation=operation,
                fields={
                    "name": name,
                    "quick_description": request.POST.get("quick_description", ""),
                    "description": request.POST.get("description", ""),
                    "recognition_rules": request.POST.get("recognition_rules", ""),
                    "exclusion_rules": request.POST.get("exclusion_rules", ""),
                },
                mode=mode,
            )
            data = request.POST.copy()
            for field_name in ("description", "recognition_rules", "exclusion_rules"):
                if suggestion.get(field_name):
                    data[field_name] = suggestion[field_name]
            form = ActivityForm(data, instance=activity)
            messages.info(request, "AI przygotowało propozycję. Zapisz ją dopiero po akceptacji.")
        except Exception as exc:
            messages.error(request, f"Nie udało się wygenerować opisu AI: {exc}")
    elif request.method == "POST" and form.is_valid():
        activity_obj = form.save(commit=False)
        activity_obj.operation = operation
        if activity is None:
            current_max = operation.activities.aggregate(m=Max("order"))["m"] or 0
            activity_obj.order = current_max + 1
        activity_obj.save()
        messages.success(request, "Czynność została zapisana.")
        return redirect(operation)

    return render(
        request,
        "processes/activity_form.html",
        {
            "form": form,
            "operation": operation,
            "activity": activity,
            "suggestion": suggestion,
            "title": "Edytuj czynność" if activity else "Dodaj czynność",
            "ai_available": ai_available,
            "ai_mock_enabled": settings.OPENAI_USE_MOCK,
        },
    )


def activity_delete(request, pk):
    activity = get_object_or_404(Activity.objects.select_related("operation"), pk=pk)
    operation = activity.operation
    if request.method == "POST":
        activity.delete()
        messages.success(request, "Czynność została usunięta.")
        return redirect(operation)
    return render(
        request,
        "processes/confirm_delete.html",
        {"object": activity, "title": "Usuń czynność", "cancel_url": operation.get_absolute_url()},
    )


@require_POST
def activity_move(request, pk, direction):
    activity = get_object_or_404(Activity.objects.select_related("operation"), pk=pk)
    siblings = list(activity.operation.activities.all())
    index = siblings.index(activity)
    target_index = index - 1 if direction == "up" else index + 1
    if 0 <= target_index < len(siblings):
        siblings[index], siblings[target_index] = siblings[target_index], siblings[index]
    for position, item in enumerate(siblings, start=1):
        if item.order != position:
            item.order = position
            item.save(update_fields=["order", "updated_at"])
    return redirect(activity.operation)


@require_POST
def hint_toggle(request, pk):
    from .models import ActivityHint

    hint = get_object_or_404(ActivityHint, pk=pk)
    hint.is_active = not hint.is_active
    hint.save(update_fields=["is_active", "updated_at"])
    return render(request, "processes/_hint_row.html", {"hint": hint})


@require_POST
def hint_delete(request, pk):
    from .models import ActivityHint

    hint = get_object_or_404(ActivityHint, pk=pk)
    hint.delete()
    return HttpResponse("")


def video_upload(request, operation_id=None):
    operation = None
    if operation_id:
        operation = get_object_or_404(Operation.objects.select_related("process"), pk=operation_id)

    form = VideoUploadForm(request.POST or None, request.FILES or None, operation=operation)
    if request.method == "POST" and form.is_valid():
        video = form.save(commit=False)
        uploaded_file = form.cleaned_data["file"]
        video.original_filename = uploaded_file.name
        video.status = Video.Status.UPLOADED
        video.save()
        try:
            video.duration_seconds = get_video_duration_seconds(video.file.path)
            video.save(update_fields=["duration_seconds"])
        except Exception as exc:
            messages.warning(request, f"Nie udało się odczytać czasu trwania przez FFmpeg: {exc}")
        try:
            anonymize_video(video)
            messages.success(
                request,
                "Film został zanonimizowany. Sprawdź podgląd i zatwierdź analizę.",
            )
        except Exception as exc:
            messages.error(request, f"Anonimizacja nie powiodła się: {exc}")
        return redirect("video_review", pk=video.pk)

    return render(
        request,
        "processes/video_upload.html",
        {"form": form, "operation": operation},
    )


def video_review(request, pk):
    video = get_object_or_404(
        Video.objects.select_related("operation", "operation__process"),
        pk=pk,
    )
    latest_analysis = video.analyses.order_by("-id").first()
    return render(
        request,
        "processes/video_review.html",
        {"video": video, "latest_analysis": latest_analysis},
    )


@require_POST
def video_reanonymize(request, pk):
    video = get_object_or_404(
        Video.objects.select_related("operation", "operation__process"),
        pk=pk,
    )
    if video.status == Video.Status.ANALYZING:
        messages.error(request, "Nie można ponowić anonimizacji w trakcie analizy.")
        return redirect("video_review", pk=video.pk)
    if not video.file:
        messages.error(request, "Brakuje oryginalnego pliku wideo.")
        return redirect("video_review", pk=video.pk)

    try:
        anonymize_video(video)
        messages.success(request, "Anonimizacja została ponowiona z oryginalnego pliku.")
    except Exception as exc:
        messages.error(request, f"Ponowna anonimizacja nie powiodła się: {exc}")
    return redirect("video_review", pk=video.pk)


def analysis_status(request, pk):
    video = get_object_or_404(Video, pk=pk)
    analysis = video.analyses.order_by("-id").first()
    return render(
        request,
        "processes/_analysis_status.html",
        {"video": video, "analysis": analysis},
    )


@require_POST
def video_approve_and_analyze(request, pk):
    video = get_object_or_404(
        Video.objects.select_related("operation", "operation__process"),
        pk=pk,
    )
    if not video.anonymized_file:
        messages.error(request, "Brakuje pliku po anonimizacji. Analiza została zablokowana.")
        return redirect("video_review", pk=video.pk)
    if not video.operation.activities.exists():
        messages.error(request, "Najpierw zdefiniuj czynności dla tej operacji.")
        return redirect(video.operation)

    video.status = Video.Status.ANALYZING
    video.approved_for_analysis_at = timezone.now()
    video.save(update_fields=["status", "approved_for_analysis_at"])
    run_analysis_in_background(video)
    messages.info(request, "Analiza została uruchomiona. Wynik pojawi się automatycznie.")
    return redirect("video_review", pk=video.pk)


def analysis_detail(request, pk):
    analysis = get_object_or_404(
        Analysis.objects.select_related(
            "video",
            "video__operation",
            "video__operation__process",
        ).prefetch_related("segments", "video__operation__activities"),
        pk=pk,
    )
    operation = analysis.video.operation
    segment_forms = [
        (segment, SegmentCorrectionForm(instance=segment, operation=operation))
        for segment in analysis.segments.select_related("activity")
    ]
    cost = None
    if analysis.estimated_cost is not None:
        rate = Decimal(str(settings.GEMINI_USD_PLN_RATE))
        cost = {
            "usd": analysis.estimated_cost,
            "pln": (analysis.estimated_cost * rate).quantize(Decimal("0.01")),
            "input_tokens": analysis.input_tokens,
            "output_tokens": analysis.output_tokens,
            "is_estimated": analysis.cost_is_estimated,
            "rate": rate,
        }
    needs_review = segments_needing_review(analysis)
    approved_count = analysis.segments.filter(is_approved=True).count()
    return render(
        request,
        "processes/analysis_detail.html",
        {
            "analysis": analysis,
            "segment_forms": segment_forms,
            "summary": analysis_summary(analysis),
            "cost": cost,
            "needs_review": needs_review,
            "operation": operation,
            "approved_count": approved_count,
        },
    )


@require_POST
def analysis_approve_all(request, pk):
    analysis = get_object_or_404(Analysis, pk=pk)
    updated = analysis.segments.filter(is_approved=False).update(
        is_approved=True,
        updated_at=timezone.now(),
    )
    if updated:
        messages.success(request, f"Zatwierdzono {updated} segment(y).")
    else:
        messages.info(request, "Wszystkie segmenty były już zatwierdzone.")
    return redirect("analysis_detail", pk=analysis.pk)


def analysis_export_csv(request, pk):
    analysis = get_object_or_404(
        Analysis.objects.select_related("video", "video__operation"),
        pk=pk,
    )
    filename = f"analysis-{analysis.pk}-segments.csv"
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("\ufeff")

    writer = csv.writer(response)
    writer.writerow(
        [
            "start_seconds",
            "end_seconds",
            "duration_seconds",
            "activity",
            "confidence",
            "is_approved",
            "reason",
        ]
    )
    for segment in analysis.segments.order_by("start_seconds", "id"):
        writer.writerow(
            [
                segment.start_seconds,
                segment.end_seconds,
                segment.duration_seconds,
                segment.activity_name,
                segment.confidence,
                "tak" if segment.is_approved else "nie",
                segment.reason,
            ]
        )
    return response


@require_POST
def segment_reassign(request, analysis_pk, segment_pk):
    analysis = get_object_or_404(
        Analysis.objects.select_related("video", "video__operation"),
        pk=analysis_pk,
    )
    segment = get_object_or_404(analysis.segments, pk=segment_pk)
    operation = analysis.video.operation
    activity = get_object_or_404(operation.activities, pk=request.POST.get("activity"))
    segment.activity = activity
    segment.activity_name = activity.name
    segment.save(update_fields=["activity", "activity_name", "updated_at"])
    return render(
        request,
        "processes/_segment_activity_cell.html",
        {"analysis": analysis, "segment": segment, "operation": operation, "saved": True},
    )


@require_POST
def segment_approve(request, analysis_pk, segment_pk):
    analysis = get_object_or_404(
        Analysis.objects.select_related("video", "video__operation"), pk=analysis_pk
    )
    segment = get_object_or_404(analysis.segments, pk=segment_pk)
    segment.is_approved = True
    segment.save(update_fields=["is_approved", "updated_at"])
    return render(
        request,
        "processes/_segment_feedback.html",
        {"analysis": analysis, "segment": segment, "operation": analysis.video.operation},
    )


@require_POST
def segment_feedback(request, analysis_pk, segment_pk):
    from .models import ActivityHint

    analysis = get_object_or_404(
        Analysis.objects.select_related("video", "video__operation"), pk=analysis_pk
    )
    segment = get_object_or_404(analysis.segments, pk=segment_pk)
    operation = analysis.video.operation
    note = (request.POST.get("note") or "").strip()
    confused_with = None
    confused_id = request.POST.get("confused_with")
    if confused_id:
        confused_with = operation.activities.filter(pk=confused_id).first()
    target_activity = segment.activity or confused_with
    hint_saved = False
    if note and target_activity is not None:
        ActivityHint.objects.create(
            activity=target_activity,
            text=note,
            confused_with=confused_with if confused_with != target_activity else None,
            source_segment=segment,
        )
        hint_saved = True
    return render(
        request,
        "processes/_segment_feedback.html",
        {
            "analysis": analysis,
            "segment": segment,
            "operation": operation,
            "hint_saved": hint_saved,
        },
    )


@require_POST
def segment_update(request, analysis_pk, segment_pk):
    analysis = get_object_or_404(
        Analysis.objects.select_related("video", "video__operation"),
        pk=analysis_pk,
    )
    segment = get_object_or_404(analysis.segments, pk=segment_pk)
    form = SegmentCorrectionForm(
        request.POST,
        instance=segment,
        operation=analysis.video.operation,
    )
    if form.is_valid():
        form.save()
        messages.success(request, "Segment został zaktualizowany.")
    else:
        messages.error(request, "Nie udało się zapisać segmentu. Sprawdź wartości czasu.")
    return redirect(reverse("analysis_detail", kwargs={"pk": analysis.pk}) + f"#segment-{segment.pk}")
