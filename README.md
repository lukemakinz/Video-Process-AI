# Demo: analiza wideo procesu z Gemini

Aplikacja Django pokazuje przepływ:

`proces -> operacje -> czynności -> upload filmu -> anonimizacja -> podgląd i zatwierdzenie -> analiza Gemini -> segmenty i korekta`.

## Uruchomienie lokalne

```bash
python3 manage.py migrate
python3 manage.py seed_demo
python3 manage.py runserver
```

Wejdź na `http://127.0.0.1:8000/`.

## Gemini

Utwórz plik `.env` na podstawie `.env.example` i ustaw `GEMINI_API_KEY`.
Bez klucza aplikacja działa w trybie demo: asystent opisów i segmentacja zwracają deterministyczne przykłady.

## Szacowany koszt analizy

Każda analiza zapisuje liczbę tokenów i szacowany koszt (USD + orientacyjnie PLN), widoczny na stronie wyniku.
Gdy API zwraca realne zużycie tokenów (`usage_metadata`), koszt liczony jest z tych danych; w trybie mock jest szacowany z długości wideo.
Ceny i kurs ustawisz w `.env` (`GEMINI_PRICE_INPUT_PER_M`, `GEMINI_PRICE_OUTPUT_PER_M`, `GEMINI_VIDEO_TOKENS_PER_SECOND`, `GEMINI_USD_PLN_RATE`) — wartości domyślne są przybliżone, sprawdź aktualny cennik modelu.

## Interfejs

UI korzysta z Tailwind CSS (Play CDN) oraz fontów Fira Sans / Fira Code. Brak kroku budowania — style kompilują się w przeglądarce.

## Prywatność wideo

Po uploadzie aplikacja tworzy plik po anonimizacji i pokazuje go użytkownikowi do zatwierdzenia. Analiza AI jest blokowana, dopóki `anonymized_file` nie istnieje i użytkownik nie kliknie zatwierdzenia.

Aplikacja używa OpenCV/YuNet do wykrywania twarzy i nakłada maskę tylko na wykryty obszar twarzy w klatkach, w których twarz jest widoczna. Brak OpenCV, błąd detekcji albo brak wykrytej twarzy zatrzymuje anonimizację z błędem — aplikacja nie zamazuje całego filmu jako fallbacku. Oryginalny plik nie jest wysyłany do Gemini.
