import json
import re
import shutil
import subprocess
import threading
import time
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.db import connection, transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.text import slugify

from .models import Activity, Analysis, AnalysisSegment, Operation, Video


def quantize_seconds(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _css_percent(value):
    return f"{float(value):.4f}%"


def get_video_duration_seconds(file_path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(file_path),
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    return quantize_seconds(float(result.stdout.strip()))


def _safe_output_path(video, prefix):
    original_path = Path(video.file.path)
    safe_stem = slugify(original_path.stem) or "video"
    output_dir = settings.MEDIA_ROOT / "anonymized" / str(video.pk)
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / f"{prefix}_{safe_stem}.mp4"


def _full_frame_blur(input_path, output_path):
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-vf",
            "boxblur=18:2",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            "-c:a",
            "copy",
            str(output_path),
        ],
        capture_output=True,
        check=True,
        text=True,
    )


def _copy_video(input_path, output_path):
    shutil.copyfile(input_path, output_path)


def _opencv_face_blur(input_path, output_path):
    import cv2

    cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    face_cascade = cv2.CascadeClassifier(str(cascade_path))
    capture = cv2.VideoCapture(str(input_path))
    if not capture.isOpened():
        raise RuntimeError("Nie można otworzyć pliku wideo do anonimizacji.")

    fps = capture.get(cv2.CAP_PROP_FPS) or 25
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    silent_path = output_path.with_name(f"{output_path.stem}_silent.mp4")
    writer = cv2.VideoWriter(
        str(silent_path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
    )
    blurred_faces = 0

    while True:
        ok, frame = capture.read()
        if not ok:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30))
        for (x, y, w, h) in faces:
            padding_x = int(w * 0.18)
            padding_y = int(h * 0.22)
            x1 = max(0, x - padding_x)
            y1 = max(0, y - padding_y)
            x2 = min(width, x + w + padding_x)
            y2 = min(height, y + h + padding_y)
            face_region = frame[y1:y2, x1:x2]
            if face_region.size:
                blur = cv2.GaussianBlur(face_region, (99, 99), 30)
                frame[y1:y2, x1:x2] = blur
                blurred_faces += 1
        writer.write(frame)

    capture.release()
    writer.release()

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(silent_path),
            "-i",
            str(input_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-shortest",
            str(output_path),
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    silent_path.unlink(missing_ok=True)
    return blurred_faces


def anonymize_video(video):
    video.status = Video.Status.ANONYMIZING
    video.anonymization_error = ""
    video.save(update_fields=["status", "anonymization_error"])

    input_path = Path(video.file.path)
    output_path = _safe_output_path(video, "anon")

    try:
        try:
            blurred_faces = _opencv_face_blur(input_path, output_path)
            if blurred_faces:
                mode = f"opencv_face_blur ({blurred_faces} wykryć twarzy)"
            else:
                mode = "opencv_face_blur_no_faces_detected"
        except ImportError:
            _copy_video(input_path, output_path)
            mode = "copy_without_full_blur_no_opencv"
        except Exception as exc:
            if output_path.exists():
                output_path.unlink()
            _copy_video(input_path, output_path)
            mode = f"copy_without_full_blur_fallback ({exc})"

        with output_path.open("rb") as handle:
            video.anonymized_file.save(output_path.name, File(handle), save=False)
        output_path.unlink(missing_ok=True)

        video.status = Video.Status.AWAITING_APPROVAL
        video.anonymized_at = timezone.now()
        video.approved_for_analysis_at = None
        video.anonymization_error = f"Tryb anonimizacji: {mode}. Sprawdź podgląd przed zatwierdzeniem analizy."
        video.save(
            update_fields=[
                "anonymized_file",
                "status",
                "anonymized_at",
                "approved_for_analysis_at",
                "anonymization_error",
            ]
        )
        return video
    except Exception as exc:
        video.status = Video.Status.FAILED
        video.anonymization_error = str(exc)
        video.save(update_fields=["status", "anonymization_error"])
        raise


def _dedupe_operation_name(process, base_name):
    existing = set(process.operations.values_list("name", flat=True))
    if base_name not in existing:
        return base_name
    index = 2
    while f"{base_name} ({index})" in existing:
        index += 1
    return f"{base_name} ({index})"


@transaction.atomic
def clone_operation(source, target_process):
    """Tworzy duplikat operacji (wraz z czynnościami) w docelowym procesie.
    Źródło pozostaje nietknięte; nazwa jest odróżniona, np. 'Malowanie (2)'."""
    new_name = _dedupe_operation_name(target_process, source.name)
    next_order = (target_process.operations.aggregate(m=Max("order"))["m"] or 0) + 1
    new_operation = Operation.objects.create(
        process=target_process,
        name=new_name,
        description=source.description,
        order=next_order,
    )
    for activity in source.activities.all():
        Activity.objects.create(
            operation=new_operation,
            name=activity.name,
            description=activity.description,
            recognition_rules=activity.recognition_rules,
            exclusion_rules=activity.exclusion_rules,
            performed_by=activity.performed_by,
            minimum_duration_seconds=activity.minimum_duration_seconds,
            order=activity.order,
        )
    return new_operation


def build_analysis_prompt(operation):
    activities = list(operation.activities.all())
    lines = [
        f'Analizujesz zanonimizowane nagranie operacji "{operation.name}" w procesie "{operation.process.name}".',
        "",
        "Przypisz każdy fragment nagrania wyłącznie do jednej z poniższych czynności.",
        "Nie twórz nowych nazw czynności. Nie zgaduj.",
        'Gdy nie ma wystarczających informacji, wybierz czynność "niepewne", jeśli jest dostępna.',
        "Zwróć wyłącznie poprawny JSON zgodny ze schematem:",
        '{"segments":[{"start_seconds":0.0,"end_seconds":1.0,"activity":"nazwa","confidence":0.8,"reason":"uzasadnienie"}]}',
        "",
        "Kontekst procesu:",
        f"Nazwa procesu: {operation.process.name}",
        f"Opis procesu: {operation.process.description or 'brak'}",
        f"Nazwa operacji: {operation.name}",
        f"Opis operacji: {operation.description or 'brak'}",
        "",
        "Dozwolone czynności:",
    ]
    for index, activity in enumerate(activities, start=1):
        minimum = (
            f"{activity.minimum_duration_seconds}s"
            if activity.minimum_duration_seconds is not None
            else "brak"
        )
        activity_lines = [
            f"{index}. {activity.name}",
            f"Opis: {activity.description or 'brak'}",
            f"Rozpoznaj, gdy: {activity.recognition_rules or 'brak'}",
            f"Nie rozpoznawaj, gdy: {activity.exclusion_rules or 'brak'}",
            f"Minimalny czas trwania: {minimum}",
            f"Wykonawca: {activity.get_performed_by_display()}",
        ]
        active_hints = list(activity.hints.filter(is_active=True))
        if active_hints:
            activity_lines.append("Wskazówki z wcześniejszych korekt:")
            for hint in active_hints:
                suffix = f" (bywa mylone z: {hint.confused_with.name})" if hint.confused_with else ""
                activity_lines.append(f"- {hint.text}{suffix}")
        activity_lines.append("")
        lines.extend(activity_lines)
    if len(activities) > 1:
        sequence = " → ".join(activity.name for activity in activities)
        lines.extend(
            [
                f"Typowa kolejność czynności (podpowiedź, nie sztywna reguła): {sequence}.",
                "Kolejność może się nie zachować — dozwolone są odstępstwa: czekanie, chodzenie, poprawki, powtórzenia lub pominięcia kroków.",
                "",
            ]
        )
    lines.extend(
        [
            "Zasady segmentacji:",
            "- segmenty nie mogą nachodzić na siebie,",
            "- start_seconds i end_seconds podawaj w sekundach od początku nagrania,",
            "- confidence ma być liczbą od 0 do 1,",
            "- reason ma krótko wyjaśniać, co widać lub słychać,",
            "- odpowiedź ma zawierać tylko JSON, bez komentarzy i markdown.",
        ]
    )
    return "\n".join(lines)


def _render_activity_block(index, activity):
    minimum = (
        f"{activity.minimum_duration_seconds}s"
        if activity.minimum_duration_seconds is not None
        else "brak"
    )
    block = [
        f"  {index}. {activity.name}",
        f"     Opis: {activity.description or 'brak'}",
        f"     Rozpoznaj, gdy: {activity.recognition_rules or 'brak'}",
        f"     Nie rozpoznawaj, gdy: {activity.exclusion_rules or 'brak'}",
        f"     Minimalny czas trwania: {minimum}",
        f"     Wykonawca: {activity.get_performed_by_display()}",
    ]
    active_hints = list(activity.hints.filter(is_active=True))
    if active_hints:
        block.append("     Wskazówki z wcześniejszych korekt:")
        for hint in active_hints:
            suffix = f" (bywa mylone z: {hint.confused_with.name})" if hint.confused_with else ""
            block.append(f"     - {hint.text}{suffix}")
    return block


def build_multi_operation_prompt(process, operations):
    """Prompt do analizy nagrania procesu, na którym może równolegle występować
    wiele operacji (różni pracownicy / różne stanowiska)."""
    lines = [
        f'Analizujesz zanonimizowane nagranie procesu "{process.name}".',
        "Na nagraniu RÓWNOLEGLE mogą występować różne operacje wykonywane przez różnych pracowników lub na różnych stanowiskach.",
        "",
        "ZADANIE: podziel nagranie na segmenty. Dla każdego segmentu podaj operację, czynność w tej operacji, czas trwania, pewność i krótkie uzasadnienie.",
        "",
        "ZASADY NADRZĘDNE:",
        "1. Najpierw rozpoznaj OPERACJĘ (po stanowisku, strefie kadru i charakterze pracy), dopiero potem CZYNNOŚĆ w obrębie tej operacji.",
        "2. Używaj wyłącznie nazw operacji i czynności z list poniżej. Nie twórz nowych nazw. Nie zgaduj.",
        "3. Operacje mogą dziać się RÓWNOLEGLE — segmenty różnych operacji MOGĄ nakładać się w czasie. Segmenty TEJ SAMEJ operacji nie nakładają się.",
        "4. Kolejność czynności w operacji to PODPOWIEDŹ, nie sztywna reguła. Realna praca bywa nieliniowa, na przykład:",
        "   - możesz wykonać czynność 2, a potem wrócić do czynności 1 (poprawka),",
        "   - możesz być w czynności 4, a następną NIE jest 5, bo trzeba poprawić coś z czynności 1,",
        "   - czynności mogą się powtarzać, być pomijane lub przeplatane.",
        "   Kieruj się tym, co realnie widać, a nie zakładaną sekwencją.",
        '5. Gdy nie możesz pewnie określić operacji lub czynności — wybierz czynność "niepewne" (jeśli dostępna) zamiast zgadywać i obniż confidence.',
        "6. Przerwy, brak pracy i czekanie oznaczaj odpowiednią czynnością, jeśli jest zdefiniowana.",
        "",
        "Zwróć wyłącznie poprawny JSON zgodny ze schematem:",
        '{"segments":[{"operation":"nazwa operacji","activity":"nazwa czynności","start_seconds":0.0,"end_seconds":1.0,"confidence":0.8,"reason":"uzasadnienie"}]}',
        "",
        "KONTEKST PROCESU:",
        f"Nazwa procesu: {process.name}",
        f"Opis procesu: {process.description or 'brak'}",
        "",
        "OPERACJE I ICH CZYNNOŚCI (zamknięte listy):",
    ]
    for op_index, operation in enumerate(operations, start=1):
        activities = list(operation.activities.all())
        lines.append("")
        lines.append(f"== Operacja {op_index}: {operation.name} ==")
        lines.append(f"   Opis operacji: {operation.description or 'brak'}")
        if len(activities) > 1:
            sequence = " → ".join(activity.name for activity in activities)
            lines.append(
                f"   Typowa kolejność czynności (podpowiedź, nie sztywna reguła): {sequence}."
            )
            lines.append(
                "   Dozwolone odstępstwa: powroty do wcześniejszych kroków, poprawki, powtórzenia, pominięcia."
            )
        lines.append("   Czynności:")
        for act_index, activity in enumerate(activities, start=1):
            lines.extend(_render_activity_block(act_index, activity))
    lines.extend(
        [
            "",
            "ZASADY SEGMENTACJI:",
            "- start_seconds i end_seconds podawaj w sekundach od początku nagrania,",
            "- confidence ma być liczbą od 0 do 1,",
            "- reason ma krótko wyjaśniać, co widać lub słychać oraz po czym poznajesz operację,",
            "- odpowiedź ma zawierać tylko JSON, bez komentarzy i markdown.",
        ]
    )
    return "\n".join(lines)


def build_analysis_prompt_for_video(video):
    """Wybiera właściwy prompt: jedno-operacyjny lub multi-operacyjny."""
    operations = video.analysis_operations()
    if len(operations) > 1:
        return build_multi_operation_prompt(video.analysis_process(), operations)
    if operations:
        return build_analysis_prompt(operations[0])
    raise ValueError("Wideo nie ma przypisanej operacji do analizy.")


def _extract_json(text):
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped).strip()
        stripped = re.sub(r"```$", "", stripped).strip()
    match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if not match:
        raise ValueError("Odpowiedź modelu nie zawiera obiektu JSON.")
    return json.loads(match.group(0))


