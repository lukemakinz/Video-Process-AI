# Etap 1 — Architektura informacji + analiza w tle — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Analiza wideo uruchamia się w kontekście operacji i działa w tle (wątek + HTMX polling), zamiast blokować request synchronicznie.

**Architecture:** Wejście do analizy tylko z poziomu operacji (globalny skrót usunięty). Po zatwierdzeniu wideo analiza startuje w osobnym wątku; strona `video_review` odpytuje endpoint statusu przez HTMX co 3 s i po zakończeniu sama pokazuje link do wyniku.

**Tech Stack:** Django 6, HTMX (CDN), Tailwind (CDN), threading (stdlib), Gemini (analiza wideo) w trybie mock w testach.

**Uwagi wykonawcze:**
- Testy uruchamiamy runnerem Django: `python3 manage.py test processes -v2` (nie pytest).
- Repozytorium nie jest pod gitem — kroki `commit` są opcjonalne. Jeśli nie używasz gita, pomiń je. (Można też raz wykonać `git init`.)
- Testy AI/wideo działają w trybie mock; w testach ustawiamy `GEMINI_USE_MOCK=true` przez `override_settings`.

---

### Task 1: HTMX w base.html + usunięcie globalnego wejścia do analizy

**Files:**
- Modify: `templates/base.html`
- Modify: `templates/processes/process_list.html`
- Test: `processes/tests.py`

- [ ] **Step 1: Napisz test (czerwony)**

Dopisz do `processes/tests.py` w klasie `ProcessDemoTests` nową metodę:

```python
    def test_home_has_no_global_analyze_entry_and_loads_htmx(self):
        response = self.client.get("/")
        body = response.content.decode()
        # globalne wejście do uploadu zniknęło ze strony głównej
        self.assertNotIn('href="/videos/upload/"', body)
        # htmx jest załadowany (potrzebny do pollingu statusu)
        self.assertIn("htmx.org", body)
```

- [ ] **Step 2: Uruchom test — ma nie przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_home_has_no_global_analyze_entry_and_loads_htmx -v2`
Expected: FAIL — strona zawiera `href="/videos/upload/"` i nie zawiera `htmx.org`.

- [ ] **Step 3: Dodaj HTMX do `base.html`**

W `templates/base.html`, w sekcji `<head>` tuż po linii ze skryptem Tailwind (`<script src="https://cdn.tailwindcss.com"></script>`), dodaj:

```html
  <script src="https://unpkg.com/htmx.org@1.9.12" defer></script>
```

- [ ] **Step 4: Usuń pozycję „Analizuj nagranie" z topbara**

W `templates/base.html` usuń cały blok `<a>` nawigacji prowadzący do `video_upload` (link z tekstem „Analizuj nagranie" wewnątrz `<nav>`). Zostają wyłącznie pozycje „Procesy" i „Admin".

- [ ] **Step 5: Usuń przycisk „Analizuj nagranie" z nagłówka listy procesów**

W `templates/processes/process_list.html` usuń z nagłówka sekcji `<a class="btn ghost" href="{% url 'video_upload' %}">…Analizuj nagranie</a>` (cały ten `<a>`). Zostaje przycisk „Dodaj proces".

- [ ] **Step 6: Uruchom test — ma przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_home_has_no_global_analyze_entry_and_loads_htmx -v2`
Expected: PASS

- [ ] **Step 7: (opcjonalnie) Commit**

```bash
git add templates/base.html templates/processes/process_list.html processes/tests.py
git commit -m "feat: htmx w base, analiza tylko z poziomu operacji"
```

---

### Task 2: CTA „następny krok" na stronie operacji

**Files:**
- Modify: `templates/processes/operation_detail.html`
- Test: `processes/tests.py`

- [ ] **Step 1: Napisz test (czerwony)**

Dopisz do `processes/tests.py` w klasie `ProcessDemoTests`:

