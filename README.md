# Leadprogress MCP

Minimalny serwer MCP: odczyty klientów, konwersacji i załączników oraz tworzenie, edycja i wysyłka projektów odpowiedzi, lokalny transport stdio.
Pierwszy odczyt loguje się przez `POST /login`. Kolejne narzędzia korzystają
z tego samego klienta HTTP, puli połączeń i ciasteczka sesji.
`get_json('/api/statistics')` odczytuje dane; po HTTP 401 lub przekierowaniu
na `/login` odnawia sesję i ponawia GET tylko raz. HTTP 403 nie ponawia logowania.
Wspólna blokada chroni sesję przed równoczesnym logowaniem. Klient zamyka się
przy zakończeniu procesu. Zmiana `.env` wymaga restartu serwera MCP.

`list_clients` zwraca wyłącznie aktywnych, uruchomionych i niewstrzymanych klientów:
`client_id`, `username`, `description`, `client_type`, `salesman`, `sent_total`,
`interested_total`, `hasUncheckedErrors`, w kolejności z API.
Nowe narzędzia odczytujące API mogą wywoływać `get_json('/api/...')`.

Pusta odpowiedź API daje `[]` (może również oznaczać brak cache statystyk w aplikacji).
Wymagane konto administratora albo operatora; zakres klientów ustala aplikacja.

## Uruchomienie (Python 3.10+ / PowerShell)

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Uzupełnij URL, login i hasło w `.env`. Plik jest ignorowany przez Git.
Zmienne środowiskowe mają pierwszeństwo przed `.env`.

W kliencie MCP ustaw polecenie i argumenty (przykładowa konfiguracja):

```json
{
  "mcpServers": {
    "leadprogress": {
      "command": "C:\\Users\\User\\Documents\\GitHub\\leadprogress-mcp\\venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\User\\Documents\\GitHub\\leadprogress-mcp\\server.py"]
    }
  }
}
```

Po podłączeniu wywołaj `list_clients` bez argumentów. `.env` jest wczytywany
z katalogu serwera, niezależnie od katalogu roboczego klienta MCP.
Ręczne uruchomienie `python server.py` czeka na komunikaty MCP na stdin.

Testy bez danych logowania i bez połączenia z produkcją:

```powershell
.\venv\Scripts\python.exe -m unittest -v
```

SDK: https://py.sdk.modelcontextprotocol.io/v1/


## Liczniki

`get_clients_summary()` używa dokładnie zakresu `list_clients`: aktywni,
uruchomieni i niewstrzymani klienci. Zwraca `total_clients` oraz dla każdego
licznika `total` (sumę) i `rows_with_value` (liczbę klientów z wartością > 0).
Mapowanie ikon: `pending_questions` = koperta, `pending_sales_call` = megafon,
`pending_sales_call_scheduled` = telefon. Są to sumy wartości API, nie liczba
unikalnych zdarzeń między różnymi skrzynkami.

`get_conversations_summary(client_id, query=None)` używa `list_conversations`
wraz z jego wyszukiwaniem i kontrolą dostępu. Zwraca `total_rows`,
`unique_contacts` (unikalne pary właściciel + adres email), liczby pozycji
`by_bucket` i `by_stage` oraz `row_counters` i `rows_with_flags`.
Brak etapu/grupy trafia do `unknown`. Sumy liczników dotyczą pozycji listy:
wartości przypisane do kontaktu mogą powtarzać się w kilku grupach.
Flagi rozmów telefonicznych liczą oznaczone pozycje, nie liczbę zdarzeń.

Oba narzędzia wykonują po jednym odczycie listy i zwracają wyłącznie agregaty.
Nie pobierają historii rozmów. Obliczenia odbywają się w MCP, bez zmian aplikacji
i bez paginacji. Pusta lista daje zera; ograniczenia źródłowego API nadal obowiązują.

## Odczyt załączników

`read_attachment(client_id, email, attachment_id)` czyta ODT, PDF i DOCX.
ID pochodzi z `timeline[].payload.attachments` w `get_conversation`.
Narzędzie najpierw sprawdza dostęp do rozmowy i obecność załącznika, dopiero potem
pobiera plik przez istniejącą sesję HTTP MCP. Nie zapisuje dokumentów na dysku.
Opcjonalne `conversation_client_id` wskazuje właściciela rozmowy w skrzynce
handlowca; `thread_id` zawęża odczyt do wątku.