def _activity_lookup(operation):
    activities = list(operation.activities.all())
    lookup = {activity.name.casefold(): activity for activity in activities}
    uncertain = next((a for a in activities if "niepew" in a.name.casefold()), None)
    return lookup, uncertain


def _normalize_segments(payload, operation, duration_seconds=None):
    if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
        raise ValueError("JSON musi zawierać listę segments.")

    lookup, uncertain = _activity_lookup(operation)
    normalized = []
    last_end = Decimal("0")
    max_duration = quantize_seconds(duration_seconds) if duration_seconds else None

    for item in payload["segments"]:
        activity_name = str(item.get("activity", "")).strip()
        activity = lookup.get(activity_name.casefold())
        if activity is None and uncertain is not None:
            activity = uncertain
            activity_name = uncertain.name
        start = quantize_seconds(item.get("start_seconds", 0))
        end = quantize_seconds(item.get("end_seconds", 0))
        if start < last_end:
            start = last_end
        if max_duration is not None:
            start = min(start, max_duration)
            end = min(end, max_duration)
        if end <= start:
            continue
        confidence = float(item.get("confidence", 0))
        normalized.append(
            {
                "activity": activity,
                "activity_name": activity.name if activity else activity_name,
                "start_seconds": start,
                "end_seconds": end,
                "confidence": max(0.0, min(1.0, confidence)),
                "reason": str(item.get("reason", ""))[:2000],
            }
        )
        last_end = end

    if not normalized:
        raise ValueError("Model nie zwrócił poprawnych segmentów.")
    return normalized


