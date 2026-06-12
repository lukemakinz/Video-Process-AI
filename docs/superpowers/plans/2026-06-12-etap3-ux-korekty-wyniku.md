# Etap 3 — UX korekty wyniku — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Szybsza korekta wyniku: sekcja „Wymaga sprawdzenia" na górze, podświetlanie aktywnego segmentu podczas odtwarzania, szybka zmiana przypisanej czynności bez rozwijania pełnej korekty.

**Architecture:** Helper `segments_needing_review` w `services.py`; widok analizy przekazuje listę do sprawdzenia i operację; nowy endpoint `segment_reassign` (HTMX) aktualizuje czynność i zwraca fragment komórki; JS na zdarzeniu `timeupdate` podświetla wiersz i pasek na osi.

**Tech Stack:** Django 6, HTMX (CDN), Tailwind (CDN), waniliowy JS.

**Uwagi wykonawcze:**
- Testy: `python3 manage.py test processes -v2`. Commity opcjonalne (brak gita).

---

### Task 1: Helper `segments_needing_review`

**Files:**
- Modify: `processes/services.py`
- Test: `processes/tests.py`

- [ ] **Step 1: Napisz test (czerwony)**

Dopisz do `processes/tests.py` w klasie `ProcessDemoTests`:

```python
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
```

- [ ] **Step 2: Uruchom test — ma nie przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_segments_needing_review_flags_low_confidence_and_uncertain -v2`
Expected: FAIL — `ImportError: cannot import name 'segments_needing_review'`.

- [ ] **Step 3: Dodaj helper do `services.py`**

Na końcu `processes/services.py` dodaj:

```python
def segments_needing_review(analysis, threshold=0.4):
    """Segmenty wymagające uwagi człowieka: niska pewność lub czynność 'niepewne'."""
    flagged = []
    for segment in analysis.segments.select_related("activity"):
        if segment.confidence < threshold or "niepew" in segment.activity_name.casefold():
            flagged.append(segment)
    return flagged
```

- [ ] **Step 4: Uruchom test — ma przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_segments_needing_review_flags_low_confidence_and_uncertain -v2`
Expected: PASS

- [ ] **Step 5: (opcjonalnie) Commit**

```bash
git add processes/services.py processes/tests.py
git commit -m "feat: helper segments_needing_review"
```

---

### Task 2: Endpoint szybkiej zmiany czynności + fragment komórki

**Files:**
- Create: `templates/processes/_segment_activity_cell.html`
- Modify: `processes/views.py`
- Modify: `processes/urls.py`
- Test: `processes/tests.py`

- [ ] **Step 1: Napisz test (czerwony)**

Dopisz do `processes/tests.py`:

```python
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
```

- [ ] **Step 2: Uruchom test — ma nie przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_segment_reassign_updates_activity -v2`
Expected: FAIL — brak URL `segment_reassign`.

- [ ] **Step 3: Utwórz fragment `_segment_activity_cell.html`**

Utwórz `templates/processes/_segment_activity_cell.html`:

```html
{% comment %}Komórka czynności segmentu z szybkim wyborem (HTMX). Parametry: analysis, segment, operation, saved.{% endcomment %}
<td id="seg-activity-{{ segment.pk }}" class="px-3 py-2.5 border-b border-slate-100 align-middle">
  <div class="flex items-center gap-2">
    <select name="activity"
            class="select !py-1 !px-2 text-xs max-w-[12rem]"
            hx-post="{% url 'segment_reassign' analysis.pk segment.pk %}"
            hx-target="#seg-activity-{{ segment.pk }}"
            hx-swap="outerHTML"
            hx-trigger="change">
      {% for a in operation.activities.all %}
        <option value="{{ a.pk }}" {% if segment.activity_id == a.pk %}selected{% endif %}>{{ a.name }}</option>
      {% endfor %}
    </select>
    {% if saved %}<span class="text-xs text-emerald-600 font-medium whitespace-nowrap">✓ zapisano</span>{% endif %}
  </div>
