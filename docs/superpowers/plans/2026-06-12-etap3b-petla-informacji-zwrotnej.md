# Etap 3b — Pętla informacji zwrotnej (👍/👎 → wskazówki do promptu) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Korekty użytkownika realnie poprawiają kolejne analizy: 👍 potwierdza segment, 👎 z uwagą tworzy wskazówkę przy czynności; aktywne wskazówki są wstrzykiwane do promptu Gemini. Mechanizm = augmentacja promptu (human-in-the-loop), bez fine-tuningu modelu.

**Architecture:** Nowy model `ActivityHint` powiązany z czynnością. `build_analysis_prompt` dokłada per-czynność aktywne wskazówki. Endpointy HTMX: `segment_approve`, `segment_feedback` (tworzy wskazówkę), `hint_toggle`, `hint_delete`. Wskazówki kuratorowane na stronie edycji czynności.

**Tech Stack:** Django 6, HTMX (CDN), Tailwind (CDN).

**Uwagi wykonawcze:**
- Testy: `python3 manage.py test processes -v2`. Commity opcjonalne (brak gita).

---

### Task 1: Model `ActivityHint` + migracja

**Files:**
- Modify: `processes/models.py`
- Modify: `processes/admin.py`
- Test: `processes/tests.py`

- [ ] **Step 1: Napisz test (czerwony)**

Dopisz do `processes/tests.py` w klasie `ProcessDemoTests`:

```python
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
```

