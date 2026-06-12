import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from .forms import OperationForm, ProcessForm, VideoUploadForm
from .models import Activity, Analysis, AnalysisSegment, Operation, Process, Video
from .services import analysis_summary, anonymize_video, build_analysis_prompt


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

    def _video_with_file(self, content=b"raw-video"):
        video = Video.objects.create(
            operation=self.operation,
            original_filename="input.mp4",
            duration_seconds=Decimal("1.00"),
        )
        video.file.save("input.mp4", SimpleUploadedFile("input.mp4", content), save=True)
        return video

    def test_anonymize_video_copies_original_when_opencv_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir, override_settings(MEDIA_ROOT=Path(tmpdir)):
            video = self._video_with_file()
            with (
                patch("processes.services._opencv_face_blur", side_effect=ImportError),
                patch("processes.services._full_frame_blur") as full_blur,
            ):
                anonymize_video(video)

            full_blur.assert_not_called()
            video.refresh_from_db()
            self.assertEqual(video.status, Video.Status.AWAITING_APPROVAL)
            self.assertIn("copy_without_full_blur_no_opencv", video.anonymization_error)
            with video.anonymized_file.open("rb") as handle:
                self.assertEqual(handle.read(), b"raw-video")

    def test_anonymize_video_keeps_frame_when_no_faces_detected(self):
        def no_faces(input_path, output_path):
            Path(output_path).write_bytes(b"opencv-output")
            return 0

        with tempfile.TemporaryDirectory() as tmpdir, override_settings(MEDIA_ROOT=Path(tmpdir)):
            video = self._video_with_file()
            video.approved_for_analysis_at = timezone.now()
            video.save(update_fields=["approved_for_analysis_at"])
            with (
                patch("processes.services._opencv_face_blur", side_effect=no_faces),
                patch("processes.services._full_frame_blur") as full_blur,
            ):
                anonymize_video(video)

            full_blur.assert_not_called()
            video.refresh_from_db()
            self.assertEqual(video.status, Video.Status.AWAITING_APPROVAL)
            self.assertIsNone(video.approved_for_analysis_at)
            self.assertIn("opencv_face_blur_no_faces_detected", video.anonymization_error)
            with video.anonymized_file.open("rb") as handle:
                self.assertEqual(handle.read(), b"opencv-output")

    def test_process_and_operation_forms_do_not_show_code_fields(self):
        self.assertNotIn("code", ProcessForm().fields)
        self.assertNotIn("code", OperationForm().fields)

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

    # --- Etap 1: IA + analiza w tle ---

    def test_home_has_no_global_analyze_entry_and_loads_htmx(self):
        response = self.client.get("/")
        body = response.content.decode()
        self.assertNotIn('href="/videos/upload/"', body)
        self.assertIn("htmx.org", body)

    def test_operation_detail_next_step_cta(self):
        with_act = self.client.get(f"/operations/{self.operation.pk}/")
        self.assertIn("Wgraj nagranie do analizy", with_act.content.decode())

        empty_op = Operation.objects.create(
            process=self.process, name="Pakowanie", order=2
        )
        empty = self.client.get(f"/operations/{empty_op.pk}/")
        body = empty.content.decode()
        self.assertIn("Najpierw zdefiniuj czynności", body)
        self.assertNotIn("Wgraj nagranie do analizy", body)

    @override_settings(GEMINI_USE_MOCK=True)
    def test_run_analysis_in_background_spawns_thread(self):
        video = Video.objects.create(
            operation=self.operation,
            original_filename="demo.mp4",
            duration_seconds=Decimal("12.00"),
            anonymized_file=SimpleUploadedFile("a.mp4", b"x"),
            approved_for_analysis_at=timezone.now(),
        )
        from processes import services
        with patch("processes.services.threading.Thread") as Thread:
            instance = Thread.return_value
            services.run_analysis_in_background(video)
            Thread.assert_called_once()
            instance.start.assert_called_once()
            self.assertEqual(Thread.call_args.kwargs["target"], services._analysis_worker)
            self.assertEqual(Thread.call_args.kwargs["args"], (video.pk,))
            self.assertTrue(Thread.call_args.kwargs["daemon"])

    def _make_video_with_analysis(self, status):
        video = Video.objects.create(
            operation=self.operation,
            original_filename="demo.mp4",
            duration_seconds=Decimal("12.00"),
        )
        Analysis.objects.create(video=video, status=status)
        return video

    def test_analysis_status_running_keeps_polling(self):
        video = self._make_video_with_analysis(Analysis.Status.RUNNING)
        r = self.client.get(f"/videos/{video.pk}/analysis-status/")
        body = r.content.decode()
        self.assertIn('hx-trigger="every 3s"', body)
        self.assertIn("Analiza w toku", body)

    def test_analysis_status_completed_links_result_and_stops(self):
        video = self._make_video_with_analysis(Analysis.Status.COMPLETED)
        analysis = video.analyses.first()
        r = self.client.get(f"/videos/{video.pk}/analysis-status/")
        body = r.content.decode()
        self.assertIn(f"/analyses/{analysis.pk}/", body)
        self.assertNotIn("hx-trigger", body)

    def test_analysis_status_failed_shows_error(self):
        video = self._make_video_with_analysis(Analysis.Status.FAILED)
        r = self.client.get(f"/videos/{video.pk}/analysis-status/")
        body = r.content.decode()
        self.assertIn("nie powiodła się", body)
        self.assertNotIn("hx-trigger", body)

    @override_settings(GEMINI_USE_MOCK=True)
    def test_approve_starts_background_and_redirects_to_review(self):
        video = Video.objects.create(
            operation=self.operation,
            original_filename="demo.mp4",
            duration_seconds=Decimal("12.00"),
            anonymized_file=SimpleUploadedFile("a.mp4", b"x"),
        )
        with patch("processes.views.run_analysis_in_background") as bg:
            r = self.client.post(f"/videos/{video.pk}/approve-and-analyze/")
            bg.assert_called_once()
        video.refresh_from_db()
        self.assertEqual(video.status, Video.Status.ANALYZING)
        self.assertIsNotNone(video.approved_for_analysis_at)
        self.assertRedirects(r, f"/videos/{video.pk}/review/")

    def test_video_reanonymize_calls_service_and_allows_reapproval(self):
        video = self._video_with_file()
        video.anonymized_file.save("old.mp4", SimpleUploadedFile("old.mp4", b"old"), save=False)
        video.status = Video.Status.COMPLETED
        video.approved_for_analysis_at = timezone.now()
        video.save(update_fields=["anonymized_file", "status", "approved_for_analysis_at"])
        Analysis.objects.create(video=video, status=Analysis.Status.COMPLETED)

        def fake_anonymize(video_obj):
            video_obj.status = Video.Status.AWAITING_APPROVAL
            video_obj.approved_for_analysis_at = None
            video_obj.save(update_fields=["status", "approved_for_analysis_at"])
            return video_obj

        with patch("processes.views.anonymize_video", side_effect=fake_anonymize) as anonymize:
            response = self.client.post(f"/videos/{video.pk}/reanonymize/")

        anonymize.assert_called_once()
        self.assertRedirects(response, f"/videos/{video.pk}/review/")
        video.refresh_from_db()
        self.assertEqual(video.status, Video.Status.AWAITING_APPROVAL)
        self.assertIsNone(video.approved_for_analysis_at)

        review = self.client.get(f"/videos/{video.pk}/review/")
        body = review.content.decode()
        self.assertIn("Zatwierdź i rozpocznij analizę Gemini", body)
        self.assertIn("Ponów anonimizację z oryginału", body)

    # --- Etap 2: asystent AI (OpenAI) ---

    @override_settings(OPENAI_USE_MOCK=True, OPENAI_API_KEY="")
    def test_assist_activity_generate_returns_all_fields(self):
        from processes.services import assist_activity
        result = assist_activity(
            self.operation,
            {"name": "krojenie pomidora", "quick_description": "operator kroi pomidora"},
            mode="generate",
        )
        for key in ("description", "recognition_rules", "exclusion_rules", "possible_confusions"):
            self.assertIn(key, result)
        self.assertTrue(result["description"])

    @override_settings(OPENAI_USE_MOCK=True, OPENAI_API_KEY="")
    def test_assist_activity_refine_preserves_input(self):
        from processes.services import assist_activity
        result = assist_activity(
            self.operation,
            {"name": "krojenie pomidora", "description": "kroi pomidora nożem"},
            mode="refine",
        )
        self.assertIn("kroi pomidora nożem", result["description"])

    @override_settings(OPENAI_USE_MOCK=True, OPENAI_API_KEY="")
    def test_assist_activity_target_returns_single_field(self):
        from processes.services import assist_activity
        result = assist_activity(
            self.operation,
            {"name": "krojenie pomidora", "description": "kroi pomidora"},
            mode="refine",
            target="exclusion_rules",
        )
        self.assertIn("exclusion_rules", result)
        self.assertNotIn("description", result)

    @override_settings(OPENAI_USE_MOCK=False, OPENAI_API_KEY="")
    def test_assist_activity_requires_openai_key_unless_mock_enabled(self):
        from processes.services import assist_activity
        with self.assertRaisesMessage(RuntimeError, "Brak OPENAI_API_KEY"):
            assist_activity(self.operation, {"name": "krojenie pomidora"})

    @override_settings(OPENAI_USE_MOCK=True, OPENAI_API_KEY="")
    def test_activity_ai_field_returns_single_field_text(self):
        url = f"/operations/{self.operation.pk}/activities/ai-field/"
        r = self.client.post(url, {
            "target": "exclusion_rules",
            "name": "krojenie pomidora",
            "description": "kroi pomidora",
        })
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("operator wykonuje inną zdefiniowaną czynność", body)

    def test_activity_ai_field_requires_activity_name(self):
        url = f"/operations/{self.operation.pk}/activities/ai-field/"
        r = self.client.post(url, {"target": "description", "name": ""})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Najpierw wpisz nazwę czynności", r.content.decode())

    @override_settings(OPENAI_USE_MOCK=False, OPENAI_API_KEY="")
    def test_activity_ai_field_reports_missing_openai_key(self):
        url = f"/operations/{self.operation.pk}/activities/ai-field/"
        r = self.client.post(url, {"target": "description", "name": "solenie kanapki"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Brak OPENAI_API_KEY", r.content.decode())

    def test_activity_form_ai_requires_activity_name(self):
        url = f"/operations/{self.operation.pk}/activities/new/"
        r = self.client.post(url, {
            "action": "ai_suggest",
            "name": "",
            "quick_description": "operator wkłada detal",
            "performed_by": "operator",
        })
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("Najpierw wpisz nazwę czynności", body)
        self.assertFalse(self.operation.activities.filter(name="").exists())

    @override_settings(OPENAI_USE_MOCK=False, OPENAI_API_KEY="")
    def test_activity_form_ai_buttons_remain_clickable_without_key(self):
        url = f"/operations/{self.operation.pk}/activities/new/"
        r = self.client.get(url)
        body = r.content.decode()
        self.assertIn('name="action" value="ai_suggest"', body)
        self.assertIn('name="action" value="ai_refine"', body)
        self.assertNotIn('value="ai_suggest"\n                    disabled', body)
        self.assertNotIn('value="ai_refine"\n                    disabled', body)

    @override_settings(OPENAI_USE_MOCK=False, OPENAI_API_KEY="")
    def test_activity_form_ai_reports_missing_openai_key(self):
        url = f"/operations/{self.operation.pk}/activities/new/"
        r = self.client.post(url, {
            "action": "ai_suggest",
            "name": "solenie kanapki",
            "quick_description": "operator soli kanapkę",
            "performed_by": "operator",
        })
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("Brak OPENAI_API_KEY", body)
        self.assertFalse(self.operation.activities.filter(name="solenie kanapki").exists())

    @override_settings(OPENAI_USE_MOCK=True, OPENAI_API_KEY="")
    def test_activity_form_ai_refine_fills_fields(self):
        url = f"/operations/{self.operation.pk}/activities/new/"
        r = self.client.post(url, {
            "action": "ai_refine",
            "name": "krojenie pomidora",
            "description": "kroi pomidora nożem",
            "recognition_rules": "",
            "exclusion_rules": "",
            "performed_by": "operator",
        })
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("kroi pomidora nożem", body)
        self.assertIn("doprecyzowano", body)
        self.assertFalse(self.operation.activities.filter(name="krojenie pomidora").exists())

    # --- Etap 3: UX korekty wyniku ---

    def test_segments_needing_review_flags_low_confidence_and_uncertain(self):
        from processes.services import segments_needing_review
        video = Video.objects.create(
            operation=self.operation, original_filename="d.mp4", duration_seconds=Decimal("30.00")
        )
        analysis = Analysis.objects.create(video=video, status=Analysis.Status.COMPLETED)
        high = AnalysisSegment.objects.create(
            analysis=analysis, activity=self.load, activity_name="załadunek detalu",
            start_seconds=Decimal("0"), end_seconds=Decimal("5"), confidence=0.9,
        )
        low = AnalysisSegment.objects.create(
            analysis=analysis, activity=self.machine, activity_name="praca maszyny",
            start_seconds=Decimal("5"), end_seconds=Decimal("10"), confidence=0.2,
        )
        unc = AnalysisSegment.objects.create(
            analysis=analysis, activity=None, activity_name="niepewne",
            start_seconds=Decimal("10"), end_seconds=Decimal("15"), confidence=0.8,
        )
        flagged = segments_needing_review(analysis)
        self.assertIn(low, flagged)
        self.assertIn(unc, flagged)
        self.assertNotIn(high, flagged)

    def test_segment_reassign_updates_activity(self):
        video = Video.objects.create(
            operation=self.operation, original_filename="d.mp4", duration_seconds=Decimal("20.00")
        )
        analysis = Analysis.objects.create(video=video, status=Analysis.Status.COMPLETED)
        seg = AnalysisSegment.objects.create(
            analysis=analysis, activity=self.load, activity_name=self.load.name,
            start_seconds=Decimal("0"), end_seconds=Decimal("5"), confidence=0.3,
        )
        r = self.client.post(
            f"/analyses/{analysis.pk}/segments/{seg.pk}/reassign/",
            {"activity": self.machine.pk},
        )
        self.assertEqual(r.status_code, 200)
        seg.refresh_from_db()
        self.assertEqual(seg.activity, self.machine)
        self.assertEqual(seg.activity_name, self.machine.name)
        self.assertIn("zapisano", r.content.decode())

    def test_analysis_detail_shows_needs_review_section(self):
        video = Video.objects.create(
            operation=self.operation, original_filename="d.mp4", duration_seconds=Decimal("20.00")
        )
        analysis = Analysis.objects.create(video=video, status=Analysis.Status.COMPLETED)
        AnalysisSegment.objects.create(
            analysis=analysis, activity=self.machine, activity_name="praca maszyny",
            start_seconds=Decimal("0"), end_seconds=Decimal("5"), confidence=0.2,
        )
        r = self.client.get(f"/analyses/{analysis.pk}/")
        body = r.content.decode()
        self.assertIn("Wymaga sprawdzenia", body)
        self.assertIn('data-start=', body)
        self.assertIn('data-end=', body)
        self.assertIn("htmx:configRequest", body)
        self.assertIn("X-CSRFToken", body)

    # --- Etap 3b: pętla informacji zwrotnej ---

    def test_activity_hint_creation_and_str(self):
        from processes.models import ActivityHint
        hint = ActivityHint.objects.create(
            activity=self.load,
            text="pieprz jest ciemniejszy niż sól",
            confused_with=self.machine,
        )
        self.assertTrue(hint.is_active)
        self.assertEqual(hint.activity, self.load)
        self.assertIn("pieprz", str(hint))
        self.assertEqual(self.load.hints.count(), 1)

    def test_build_prompt_includes_active_hints_only(self):
        from processes.models import ActivityHint
        ActivityHint.objects.create(
            activity=self.load, text="detal trzymany oburącz", is_active=True
        )
        ActivityHint.objects.create(
            activity=self.load, text="wskazówka wyłączona", is_active=False
        )
        prompt = build_analysis_prompt(self.operation)
        self.assertIn("Wskazówki z wcześniejszych korekt", prompt)
        self.assertIn("detal trzymany oburącz", prompt)
        self.assertNotIn("wskazówka wyłączona", prompt)

    def _segment_for_feedback(self):
        video = Video.objects.create(
            operation=self.operation, original_filename="d.mp4", duration_seconds=Decimal("20.00")
        )
        analysis = Analysis.objects.create(video=video, status=Analysis.Status.COMPLETED)
        seg = AnalysisSegment.objects.create(
            analysis=analysis, activity=self.load, activity_name=self.load.name,
            start_seconds=Decimal("0"), end_seconds=Decimal("5"), confidence=0.3,
        )
        return analysis, seg

    def test_segment_approve_sets_is_approved(self):
        analysis, seg = self._segment_for_feedback()
        r = self.client.post(f"/analyses/{analysis.pk}/segments/{seg.pk}/approve/")
        self.assertEqual(r.status_code, 200)
        seg.refresh_from_db()
        self.assertTrue(seg.is_approved)
        self.assertIn("potwierdzono", r.content.decode().casefold())

    def test_segment_feedback_creates_hint(self):
        from processes.models import ActivityHint
        analysis, seg = self._segment_for_feedback()
        r = self.client.post(
            f"/analyses/{analysis.pk}/segments/{seg.pk}/feedback/",
            {"note": "pieprz jest ciemniejszy", "confused_with": self.machine.pk},
        )
        self.assertEqual(r.status_code, 200)
        hint = ActivityHint.objects.get(source_segment=seg)
        self.assertEqual(hint.activity, self.load)
        self.assertEqual(hint.confused_with, self.machine)
        self.assertIn("pieprz jest ciemniejszy", hint.text)
        self.assertIn("uwzględni", r.content.decode())

    def test_hint_toggle_flips_active(self):
        from processes.models import ActivityHint
        hint = ActivityHint.objects.create(activity=self.load, text="x", is_active=True)
        r = self.client.post(f"/hints/{hint.pk}/toggle/")
        self.assertEqual(r.status_code, 200)
        hint.refresh_from_db()
        self.assertFalse(hint.is_active)

    def test_hint_delete_removes(self):
        from processes.models import ActivityHint
        hint = ActivityHint.objects.create(activity=self.load, text="x")
        r = self.client.post(f"/hints/{hint.pk}/delete/")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(ActivityHint.objects.filter(pk=hint.pk).exists())

    # --- Etap 3c: kolejność czynności ---

    def test_activity_create_sets_next_order(self):
        self.load.order = 1
        self.load.save(update_fields=["order"])
        self.machine.order = 2
        self.machine.save(update_fields=["order"])
        url = f"/operations/{self.operation.pk}/activities/new/"
        r = self.client.post(url, {"action": "save", "name": "nowa czynnosc", "performed_by": "operator"})
        self.assertEqual(r.status_code, 302)
        new = self.operation.activities.get(name="nowa czynnosc")
        self.assertEqual(new.order, 3)

    def test_activity_move_swaps_order(self):
        self.client.post(f"/activities/{self.load.pk}/move/up/")
        self.load.refresh_from_db()
        self.machine.refresh_from_db()
        self.assertLess(self.load.order, self.machine.order)

    def test_prompt_includes_activity_order_hint(self):
        self.machine.order = 1; self.machine.save(update_fields=["order"])
        self.load.order = 2; self.load.save(update_fields=["order"])
        prompt = build_analysis_prompt(self.operation)
        self.assertIn("Typowa kolejność", prompt)
        self.assertIn("nie sztywna reguła", prompt)
        self.assertLess(prompt.index("praca maszyny"), prompt.index("załadunek detalu"))

    # --- Etap 4a: eksport CSV i zatwierdzanie całej analizy ---

    def _analysis_with_two_segments(self):
        video = Video.objects.create(
            operation=self.operation, original_filename="export.mp4", duration_seconds=Decimal("12.00")
        )
        analysis = Analysis.objects.create(video=video, status=Analysis.Status.COMPLETED)
        AnalysisSegment.objects.create(
            analysis=analysis, activity=self.load, activity_name=self.load.name,
            start_seconds=Decimal("0.00"), end_seconds=Decimal("4.00"), confidence=0.91,
            reason="operator wkłada detal",
        )
        AnalysisSegment.objects.create(
            analysis=analysis, activity=self.machine, activity_name=self.machine.name,
            start_seconds=Decimal("4.00"), end_seconds=Decimal("12.00"), confidence=0.82,
            reason="maszyna pracuje",
        )
        return analysis

    def test_analysis_approve_all_marks_all_segments(self):
        analysis = self._analysis_with_two_segments()
        r = self.client.post(f"/analyses/{analysis.pk}/approve-all/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(analysis.segments.filter(is_approved=True).count(), 2)
        self.assertFalse(analysis.segments.filter(is_approved=False).exists())

    def test_analysis_export_csv_contains_segments(self):
        analysis = self._analysis_with_two_segments()
        r = self.client.get(f"/analyses/{analysis.pk}/export.csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r["Content-Type"])
        self.assertIn("attachment;", r["Content-Disposition"])
        body = r.content.decode("utf-8-sig")
        self.assertIn("start_seconds,end_seconds,duration_seconds,activity,confidence,is_approved,reason", body)
        self.assertIn("0.00,4.00,4.00,załadunek detalu,0.91,nie,operator wkłada detal", body)
        self.assertIn("4.00,12.00,8.00,praca maszyny,0.82,nie,maszyna pracuje", body)

    def test_analysis_detail_shows_export_and_approve_actions(self):
        analysis = self._analysis_with_two_segments()
        r = self.client.get(f"/analyses/{analysis.pk}/")
        body = r.content.decode()
        self.assertIn(f"/analyses/{analysis.pk}/export.csv", body)
        self.assertIn(f"/analyses/{analysis.pk}/approve-all/", body)
        self.assertIn("Zatwierdzone", body)
        self.assertIn("0/2", body)
