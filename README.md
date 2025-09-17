# Wyniki Tenis Overlay

Aplikacja Flask do zarządzania linkami i konfiguracją overlayów wykorzystywanych do prezentowania wyników meczów tenisowych. Projekt udostępnia interfejsy www oraz API REST do konfiguracji rozkładu widoków dla wielu kortów, a także panel administracyjny zabezpieczony podstawową autoryzacją.

## Funkcje
- prezentacja listy dostępnych kortów oraz linków do overlayów na stronie głównej,
- widok pojedynczego kortu z dynamicznie wczytywaną konfiguracją,
- widok „cztery korty” renderujący overlaye w układzie narożników,
- panel `/config` do edycji rozmiarów wycinków, skalowania i położenia etykiet,
- panel `/overlay-links` do zarządzania linkami overlayów (dodawanie, edycja, usuwanie),
- API `/api/overlay-links` do integracji z zewnętrznymi narzędziami,
- automatyczna inicjalizacja bazy SQLite oraz wypełnienie jej przykładowymi danymi z `overlay_links.json`.

## Wymagania
- Python 3.10+
- Narzędzie `pip` do instalacji zależności
- (Opcjonalnie) wirtualne środowisko `venv`

## Instalacja i uruchomienie
1. Zainstaluj zależności:
   ```bash
   pip install -r requirements.txt
   ```
2. Skonfiguruj zmienne środowiskowe kopiując plik `.env.example` do `.env` i modyfikując wartości pod swoje potrzeby:
   ```bash
   cp .env.example .env
   ```
   Aplikacja automatycznie wczyta zmienne z pliku `.env` przy starcie, dlatego nie ma potrzeby ręcznego eksportowania ich do środowiska powłoki.
3. Uruchom aplikację w trybie deweloperskim poleceniem:
   ```bash
   flask run --app main.py --debug
   ```
   Alternatywnie możesz wystartować aplikację poleceniem `python main.py` lub użyć kontenera Docker (`docker-compose up`).

Po uruchomieniu aplikacja nasłuchuje na porcie określonym zmienną `PORT` (domyślnie `5000`). Baza danych `overlay.db` zostanie utworzona automatycznie przy pierwszym starcie.

## Konfiguracja środowiska
W pliku `.env.example` znajdują się wszystkie najważniejsze zmienne środowiskowe:

| Zmienna | Opis |
| --- | --- |
| `FLASK_APP` | Nazwa modułu używanego przez `flask run`. |
| `FLASK_ENV` | Tryb pracy aplikacji (np. `development` lub `production`). |
| `PORT` | Port, na którym aplikacja nasłuchuje żądań HTTP. |
| `DATABASE_URL` | URI bazy danych SQLAlchemy. Domyślnie używany jest plik `sqlite:///overlay.db`. |
| `CONFIG_AUTH_USERNAME` | Login wymagany przy dostępie do panelu `/config`. |
| `CONFIG_AUTH_PASSWORD` | Hasło do panelu konfiguracji. |
| `LOG_LEVEL` | Poziom logowania aplikacji (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`). |

Po ustawieniu loginu i hasła użytkownik zostanie poproszony o podanie tych danych przy próbie wejścia na `/config`. Brak zdefiniowanych zmiennych skutkuje odmową dostępu.

## Interfejs użytkownika
- **`/`** – strona główna prezentująca listę zarejestrowanych kortów i linki do overlayów.
- **`/kort/<id>`** – widok pojedynczego kortu z głównym overlayem i miniaturami pozostałych kortów.
- **`/kort/all`** – widok z czterema overlayami rozmieszczonymi w narożnikach.
- **`/config`** – panel konfiguracji parametrów overlayów (wymaga autoryzacji Basic Auth).
- **`/overlay-links`** – panel zarządzania linkami overlayów (dodawanie, edycja, usuwanie).

## API
Aplikacja udostępnia REST API pozwalające na integrację z zewnętrznymi systemami:

| Metoda | Endpoint | Opis |
| --- | --- | --- |
| `GET` | `/api/overlay-links` | Lista wszystkich zarejestrowanych linków overlayów. |
| `POST` | `/api/overlay-links` | Dodanie nowego linku (wymaga pól `kort_id`, `overlay`, `control`). |
| `GET` | `/api/overlay-links/<id>` | Pobranie pojedynczego linku. |
| `PUT` | `/api/overlay-links/<id>` | Aktualizacja istniejącego linku. |
| `DELETE` | `/api/overlay-links/<id>` | Usunięcie linku. |

Dane są walidowane – aplikacja odrzuca żądania z niepoprawnymi adresami URL lub zduplikowanym `kort_id`.

## Logowanie
Poziom logowania kontrolowany jest zmienną `LOG_LEVEL`. Domyślna wartość `INFO` zapewnia podstawowe komunikaty (m.in. o seedowaniu bazy). Ustawienie `DEBUG` dostarcza dodatkowych informacji diagnostycznych, a `WARNING` i wyżej ograniczają liczbę komunikatów.

## Testy
W projekcie znajdują się testy jednostkowe oparte o `pytest`. Aby je uruchomić, wykonaj:

```bash
pytest
```

## Dodatkowe informacje
- Początkowe linki do overlayów są trzymane w pliku `overlay_links.json`. Przy pierwszym uruchomieniu aplikacja wczyta je do bazy (pomijając niepoprawne adresy).
- Konfiguracja wyglądu overlayów przechowywana jest w tabeli `overlay_config`. Panel `/config` pozwala na modyfikację parametrów dla każdego narożnika osobno.
