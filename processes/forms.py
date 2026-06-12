from pathlib import Path

from django import forms

from .models import Activity, AnalysisSegment, Operation, Process, Video


class StyledFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "check-input")
            elif isinstance(widget, forms.Select):
                widget.attrs.setdefault("class", "select")
            else:
                widget.attrs.setdefault("class", "input")


class ProcessForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Process
        fields = ["name", "description"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
        }


class OperationForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Operation
        fields = ["name", "description", "order"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "order": forms.NumberInput(attrs={"min": 1}),
        }


class ActivityForm(StyledFormMixin, forms.ModelForm):
    quick_description = forms.CharField(
        label="Krótki opis dla AI",
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": "Np. Operator wkłada detal do frezarki.",
            }
        ),
        help_text="Nie jest zapisywany. Służy tylko do wygenerowania propozycji opisu.",
    )

    class Meta:
        model = Activity
        fields = [
            "name",
            "quick_description",
            "description",
            "recognition_rules",
            "exclusion_rules",
            "minimum_duration_seconds",
            "performed_by",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "recognition_rules": forms.Textarea(attrs={"rows": 5}),
            "exclusion_rules": forms.Textarea(attrs={"rows": 5}),
            "minimum_duration_seconds": forms.NumberInput(attrs={"min": 0, "step": 0.1}),
        }


class OperationChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.process.name} / {obj.name}"


class VideoUploadForm(StyledFormMixin, forms.ModelForm):
    operation = OperationChoiceField(
        label="Operacja",
        queryset=Operation.objects.select_related("process").order_by("process__name", "order"),
    )

    class Meta:
        model = Video
        fields = ["operation", "file"]

    def __init__(self, *args, operation=None, **kwargs):
        super().__init__(*args, **kwargs)
        if operation is not None:
            self.fields["operation"].initial = operation
            self.fields["operation"].queryset = Operation.objects.filter(pk=operation.pk)
            self.fields["operation"].widget = forms.HiddenInput()

    def clean_file(self):
        return _validate_video_file(self.cleaned_data["file"])


def _validate_video_file(uploaded_file):
    extension = Path(uploaded_file.name).suffix.lower()
    if extension not in {".mp4", ".mov"}:
        raise forms.ValidationError("Demo obsługuje tylko pliki MP4 i MOV.")
    if uploaded_file.size > 1024 * 1024 * 1024:
        raise forms.ValidationError("Maksymalny rozmiar pliku w demo to 1 GB.")
    return uploaded_file


class ProcessVideoUploadForm(StyledFormMixin, forms.ModelForm):
    operations = forms.ModelMultipleChoiceField(
        label="Operacje do analizy",
        queryset=Operation.objects.none(),
        widget=forms.CheckboxSelectMultiple(attrs={"class": ""}),
        help_text="Zaznacz operacje, które mogą wystąpić na nagraniu (możesz wybrać jedną).",
    )

    class Meta:
        model = Video
        fields = ["file"]

    def __init__(self, *args, process=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.process = process
        if process is not None:
            self.fields["operations"].queryset = process.operations.order_by("order", "name")

    def clean_operations(self):
        operations = self.cleaned_data["operations"]
        if not operations:
            raise forms.ValidationError("Zaznacz przynajmniej jedną operację.")
        return operations

    def clean_file(self):
        return _validate_video_file(self.cleaned_data["file"])


class SegmentCorrectionForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = AnalysisSegment
        fields = [
            "activity",
            "start_seconds",
            "end_seconds",
            "confidence",
            "reason",
            "is_approved",
        ]
        widgets = {
            "start_seconds": forms.NumberInput(attrs={"min": 0, "step": 0.1}),
            "end_seconds": forms.NumberInput(attrs={"min": 0, "step": 0.1}),
            "confidence": forms.NumberInput(attrs={"min": 0, "max": 1, "step": 0.01}),
            "reason": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, operation, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["activity"].queryset = operation.activities.all()

    def clean(self):
        cleaned_data = super().clean()
        start = cleaned_data.get("start_seconds")
        end = cleaned_data.get("end_seconds")
        if start is not None and end is not None and end <= start:
            raise forms.ValidationError("Koniec segmentu musi być późniejszy niż początek.")
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        if instance.activity:
            instance.activity_name = instance.activity.name
        if commit:
            instance.save()
        return instance
