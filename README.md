# kaufda-mcp

**Für den günstigeren Wocheneinkauf.** Ein MCP Server, der deiner KI (Claude, ChatGPT Desktop, Cursor oder jeder andere MCP Client) direkten Zugriff auf die Prospekte und Angebote von [kaufDA](https://www.kaufda.de) gibt.

Du sagst der KI einmal, wo du wohnst und in welchen Märkten du einkaufst. Danach reicht ein Satz wie *"Stell mir den Wocheneinkauf zusammen: Hackfleisch, Nudeln, Eier, Milch, Kaffee"* und die KI sucht selbstständig die besten Angebote raus, prüft ab wann und bis wann sie gelten, schaut sich zur Kontrolle die Prospektseite an und gibt dir eine fertige Liste pro Markt mit Link zum Prospekt. Wenn du willst, landet die Liste direkt in deiner **Bring!** Einkaufsliste, mit Markt, Preis und Gültigkeit als Notiz am Artikel.

## Mehrwert

* **Kein Prospekte Blättern mehr:** Die KI durchsucht alle Prospekte in deiner Nähe in Sekunden.
* **Richtige Gültigkeit pro Angebot:** Viele Angebote gelten erst ab Donnerstag, und Prospekte der nächsten Woche sind oft schon online. Jedes Angebot hat seinen eigenen Zeitraum und einen Status bezogen auf heute, z.B. `aktiv`, `aktiv, endet heute` oder `demnächst, ab morgen Do 15.10.`.
* **App Preise richtig verrechnet:** Preise mit PENNY App, Lidl Plus, Kaufland Card, REWE App, EDEKA App, Netto App, Netto plus, famila, Markant, Marktkauf und PAYBACK werden erkannt und getrennt vom Ladenpreis ausgewiesen. Du sagst der KI einmal, welche Apps du nutzt, dann rechnet sie mit deinem persönlichen Preis (`your_price`). Für Apps, die du nicht hast, zeigt sie dir nur, wie viel du damit sparen würdest. Treuepunkte Aktionen, Mengenrabatte ("ab 4 Dosen", "4 kaufen, 2 zahlen"), reine Online Angebote und REWE Bonus Guthaben werden ebenfalls erkannt und nicht als normaler Preis verkauft.
* **Verifizierbar:** Jedes Angebot hat eine `cutout_url` auf den Ausschnitt aus dem Prospekt (mit Preis, kein reines Produktfoto). Die KI kann sich Ausschnitt, Umgebung oder die ganze Prospektseite als Bild ansehen und so Preis und Zeitraum selbst gegenprüfen. Zu jedem Treffer gibt es einen kaufDA Link auf die passende Seite.
* **Vergleichbar:** Grundpreise (€/kg, €/l) werden ausgewertet und, wo sie fehlen, aus der Packungsgröße geschätzt.
* **Dein Profil bleibt gespeichert:** Standort, Lieblingsmärkte und Suchradius werden lokal gespeichert und gelten für jede weitere Unterhaltung.
* **Einkaufsplan statt Einzelpreise:** Für die ganze Liste rechnet der Server aus, wo du insgesamt am günstigsten wegkommst: bester Mix über alle Märkte gegen den besten Plan mit 1, 2 oder 3 Märkten. Er empfiehlt den Plan mit den wenigsten Wegen, der kaum teurer ist ("nur 3 statt 4 Märkte, ca. 0,26 € teurer"), und zeigt die Alternativen. Gerechnet wird mit der Menge, die du brauchst ("Milch, 2 l" ergibt 2 × 1 l, nicht die 12er Kiste mit dem besten Literpreis).
* **Sammelbegriffe:** Steht nur "Trinken", "Snacks" oder "Grillzeug" auf der Liste, sucht die KI breit (Wasser, Cola, Saft, ...) und schlägt die echten Deals nach Rabatt vor.
* **Einkaufszeitraum:** Standard ist diese Woche bis Samstag, inklusive Angeboten ab Donnerstag. Für jeden Markt wird berechnet, an welchen Tagen alle ausgewählten Angebote gleichzeitig gelten. Dringende Artikel bekommen nur Angebote, die heute schon gelten.
* **Bring! lesen und schreiben:** *"Schau dir meine Einkaufsliste an und mach einen Plan"* liest deine Bring! Liste, inklusive deiner Mengenangaben und der Markierung *Dringend*. Nach deinem OK schreibt die KI den Plan zurück: kurzer Titel (*Milch*), in der Beschreibung deine Menge plus Markt, Produkt, Preis und Gültigkeit (*"2 l · bei Netto: Bärenmarke Frische Milch 2 × 0,88 € ab Do 15.10."*) und die Markierung *Im Angebot*. Deine eigene Notiz bleibt beim nächsten Plan erhalten, nur der Marktteil wird ersetzt.

## Tools

| Tool | Was es macht |
| --- | --- |
| `get_preferences` | Gespeicherten Standort, Lieblingsmärkte, genutzte Apps/Kundenkarten und Suchradius anzeigen |
| `set_preferences` | Standort (PLZ, Ort oder Adresse), Lieblingsmärkte, Apps/Kundenkarten und Radius speichern |
| `search_offers` | Produkt suchen: Preis, Grundpreis, UVP oder Normalpreis, Bedingungen, Gültigkeit, Markt, Prospektseite und Link |
| `shopping_list` | Einkaufsplan für eine Liste (Eingabe oder direkt die Bring! Liste): Empfehlung, Vergleichspläne mit 1/2/3 Märkten, Alternativen, Auswahl bei Sammelbegriffen, fertige Bring! Updates |
| `list_brochures` | Aktuelle und kommende Prospekte in der Nähe, mit nächster Filiale und Entfernung |
| `get_brochure_offers` | Alle Angebote eines Prospekts, filterbar nach Suchbegriff oder Seite |
| `view_brochure_page` | Prospektseite als Bild plus die erkannten Angebote dieser Seite |
| `view_offer` | Angebot als Bild aus dem Prospekt mit allen Details. `mode="context"` zeigt zusätzlich die Umgebung, falls Preis oder Datumsbanner abgeschnitten sind (bei Sammelangeboten die ganze Seite) |
| `suggest_search_terms` | Vorschläge für passende Suchbegriffe |
| `bring_connect` | Bring! verbinden: liefert einen einmaligen lokalen Login Link (siehe unten) |
| `bring_status` | Verbindung, Konto und vorhandene Bring! Listen anzeigen |
| `bring_select_list` | Bring! Liste auswählen, in die eingekauft wird |
| `bring_get_list` | Bring! Liste anzeigen: Titel, Beschreibung/Menge, Dringend, Wenn's passt, Im Angebot |
| `bring_add_items` | Artikel anlegen oder aktualisieren: Beschreibung/Menge und die Markierungen Dringend, Wenn's passt, Im Angebot |
| `bring_remove_items` | Artikel löschen oder als gekauft abhaken |
| `bring_disconnect` | Verbindung trennen und gespeicherte Sitzung löschen |

Zusätzlich gibt es den Prompt **`wocheneinkauf`**, der den kompletten Ablauf (Profil prüfen, Liste suchen, unsichere Treffer verifizieren, Ergebnis pro Markt) vorgibt.

## Installation

Voraussetzung ist [uv](https://docs.astral.sh/uv/getting-started/installation/). Python und alle Abhängigkeiten holt uv automatisch, ein Klonen des Repos ist nicht nötig.

**Claude Code**

```bash
claude mcp add kaufda --scope user -- uvx --from git+https://github.com/vertexitde/kaufda-mcp kaufda-mcp
```

**Claude Desktop** (`%APPDATA%\Claude\claude_desktop_config.json` unter Windows, `~/Library/Application Support/Claude/claude_desktop_config.json` unter macOS)

```json
{
  "mcpServers": {
    "kaufda": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/vertexitde/kaufda-mcp", "kaufda-mcp"]
    }
  }
}
```

**Als HTTP Server** (für Clients, die per Netzwerk anbinden)

```bash
uvx --from git+https://github.com/vertexitde/kaufda-mcp kaufda-mcp --transport streamable-http --port 8765
```

### Optionale Umgebungsvariablen

| Variable | Bedeutung |
| --- | --- |
| `KAUFDA_LOCATION` | Standard Standort (PLZ oder Ort), solange keiner gespeichert ist |
| `KAUFDA_MCP_PREFS` | Pfad zur Einstellungsdatei (Standard: `%APPDATA%\kaufda-mcp\preferences.json` bzw. `~/.config/kaufda-mcp/preferences.json`) |
| `KAUFDA_MCP_LOGLEVEL` | Log Level auf stderr (Standard `WARNING`) |
| `BRING_EMAIL`, `BRING_PASSWORD` | Bring! Zugang für Server ohne Schlüsselspeicher (sonst `bring-login` nutzen) |

## Bring! Einkaufsliste

Die KI fragt einmal, ob du eine Einkaufslisten App nutzt. Sagst du *"Ja, Bring!"*, läuft es so:

1. Die KI ruft `bring_connect` auf und gibt dir einen Link wie `http://127.0.0.1:12713/...`. Das ist eine kleine Login Seite, die nur auf deinem Rechner läuft und nach 10 Minuten bzw. nach dem Login wieder verschwindet.
2. Dort meldest du dich mit deinen Bring! Zugangsdaten an. **Die KI sieht dein Passwort nie**, es geht direkt an Bring!.
3. Gespeichert wird nur das Sitzungstoken von Bring!, nicht dein Passwort, und zwar im Schlüsselspeicher deines Systems (Windows Anmeldeinformationsverwaltung, macOS Schlüsselbund, Secret Service unter Linux). Abgelaufene Tokens werden automatisch erneuert.
4. Hast du mehrere Listen, fragt die KI, welche sie nutzen soll.
5. Nach jeder Einkaufsplanung fragt die KI, ob sie die Artikel in Bring! eintragen soll.

Die Markierungen (Dringend, Wenn's passt, Im Angebot) sind in keiner öffentlichen Doku beschrieben. Der Server liest sie bei jedem Artikel mit, lernt das Format von Artikeln, die du in der App markiert hast, und prüft nach jedem Schreiben, ob Bring! sie wirklich übernommen hat. Falls nicht, sagt die KI dir das: einmal in der App einen Artikel als dringend markieren, danach klappt es.

**Ohne Browser (Server, Bot):**

```bash
uvx --from git+https://github.com/vertexitde/kaufda-mcp kaufda-mcp bring-login
```

fragt Mail und Passwort im Terminal ab (`bring-status` zeigt die Listen, `bring-logout` löscht die Sitzung). Alternativ die Umgebungsvariablen `BRING_EMAIL` und `BRING_PASSWORD` für den MCP Server setzen.

Bring! bietet keine offizielle API. Genutzt werden die Endpunkte der Bring! Apps, wie sie von der Community dokumentiert sind (u.a. [bring-api](https://github.com/miaucl/bring-api), das auch Home Assistant verwendet).

## So redest du mit der KI

* *"Ich wohne in 24534 Neumünster und kaufe meistens bei Lidl, Edeka, Penny und Aldi."*
* *"Ich hab die PENNY App und Lidl Plus."*
* *"Wo gibt es diese Woche Butter am günstigsten?"*
* *"Ich gehe Donnerstag einkaufen. Was davon ist dann im Angebot: Eier, Hackfleisch, Bananen?"*
* *"Was hat Lidl nächste Woche im Prospekt? Zeig mir die Seite mit dem Kaffee."*
* *"Gibt es am Samstag irgendwo Bier im Angebot?"*
* *"Ich nutze Bring!, pack die Sachen auf meine Liste Zuhause."*
* *"Schau dir meine Einkaufsliste an und mach daraus einen Einkaufsplan. Wo komm ich insgesamt am günstigsten weg?"*
* *"Bei Trinken such mir was Gutes raus, ich will nicht in mehr als zwei Läden."*

## Entwicklung

```bash
git clone https://github.com/vertexitde/kaufda-mcp && cd kaufda-mcp
uv sync
uv run python tests/test_normalize.py   # Unit Tests, offline
uv run python tests/smoke_live.py       # alle Tools live gegen kaufda.de
uv run python tests/smoke_stdio.py      # Server per stdio wie ein MCP Client starten
uv run python tests/check_days.py       # Gültigkeit abhängig vom Wochentag
uv run python tests/test_bring.py       # Bring! gegen simulierte API, inkl. Login Seite
uv run python tests/smoke_bring_stdio.py  # Bring! Ablauf über das MCP Protokoll
uv run python tests/smoke_plan.py      # Einkaufsplan aus simulierter Bring! Liste mit echten Angeboten
```

Aufbau: `client.py` (HTTP Zugriff auf kaufDA und Geocoding), `normalize.py` (Preise, Gültigkeit, Relevanz), `prefs.py` (gespeichertes Profil), `bring.py` (Bring! Client, Schlüsselspeicher, Login Seite), `server.py` (MCP Tools).

## Hinweise

* kaufDA bietet keine offizielle API. Der Server nutzt die öffentlichen Endpunkte, die auch die kaufDA Webseite verwendet. Ändert kaufDA etwas, muss `client.py` angepasst werden.
* Ortsangaben werden über OpenStreetMap Nominatim in Koordinaten umgewandelt (maximal eine Anfrage pro Sekunde, Ergebnisse werden zwischengespeichert).
* Gedacht für den privaten Gebrauch. Bitte die Nutzungsbedingungen von kaufDA beachten. Kein offizielles Projekt von kaufDA oder Bonial.
