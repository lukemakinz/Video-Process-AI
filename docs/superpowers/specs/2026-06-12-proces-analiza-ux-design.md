# Specyfikacja: uporządkowanie przepływu i UX analizy wideo

Data: 2026-06-12
Status: zatwierdzony projekt, przed planem wdrożenia

## Kontekst

Aplikacja Django (demo) realizuje przepływ:
`Proces → Operacja → Czynności (z opisem i kryteriami) → wgranie wideo → anonimizacja → analiza GenAI → segmenty na osi czasu → korekta`.

Model danych (istniejący, bez zmian dla Etapów 1–3):
- `Process` → `Operation` (FK, `order`) → `Activity` (FK; `description`, `recognition_rules`, `exclusion_rules`, `performed_by`, `minimum_duration_seconds`).
- `Video` (FK do `Operation`; `status`, `anonymized_file`, `approved_for_analysis_at`).
- `Analysis` (FK do `Video`; `status`, `prompt`, `input_tokens`, `output_tokens`, `estimated_cost`, `cost_is_estimated`).
- `AnalysisSegment` (FK do `Analysis` i opcjonalnie `Activity`; `start_seconds`, `end_seconds`, `confidence`, `reason`, `is_approved`).

Potwierdzony model mentalny: czynność musi być precyzyjnie opisana, bo to jedyna podstawa, na której model przypisuje fragmenty „od–do" do zamkniętej listy czynności operacji.

## Cel

Uporządkować architekturę informacji (analiza zawsze w kontekście operacji), uodpornić analizę na długie wywołania (uruchomienie w tle), wzmocnić asystenta AI (generowanie i poprawianie opisów) oraz usprawnić korektę wyniku. Wdrożenie falami z priorytetami P0→P2; każdy etap zatwierdzany osobno.

## Decyzje architektoniczne

- **Analiza w tle:** wątek (`threading.Thread`) + odpytywanie statusu przez HTMX co ~3 s. Bez Celery/Redis (wystarcza na demo na jednej maszynie; pod produkcję docelowo kolejka zadań).
- **Wejście do analizy:** wyłącznie z poziomu operacji. Globalny skrót „Analizuj nagranie" znika z topbara.
- **Modele AI (podział providerów):** opisy czynności (asystent tekstowy) — **OpenAI GPT** (domyślnie `gpt-4o-mini`, konfigurowalne); analiza wideo — **wyłącznie Gemini**. Dwa osobne klucze API (`OPENAI_API_KEY`, `GEMINI_API_KEY`). Bez odpowiedniego klucza dany tryb działa na deterministycznym mocku.
- **HTMX:** dodany do `base.html` (CDN), używany do pollingu statusu i regeneracji pojedynczych pól AI.
- **Język:** UI i komunikaty po polsku.
- **Styl:** istniejący system Tailwind (Play CDN, tryb jasny, Fira Sans/Code, komponenty w `@layer`).

---

## Etap 1 — Architektura informacji + analiza w tle (P0)

### 1.1 Nawigacja
- Z topbara (`base.html`) usunąć pozycję „Analizuj nagranie". Zostają: **Procesy**, **Admin**.
- Trasa `/videos/upload/` (bez operacji) pozostaje jako fallback, ale interfejs prowadzi zawsze przez operację. Główną trasą jest `/operations/<id>/videos/upload/`.

### 1.2 Prowadzenie „następny krok" na stronie operacji
W `operation_detail.html` dodać wyraźny blok stanu:
- gdy `operation.activities` jest puste → prominentny CTA „Dodaj czynności"; przycisk analizy nieaktywny z wyjaśnieniem „Najpierw zdefiniuj czynności".
- gdy czynności istnieją → prominentny CTA „Wgraj nagranie do analizy".

### 1.3 Analiza w tle
- `video_approve_and_analyze` (POST): walidacja jak dziś (jest `anonymized_file`, operacja ma czynności), ustawia `approved_for_analysis_at`, tworzy `Analysis(status=RUNNING)`, uruchamia `threading.Thread(target=run_video_analysis, args=(video,))`, przekierowuje na stronę „analiza w toku".
- `run_video_analysis` bez zmian logicznych; musi domykać połączenie DB wątku (`django.db.connection.close()` na końcu wątku).
- Nowy widok statusu `analysis_status(video_pk)` zwracający **fragment HTML** (HTMX): spinner gdy `RUNNING`/`ANALYZING`, link do wyniku gdy `COMPLETED`, komunikat błędu gdy `FAILED`.
- Strona „analiza w toku": rozszerzamy `video_review.html` o stan po zatwierdzeniu — kontener z `hx-get` na endpoint statusu, `hx-trigger="load, every 3s"`, który po zakończeniu podmienia treść na link „Otwórz wynik analizy". Nie tworzymy osobnego szablonu.
- `base.html`: dodać skrypt HTMX (CDN).

