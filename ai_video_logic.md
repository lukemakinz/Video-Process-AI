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
  "activity": "nazwa czynności albo niepewne",
  "confidence": 0.62,
  "alternative_activity": "inna możliwa czynność albo null",
  "evidence": ["konkretny widoczny/słyszalny sygnał"],
  "missing_evidence": ["czego nie widać, a byłoby potrzebne do pewności"],
  "reason": "krótkie uzasadnienie",
  "confidence_reason": "dlaczego confidence ma właśnie taki poziom"
}
```

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

`Dobrze` oznacza segment jako zatwierdzony i wpływa na review/eksport. Obecnie
nie tworzy pozytywnego przykładu dla promptu. Jeśli chcemy, żeby zatwierdzone
segmenty też uczyły przyszłe analizy, trzeba dodać osobny mechanizm przykładów
pozytywnych.

## Próg przeglądu

Domyślny próg przeglądu to `0.65`. Segmenty poniżej tego progu oraz segmenty
z nazwą zawierającą `niepew` są oznaczane jako wymagające uwagi człowieka.
