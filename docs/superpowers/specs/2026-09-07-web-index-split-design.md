# Split di `web/index.html` in asset esterni

Data: 2026-09-07
Stato: approvato, pronto per l'implementazione

## Obiettivo

`web/index.html` è un file singolo da 1930 righe (99.519 byte). Ogni modifica,
anche minima, costringe un agente a leggerlo per intero: ~28K token. Il costo
dominante di una sessione agentica è **numero di round-trip × dimensione del
contesto**, quindi un file che non si può leggere a pezzi moltiplica entrambi.

Obiettivo: portare `index.html` a ~370 righe di solo markup, con il
comportamento **identico**. Nessuna modifica funzionale.

## Vincoli verificati

Questi fatti sono stati verificati sul repo, non assunti:

1. **Niente ES module.** Il markup contiene **74 handler inline** che invocano
   **35 funzioni globali distinte** (`onclick="fireKey(...)"`,
   `oninput="doSearch()"`, `onclick="goHome()"`). `<script type="module">` ha
   scope di modulo: `window.goHome` non esisterebbe e tutti e 74 gli handler si
   romperebbero. Si usano **script classici**, i globali restano globali, e
   **l'ordine dei tag `<script>` è significativo**.

2. **Il service worker non fa caching.** `web/sw.js` gestisce solo Web Push:
   nessun handler `fetch`, nessuna Cache API. Aggiungere file **non** richiede
   di toccare `sw.js` e non ha effetti offline.

3. **Il relay ha già un punto di estensione pulito.** `relay/herdr_relay.py:1088`
   definisce `static_files`; `public_paths` (riga 1095) lo assorbe via
   `*static_files`. Il generico `if path in static_files` (riga 1221) serve
   qualunque file registrato con il suo content-type. **Aggiungere un asset = una
   riga**, nessun handler nuovo.

4. **Playwright è già compatibile.** `tests/check_terminal_layout.py:37` fa
   `context.route("**/*", asset)` servendo da `web/`, quindi gli `<script src>`
   esterni si risolvono da soli. **Zero modifiche.**

5. **L'harness `vm` è già compatibile.** `tests/web_controls_harness.js:85`
   estrae i blocchi **per id** (`<script id="...">`) e li valuta con
   `vm.runInContext`. I blocchi id'd restano inline (vedi sotto), quindi
   `test_terminal_viewport.js` e `test_web_fullscreen.js` **non cambiano**.

## Struttura risultante

I quattro blocchi `<script id="...">` già esistenti — `fullscreenControls`,
`terminalViewport`, `terminalInput`, `terminalAttachments` — **restano inline**:
sono già piccoli e delimitati, e l'harness `vm` li estrae per id. Spostarli
romperebbe i test senza guadagno.

Si estraggono il CSS e il blocco `<script>` finale non nominato (righe 720-1928,
1208 righe, il 63% del file):

| Nuovo file | Origine | ~N | Responsabilità |
|---|---|---|---|
| `web/app.css` | 22-205 | 183 | CSS principale (il blocco `data-font-face` resta inline) |
| `web/js/connection.js` | 722-960 | 239 | stato globale, WebSocket, status, storage, setup, demo |
| `web/js/ansi.js` | 961-1029 | 70 | `ansiFragment` — foglia pura |
| `web/js/dashboard.js` | 1030-1272 | 243 | `handleMessage`, render lista/workspace |
| `web/js/terminal.js` | 1273-1614 | 340 | vista pane, history, ricerca, menu contestuale |
| `web/js/input.js` | 1615-1845 | 230 | tastiera, key queue, ctrl presets, command palette |
| `web/js/push.js` | 1846-1927 | 82 | service worker + web push |

**I range di righe sono rigorosi: nessuna funzione cambia file.** Il blocco
contiene statement top-level eseguibili (righe 795, 799, 800, 1754, 1922, 1927),
quindi l'ordine degli statement è semantico. Con tagli rigorosi la
concatenazione dei file in ordine di caricamento riproduce il blocco originale
carattere per carattere, il che rende il refactor verificabile meccanicamente.
Riorganizzare per responsabilità è un lavoro successivo e separato.

**Le dichiarazioni di stato condiviso** (riga 722: `let ws`, `agents`,
`activePane`, `refreshInterval`, ...) vanno in `connection.js`, che deve essere
caricato **per primo** fra i moduli.

## Ordine di caricamento

In `index.html`, subito prima di `</body>`, nell'ordine esatto:

```html
<script src="./js/connection.js"></script>
<script src="./js/ansi.js"></script>
<script src="./js/dashboard.js"></script>
<script src="./js/terminal.js"></script>
<script src="./js/input.js"></script>
<script src="./js/push.js"></script>
```

`connection.js` per primo perché dichiara lo stato condiviso. `push.js` per
ultimo perché si registra all'avvio. `security.js` resta dov'è (riga 575).

Il CSS: `<link rel="stylesheet" href="./app.css">` nel `<head>`, al posto del
blocco `<style>` alle righe 22-205.

## Modifica al relay

In `relay/herdr_relay.py:1088`, aggiungere a `static_files`:

```python
"/app.css": ("app.css", "text/css; charset=utf-8", "no-cache"),
"/js/connection.js": ("js/connection.js", "application/javascript", "no-cache"),
"/js/ansi.js": ("js/ansi.js", "application/javascript", "no-cache"),
"/js/dashboard.js": ("js/dashboard.js", "application/javascript", "no-cache"),
"/js/terminal.js": ("js/terminal.js", "application/javascript", "no-cache"),
"/js/input.js": ("js/input.js", "application/javascript", "no-cache"),
"/js/push.js": ("js/push.js", "application/javascript", "no-cache"),
```

