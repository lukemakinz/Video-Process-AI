import csv
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Max, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from .forms import (
    ActivityForm,
    OperationForm,
    ProcessForm,
    ProcessVideoUploadForm,
    SegmentCorrectionForm,
)
from .models import Activity, Analysis, AnalysisSegment, Operation, Process, Video
from .services import (
    analysis_summary,
    assist_activity,
    clone_operation,
    get_video_duration_seconds,
    quantize_seconds,
    run_anonymization_in_background,
    run_analysis_in_background,
    segments_needing_review,
    analysis_confidence_unreliable,
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
        messages.success(request, _("Proces został utworzony."))
        return redirect(process)
    return render(
        request,
        "processes/form.html",
        {"form": form, "title": _("Dodaj proces"), "submit_label": _("Zapisz proces")},
    )


def process_detail(request, pk):
    process = get_object_or_404(Process.objects.prefetch_related("operations"), pk=pk)
    return render(request, "processes/process_detail.html", {"process": process})


def process_videos(request, pk):
    """Historia przeanalizowanych nagrań procesu — jeden wiersz na wideo,
    najnowszy status, z paginacją."""
    process = get_object_or_404(Process, pk=pk)
    videos = (
        Video.objects.filter(
            Q(process=process)
            | Q(operations__process=process)
            | Q(operation__process=process)
        )
        .distinct()
        .order_by("-created_at")
    )
    page_obj = Paginator(videos, 20).get_page(request.GET.get("page"))
    for video in page_obj:
        if video.status == Video.Status.COMPLETED:
            analysis = (
                video.analyses.filter(status=Analysis.Status.COMPLETED)
                .order_by("-id")
                .first()
                or video.analyses.order_by("-id").first()
            )
            video.target_url = (
                reverse("analysis_detail", kwargs={"pk": analysis.pk})
                if analysis
                else reverse("video_review", kwargs={"pk": video.pk})
            )
        else:
            video.target_url = reverse("video_review", kwargs={"pk": video.pk})
    return render(
        request,
        "processes/process_videos.html",
        {"process": process, "page_obj": page_obj},
    )


def process_edit(request, pk):
    process = get_object_or_404(Process, pk=pk)
    form = ProcessForm(request.POST or None, instance=process)
    if request.method == "POST" and form.is_valid():
        process = form.save()
        messages.success(request, _("Proces został zaktualizowany."))
        return redirect(process)
    return render(
        request,
        "processes/form.html",
        {"form": form, "title": _("Edytuj proces"), "submit_label": _("Zapisz zmiany")},
    )


def process_delete(request, pk):
    process = get_object_or_404(Process, pk=pk)
    if request.method == "POST":
        process.delete()
        messages.success(request, _("Proces został usunięty."))
        return redirect("process_list")
    return render(
        request,
        "processes/confirm_delete.html",
        {"object": process, "title": _("Usuń proces"), "cancel_url": process.get_absolute_url()},
    )


def operation_import(request, process_id):
    process = get_object_or_404(Process, pk=process_id)
    if request.method == "POST":
        ids = request.POST.getlist("operations")
        sources = Operation.objects.filter(pk__in=ids).prefetch_related("activities")
        count = 0
        for source in sources:
            clone_operation(source, process)
            count += 1
        if count:
            messages.success(
                request,
                _("Zaimportowano %(count)d operacji jako duplikaty.") % {"count": count},
            )
        else:
            messages.warning(request, _("Nie wybrano żadnej operacji."))
        return redirect(process)
    operations = (
        Operation.objects.exclude(process=process)
        .select_related("process")
        .prefetch_related("activities")
        .order_by("process__name", "order")
    )
    return render(
        request,
        "processes/operation_import.html",
        {"process": process, "operations": operations},
    )


def operation_create(request, process_id):
    process = get_object_or_404(Process, pk=process_id)
    next_order = (process.operations.aggregate(max_order=Max("order"))["max_order"] or 0) + 1
    form = OperationForm(request.POST or None, initial={"order": next_order})
    if request.method == "POST" and form.is_valid():
        operation = form.save(commit=False)
        operation.process = process
        operation.save()
        messages.success(request, _("Operacja została dodana."))
        return redirect(operation)
    return render(
        request,
        "processes/form.html",
        {
            "form": form,
            "title": _("Dodaj operację: %(name)s") % {"name": process.name},
            "submit_label": _("Zapisz operację"),
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
        messages.success(request, _("Operacja została zaktualizowana."))
        return redirect(operation)
    return render(
        request,
        "processes/form.html",
        {
            "form": form,
            "title": _("Edytuj operację"),
            "submit_label": _("Zapisz zmiany"),
            "cancel_url": operation.get_absolute_url(),
        },
    )


def operation_delete(request, pk):
    operation = get_object_or_404(Operation.objects.select_related("process"), pk=pk)
    cancel_url = operation.get_absolute_url()
    process_url = operation.process.get_absolute_url()
    if request.method == "POST":
        operation.delete()
        messages.success(request, _("Operacja została usunięta."))
        return redirect(process_url)
    return render(
        request,
        "processes/confirm_delete.html",
        {"object": operation, "title": _("Usuń operację"), "cancel_url": cancel_url},
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
            messages.error(request, _("Wpisz nazwę czynności przed użyciem AI."))
            return render(
                request,
                "processes/activity_form.html",
                {
                    "form": form,
                    "operation": operation,
                    "activity": activity,
                    "suggestion": suggestion,
                    "title": _("Edytuj czynność") if activity else _("Dodaj czynność"),
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
            messages.info(request, _("AI przygotowało propozycję. Zapisz ją dopiero po akceptacji."))
        except Exception as exc:
            messages.error(request, _("Nie udało się wygenerować opisu AI: %(error)s") % {"error": exc})
    elif request.method == "POST" and form.is_valid():
        activity_obj = form.save(commit=False)
        activity_obj.operation = operation
        if activity is None:
            current_max = operation.activities.aggregate(m=Max("order"))["m"] or 0
            activity_obj.order = current_max + 1
        activity_obj.save()
        messages.success(request, _("Czynność została zapisana."))
        return redirect(operation)

    return render(
        request,
        "processes/activity_form.html",
        {
            "form": form,
            "operation": operation,
            "activity": activity,
            "suggestion": suggestion,
            "title": _("Edytuj czynność") if activity else _("Dodaj czynność"),
            "ai_available": ai_available,
            "ai_mock_enabled": settings.OPENAI_USE_MOCK,
        },
    )


def activity_delete(request, pk):
    activity = get_object_or_404(Activity.objects.select_related("operation"), pk=pk)
    operation = activity.operation
    if request.method == "POST":
        activity.delete()
        messages.success(request, _("Czynność została usunięta."))
        return redirect(operation)
    return render(
        request,
        "processes/confirm_delete.html",
        {"object": activity, "title": _("Usuń czynność"), "cancel_url": operation.get_absolute_url()},
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
    if operation_id:
        operation = get_object_or_404(Operation.objects.select_related("process"), pk=operation_id)
        messages.info(request, _("Analizę filmu uruchom z poziomu procesu."))
        return redirect(operation.process)
    messages.info(request, _("Wybierz proces, dla którego chcesz przeanalizować film."))
    return redirect("process_list")


def _valid_video_model_name(value):
    allowed = {choice[0] for choice in settings.GEMINI_VIDEO_MODEL_CHOICES}
    return value if value in allowed else settings.GEMINI_VIDEO_MODEL


def process_video_upload(request, process_id):
    process = get_object_or_404(Process.objects.prefetch_related("operations"), pk=process_id)
    form = ProcessVideoUploadForm(
        request.POST or None, request.FILES or None, process=process
    )
    if request.method == "POST" and form.is_valid():
        video = form.save(commit=False)
        uploaded_file = form.cleaned_data["file"]
        video.original_filename = uploaded_file.name
        video.process = process
        video.status = Video.Status.UPLOADED
        video.analysis_model_name = _valid_video_model_name(
            form.cleaned_data.get("analysis_model_name")
        )
        video.save()
        video.operations.set(form.cleaned_data["operations"])
        try:
            video.duration_seconds = get_video_duration_seconds(video.file.path)
            video.save(update_fields=["duration_seconds"])
        except Exception as exc:
            messages.warning(request, _("Nie udało się odczytać czasu trwania przez FFmpeg: %(error)s") % {"error": exc})
        run_anonymization_in_background(video)
        messages.info(
            request,
            _("Film został wgrany. Anonimizacja twarzy działa w tle, a status odświeży się automatycznie."),
        )
        return redirect("video_review", pk=video.pk)

    return render(
        request,
        "processes/process_video_upload.html",
        {"form": form, "process": process},
    )


def video_review(request, pk):
    video = get_object_or_404(
        Video.objects.select_related("operation", "operation__process"),
        pk=pk,
    )
    _mark_stale_anonymization(video)
    latest_analysis = video.analyses.order_by("-id").first()
    return render(
        request,
        "processes/video_review.html",
        {
            "video": video,
            "latest_analysis": latest_analysis,
            "model_choices": settings.GEMINI_VIDEO_MODEL_CHOICES,
            "selected_model": video.analysis_model_name or settings.GEMINI_VIDEO_MODEL,
        },
    )


def video_reanonymize(request, pk):
    video = get_object_or_404(
        Video.objects.select_related("operation", "operation__process"),
        pk=pk,
    )
    if request.method != "POST":
        messages.info(request, _("Ponowną anonimizację uruchom przyciskiem na stronie podglądu."))
        return redirect("video_review", pk=video.pk)
    if video.status == Video.Status.ANALYZING:
        messages.error(request, _("Nie można ponowić anonimizacji w trakcie analizy."))
        return redirect("video_review", pk=video.pk)
    if not video.file:
        messages.error(request, _("Brakuje oryginalnego pliku wideo."))
        return redirect("video_review", pk=video.pk)

    run_anonymization_in_background(video)
    messages.info(request, _("Ponowna anonimizacja została uruchomiona. Status odświeży się automatycznie."))
    return redirect("video_review", pk=video.pk)


def anonymization_status(request, pk):
    video = get_object_or_404(Video, pk=pk)
    _mark_stale_anonymization(video)
    response = render(request, "processes/_anonymization_status.html", {"video": video})
    if video.status != Video.Status.ANONYMIZING:
        response["HX-Refresh"] = "true"
    return response


def _mark_stale_anonymization(video):
    if video.status != Video.Status.ANONYMIZING:
        return
    last_seen = video.anonymization_progress_updated_at or video.created_at
    if last_seen and timezone.now() - last_seen > timedelta(minutes=5):
        video.status = Video.Status.FAILED
        video.anonymization_error = _(
            "Anonimizacja została przerwana albo worker nie aktualizuje postępu od ponad 5 minut. Uruchom ponownie z oryginału."
        )
        video.anonymization_progress_label = _("Przerwano anonimizację")
        video.save(
            update_fields=[
                "status",
                "anonymization_error",
                "anonymization_progress_label",
            ]
        )


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
        messages.error(request, _("Brakuje pliku po anonimizacji. Analiza została zablokowana."))
        return redirect("video_review", pk=video.pk)
    operations = video.analysis_operations()
    if not operations or not any(op.activities.exists() for op in operations):
        messages.error(request, _("Żadna z wybranych operacji nie ma zdefiniowanych czynności."))
        return redirect("video_review", pk=video.pk)

    model_name = _valid_video_model_name(request.POST.get("analysis_model_name"))
    video.status = Video.Status.ANALYZING
    video.approved_for_analysis_at = timezone.now()
    video.analysis_model_name = model_name
    video.save(update_fields=["status", "approved_for_analysis_at", "analysis_model_name"])
    run_analysis_in_background(video)
    messages.info(
        request,
        _("Analiza została uruchomiona modelem %(model)s. Wynik pojawi się automatycznie.")
        % {"model": model_name},
    )
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
    operations = analysis.video.analysis_operations()
    operation = operations[0] if operations else None
    segments = list(
        analysis.segments.select_related("activity", "activity__operation", "operation")
    )
    segment_forms = []
    for segment in segments:
        segment.resolved_operation = (
            segment.operation
            or (segment.activity.operation if segment.activity_id else None)
            or operation
        )
        segment_forms.append(
            (
                segment,
                SegmentCorrectionForm(
                    instance=segment,
                    operation=segment.resolved_operation,
                ),
            )
        )
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
            "confidence_plateau": analysis_confidence_unreliable(analysis),
            "operation": operation,
            "operations": operations,
            "approved_count": approved_count,
            "model_choices": settings.GEMINI_VIDEO_MODEL_CHOICES,
            "selected_model": analysis.model_name or settings.GEMINI_VIDEO_MODEL,
        },
    )


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
    operations = analysis.video.analysis_operations()
    activity = get_object_or_404(
        Activity, pk=request.POST.get("activity"), operation__in=operations
    )
    segment.activity = activity
    segment.activity_name = activity.name
    segment.operation = activity.operation
    segment.operation_name = activity.operation.name
    segment.save(
        update_fields=["activity", "activity_name", "operation", "operation_name", "updated_at"]
    )
    return render(
        request,
        "processes/_segment_activity_cell.html",
        {"analysis": analysis, "segment": segment, "operation": activity.operation, "saved": True},
    )


@require_POST
def segment_approve(request, analysis_pk, segment_pk):
    analysis = get_object_or_404(
        Analysis.objects.select_related("video", "video__operation"), pk=analysis_pk
    )
    segment = get_object_or_404(
        analysis.segments.select_related("operation", "activity"),
        pk=segment_pk,
    )
    segment.is_approved = True
    segment.save(update_fields=["is_approved", "updated_at"])
    operation = segment.operation or (
        segment.activity.operation if segment.activity_id else analysis.video.operation
    )
    return render(
        request,
        "processes/_segment_feedback.html",
        {"analysis": analysis, "segment": segment, "operation": operation},
    )


@require_POST
def segment_feedback(request, analysis_pk, segment_pk):
    from .models import ActivityHint

    analysis = get_object_or_404(
        Analysis.objects.select_related("video", "video__operation"), pk=analysis_pk
    )
    segment = get_object_or_404(
        analysis.segments.select_related("operation", "activity", "activity__operation"),
        pk=segment_pk,
    )
    operations = analysis.video.analysis_operations()
    operation = segment.operation or (segment.activity.operation if segment.activity_id else None)
    note = (request.POST.get("note") or "").strip()
    selected_activity = None
    confused_id = request.POST.get("confused_with")
    if confused_id and operation is not None:
        selected_activity = operation.activities.filter(pk=confused_id).first()
    target_activity = selected_activity or segment.activity
    if target_activity is None and operation is not None:
        target_activity = operation.activities.first()
    if target_activity is not None and operation is None:
        operation = target_activity.operation
    if operation is None and operations:
        operation = operations[0]
    confused_with = None
    if selected_activity is not None and segment.activity_id and segment.activity_id != selected_activity.pk:
        confused_with = segment.activity
    hint_saved = False
    if note and target_activity is not None:
        ActivityHint.objects.create(
            activity=target_activity,
            text=note,
            confused_with=confused_with,
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
        messages.success(request, _("Segment został zaktualizowany."))
    else:
        messages.error(request, _("Nie udało się zapisać segmentu. Sprawdź wartości czasu."))
    return redirect(reverse("analysis_detail", kwargs={"pk": analysis.pk}) + f"#segment-{segment.pk}")


SEGMENT_MIN_LEN = Decimal("0.20")  # minimalna długość segmentu (s)


@require_POST
def segment_create(request, analysis_pk):
    """Ręczne dodanie aktywności (segmentu) na osi czasu: czynność + zakres czasu."""
    analysis = get_object_or_404(Analysis.objects.select_related("video"), pk=analysis_pk)
    redirect_to = reverse("analysis_detail", kwargs={"pk": analysis.pk})

    operations = analysis.video.analysis_operations()
    allowed_activity_ids = {
        a.pk for op in operations for a in op.activities.all()
    }
    try:
        activity_id = int(request.POST.get("activity"))
        start = quantize_seconds(request.POST.get("start_seconds"))
        end = quantize_seconds(request.POST.get("end_seconds"))
    except (TypeError, ValueError, ArithmeticError):
        messages.error(request, _("Uzupełnij czynność oraz poprawny zakres czasu."))
        return redirect(redirect_to)

    if activity_id not in allowed_activity_ids:
        messages.error(request, _("Wybierz czynność z listy."))
        return redirect(redirect_to)
    activity = Activity.objects.select_related("operation").get(pk=activity_id)

    duration = analysis.video.duration_seconds
    if start < 0 or end <= start or (end - start) < SEGMENT_MIN_LEN:
        messages.error(request, _("Czas zakończenia musi być późniejszy niż początek."))
        return redirect(redirect_to)
    if duration and end > quantize_seconds(duration):
        messages.error(request, _("Zakres wykracza poza długość nagrania."))
        return redirect(redirect_to)

    new_segment = AnalysisSegment.objects.create(
        analysis=analysis,
        activity=activity,
        activity_name=activity.name,
        operation=activity.operation,
        operation_name=activity.operation.name,
        start_seconds=start,
        end_seconds=end,
        confidence=1.0,
        reason="",
        is_approved=True,
    )
    messages.success(request, _("Aktywność dodana."))
    return redirect(redirect_to + f"#segment-{new_segment.pk}")


@require_POST
def segment_delete(request, analysis_pk, segment_pk):
    """Usuwa segment i domyka lukę: rozciąga sąsiada w tej samej operacji
    tak, aby oś pozostała ciągła."""
    analysis = get_object_or_404(Analysis, pk=analysis_pk)
    segment = get_object_or_404(analysis.segments, pk=segment_pk)
    redirect_to = reverse("analysis_detail", kwargs={"pk": analysis.pk})

    same_op = analysis.segments.exclude(pk=segment.pk).filter(operation_name=segment.operation_name)
    prev = same_op.filter(end_seconds=segment.start_seconds).first()
    nxt = same_op.filter(start_seconds=segment.end_seconds).first()
    if prev is not None:
        prev.end_seconds = segment.end_seconds
        prev.is_approved = False
        prev.save(update_fields=["end_seconds", "is_approved", "updated_at"])
    elif nxt is not None:
        nxt.start_seconds = segment.start_seconds
        nxt.is_approved = False
        nxt.save(update_fields=["start_seconds", "is_approved", "updated_at"])

    segment.delete()
    messages.success(request, _("Segment usunięty."))
    return redirect(redirect_to)