- [ ] **Step 2: Uruchom test — ma nie przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_activity_hint_creation_and_str -v2`
Expected: FAIL — `ImportError: cannot import name 'ActivityHint'`.

- [ ] **Step 3: Dodaj model na końcu `processes/models.py`**

```python
class ActivityHint(TimeStampedModel):
    activity = models.ForeignKey(
        Activity,
        verbose_name="czynność",
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
```

- [ ] **Step 4: Zarejestruj w adminie**

W `processes/admin.py` dodaj (jeśli plik używa `admin.site.register`, dopasuj styl istniejących wpisów; jeśli pusty/inny, dodaj import i rejestrację):

```python
from .models import ActivityHint

admin.site.register(ActivityHint)
```

- [ ] **Step 5: Migracja**

Run: `python3 manage.py makemigrations processes`
Expected: utworzony plik migracji `0003_activityhint`.

Run: `python3 manage.py migrate`
Expected: zastosowano.

- [ ] **Step 6: Uruchom test — ma przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_activity_hint_creation_and_str -v2`
Expected: PASS

- [ ] **Step 7: (opcjonalnie) Commit**

```bash
git add processes/models.py processes/admin.py processes/migrations/ processes/tests.py
git commit -m "feat: model ActivityHint + migracja"
```

---

### Task 2: Wstrzyknięcie aktywnych wskazówek do promptu

**Files:**
- Modify: `processes/services.py`
- Test: `processes/tests.py`

- [ ] **Step 1: Napisz test (czerwony)**

Dopisz do `processes/tests.py`:

```python
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
```

- [ ] **Step 2: Uruchom test — ma nie przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_build_prompt_includes_active_hints_only -v2`
Expected: FAIL — prompt nie zawiera sekcji wskazówek.

- [ ] **Step 3: Dodaj wskazówki do bloku czynności w `build_analysis_prompt`**

W `processes/services.py` w funkcji `build_analysis_prompt`, w pętli `for index, activity in enumerate(...)`, zastąp blok `lines.extend([...])` poniższym (dodaje aktywne wskazówki przed pustą linią):

```python
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
```

- [ ] **Step 4: Uruchom test — ma przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_build_prompt_includes_active_hints_only -v2`
Expected: PASS

- [ ] **Step 5: (opcjonalnie) Commit**

```bash
git add processes/services.py processes/tests.py
git commit -m "feat: wstrzykiwanie aktywnych wskazowek do promptu"
```

---

### Task 3: Endpointy 👍 (approve) i 👎 (feedback)

**Files:**
- Create: `templates/processes/_segment_feedback.html`
- Modify: `processes/views.py`
- Modify: `processes/urls.py`
- Test: `processes/tests.py`

- [ ] **Step 1: Napisz testy (czerwone)**

Dopisz do `processes/tests.py`:

```python
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
        self.assertIn("potwierdzono", r.content.decode())

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
```

- [ ] **Step 2: Uruchom testy — mają nie przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_segment_approve_sets_is_approved processes.tests.ProcessDemoTests.test_segment_feedback_creates_hint -v2`
Expected: FAIL — brak tras `segment_approve` / `segment_feedback`.

- [ ] **Step 3: Utwórz fragment `_segment_feedback.html`**

Utwórz `templates/processes/_segment_feedback.html`:

```html
{% comment %}Widżet oceny segmentu (HTMX). Parametry: analysis, segment, operation, hint_saved.{% endcomment %}
<div id="seg-feedback-{{ segment.pk }}" class="flex flex-col gap-1">
  {% if hint_saved %}
    <span class="text-xs text-emerald-700 font-medium">✓ Uwaga zapisana — AI uwzględni ją przy następnej analizie.</span>
  {% elif segment.is_approved %}
    <span class="text-xs text-emerald-700 font-medium">✓ Potwierdzono</span>
  {% else %}
    <div class="flex items-center gap-2">
      <button type="button"
              class="btn ghost small"
              hx-post="{% url 'segment_approve' analysis.pk segment.pk %}"
              hx-target="#seg-feedback-{{ segment.pk }}" hx-swap="outerHTML">
        👍 Dobrze
      </button>
      <details>
        <summary class="btn ghost small cursor-pointer list-none">👎 Popraw</summary>
        <form class="mt-2 p-3 bg-slate-50 rounded-lg flex flex-col gap-2 w-72"
              hx-post="{% url 'segment_feedback' analysis.pk segment.pk %}"
              hx-target="#seg-feedback-{{ segment.pk }}" hx-swap="outerHTML">
          <textarea name="note" rows="2" class="input text-xs" placeholder="np. pieprz jest ciemniejszy niż sól"></textarea>
          <select name="confused_with" class="select text-xs">
            <option value="">— powinno być (opcjonalnie) —</option>
            {% for a in operation.activities.all %}<option value="{{ a.pk }}">{{ a.name }}</option>{% endfor %}
          </select>
          <button type="submit" class="btn primary small">Zapisz uwagę dla AI</button>
        </form>
      </details>
    </div>
  {% endif %}
</div>
```

- [ ] **Step 4: Dodaj widoki**

W `processes/views.py` dodaj (po `segment_reassign`):

```python
@require_POST
def segment_approve(request, analysis_pk, segment_pk):
    analysis = get_object_or_404(
        Analysis.objects.select_related("video", "video__operation"), pk=analysis_pk
    )
    segment = get_object_or_404(analysis.segments, pk=segment_pk)
    segment.is_approved = True
    segment.save(update_fields=["is_approved", "updated_at"])
    return render(
        request,
        "processes/_segment_feedback.html",
        {"analysis": analysis, "segment": segment, "operation": analysis.video.operation},
    )


@require_POST
def segment_feedback(request, analysis_pk, segment_pk):
    from .models import ActivityHint

    analysis = get_object_or_404(
        Analysis.objects.select_related("video", "video__operation"), pk=analysis_pk
    )
    segment = get_object_or_404(analysis.segments, pk=segment_pk)
    operation = analysis.video.operation
    note = (request.POST.get("note") or "").strip()
    confused_with = None
    confused_id = request.POST.get("confused_with")
    if confused_id:
        confused_with = operation.activities.filter(pk=confused_id).first()
    target_activity = segment.activity or confused_with
    hint_saved = False
    if note and target_activity is not None:
        ActivityHint.objects.create(
            activity=target_activity,
            text=note,
            confused_with=confused_with if confused_with != target_activity else None,
            source_segment=segment,
        )
        hint_saved = True
    return render(
        request,
        "processes/_segment_feedback.html",
        {
            "analysis": analysis,
            "segment": segment,
            "operation": operation,
            "hint_saved": hint_saved,
        },
    )
```

- [ ] **Step 5: Dodaj trasy**

W `processes/urls.py`, po trasie `segment_reassign`, dodaj:

```python
    path(
        "analyses/<int:analysis_pk>/segments/<int:segment_pk>/approve/",
        views.segment_approve,
        name="segment_approve",
    ),
    path(
        "analyses/<int:analysis_pk>/segments/<int:segment_pk>/feedback/",
        views.segment_feedback,
        name="segment_feedback",
    ),
```

- [ ] **Step 6: Uruchom testy — mają przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_segment_approve_sets_is_approved processes.tests.ProcessDemoTests.test_segment_feedback_creates_hint -v2`
Expected: PASS (2)

- [ ] **Step 7: (opcjonalnie) Commit**

```bash
git add templates/processes/_segment_feedback.html processes/views.py processes/urls.py processes/tests.py
git commit -m "feat: endpointy oceny segmentu (approve/feedback) tworzace wskazowki"
```

---

### Task 4: Zarządzanie wskazówkami przy czynności (toggle/usuń)

**Files:**
- Modify: `processes/views.py`
- Modify: `processes/urls.py`
- Modify: `templates/processes/activity_form.html`
- Test: `processes/tests.py`

- [ ] **Step 1: Napisz testy (czerwone)**

Dopisz do `processes/tests.py`:

```python
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
```

- [ ] **Step 2: Uruchom testy — mają nie przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_hint_toggle_flips_active processes.tests.ProcessDemoTests.test_hint_delete_removes -v2`
Expected: FAIL — brak tras.

- [ ] **Step 3: Dodaj widoki**

W `processes/views.py` dodaj (np. po `activity_delete`):

```python
@require_POST
def hint_toggle(request, pk):
    from .models import ActivityHint

    hint = get_object_or_404(ActivityHint, pk=pk)
    hint.is_active = not hint.is_active
    hint.save(update_fields=["is_active", "updated_at"])
    return render(request, "processes/_hint_row.html", {"hint": hint})


@require_POST
def hint_delete(request, pk):
    from .models import ActivityHint

    hint = get_object_or_404(ActivityHint, pk=pk)
    hint.delete()
    return HttpResponse("")
```

- [ ] **Step 4: Dodaj trasy**

W `processes/urls.py`, przed zamykającym `]`, dodaj:

```python
    path("hints/<int:pk>/toggle/", views.hint_toggle, name="hint_toggle"),
    path("hints/<int:pk>/delete/", views.hint_delete, name="hint_delete"),
