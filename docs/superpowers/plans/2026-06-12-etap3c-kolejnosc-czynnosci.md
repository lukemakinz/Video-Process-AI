# Etap 3c — Kolejność czynności (miękka podpowiedź do promptu) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Czynności mają zdefiniowaną kolejność w operacji; typowa sekwencja trafia do promptu jako miękka podpowiedź (z wyraźnym zastrzeżeniem o dozwolonych odstępstwach).

**Architecture:** Pole `Activity.order` + sortowanie; przesuwanie ↑/↓ na stronie operacji (`activity_move`); `build_analysis_prompt` wypisuje czynności w kolejności i dodaje sekcję „Typowa kolejność".

**Tech Stack:** Django 6, Tailwind (CDN).

**Uwagi:** Testy `python3 manage.py test processes -v2`. Commity opcjonalne (brak gita).

---

### Task 1: Pole `Activity.order` + migracja + kolejność przy tworzeniu

**Files:**
- Modify: `processes/models.py`
- Modify: `processes/views.py`
- Test: `processes/tests.py`

- [x] **Step 1: Napisz test (czerwony)**

Dopisz do `processes/tests.py` w klasie `ProcessDemoTests`:

```python
    # --- Etap 3c: kolejność czynności ---

    def test_activity_create_sets_next_order(self):
        url = f"/operations/{self.operation.pk}/activities/new/"
        r = self.client.post(url, {"action": "save", "name": "nowa czynnosc", "performed_by": "operator"})
        self.assertEqual(r.status_code, 302)
        new = self.operation.activities.get(name="nowa czynnosc")
        self.assertEqual(new.order, 2)
```

- [x] **Step 2: Uruchom test — ma nie przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_activity_create_sets_next_order -v2`
Expected: FAIL — `Activity` nie ma pola `order` (FieldError) lub `order` ≠ 2.

- [x] **Step 3: Dodaj pole i sortowanie w `models.py`**

W `processes/models.py` w klasie `Activity` dodaj pole (np. po `performed_by`):

```python
    order = models.PositiveIntegerField("kolejność", default=1)
```

i zmień `Meta.ordering` z `["name"]` na:

```python
        ordering = ["order", "name"]
```

- [x] **Step 4: Migracja**

Run: `python3 manage.py makemigrations processes && python3 manage.py migrate`
Expected: utworzono i zastosowano `0004_activity_order`.

- [x] **Step 5: Ustaw kolejny `order` przy tworzeniu w `_activity_form`**

W `processes/views.py` w funkcji `_activity_form`, w bloku zapisu nowej czynności (`elif request.method == "POST" and form.is_valid():`), ustaw `order` dla nowej czynności. Zastąp:

```python
    elif request.method == "POST" and form.is_valid():
        activity_obj = form.save(commit=False)
        activity_obj.operation = operation
        activity_obj.save()
        messages.success(request, "Czynność została zapisana.")
        return redirect(operation)
```

na:

```python
    elif request.method == "POST" and form.is_valid():
        activity_obj = form.save(commit=False)
        activity_obj.operation = operation
        if activity is None:
            current_max = operation.activities.aggregate(m=Max("order"))["m"] or 0
            activity_obj.order = current_max + 1
        activity_obj.save()
        messages.success(request, "Czynność została zapisana.")
        return redirect(operation)
```

(`Max` jest już importowane w `views.py` z `django.db.models`.)

- [x] **Step 6: Uruchom test — ma przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_activity_create_sets_next_order -v2`
Expected: PASS

- [ ] **Step 7: (opcjonalnie) Commit**

```bash
git add processes/models.py processes/views.py processes/migrations/ processes/tests.py
git commit -m "feat: pole Activity.order + kolejnosc przy tworzeniu"
```

---

### Task 2: Przesuwanie czynności ↑/↓ (`activity_move`)

**Files:**
- Modify: `processes/views.py`
- Modify: `processes/urls.py`
- Test: `processes/tests.py`

- [x] **Step 1: Napisz test (czerwony)**

Dopisz do `processes/tests.py`:

```python
    def test_activity_move_swaps_order(self):
        # początkowo (order,name): [praca maszyny, załadunek detalu]
        self.client.post(f"/activities/{self.load.pk}/move/up/")
        self.load.refresh_from_db()
        self.machine.refresh_from_db()
        self.assertLess(self.load.order, self.machine.order)
```

- [x] **Step 2: Uruchom test — ma nie przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_activity_move_swaps_order -v2`
Expected: FAIL — brak trasy `activity_move`.

- [x] **Step 3: Dodaj widok `activity_move`**

W `processes/views.py` dodaj (po `activity_delete`):

```python
@require_POST
def activity_move(request, pk, direction):
    activity = get_object_or_404(Activity.objects.select_related("operation"), pk=pk)
    siblings = list(activity.operation.activities.all())
    index = siblings.index(activity)
    target_index = index - 1 if direction == "up" else index + 1
    if 0 <= target_index < len(siblings):
        siblings[index], siblings[target_index] = siblings[target_index], siblings[index]
    for position, item in enumerate(siblings, start=1):
        if item.order != position:
            item.order = position
            item.save(update_fields=["order", "updated_at"])
    return redirect(activity.operation)
```

- [x] **Step 4: Dodaj trasę**

W `processes/urls.py`, po trasie `activity_delete`, dodaj:

```python
    path(
        "activities/<int:pk>/move/<str:direction>/",
        views.activity_move,
        name="activity_move",
    ),
