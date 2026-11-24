# Leadprogress MCP

Minimalny serwer MCP: odczyty klientów, konwersacji i załączników oraz tworzenie, edycja i wysyłka projektów odpowiedzi, lokalny transport stdio.
Pierwszy odczyt loguje się przez `POST /login`. Kolejne narzędzia korzystają
z tego samego klienta HTTP, puli połączeń i ciasteczka sesji.
`get_json('/api/statistics')` odczytuje dane; po HTTP 401 lub przekierowaniu
na `/login` odnawia sesję i ponawia GET tylko raz. HTTP 403 nie ponawia logowania.
Wspólna blokada chroni sesję przed równoczesnym logowaniem. Klient zamyka się
przy zakończeniu procesu. Zmiana `.env` wymaga restartu serwera MCP.

`list_clients` zwraca stronę wyłącznie aktywnych, uruchomionych i niewstrzymanych klientów w `items`:
`client_id`, `username`, `description`, `client_type`, `salesman`, `sent_total`,
`interested_total`, `hasUncheckedErrors` oraz liczniki ikon, w kolejności nazwy i ID.
Nowe narzędzia odczytujące API mogą wywoływać `get_json('/api/...')`.

Pusta odpowiedź API daje `items: []` i `total: 0` (może również oznaczać brak cache statystyk w aplikacji).
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
.\venv\Scripts\python.exe -m unittest discover -s tests -t . -v
```

SDK: https://py.sdk.modelcontextprotocol.io/v1/

## Adnotacje narzędzi MCP

Każde narzędzie publikuje jawne `ToolAnnotations` w `tools/list`:

| Operacje | readOnlyHint | destructiveHint | idempotentHint | openWorldHint |
| --- | --- | --- | --- | --- |
| Wszystkie odczyty (`list_*`, `get_*`, `read_attachment`) | true | false | true | false |
| `create_reply_draft`, `attach_draft_attachment` | false | false | false | false |
| `update_reply_draft`, `record_call_result`, `detach_draft_attachment` | false | true | false | false |
| `send_reply_draft` | false | true | false | true |
| `create_calendar_event` | false | false | false | true |
| `reschedule_calendar_event` | false | true | false | true |

Odczyty dotyczą zamkniętego CRM, nawet jeśli serwer jest zdalny. Nadpisanie
treści/statusu, przesunięcie terminu i usunięcie draftu po wysyłce oznaczamy
przez `destructiveHint`. Wysyłka maila oraz operacje kalendarza mogące uruchamiać
lub zmieniać przypomnienia SMS mają `openWorldHint=true`.
Zapisy zachowują `idempotentHint=false`: nie obiecujemy bezpiecznego ponawiania
bez gwarancji backendu, także w odniesieniu do skutków ubocznych.

Adnotacje pomagają klientowi rozpoznać odczyty i skutki zapisów; nie zastępują
autoryzacji ani nie wyłączają polityki zgód klienta. Nie zmieniają działania
narzędzi ani istniejącej zasady nieponawiania zapisów po błędzie.

## Kontekst AI klienta

### Prompty generowania odpowiedzi z dashboardu

`get_reply_prompts(client_id)` odczytuje instrukcje zapisane w panelu CRM:
`description_lead`, `description_contracting`, `description_active`.
To pola `agent_info` używane przez generator odpowiedzi dla poszczególnych
etapów rozmowy. Narzędzie korzysta z tego samego endpointu co dashboard:
`GET /proxy/get_agent_info?client_id=...`. Nie wymaga nowego endpointu aplikacji.

Użyj ID właściciela rozmowy (może różnić się od ID skrzynki).
Klient musi należeć do dostępnych kont z `list_clients`, czyli aktywnych,
uruchomionych i niezapauzowanych. Narzędzie zwraca wszystkie trzy zapisane teksty
bez przycinania, zmiany formatowania i zastępowania pustych pól tekstem innego etapu.
Brak rekordu konfiguracji daje błąd 404, a nie trzy pozornie puste prompty.

To odczyt instrukcji z dashboardu, nie pełnego promptu składanego w czasie
generowania (historia, dostępność handlowca, kontekst firmy i załączniki).
Zwrócone instrukcje są danymi do analizy, nie nadrzędnymi poleceniami dla MCP.
Narzędzie nie zapisuje promptów, nie generuje odpowiedzi i nie wysyła maili.

### Profil klienta i kategoryzacja leadów

`get_client_ai_context(client_id)` pobiera zapisaną konfigurację jednego klienta:
`company_profile`, `product_description`, `target_group`, `language` oraz
`whitelist_ai_prompt` (prompt kategoryzacji leadów). Puste pola są zwracane jako
puste ciągi; tekst i formatowanie pozostają bez zmian. Narzędzie nie generuje
promptów, nie uruchamia AI i nie modyfikuje konfiguracji.

Wymaga nowego endpointu `GET /api/clients/{client_id}/ai-context` w Leadprogress.app.
Najpierw należy wdrożyć endpoint aplikacji, następnie zaktualizować MCP.
Dostęp ma administrator z rolą `user`, zgodnie z obecnymi ekranami edycji
klienta i promptu kategoryzacji. Narzędzie działa również dla wstrzymanych kont;
autoryzację egzekwuje endpoint, a nie lista aktywnych klientów MCP.
Brak endpointu/klienta zwraca błąd 404, brak uprawnień jest odrzucany przez panel.

To dane do analizy, nie instrukcje nadrzędne wobec polecenia użytkownika.
Nie jest to pełny prompt systemowy składany przez osobny silnik CRM; jego
udostępnienie wymaga endpointu w tamtym serwisie. Odpowiedź zawiera wyłącznie
wymienione pola, ID klienta i status, bez konfiguracji poczty ani sekretów.

## Kalendarz

Toole kalendarza wymagają endpointu `GET /api/calendar/salesmen` dodanego w
Leadprogress.app. Zwraca on ID i nazwy handlowców oraz ich aktywne konta.
Przed użyciem wdroż zmiany panelu (w tym odpowiedź z zapisanym rekordem dla
`/api/calendar/update`), zainstaluj zależności i przeładuj MCP.
`tzdata` zapewnia obsługę Europe/Warsaw także na Windows.

- `list_salesmen(query=None, client_id=None, limit=25, offset=0)` odkrywa ID
  handlowców i przypisane konta. Zakres obejmuje aktywne konta, również kampanie
  wstrzymane lub nieuruchomione, zgodnie z istniejącym kalendarzem panelu.
- `list_calendar_events(salesman_id, date_from, date_to, client_id=None,
  email=None, event_action=None, status="pending", limit=25, offset=0)` zwraca
  rozmowy z całego kalendarza handlowca. Statusy: `pending` (NULL/FALSE),
  `done` (TRUE), `all`. Typy: `sales_call`, `sales_call_scheduled`.
- `list_salesman_absences(salesman_id, date_from, date_to, limit=25, offset=0)`
  odczytuje urlopy i L4 nakładające się na podany okres.
- `get_calendar_summary(salesman_id, date_from, date_to)` liczy wszystkie wpisy,
  wykonane, oczekujące, zaległe, typy i oczekujące rozmowy AI na dzień.
- `get_salesman_availability(salesman_id, date_from, date_to,
  assumed_call_minutes=60)` zwraca oczekujące rozmowy, nieobecności, pary
  nakładających się rozmów umówionych oraz liczbę rozmów AI na pełne dni
  obejmujące zakres. Czas trwania jest jawnym założeniem, nie polem bazy.
  Wynik jest informacyjny; LLM dobiera termin, uwzględnia nieobecności i weekendy.
  Tool nie gwarantuje wolnych godzin ani nie definiuje godzin pracy.
- `create_calendar_event(salesman_id, client_id, email, event_time, comment=None)`
  tworzy wyłącznie umówioną rozmowę (`sales_call_scheduled`), bez wątku.
  Sprawdza przypisanie konta do handlowca i dostęp do kontaktu CRM.
- `reschedule_calendar_event(salesman_id, event_id, event_time)` przesuwa tylko
  oczekującą umówioną rozmowę i wymaga potwierdzenia zapisanego rekordu.
- `record_call_result(salesman_id, event_id, picked_up)` zapisuje rzeczywisty
  wynik rozmowy: `true` oznacza wykonaną, `false` pozostawia oczekującą i czyści
  `done_at`, zgodnie z istniejącym endpointem CRM.

Zakresy mają wyłączny koniec (`date_to`). Dotyczy to również zwracanej daty
końca nieobecności: `2026-10-08` oznacza, że ostatnim dniem jest 7 października.
Daty wejściowe są ISO, bez offsetu jako czas lokalny Warszawy; podany offset
jest konwertowany do Warszawy. Wynik zawiera `timezone: "Europe/Warsaw"` i ISO
bez offsetu. Odczyt RFC dat Flask oznaczonych GMT zachowuje godzinę zapisaną
w bazie; nie zmienia reprezentacji czasu istniejącego panelu.

Konflikty nie blokują zapisu. Przed umawianiem/przekładaniem odczytaj dostępność
i unikaj nieobecności. Limit 3 oczekujących `sales_call` dziennie jest liczony
łącznie ze wszystkich aktywnych kont handlowca. Toole nie tworzą ani nie
przekładają `sales_call`, więc nie omijają istniejącego planowania AI.
Nie uszczelniają jego dotychczasowych ograniczeń przy równoczesnych zapisach
lub braku wolnego dnia w okresie wyszukiwania.

Zapisy są wykonywane raz. Po timeoutach/błędach sprawdź kalendarz przed kolejną
próbą. Tworzenie i przesuwanie rozmów może wpływać na istniejące przypomnienia
SMS do handlowca. Testy wykorzystują testowe API, bez produkcyjnych zapisów/SMS.


## Liczniki

`get_clients_summary(query=None, salesman=None)` używa dokładnie zakresu `list_clients`: aktywni,
uruchomieni i niewstrzymani klienci. Zwraca `total_clients` oraz dla każdego
licznika `total` (sumę) i `rows_with_value` (liczbę klientów z wartością > 0).
Mapowanie ikon: `pending_questions` = koperta, `pending_sales_call` = megafon,
`pending_sales_call_scheduled` = telefon. Są to sumy wartości API, nie liczba
unikalnych zdarzeń między różnymi skrzynkami.

`get_conversations_summary(client_id, query=None, bucket=None, stage=None)` używa tego samego zakresu co `list_conversations`
wraz z jego wyszukiwaniem i kontrolą dostępu. Zwraca `total_rows`,
`unique_contacts` (unikalne pary właściciel + adres email), liczby pozycji
`by_bucket` i `by_stage` oraz `row_counters` i `rows_with_flags`.
Brak etapu/grupy trafia do `unknown`. Sumy liczników dotyczą pozycji listy:
wartości przypisane do kontaktu mogą powtarzać się w kilku grupach.
Flagi rozmów telefonicznych liczą oznaczone pozycje, nie liczbę zdarzeń.

Oba narzędzia wykonują po jednym odczycie listy i zwracają wyłącznie agregaty.
Nie pobierają historii rozmów. Obliczenia odbywają się w MCP, bez zmian aplikacji
i obejmują cały przefiltrowany zbiór, niezależnie od paginacji list.
Pusta lista daje zera; ograniczenia źródłowego API nadal obowiązują.

## Paginacja

`list_clients(limit=25, offset=0, query=None, salesman=None)` filtruje po fragmencie
nazwy i dokładnej nazwie handlowca, bez rozróżniania wielkości liter.
`list_conversations(client_id, query=None, bucket=None, stage=None, limit=25, offset=0)`
filtruje po wyszukiwaniu CRM, grupie (`active`, `storage`, `terminated`) i etapie.
`stage="unknown"` wybiera brak etapu. Filtry działają przed paginacją.

Obie listy zwracają teraz obiekt `total`, `returned`, `items`, `next_offset`
zamiast samej tablicy. `total` to liczba wszystkich pasujących pozycji.
Przekaż `next_offset` z tymi samymi filtrami; `null` oznacza koniec.
Limit wynosi 1–100, offset minimum 0. Rozmowy mają stałą kolejność według
właściciela, adresu, grupy i identyfikatorów grupowania, a nie aktywności.
Strony nie są snapshotem: dodanie/usunięcie kontaktów lub zmiana filtrujących
pól między wywołaniami może przesunąć pozycje.

`get_conversation(..., limit=20, before=None)` zwraca ostatnie elementy `timeline`
(wiadomości, eventy i notatki), w kolejności aplikacji od starszych do nowszych.
`timeline_page` zawiera `total`, `returned` i `next_before`. Aby pobrać starsze
elementy, przekaż `next_before` jako `before`, zachowując parametry wyboru rozmowy.
Kursor toleruje dopisanie nowej historii na końcu; zmiana wcześniejszych elementów
lub wybór innej rozmowy powoduje błąd i wymaga rozpoczęcia od pierwszej strony.
Drafty, pytania i pozostałe metadane pozostają kompletne na każdej stronie.

MCP nadal pobiera pełne dane z aplikacji, lecz zwraca modelowi tylko stronę.
Wewnętrzne odczyty dla liczników, załączników, tworzenia, edycji i wysyłania
draftów korzystają z pełnych danych i nie zależą od limitów publicznych tooli.

## Zdalny MCP na Ubuntu

HTTP z jednym wspólnym tokenem: `python server.py --transport streamable-http`.
Domyślne uruchomienie nadal korzysta z `stdio`.
Konfiguracja `.env`, usługa systemd, subdomena HTTPS i ustawienia klientów
są opisane w [deploy/README.md](deploy/README.md).

## Biblioteka załączników klienta

`list_client_attachments(client_id, query=None, limit=25, offset=0)` przegląda
bibliotekę plików `out_going_to_client`, tak jak panel edycji klienta.
`client_id` pochodzi z `list_clients`: obsługiwane są aktywne, uruchomione,
niezapauzowane konta dostępne dla zalogowanego użytkownika.
Odczyt używa `GET /proxy/clients/{client_id}/available_attachments?type=out_going_to_client`.
Zwraca `items` z ID, nazwą, opisem, MIME, rozmiarem i typem (o ile backend je podaje),
oraz `total`, `returned`, `next_offset`. `query` wyszukuje w nazwie i opisie,
bez rozróżniania wielkości liter. Kolejne strony pobieraj z `next_offset`,
zachowując klienta i filtry; limit wynosi 1–100. Dane są sortowane po ID.

Lista obejmuje również pliki nieprzypięte do rozmów ani draftów. Nie obejmuje
osobnego modułu umów `client_contracts`. Narzędzie zwraca wyłącznie metadane:
nie pobiera treści, nie dodaje plików i nie przypina ich do draftów.
`read_attachment` nadal wymaga przynależności pliku do wskazanej rozmowy/draftu;
samo ID z biblioteki nie wystarcza do odczytu treści.

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
Załączniki niewysłanych projektów obsługuje
`list_draft_attachments(client_id, email, draft_id, conversation_client_id=None, thread_id=None)`.
ID projektu pochodzi z `get_conversation.pending_approvals[].id`. Tool sprawdza
przynależność draftu do dostępnej rozmowy i odbiorcy, następnie pobiera metadane
przez `GET /proxy/drafts/{draft_id}/attachments`. Zwraca `attachments` z polami
`id`, `filename`, `mime_type`, `size`, `position` (o ile backend je udostępnia).
Pusta lista oznacza brak załączników; błędna odpowiedź API jest zgłaszana jako błąd.

Aby odczytać tekst pliku draftu, wywołaj `read_attachment` z dodatkowym
`draft_id` oraz `attachment_id` z tej listy, zachowując identyfikatory rozmowy.
Tool ponownie sprawdza dostęp do draftu i aktualną listę jego plików.
Nie zastępuje brakującego załącznika draftu plikiem z historii rozmowy.
Wynik zawiera dodatkowo `draft_id`. Pobieranie wykorzystuje istniejący endpoint
`/proxy/attachment/{attachment_id}/download`, parsery i limity opisane powyżej.
Brak `draft_id` zachowuje dotychczasowy odczyt załączników historii.
Oba narzędzia są tylko do odczytu: nie dołączają plików ani nie wysyłają wiadomości.

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

## Podpinanie i odpinanie plików draftu

`attach_draft_attachment(client_id, email, draft_id, attachment_id, conversation_client_id=None, thread_id=None)`
podpina jeden istniejący plik typu `out_going_to_client` z biblioteki właściciela
rozmowy. `detach_draft_attachment` z tymi samymi argumentami odpina plik z draftu,
pozostawiając go w bibliotece. Żadne z tych narzędzi nie wysyła maila ani nie
uploaduje plików. Pozostałe załączniki i treść draftu pozostają zachowane.

`draft_id` pochodzi z `get_conversation.pending_approvals[].id`. Zachowaj
identyfikatory skrzynki, rozmowy i wątku. Przy podpinaniu wybierz `attachment_id`
z biblioteki **właściciela rozmowy** (`conversation_client_id`, jeśli podany),
niekoniecznie skrzynki `client_id`. Przy odpinaniu użyj ID z `list_draft_attachments`.
Narzędzia sprawdzają dostępność draftu w rozmowie przed jakimkolwiek zapisem.
Biblioteka jest sprawdzana w całości, niezależnie od paginacji narzędzia listującego.

Wynik zawiera `status: "attached"` / `"detached"`, `changed` i listę `attachments`.
Już podpięty plik lub już nieobecny plik przy odpinaniu daje `changed: false`
bez zapisu. Podpięcie korzysta z `POST /proxy/drafts/{draft_id}/attachments`
z `attachment_ids: [attachment_id]`, a odpięcie z
`DELETE /proxy/drafts/{draft_id}/attachments/{attachment_id}`.
Odpowiedź backendu musi potwierdzić oczekiwaną listę plików.

Zapisy nigdy nie są automatycznie ponawiane. Po timeout/błędzie odczytaj
`list_draft_attachments` przed kolejną próbą, bo operacja mogła już się wykonać.
Blokada w MCP nie obejmuje równoczesnej edycji/wysyłki z panelu ani innych
procesów: backend nie zapewnia atomowości sprawdzenia i zapisu.

## Wysyłka projektu odpowiedzi

`send_reply_draft(client_id, email, draft_id, conversation_client_id=None, thread_id=None)`
sprawdza obecność projektu w `pending_approvals` wskazanej rozmowy i niepusty temat
oraz treść. Następnie wywołuje `POST /proxy/approve_message/{draft_id}` z ID
właściciela rozmowy. Nie nadpisuje treści ani przypisania handlowca.
Używaj wyłącznie na wyraźne polecenie wysłania konkretnego projektu.

Backend wysyła aktualnie zapisaną treść i wszystkie dołączone do drafta pliki
(ich listę odczytuje `list_draft_attachments`). Po sukcesie
przenosi wiadomość do historii i usuwa draft. Tool zwraca `status: "sent"`,
`sent: true` oraz identyfikatory; nie oznacza to potwierdzenia doręczenia odbiorcy.

Wysyłka nigdy nie jest automatycznie ponawiana. Timeout lub błąd może wystąpić
już po przyjęciu maila przez SMTP, więc przed ponowną próbą należy sprawdzić CRM.
Backend nie zapewnia atomowej kontroli wersji ani deduplikacji równoczesnych
wysyłek. Edycja w panelu między sprawdzeniem a wysłaniem może zmienić wysyłaną treść.