```

- [ ] **Step 5: Utwórz fragment wiersza wskazówki `_hint_row.html`**

Utwórz `templates/processes/_hint_row.html`:

```html
{% comment %}Wiersz wskazówki czynności. Parametr: hint.{% endcomment %}
<li id="hint-{{ hint.pk }}" class="flex items-center justify-between gap-3 py-2 border-b border-slate-100 text-sm">
  <span class="{% if not hint.is_active %}line-through text-slate-400{% else %}text-slate-700{% endif %}">
    {{ hint.text }}{% if hint.confused_with %} <span class="text-xs text-slate-400">(mylona z: {{ hint.confused_with.name }})</span>{% endif %}
  </span>
  <span class="flex items-center gap-2 shrink-0">
    <button type="button" class="btn ghost small"
            hx-post="{% url 'hint_toggle' hint.pk %}" hx-target="#hint-{{ hint.pk }}" hx-swap="outerHTML">
      {% if hint.is_active %}Wyłącz{% else %}Włącz{% endif %}
    </button>
    <button type="button" class="btn ghost small text-red-600 hover:bg-red-50 hover:border-red-200"
            hx-post="{% url 'hint_delete' hint.pk %}" hx-target="#hint-{{ hint.pk }}" hx-swap="outerHTML">
      Usuń
    </button>
  </span>
