# Pomysł: śledzenie wielu operatorów w gnieździe (ByteTrack + znacznik)

> Notatka koncepcyjna. Nie wdrożone. Zapis dyskusji, żeby nie zginął.

## Problem

Scenariusz: 2–3 operatorów w jednym gnieździe, każdy odpowiada za swoją operację
(swoje zdefiniowane czynności) w ramach procesu. Operatorzy **przemieszczają się
i krzyżują drogi**.

Obecne rozwiązanie (cały film + prompt multi-operacyjny do Gemini) rozpoznaje
„kto wykonuje którą operację" **po strefie kadru / charakterze pracy**. Gdy ludzie
się przemieszczają i mijają, ta heurystyka się sypie → podmiany operacji w punktach
skrzyżowań.

Dodatkowy konflikt: **anonimizacja rozmywa twarze**, a operatorzy zwykle noszą
**identyczne uniformy** → po skrzyżowaniu nie ma czym ich odróżnić w obrazie.
Czyli prywatność (rozmyta twarz) wprost utrudnia rozróżnianie operatorów.

Obecny model danych: segment = `(operacja, czynność)`, bez wymiaru operatora.
`performed_by` to tylko *typ* (Operator/Maszyna), nie tożsamość. Więc dziś nie da
się odpowiedzieć „co zrobił **konkretny** operator nr 2", tylko „co działo się
w operacji X".

## Pomysł rozwiązania

Dołożyć etap przetwarzania wideo **pomiędzy anonimizacją a wywołaniem Gemini**,
który najpierw rozdziela ludzi na osobne ścieżki i przypina im tożsamość, a dopiero
potem pyta AI „co robił ten konkretny człowiek".

### 1. Fizycznie (w hali)
Każdy operator dostaje inny, wyraźnie różny kolor znacznika
(czapka / kamizelka / plastron). **Kolory mocno różne** (np. czerwony / niebieski /
zielony — NIE żółty vs pomarańczowy, bo za blisko przy słabym świetle).

Typ znacznika zależy od kąta kamery:
- kamera z góry / sufit → **czapka** (czubek głowy zawsze widać),
- kamera z boku / pod kątem → **kamizelka / plastron na plecach**,
- mieszane / ludzie się obracają → i czapka, i kamizelka (redundancja).

Znacznik = fizyczna cecha, którą podajemy programowi. Software jej nie „wstawia",
tylko **odczytuje** to, co kamera zobaczy — różnicy, której nie ma w pikselach,
algorytm nie wymyśli.

### 2. W kodzie — nowe kroki przed Gemini

**Krok A — „kto i gdzie jest" (ByteTrack).**
Tracking-by-detection:
1. detekcja osób na każdej klatce (prostokąty, np. YOLO),
2. tracker skleja prostokąty w **ścieżki** po ruchu (filtr Kalmana + IoU) i nadaje
   każdej osobie **stabilne ID** (`osoba_1`, `osoba_2`) przez całe nagranie.

Ważne: **ByteTrack sam NIE patrzy na wygląd / kolor.** Trzyma tożsamość tylko po
ciągłości ruchu. Rozróżnianie po znaczniku to osobny krok (Krok B).

**Krok B — „kto to konkretnie" (odczyt znacznika).**
Program patrzy na dominujący kolor w obszarze każdej ścieżki i mapuje:
`osoba_1 = żółta = spawacz`, `osoba_2 = pomarańczowa = monter`. Ścieżki przestają
być anonimowe — są przypięte do operatora i jego operacji, spójnie też między
nagraniami.

**Krok C — pytanie do Gemini per ścieżka.**
Zamiast „zgadnij kto co robi na całym filmie", pytamy osobno o każdą osobę:
„oto co przez cały film robiła `osoba_1` (spawacz) — podziel TYLKO jej pracę na
czynności". Pytanie prostsze i mniej podatne na błędy, bo „kto" jest rozstrzygnięte
zanim AI zacznie.

### 3. Model danych
Segment dostaje pole `track_id` / `operator` — wreszcie da się policzyć **KPI per
konkretny operator**, nie tylko per operacja.

## Dlaczego to rozwiązuje krzyżowanie „u źródła"

Tożsamość podąża za **osobą (ruch + kolor)**, nie za miejscem w kadrze. Operator może
przejść przez całe gniazdo, wejść komuś w strefę — jego ID (a więc operacja) idzie
z nim.

