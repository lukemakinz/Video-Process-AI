# AI video analysis logic

## Cel

Analiza wideo ma działać uniwersalnie dla różnych procesów: jazdy, gotowania,
montażu, pakowania itd. Model nie może być zmuszany do pewnego wyboru, gdy
obraz pokazuje tylko częściowy dowód. `confidence` zwracane przez model jest
traktowane jako sygnał wejściowy, a nie jako gotowa, zaufana metryka.

## Pipeline

1. Aplikacja buduje prompt z definicji procesu, operacji i czynności.
2. Do promptu trafiają aktywne wskazówki z wcześniejszych korekt użytkownika.
3. Użytkownik wybiera model Gemini do analizy wideo.
4. Model zwraca segmenty JSON.
5. Aplikacja normalizuje czasy, nazwy czynności i operacje.
6. Aplikacja kalibruje `confidence` własnymi regułami jakości dowodu.
7. Aplikacja wykonuje kontrolę czasową segmentów.
8. Segmenty o niskiej pewności albo `niepewne` trafiają do przeglądu.

## Wybór modelu

Model jest zapisywany przy `Video.analysis_model_name`, dzięki czemu można
uruchomić ten sam film różnymi modelami bez zmiany globalnego `.env`.

Domyślny model pochodzi z `GEMINI_VIDEO_MODEL`. Lista modeli w UI pochodzi z
`GEMINI_VIDEO_MODEL_CHOICES`, np.:

- `gemini-3.5-flash` - aktualny stabilny model do większości analiz,
- `gemini-3.1-pro-preview` - mocniejsze rozumowanie, wolniej/drożej,
- `gemini-3-flash-preview` - starszy preview do porównań,
- `gemini-3.1-flash-lite` - szybciej/taniej,
- `gemini-flash-latest`,
- `gemini-pro-latest`.

Na teraz Gemini API nie wystawia stabilnego `gemini-3.5-pro` dla
`generateContent`; wariant Pro w UI to `gemini-3.1-pro-preview`. W praktyce Pro
może lepiej wychwytywać trudne manewry, ale może też mocniej scalać kilka
krótkich zdarzeń w długi segment. Dlatego wybór modelu nie zastępuje review i
kalibracji confidence.

## Kontrakt JSON z modelem

Model ma zwracać dla każdego segmentu:

```json
{
  "start_seconds": 0.0,
  "end_seconds": 1.0,
  "observed": "neutralny opis tego, co widać, BEZ nazywania czynności",
  "activity": "nazwa czynności albo niepewne",
  "confidence": 0.62,
  "alternative_activity": "inna możliwa czynność albo null",
  "evidence": ["konkretny widoczny/słyszalny sygnał"],
  "missing_evidence": ["czego nie widać, a byłoby potrzebne do pewności"],
  "reason": "krótkie uzasadnienie",
  "confidence_reason": "dlaczego confidence ma właśnie taki poziom"
}
```

Pole `observed` jest celowo PIERWSZE przed `activity`. Model generuje JSON od
lewej do prawej, więc najpierw musi opisać sam obraz, a `activity` wybrać dopiero
z tego, co zaobserwował. To uniwersalna technika anty-konfabulacyjna („najpierw
patrz, potem nazywaj") — odcina mechanizm „wybierz etykietę, potem dorób pasujący
dowód". `observed` nie jest zapisywany jako osobne pole segmentu (parser je
pomija), służy wyłącznie wymuszeniu groundingu w odpowiedzi modelu.

Dla analizy multi-operation dochodzi pole `operation`.

Parser akceptuje zarówno obiekt:

```json
{"segments": []}
```

jak i top-level listę:

```json
[]
```

Top-level lista jest opakowywana jako `{"segments": lista}`.
Jeśli model błędnie opakuje listę segmentów w `box_2d`, parser próbuje ją
odzyskać, ale prompt nadal zakazuje tego formatu. Docelowy kontrakt API to
wyłącznie `segments`.

## Brak cichego fallbacku demo

Jeśli używane jest realne Gemini i odpowiedzi nie da się sparsować, analiza ma
zakończyć się błędem i zachować surową odpowiedź modelu w `raw_response`.
Aplikacja nie może wtedy zapisać segmentów demo jako udanej analizy.

Fallback demo jest dopuszczalny tylko w trybie mock/braku realnego klucza API,
czyli wtedy, gdy użytkownik świadomie pracuje bez realnej analizy modelu.

## Systemowe `niepewne`

`niepewne` jest etykietą systemową, dostępną nawet wtedy, gdy użytkownik nie
doda jej jako czynności w operacji. Nie jest nową czynnością procesu. Oznacza
fragment wymagający oceny człowieka.

Używamy jej, gdy:

