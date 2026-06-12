# Etap 2 — Asystent AI: Generuj + Popraw (GPT) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Opisy czynności wspiera OpenAI GPT w dwóch trybach — „Generuj z AI" (od zera) i „Popraw z AI" (szlifowanie istniejącej treści), z regeneracją pojedynczego pola przez HTMX. Analiza wideo pozostaje na Gemini.

**Architecture:** Nowy klient OpenAI i funkcja `assist_activity(operation, fields, mode, target=None)` w `services.py`. Bez `OPENAI_API_KEY` działa deterministyczny mock. Widok formularza obsługuje akcje `ai_suggest`/`ai_refine`; osobny endpoint HTMX regeneruje pojedyncze pole.

**Tech Stack:** Django 6, OpenAI SDK (`openai`, import leniwy), HTMX (CDN, dodany w Etapie 1), tryb mock w testach (brak klucza).

**Uwagi wykonawcze:**
- Testy: `python3 manage.py test processes -v2`. Działają w trybie mock (brak `OPENAI_API_KEY`), bez realnych wywołań API i bez konieczności instalacji `openai` (import jest leniwy, wewnątrz `_openai_client`).
- Commity opcjonalne (brak gita).

---

### Task 1: Ustawienia OpenAI + klient + `assist_activity` (services)

**Files:**
- Modify: `video_process_demo/settings.py`
- Modify: `processes/services.py`
- Test: `processes/tests.py`

- [ ] **Step 1: Napisz testy (czerwone)**

Dopisz do `processes/tests.py` w klasie `ProcessDemoTests`:

```python
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

    def test_assist_activity_refine_preserves_input(self):
        from processes.services import assist_activity
        result = assist_activity(
            self.operation,
            {"name": "krojenie pomidora", "description": "kroi pomidora nożem"},
            mode="refine",
        )
        # mock w trybie refine wzbogaca, ale zachowuje wejściowy opis
        self.assertIn("kroi pomidora nożem", result["description"])

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
```

- [ ] **Step 2: Uruchom testy — mają nie przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_assist_activity_generate_returns_all_fields processes.tests.ProcessDemoTests.test_assist_activity_refine_preserves_input processes.tests.ProcessDemoTests.test_assist_activity_target_returns_single_field -v2`
Expected: FAIL — `ImportError: cannot import name 'assist_activity'`.

- [ ] **Step 3: Dodaj ustawienia OpenAI**

W `video_process_demo/settings.py`, po bloku zmiennych `GEMINI_*` (przed lub po `_float_env`), dodaj:

```python
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-4o-mini")
```

- [ ] **Step 4: Dodaj klienta i `assist_activity` do `services.py`**

W `processes/services.py` znajdź istniejącą funkcję `suggest_activity_description` i zastąp całość (od `def suggest_activity_description` do jej `return`) poniższym kodem (dodaje klienta OpenAI, rdzeń `assist_activity`, mock i wstecznie zgodny `suggest_activity_description`):

```python
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
        return _assist_mock(fields, mode, target)

    intent = (
        "Popraw i doszlifuj istniejący opis, zachowując intencję autora."
        if mode == "refine"
        else "Przygotuj opis od zera."
    )
    scope = (
        f"Zwróć wyłącznie pole '{target}'."
        if target
        else "Zwróć wszystkie pola."
    )
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
```

- [ ] **Step 5: Uruchom testy — mają przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_assist_activity_generate_returns_all_fields processes.tests.ProcessDemoTests.test_assist_activity_refine_preserves_input processes.tests.ProcessDemoTests.test_assist_activity_target_returns_single_field -v2`
Expected: PASS (3)

- [ ] **Step 6: (opcjonalnie) Commit**

```bash
git add video_process_demo/settings.py processes/services.py processes/tests.py
git commit -m "feat: asystent opisow na OpenAI (generate/refine/target)"
```

---

### Task 2: Zależność i `.env.example`

**Files:**
- Modify: `requirements.txt`
- Modify: `.env.example`

- [ ] **Step 1: Dodaj zależność**

W `requirements.txt` dodaj linię (po `google-genai`):

```
openai==1.59.6
```

- [ ] **Step 2: Dodaj zmienne do `.env.example`**

W `.env.example`, po linii `GOOGLE_API_KEY=`, dodaj:

```
# OpenAI — używane WYŁĄCZNIE do opisów czynności (analiza wideo zostaje na Gemini).
OPENAI_API_KEY=your-openai-api-key-here
OPENAI_TEXT_MODEL=gpt-4o-mini
```

- [ ] **Step 3: Weryfikacja (sanity)**

Run: `python3 manage.py check`
Expected: brak błędów.

- [ ] **Step 4: (opcjonalnie) Commit**

```bash
git add requirements.txt .env.example
git commit -m "chore: zaleznosc openai i zmienne srodowiskowe"
```