```python
    def test_operation_detail_next_step_cta(self):
        # operacja z setUp ma już czynności (self.load, self.machine) -> CTA upload
        with_act = self.client.get(f"/operations/{self.operation.pk}/")
        self.assertIn("Wgraj nagranie do analizy", with_act.content.decode())

        # nowa operacja bez czynności -> CTA dodania czynności, brak CTA uploadu
        empty_op = Operation.objects.create(
            process=self.process, name="Pakowanie", order=2
        )
        empty = self.client.get(f"/operations/{empty_op.pk}/")
        body = empty.content.decode()
        self.assertIn("Najpierw zdefiniuj czynności", body)
        self.assertNotIn("Wgraj nagranie do analizy", body)
```

- [ ] **Step 2: Uruchom test — ma nie przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_operation_detail_next_step_cta -v2`
Expected: FAIL — brak tekstów CTA w szablonie.

- [ ] **Step 3: Dodaj blok „następny krok" do `operation_detail.html`**

W `templates/processes/operation_detail.html`, bezpośrednio pod blokiem nagłówka (`</section>` zamykającym sekcję z `<h1>{{ operation.name }}</h1>`), wstaw:

```html
{% if operation.activities.all %}
  <div class="rounded-xl border border-primary-100 bg-primary-50 p-4 sm:p-5 mb-6 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
    <div>
      <p class="font-medium text-primary-800">Operacja gotowa do analizy</p>
      <p class="text-sm text-primary-700/80">Wgraj nagranie tej operacji — Gemini przypisze fragmenty do zdefiniowanych czynności.</p>
    </div>
    <a class="btn primary shrink-0" href="{% url 'operation_video_upload' operation.pk %}">
      <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5"/></svg>
      Wgraj nagranie do analizy
    </a>
  </div>
{% else %}
  <div class="rounded-xl border border-amber-200 bg-amber-50 p-4 sm:p-5 mb-6 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
    <div>
      <p class="font-medium text-amber-800">Najpierw zdefiniuj czynności</p>
      <p class="text-sm text-amber-700/80">Analiza wymaga zamkniętej listy czynności — to jedyne kategorie, które rozpozna model.</p>
    </div>
    <a class="btn secondary shrink-0" href="{% url 'activity_create' operation.pk %}">Dodaj czynność</a>
  </div>
{% endif %}
```

- [ ] **Step 4: Uruchom test — ma przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_operation_detail_next_step_cta -v2`
Expected: PASS

- [ ] **Step 5: (opcjonalnie) Commit**

```bash
git add templates/processes/operation_detail.html processes/tests.py
git commit -m "feat: CTA nastepny krok na stronie operacji"
```

---

### Task 3: Uruchamianie analizy w tle (services)

**Files:**
- Modify: `processes/services.py`
- Test: `processes/tests.py`

- [ ] **Step 1: Napisz test (czerwony)**

Dopisz do `processes/tests.py`. Na górze pliku upewnij się, że są importy (dodaj brakujące):

```python
from unittest.mock import patch
from django.test import TestCase, override_settings
```

Nowa metoda testowa w `ProcessDemoTests`:

```python
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
            # wątek ma być daemonem i celować w worker
            self.assertEqual(Thread.call_args.kwargs["target"], services._analysis_worker)
            self.assertEqual(Thread.call_args.kwargs["args"], (video.pk,))
            self.assertTrue(Thread.call_args.kwargs["daemon"])
```

Dodaj też import `timezone` u góry `tests.py`, jeśli go nie ma:

```python
from django.utils import timezone
```

- [ ] **Step 2: Uruchom test — ma nie przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_run_analysis_in_background_spawns_thread -v2`
Expected: FAIL — `AttributeError: module 'processes.services' has no attribute 'run_analysis_in_background'`.

- [ ] **Step 3: Dodaj wątek i worker do `services.py`**

Na górze `processes/services.py` dodaj import (obok istniejących `import` ze stdlib):

```python
import threading
```

oraz rozszerz import z `django.db`:

```python
from django.db import connection, transaction
```

Na końcu pliku dodaj:

```python
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
```

- [ ] **Step 4: Uruchom test — ma przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_run_analysis_in_background_spawns_thread -v2`
Expected: PASS

- [ ] **Step 5: (opcjonalnie) Commit**

```bash
git add processes/services.py processes/tests.py
git commit -m "feat: uruchamianie analizy w osobnym watku"
```

---