`public_paths` li eredita automaticamente. Nessun handler nuovo: il generico
alla riga 1221 li serve. **Non** inserire nulla sopra la riga 1146 (il commento
avverte che le rotte statiche vanno sempre sotto).

## Strategia per i test — la parte non banale

Quattro consumatori leggono `index.html` come **testo** e cercano simboli JS.
Spostare il JS li rompe. La soluzione **non** è puntare ogni test al file
giusto: i confini sono per responsabilità e possono cambiare, quindi un test
legato a "in quale file sta il simbolo" è fragile per costruzione.

Si introduce **un helper unico** che ricompone il sorgente.

### Nuovo file: `tests/web_source.py`

```python
"""Ricompone il sorgente della dashboard dopo lo split in asset esterni."""
import re
from pathlib import Path

WEB = Path(__file__).parent.parent / "web"


def dashboard_markup():
    """Solo index.html — per le asserzioni sul markup."""
    return (WEB / "index.html").read_text(encoding="utf-8")


def dashboard_script():
    """Tutto il JS della dashboard, nell'ordine di caricamento della pagina.

    Concatena i blocchi <script> inline e i file esterni referenziati, nello
    stesso ordine in cui il browser li esegue. Le asserzioni su simboli JS
    restano valide qualunque sia il file che finisce per ospitarli.
    """
    markup = dashboard_markup()
    parts = []
    for match in re.finditer(
        r'<script(?:\s[^>]*)?>(.*?)</script>', markup, re.DOTALL
    ):
        tag = match.group(0)
        src = re.search(r'\bsrc="([^"]+)"', tag)
        if src:
            path = WEB / src.group(1).lstrip("./")
            if path.is_file():
                parts.append(path.read_text(encoding="utf-8"))
        else:
            parts.append(match.group(1))
    return "\n".join(parts)
```

L'ordine di concatenazione segue l'ordine dei tag nella pagina, quindi le
asserzioni posizionali continuano a funzionare (vedi `goHome` sotto).

### Aggiornamenti test per test

| File | Cosa fare |
|---|---|
| `tests/test_client_payloads.py` (righe 10, 25, 41) | sostituire `(ROOT / "web" / "index.html").read_text(...)` con `dashboard_script()`. Gli `assertIn` restano **invariati**. |
| `tests/test_ansi_terminal.py:101` | `page = dashboard_script()`. Copre sia `function ansiFragment` (→ `ansi.js`) sia `format:'ansi'` (dentro `refreshPane()` → `terminal.js`): due file diversi, un solo sorgente ricomposto. |
| `tests/test_web_assets.py::test_going_home_clears_every_filter` | usare `dashboard_script()`. Lo slice `source[index("function goHome()"):index("function closeTerminal()")]` continua a funzionare perché entrambe finiscono in `terminal.js` nello stesso ordine. |
| `tests/test_web_assets.py::test_dashboard_has_no_esm_sh_executable_script` | **estendere**, non solo adattare. Oggi ispeziona `src + body` degli script di `index.html`; dopo lo split i body sono altrove e il test passerebbe a vuoto, perdendo i denti. Deve controllare anche il contenuto dei file esterni. |
| `tests/test_web_assets.py` — resto (`CredentialFieldCollector`, setup guide, `test_the_title_is_a_button`) | **nessuna modifica**: asseriscono sul markup, che resta in `index.html`. |
| `tests/web_controls_harness.js`, `test_terminal_viewport.js`, `test_web_fullscreen.js` | **nessuna modifica**: estraggono blocchi per id, che restano inline. |
| `tests/check_terminal_layout.py` | **nessuna modifica**: Playwright serve da `web/` via route. |
| `tests/run.sh` (check 9 e 10) | **estendere**. Il check 9 fa `grep -q "WebSocket"` e `grep -q "sendKey"` su `index.html`: entrambi i simboli escono nello split. Il check 10 cerca segreti solo lì. Vanno puntati a `web/index.html` + `web/js/*.js`. |
| `tests/test_herdr_relay.py` | aggiungere la verifica che i nuovi asset siano serviti con il content-type giusto, sul modello del test esistente alle righe 296+. |

## Verifica

Nell'ordine. Non passare allo step successivo se il precedente fallisce.

1. `tests/run.sh` — l'intera suite passa.
2. `uv run relay/herdr_relay.py`, aprire la dashboard: nessun 404 in console,
   nessun `ReferenceError`.
3. Provare a mano gli handler inline, che sono il rischio principale:
   apertura terminale, `goHome` dal titolo, ricerca, command palette,
   tastiera mobile (`fireKey`, `quickSend`, `pressCtrl`).
4. `git diff --stat` — `index.html` deve calare di ~1560 righe e i nuovi file
   devono sommare grosso modo altrettanto. Uno scarto grosso significa codice
   perso o duplicato.

## Fuori scope

- Convertire gli handler inline ad `addEventListener` (refactor separato e molto
  più grande; è ciò che sbloccherebbe gli ES module).
- Introdurre un build step, un bundler o un framework.
- Qualsiasi modifica di comportamento, stile o copy.
- Spostare i quattro blocchi `<script id="...">`.
- Toccare `sw.js` o `security.js`.
