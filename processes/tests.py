import tempfile
from datetime import timedelta
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

    def test_prompt_anchors_duration_and_demands_decimal_seconds(self):
        from processes.services import build_multi_operation_prompt
        prompt = build_analysis_prompt(self.operation, duration_seconds=Decimal("90.47"))
        self.assertIn("90.47", prompt)
        self.assertIn("SEKUNDACH DZIESIĘTNYCH", prompt)
        self.assertIn("nie w formacie zegarowym minuty:sekundy", prompt.replace("a NIE", "nie"))
        multi = build_multi_operation_prompt(
            self.process, [self.operation], duration_seconds=Decimal("90.47")
        )
        self.assertIn("90.47", multi)
        self.assertIn("SEKUNDACH DZIESIĘTNYCH", multi)

    def test_prompt_demands_evidence_alternatives_and_uncertain_activity(self):
        prompt = build_analysis_prompt(self.operation)
        self.assertIn('"alternative_activity"', prompt)
        self.assertIn('"evidence"', prompt)
        self.assertIn('"missing_evidence"', prompt)
        self.assertIn('activity":"nazwa czynności albo niepewne"', prompt)
        self.assertIn("nie używaj stałej wartości confidence", prompt)
        self.assertIn("nie scalaj kilku odrębnych wystąpień", prompt)
        self.assertIn("za krótkie, żeby było znaczące", prompt)
        self.assertIn("przeplot A/B/A", prompt)
        self.assertIn("stabilne, widoczne sygnały innej zdefiniowanej czynności", prompt)
        self.assertIn('"box_2d"', prompt)

    def test_prompt_includes_universal_evidence_discipline(self):
        from processes.services import build_multi_operation_prompt

        single = build_analysis_prompt(self.operation)
        multi = build_multi_operation_prompt(self.process, [self.operation])
        for prompt in (single, multi):
            self.assertIn("Dyscyplina dowodowa", prompt)
            self.assertIn("najbliższy odpowiednik", prompt)
            self.assertIn("nie wpisuj sygnałów, których nie widać", prompt)
            self.assertIn("brak wyraźnego sygnału odróżniającego", prompt)

    def test_confidence_plateau_marks_segments_unreliable(self):
        from decimal import Decimal as D
        from processes.services import _apply_temporal_quality_checks

        segs = []
        for i, name in enumerate(["A", "B", "A", "B", "A"]):
            segs.append(
                {
                    "start_seconds": D(i * 3),
                    "end_seconds": D(i * 3 + 3),
                    "activity_name": name,
                    "operation_name": "",
                    "confidence": 0.95,
                    "_model_confidence": 0.95,
                }
            )
        _apply_temporal_quality_checks(segs)
        self.assertTrue(all(s.get("confidence_unreliable") for s in segs))
        self.assertTrue(all(s["confidence"] <= 0.64 for s in segs))

    def test_video_content_part_sets_fps_from_settings(self):
        from processes.services import _video_content_part

        class FakeUpload:
            uri = "files/abc"
            mime_type = "video/mp4"

        up = FakeUpload()
        with override_settings(GEMINI_VIDEO_FPS=0):
            self.assertIs(_video_content_part(up), up)
        try:
            from google.genai import types  # noqa: F401
        except Exception:
            self.skipTest("google.genai niedostępne")
        with override_settings(GEMINI_VIDEO_FPS=5):
            part = _video_content_part(up)
            self.assertIsNot(part, up)
            self.assertEqual(float(part.video_metadata.fps), 5.0)

    def test_prompt_adds_pairwise_confusion_rules_from_feedback(self):
        from processes.models import ActivityHint

        ActivityHint.objects.create(
            activity=self.load,
            confused_with=self.machine,
            text="small setup movements still mean loading, not machine work",
        )
        prompt = build_analysis_prompt(self.operation)
        self.assertIn("Reguły rozróżniania często mylonych czynności", prompt)
        self.assertIn('między "załadunek detalu" i "praca maszyny"', prompt)
        self.assertIn("small setup movements", prompt)

    def test_extract_json_recovers_segments_from_box2d_wrapper(self):
        from processes.services import _extract_json

        raw = """
```json
[
  {"box_2d": [
    {"start_seconds": 0, "end_seconds": 1, "activity": "załadunek detalu", "confidence": 0.8}
  ]
]
```
"""
        payload = _extract_json(raw)

        self.assertEqual(len(payload["segments"]), 1)
        self.assertEqual(payload["segments"][0]["activity"], "załadunek detalu")

    def test_normalize_calibrates_repeated_high_confidence_and_short_flaps(self):
        from processes.services import _normalize_segments

        payload = {
            "segments": [
                {
                    "start_seconds": 0,
                    "end_seconds": 3,
                    "activity": self.load.name,
                    "confidence": 0.95,
                    "reason": "loading",
                },
                {
                    "start_seconds": 3,
                    "end_seconds": 4,
                    "activity": self.machine.name,
                    "confidence": 0.95,
                    "reason": "brief machine-like motion",
                },
                {
                    "start_seconds": 4,
                    "end_seconds": 7,
                    "activity": self.load.name,
                    "confidence": 0.95,
                    "reason": "loading again",
                },
                {
                    "start_seconds": 7,
                    "end_seconds": 10,
                    "activity": self.machine.name,
                    "confidence": 0.95,
                    "reason": "machine work",
                },
            ]
        }

        segments = _normalize_segments(payload, self.operation, duration_seconds=Decimal("10"))

        self.assertTrue(all(segment["confidence"] < 0.95 for segment in segments))
        self.assertEqual(segments[1]["activity"], self.machine)
        self.assertLessEqual(segments[1]["confidence"], 0.58)
        self.assertIn("Kalibracja", segments[0]["reason"])
        self.assertIn("Kontrola czasowa", segments[1]["reason"])

    def test_normalize_lowers_confidence_when_alternative_or_missing_evidence_exists(self):
        from processes.services import _normalize_segments

        payload = {
            "segments": [
                {
                    "start_seconds": 0,
                    "end_seconds": 6,
                    "activity": self.load.name,
                    "confidence": 0.95,
                    "alternative_activity": self.machine.name,
                    "evidence": ["hands are near the part"],
                    "missing_evidence": ["the part placement is partly hidden"],
                    "reason": "could be loading",
                    "confidence_reason": "ambiguous with machine setup",
                }
            ]
        }

        segment = _normalize_segments(payload, self.operation)[0]

        self.assertLessEqual(segment["confidence"], 0.64)
        self.assertIn("Dowody: hands are near the part", segment["reason"])
        self.assertIn("Brakujące dowody", segment["reason"])
        self.assertIn("Alternatywa: praca maszyny", segment["reason"])

    def test_unknown_activity_becomes_system_uncertain(self):
        from processes.services import _normalize_segments

        payload = {
            "segments": [
                {
                    "start_seconds": 0,
                    "end_seconds": 4,
                    "activity": "not a defined activity",
                    "confidence": 0.91,
                    "reason": "unclear movement",
                }
            ]
        }

        segment = _normalize_segments(payload, self.operation)[0]

        self.assertIsNone(segment["activity"])
        self.assertEqual(segment["activity_name"], "niepewne")
        self.assertLessEqual(segment["confidence"], 0.45)

    def test_extract_json_accepts_top_level_segment_array(self):
        from processes.services import _extract_json

        payload = _extract_json(
            """```json
[
  {"start_seconds": 0, "end_seconds": 1, "activity": "x", "confidence": 0.5}
]
```"""
        )

        self.assertIn("segments", payload)
        self.assertEqual(len(payload["segments"]), 1)
        self.assertEqual(payload["segments"][0]["activity"], "x")

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

    def test_anonymize_video_fails_when_opencv_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir, override_settings(MEDIA_ROOT=Path(tmpdir)):
            video = self._video_with_file()
            with patch("processes.services._opencv_face_blur", side_effect=ImportError("No module named cv2")):
                with self.assertRaises(ImportError):
                    anonymize_video(video)

            video.refresh_from_db()
            self.assertEqual(video.status, Video.Status.FAILED)
            self.assertFalse(video.anonymized_file)
            self.assertIn("No module named cv2", video.anonymization_error)

    def test_anonymize_video_fails_when_no_faces_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir, override_settings(MEDIA_ROOT=Path(tmpdir)):
            video = self._video_with_file()
            video.approved_for_analysis_at = timezone.now()
            video.save(update_fields=["approved_for_analysis_at"])
            with patch(
                "processes.services._opencv_face_blur",
                side_effect=RuntimeError("Nie wykryto twarzy w filmie."),
            ):
                with self.assertRaises(RuntimeError):
                    anonymize_video(video)

            video.refresh_from_db()
            self.assertEqual(video.status, Video.Status.FAILED)
            self.assertFalse(video.anonymized_file)
            self.assertIn("Nie wykryto twarzy", video.anonymization_error)

    def test_anonymize_video_uses_face_detector_output_only(self):
        def face_blur(input_path, output_path, progress_callback=None):
            if progress_callback:
                progress_callback(10, 10, "Wykrywanie i maskowanie twarzy", percent=90)
            Path(output_path).write_bytes(b"face-mask-output")
            return 4

        with tempfile.TemporaryDirectory() as tmpdir, override_settings(MEDIA_ROOT=Path(tmpdir)):
            video = self._video_with_file()
            video.approved_for_analysis_at = timezone.now()
            video.save(update_fields=["approved_for_analysis_at"])
            with patch("processes.services._opencv_face_blur", side_effect=face_blur) as face_blur_mock:
                anonymize_video(video)

            face_blur_mock.assert_called_once()
            video.refresh_from_db()
            self.assertEqual(video.status, Video.Status.AWAITING_APPROVAL)
            self.assertIsNone(video.approved_for_analysis_at)
            self.assertIn("Rozmyto 4 wykrytych wystąpień twarzy", video.anonymization_error)
            self.assertNotIn("yunet_face_blur", video.anonymization_error)
            with video.anonymized_file.open("rb") as handle:
                self.assertEqual(handle.read(), b"face-mask-output")

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

    def test_gantt_merges_adjacent_same_activity_segments(self):
        video = Video.objects.create(
            operation=self.operation,
            original_filename="demo.mp4",
            duration_seconds=Decimal("10.00"),
        )
        analysis = Analysis.objects.create(video=video, status=Analysis.Status.COMPLETED)
        # AI zwróciło 3 stykające się segmenty tej samej czynności (0-3, 3-7, 7-10).
        for start, end, conf in [("0", "3", 0.9), ("3", "7", 0.6), ("7", "10", 0.3)]:
            AnalysisSegment.objects.create(
                analysis=analysis,
                activity=self.load,
                activity_name=self.load.name,
                start_seconds=Decimal(start),
                end_seconds=Decimal(end),
                confidence=conf,
            )

        summary = analysis_summary(analysis)

        rows = summary["gantt_rows"]
        self.assertEqual(len(rows), 1)
        bars = rows[0]["bars"]
        # Trzy przylegające segmenty mają się scalić w jeden blok 0-10 s.
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["start_seconds"], Decimal("0"))
        self.assertEqual(bars[0]["end_seconds"], Decimal("10"))
        self.assertEqual(bars[0]["left"], "0.0000%")
        self.assertEqual(bars[0]["width"], "100.0000%")

    def _single_segment_analysis(self, start="0", end="10"):
        video = Video.objects.create(
            operation=self.operation,
            original_filename="demo.mp4",
            duration_seconds=Decimal("10.00"),
        )
        analysis = Analysis.objects.create(video=video, status=Analysis.Status.COMPLETED)
        seg = AnalysisSegment.objects.create(
            analysis=analysis,
            activity=self.load,
            activity_name=self.load.name,
            start_seconds=Decimal(start),
            end_seconds=Decimal(end),
            confidence=0.8,
        )
        return analysis, seg

    def test_segment_create_adds_activity(self):
        analysis, seg = self._single_segment_analysis("0", "10")
        r = self.client.post(
            f"/analyses/{analysis.pk}/segments/create/",
            {"activity": self.machine.pk, "start_seconds": "3.0", "end_seconds": "6.0"},
        )
        self.assertEqual(r.status_code, 302)
        new = analysis.segments.get(start_seconds=Decimal("3.00"))
        self.assertEqual(new.end_seconds, Decimal("6.00"))
        self.assertEqual(new.activity, self.machine)
        self.assertEqual(new.operation, self.operation)
        self.assertEqual(new.activity_name, self.machine.name)
        self.assertTrue(new.is_approved)

    def test_segment_create_rejects_bad_range(self):
        analysis, seg = self._single_segment_analysis("0", "10")
        r = self.client.post(
            f"/analyses/{analysis.pk}/segments/create/",
            {"activity": self.machine.pk, "start_seconds": "6.0", "end_seconds": "3.0"},
        )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(analysis.segments.count(), 1)

    def test_segment_create_rejects_unknown_activity(self):
        analysis, seg = self._single_segment_analysis("0", "10")
        r = self.client.post(
            f"/analyses/{analysis.pk}/segments/create/",
            {"activity": "99999", "start_seconds": "1.0", "end_seconds": "2.0"},
        )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(analysis.segments.count(), 1)

    def _two_segment_analysis(self):
        video = Video.objects.create(
            operation=self.operation,
            original_filename="demo.mp4",
            duration_seconds=Decimal("20.00"),
        )
        analysis = Analysis.objects.create(video=video, status=Analysis.Status.COMPLETED)
        a = AnalysisSegment.objects.create(
            analysis=analysis, activity=self.load, activity_name=self.load.name,
            start_seconds=Decimal("0"), end_seconds=Decimal("10"), confidence=0.8,
        )
        b = AnalysisSegment.objects.create(
            analysis=analysis, activity=self.machine, activity_name=self.machine.name,
            start_seconds=Decimal("10"), end_seconds=Decimal("20"), confidence=0.8,
        )
        return analysis, a, b

    def test_segment_delete_closes_gap_by_extending_previous(self):
        analysis, a, b = self._two_segment_analysis()
        r = self.client.post(f"/analyses/{analysis.pk}/segments/{b.pk}/delete/")
        self.assertEqual(r.status_code, 302)
        self.assertFalse(analysis.segments.filter(pk=b.pk).exists())
        a.refresh_from_db()
        # poprzedni rozciągnięty, by domknąć lukę
        self.assertEqual(a.end_seconds, Decimal("20.00"))

    # --- Historia analiz wideo (paginacja) ---

    def test_process_videos_lists_only_this_process(self):
        Video.objects.create(
            process=self.process,
            operation=self.operation,
            original_filename="mine.mp4",
            duration_seconds=Decimal("10.00"),
        )
        other_process = Process.objects.create(name="Inny proces")
        other_op = Operation.objects.create(process=other_process, name="Op X", order=1)
        Video.objects.create(
            process=other_process,
            operation=other_op,
            original_filename="foreign.mp4",
            duration_seconds=Decimal("10.00"),
        )
        response = self.client.get(f"/processes/{self.process.pk}/videos/")
        body = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("mine.mp4", body)
        self.assertNotIn("foreign.mp4", body)

    def test_process_videos_paginates_at_20(self):
        for i in range(21):
            Video.objects.create(
                process=self.process,
                operation=self.operation,
                original_filename=f"v{i}.mp4",
                duration_seconds=Decimal("5.00"),
            )
        page1 = self.client.get(f"/processes/{self.process.pk}/videos/")
        self.assertEqual(len(page1.context["page_obj"].object_list), 20)
        page2 = self.client.get(f"/processes/{self.process.pk}/videos/?page=2")
        self.assertEqual(len(page2.context["page_obj"].object_list), 1)

    def test_process_videos_row_links_depend_on_status(self):
        done = Video.objects.create(
            process=self.process,
            operation=self.operation,
            original_filename="done.mp4",
            duration_seconds=Decimal("5.00"),
            status=Video.Status.COMPLETED,
        )
        analysis = Analysis.objects.create(video=done, status=Analysis.Status.COMPLETED)
        pending = Video.objects.create(
            process=self.process,
            operation=self.operation,
            original_filename="pending.mp4",
            duration_seconds=Decimal("5.00"),
            status=Video.Status.AWAITING_APPROVAL,
        )
        body = self.client.get(f"/processes/{self.process.pk}/videos/").content.decode()
        self.assertIn(f"/analyses/{analysis.pk}/", body)
        self.assertIn(f"/videos/{pending.pk}/review/", body)

    # --- Etap 1: IA + analiza w tle ---

    def test_home_has_no_global_analyze_entry_and_loads_htmx(self):
        response = self.client.get("/")
        body = response.content.decode()
        self.assertNotIn('href="/videos/upload/"', body)
        self.assertIn("htmx.org", body)

    def test_operation_detail_has_no_operation_level_analyze_entry(self):
        with_act = self.client.get(f"/operations/{self.operation.pk}/")
        with_act_body = with_act.content.decode()
        self.assertNotIn("Wgraj nagranie do analizy", with_act_body)
        self.assertNotIn("Analizuj film", with_act_body)
        self.assertNotIn(f"/operations/{self.operation.pk}/videos/upload/", with_act_body)

        empty_op = Operation.objects.create(
            process=self.process, name="Pakowanie", order=2
        )
        empty = self.client.get(f"/operations/{empty_op.pk}/")
        body = empty.content.decode()
        self.assertIn("Najpierw zdefiniuj czynności", body)
        self.assertNotIn("Wgraj nagranie do analizy", body)
        self.assertNotIn(f"/operations/{empty_op.pk}/videos/upload/", body)

    def test_process_detail_keeps_only_process_level_analyze_entry(self):
        response = self.client.get(f"/processes/{self.process.pk}/")
        body = response.content.decode()
        self.assertIn(f"/processes/{self.process.pk}/analyze-video/", body)
        self.assertNotIn(f"/operations/{self.operation.pk}/videos/upload/", body)

    def test_operation_video_upload_redirects_to_process(self):
        response = self.client.get(f"/operations/{self.operation.pk}/videos/upload/")
        self.assertRedirects(response, f"/processes/{self.process.pk}/")

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

    @override_settings(LANGUAGE_CODE="en")
    def test_analysis_status_running_translates_heading(self):
        video = self._make_video_with_analysis(Analysis.Status.RUNNING)
        r = self.client.get(
            f"/videos/{video.pk}/analysis-status/",
            HTTP_ACCEPT_LANGUAGE="en",
        )
        body = r.content.decode()
        self.assertIn("Analysis in progress", body)
        self.assertIn("The result will appear here automatically", body)
        self.assertNotIn("Analiza w toku", body)

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
            r = self.client.post(
                f"/videos/{video.pk}/approve-and-analyze/",
                {"analysis_model_name": "gemini-3.1-pro-preview"},
            )
            bg.assert_called_once()
        video.refresh_from_db()
        self.assertEqual(video.status, Video.Status.ANALYZING)
        self.assertIsNotNone(video.approved_for_analysis_at)
        self.assertEqual(video.analysis_model_name, "gemini-3.1-pro-preview")
        self.assertRedirects(r, f"/videos/{video.pk}/review/")

    def test_video_reanonymize_starts_background_and_shows_polling(self):
        video = self._video_with_file()
        video.anonymized_file.save("old.mp4", SimpleUploadedFile("old.mp4", b"old"), save=False)
        video.status = Video.Status.COMPLETED
        video.approved_for_analysis_at = timezone.now()
        video.save(update_fields=["anonymized_file", "status", "approved_for_analysis_at"])
        Analysis.objects.create(video=video, status=Analysis.Status.COMPLETED)

        with patch("processes.views.run_anonymization_in_background") as anonymize:
            response = self.client.post(f"/videos/{video.pk}/reanonymize/")

        anonymize.assert_called_once()
        self.assertRedirects(response, f"/videos/{video.pk}/review/")

        video.status = Video.Status.ANONYMIZING
        video.anonymization_error = ""
        video.save(update_fields=["status", "anonymization_error"])
        video.refresh_from_db()

        review = self.client.get(f"/videos/{video.pk}/review/")
        body = review.content.decode()
        self.assertIn("Anonimizacja w toku", body)
        self.assertIn('hx-trigger="load, every 3s"', body)
        self.assertIn("Uruchom ponownie z oryginału", body)
        self.assertNotIn("Zatwierdź i rozpocznij analizę Gemini", body)

    def test_video_reanonymize_get_redirects_to_review(self):
        video = self._video_with_file()
        response = self.client.get(f"/videos/{video.pk}/reanonymize/")
        self.assertRedirects(response, f"/videos/{video.pk}/review/")

    def test_anonymization_status_refreshes_review_after_completion(self):
        video = Video.objects.create(
            operation=self.operation,
            original_filename="demo.mp4",
            duration_seconds=Decimal("12.00"),
            status=Video.Status.AWAITING_APPROVAL,
            anonymized_file=SimpleUploadedFile("a.mp4", b"x"),
        )
        response = self.client.get(f"/videos/{video.pk}/anonymization-status/")
        self.assertEqual(response.headers["HX-Refresh"], "true")
        self.assertIn("Anonimizacja zakończona", response.content.decode())

    def test_anonymization_status_shows_progress(self):
        video = Video.objects.create(
            operation=self.operation,
            original_filename="demo.mp4",
            duration_seconds=Decimal("12.00"),
            status=Video.Status.ANONYMIZING,
            anonymization_progress_percent=37,
            anonymization_progress_current=370,
            anonymization_progress_total=1000,
            anonymization_progress_label="Wykrywanie i maskowanie twarzy",
            anonymization_progress_updated_at=timezone.now(),
        )
        response = self.client.get(f"/videos/{video.pk}/anonymization-status/")
        body = response.content.decode()
        self.assertIn("37%", body)
        self.assertIn("370/1000 klatek", body)
        self.assertIn("Wykrywanie i maskowanie twarzy", body)
        self.assertNotIn("HX-Refresh", response.headers)

    def test_stale_anonymization_is_marked_failed(self):
        video = Video.objects.create(
            operation=self.operation,
            original_filename="demo.mp4",
            duration_seconds=Decimal("12.00"),
            status=Video.Status.ANONYMIZING,
            anonymization_progress_percent=42,
            anonymization_progress_updated_at=timezone.now() - timedelta(minutes=6),
        )
        response = self.client.get(f"/videos/{video.pk}/anonymization-status/")
        video.refresh_from_db()
        self.assertEqual(video.status, Video.Status.FAILED)
        self.assertIn("przerwana", video.anonymization_error)
        self.assertEqual(response.headers["HX-Refresh"], "true")

    def test_video_review_does_not_show_awaiting_approval_badge(self):
        video = Video.objects.create(
            operation=self.operation,
            original_filename="demo.mp4",
            duration_seconds=Decimal("12.00"),
            status=Video.Status.AWAITING_APPROVAL,
            anonymized_file=SimpleUploadedFile("a.mp4", b"x"),
        )
        response = self.client.get(f"/videos/{video.pk}/review/")
        body = response.content.decode()
        self.assertNotIn("Do zatwierdzenia", body)
        self.assertIn("Analiza AI", body)

    def test_video_review_completed_allows_rerun_with_model_choice(self):
        video = Video.objects.create(
            operation=self.operation,
            original_filename="demo.mp4",
            duration_seconds=Decimal("12.00"),
            status=Video.Status.COMPLETED,
            anonymized_file=SimpleUploadedFile("a.mp4", b"x"),
            analysis_model_name="gemini-3.1-pro-preview",
        )
        Analysis.objects.create(video=video, status=Analysis.Status.COMPLETED)

        response = self.client.get(f"/videos/{video.pk}/review/")
        body = response.content.decode()

        self.assertIn("Uruchom nową analizę", body)
        self.assertIn('name="analysis_model_name"', body)
        self.assertIn('value="gemini-3.1-pro-preview" selected', body)
        self.assertIn('value="gemini-3.5-flash"', body)
        self.assertNotIn('value="gemini-2.5-flash"', body)
        self.assertNotIn('value="gemini-2.5-pro"', body)

    @override_settings(
        GEMINI_USE_MOCK=False,
        GEMINI_API_KEY="real-key-in-test",
        GEMINI_FALLBACK_TO_MOCK=True,
    )
    def test_real_gemini_parse_error_does_not_silently_use_demo_segments(self):
        from processes.services import run_video_analysis

        video = Video.objects.create(
            operation=self.operation,
            original_filename="demo.mp4",
            duration_seconds=Decimal("12.00"),
            anonymized_file=SimpleUploadedFile("a.mp4", b"x"),
            approved_for_analysis_at=timezone.now(),
        )

        with patch(
            "processes.services._analyze_with_gemini",
            return_value=("not json", {"input_tokens": 1, "output_tokens": 1}),
        ):
            analysis = run_video_analysis(video)

        analysis.refresh_from_db()
        video.refresh_from_db()
        self.assertEqual(analysis.status, Analysis.Status.FAILED)
        self.assertEqual(video.status, Video.Status.FAILED)
        self.assertEqual(analysis.segments.count(), 0)
        self.assertEqual(analysis.raw_response, "not json")
        self.assertNotIn("Segment demo", analysis.raw_response)

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
        self.assertEqual(hint.activity, self.machine)
        self.assertEqual(hint.confused_with, self.load)
        self.assertIn("pieprz jest ciemniejszy", hint.text)
        self.assertIn("uwzględni", r.content.decode())
        prompt = build_analysis_prompt(self.operation)
        self.assertIn("pieprz jest ciemniejszy", prompt)
        self.assertIn("bywa mylone z: załadunek detalu", prompt)

    def test_segment_feedback_for_process_video_is_used_in_future_prompts(self):
        from processes.models import ActivityHint
        from processes.services import build_multi_operation_prompt

        video = Video.objects.create(
            process=self.process,
            original_filename="process.mp4",
            duration_seconds=Decimal("20.00"),
        )
        video.operations.set([self.operation])
        analysis = Analysis.objects.create(video=video, status=Analysis.Status.COMPLETED)
        seg = AnalysisSegment.objects.create(
            analysis=analysis,
            activity=self.load,
            activity_name=self.load.name,
            start_seconds=Decimal("12"),
            end_seconds=Decimal("20"),
            confidence=0.4,
        )

        note = "At the end the driver stops the car and moves his hands away"
        r = self.client.post(
            f"/analyses/{analysis.pk}/segments/{seg.pk}/feedback/",
            {"note": note, "confused_with": self.machine.pk},
        )

        self.assertEqual(r.status_code, 200)
        hint = ActivityHint.objects.get(source_segment=seg)
        self.assertEqual(hint.activity, self.machine)
        self.assertEqual(hint.confused_with, self.load)
        prompt = build_multi_operation_prompt(self.process, [self.operation])
        self.assertIn(note, prompt)
        self.assertIn("bywa mylone z: załadunek detalu", prompt)

    @override_settings(LANGUAGE_CODE="en")
    def test_segment_feedback_controls_translate_to_english(self):
        analysis, _seg = self._segment_for_feedback()
        response = self.client.get(
            f"/analyses/{analysis.pk}/",
            HTTP_ACCEPT_LANGUAGE="en",
        )
        body = response.content.decode()
        # Przyciski oceny to teraz ikony kciuka z etykietami dostępności (bez tekstu Good/Correct).
        self.assertIn("Confirm segment", body)
        self.assertIn("Edit segment", body)
        self.assertIn("Save note for AI", body)
        self.assertIn("e.g. pepper is darker than salt", body)
        self.assertNotIn("Dobrze", body)
        self.assertNotIn(">Popraw<", body)

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

    # --- Import / klonowanie operacji ---

    def test_clone_operation_duplicates_with_activities(self):
        from processes.services import clone_operation
        target = Process.objects.create(name="Nowy proces")
        new_op = clone_operation(self.operation, target)
        self.assertEqual(new_op.process, target)
        self.assertEqual(new_op.activities.count(), self.operation.activities.count())
        self.assertEqual(self.operation.process, self.process)
        a = new_op.activities.first()
        a.name = "ZMIENIONE"
        a.save()
        self.assertFalse(self.operation.activities.filter(name="ZMIENIONE").exists())

    def test_clone_operation_dedupes_name(self):
        from processes.services import clone_operation
        new_op = clone_operation(self.operation, self.process)
        self.assertEqual(new_op.name, "Frezowanie (2)")
        new_op2 = clone_operation(self.operation, self.process)
        self.assertEqual(new_op2.name, "Frezowanie (3)")

    def test_operation_import_view_clones(self):
        target = Process.objects.create(name="Docelowy")
        r = self.client.post(
            f"/processes/{target.pk}/operations/import/",
            {"operations": [self.operation.pk]},
        )
        self.assertEqual(r.status_code, 302)
        self.assertTrue(target.operations.filter(name="Frezowanie").exists())
        self.assertEqual(
            target.operations.get(name="Frezowanie").activities.count(),
            self.operation.activities.count(),
        )

    def test_process_video_upload_creates_video_with_operations(self):
        op2 = Operation.objects.create(process=self.process, name="Malowanie", order=2)
        Activity.objects.create(operation=op2, name="nakładanie farby", performed_by=Activity.Performer.OPERATOR)
        upload = SimpleUploadedFile("clip.mp4", b"data", content_type="video/mp4")
        with (
            patch("processes.views.get_video_duration_seconds", side_effect=Exception("no ffprobe")),
            patch("processes.views.run_anonymization_in_background") as anonymize,
        ):
            r = self.client.post(
                f"/processes/{self.process.pk}/analyze-video/",
                {
                    "operations": [self.operation.pk, op2.pk],
                    "file": upload,
                    "analysis_model_name": "gemini-3.1-pro-preview",
                },
            )
        self.assertEqual(r.status_code, 302)
        anonymize.assert_called_once()
        video = Video.objects.filter(original_filename="clip.mp4").first()
        self.assertIsNotNone(video)
        self.assertEqual(video.process, self.process)
        self.assertEqual(video.analysis_model_name, "gemini-3.1-pro-preview")
        self.assertEqual(
            set(video.operations.values_list("pk", flat=True)),
            {self.operation.pk, op2.pk},
        )

    @override_settings(GEMINI_USE_MOCK=True)
    def test_run_multi_operation_analysis_assigns_operations(self):
        from processes.services import run_video_analysis
        op2 = Operation.objects.create(process=self.process, name="Malowanie", order=2)
        Activity.objects.create(operation=op2, name="nakładanie farby", performed_by=Activity.Performer.OPERATOR)
        video = Video.objects.create(
            process=self.process, original_filename="m.mp4",
            duration_seconds=Decimal("30.00"), approved_for_analysis_at=timezone.now(),
            anonymized_file=SimpleUploadedFile("a.mp4", b"x"),
        )
        video.operations.set([self.operation, op2])
        analysis = run_video_analysis(video)
        self.assertEqual(analysis.status, Analysis.Status.COMPLETED)
        op_names = set(analysis.segments.values_list("operation__name", flat=True))
        self.assertIn("Frezowanie", op_names)
        self.assertIn("Malowanie", op_names)
        # segmenty różnych operacji mogą się nakładać w czasie (równoległe lane'y)
        frez0 = analysis.segments.filter(operation=self.operation).order_by("start_seconds").first()
        mal0 = analysis.segments.filter(operation=op2).order_by("start_seconds").first()
        self.assertEqual(frez0.start_seconds, mal0.start_seconds)

    @override_settings(
        GEMINI_USE_MOCK=False,
        GEMINI_API_KEY="real-key-in-test",
        GEMINI_FALLBACK_TO_MOCK=False,
    )
    def test_run_video_analysis_uses_model_selected_on_video(self):
        from processes.services import run_video_analysis

        video = Video.objects.create(
            operation=self.operation,
            original_filename="model.mp4",
            duration_seconds=Decimal("12.00"),
            approved_for_analysis_at=timezone.now(),
            anonymized_file=SimpleUploadedFile("a.mp4", b"x"),
            analysis_model_name="gemini-3.1-pro-preview",
        )
        raw = (
            '{"segments":[{"start_seconds":0,"end_seconds":12,'
            f'"activity":"{self.load.name}","confidence":0.9,'
            '"evidence":["operator loads part"],"missing_evidence":[],'
            '"alternative_activity":null,"reason":"visible loading",'
            '"confidence_reason":"clear evidence"}]}'
        )

        with patch(
            "processes.services._analyze_with_gemini",
            return_value=(raw, {"input_tokens": 10, "output_tokens": 5}),
        ) as analyze:
            analysis = run_video_analysis(video)

        self.assertEqual(analysis.status, Analysis.Status.COMPLETED)
        self.assertEqual(analysis.model_name, "gemini-3.1-pro-preview")
        analyze.assert_called_once()
        self.assertEqual(analyze.call_args.kwargs["model_name"], "gemini-3.1-pro-preview")

    def test_multi_operation_prompt_structure(self):
        from processes.services import build_multi_operation_prompt
        op2 = Operation.objects.create(process=self.process, name="Malowanie ścian", order=2)
        Activity.objects.create(operation=op2, name="nakładanie farby", performed_by=Activity.Performer.OPERATOR)
        prompt = build_multi_operation_prompt(self.process, [self.operation, op2])
        self.assertIn("Frezowanie", prompt)
        self.assertIn("Malowanie ścian", prompt)
        self.assertIn('"operation"', prompt)
        self.assertIn("Najpierw rozpoznaj OPERACJĘ", prompt)
        self.assertIn("RÓWNOLEGLE", prompt)
        self.assertIn("NIE jest 5", prompt)

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

    def test_analysis_detail_shows_approved_count(self):
        analysis = self._analysis_with_two_segments()
        r = self.client.get(f"/analyses/{analysis.pk}/")
        body = r.content.decode()
        # Eksport CSV jest zakomentowany, a "Zatwierdź całość" usunięty z UI.
        self.assertNotIn(f"/analyses/{analysis.pk}/approve-all/", body)
        self.assertIn("Zatwierdzone", body)
        self.assertIn("0/2", body)
