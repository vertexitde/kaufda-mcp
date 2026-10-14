# kaufda-mcp

**Für den günstigeren Wocheneinkauf.** Ein MCP Server, der deiner KI (Claude, ChatGPT Desktop, Cursor oder jeder andere MCP Client) direkten Zugriff auf die Prospekte und Angebote von [kaufDA](https://www.kaufda.de) gibt.

Du sagst der KI einmal, wo du wohnst und in welchen Märkten du einkaufst. Danach reicht ein Satz wie *"Stell mir den Wocheneinkauf zusammen: Hackfleisch, Nudeln, Eier, Milch, Kaffee"* und die KI sucht selbstständig die besten Angebote raus, prüft ab wann und bis wann sie gelten, schaut sich zur Kontrolle die Prospektseite an und gibt dir eine fertige Liste pro Markt mit Link zum Prospekt.

## Mehrwert

* **Kein Prospekte Blättern mehr:** Die KI durchsucht alle Prospekte in deiner Nähe in Sekunden.
* **Richtige Gültigkeit pro Angebot:** Viele Angebote gelten erst ab Donnerstag, und Prospekte der nächsten Woche sind oft schon online. Jedes Angebot hat seinen eigenen Zeitraum und einen Status bezogen auf heute, z.B. `aktiv`, `aktiv, endet heute` oder `demnächst, ab morgen Do 15.10.`.
* **App Preise richtig verrechnet:** Preise mit PENNY App, Lidl Plus, Kaufland Card, REWE App, EDEKA App, Netto App, Netto plus, famila, Markant, Marktkauf und PAYBACK werden erkannt und getrennt vom Ladenpreis ausgewiesen. Du sagst der KI einmal, welche Apps du nutzt, dann rechnet sie mit deinem persönlichen Preis (`your_price`). Für Apps, die du nicht hast, zeigt sie dir nur, wie viel du damit sparen würdest. Treuepunkte Aktionen, Mengenrabatte ("ab 4 Dosen", "4 kaufen, 2 zahlen"), reine Online Angebote und REWE Bonus Guthaben werden ebenfalls erkannt und nicht als normaler Preis verkauft.
* **Verifizierbar:** Jedes Angebot hat eine `cutout_url` auf den Ausschnitt aus dem Prospekt (mit Preis, kein reines Produktfoto). Die KI kann sich Ausschnitt, Umgebung oder die ganze Prospektseite als Bild ansehen und so Preis und Zeitraum selbst gegenprüfen. Zu jedem Treffer gibt es einen kaufDA Link auf die passende Seite.
* **Vergleichbar:** Grundpreise (€/kg, €/l) werden ausgewertet und, wo sie fehlen, aus der Packungsgröße geschätzt.
* **Dein Profil bleibt gespeichert:** Standort, Lieblingsmärkte und Suchradius werden lokal gespeichert und gelten für jede weitere Unterhaltung.
* **Einkaufsplanung:** Für jeden Markt wird berechnet, an welchen Tagen alle ausgewählten Angebote gleichzeitig gelten.

## Tools

| Tool | Was es macht |
| --- | --- |
| `get_preferences` | Gespeicherten Standort, Lieblingsmärkte, genutzte Apps/Kundenkarten und Suchradius anzeigen |
| `set_preferences` | Standort (PLZ, Ort oder Adresse), Lieblingsmärkte, Apps/Kundenkarten und Radius speichern |
| `search_offers` | Produkt suchen: Preis, Grundpreis, UVP oder Normalpreis, Bedingungen, Gültigkeit, Markt, Prospektseite und Link |
| `shopping_list` | Ganze Einkaufsliste auf einmal: bestes Angebot pro Artikel, gruppiert nach Markt, mit Einkaufszeitraum und Alternativen |
| `list_brochures` | Aktuelle und kommende Prospekte in der Nähe, mit nächster Filiale und Entfernung |
| `get_brochure_offers` | Alle Angebote eines Prospekts, filterbar nach Suchbegriff oder Seite |
| `view_brochure_page` | Prospektseite als Bild plus die erkannten Angebote dieser Seite |
| `view_offer` | Angebot als Bild aus dem Prospekt mit allen Details. `mode="context"` zeigt zusätzlich die Umgebung, falls Preis oder Datumsbanner abgeschnitten sind (bei Sammelangeboten die ganze Seite) |
| `suggest_search_terms` | Vorschläge für passende Suchbegriffe |

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

## So redest du mit der KI

* *"Ich wohne in 24534 Neumünster und kaufe meistens bei Lidl, Edeka, Penny und Aldi."*
* *"Ich hab die PENNY App und Lidl Plus."*
* *"Wo gibt es diese Woche Butter am günstigsten?"*
* *"Ich gehe Donnerstag einkaufen. Was davon ist dann im Angebot: Eier, Hackfleisch, Bananen?"*
* *"Was hat Lidl nächste Woche im Prospekt? Zeig mir die Seite mit dem Kaffee."*
* *"Gibt es am Samstag irgendwo Bier im Angebot?"*

## Entwicklung

```bash
git clone https://github.com/vertexitde/kaufda-mcp && cd kaufda-mcp
uv sync
uv run python tests/test_normalize.py   # Unit Tests, offline
uv run python tests/smoke_live.py       # alle Tools live gegen kaufda.de
uv run python tests/smoke_stdio.py      # Server per stdio wie ein MCP Client starten
uv run python tests/check_days.py       # Gültigkeit abhängig vom Wochentag
```

Aufbau: `client.py` (HTTP Zugriff auf kaufDA und Geocoding), `normalize.py` (Preise, Gültigkeit, Relevanz), `prefs.py` (gespeichertes Profil), `server.py` (MCP Tools).

## Hinweise

* kaufDA bietet keine offizielle API. Der Server nutzt die öffentlichen Endpunkte, die auch die kaufDA Webseite verwendet. Ändert kaufDA etwas, muss `client.py` angepasst werden.
* Ortsangaben werden über OpenStreetMap Nominatim in Koordinaten umgewandelt (maximal eine Anfrage pro Sekunde, Ergebnisse werden zwischengespeichert).
* Gedacht für den privaten Gebrauch. Bitte die Nutzungsbedingungen von kaufDA beachten. Kein offizielles Projekt von kaufDA oder Bonial.