</li>
```

- [ ] **Step 6: Pokaż listę wskazówek na formularzu edycji czynności**

W `templates/processes/activity_form.html`, bezpośrednio przed końcowym blokiem przycisków (przed `<div class="flex items-center gap-2 pt-2 border-t border-slate-100">`), wstaw:

```html
  {% if activity and activity.hints.all %}
    <div class="rounded-lg border border-slate-200 bg-white p-4 mb-5">
      <p class="text-sm font-semibold text-slate-700 mb-2">Wskazówki z korekt (trafiają do promptu, gdy aktywne)</p>
      <ul id="hint-list">
        {% for hint in activity.hints.all %}
          {% include "processes/_hint_row.html" with hint=hint %}
        {% endfor %}
      </ul>
    </div>
  {% endif %}
```

- [ ] **Step 7: Uruchom testy — mają przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_hint_toggle_flips_active processes.tests.ProcessDemoTests.test_hint_delete_removes -v2`
Expected: PASS (2)

- [ ] **Step 8: (opcjonalnie) Commit**

```bash
git add processes/views.py processes/urls.py templates/processes/_hint_row.html templates/processes/activity_form.html processes/tests.py
git commit -m "feat: zarzadzanie wskazowkami przy czynnosci (toggle/usun)"
```

---

### Task 5: 👍/👎 w tabeli segmentów wyniku analizy

**Files:**
- Modify: `templates/processes/analysis_detail.html`
- Test: weryfikacja manualna (smoke)

- [ ] **Step 1: Dodaj widżet oceny w rozwijanym wierszu korekty**

W `templates/processes/analysis_detail.html` znajdź rozwijany wiersz korekty — element `<details class="group">` z `<summary ...>Popraw segment</summary>` w komórce `<td colspan="7" ...>`. Bezpośrednio po otwierającym `<td colspan="7" class="!py-0">` (a przed `<details`), wstaw widżet oceny:

```html
              <div class="py-3">
                {% include "processes/_segment_feedback.html" with analysis=analysis segment=segment operation=operation %}
              </div>
```

- [ ] **Step 2: Weryfikacja `check` + pełne testy**

Run: `python3 manage.py check && python3 manage.py test processes -v1`
Expected: brak błędów; wszystkie testy PASS.

- [ ] **Step 3: Weryfikacja manualna (smoke)**

Run: `python3 manage.py runserver`, otwórz wynik analizy → rozwiń „Popraw segment": widać 👍 Dobrze / 👎 Popraw. 👎 → wpisz uwagę + „powinno być" → „Zapisz uwagę dla AI" → komunikat o zapisaniu. Wejdź w edycję tej czynności → wskazówka jest na liście (Wyłącz/Usuń). Kolejna analiza tej operacji zawiera wskazówkę w promptcie.

- [ ] **Step 4: (opcjonalnie) Commit**

```bash
git add templates/processes/analysis_detail.html
git commit -m "feat: widzet 👍/👎 przy segmentach wyniku"
```

---

## Self-Review (wynik)

**Pokrycie specyfikacji (Etap 3b):**
- 3b.1 Ocena per segment (👍 is_approved / 👎 uwaga + confused_with) → Task 3 + Task 5. ✓
- 3b.2 Model `ActivityHint` (activity/text/confused_with/source_segment/is_active) → Task 1. ✓
- 3b.3 Wstrzyknięcie do promptu (tylko aktywne) → Task 2. ✓
- 3b.4 Kontrola human-curated (lista, toggle, usuń przy czynności) → Task 4. ✓
- 3b.5 Testy → Task 1, 2, 3, 4. ✓

**Placeholdery:** brak.

**Spójność nazw:** `ActivityHint` (model↔widoki↔testy), `segment_approve`/`segment_feedback`/`hint_toggle`/`hint_delete` spójne (widok↔trasa↔fragment), `_segment_feedback.html` i `_hint_row.html` używane w endpoincie i w szablonach. Relacje: `activity.hints`, `segment.hints`, `hint.confused_with`.

**Uwaga:** gdy `segment.activity` jest None (np. „niepewne"), wskazówka jest przypinana do `confused_with`; jeśli oba puste lub brak treści — wskazówka nie powstaje (zwracany neutralny fragment).