</td>
```

- [ ] **Step 4: Dodaj widok `segment_reassign`**

W `processes/views.py` dodaj (np. po `segment_update`):

```python
@require_POST
def segment_reassign(request, analysis_pk, segment_pk):
    analysis = get_object_or_404(
        Analysis.objects.select_related("video", "video__operation"),
        pk=analysis_pk,
    )
    segment = get_object_or_404(analysis.segments, pk=segment_pk)
    operation = analysis.video.operation
    activity = get_object_or_404(operation.activities, pk=request.POST.get("activity"))
    segment.activity = activity
    segment.activity_name = activity.name
    segment.save(update_fields=["activity", "activity_name", "updated_at"])
    return render(
        request,
        "processes/_segment_activity_cell.html",
        {"analysis": analysis, "segment": segment, "operation": operation, "saved": True},
    )
```

- [ ] **Step 5: Dodaj trasę**

W `processes/urls.py`, po trasie `segment_update`, dodaj:

```python
    path(
        "analyses/<int:analysis_pk>/segments/<int:segment_pk>/reassign/",
        views.segment_reassign,
        name="segment_reassign",
    ),
```

- [ ] **Step 6: Uruchom test — ma przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_segment_reassign_updates_activity -v2`
Expected: PASS

- [ ] **Step 7: (opcjonalnie) Commit**

```bash
git add templates/processes/_segment_activity_cell.html processes/views.py processes/urls.py processes/tests.py
git commit -m "feat: szybka zmiana czynnosci segmentu (HTMX)"
```

---

### Task 3: Widok analizy przekazuje „wymaga sprawdzenia" + operację

**Files:**
- Modify: `processes/views.py`
- Test: `processes/tests.py`

- [ ] **Step 1: Napisz test (czerwony)**

Dopisz do `processes/tests.py`:

```python
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
        # wiersze tabeli mają atrybuty danych do podświetlania w czasie
        self.assertIn('data-start=', body)
        self.assertIn('data-end=', body)
```

- [ ] **Step 2: Uruchom test — ma nie przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_analysis_detail_shows_needs_review_section -v2`
Expected: FAIL — brak tekstu i atrybutów (szablon jeszcze nie zmieniony).

- [ ] **Step 3: Zaktualizuj kontekst widoku `analysis_detail`**

W `processes/views.py` w funkcji `analysis_detail`, w słowniku kontekstu przekazywanym do `render`, dodaj klucze `needs_review` i `operation`. Najpierw dodaj import helpera do bloku `from .services import (...)`:

```python
    segments_needing_review,
```

Następnie w `analysis_detail` przed `return render(...)` dodaj:

```python
    needs_review = segments_needing_review(analysis)
```

i rozszerz słownik kontekstu o:

```python
            "needs_review": needs_review,
            "operation": operation,
```

(`operation` jest już zdefiniowane w tej funkcji jako `analysis.video.operation`.)

- [ ] **Step 4: (implementacja szablonu w Task 4 — tu tylko kontekst)**

Test z Kroku 1 wymaga też zmian w szablonie (Task 4). Po Task 4 wróć i uruchom ten test.

- [ ] **Step 5: (opcjonalnie) Commit**

```bash
git add processes/views.py processes/tests.py
git commit -m "feat: kontekst needs_review w widoku analizy"
```

---

### Task 4: Szablon analizy — sekcja „Wymaga sprawdzenia", inline select, podświetlanie

**Files:**
- Modify: `templates/processes/analysis_detail.html`
- Test: `processes/tests.py` (test z Task 3)

- [ ] **Step 1: Dodaj sekcję „Wymaga sprawdzenia" na górze treści**

W `templates/processes/analysis_detail.html`, bezpośrednio po bloku nagłówka (po `</section>` zamykającej sekcję z `<h1>Wynik analizy</h1>` i statusem), wstaw:

```html
{% if needs_review %}
  <section class="rounded-xl border border-amber-200 bg-amber-50 p-4 sm:p-5 mb-6">
    <div class="flex items-center gap-2 mb-3">
      <svg class="h-5 w-5 text-amber-600" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z"/></svg>
      <h2 class="text-amber-800">Wymaga sprawdzenia — {{ needs_review|length }} fragment(y)</h2>
    </div>
    <ul class="space-y-1.5">
      {% for segment in needs_review %}
        <li class="flex items-center justify-between gap-3 text-sm">
          <button type="button" class="font-mono text-amber-800 hover:underline cursor-pointer" onclick="seekTo({{ segment.start_seconds|js_number }})">
            {{ segment.start_seconds|seconds_time }}–{{ segment.end_seconds|seconds_time }}
          </button>
          <span class="text-amber-700">{{ segment.activity_name }}</span>
          <span class="conf {{ segment.confidence|confidence_level }}">{{ segment.confidence|confidence_percent }}</span>
        </li>
      {% endfor %}
    </ul>
  </section>
{% endif %}
```

- [ ] **Step 2: Dodaj atrybuty danych do wierszy tabeli segmentów**

W `templates/processes/analysis_detail.html` znajdź wiersz tabeli segmentów `<tr id="segment-{{ segment.pk }}">` i zamień na:

```html
          <tr id="segment-{{ segment.pk }}" class="segment-row" data-start="{{ segment.start_seconds|js_number }}" data-end="{{ segment.end_seconds|js_number }}">