```

- [x] **Step 5: Uruchom test — ma przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_activity_move_swaps_order -v2`
Expected: PASS

- [ ] **Step 6: (opcjonalnie) Commit**

```bash
git add processes/views.py processes/urls.py processes/tests.py
git commit -m "feat: przesuwanie czynnosci gora/dol"
```

---

### Task 3: „Typowa kolejność" w promptcie

**Files:**
- Modify: `processes/services.py`
- Test: `processes/tests.py`

- [x] **Step 1: Napisz test (czerwony)**

Dopisz do `processes/tests.py`:

```python
    def test_prompt_includes_activity_order_hint(self):
        self.machine.order = 1; self.machine.save(update_fields=["order"])
        self.load.order = 2; self.load.save(update_fields=["order"])
        prompt = build_analysis_prompt(self.operation)
        self.assertIn("Typowa kolejność", prompt)
        self.assertIn("nie sztywna reguła", prompt)
        self.assertLess(prompt.index("praca maszyny"), prompt.index("załadunek detalu"))
```

- [x] **Step 2: Uruchom test — ma nie przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_prompt_includes_activity_order_hint -v2`
Expected: FAIL — brak sekcji „Typowa kolejność".

- [x] **Step 3: Dodaj sekcję kolejności w `build_analysis_prompt`**

W `processes/services.py` w `build_analysis_prompt` zmień pierwszą linię z:

```python
    activities = operation.activities.all()
```

na:

```python
    activities = list(operation.activities.all())
```

Następnie, bezpośrednio przed `lines.extend([` z `"Zasady segmentacji:"`, dodaj:

```python
    if len(activities) > 1:
        sequence = " → ".join(activity.name for activity in activities)
        lines.extend(
            [
                f"Typowa kolejność czynności (podpowiedź, nie sztywna reguła): {sequence}.",
                "Kolejność może się nie zachować — dozwolone są odstępstwa: czekanie, chodzenie, poprawki, powtórzenia lub pominięcia kroków.",
                "",
            ]
        )
```

- [x] **Step 4: Uruchom test — ma przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_prompt_includes_activity_order_hint -v2`
Expected: PASS

- [ ] **Step 5: (opcjonalnie) Commit**

```bash
git add processes/services.py processes/tests.py
git commit -m "feat: typowa kolejnosc czynnosci w promptcie"
```

---

### Task 4: Strzałki ↑/↓ przy czynnościach na stronie operacji

**Files:**
- Modify: `templates/processes/operation_detail.html`
- Test: weryfikacja `check` + pełne testy + smoke

- [x] **Step 1: Dodaj kolumnę kolejności w tabeli czynności**

W `templates/processes/operation_detail.html`, w tabeli czynności, w `<thead>` dodaj pierwszą kolumnę przed `<th>Nazwa</th>`:

```html
            <th class="w-24">#</th>
```

oraz w `<tbody>` w wierszu czynności dodaj pierwszą komórkę przed `<td><span class="font-medium text-slate-800">{{ activity.name }}</span></td>`:

```html
              <td>
                <div class="flex items-center gap-1.5">
                  <span class="font-mono font-semibold text-slate-700 w-5">{{ activity.order }}</span>
                  <form method="post" action="{% url 'activity_move' activity.pk 'up' %}">
                    {% csrf_token %}<button class="grid place-items-center h-6 w-6 rounded border border-slate-200 text-slate-500 hover:bg-slate-100 cursor-pointer" title="Wyżej" type="submit">↑</button>
                  </form>
                  <form method="post" action="{% url 'activity_move' activity.pk 'down' %}">
                    {% csrf_token %}<button class="grid place-items-center h-6 w-6 rounded border border-slate-200 text-slate-500 hover:bg-slate-100 cursor-pointer" title="Niżej" type="submit">↓</button>
                  </form>
                </div>
              </td>
```

- [x] **Step 2: Weryfikacja `check` + pełne testy**

Run: `python3 manage.py check && python3 manage.py test processes -v1`
Expected: brak błędów; wszystkie testy PASS.

- [x] **Step 3: Weryfikacja manualna (smoke)**

Run: `python3 manage.py runserver`, wejdź w operację → tabela czynności ma numerację i strzałki ↑/↓; zmiana kolejności przestawia czynności; nowo dodana analiza ma w promptcie sekcję „Typowa kolejność".

- [ ] **Step 4: (opcjonalnie) Commit**

```bash
git add templates/processes/operation_detail.html
git commit -m "feat: strzalki kolejnosci czynnosci na stronie operacji"
```

---

## Self-Review (wynik)

**Pokrycie specyfikacji (Etap 3c):**
- 3c.1 Model `Activity.order` + ordering + kolejność przy tworzeniu → Task 1. ✓
- 3c.2 Przesuwanie ↑/↓ (`activity_move`) → Task 2 + Task 4 (UI). ✓
- 3c.3 Prompt „Typowa kolejność" (miękka, z zastrzeżeniem) → Task 3. ✓
- 3c.4 Testy → Task 1, 2, 3. ✓

**Placeholdery:** brak.

**Spójność nazw:** `activity_move` (widok↔trasa↔szablon), `Activity.order` używane w modelu, widoku tworzenia, `activity_move` i promptcie.

**Uwaga:** sekcja kolejności pojawia się tylko gdy operacja ma >1 czynność; kolejność jest wypisywana zgodnie z `Meta.ordering = ["order", "name"]`.
