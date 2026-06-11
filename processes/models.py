from decimal import Decimal

from django.db import models
from django.urls import reverse


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField("utworzono", auto_now_add=True)
    updated_at = models.DateTimeField("zaktualizowano", auto_now=True)

    class Meta:
        abstract = True


class Process(TimeStampedModel):
    name = models.CharField("nazwa procesu", max_length=200)
    code = models.CharField("kod procesu", max_length=80, blank=True)
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
    name = models.CharField("nazwa operacji", max_length=200)
    code = models.CharField("kod operacji", max_length=80, blank=True)
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
        OPERATOR = "operator", "Operator"
        MACHINE = "machine", "Maszyna"
        BOTH = "both", "Operator i maszyna"
        UNKNOWN = "unknown", "Nieokreślone"

    operation = models.ForeignKey(
        Operation,
        verbose_name="operacja",
        related_name="activities",
        on_delete=models.CASCADE,
    )
    name = models.CharField("nazwa czynności", max_length=200)
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

    class Meta:
        ordering = ["name"]
        verbose_name = "czynność"
        verbose_name_plural = "czynności"

    def __str__(self):
        return self.name


class Video(models.Model):
    class Status(models.TextChoices):
        UPLOADED = "uploaded", "Wgrano"
        ANONYMIZING = "anonymizing", "Anonimizacja"
        AWAITING_APPROVAL = "awaiting_approval", "Do zatwierdzenia"
        APPROVED = "approved", "Zatwierdzono"
        ANALYZING = "analyzing", "Analiza"
        COMPLETED = "completed", "Zakończono"
        FAILED = "failed", "Błąd"

    operation = models.ForeignKey(
        Operation,
        verbose_name="operacja",
        related_name="videos",
        on_delete=models.CASCADE,
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
    anonymized_at = models.DateTimeField("zanonimizowano", null=True, blank=True)
    approved_for_analysis_at = models.DateTimeField(
        "zatwierdzono do analizy",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField("utworzono", auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "wideo"
        verbose_name_plural = "wideo"

    def __str__(self):
        return self.original_filename


class Analysis(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "W kolejce"
        RUNNING = "running", "Analiza"
        COMPLETED = "completed", "Zakończono"
        FAILED = "failed", "Błąd"

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
        verbose_name="czynność",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    activity_name = models.CharField("nazwa czynności", max_length=200)
    start_seconds = models.DecimalField("od", max_digits=10, decimal_places=2)
    end_seconds = models.DecimalField("do", max_digits=10, decimal_places=2)
    confidence = models.FloatField("pewność", default=0.0)
    reason = models.TextField("uzasadnienie", blank=True)
    is_approved = models.BooleanField("zatwierdzony", default=False)

    class Meta:
        ordering = ["start_seconds", "id"]
        verbose_name = "segment analizy"
        verbose_name_plural = "segmenty analizy"

    def __str__(self):
        return f"{self.activity_name}: {self.start_seconds}-{self.end_seconds}s"

    @property
    def duration_seconds(self):
        return max(Decimal("0"), self.end_seconds - self.start_seconds)