```

- [ ] **Step 3: Zamień komórkę czynności na szybki wybór (inline select)**

W tej samej tabeli znajdź komórkę `<td><strong class="font-medium text-slate-800">{{ segment.activity_name }}</strong></td>` i zastąp ją włączeniem fragmentu:

```html
            {% include "processes/_segment_activity_cell.html" with analysis=analysis segment=segment operation=operation saved=False %}
```

- [ ] **Step 4: Dodaj atrybuty danych do pasków gantt**

W `templates/processes/analysis_detail.html` znajdź `<button type="button" class="gantt-bar"` i dodaj atrybuty danych (zaraz po `class="gantt-bar"`):

```html
            <button type="button" class="gantt-bar" data-start="{{ bar.start_seconds|js_number }}" data-end="{{ bar.end_seconds|js_number }}"
```

(reszta atrybutów `style`, `onclick`, `title` bez zmian)

- [ ] **Step 5: Dodaj podświetlanie aktywnego segmentu (JS)**

W `templates/processes/analysis_detail.html` w bloku `{% block scripts %}`, w istniejącym `<script>`, po definicji funkcji `seekTo`, dodaj:

```javascript
  (function () {
    const player = document.getElementById("analysis-video");
    if (!player) return;
    const rows = Array.from(document.querySelectorAll(".segment-row"));
    const bars = Array.from(document.querySelectorAll(".gantt-bar"));
    function inRange(el, t) {
      return t >= parseFloat(el.dataset.start) && t < parseFloat(el.dataset.end);
    }
    player.addEventListener("timeupdate", function () {
      const t = player.currentTime;
      rows.forEach((r) => r.classList.toggle("bg-primary-50", inRange(r, t)));
      bars.forEach((b) => b.classList.toggle("ring-2", inRange(b, t)));
      bars.forEach((b) => b.classList.toggle("ring-amber-400", inRange(b, t)));
    });
  })();
```

- [ ] **Step 6: Uruchom test z Task 3 + pełny zestaw**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_analysis_detail_shows_needs_review_section -v2`
Expected: PASS

Run: `python3 manage.py check && python3 manage.py test processes -v1`
Expected: brak błędów; wszystkie testy PASS.

- [ ] **Step 7: Weryfikacja manualna (smoke)**

Run: `python3 manage.py runserver`, otwórz wynik analizy: na górze widać „Wymaga sprawdzenia", odtwarzanie wideo podświetla bieżący wiersz i pasek, zmiana czynności w select zapisuje się od razu (✓ zapisano).

- [ ] **Step 8: (opcjonalnie) Commit**

```bash
git add templates/processes/analysis_detail.html
git commit -m "feat: sekcja wymaga sprawdzenia, inline reassign, podswietlanie segmentu"
```

---

## Self-Review (wynik)

**Pokrycie specyfikacji (Etap 3):**
- 3.1 „Wymaga sprawdzenia" → Task 1 (helper) + Task 3 (kontekst) + Task 4 (sekcja). ✓
- 3.2 Podświetlanie aktywnego segmentu → Task 4 (data-atrybuty + JS). ✓
- 3.3 Szybka zmiana czynności → Task 2 (endpoint + fragment) + Task 4 (inline select). ✓
- 3.4 Testy → Task 1, 2, 3. ✓

**Placeholdery:** brak.

**Spójność nazw:** `segments_needing_review` (services↔widok), `segment_reassign` (widok↔trasa↔fragment), `_segment_activity_cell.html` (include w tabeli i zwrot endpointu), `.segment-row`/`data-start`/`data-end` spójne między szablonem a JS.

**Uwaga:** sekcja „Wymaga sprawdzenia" i inline reassign używają istniejących filtrów (`confidence_level`, `seconds_time`, `js_number`) oraz `seekTo` z Etapu/ bazy — bez nowych zależności.
