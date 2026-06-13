import json
import re
import subprocess
import threading
import time
from collections import Counter, defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.db import connection, transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_noop

from .models import Activity, Analysis, AnalysisSegment, Operation, Video

FACE_DETECTOR_MODEL = Path(__file__).resolve().parent / "assets" / "models" / "face_detection_yunet_2023mar.onnx"
SYSTEM_UNCERTAIN_ACTIVITY = "niepewne"


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


def _opencv_face_blur(input_path, output_path, progress_callback=None):
    import cv2
    import numpy as np

    if not FACE_DETECTOR_MODEL.exists():
        raise RuntimeError("Brakuje modelu detekcji twarzy YuNet.")

    capture = cv2.VideoCapture(str(input_path))
    if not capture.isOpened():
        raise RuntimeError("Nie można otworzyć pliku wideo do anonimizacji.")

    fps = capture.get(cv2.CAP_PROP_FPS) or 25
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if progress_callback:
        progress_callback(0, total_frames, gettext_noop("Wykrywanie i maskowanie twarzy"), percent=1, force=True)
    max_detector_side = 960
    detector_scale = min(1.0, max_detector_side / max(width, height))
    detector_size = (
        max(1, int(width * detector_scale)),
        max(1, int(height * detector_scale)),
    )
    output_scale = min(1.0, 1280 / max(width, height))
    output_size = (
        max(1, int(width * output_scale)),
        max(1, int(height * output_scale)),
    )
    detector = cv2.FaceDetectorYN_create(
        str(FACE_DETECTOR_MODEL),
        "",
        detector_size,
        0.75,
        0.3,
        5000,
    )
    silent_path = output_path.with_name(f"{output_path.stem}_silent.mp4")
    writer = cv2.VideoWriter(
        str(silent_path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        output_size,
    )
    if not writer.isOpened():
        capture.release()
        raise RuntimeError("Nie można utworzyć pliku roboczego po anonimizacji.")

    detection_interval = max(1, int(round(fps / 8)))
    frame_index = 0
    active_boxes = []
    active_ttl = 0
    blurred_faces = 0
    last_progress_at = 0.0

    while True:
        ok, frame = capture.read()
        if not ok:
            break

        if frame_index % detection_interval == 0:
            detector_frame = (
                cv2.resize(frame, detector_size, interpolation=cv2.INTER_AREA)
                if detector_scale < 1.0
                else frame
            )
            _, faces = detector.detect(detector_frame)
            active_boxes = []
            if faces is not None:
                for face in faces:
                    x, y, w, h = (face[:4] / detector_scale).astype(int)
                    if w * h >= 1800:
                        active_boxes.append((x, y, w, h))
            active_ttl = detection_interval if active_boxes else 0

        for x, y, w, h in active_boxes:
            padding_x = int(w * 0.10)
            padding_y = int(h * 0.12)
            x1 = max(0, x - padding_x)
            y1 = max(0, y - padding_y)
            x2 = min(width, x + w + padding_x)
            y2 = min(height, y + h + padding_y)
            face_region = frame[y1:y2, x1:x2]
            if face_region.size:
                kernel = max(31, ((min(face_region.shape[:2]) // 3) | 1))
                blur = cv2.GaussianBlur(face_region, (kernel, kernel), 0)
                mask = cv2.ellipse(
                    np.zeros(face_region.shape[:2], dtype="uint8"),
                    (face_region.shape[1] // 2, face_region.shape[0] // 2),
                    (max(1, face_region.shape[1] // 2), max(1, face_region.shape[0] // 2)),
                    0,
                    0,
                    360,
                    255,
                    -1,
                )
                feather = max(15, ((min(face_region.shape[:2]) // 9) | 1))
                mask = cv2.GaussianBlur(mask, (feather, feather), 0).astype("float32") / 255.0
                mask = mask[..., None]
                frame[y1:y2, x1:x2] = (blur * mask + face_region * (1.0 - mask)).astype("uint8")
                blurred_faces += 1
        output_frame = (
            cv2.resize(frame, output_size, interpolation=cv2.INTER_AREA)
            if output_scale < 1.0
            else frame
        )
        writer.write(output_frame)
        if active_ttl:
            active_ttl -= 1
            if not active_ttl:
                active_boxes = []
        frame_index += 1
        if progress_callback and total_frames:
            now = time.monotonic()
            if now - last_progress_at >= 1.0 or frame_index >= total_frames:
                percent = min(90, max(1, int(frame_index / total_frames * 90)))
                progress_callback(
                    frame_index,
                    total_frames,
                    gettext_noop("Wykrywanie i maskowanie twarzy"),
                    percent=percent,
                )
                last_progress_at = now

    capture.release()
    writer.release()

    if not blurred_faces:
        silent_path.unlink(missing_ok=True)
        raise RuntimeError("Nie wykryto twarzy w filmie. Anonimizacja nie została wykonana.")

    if progress_callback:
        progress_callback(total_frames, total_frames, gettext_noop("Łączenie audio i zapis pliku"), percent=95, force=True)

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
            "ultrafast",
            "-crf",
            "28",
            "-threads",
            "0",
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
    def update_progress(current, total, label, percent=None, force=False):
        if percent is None:
            percent = int(current / total * 100) if total else 0
        percent = max(0, min(100, int(percent)))
        Video.objects.filter(pk=video.pk).update(
            anonymization_progress_current=max(0, int(current or 0)),
            anonymization_progress_total=max(0, int(total or 0)),
            anonymization_progress_percent=percent,
            anonymization_progress_label=label,
            anonymization_progress_updated_at=timezone.now(),
        )
        video.anonymization_progress_current = max(0, int(current or 0))
        video.anonymization_progress_total = max(0, int(total or 0))
        video.anonymization_progress_percent = percent
        video.anonymization_progress_label = label
        video.anonymization_progress_updated_at = timezone.now()

    video.status = Video.Status.ANONYMIZING
    video.anonymization_error = ""
    video.anonymized_file = ""
    video.anonymization_progress_current = 0
    video.anonymization_progress_total = 0
    video.anonymization_progress_percent = 0
    video.anonymization_progress_label = gettext_noop("Przygotowanie pliku")
    video.anonymization_progress_updated_at = timezone.now()
    video.save(
        update_fields=[
            "status",
            "anonymization_error",
            "anonymized_file",
            "anonymization_progress_current",
            "anonymization_progress_total",
            "anonymization_progress_percent",
            "anonymization_progress_label",
            "anonymization_progress_updated_at",
        ]
    )

    input_path = Path(video.file.path)
    output_path = _safe_output_path(video, "anon")

    try:
        blurred_faces = _opencv_face_blur(input_path, output_path, progress_callback=update_progress)
        if blurred_faces:
            anonymization_summary = f"Rozmyto {blurred_faces} wykrytych wystąpień twarzy."
        else:
            anonymization_summary = "Nie wykryto twarzy do rozmycia."

        with output_path.open("rb") as handle:
            video.anonymized_file.save(output_path.name, File(handle), save=False)
        output_path.unlink(missing_ok=True)

        update_progress(
            video.anonymization_progress_total,
            video.anonymization_progress_total,
            "Anonimizacja zakończona",
            percent=100,
            force=True,
        )
        video.status = Video.Status.AWAITING_APPROVAL
        video.anonymized_at = timezone.now()
        video.approved_for_analysis_at = None
        video.anonymization_error = f"{anonymization_summary} Sprawdź podgląd przed zatwierdzeniem analizy."
        video.save(
            update_fields=[
                "anonymized_file",
                "status",
                "anonymized_at",
                "approved_for_analysis_at",
                "anonymization_error",
                "anonymization_progress_current",
                "anonymization_progress_total",
                "anonymization_progress_percent",
                "anonymization_progress_label",
                "anonymization_progress_updated_at",
            ]
        )
        return video
    except Exception as exc:
        output_path.unlink(missing_ok=True)
        output_path.with_name(f"{output_path.stem}_silent.mp4").unlink(missing_ok=True)
        video.status = Video.Status.FAILED
        video.anonymization_error = str(exc)
        video.anonymization_progress_label = gettext_noop("Błąd anonimizacji")
        video.anonymization_progress_updated_at = timezone.now()
        video.save(
            update_fields=[
                "status",
                "anonymization_error",
                "anonymization_progress_label",
                "anonymization_progress_updated_at",
            ]
        )
        raise


def _anonymization_worker(video_pk):
    try:
        video = Video.objects.get(pk=video_pk)
        anonymize_video(video)
    finally:
        connection.close()


def run_anonymization_in_background(video):
    video.status = Video.Status.ANONYMIZING
    video.anonymization_error = ""
    video.approved_for_analysis_at = None
    video.anonymization_progress_current = 0
    video.anonymization_progress_total = 0
    video.anonymization_progress_percent = 0
    video.anonymization_progress_label = gettext_noop("W kolejce do anonimizacji")
    video.anonymization_progress_updated_at = timezone.now()
    video.save(
        update_fields=[
            "status",
            "anonymization_error",
            "approved_for_analysis_at",
            "anonymization_progress_current",
            "anonymization_progress_total",
            "anonymization_progress_percent",
            "anonymization_progress_label",
            "anonymization_progress_updated_at",
        ]
    )
    thread = threading.Thread(target=_anonymization_worker, args=(video.pk,), daemon=True)
    thread.start()
    return thread


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


def _duration_rule_lines(duration_seconds):
    """Reguły skali czasu: kotwiczą długość nagrania i wymuszają sekundy dziesiętne
    (model bywa, że zwraca czas zegarowo mm:ss, np. 1.30 zamiast 90.0)."""
    lines = []
    if duration_seconds:
        total = quantize_seconds(duration_seconds)
        lines.append(
            f"- nagranie trwa {total} sekund — segmenty muszą pokrywać całą jego długość,"
            f" a end_seconds nie może przekraczać {total},"
        )
    lines.append(
        "- czas podawaj w SEKUNDACH DZIESIĘTNYCH liczonych od początku (np. 75.5),"
        " a NIE w formacie zegarowym minuty:sekundy (90 sekund to 90.0, nie 1.30),"
    )
    return lines


def _segment_contract_lines(include_operation=False):
    operation_field = '"operation":"nazwa operacji",' if include_operation else ""
    return [
        "Zwróć wyłącznie poprawny JSON zgodny ze schematem:",
        (
            '{"segments":[{'
            f"{operation_field}"
            '"start_seconds":0.0,'
            '"end_seconds":1.0,'
            '"activity":"nazwa czynności albo niepewne",'
            '"confidence":0.62,'
            '"alternative_activity":"inna możliwa czynność albo null",'
            '"evidence":["konkretny widoczny/słyszalny sygnał"],'
            '"missing_evidence":["czego nie widać, a byłoby potrzebne do pewności"],'
            '"reason":"krótkie uzasadnienie",'
            '"confidence_reason":"dlaczego confidence ma właśnie taki poziom"'
            "}]}"
        ),
        'Nie używaj kluczy typu "box_2d", bounding box ani dodatkowych opakowań; lista segmentów ma być wyłącznie pod kluczem "segments".',
    ]


def _confidence_rule_lines():
    return [
        "Kalibracja confidence:",
        "- nie używaj stałej wartości confidence dla wielu segmentów; każda wartość ma wynikać z jakości dowodu,",
        "- 0.90-1.00 tylko gdy widać charakterystyczne, stabilne sygnały startu, trwania i końca czynności oraz brak realnej alternatywy,",
        "- 0.70-0.89 gdy czynność jest prawdopodobna, ale część sygnałów jest pośrednia albo krótka,",
        "- 0.45-0.69 gdy istnieje sensowna alternatywna czynność lub widać tylko część dowodów,",
        "- 0.20-0.44 gdy obraz/dźwięk jest niejasny; wtedy zwykle wybierz activity=\"niepewne\",",
        "- jeśli ruch jest tylko mikro-korektą, przejściem albo gestem bez stabilnej zmiany czynności, nie twórz nowej pewnej czynności bez mocnego dowodu,",
        "- jeśli krótki fragment ma stabilne, widoczne sygnały innej zdefiniowanej czynności, wydziel go jako osobny segment z adekwatnym confidence zamiast ukrywać go w sąsiednim segmencie.",
    ]


def _evidence_discipline_lines():
    """Domenowo-neutralne reguły przeciw halucynacji dowodu i nadmiernej pewności.
    Działają tak samo dla jazdy, kuchni, biura itd. — opierają decyzję na tym,
    co realnie widać w kadrze, a nie na kontekście sceny czy obecności sprzętu."""
    return [
        "Dyscyplina dowodowa (obowiązuje dla każdej domeny i każdej czynności):",
        "- klasyfikuj wyłącznie na podstawie tego, co realnie widać lub słychać w danym fragmencie; nie zakładaj czynności na podstawie kontekstu sceny ani samej obecności narzędzia/sprzętu/stanowiska w kadrze,",
        "- decyduj na podstawie obserwowalnej zmiany (kierunku, obiektu, narzędzia, fazy, pozycji), a nie na podstawie tego, co zwykle towarzyszy danej czynności,",
        "- dla rozważanej czynności wskaż jej najbliższy odpowiednik (najłatwiejszy do pomylenia) i wybierz tę czynność tylko wtedy, gdy widać sygnał jednoznacznie oddzielający ją od tego odpowiednika; w przeciwnym razie użyj \"" + SYSTEM_UNCERTAIN_ACTIVITY + "\",",
        "- w evidence wpisuj tylko obserwacje możliwe do wskazania na konkretnej klatce; nie wpisuj sygnałów, których nie widać (np. \"narzędzie w ruchu\", gdy nic się nie porusza, albo \"skręt\", gdy kierunek się nie zmienia),",
        f'- brak wyraźnego sygnału odróżniającego oznacza "{SYSTEM_UNCERTAIN_ACTIVITY}", a nie najbardziej prawdopodobny wybór.',
    ]


def _granularity_rule_lines():
    return [
        "Granularność segmentów:",
        "- nie scalaj kilku odrębnych wystąpień tej samej czynności w jeden długi segment, jeśli widać między nimi wyraźną zmianę kierunku, obiektu, narzędzia, fazy pracy albo krótką fazę przejściową,",
        "- dziel takie wystąpienia na osobne segmenty, gdy da się wskazać ich granice czasowe z dokładnością około 1 sekundy lub lepszą,",
        "- nie używaj kryterium \"za krótkie, żeby było znaczące\" do usuwania widocznych fragmentów innej zdefiniowanej czynności; krótki segment jest lepszy niż błędne włączenie go do długiego segmentu sąsiedniego,",
        "- długi segment może mieć wysokie confidence tylko wtedy, gdy ta sama czynność jest ciągła przez cały zakres czasu; jeśli wewnątrz widać przeplot A/B/A, podziel go na A, B i A,",
        "- minimalny czas trwania czynności jest wskazówką do obniżenia confidence i review, a nie pozwoleniem na wchłonięcie krótszego fragmentu przez inną czynność,",
        "- nie dziel rytmicznych mikro-ruchów w ramach tej samej ciągłej czynności, jeśli nie zmieniają znaczenia procesu.",
    ]


def _system_uncertain_lines():
    return [
        f'Poza zdefiniowanymi czynnościami możesz użyć systemowej etykiety "{SYSTEM_UNCERTAIN_ACTIVITY}".',
        f'Użyj "{SYSTEM_UNCERTAIN_ACTIVITY}", gdy nie da się uczciwie rozstrzygnąć między czynnościami albo brakuje kluczowych dowodów.',
        f'"{SYSTEM_UNCERTAIN_ACTIVITY}" nie jest nową czynnością procesu, tylko sygnałem do przeglądu przez człowieka.',
    ]


def _active_confusion_rule_lines(activities, indent=""):
    seen = set()
    lines = []
    for activity in activities:
        hints = activity.hints.filter(is_active=True, confused_with__isnull=False).select_related(
            "confused_with"
        )
        for hint in hints:
            text = hint.text.strip()
            if not text:
                continue
            key = (activity.pk, hint.confused_with_id, text.casefold())
            if key in seen:
                continue
            seen.add(key)
            lines.append(
                f'{indent}- Gdy wahasz się między "{activity.name}" i "{hint.confused_with.name}", '
                f'wybierz "{activity.name}" tylko jeśli pasuje ta korekta: {text}. '
                f'Jeśli dowód jest częściowy, obniż confidence albo użyj "{SYSTEM_UNCERTAIN_ACTIVITY}".'
            )
    return lines


def build_analysis_prompt(operation, duration_seconds=None):
    activities = list(operation.activities.all())
    lines = [
        f'Analizujesz zanonimizowane nagranie operacji "{operation.name}" w procesie "{operation.process.name}".',
        "",
        "Przypisz każdy fragment nagrania wyłącznie do jednej z poniższych czynności.",
        f'Nie twórz nowych nazw czynności poza systemową etykietą "{SYSTEM_UNCERTAIN_ACTIVITY}". Nie zgaduj.',
        *_system_uncertain_lines(),
        *_segment_contract_lines(),
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
    confusion_lines = _active_confusion_rule_lines(activities)
    if confusion_lines:
        lines.extend(["Reguły rozróżniania często mylonych czynności:", *confusion_lines, ""])
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
            *_evidence_discipline_lines(),
            "",
            "Zasady segmentacji:",
            "- segmenty nie mogą nachodzić na siebie,",
            *_duration_rule_lines(duration_seconds),
            *_granularity_rule_lines(),
            *_confidence_rule_lines(),
            "- reason ma krótko wyjaśniać, co widać lub słychać,",
            "- evidence ma zawierać konkretne obserwacje, a nie parafrazę nazwy czynności,",
            "- alternative_activity ustaw, gdy realnie możliwa jest inna czynność z listy,",
            "- missing_evidence zostaw jako pustą listę tylko wtedy, gdy nie brakuje żadnego ważnego dowodu,",
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


def build_multi_operation_prompt(process, operations, duration_seconds=None):
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
        f'5. Gdy nie możesz pewnie określić operacji lub czynności — wybierz activity="{SYSTEM_UNCERTAIN_ACTIVITY}" zamiast zgadywać i obniż confidence.',
        "6. Przerwy, brak pracy i czekanie oznaczaj odpowiednią czynnością, jeśli jest zdefiniowana.",
        *_system_uncertain_lines(),
        "",
        *_segment_contract_lines(include_operation=True),
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
        confusion_lines = _active_confusion_rule_lines(activities, indent="   ")
        if confusion_lines:
            lines.append("   Reguły rozróżniania często mylonych czynności:")
            lines.extend(confusion_lines)
    lines.extend(
        [
            "",
            *_evidence_discipline_lines(),
            "",
            "ZASADY SEGMENTACJI:",
            *_duration_rule_lines(duration_seconds),
            *_granularity_rule_lines(),
            *_confidence_rule_lines(),
            "- reason ma krótko wyjaśniać, co widać lub słychać oraz po czym poznajesz operację,",
            "- evidence ma zawierać konkretne obserwacje, a nie parafrazę nazwy czynności,",
            "- alternative_activity ustaw, gdy realnie możliwa jest inna czynność z tej samej operacji,",
            "- missing_evidence zostaw jako pustą listę tylko wtedy, gdy nie brakuje żadnego ważnego dowodu,",
            "- odpowiedź ma zawierać tylko JSON, bez komentarzy i markdown.",
        ]
    )
    return "\n".join(lines)


def build_analysis_prompt_for_video(video):
    """Wybiera właściwy prompt: jedno-operacyjny lub multi-operacyjny."""
    operations = video.analysis_operations()
    if len(operations) > 1:
        return build_multi_operation_prompt(
            video.analysis_process(), operations, duration_seconds=video.duration_seconds
        )
    if operations:
        return build_analysis_prompt(operations[0], duration_seconds=video.duration_seconds)
    raise ValueError("Wideo nie ma przypisanej operacji do analizy.")


def _coerce_segments_payload(parsed):
    if isinstance(parsed, list):
        if len(parsed) == 1 and isinstance(parsed[0], dict):
            for key in ("segments", "box_2d"):
                if isinstance(parsed[0].get(key), list):
                    return {"segments": parsed[0][key]}
        return {"segments": parsed}
    if isinstance(parsed, dict):
        for key in ("box_2d",):
            if isinstance(parsed.get(key), list):
                return {"segments": parsed[key]}
        return parsed
    return parsed


def _extract_balanced_json_array(text, start_index):
    depth = 0
    in_string = False
    escaped = False
    for index in range(start_index, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return text[start_index : index + 1]
    return ""


def _extract_wrapped_segments(text):
    for key in ("segments", "box_2d"):
        match = re.search(rf'"{key}"\s*:\s*\[', text)
        if not match:
            continue
        start = text.find("[", match.start())
        if start < 0:
            continue
        fragment = _extract_balanced_json_array(text, start)
        if not fragment:
            continue
        parsed = json.loads(fragment)
        if isinstance(parsed, list):
            return {"segments": parsed}
    return None


def _extract_json(text):
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped).strip()
        stripped = re.sub(r"```$", "", stripped).strip()

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, (list, dict)):
        return _coerce_segments_payload(parsed)

    match = re.search(r"(\{.*\}|\[.*\])", stripped, flags=re.DOTALL)
    if not match:
        raise ValueError("Odpowiedź modelu nie zawiera obiektu JSON.")
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        wrapped = _extract_wrapped_segments(stripped)
        if wrapped is not None:
            return wrapped
        raise
    return _coerce_segments_payload(parsed)


def _activity_lookup(operation):
    activities = list(operation.activities.all())
    lookup = {activity.name.casefold(): activity for activity in activities}
    uncertain = next((a for a in activities if "niepew" in a.name.casefold()), None)
    return lookup, uncertain


def _is_uncertain_activity_name(name):
    normalized = str(name or "").strip().casefold()
    return normalized in {
        SYSTEM_UNCERTAIN_ACTIVITY,
        "uncertain",
        "unclear",
        "niejasne",
        "nie wiadomo",
    } or "niepew" in normalized


def _coerce_text_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _clean_optional_name(value):
    if value is None:
        return ""
    text = str(value).strip()
    if text.casefold() in {"", "null", "none", "brak", "n/a"}:
        return ""
    return text


def _clamp_confidence(value):
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = 0.0
    return max(0.0, min(1.0, confidence))


def _resolve_activity(activity_name, lookup, uncertain):
    if _is_uncertain_activity_name(activity_name):
        return uncertain, uncertain.name if uncertain else SYSTEM_UNCERTAIN_ACTIVITY
    activity = lookup.get(str(activity_name or "").strip().casefold())
    if activity is not None:
        return activity, activity.name
    if uncertain is not None:
        return uncertain, uncertain.name
    return None, SYSTEM_UNCERTAIN_ACTIVITY


def _compose_segment_reason(item):
    parts = []
    reason = str(item.get("reason", "")).strip()
    if reason:
        parts.append(reason)
    evidence = _coerce_text_list(item.get("evidence"))
    if evidence:
        parts.append("Dowody: " + "; ".join(evidence))
    missing = _coerce_text_list(item.get("missing_evidence"))
    if missing:
        parts.append("Brakujące dowody: " + "; ".join(missing))
    alternative = _clean_optional_name(item.get("alternative_activity"))
    if alternative and not _is_uncertain_activity_name(alternative):
        parts.append(f"Alternatywa: {alternative}")
    confidence_reason = str(item.get("confidence_reason") or "").strip()
    if confidence_reason:
        parts.append(f"Uzasadnienie pewności: {confidence_reason}")
    return " | ".join(parts)[:2000]


def _append_reason_note(segment, note):
    current = (segment.get("reason") or "").strip()
    segment["reason"] = f"{current} | {note}"[:2000] if current else note[:2000]


def _calibrate_confidence(item, activity, activity_name, start, end):
    raw_confidence = _clamp_confidence(item.get("confidence", 0))
    confidence = raw_confidence
    evidence = _coerce_text_list(item.get("evidence"))
    missing = _coerce_text_list(item.get("missing_evidence"))
    alternative = _clean_optional_name(item.get("alternative_activity"))
    has_alternative = bool(alternative) and not _is_uncertain_activity_name(alternative)
    has_structured_confidence = any(
        key in item
        for key in ("evidence", "missing_evidence", "alternative_activity", "confidence_reason")
    )

    if raw_confidence >= 0.9 and not has_structured_confidence:
        confidence = min(confidence, 0.82)
    if not evidence:
        confidence = min(confidence, 0.82)
    if has_alternative and alternative.casefold() != str(activity_name).casefold():
        confidence = min(confidence, 0.64)
    if missing:
        confidence = min(confidence, 0.64 if has_alternative else 0.72)
    if _is_uncertain_activity_name(activity_name):
        confidence = min(confidence, 0.45)

    duration = end - start
    if duration < Decimal("0.75"):
        confidence = min(confidence, 0.50)
    elif duration < Decimal("1.50"):
        confidence = min(confidence, 0.62)
    elif duration < Decimal("2.50"):
        confidence = min(confidence, 0.76)

    if (
        activity is not None
        and activity.minimum_duration_seconds is not None
        and duration < Decimal(str(activity.minimum_duration_seconds))
    ):
        confidence = min(confidence, 0.50)

    return round(confidence, 4), raw_confidence


def _lane_key(segment):
    operation = segment.get("operation")
    if operation is not None:
        return ("op", operation.pk)
    return ("single", segment.get("operation_name", ""))


def _apply_temporal_quality_checks(segments):
    lanes = defaultdict(list)
    for segment in segments:
        lanes[_lane_key(segment)].append(segment)

    for lane in lanes.values():
        lane.sort(key=lambda item: (item["start_seconds"], item["end_seconds"]))
        if len(lane) >= 4:
            raw_counts = Counter(
                round(float(segment.get("_model_confidence", segment["confidence"])), 2)
                for segment in lane
                if float(segment.get("_model_confidence", segment["confidence"])) >= 0.9
            )
            if raw_counts:
                plateau_value, plateau_count = raw_counts.most_common(1)[0]
                if plateau_count >= max(4, int(len(lane) * 0.6)):
                    for segment in lane:
                        if round(float(segment.get("_model_confidence", segment["confidence"])), 2) == plateau_value:
                            segment["confidence"] = min(segment["confidence"], 0.64)
                            segment["confidence_unreliable"] = True
                            _append_reason_note(
                                segment,
                                "Kalibracja: model użył powtarzalnej wysokiej pewności; liczbowa pewność jest niewiarygodna — zweryfikuj ręcznie.",
                            )

        for index in range(1, len(lane) - 1):
            previous = lane[index - 1]
            current = lane[index]
            following = lane[index + 1]
            duration = current["end_seconds"] - current["start_seconds"]
            same_neighbors = previous["activity_name"] == following["activity_name"]
            different_middle = current["activity_name"] != previous["activity_name"]
            if same_neighbors and different_middle and duration <= Decimal("2.00"):
                current["confidence"] = min(current["confidence"], 0.58)
                _append_reason_note(
                    current,
                    "Kontrola czasowa: krótki przełącznik między dwiema częściami tej samej czynności; możliwa korekta lub przejście zamiast osobnej czynności.",
                )

        for segment in lane:
            segment.pop("_model_confidence", None)

    return segments


def _normalize_segments(payload, operation, duration_seconds=None):
    if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
        raise ValueError("JSON musi zawierać listę segments.")

    lookup, uncertain = _activity_lookup(operation)
    normalized = []
    last_end = Decimal("0")
    max_duration = quantize_seconds(duration_seconds) if duration_seconds else None

    for item in payload["segments"]:
        activity_name = str(item.get("activity", "")).strip()
        activity, activity_name = _resolve_activity(activity_name, lookup, uncertain)
        start = quantize_seconds(item.get("start_seconds", 0))
        end = quantize_seconds(item.get("end_seconds", 0))
        if start < last_end:
            start = last_end
        if max_duration is not None:
            start = min(start, max_duration)
            end = min(end, max_duration)
        if end <= start:
            continue
        confidence, raw_confidence = _calibrate_confidence(item, activity, activity_name, start, end)
        normalized.append(
            {
                "activity": activity,
                "activity_name": activity.name if activity else activity_name,
                "start_seconds": start,
                "end_seconds": end,
                "confidence": confidence,
                "reason": _compose_segment_reason(item),
                "_model_confidence": raw_confidence,
            }
        )
        last_end = end

    if not normalized:
        raise ValueError("Model nie zwrócił poprawnych segmentów.")
    return _apply_temporal_quality_checks(normalized)


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
        activity, activity_name = _resolve_activity(activity_name, entry["lookup"], entry["uncertain"])
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
        confidence, raw_confidence = _calibrate_confidence(item, activity, activity_name, start, end)
        normalized.append(
            {
                "operation": operation,
                "operation_name": operation.name,
                "activity": activity,
                "activity_name": activity.name if activity else activity_name,
                "start_seconds": start,
                "end_seconds": end,
                "confidence": confidence,
                "reason": _compose_segment_reason(item),
                "_model_confidence": raw_confidence,
            }
        )
        last_end_by_op[operation.pk] = end

    if not normalized:
        raise ValueError("Model nie zwrócił poprawnych segmentów.")
    return _apply_temporal_quality_checks(normalized)


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


def _video_content_part(uploaded):
    """Owija przesłany plik w Part z ustawionym fps, gdy GEMINI_VIDEO_FPS > 0.
    Domyślne próbkowanie modelu (~1 fps) nie wystarcza do rozróżniania krótkich,
    szybkich ruchów, więc podbijamy liczbę klatek widzianych przez model. Gdy fps
    jest 0/niezdefiniowany lub typy nie są dostępne — zwracamy plik bez zmian."""
    fps = float(getattr(settings, "GEMINI_VIDEO_FPS", 0) or 0)
    if fps <= 0:
        return uploaded
    try:
        from google.genai import types

        return types.Part(
            file_data=types.FileData(
                file_uri=uploaded.uri,
                mime_type=getattr(uploaded, "mime_type", None),
            ),
            video_metadata=types.VideoMetadata(fps=fps),
        )
    except Exception:
        # Nie blokuj analizy, jeśli SDK nie wspiera VideoMetadata — wróć do domyślnego próbkowania.
        return uploaded


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
        "recognition_rules": "- dominująca, widoczna zmiana odpowiadająca tej czynności utrzymuje się przez większość fragmentu\n- sygnał jest duży i czytelny w kadrze, nie wymaga rozpoznawania drobnych detali",
        "exclusion_rules": "- inna, podobna czynność ma wyraźniejszy sygnał w tym fragmencie\n- brak wyraźnego sygnału odróżniającego od czynności-bliźniaka — wtedy wybierz \"niepewne\", nie zgaduj",
        "possible_confusions": "- najbardziej podobna czynność z listy\n- niepewne",
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
Jesteś światowej klasy prompt engineerem. Tworzysz definicje czynności do automatycznej analizy wideo.
{intent}

KONTEKST: tekst, który tworzysz, trafia WPROST do promptu, którym inny model wideo dzieli nagranie na segmenty i przypisuje czynności. Działa to UNIWERSALNIE w różnych domenach — praca w kuchni, fabryce, biurze, hobby (np. sim racing) i innych. Nie zakładaj konkretnej branży. Twoim celem jest tak opisać tę czynność, żeby model pewnie odróżnił ją od czynności NAJBARDZIEJ do niej podobnej.

ZASADA NACZELNA (najważniejsza):
- Oprzyj rozpoznanie na OBSERWOWALNEJ ZMIANIE i na kontraście z najbliższym „bliźniakiem" (czynnością najłatwiejszą do pomylenia), a nie na samym kontekście sceny czy obecności sprzętu.
- Wskaż JEDEN dominujący sygnał, który jest DUŻY i czytelny w kadrze oraz utrzymuje się przez czynność — zamiast listy drobnych detali (np. układ szprych, napisy, ułożenie palców). Mikro-detale, których model nie rozdzieli na nagraniu, prowokują go do zgadywania i ZMYŚLANIA dowodu.

ZASADY PISANIA:
- Opisuj WYŁĄCZNIE to, co realnie widać w kadrze (obiekty, ruch i pozycja rąk/ciała, narzędzia, co się zmienia), tak by dało się to potwierdzić na pojedynczej klatce.
- ZAKAZane: meta-opisy („analiza sytuacji, w której…") oraz opisy CELU/INTENCJI/ocen. Od razu opisuj obraz.
- Zwięźle, rzeczowo, po polsku.

ZNACZENIE PÓL:
- description: co dokładnie widać w kadrze podczas tej czynności (sam obserwowalny obraz, bez celu i oceny).
- recognition_rules: 1–3 najpewniejsze, widoczne sygnały; na pierwszym miejscu dominująca, łatwa do sprawdzenia ZMIANA.
- exclusion_rules: kiedy NIE przypisywać tej czynności. OBOWIĄZKOWO: (a) nazwij czynność-bliźniaka, z którą bywa mylona, oraz widoczny sygnał, który je oddziela; (b) dopisz, że przy braku tego sygnału model ma wybrać „niepewne", a nie zgadywać. ZAKAZane reguły o jakości, technice, poprawności, bezpieczeństwie, wyniku czy szybkości wykonania — tylko o tym, co WIDAĆ.

PRZYKŁADY DOBREGO KONTRASTU (różne domeny, tylko dla stylu):
- kuchnia: „zawartość naczynia mieszana ruchem okrężnym" vs bliźniak „składnik przenoszony do/z naczynia";
- biuro: „palce uderzają w klawisze, treść na ekranie przyrasta" vs bliźniak „dłonie nieruchome, wzrok na ekranie (czytanie)";
- fabryka: „detal wkładany/mocowany w uchwycie" vs bliźniak „uchwyt pusty, maszyna pracuje";
- sim racing: „tor przed pojazdem wyraźnie zakręca" vs bliźniak „tor biegnie prosto".

DANE WEJŚCIOWE:
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


def _analyze_with_gemini(video, prompt, model_name=None):
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
        model=model_name or settings.GEMINI_VIDEO_MODEL,
        contents=[_video_content_part(uploaded), prompt],
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
                confidence_unreliable=segment.get("confidence_unreliable", False),
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
    model_name = video.analysis_model_name or settings.GEMINI_VIDEO_MODEL
    analysis = Analysis.objects.create(
        video=video,
        status=Analysis.Status.RUNNING,
        model_name=model_name if not settings.GEMINI_USE_MOCK else "mock",
        prompt=prompt,
        started_at=timezone.now(),
    )
    video.status = Video.Status.ANALYZING
    video.save(update_fields=["status"])
    raw_response = ""

    try:
        raw_response, usage = _analyze_with_gemini(video, prompt, model_name=model_name)
        try:
            payload = _extract_json(raw_response)
            segments = _segments_from_payload(payload, operations, video.duration_seconds)
        except Exception:
            allow_mock_fallback = (
                settings.GEMINI_FALLBACK_TO_MOCK
                and (settings.GEMINI_USE_MOCK or not settings.GEMINI_API_KEY)
            )
            if not allow_mock_fallback:
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
        analysis.raw_response = raw_response
        analysis.completed_at = timezone.now()
        analysis.save(update_fields=["status", "error_message", "raw_response", "completed_at"])
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
        row["bars"].append(
            {
                "id": segment.pk,
                "start_seconds": segment.start_seconds,
                "end_seconds": segment.end_seconds,
                "duration": duration,
                "confidence": segment.confidence,
                "reason": segment.reason,
                "color": color,
            }
        )

    # Sklej przylegające/nakładające się słupki tej samej czynności w jeden blok
    # (AI bywa, że dzieli ciągłą czynność na kilka segmentów pod rząd).
    for row in gantt_by_key.values():
        ordered = sorted(row["bars"], key=lambda b: b["start_seconds"])
        merged = []
        for bar in ordered:
            if merged and bar["start_seconds"] <= merged[-1]["end_seconds"]:
                prev = merged[-1]
                prev_dur = prev["end_seconds"] - prev["start_seconds"]
                bar_dur = bar["end_seconds"] - bar["start_seconds"]
                prev["end_seconds"] = max(prev["end_seconds"], bar["end_seconds"])
                total = (prev["end_seconds"] - prev["start_seconds"]) or Decimal("1")
                # pewność jako średnia ważona czasem trwania scalanych segmentów
                prev["confidence"] = round(
                    (prev["confidence"] * float(prev_dur) + bar["confidence"] * float(bar_dur))
                    / float(prev_dur + bar_dur or 1),
                    4,
                )
                prev["duration"] = prev["end_seconds"] - prev["start_seconds"]
            else:
                merged.append(dict(bar))
        for bar in merged:
            left = bar["start_seconds"] / timeline_duration * Decimal("100")
            width = bar["duration"] / timeline_duration * Decimal("100")
            bar["left"] = _css_percent(left)
            bar["width"] = _css_percent(width)
        row["bars"] = merged

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


def segments_needing_review(analysis, threshold=0.65):
    """Segmenty wymagające uwagi człowieka: niska pewność lub czynność 'niepewne'."""
    flagged = []
    for segment in analysis.segments.select_related("activity"):
        if segment.confidence < threshold or "niepew" in segment.activity_name.casefold():
            flagged.append(segment)
    return flagged