Wynik zawiera nazwę, format, rozmiar, tekst, `truncated` i `warnings`.
Limit pliku: 10 MiB; rozpakowanego ODT/DOCX: 20 MiB; PDF: pierwsze 100 stron.
`max_chars` domyślnie wynosi 30000, maksymalnie 100000. Przekroczenie limitu tekstu
lub liczby stron daje jawne `truncated: true`; zbyt duży plik jest odrzucany.
PDF zwraca także `page_count`, `pages_read` i `pages_without_text` (z odczytanej części).

To odczyt tekstu, bez OCR, obrazów i odwzorowania układu. ODT/DOCX obejmują główną
treść i tekst tabel, bez nagłówków/stopki i osobnych części przypisów. PDF bez tekstu
zwraca `status: "no_text"`; strony bez tekstu są wskazane także w dokumentach mieszanych.
Pliki zabezpieczone hasłem, uszkodzone i nieobsługiwane formaty zwracają błąd.
Załączniki niewysłanych projektów nie są jeszcze obsługiwane.

Nowe zależności: `pypdf` i `defusedxml` (w `requirements.txt`). Po aktualizacji
zależności i kodu przeładuj MCP. Testy obejmują trzy formaty przez prawdziwy protokół
stdio MCP z testowym API, kontrolę przynależności pliku, limity i odnowienie sesji.


## Tworzenie projektu odpowiedzi

`create_reply_draft(client_id, email, subject, body, thread_id=None, conversation_client_id=None)`
sprawdza dostępność kontaktu i wybór rozmowy, tworzy pusty projekt, następnie zapisuje
jego temat i treść. Zwraca `draft_id`, zapisany tekst oraz `sent: false`. Nie wywołuje
endpointu wysyłki. `client_id` to skrzynka; `conversation_client_id` wskazuje innego
właściciela rozmowy w widoku handlowca.

Dodatni `thread_id` wybiera istniejący wątek. Brak `thread_id` tworzy NOWY wątek,
zgodnie z backendem CRM; `0` jest odrzucane. Backend nie zwraca ID nowego wątku.
Temat i treść są wymagane. Tool nie dołącza załączników.

Tworzenie i zapis treści to dwa osobne żądania. Nie są automatycznie ponawiane.
Jeżeli drugi krok zawiedzie, komunikat zawiera ID już utworzonego projektu;
należy sprawdzić go w CRM, zamiast tworzyć kolejny. Timeout oznacza nieznany
wynik zapisu, a nie pewność, że operacja się nie wykonała.

## Edycja projektu odpowiedzi

`update_reply_draft(client_id, email, draft_id, subject=None, body=None,
conversation_client_id=None, thread_id=None)` pobiera aktualny projekt z
`pending_approvals` wskazanej rozmowy i sprawdza jego ID oraz adres odbiorcy.
Przekaż temat i/lub treść; pominięte pole zachowuje aktualną wartość.
Zapis nadpisuje oba pola tego samego projektu i zwraca `sent: false`.
Backend usuwa skrajne białe znaki i aktualizuje czas edycji.
Tool nie tworzy projektu, nie wysyła wiadomości i nie ponawia zapisu.
Backend nie zapewnia kontroli wersji: równoczesna edycja w panelu może zostać nadpisana.

## Wysyłka projektu odpowiedzi

`send_reply_draft(client_id, email, draft_id, conversation_client_id=None, thread_id=None)`
sprawdza obecność projektu w `pending_approvals` wskazanej rozmowy i niepusty temat
oraz treść. Następnie wywołuje `POST /proxy/approve_message/{draft_id}` z ID
właściciela rozmowy. Nie nadpisuje treści ani przypisania handlowca.
Używaj wyłącznie na wyraźne polecenie wysłania konkretnego projektu.

Backend wysyła aktualnie zapisaną treść i wszystkie dołączone do drafta pliki
(ich lista nie jest jeszcze zwracana przez `get_conversation`). Po sukcesie
przenosi wiadomość do historii i usuwa draft. Tool zwraca `status: "sent"`,
`sent: true` oraz identyfikatory; nie oznacza to potwierdzenia doręczenia odbiorcy.

Wysyłka nigdy nie jest automatycznie ponawiana. Timeout lub błąd może wystąpić
już po przyjęciu maila przez SMTP, więc przed ponowną próbą należy sprawdzić CRM.
Backend nie zapewnia atomowej kontroli wersji ani deduplikacji równoczesnych
wysyłek. Edycja w panelu między sprawdzeniem a wysłaniem może zmienić wysyłaną treść.