---

### Task 3: Endpoint HTMX regeneracji pojedynczego pola

**Files:**
- Modify: `processes/views.py`
- Modify: `processes/urls.py`
- Test: `processes/tests.py`

- [ ] **Step 1: Napisz test (czerwony)**

Dopisz do `processes/tests.py`:

```python
    def test_activity_ai_field_returns_single_field_text(self):
        url = f"/operations/{self.operation.pk}/activities/ai-field/"
        r = self.client.post(url, {
            "target": "exclusion_rules",
            "name": "krojenie pomidora",
            "description": "kroi pomidora",
        })
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        # zwraca sam tekst pola (mock zawiera frazę z reguł wykluczenia)
        self.assertIn("operator wykonuje inną zdefiniowaną czynność", body)
```

- [ ] **Step 2: Uruchom test — ma nie przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_activity_ai_field_returns_single_field_text -v2`
Expected: FAIL — brak URL `activity_ai_field` (NoReverseMatch/404).

- [ ] **Step 3: Dodaj widok**

W `processes/views.py` dodaj import na górze (jeśli brak):

```python
from django.http import HttpResponse
```

oraz import `assist_activity` z serwisów (dodaj do istniejącego bloku `from .services import (...)`):

```python
    assist_activity,
```

Dodaj funkcję (np. po `activity_edit`):

```python
@require_POST
def activity_ai_field(request, operation_id):
    operation = get_object_or_404(Operation.objects.select_related("process"), pk=operation_id)
    target = request.POST.get("target")
    if target not in {"description", "recognition_rules", "exclusion_rules"}:
        return HttpResponse("", status=400)
    fields = {
        "name": request.POST.get("name", ""),
        "quick_description": request.POST.get("quick_description", ""),
        "description": request.POST.get("description", ""),
        "recognition_rules": request.POST.get("recognition_rules", ""),
        "exclusion_rules": request.POST.get("exclusion_rules", ""),
    }
    mode = "refine" if fields.get(target) else "generate"
    try:
        result = assist_activity(operation, fields, mode=mode, target=target)
        return HttpResponse(result.get(target, ""))
    except Exception as exc:
        return HttpResponse(f"Nie udało się wygenerować pola: {exc}", status=200)
```

- [ ] **Step 4: Dodaj trasę**

W `processes/urls.py`, po trasie `activity_create`, dodaj:

```python
    path(
        "operations/<int:operation_id>/activities/ai-field/",
        views.activity_ai_field,
        name="activity_ai_field",
    ),
```

- [ ] **Step 5: Uruchom test — ma przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_activity_ai_field_returns_single_field_text -v2`
Expected: PASS

- [ ] **Step 6: (opcjonalnie) Commit**

```bash
git add processes/views.py processes/urls.py processes/tests.py
git commit -m "feat: endpoint HTMX regeneracji pojedynczego pola AI"
```

---

### Task 4: Widok formularza — tryb „Popraw z AI" (akcja `ai_refine`)

**Files:**
- Modify: `processes/views.py`
- Test: `processes/tests.py`

- [ ] **Step 1: Napisz test (czerwony)**

Dopisz do `processes/tests.py`:

```python
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
        # po refine pole description zawiera doprecyzowaną wersję wejścia
        self.assertIn("kroi pomidora nożem", body)
        self.assertIn("doprecyzowano", body)
        # nie zapisał czynności (tryb AI tylko proponuje)
        self.assertFalse(self.operation.activities.filter(name="krojenie pomidora").exists())
```

- [ ] **Step 2: Uruchom test — ma nie przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_activity_form_ai_refine_fills_fields -v2`
Expected: FAIL — akcja `ai_refine` nieobsługiwana (formularz zapisze lub zignoruje).

- [ ] **Step 3: Zaktualizuj `_activity_form`**

W `processes/views.py` znajdź funkcję `_activity_form`. Zastąp blok obsługujący `ai_suggest` poniższym (obsługuje oba tryby przez `assist_activity`):

```python
    if request.method == "POST" and request.POST.get("action") in {"ai_suggest", "ai_refine"}:
        mode = "refine" if request.POST.get("action") == "ai_refine" else "generate"
        try:
            suggestion = assist_activity(
                operation=operation,
                fields={
                    "name": request.POST.get("name", ""),
                    "quick_description": request.POST.get("quick_description", ""),
                    "description": request.POST.get("description", ""),
                    "recognition_rules": request.POST.get("recognition_rules", ""),
                    "exclusion_rules": request.POST.get("exclusion_rules", ""),
                },
                mode=mode,
            )
            data = request.POST.copy()
            for field_name in ("description", "recognition_rules", "exclusion_rules"):
                if suggestion.get(field_name):
                    data[field_name] = suggestion[field_name]
            form = ActivityForm(data, instance=activity)
            messages.info(request, "AI przygotowało propozycję. Zapisz ją dopiero po akceptacji.")
        except Exception as exc:
            messages.error(request, f"Nie udało się wygenerować opisu AI: {exc}")