### Słaby punkt: ID switch
Gdy dwie osoby mocno się zasłonią / przejdą tuż obok, tracker może **zamienić ID**.
To groźne, bo trwałe (po zamianie wszystkie kolejne segmenty obu osób są pomylone).
Normalnie ratuje re-identyfikacja po wyglądzie — ale tu twarze rozmyte, a uniformy
identyczne, więc re-ID po wyglądzie prawie nie działa. **I dokładnie tu wchodzi
znacznik:** po skrzyżowaniu odczytujemy kolor → przywracamy właściwe ID
(re-anchor). Znacznik jest odporny na to, co psuje re-ID (twarz, identyczny uniform),
i jest zgodny z anonimizacją (identyfikujemy numer/kolor, nie twarz).

## Warianty trackera

- **Droga 1 (prostsza, zalecana na start):** goły ByteTrack (ruch) + osobny odczyt
  koloru. Tracker prowadzi ścieżki, znacznik poprawia po skrzyżowaniach.
- **Droga 2 (cięższa):** tracker patrzący od razu na wygląd (DeepSORT / BoT-SORT) —
  kolor wchodzi do śledzenia od środka. Mocniejsze, ale cięższe. Dopiero gdy
  Droga 1 zawiedzie.

## Hierarchia rozwiązań (od najtańszego)

| Podejście | Krzyżowanie | Atrybucja per osoba | Koszt |
|---|---|---|---|
| Dziś (strefa kadru + całe wideo) | słabo | brak | — |
| + ByteTrack | dobrze, do zasłonięć | tory anonimowe | średni |
| + znacznik (kolor) | bardzo dobrze (re-anchor) | pełna, spójna między filmami | tracking + dyscyplina noszenia |
| + ArUco zamiast koloru | maks. (zero pomyłek) | pełna | jw. + drukowane markery, dobra widoczność |

## Rekomendowane wdrożenie (etapami)

**Krok 0 — najpierw udowodnij, że problem boli (zero kodu).**
Nagrać jedno realne nagranie z 2–3 operatorami, puścić przez obecny pipeline,
policzyć jak często przypisanie operacji się rozjeżdża przy mijaniu.
- rzadko (review wyłapuje) → **nie budować nic**,
- często i regularnie → krok 1.

**Krok 1 — minimalny pipeline.**
1. fizycznie: różnokolorowe znaczniki (kolory mocno różne; typ wg kąta kamery),
2. goły ByteTrack (ścieżki po ruchu),
3. prosty odczyt koloru → mapowanie ścieżka → operator/operacja,
4. pytanie do Gemini per ścieżka,
5. model danych: pole `operator` / `track_id` na segmencie.

**NIE robić na start:** ArUco, DeepSORT/BoT-SORT, re-ID po wyglądzie — dokładać
dopiero, gdy prosta wersja okaże się za słaba.

## Co kosztuje / ograniczenia

- To nie poprawka promptu, tylko **nowy etap przetwarzania wideo** (detekcja +
  tracking + odczyt koloru) + więcej czasu liczenia na nagranie.
- Operatorzy **muszą** nosić swój kolor i być widoczni dla kamery (zdjęta czapka /
  stanie tyłem w ciasnym kącie → znacznik chwilowo nie działa).
- Kamera najlepiej **stała** i obejmująca całe gniazdo.

## Pytania otwarte (decydują o wyborze)

1. **Jak ustawiona jest kamera?** (z góry → czapka; z boku → kamizelka)
2. **Czy celem są KPI per konkretny operator**, czy wystarczy „co działo się
   w operacji X"? Jeśli to drugie i operatorzy trzymają się stanowisk — może
   wystarczy poprawić sam prompt (jawne strefy kadru) i odpuścić tracking.

## Gdzie to wpina się w obecny kod (orientacyjnie)

- Obecny przepływ analizy: `processes/services.py` → `_analyze_with_gemini`
  (upload całego pliku + `build_multi_operation_prompt`) → `_normalize_multi_segments`
  → `persist_segments`.
- Nowy etap (detekcja + ByteTrack + odczyt koloru) wszedłby **przed**
  `_analyze_with_gemini`, produkując ścieżki per operator; prompt/wywołanie Gemini
  zmieniłoby się na „per ścieżka"; normalizacja i model segmentu dostałyby wymiar
  `operator` / `track_id`.
