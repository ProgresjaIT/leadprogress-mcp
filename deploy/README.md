# Ubuntu: MCP przez HTTPS z jednym wspolnym tokenem

## 1. Kod i konfiguracja procesu

Wgraj aktualne pliki projektu, w szczegolnosci `server.py` i `http_transport.py`.
Ponizsze sciezki `/opt/leadprogress-mcp` i uzytkownik `leadprogress` sa przykladami:
zastap je sciezka projektu i kontem, ktore juz masz na serwerze.
W istniejacym `.env` obok `server.py` zachowaj ustawienia LEADPROGRESS_* i dodaj:

```dotenv
MCP_AUTH_TOKEN=WKLEJ_TUTAJ_WYGENEROWANY_TOKEN
MCP_PUBLIC_URL=https://mcp.leadprogressai.app
MCP_PORT=8001
```

Wygeneruj token lokalnie na serwerze, np.:

```bash
/opt/leadprogress-mcp/venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(48))'
chmod 600 /opt/leadprogress-mcp/.env
```

Plik musi nalezec do konta uruchamiajacego usluge i byc dla niego czytelny.
Nie wgrywaj `.env` do Git. MCP wymaga minimum 32 znakow bez bialych znakow.
Zmiana tokenu wymaga restartu uslugi i zmiany konfiguracji wszystkich klientow.
Wszystkie komputery maja wspolny token i uprawnienia wspolnego konta CRM.

Nie trzeba tworzyc nowego venv. Uzyj istniejacego; `uvicorn` i `starlette`
sa zaleznosciami SDK `mcp`. Uruchomienie bez argumentow zachowuje `stdio`,
bez wymogu tokenu HTTP. HTTP uruchamia sie tak:

```bash
/opt/leadprogress-mcp/venv/bin/python /opt/leadprogress-mcp/server.py --transport streamable-http
```

Proces slucha tylko na `127.0.0.1:8001`. Uzywa bezstanowego Streamable HTTP,
jednego procesu i wspolnej sesji CRM; nie wymaga trwalych sesji MCP.
Naglowek Authorization jest sprawdzany przy kazdym zadaniu HTTP.
Log dostepu Uvicorn jest wylaczony; MCP i bledy procesu trafiaja do journald.

## 2. Usluga systemd

Dostosuj `leadprogress-mcp.service` (User, Group, WorkingDirectory, ExecStart),
nastepnie skopiuj go do `/etc/systemd/system/leadprogress-mcp.service`.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now leadprogress-mcp
sudo systemctl status leadprogress-mcp
sudo journalctl -u leadprogress-mcp -n 100 --no-pager
```

Po wgraniu nowych plikow lub zmianie `.env`:

```bash
sudo systemctl restart leadprogress-mcp
```

## 3. Subdomena i HTTPS

Skieruj rekord DNS `mcp.leadprogressai.app` na ten sam serwer.
W istniejacym serwerze WWW utworz host dla tej subdomeny z certyfikatem HTTPS,
a ruch `/mcp` przekazuj na `http://127.0.0.1:8001/mcp`.
To jest reverse proxy: HTTPS na porcie 443 przekazuje zadania do procesu MCP.
Sam rekord DNS nie ustawia certyfikatu ani nie wybiera procesu docelowego.
Nie trzeba instalowac dodatkowego proxy, jesli serwer WWW juz to obsluguje.

Na Twoim serwerze dziala Apache. Skopiuj dolaczony
`mcp.leadprogressai.app.conf` do `/etc/apache2/sites-available/`.
Ten plik zawiera poczatkowy VirtualHost HTTP, aby Certbot mogl utworzyc
certyfikat i skonfigurowac HTTPS. Najpierw ustaw DNS, potem:

```bash
sudo a2enmod proxy proxy_http ssl
sudo a2ensite mcp.leadprogressai.app.conf
sudo apache2ctl configtest
sudo systemctl reload apache2
sudo certbot --apache -d mcp.leadprogressai.app --redirect
sudo apache2ctl configtest
sudo systemctl reload apache2
```

Zakladamy istniejacy Certbot z pluginem Apache (masz juz certyfikaty Let's Encrypt).
Nie wysylaj tokenu przez HTTP w trakcie konfiguracji; klientow podlacz dopiero
po uruchomieniu HTTPS. Sprawdz utworzony VirtualHost SSL: powinien miec
`ServerName mcp.leadprogressai.app`, `ProxyPreserveHost On` i oba ProxyPass z
zalaczonego pliku. Apache przekazuje Authorization; nie dodawaj Basic Auth.
Nie uzywaj certyfikatu leadprogressai.app dla MCP, chyba ze obejmuje takze
subdomene. Certbot powyzej uzyskuje certyfikat dla nowej subdomeny.
Nie wystawiaj portu 8001 w firewallu.

## 4. Klienci Codex

Na kazdym komputerze, w `~/.codex/config.toml`
(Windows: `%USERPROFILE%\.codex\config.toml`) zastap sekcje lokalnego MCP
z `command` i `args` ponizsza konfiguracja:

```toml
[mcp_servers.leadprogress]
url = "https://mcp.leadprogressai.app/mcp"
http_headers = { Authorization = "Bearer TEN_SAM_TOKEN_CO_W_SERWEROWYM_ENV" }
```

Nie tworz drugiej sekcji o tej samej nazwie. Wpisz prawdziwy token i przeladuj
polaczenie MCP / uruchom ponownie Codex. Dane konta CRM zostaja tylko na serwerze.
Bez OAuth i bez logowania w przegladarce.

## Weryfikacja

Lokalne testy transportu: `venv/bin/python -m unittest test_http_transport -v`.
Nie lacza sie z produkcyjnym CRM. Testy wdrozenia przez publiczna subdomene,
kilka komputerow i operacje na prawdziwych danych pozostaja osobnym krokiem.
