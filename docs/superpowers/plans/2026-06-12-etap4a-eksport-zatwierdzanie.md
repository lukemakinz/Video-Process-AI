# Etap 4a — Eksport CSV i zatwierdzenie całej analizy — Implementation Plan

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wynik analizy można zatwierdzić jednym kliknięciem oraz wyeksportować do CSV z segmentami i metrykami potrzebnymi do dalszej pracy.

**Architecture:** Bez nowego modelu. `analysis_approve_all` ustawia `AnalysisSegment.is_approved=True` dla wszystkich segmentów danej analizy. `analysis_export_csv` generuje odpowiedź `text/csv` z wierszem nagłówka i segmentami w kolejności czasu.

---

### Task 1: Testy

- [x] Dodać test `test_analysis_approve_all_marks_all_segments`.
- [x] Dodać test `test_analysis_export_csv_contains_segments`.

### Task 2: Widoki i trasy

- [x] Dodać widok `analysis_approve_all`.
- [x] Dodać widok `analysis_export_csv`.
- [x] Dodać trasy `/analyses/<pk>/approve-all/` i `/analyses/<pk>/export.csv`.

### Task 3: UI

- [x] Dodać przyciski „Eksport CSV” i „Zatwierdź całość” w nagłówku wyniku analizy.
- [x] Pokazać licznik zatwierdzonych segmentów w szczegółach analizy.

### Task 4: Weryfikacja

- [x] `python3 manage.py check`
- [x] `python3 manage.py test processes -v1`
- [x] Smoke HTTP: przyciski widoczne, zatwierdzenie działa, eksport zwraca CSV.