### 1.4 Testy Etapu 1
- analiza startuje i kończy się w tle (test z `GEMINI_USE_MOCK=true`, synchroniczne wywołanie celu wątku w teście), status przechodzi RUNNING→COMPLETED.
- endpoint statusu zwraca odpowiedni fragment dla każdego statusu.
- strona operacji bez czynności blokuje analizę i pokazuje właściwy CTA.

---

## Etap 2 — Asystent AI: generuj + popraw, per-pole (P1)

### 2.1 Dwa tryby
- „Generuj z AI" (istniejące): tworzy opis + kryteria z nazwy i krótkiego opisu.
- „Popraw z AI" (nowe): bierze **obecną** treść pól i ją doszlifowuje (zwięźlej, precyzyjniej, uzupełnia brakujące kryteria), nie tracąc intencji autora.

### 2.2 Regeneracja pojedynczego pola
- Przy polach `description`, `recognition_rules`, `exclusion_rules` mały przycisk „Popraw z AI" / „Generuj".
- Endpoint HTMX `activity_ai_field(operation_id)` przyjmuje nazwę pola + aktualne wartości formularza, zwraca treść tylko tego pola (HTMX wstawia ją do textarea, reszta formularza nietknięta).

### 2.3 services.py + provider OpenAI
- Asystent opisów przechodzi z Gemini na **OpenAI GPT**. Istniejące `suggest_activity_description` (dziś używa `_gemini_client`/`GEMINI_TEXT_MODEL`) zastępujemy wywołaniem OpenAI; `_gemini_client` zostaje wyłącznie dla analizy wideo.
- Jedna funkcja `assist_activity(operation, fields, mode, target=None)` z trybami `generate|refine`; `target` ogranicza wynik do jednego pola. Zwraca ustrukturyzowany JSON (opis, rozpoznanie, wykluczenie, możliwe pomyłki). Zachować fallback mock (bez `OPENAI_API_KEY`) zwracający deterministyczne treści.
- Nowy klient `_openai_client()` + ustawienia: `OPENAI_API_KEY`, `OPENAI_TEXT_MODEL` (domyślnie `gpt-4o-mini`). Dodać zależność `openai` do `requirements.txt` i zmienne do `.env.example`.
- Koszt asystenta tekstowego jest pomijalny i pozostaje poza panelem kosztów (koszt dotyczy tylko analizy wideo Gemini).

### 2.4 Testy Etapu 2
- tryb `refine` zachowuje sens wejścia (mock: zwraca wzbogaconą wersję wejścia).
- endpoint pojedynczego pola zwraca tylko to pole.

---

## Etap 3 — UX korekty wyniku (P1)

### 3.1 „Wymaga sprawdzenia"
- Na górze `analysis_detail.html` sekcja listująca segmenty o `confidence < próg` (domyślnie 0.4) lub przypisane do czynności „niepewne", z liczbą „X fragmentów do sprawdzenia", skokiem do segmentu i szybką zmianą czynności.

### 3.2 Podświetlanie aktywnego segmentu
- JS: nasłuch `timeupdate` na `#analysis-video`; podświetla bieżący pasek na osi (gantt) i wiersz w tabeli segmentów.

### 3.3 Szybka zmiana przypisanej czynności
- W tabeli segmentów inline `select` z czynnościami operacji; zapis przez HTMX (POST do istniejącej logiki `segment_update`, zwrot zaktualizowanego wiersza) bez rozwijania pełnej korekty.

### 3.4 Testy Etapu 3
- segmenty poniżej progu trafiają do sekcji „wymaga sprawdzenia".
- szybka zmiana czynności aktualizuje `activity` i `activity_name`.

---

## Etap 3b — Pętla informacji zwrotnej (👍/👎 → wskazówki do promptu) (P1)

Cel: korekty użytkownika realnie poprawiają kolejne analizy bez fine-tuningu modelu. Mechanizm = augmentacja promptu wskazówkami zebranymi przy czynności (human-in-the-loop), nie trening wag Gemini.