```

Uwaga: pozostała część funkcji (`elif request.method == "POST" and form.is_valid(): ... save`) zostaje bez zmian. Upewnij się, że `assist_activity` jest zaimportowane (Task 3, Step 3).

- [ ] **Step 4: Uruchom test — ma przejść**

Run: `python3 manage.py test processes.tests.ProcessDemoTests.test_activity_form_ai_refine_fills_fields -v2`
Expected: PASS

- [ ] **Step 5: (opcjonalnie) Commit**

```bash
git add processes/views.py processes/tests.py
git commit -m "feat: tryb Popraw z AI w formularzu czynnosci"
```

---

### Task 5: Szablon formularza — przyciski Generuj/Popraw + regeneracja per pole

**Files:**
- Modify: `templates/processes/activity_form.html`
- Test: weryfikacja manualna (smoke)

- [ ] **Step 1: Dodaj przycisk „Popraw z AI" obok „Generuj z AI"**

W `templates/processes/activity_form.html` znajdź blok z przyciskiem `name="action" value="ai_suggest"` (w ramce „Asystent AI"). Zastąp pojedynczy przycisk dwoma:

```html
          <div class="flex items-center gap-2">
            <button class="btn secondary small" type="submit" name="action" value="ai_suggest">
              <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 00-2.456 2.456z"/></svg>
              Generuj z AI
            </button>
            <button class="btn ghost small" type="submit" name="action" value="ai_refine">
              <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L10.582 16.07a4.5 4.5 0 01-1.897 1.13L6 18l.8-2.685a4.5 4.5 0 011.13-1.897l8.932-8.931z"/></svg>
              Popraw z AI
            </button>
          </div>
```

- [ ] **Step 2: Dodaj przyciski regeneracji per pole (HTMX)**

W `templates/processes/activity_form.html` w pętli `{% for field in form.visible_fields %}` dodaj, bezpośrednio po linii `{{ field }}`, mały przycisk regeneracji dla trzech pól tekstowych:

```html
      {% if field.name == 'description' or field.name == 'recognition_rules' or field.name == 'exclusion_rules' %}
        <button type="button"
                class="mt-1 text-xs font-medium text-primary hover:underline cursor-pointer"
                hx-post="{% url 'activity_ai_field' operation.pk %}"
                hx-include="closest form"
                hx-vals='{"target": "{{ field.name }}"}'
                hx-target="#{{ field.id_for_label }}"
                hx-swap="innerHTML">
          ✦ Popraw to pole z AI
        </button>
      {% endif %}
```

- [ ] **Step 3: Weryfikacja `check` + pełne testy**

Run: `python3 manage.py check && python3 manage.py test processes -v1`
Expected: brak błędów; wszystkie testy PASS.

- [ ] **Step 4: Weryfikacja manualna (smoke)**

Run: `python3 manage.py runserver`, wejdź w operację → „Dodaj czynność". Wpisz nazwę + krótki opis → „Generuj z AI" wypełnia pola. Zmień opis → „Popraw z AI" szlifuje. Klik „✦ Popraw to pole z AI" pod pojedynczym polem regeneruje tylko to pole (bez klucza OpenAI działa na mocku).

- [ ] **Step 5: (opcjonalnie) Commit**

```bash
git add templates/processes/activity_form.html
git commit -m "feat: przyciski Generuj/Popraw z AI i regeneracja per pole"
```

---

## Self-Review (wynik)

**Pokrycie specyfikacji (Etap 2):**
- 2.1 Dwa tryby Generuj/Popraw → Task 4 (widok) + Task 5 (UI). ✓
- 2.2 Regeneracja pojedynczego pola (HTMX) → Task 3 (endpoint) + Task 5 (przyciski). ✓
- 2.3 Provider OpenAI: `_openai_client`, `assist_activity(generate|refine, target)`, ustawienia, zależność, zgodny wstecznie `suggest_activity_description`, fallback mock → Task 1 + Task 2. ✓
- Koszt asystenta poza panelem kosztów → bez zmian (zgodne). ✓

**Placeholdery:** brak — każdy krok ma pełny kod i komendę.

**Spójność nazw:** `assist_activity(operation, fields, mode, target)` używana identycznie w services (Task 1), endpoincie (Task 3) i widoku formularza (Task 4); `activity_ai_field` spójne w widoku, trasie i szablonie (Task 3, 5); klucze pól `description`/`recognition_rules`/`exclusion_rules` spójne wszędzie.

**Uwaga:** import `openai` jest leniwy (wewnątrz `_openai_client`), więc brak pakietu nie blokuje testów ani trybu mock.