def _multi_activity_lookup(operations):
    by_op = {}
    for operation in operations:
        activities = list(operation.activities.all())
        lookup = {activity.name.casefold(): activity for activity in activities}
        uncertain = next((a for a in activities if "niepew" in a.name.casefold()), None)
        by_op[operation.name.casefold()] = {
            "operation": operation,
            "lookup": lookup,
            "uncertain": uncertain,
        }
    return by_op


def _normalize_multi_segments(payload, operations, duration_seconds=None):
    if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
        raise ValueError("JSON musi zawierać listę segments.")

    by_op = _multi_activity_lookup(operations)
    op_keys = list(by_op.keys())
    normalized = []
    last_end_by_op = defaultdict(lambda: Decimal("0"))
    max_duration = quantize_seconds(duration_seconds) if duration_seconds else None

    for item in payload["segments"]:
        op_name = str(item.get("operation", "")).strip()
        entry = by_op.get(op_name.casefold())
        if entry is None and len(op_keys) == 1:
            entry = by_op[op_keys[0]]
        if entry is None:
            continue
        operation = entry["operation"]
        activity_name = str(item.get("activity", "")).strip()
        activity = entry["lookup"].get(activity_name.casefold())
        if activity is None and entry["uncertain"] is not None:
            activity = entry["uncertain"]
            activity_name = entry["uncertain"].name
        start = quantize_seconds(item.get("start_seconds", 0))
        end = quantize_seconds(item.get("end_seconds", 0))
        # Brak nakładania tylko w obrębie tej samej operacji; różne operacje równolegle.
        if start < last_end_by_op[operation.pk]:
            start = last_end_by_op[operation.pk]
        if max_duration is not None:
            start = min(start, max_duration)
            end = min(end, max_duration)
        if end <= start:
            continue
        confidence = float(item.get("confidence", 0))
        normalized.append(
            {
                "operation": operation,
                "operation_name": operation.name,
                "activity": activity,
                "activity_name": activity.name if activity else activity_name,
                "start_seconds": start,
                "end_seconds": end,
                "confidence": max(0.0, min(1.0, confidence)),
                "reason": str(item.get("reason", ""))[:2000],
            }
        )
        last_end_by_op[operation.pk] = end

    if not normalized:
        raise ValueError("Model nie zwrócił poprawnych segmentów.")
    return normalized