### 3b.1 Ocena per segment
- Przy każdym segmencie (oś/tabela) przyciski **👍 potwierdź** / **👎 popraw**.
- 👍 ustawia `AnalysisSegment.is_approved = True`.
- 👎 otwiera pole „uwaga" + opcjonalny wybór „pomylono z: [czynność operacji]".

### 3b.2 Przechowywanie wskazówek
- Nowy model `ActivityHint(activity FK, text, confused_with FK→Activity null, source_segment FK→AnalysisSegment null, is_active=True, created_at)`. Tylko wskazówki `is_active=True` trafiają do promptu.
- 👎 z uwagą tworzy `ActivityHint` powiązany z czynnością, której segment dotyczył.

### 3b.3 Wstrzyknięcie do promptu
- `build_analysis_prompt` dla każdej czynności dokłada sekcję „Wskazówki z wcześniejszych korekt:" złożoną z aktywnych `ActivityHint` tej czynności (np. „pieprz jest ciemniejszy niż sól").

### 3b.4 Kontrola (human-curated)
- Wskazówki są widoczne i edytowalne/usuwalne na stronie czynności, aby prompt nie puchł i nie zbierał sprzeczności. Decyzja: wskazówki są kuratorowane przez człowieka (nie wpływają automatycznie bez możliwości przeglądu).

### 3b.5 Testy
- 👎 z uwagą tworzy `ActivityHint` przy właściwej czynności.
- `build_analysis_prompt` zawiera tekst wskazówki dla tej czynności.
- 👍 ustawia `is_approved`.

---

## Etap 3c — Kolejność czynności jako miękka podpowiedź do promptu (P1)

Cel: model dostaje typową sekwencję czynności w operacji, co poprawia rozróżnianie podobnych kroków. Kolejność jest **podpowiedzią, nie regułą** — dozwolone odstępstwa (czekanie, chodzenie, poprawki, powtórzenia, pominięcia).

### 3c.1 Model
- Dodać pole `Activity.order = PositiveIntegerField(default=1)`; `Meta.ordering = ["order", "name"]`. Migracja.
- `activity_create` ustawia kolejny `order` (jak `operation_create`).

### 3c.2 Zmiana kolejności w UI
- Na stronie operacji strzałki ↑/↓ przy czynnościach (endpoint `activity_move`, analogicznie do `operation_move`).

### 3c.3 Prompt
- `build_analysis_prompt` wypisuje czynności w kolejności `order` i dodaje sekcję „Typowa kolejność czynności: 1 → 2 → …" z wyraźnym zastrzeżeniem, że to podpowiedź i możliwe są odstępstwa (czekanie/chodzenie/poprawki/powtórzenia/pominięcia).

### 3c.4 Testy
- prompt zawiera sekcję kolejności i wymienia czynności w kolejności `order`.
- `activity_move` zamienia kolejność sąsiadów.

---

## Etap 4 — Reszta (P2, zakres doprecyzowany przy tej fali)

- Scalanie/dzielenie segmentów; przeciąganie granic na osi czasu.
- Eksport wyniku do CSV; „Zatwierdź całą analizę" (ustawia `is_approved` dla wszystkich segmentów). **Zrealizowane w Etapie 4a.**
- Biblioteka/szablony czynności do kopiowania między operacjami.
- Skróty klawiszowe w korekcie.
- Pasek postępu konfiguracji procesu (operacje? czynności? gotowe do analizy?).

Ewentualne pola modelu (np. znacznik zatwierdzenia całej analizy) ustalimy na początku Etapu 4.

---

## Poza zakresem (na teraz)

- Własny model CV/YOLO, trening modeli, integracja MES/PLC.
- Centralna biblioteka standardów między zakładami i porównania zakładów.
- Tryb ciemny.
- Kolejka zadań produkcyjna (Celery/Redis) — świadomie odłożona na rzecz wątku + HTMX.

## Ryzyka / uwagi

- SQLite + wątki: możliwe krótkie blokady zapisu przy równoległych analizach; akceptowalne na demo (jeden użytkownik). Pod produkcję: Postgres + kolejka.
- Anonimizacja fallbackiem „pełne rozmycie obrazu" niszczy sygnał dla modelu — niezależny problem, poza tą specyfikacją, ale warto rozwiązać przed testem na realnym nagraniu.
- HTMX i Tailwind z CDN wymagają internetu (zgodne z dotychczasowymi decyzjami demo).
