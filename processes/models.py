from decimal import Decimal

from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField("utworzono", auto_now_add=True)
    updated_at = models.DateTimeField("zaktualizowano", auto_now=True)

    class Meta:
        abstract = True


class Process(TimeStampedModel):
    name = models.CharField("nazwa procesu", max_length=200)
    description = models.TextField("opis procesu", blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "proces"
        verbose_name_plural = "procesy"

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("process_detail", kwargs={"pk": self.pk})


class Operation(TimeStampedModel):
    process = models.ForeignKey(
        Process,
        verbose_name="proces",
        related_name="operations",
        on_delete=models.CASCADE,
    )
    name = models.CharField(_("nazwa operacji"), max_length=200)
    description = models.TextField("opis operacji", blank=True)
    order = models.PositiveIntegerField("kolejność", default=1)

    class Meta:
        ordering = ["order", "created_at"]
        verbose_name = "operacja"
        verbose_name_plural = "operacje"

    def __str__(self):
        return f"{self.process.name} / {self.name}"

    def get_absolute_url(self):
        return reverse("operation_detail", kwargs={"pk": self.pk})


class Activity(TimeStampedModel):
    class Performer(models.TextChoices):
        OPERATOR = "operator", _("Operator")
        MACHINE = "machine", _("Maszyna")
        BOTH = "both", _("Operator i maszyna")
        UNKNOWN = "unknown", _("Nieokreślone")

    operation = models.ForeignKey(
        Operation,
        verbose_name=_("operacja"),
        related_name="activities",
        on_delete=models.CASCADE,
    )
    name = models.CharField(_("nazwa czynności"), max_length=200)
    description = models.TextField("opis tego, co powinno być widoczne", blank=True)
    recognition_rules = models.TextField("warunki rozpoznania", blank=True)
    exclusion_rules = models.TextField("warunki wykluczenia", blank=True)
    minimum_duration_seconds = models.DecimalField(
        "minimalny czas trwania",
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
    )
    performed_by = models.CharField(
        "wykonawca",
        max_length=20,
        choices=Performer.choices,
        default=Performer.OPERATOR,
    )
    order = models.PositiveIntegerField("kolejność", default=1)

    class Meta:
        ordering = ["order", "name"]
        verbose_name = "czynność"
        verbose_name_plural = "czynności"

    def __str__(self):
        return self.name


class Video(models.Model):
    class Status(models.TextChoices):
        UPLOADED = "uploaded", _("Wgrano")
        ANONYMIZING = "anonymizing", _("Anonimizacja")
        AWAITING_APPROVAL = "awaiting_approval", _("Do zatwierdzenia")
        APPROVED = "approved", _("Zatwierdzono")
        ANALYZING = "analyzing", _("Analiza")
        COMPLETED = "completed", _("Zakończono")
        FAILED = "failed", _("Błąd")

    process = models.ForeignKey(
        "Process",
        verbose_name="proces",
        related_name="videos",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
    operation = models.ForeignKey(
        Operation,
        verbose_name="operacja (pojedyncza, zgodność wsteczna)",
        related_name="videos",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
    operations = models.ManyToManyField(
        Operation,
        verbose_name="operacje do analizy",
        related_name="analysis_videos",
        blank=True,
    )
    file = models.FileField("plik wideo", upload_to="videos/%Y/%m/%d/")
    anonymized_file = models.FileField(
        "plik po anonimizacji",
        upload_to="anonymized/%Y/%m/%d/",
        blank=True,
    )
    original_filename = models.CharField("oryginalna nazwa pliku", max_length=255)
    duration_seconds = models.DecimalField(
        "czas trwania",
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )
    status = models.CharField(
        "status",
        max_length=20,
        choices=Status.choices,
        default=Status.UPLOADED,
    )
    anonymization_error = models.TextField("błąd anonimizacji", blank=True)
    anonymization_progress_percent = models.PositiveSmallIntegerField(
        "postęp anonimizacji (%)",
        default=0,
    )
    anonymization_progress_current = models.PositiveIntegerField(
        "przetworzone klatki anonimizacji",
        default=0,
    )
    anonymization_progress_total = models.PositiveIntegerField(
        "liczba klatek anonimizacji",
        default=0,
    )
    anonymization_progress_label = models.CharField(
        "etap anonimizacji",
        max_length=120,
        blank=True,
    )
    anonymization_progress_updated_at = models.DateTimeField(
        "ostatnia aktualizacja postępu anonimizacji",
        null=True,
        blank=True,
    )
    anonymized_at = models.DateTimeField("zanonimizowano", null=True, blank=True)
    approved_for_analysis_at = models.DateTimeField(
        "zatwierdzono do analizy",
        null=True,
        blank=True,
    )
    analysis_model_name = models.CharField(
        "model analizy Gemini",
        max_length=120,
        blank=True,
    )
    created_at = models.DateTimeField("utworzono", auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "wideo"
        verbose_name_plural = "wideo"

    def __str__(self):
        return self.original_filename

    def analysis_operations(self):
        """Operacje objęte analizą: wybrane operacje, a w razie ich braku
        pojedyncza operacja (zgodność wsteczna)."""
        ops = list(self.operations.all())
        if ops:
            return ops
        return [self.operation] if self.operation_id else []

    def analysis_process(self):
        if self.process_id:
            return self.process
        ops = self.analysis_operations()
        return ops[0].process if ops else None

    @property
    def is_multi_operation(self):
        return len(self.analysis_operations()) > 1


class Analysis(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", _("W kolejce")
        RUNNING = "running", _("Analiza")
        COMPLETED = "completed", _("Zakończono")
        FAILED = "failed", _("Błąd")

    video = models.ForeignKey(
        Video,
        verbose_name="wideo",
        related_name="analyses",
        on_delete=models.CASCADE,
    )
    status = models.CharField(
        "status",
        max_length=20,
        choices=Status.choices,
        default=Status.QUEUED,
    )
    model_name = models.CharField("model", max_length=120, blank=True)
    prompt = models.TextField("prompt", blank=True)
    raw_response = models.TextField("surowa odpowiedź", blank=True)
    started_at = models.DateTimeField("start", null=True, blank=True)
    completed_at = models.DateTimeField("koniec", null=True, blank=True)
    error_message = models.TextField("komunikat błędu", blank=True)
    input_tokens = models.PositiveIntegerField("tokeny wejściowe", null=True, blank=True)
    output_tokens = models.PositiveIntegerField("tokeny wyjściowe", null=True, blank=True)
    estimated_cost = models.DecimalField(
        "szacowany koszt (USD)",
        max_digits=10,
        decimal_places=4,
        null=True,
        blank=True,
    )
    cost_is_estimated = models.BooleanField("koszt szacowany", default=True)

    class Meta:
        ordering = ["-started_at", "-id"]
        verbose_name = "analiza"
        verbose_name_plural = "analizy"

    def __str__(self):
        return f"Analiza {self.pk} / {self.video.original_filename}"

    def get_absolute_url(self):
        return reverse("analysis_detail", kwargs={"pk": self.pk})


class AnalysisSegment(TimeStampedModel):
    analysis = models.ForeignKey(
        Analysis,
        verbose_name="analiza",
        related_name="segments",
        on_delete=models.CASCADE,
    )
    activity = models.ForeignKey(
        Activity,
        verbose_name=_("czynność"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    operation = models.ForeignKey(
        Operation,
        verbose_name=_("operacja"),
        related_name="segments",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    operation_name = models.CharField("nazwa operacji", max_length=200, blank=True)
    activity_name = models.CharField("nazwa czynności", max_length=200)
    start_seconds = models.DecimalField(_("od"), max_digits=10, decimal_places=2)
    end_seconds = models.DecimalField(_("do"), max_digits=10, decimal_places=2)
    confidence = models.FloatField(_("pewność"), default=0.0)
    # True, gdy model nie różnicował pewności (zwrócił stałe wysokie wartości dla
    # większości segmentów). Wtedy liczbowa pewność jest niewiarygodna i UI nie
    # powinno jej pokazywać jako realnego pomiaru.
    confidence_unreliable = models.BooleanField(_("pewność niewiarygodna"), default=False)
    reason = models.TextField(_("uzasadnienie"), blank=True)
    is_approved = models.BooleanField(_("zatwierdzony"), default=False)

    class Meta:
        ordering = ["start_seconds", "id"]
        verbose_name = "segment analizy"
        verbose_name_plural = "segmenty analizy"

    def __str__(self):
        return f"{self.activity_name}: {self.start_seconds}-{self.end_seconds}s"

    @property
    def duration_seconds(self):
        return max(Decimal("0"), self.end_seconds - self.start_seconds)


class ActivityHint(TimeStampedModel):
    activity = models.ForeignKey(
        Activity,
        verbose_name=_("czynność"),
        related_name="hints",
        on_delete=models.CASCADE,
    )
    text = models.TextField("wskazówka")
    confused_with = models.ForeignKey(
        Activity,
        verbose_name="mylona z",
        related_name="confused_hints",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    source_segment = models.ForeignKey(
        AnalysisSegment,
        verbose_name="segment źródłowy",
        related_name="hints",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    is_active = models.BooleanField("aktywna", default=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "wskazówka czynności"
        verbose_name_plural = "wskazówki czynności"

    def __str__(self):
        return f"{self.activity.name}: {self.text[:40]}"
