from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from .forms import VideoUploadForm
from .models import Activity, Analysis, AnalysisSegment, Operation, Process, Video
from .services import analysis_summary, build_analysis_prompt


class ProcessDemoTests(TestCase):
    def setUp(self):
        self.process = Process.objects.create(
            name="Produkcja elementu A",
            description="Obróbka i kontrola elementu metalowego",
        )
        self.operation = Operation.objects.create(
            process=self.process,
            name="Frezowanie",
            description="Obróbka detalu na frezarce CNC",
            order=1,
        )
        self.load = Activity.objects.create(
            operation=self.operation,
            name="załadunek detalu",
            description="Operator wkłada detal do frezarki.",
            recognition_rules="- operator trzyma detal",
            exclusion_rules="- operator wyjmuje detal",
            performed_by=Activity.Performer.OPERATOR,
        )
        self.machine = Activity.objects.create(
            operation=self.operation,
            name="praca maszyny",
            description="Maszyna wykonuje obróbkę.",
            performed_by=Activity.Performer.MACHINE,
        )

    def test_prompt_contains_only_defined_activities(self):
        prompt = build_analysis_prompt(self.operation)

        self.assertIn("Frezowanie", prompt)
        self.assertIn("załadunek detalu", prompt)
        self.assertIn("praca maszyny", prompt)
        self.assertIn("Nie twórz nowych nazw czynności", prompt)

    def test_upload_form_rejects_unsupported_format(self):
        upload = SimpleUploadedFile("film.avi", b"demo", content_type="video/avi")
        form = VideoUploadForm(data={"operation": self.operation.pk}, files={"file": upload})

        self.assertFalse(form.is_valid())
        self.assertIn("Demo obsługuje tylko pliki MP4 i MOV.", form.errors["file"])

    def test_analysis_summary_counts_operator_and_machine_time(self):
        video = Video.objects.create(
            operation=self.operation,
            original_filename="demo.mp4",
            duration_seconds=Decimal("20.00"),
        )
        analysis = Analysis.objects.create(video=video, status=Analysis.Status.COMPLETED)
        AnalysisSegment.objects.create(
            analysis=analysis,
            activity=self.load,
            activity_name=self.load.name,
            start_seconds=Decimal("0.00"),
            end_seconds=Decimal("8.00"),
            confidence=0.9,
        )
        AnalysisSegment.objects.create(
            analysis=analysis,
            activity=self.machine,
            activity_name=self.machine.name,
            start_seconds=Decimal("8.00"),
            end_seconds=Decimal("20.00"),
            confidence=0.8,
        )

        summary = analysis_summary(analysis)

        self.assertEqual(summary["operator"], Decimal("8.00"))
        self.assertEqual(summary["machine"], Decimal("12.00"))
        self.assertEqual(summary["segmented_duration"], Decimal("20.00"))
        self.assertEqual(len(summary["gantt_rows"]), 2)
        self.assertEqual(summary["gantt_rows"][0]["name"], "załadunek detalu")
        self.assertEqual(summary["gantt_rows"][0]["bars"][0]["left"], "0.0000%")
        self.assertEqual(summary["gantt_rows"][0]["bars"][0]["width"], "40.0000%")
        self.assertEqual(summary["gantt_rows"][1]["name"], "praca maszyny")