def _mock_multi_segments(operations, duration_seconds):
    total = quantize_seconds(duration_seconds or 60)
    if total <= 0:
        total = Decimal("60.00")
    segments = []
    for op_index, operation in enumerate(operations):
        activities = list(operation.activities.all())
        if not activities:
            continue
        count = min(len(activities), 3 if total < Decimal("12") else 4)
        chosen = activities[:count]
        seg_len = (total / Decimal(len(chosen))).quantize(Decimal("0.01"))
        start = Decimal("0.00")
        for index, activity in enumerate(chosen):
            end = total if index == len(chosen) - 1 else start + seg_len
            segments.append(
                {
                    "operation": operation.name,
                    "activity": activity.name,
                    "start_seconds": float(start),
                    "end_seconds": float(end),
                    "confidence": max(0.5, 0.88 - index * 0.05 - op_index * 0.03),
                    "reason": f"Segment demo: operacja {operation.name}, czynność {activity.name}.",
                }
            )
            start = end
    if not segments:
        raise ValueError("Wybrane operacje nie mają zdefiniowanych czynności.")
    return {"segments": segments}


def _gemini_client():
    if settings.GEMINI_USE_MOCK or not settings.GEMINI_API_KEY:
        return None
    from google import genai

    return genai.Client(api_key=settings.GEMINI_API_KEY)