### Task 4: Endpoint statusu analizy + fragment HTMX

**Files:**
- Create: `templates/processes/_analysis_status.html`
- Modify: `processes/views.py`
- Modify: `processes/urls.py`
- Test: `processes/tests.py`

- [ ] **Step 1: Napisz test (czerwony)**

Dopisz do `processes/tests.py`:

```python
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
```

- [ ] **Step 2: Uruchom testy — mają nie przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_analysis_status_running_keeps_polling processes.tests.ProcessDemoTests.test_analysis_status_completed_links_result_and_stops processes.tests.ProcessDemoTests.test_analysis_status_failed_shows_error -v2`
Expected: FAIL — brak URL `analysis-status` (404 / NoReverseMatch).

- [ ] **Step 3: Utwórz fragment `_analysis_status.html`**

Utwórz `templates/processes/_analysis_status.html`:

```html
{% comment %}Fragment statusu analizy. Swap outerHTML; gdy trwa — sam się odpytuje, gdy gotowe/błąd — przestaje.{% endcomment %}
{% if analysis and analysis.status == 'completed' %}
  <div id="analysis-status" class="rounded-xl border border-emerald-200 bg-emerald-50 p-5 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
    <div class="flex items-center gap-3">
      <svg class="h-6 w-6 text-emerald-600" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
      <p class="font-medium text-emerald-800">Analiza zakończona</p>
    </div>
    <a class="btn primary shrink-0" href="{{ analysis.get_absolute_url }}">Otwórz wynik analizy</a>
  </div>
{% elif analysis and analysis.status == 'failed' %}
  <div id="analysis-status" class="rounded-xl border border-red-200 bg-red-50 p-5">
    <p class="font-medium text-red-800">Analiza nie powiodła się</p>
    <p class="text-sm text-red-700/80 mt-1">{{ analysis.error_message|default:"Spróbuj ponownie." }}</p>
    <form method="post" action="{% url 'video_approve_and_analyze' video.pk %}" class="mt-3">
      {% csrf_token %}
      <button class="btn ghost small" type="submit">Spróbuj ponownie</button>
    </form>
  </div>
{% else %}
  <div id="analysis-status"
       hx-get="{% url 'analysis_status' video.pk %}"
       hx-trigger="every 3s"
       hx-swap="outerHTML"
       class="rounded-xl border border-primary-100 bg-primary-50 p-5 flex items-center gap-3">
    <svg class="h-5 w-5 text-primary animate-spin" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path></svg>
    <div>
      <p class="font-medium text-primary-800">Analiza w toku…</p>
      <p class="text-sm text-primary-700/80">Wynik pojawi się tutaj automatycznie. Możesz nie zamykać tej strony.</p>
    </div>
  </div>
{% endif %}
```

- [ ] **Step 4: Dodaj widok `analysis_status`**

W `processes/views.py` dodaj funkcję (np. pod `video_review`):

```python
def analysis_status(request, pk):
    video = get_object_or_404(Video, pk=pk)
    analysis = video.analyses.order_by("-id").first()
    return render(
        request,
        "processes/_analysis_status.html",
        {"video": video, "analysis": analysis},
    )
```

- [ ] **Step 5: Dodaj trasę**

W `processes/urls.py` dodaj w `urlpatterns` (po trasie `video_review`):

```python
    path(
        "videos/<int:pk>/analysis-status/",
        views.analysis_status,
        name="analysis_status",
    ),
```

- [ ] **Step 6: Uruchom testy — mają przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_analysis_status_running_keeps_polling processes.tests.ProcessDemoTests.test_analysis_status_completed_links_result_and_stops processes.tests.ProcessDemoTests.test_analysis_status_failed_shows_error -v2`
Expected: PASS (wszystkie 3)

- [ ] **Step 7: (opcjonalnie) Commit**

```bash
git add templates/processes/_analysis_status.html processes/views.py processes/urls.py processes/tests.py
git commit -m "feat: endpoint statusu analizy z fragmentem HTMX"
```

---

### Task 5: Przepięcie zatwierdzenia na analizę w tle + polling w video_review

**Files:**
- Modify: `processes/views.py`
- Modify: `templates/processes/video_review.html`
- Test: `processes/tests.py`