- model nie potrafi uczciwie rozstrzygnąć między czynnościami,
- brakuje kluczowych dowodów,
- nazwa czynności zwrócona przez model nie pasuje do listy zdefiniowanych
  czynności.

## Dyscyplina dowodowa (uniwersalna)

Najczęstszy błąd modeli multimodalnych to **halucynacja dowodu**: model wpisuje
sygnał, którego nie ma w kadrze, bo wnioskuje z kontekstu sceny (np. „tor
wyścigowy → pewnie zakręt", „patelnia → pewnie smażenie", „klawiatura → pewnie
pisanie"). Dlatego prompt zawiera domenowo-neutralny blok zasad
(`_evidence_discipline_lines`), wstrzykiwany do obu wariantów promptu
(jedno- i wielooperacyjnego). Zasady są celowo niezależne od domeny — działają
tak samo dla jazdy, gotowania czy pracy biurowej:

- klasyfikuj tylko po tym, co realnie widać/słychać w danym fragmencie, nie po
  kontekście sceny ani samej obecności narzędzia/sprzętu,
- decyduj po obserwowalnej **zmianie** (kierunek, obiekt, narzędzie, faza,
  pozycja), a nie po tym, co „zwykle" towarzyszy czynności,
- dla każdej czynności rozważ jej **najbliższy odpowiednik** (najłatwiejszy do
  pomylenia) i wybierz ją tylko, gdy widać sygnał jednoznacznie je oddzielający;
  inaczej `niepewne`,
- `evidence` może zawierać tylko obserwacje możliwe do wskazania na klatce — bez
  sygnałów, których nie widać (np. „skręt", gdy kierunek się nie zmienia),
- oceniaj każdy fragment **niezależnie** — nie wnioskuj czynności z oczekiwanej
  kolejności, rytmu ani z sąsiednich segmentów; nie układaj „zgrabnego"
  naprzemiennego wzoru, jeśli obraz go nie potwierdza (to częsta narracyjna
  konfabulacja modelu),
- `confidence` ma odzwierciedlać, na ile **jednoznaczny i widoczny** jest sygnał
  odróżniający tę czynność od najbliższej alternatywy, a nie ogólne wrażenie, że
  czynność „pasuje",
- brak wyraźnego sygnału odróżniającego = `niepewne`, a nie najbardziej
  prawdopodobny strzał.

Reguła jest świadomie ogólna, żeby prompt pozostał uniwersalny. Domenowo-
specyficzne rozróżnienia (np. „prosto vs zakręt") nie są zaszywane w kodzie —
budują się przez definicje czynności (`Rozpoznaj, gdy` / `Nie rozpoznawaj, gdy`)
oraz przez pary `confused_with` z feedbacku użytkownika.

Te same zasady wymusza generator definicji czynności (asystent AI w formularzu
nowej czynności, `assist_activity`). Generator jest uniwersalny (bez założeń o
branży), preferuje JEDEN dominujący, duży sygnał zamiast mikro-detali (które
model zmyśla), a w `exclusion_rules` wymaga wskazania bliźniaka + sygnału
różnicującego + furtki `niepewne`. Zakazane są reguły o jakości, technice,
bezpieczeństwie czy wyniku — tylko o tym, co widać.

## Próbkowanie klatek wideo

Domyślnie Gemini próbkuje wideo z ~1 klatką/s, co nie wystarcza do rozróżniania
krótkich, szybkich ruchów. Wysyłka ustawia `fps` przez `video_metadata`
(`_video_content_part`), konfigurowalne przez `GEMINI_VIDEO_FPS` (domyślnie 5,
`0` = domyślne API). Wyższy fps = więcej realnych klatek i proporcjonalnie wyższy
koszt tokenów wideo. Uwaga: więcej klatek pomaga modelowi widzieć, ale nie
naprawia jego nadgorliwości — model bywa, że i tak zwraca stałą wysoką pewność.

## Pewność niewiarygodna (brak różnicowania)

Gdy model zwróci wysoką pewność (≥0.9) dla większości segmentów — także gdy
rozbije ją na np. 0.95 i 0.9 — znaczy to, że w ogóle nie różnicował pewności.
`_apply_temporal_quality_checks` oznacza wtedy te segmenty flagą
`AnalysisSegment.confidence_unreliable` i obcina je do 0.64. UI nie pokazuje
wówczas mylącego procentu, tylko etykietę „niewiarygodna".

Gdy ≥60% segmentów jest niewiarygodnych (`analysis_confidence_unreliable`), strona
analizy NIE listuje ich pojedynczo jako alarmów „Wymaga sprawdzenia" — pokazuje
jedną spokojną notkę na poziomie analizy: model nie różnicował pewności, liczby
są orientacyjne, zalecany przegląd całości. Dzięki temu trafna analiza nie tonie
w kilkunastu fałszywych alarmach. Segmenty merytorycznie `niepewne` nadal trafiają
do przeglądu osobno.

## Kalibracja confidence

Modelowe `confidence` jest obcinane przez aplikację, gdy odpowiedź wygląda na
zbyt pewną względem dowodów:

- brak pól `evidence`, `missing_evidence`, `alternative_activity` i
  `confidence_reason` ogranicza wysoką pewność,
- realna `alternative_activity` ogranicza pewność do poziomu przeglądu,
- `missing_evidence` obniża pewność,
- `niepewne` ma niską pewność,
- bardzo krótkie segmenty mają obniżoną pewność,
- segment krótszy niż `minimum_duration_seconds` czynności ma obniżoną pewność.

Wartość `0.95` z modelu nie jest już automatycznie pokazywana jako 95%.

## Granularność segmentów

Model nie powinien scalać kilku odrębnych wystąpień tej samej czynności w jeden
długi segment, jeśli widać wyraźną zmianę kierunku, obiektu, narzędzia, fazy
pracy albo krótką fazę przejściową.

Krótkie fragmenty nie są automatycznie pomijalne. Jeśli fragment ma stabilne,
widoczne sygnały innej zdefiniowanej czynności, powinien zostać osobnym
segmentem, nawet gdy dostanie niższe `confidence`. Minimalny czas trwania
czynności służy do ostrożności i review, a nie do wchłaniania krótkiej
czynności przez długi sąsiedni segment.

Przykłady:

- w jeździe sekwencja kilku zakrętów z widoczną zmianą kierunku nie powinna być
  jednym długim segmentem `turning`, jeśli da się wskazać granice,
- krótka, widoczna jazda prosto między zakrętami powinna zostać osobnym
  segmentem `driving in a straight line`, zamiast znikać w długim segmencie
  `turning`,
- w gotowaniu kilka osobnych kroków tym samym narzędziem nie powinno być
  scalone, jeśli zmienia się obiekt lub cel działania,
- rytmiczne mikro-ruchy w ramach tej samej ciągłej czynności nie są dzielone,
  jeśli nie zmieniają znaczenia procesu.

## Kontrola czasowa

Po normalizacji aplikacja sprawdza segmenty w obrębie tej samej operacji:

- jeśli model użył tej samej wysokiej pewności dla większości segmentów, np.
  ciągle `0.95`, aplikacja obniża te wartości i dopisuje notkę kalibracyjną,
- jeśli pojawia się krótki wzorzec `A -> B -> A`, środkowy segment dostaje
  niższe confidence, bo może być tylko korektą, przejściem albo gestem w ramach
  tej samej czynności.

Ta reguła jest domenowo-neutralna. W jeździe dotyczy np. małych korekt
kierownicą między fragmentami jazdy prosto. W gotowaniu może dotyczyć krótkiego
ruchu ręką między dwiema częściami mieszania.

## Wskazówki z feedbacku

`Popraw` + notatka tworzy `ActivityHint`.

Jeśli użytkownik wybierze w polu "powinno być" inną czynność, notatka jest
zapisywana pod poprawną czynnością, a pierwotna czynność staje się
`confused_with`. Dzięki temu kolejne prompty dostają reguły typu:

> Gdy wahasz się między A i B, wybierz A tylko jeśli pasuje ta korekta.

To buduje pary rozróżniające czynności, zamiast tylko dopisywać luźne notatki.

## Co daje `Dobrze`

`Dobrze` oznacza segment jako zatwierdzony (wpływa na review/eksport) ORAZ tworzy
**pozytywny przykład**: `ActivityHint` z `is_positive=True` i `source_segment`
wskazującym zatwierdzony segment. Powtórne zatwierdzenie tego samego segmentu nie
duplikuje wpisu (`get_or_create`). Pozytywne przykłady przetrwają re-analizę, bo
`source_segment` ma `on_delete=SET_NULL`.

W prompcie (`_activity_hint_lines`) korekty z `Popraw` (`is_positive=False`) i
potwierdzenia z `Dobrze` (`is_positive=True`) są rozdzielone: korekty renderują
się jako wskazówki/pary `confused_with`, a potwierdzenia jako zbiorcze
wzmocnienie („potwierdzone przez człowieka N raz(y) — sprawdzony wzorzec").
Domyka to pętlę uczenia: złe poprawiasz, dobre potwierdzasz, a model dostaje oba
sygnały.

## Próg przeglądu

Domyślny próg przeglądu to `0.65`. Segmenty poniżej tego progu oraz segmenty
z nazwą zawierającą `niepew` są oznaczane jako wymagające uwagi człowieka.