def _openai_client():
    if not settings.OPENAI_API_KEY:
        return None
    from openai import OpenAI

    return OpenAI(api_key=settings.OPENAI_API_KEY)


def _assist_mock(fields, mode, target):
    name = fields.get("name") or "czynność"
    base = fields.get("description") or fields.get("quick_description") or f"{name} obserwowana w nagraniu."
    if mode == "refine" and fields.get("description"):
        description = f"{fields['description']} (doprecyzowano: widoczny początek i koniec czynności)."
    else:
        description = f"{base} Opis doprecyzowany na potrzeby segmentacji wideo."
    full = {
        "description": description,
        "recognition_rules": "- widoczny jest początek i koniec czynności\n- działania odpowiadają nazwie czynności\n- obiekt lub maszyna w oczekiwanym kontekście",
        "exclusion_rules": "- operator wykonuje inną zdefiniowaną czynność\n- obraz nie pozwala potwierdzić działania\n- widoczny jest tylko etap przygotowania lub zakończenia",
        "possible_confusions": "- inne\n- niepewne",
    }
    if target:
        return {target: full.get(target, "")}
    return full


def assist_activity(operation, fields, mode="generate", target=None):
    """Asystent opisu czynności (OpenAI GPT).

    fields: dict z kluczami name/quick_description/description/recognition_rules/exclusion_rules.
    mode: 'generate' (od zera) | 'refine' (szlifowanie istniejącej treści).
    target: jeśli podany (np. 'exclusion_rules'), zwraca tylko to jedno pole.
    """
    client = _openai_client()
    if client is None:
        if settings.OPENAI_USE_MOCK:
            return _assist_mock(fields, mode, target)
        raise RuntimeError(
            "Brak OPENAI_API_KEY. Ustaw klucz w pliku .env albo włącz OPENAI_USE_MOCK=true dla trybu demo."
        )

    intent = (
        "Popraw i doszlifuj istniejący opis, zachowując intencję autora."
        if mode == "refine"
        else "Przygotuj opis od zera."
    )
    scope = f"Zwróć wyłącznie pole '{target}'." if target else "Zwróć wszystkie pola."
    schema = (
        '{"%s": "..."}' % target
        if target
        else '{"description":"...","recognition_rules":"- ...","exclusion_rules":"- ...","possible_confusions":"- ..."}'
    )
    prompt = f"""
Jesteś asystentem inżyniera procesu. {intent}
Opis służy do analizy wideo produkcji gniazdowej. Pisz precyzyjnie i konkretnie.

Proces: {operation.process.name}
Operacja: {operation.name}
Nazwa czynności: {fields.get('name') or 'do uzupełnienia'}
Krótki opis: {fields.get('quick_description') or 'brak'}
Obecny opis: {fields.get('description') or 'brak'}
Obecne warunki rozpoznania: {fields.get('recognition_rules') or 'brak'}
Obecne warunki wykluczenia: {fields.get('exclusion_rules') or 'brak'}

{scope}
Zwróć wyłącznie JSON: {schema}
"""
    response = client.chat.completions.create(
        model=settings.OPENAI_TEXT_MODEL,
        messages=[
            {"role": "system", "content": "Zwracasz wyłącznie poprawny JSON, bez markdown."},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content)


def suggest_activity_description(operation, name, quick_description):
    """Zgodność wsteczna: generowanie opisu od zera."""
    return assist_activity(
        operation,
        {"name": name, "quick_description": quick_description},
        mode="generate",
    )


def _mock_segments(operation, duration_seconds):
    activities = list(operation.activities.all())
    if not activities:
        raise ValueError("Operacja nie ma zdefiniowanych czynności.")

    total = quantize_seconds(duration_seconds or 60)
    if total <= 0:
        total = Decimal("60.00")

    preferred_order = [
        "podejście do maszyny",
        "otwarcie maszyny",
        "załadunek detalu",
        "zamknięcie maszyny",
        "uruchomienie maszyny",
        "praca maszyny",
        "oczekiwanie operatora",
        "otwarcie maszyny po zakończeniu",
        "rozładunek detalu",
        "kontrola detalu",
        "niepewne",
    ]
    by_name = {activity.name.casefold(): activity for activity in activities}
    ordered = [by_name[name] for name in preferred_order if name in by_name]
    ordered.extend(activity for activity in activities if activity not in ordered)
    segment_count = 3 if total < Decimal("12") else 6
    selected = ordered[: min(len(ordered), segment_count)]
    segment_length = (total / Decimal(len(selected))).quantize(Decimal("0.01"))
    segments = []
    start = Decimal("0.00")
    for index, activity in enumerate(selected):
        end = total if index == len(selected) - 1 else start + segment_length
        segments.append(
            {
                "start_seconds": float(start),
                "end_seconds": float(end),
                "activity": activity.name,
                "confidence": max(0.52, 0.9 - index * 0.06),
                "reason": f"Segment demo przypisany do czynności: {activity.name}.",
            }
        )
        start = end
    return {"segments": segments}


def _wait_for_uploaded_file(client, uploaded):
    state = getattr(uploaded, "state", None)
    state_name = getattr(state, "name", None)
    while state_name == "PROCESSING":
        time.sleep(2)
        uploaded = client.files.get(name=uploaded.name)
        state = getattr(uploaded, "state", None)
        state_name = getattr(state, "name", None)
    if state_name and state_name != "ACTIVE":
        raise RuntimeError(f"Gemini nie przetworzył pliku. Status: {state_name}")
    return uploaded


def _analyze_with_gemini(video, prompt):
    """Zwraca krotkę (surowy_tekst, usage), gdzie usage to dict z tokenami
    zwróconymi przez API albo None (wtedy koszt jest szacowany z długości wideo)."""
    if not video.approved_for_analysis_at:
        raise RuntimeError("Film musi zostać zatwierdzony po anonimizacji przed analizą.")
    if not video.anonymized_file:
        raise RuntimeError("Brakuje pliku po anonimizacji. Analiza została zablokowana.")

    client = _gemini_client()
    if client is None:
        operations = video.analysis_operations()
        if len(operations) > 1:
            payload = _mock_multi_segments(operations, video.duration_seconds)
        else:
            payload = _mock_segments(operations[0], video.duration_seconds)
        return json.dumps(payload, ensure_ascii=False), None

    uploaded = client.files.upload(file=video.anonymized_file.path)
    uploaded = _wait_for_uploaded_file(client, uploaded)
    response = client.models.generate_content(
        model=settings.GEMINI_VIDEO_MODEL,
        contents=[uploaded, prompt],
    )
    meta = getattr(response, "usage_metadata", None)
    usage = None
    if meta is not None:
        usage = {
            "input_tokens": int(getattr(meta, "prompt_token_count", 0) or 0),
            "output_tokens": int(getattr(meta, "candidates_token_count", 0) or 0),
        }
    return response.text, usage


def _estimate_tokens(video, prompt, raw_response):
    """Szacuje zużycie tokenów, gdy API nie zwróciło realnych liczb (tryb mock).
    Wideo liczone jako ~N tokenów na sekundę, tekst ~4 znaki na token."""
    duration = float(video.duration_seconds or 0)
    video_tokens = int(duration * settings.GEMINI_VIDEO_TOKENS_PER_SECOND)
    prompt_tokens = max(1, len(prompt or "") // 4)
    output_tokens = max(1, len(raw_response or "") // 4)
    return {
        "input_tokens": video_tokens + prompt_tokens,
        "output_tokens": output_tokens,
    }


def estimate_analysis_cost(input_tokens, output_tokens):
    """Szacowany koszt w USD na podstawie cennika z ustawień."""
    input_cost = (
        Decimal(int(input_tokens or 0))
        / Decimal(1_000_000)
        * Decimal(str(settings.GEMINI_PRICE_INPUT_PER_M))
    )
    output_cost = (
        Decimal(int(output_tokens or 0))
        / Decimal(1_000_000)
        * Decimal(str(settings.GEMINI_PRICE_OUTPUT_PER_M))
    )
    return (input_cost + output_cost).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


@transaction.atomic
def persist_segments(analysis, segments):
    analysis.segments.all().delete()
    AnalysisSegment.objects.bulk_create(
        [
            AnalysisSegment(
                analysis=analysis,
                activity=segment["activity"],
                activity_name=segment["activity_name"],
                operation=segment.get("operation"),
                operation_name=segment.get("operation_name", ""),
                start_seconds=segment["start_seconds"],
                end_seconds=segment["end_seconds"],
                confidence=segment["confidence"],
                reason=segment["reason"],
            )
            for segment in segments
        ]
    )


def _segments_from_payload(payload, operations, duration_seconds):
    if len(operations) > 1:
        return _normalize_multi_segments(payload, operations, duration_seconds)
    segments = _normalize_segments(payload, operations[0], duration_seconds)
    for segment in segments:
        segment.setdefault("operation", operations[0])
        segment.setdefault("operation_name", operations[0].name)
    return segments


def run_video_analysis(video):
    operations = video.analysis_operations()
    if not operations:
        raise ValueError("Wideo nie ma przypisanej operacji do analizy.")
    prompt = build_analysis_prompt_for_video(video)
    analysis = Analysis.objects.create(
        video=video,
        status=Analysis.Status.RUNNING,
        model_name=settings.GEMINI_VIDEO_MODEL if not settings.GEMINI_USE_MOCK else "mock",
        prompt=prompt,
        started_at=timezone.now(),
    )
    video.status = Video.Status.ANALYZING
    video.save(update_fields=["status"])

    try:
        raw_response, usage = _analyze_with_gemini(video, prompt)
        try:
            payload = _extract_json(raw_response)
            segments = _segments_from_payload(payload, operations, video.duration_seconds)
        except Exception:
            if not settings.GEMINI_FALLBACK_TO_MOCK:
                raise
            if len(operations) > 1:
                payload = _mock_multi_segments(operations, video.duration_seconds)
            else:
                payload = _mock_segments(operations[0], video.duration_seconds)
            segments = _segments_from_payload(payload, operations, video.duration_seconds)
            raw_response = json.dumps(
                {
                    "fallback_reason": "Nie udało się sparsować odpowiedzi Gemini, użyto segmentów demo.",
                    "original_response": raw_response,
                    "mock": payload,
                },
                ensure_ascii=False,
            )

        # Koszt: realne tokeny z API jeśli dostępne, w przeciwnym razie szacunek.
        cost_is_estimated = usage is None
        if usage is None:
            usage = _estimate_tokens(video, prompt, raw_response)
        input_tokens = usage["input_tokens"]
        output_tokens = usage["output_tokens"]

        persist_segments(analysis, segments)
        analysis.status = Analysis.Status.COMPLETED
        analysis.raw_response = raw_response
        analysis.input_tokens = input_tokens
        analysis.output_tokens = output_tokens
        analysis.estimated_cost = estimate_analysis_cost(input_tokens, output_tokens)
        analysis.cost_is_estimated = cost_is_estimated
        analysis.completed_at = timezone.now()
        analysis.save(
            update_fields=[
                "status",
                "raw_response",
                "input_tokens",
                "output_tokens",
                "estimated_cost",
                "cost_is_estimated",
                "completed_at",
            ]
        )
        video.status = Video.Status.COMPLETED
        video.save(update_fields=["status"])
    except Exception as exc:
        analysis.status = Analysis.Status.FAILED
        analysis.error_message = str(exc)
        analysis.completed_at = timezone.now()
        analysis.save(update_fields=["status", "error_message", "completed_at"])
        video.status = Video.Status.FAILED
        video.save(update_fields=["status"])
    return analysis


def analysis_summary(analysis):
    rows = []
    gantt_rows = []
    totals_by_activity = defaultdict(Decimal)
    gantt_by_activity = {}
    totals = {
        "operator": Decimal("0"),
        "machine": Decimal("0"),
        "walking": Decimal("0"),
        "waiting": Decimal("0"),
        "uncertain": Decimal("0"),
    }
    total_duration = Decimal("0")
    max_segment_end = Decimal("0")
    segments = list(analysis.segments.select_related("activity", "operation"))

    for segment in segments:
        duration = segment.duration_seconds
        total_duration += duration
        totals_by_activity[segment.activity_name] += duration
        max_segment_end = max(max_segment_end, segment.end_seconds)

        activity = segment.activity
        name = segment.activity_name.casefold()
        if activity and activity.performed_by == Activity.Performer.MACHINE:
            totals["machine"] += duration
        elif activity and activity.performed_by in {Activity.Performer.OPERATOR, Activity.Performer.BOTH}:
            totals["operator"] += duration
        if "chod" in name:
            totals["walking"] += duration
        if "oczek" in name:
            totals["waiting"] += duration
        if "niepew" in name:
            totals["uncertain"] += duration

    video_duration = analysis.video.duration_seconds or total_duration or max_segment_end
    timeline_duration = max(video_duration, max_segment_end, Decimal("1"))

    palette = ["#2563eb", "#059669", "#7c3aed", "#d97706", "#db2777", "#0891b2", "#65a30d", "#dc2626"]

    def op_name_of(segment):
        if segment.operation_name:
            return segment.operation_name
        if segment.operation_id:
            return segment.operation.name
        return ""

    operation_colors = {}
    for segment in segments:
        oname = op_name_of(segment)
        if oname and oname not in operation_colors:
            operation_colors[oname] = palette[len(operation_colors) % len(palette)]
    multi = len(operation_colors) > 1

    gantt_by_key = {}
    for segment in segments:
        duration = segment.duration_seconds
        oname = op_name_of(segment)
        color = operation_colors.get(oname, "#1c2b4a") if multi else "#1c2b4a"
        key = (oname, segment.activity_name)
        if key not in gantt_by_key:
            gantt_by_key[key] = {
                "name": segment.activity_name,
                "operation_name": oname,
                "color": color,
                "duration": Decimal("0"),
                "first_start": segment.start_seconds,
                "bars": [],
            }
        row = gantt_by_key[key]
        row["duration"] += duration
        row["first_start"] = min(row["first_start"], segment.start_seconds)
        left = segment.start_seconds / timeline_duration * Decimal("100")
        width = duration / timeline_duration * Decimal("100")
        row["bars"].append(
            {
                "id": segment.pk,
                "start_seconds": segment.start_seconds,
                "end_seconds": segment.end_seconds,
                "duration": duration,
                "confidence": segment.confidence,
                "reason": segment.reason,
                "left": _css_percent(left),
                "width": _css_percent(width),
                "color": color,
            }
        )

    for name, duration in sorted(totals_by_activity.items(), key=lambda item: item[0].casefold()):
        percent = Decimal("0")
        if total_duration:
            percent = (duration / total_duration * Decimal("100")).quantize(Decimal("0.1"))
        rows.append({"name": name, "duration": duration, "percent": percent})

    gantt_rows = sorted(
        gantt_by_key.values(),
        key=lambda row: (row["operation_name"].casefold(), row["first_start"], row["name"].casefold()),
    )
    operations_legend = (
        [{"name": n, "color": c} for n, c in operation_colors.items()] if multi else []
    )

    return {
        "video_duration": video_duration,
        "segmented_duration": total_duration,
        "timeline_duration": timeline_duration,
        "activity_rows": rows,
        "gantt_rows": gantt_rows,
        "operations_legend": operations_legend,
        "is_multi_operation": multi,
        "operator": totals["operator"],
        "machine": totals["machine"],
        "walking": totals["walking"],
        "waiting": totals["waiting"],
        "uncertain": totals["uncertain"],
    }


def _analysis_worker(video_pk):
    """Cel wątku: pobiera wideo i uruchamia analizę, domykając połączenie DB wątku."""
    try:
        video = Video.objects.select_related("operation", "operation__process").get(pk=video_pk)
        run_video_analysis(video)
    finally:
        connection.close()


def run_analysis_in_background(video):
    """Startuje analizę w osobnym wątku i natychmiast zwraca (nie czeka na wynik)."""
    thread = threading.Thread(target=_analysis_worker, args=(video.pk,), daemon=True)
    thread.start()
    return thread


def segments_needing_review(analysis, threshold=0.4):
    """Segmenty wymagające uwagi człowieka: niska pewność lub czynność 'niepewne'."""
    flagged = []
    for segment in analysis.segments.select_related("activity"):
        if segment.confidence < threshold or "niepew" in segment.activity_name.casefold():
            flagged.append(segment)
    return flagged