- [ ] **Step 1: Napisz test (czerwony)**

Dopisz do `processes/tests.py`:

```python
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
```

- [ ] **Step 2: Uruchom test — ma nie przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_approve_starts_background_and_redirects_to_review -v2`
Expected: FAIL — `run_analysis_in_background` nie jest importowane w `views`, a widok wciąż działa synchronicznie / przekierowuje do analizy.

- [ ] **Step 3: Zaktualizuj import w `views.py`**

W `processes/views.py` w imporcie z `.services` dodaj `run_analysis_in_background` (obok istniejących):

```python
from .services import (
    analysis_summary,
    anonymize_video,
    get_video_duration_seconds,
    run_analysis_in_background,
    run_video_analysis,
    suggest_activity_description,
)
```

- [ ] **Step 4: Przepisz `video_approve_and_analyze`**

W `processes/views.py` zastąp ciało funkcji `video_approve_and_analyze` (zachowując dekorator `@require_POST`):

```python
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
```

- [ ] **Step 5: Pokaż polling w `video_review.html`**

W `templates/processes/video_review.html` zastąp blok decydujący o akcjach (obecnie `{% if not latest_analysis %} …formularz zatwierdzenia… {% else %} …link do wyniku… {% endif %}`) następującym:

```html
    {% if latest_analysis or video.status == 'analyzing' %}
      {% include "processes/_analysis_status.html" with video=video analysis=latest_analysis %}
      <div class="mt-3">
        <a class="btn ghost" href="{{ video.operation.get_absolute_url }}">Wróć do operacji</a>
      </div>
    {% else %}
      <form method="post" action="{% url 'video_approve_and_analyze' video.pk %}" class="flex flex-wrap items-center gap-2 mt-5 pt-5 border-t border-slate-100">
        {% csrf_token %}
        <button class="btn primary" type="submit">
          <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
          Zatwierdź i rozpocznij analizę Gemini
        </button>
        <a class="btn ghost" href="{{ video.operation.get_absolute_url }}">Wróć do operacji</a>
      </form>
    {% endif %}
```

- [ ] **Step 6: Uruchom test — ma przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_approve_starts_background_and_redirects_to_review -v2`
Expected: PASS

- [ ] **Step 7: Pełny zestaw testów + check**

Run: `python3 manage.py check && python3 manage.py test processes -v2`
Expected: brak błędów; wszystkie testy PASS.

- [ ] **Step 8: Weryfikacja manualna (smoke)**

Run: `python3 manage.py runserver` i przejdź: operacja z czynnościami → „Wgraj nagranie do analizy" → upload pliku MP4 → „Zatwierdź i rozpocznij analizę" → na stronie review pojawia się spinner „Analiza w toku…", który po chwili sam zamienia się w „Otwórz wynik analizy".

- [ ] **Step 9: (opcjonalnie) Commit**

```bash
git add processes/views.py templates/processes/video_review.html processes/tests.py
git commit -m "feat: analiza w tle z pollingiem statusu na review"
```

---

## Self-Review (wynik)

**Pokrycie specyfikacji (Etap 1):**
- 1.1 Nawigacja (usunięcie globalnego wejścia) → Task 1. ✓
- 1.2 CTA „następny krok" na operacji → Task 2. ✓
- 1.3 Analiza w tle (wątek, worker, endpoint statusu, HTMX polling, HTMX w base) → Tasks 1, 3, 4, 5. ✓
- 1.4 Testy (start/koniec w tle, fragment statusu per status, blokada bez czynności) → Tasks 2–5. ✓

**Placeholdery:** brak — każdy krok ma konkretny kod i komendę.

**Spójność nazw:** `run_analysis_in_background` i `_analysis_worker` (services) używane identycznie w Tasks 3 i 5; URL `analysis_status` i `video_approve_and_analyze` spójne między widokiem, trasą i szablonem `_analysis_status.html`.

**Uwaga o stanie:** po przepięciu na tryb async stary, synchroniczny przepływ `run_video_analysis` pozostaje używany przez worker — bez zmian logiki analizy, zmienia się tylko sposób wywołania.
