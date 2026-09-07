# Split di `web/index.html` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Portare `web/index.html` da 1930 a ~370 righe estraendo CSS e JS in asset esterni, senza alcun cambiamento di comportamento.

**Architecture:** Script classici (non ES module) caricati in ordine fisso, globali preservati. Estrazione **dal fondo verso l'alto** per mantenere contiguo il blocco inline residuo. Range di righe rigorosi: nessuna funzione cambia file, così la concatenazione dei file in ordine di caricamento riproduce il blocco originale.

**Tech Stack:** HTML/CSS/JS senza build step; relay Python (websockets); test in unittest + node `vm` + Playwright.

**Spec:** `docs/superpowers/specs/2026-09-07-web-index-split-design.md`

## Global Constraints

- **Nessun ES module.** 74 handler inline nel markup invocano 35 funzioni globali. `<script type="module">` li romperebbe tutti. Solo `<script src="...">` classici.
- **L'ordine di caricamento è semantico.** Il blocco contiene statement top-level eseguibili alle righe 795, 799, 800, 1754, 1922, 1927. `if (savedUrl && relayToken) connect();` (1927) deve restare l'ultima istruzione eseguita.
- **`security.js` deve caricarsi prima di `connection.js`**: la riga 799 chiama `HerdrSecurity.readStoredRelayToken`.
- **Nessuna funzione cambia file.** Tagli per range di righe, non per responsabilità.
- **Nessun cambiamento funzionale, di stile o di copy.**
- **I 4 blocchi `<script id="...">` (373-719) restano inline.** L'harness `vm` li estrae per id.
- Non inserire rotte statiche sopra `relay/herdr_relay.py:1146` (il commento lì lo vieta esplicitamente).

## Ordine di caricamento finale

```html
<script src="./js/connection.js"></script>
<script src="./js/ansi.js"></script>
<script src="./js/dashboard.js"></script>
<script src="./js/terminal.js"></script>
<script src="./js/input.js"></script>
<script src="./js/push.js"></script>
```

## Verifica ricorrente

Dopo ogni task:

```bash
tests/run.sh
```

E il controllo di integrità meccanico — la concatenazione dei file estratti, in
ordine di caricamento, più il blocco inline residuo, deve contenere le stesse
righe del blocco originale:

```bash
git show HEAD:web/index.html | sed -n '722,1927p' > /tmp/orig-block.js
cat web/js/*.js  # confronta a occhio il totale righe con /tmp/orig-block.js
```

---

### Task 1: Rendere i test resilienti PRIMA di spostare codice

Questo task non sposta una riga di JS. Introduce l'helper che ricompone il
sorgente mentre tutto è ancora inline, così i test restano verdi a ogni passo
successivo.

**Files:**
- Create: `tests/web_source.py`
- Modify: `tests/test_client_payloads.py:10,25,41`
- Modify: `tests/test_ansi_terminal.py:101`
- Modify: `tests/test_web_assets.py` (`test_going_home_clears_every_filter`)

- [ ] **Step 1: creare l'helper**

```python
# tests/web_source.py
"""Ricompone il sorgente della dashboard dopo lo split in asset esterni."""
import re
from pathlib import Path

WEB = Path(__file__).parent.parent / "web"


def dashboard_markup():
    """Solo index.html — per le asserzioni sul markup."""
    return (WEB / "index.html").read_text(encoding="utf-8")


def dashboard_script():
    """Tutto il JS della dashboard, nell'ordine di esecuzione della pagina.

    Concatena i blocchi <script> inline e i file esterni referenziati nello
    stesso ordine in cui il browser li esegue, così le asserzioni su simboli
    JS restano valide qualunque file finisca per ospitarli.
    """
    markup = dashboard_markup()
    parts = []
    for match in re.finditer(r"<script(?:\s[^>]*)?>(.*?)</script>", markup, re.DOTALL):
        src = re.search(r'\bsrc="([^"]+)"', match.group(0))
        if src:
            path = WEB / src.group(1).lstrip("./")
            if path.is_file():
                parts.append(path.read_text(encoding="utf-8"))
        else:
            parts.append(match.group(1))
    return "\n".join(parts)
```

- [ ] **Step 2: verificare che l'helper veda già il codice inline**

Run:
```bash
cd /home/maxto/projects/personal/herdr-remote
python3 -c "
import sys; sys.path.insert(0, 'tests')
from web_source import dashboard_script
s = dashboard_script()
for sym in ('function ansiFragment', \"format:'ansi'\", 'function goHome()', 'function closeTerminal()', 'sessionBadge(a)', 'ambiguousLabels'):
    assert sym in s, sym
print('ok', len(s), 'caratteri')
"
```
Expected: `ok <N> caratteri`. Se fallisce, la regex non sta catturando i blocchi inline — sistemare prima di procedere.

- [ ] **Step 3: repuntare i test sul sorgente ricomposto**

In `tests/test_client_payloads.py`, aggiungere in cima `from web_source import dashboard_script` e sostituire le tre occorrenze di
`source = (ROOT / "web" / "index.html").read_text(encoding="utf-8")` con
`source = dashboard_script()`. Gli `assertIn` restano invariati.

In `tests/test_ansi_terminal.py:101`, sostituire
`page = (WEB_DIR / "index.html").read_text(encoding="utf-8")` con
`page = dashboard_script()`.

In `tests/test_web_assets.py::test_going_home_clears_every_filter`, sostituire
`source = (Path(__file__).parent.parent / "web" / "index.html").read_text()` con
`source = dashboard_script()`. Lo slice fra `function goHome()` e
`function closeTerminal()` continua a funzionare: restano nello stesso file e
nello stesso ordine.

**Non toccare** `CredentialFieldCollector`, `test_setup_guide_points_at_this_fork`
e `test_the_title_is_a_button`: asseriscono sul markup, che resta in `index.html`.

- [ ] **Step 4: la suite deve essere verde con tutto ancora inline**

Run: `tests/run.sh`
Expected: `0 failed`. Nessun comportamento è cambiato — se qualcosa fallisce qui, è l'helper ad essere sbagliato.

- [ ] **Step 5: commit**

```bash
git add tests/web_source.py tests/test_client_payloads.py tests/test_ansi_terminal.py tests/test_web_assets.py
git commit -m "test(web): read dashboard JS through a recomposed source helper"
```

---

### Task 2: Estrarre `web/app.css`

Il CSS non ha dipendenze d'ordine: si estrae per primo e prova end-to-end la
catena relay + Playwright.

**Files:**
- Create: `web/app.css`
- Modify: `web/index.html:22-205` (blocco `<style>`; il blocco `data-font-face` alle righe 13-21 **resta**)
- Modify: `relay/herdr_relay.py:1088`

- [ ] **Step 1: estrarre il blocco**

Spostare il contenuto fra `<style>` (riga 22) e `</style>` (riga 205) in `web/app.css`, e sostituire il blocco nel `<head>` con:

```html
<link rel="stylesheet" href="./app.css">
```

- [ ] **Step 2: registrare l'asset nel relay**

In `relay/herdr_relay.py`, dentro il dict `static_files` (riga 1088):

```python
"/app.css": ("app.css", "text/css; charset=utf-8", "no-cache"),
```

`public_paths` lo eredita via `*static_files`; il generico alla riga 1221 lo serve. Nessun handler nuovo.

- [ ] **Step 3: la suite deve restare verde**

Run: `tests/run.sh`
Expected: `0 failed`.

- [ ] **Step 4: verifica visiva**

Run: `uv run relay/herdr_relay.py`, aprire la dashboard.
Expected: identica a prima, nessun 404 su `/app.css` nella console di rete.

- [ ] **Step 5: commit**

```bash
git add web/app.css web/index.html relay/herdr_relay.py
git commit -m "refactor(web): extract the stylesheet into app.css"
```

---

### Task 3: Estrarre `web/js/push.js` (righe 1846-1927)

Si parte **dal fondo**: il blocco inline residuo resta contiguo da riga 722 e il
file esterno si accoda dopo, preservando l'ordine di esecuzione.

Contiene `initPush()` (chiamata top-level a riga 1922) e l'istruzione finale
`if (savedUrl && relayToken) connect();` (1927), che deve restare l'ultima cosa
eseguita della pagina.

**Files:**
- Create: `web/js/push.js`
- Modify: `web/index.html:1846-1927`
- Modify: `relay/herdr_relay.py:1088`

- [ ] **Step 1: estrarre**

Spostare le righe 1846-1927 in `web/js/push.js`. Chiudere il blocco inline dopo la riga 1845 con `</script>` e aggiungere subito dopo:

```html
<script src="./js/push.js"></script>
```

- [ ] **Step 2: registrare l'asset**

```python
"/js/push.js": ("js/push.js", "application/javascript", "no-cache"),
```

- [ ] **Step 3: suite verde**

Run: `tests/run.sh`
Expected: `0 failed`.

- [ ] **Step 4: verifica del bootstrap**

Run: `uv run relay/herdr_relay.py`, aprire la dashboard con un relay salvato in `localStorage`.
Expected: si connette da sola all'avvio (è l'effetto di `connect()` alla riga 1927). Nessun `ReferenceError` in console.

- [ ] **Step 5: commit**

```bash
git add web/js/push.js web/index.html relay/herdr_relay.py
git commit -m "refactor(web): extract web push into js/push.js"
```

---

### Task 4: Estrarre `web/js/input.js` (righe 1615-1845)

Tastiera, key queue, ctrl presets, command palette. Contiene lo statement
top-level `if (savedUrl) setTimeout(connect, 100); else showSetup();` (1754),
che deve restare nella sua posizione relativa.

**Files:**
- Create: `web/js/input.js`
- Modify: `web/index.html:1615-1845`
- Modify: `relay/herdr_relay.py:1088`

- [ ] **Step 1: estrarre** le righe 1615-1845 in `web/js/input.js`; `<script src="./js/input.js"></script>` **prima** del tag di `push.js`.
- [ ] **Step 2: registrare** `"/js/input.js": ("js/input.js", "application/javascript", "no-cache"),`
- [ ] **Step 3: suite verde** — `tests/run.sh`, `0 failed`.
- [ ] **Step 4: verifica manuale** — aprire un terminale e provare la tastiera mobile (`fireKey`, `quickSend`, `pressCtrl`) e la command palette. Sono handler inline: un `ReferenceError` qui significa ordine di caricamento sbagliato.
- [ ] **Step 5: commit**

```bash
git add web/js/input.js web/index.html relay/herdr_relay.py
git commit -m "refactor(web): extract keyboard and palette into js/input.js"
```

---

### Task 5: Estrarre `web/js/terminal.js` (righe 1273-1614)

Vista pane, history, ricerca, menu contestuale.

**Files:**
- Create: `web/js/terminal.js`
- Modify: `web/index.html:1273-1614`
- Modify: `relay/herdr_relay.py:1088`

- [ ] **Step 1: estrarre** le righe 1273-1614 in `web/js/terminal.js`; tag **prima** di `input.js`.
- [ ] **Step 2: registrare** `"/js/terminal.js": ("js/terminal.js", "application/javascript", "no-cache"),`
- [ ] **Step 3: suite verde** — `tests/run.sh`. Attenzione a `test_going_home_clears_every_filter`: `goHome` e `closeTerminal` finiscono entrambe qui, nello stesso ordine, quindi lo slice regge.
- [ ] **Step 4: verifica manuale** — aprire un terminale, `goHome` dal titolo, ricerca nel pane, menu contestuale (rename/close/interrupt).
- [ ] **Step 5: commit**

```bash
git add web/js/terminal.js web/index.html relay/herdr_relay.py
git commit -m "refactor(web): extract the terminal view into js/terminal.js"
```

---

### Task 6: Estrarre `web/js/dashboard.js` (righe 1030-1272)

`handleMessage` e il rendering di lista agenti e workspace.

**Files:**
- Create: `web/js/dashboard.js`
- Modify: `web/index.html:1030-1272`
- Modify: `relay/herdr_relay.py:1088`

- [ ] **Step 1: estrarre** le righe 1030-1272 in `web/js/dashboard.js`; tag **prima** di `terminal.js`.
- [ ] **Step 2: registrare** `"/js/dashboard.js": ("js/dashboard.js", "application/javascript", "no-cache"),`
- [ ] **Step 3: suite verde** — `tests/run.sh`. Qui vive gran parte di ciò che assertisce `test_client_payloads.py`.
- [ ] **Step 4: verifica manuale** — connettersi a un relay reale: la lista agenti si popola, i badge di sessione compaiono, la navigazione workspace/tab funziona.
- [ ] **Step 5: commit**

```bash
git add web/js/dashboard.js web/index.html relay/herdr_relay.py
git commit -m "refactor(web): extract the agent dashboard into js/dashboard.js"
```

---

### Task 7: Estrarre `web/js/ansi.js` (righe 961-1029)

`ansiFragment`, foglia pura senza dipendenze in entrata.

**Files:**
- Create: `web/js/ansi.js`
- Modify: `web/index.html:961-1029`
- Modify: `relay/herdr_relay.py:1088`

- [ ] **Step 1: estrarre** le righe 961-1029 in `web/js/ansi.js`; tag **prima** di `dashboard.js`.
- [ ] **Step 2: registrare** `"/js/ansi.js": ("js/ansi.js", "application/javascript", "no-cache"),`
- [ ] **Step 3: suite verde** — `tests/run.sh`. `test_ansi_terminal.py` cerca `function ansiFragment` (ora qui) e `format:'ansi'` (in `terminal.js`): li trova entrambi solo attraverso `dashboard_script()`, introdotto nel Task 1.
- [ ] **Step 4: verifica manuale** — leggere un pane con output colorato: i colori ANSI devono rendersi.
- [ ] **Step 5: commit**

```bash
git add web/js/ansi.js web/index.html relay/herdr_relay.py
git commit -m "refactor(web): extract the ANSI renderer into js/ansi.js"
```

---

### Task 8: Estrarre `web/js/connection.js` (righe 722-960)

L'ultimo pezzo: stato globale condiviso, WebSocket, status, storage, setup, demo.
Il blocco inline finale sparisce del tutto.

**Files:**
- Create: `web/js/connection.js`
- Modify: `web/index.html:720-960` (rimuovere il tag `<script>` residuo)
- Modify: `relay/herdr_relay.py:1088`

- [ ] **Step 1: estrarre** le righe 722-960 in `web/js/connection.js` ed eliminare il tag `<script>` inline ormai vuoto (righe 720-721 e la sua chiusura). Il tag va **prima** di `ansi.js` e **dopo** `security.js`: la riga 799 chiama `HerdrSecurity.readStoredRelayToken`.
- [ ] **Step 2: registrare** `"/js/connection.js": ("js/connection.js", "application/javascript", "no-cache"),`
- [ ] **Step 3: verificare l'ordine dei tag**

Run:
```bash
grep -n '<script' web/index.html | tail -10
```
Expected, in quest'ordine: `security.js`, il blocco id'd `terminalAttachments`, poi `connection.js`, `ansi.js`, `dashboard.js`, `terminal.js`, `input.js`, `push.js`.

- [ ] **Step 4: suite verde** — `tests/run.sh`, `0 failed`.
- [ ] **Step 5: verifica manuale completa** — ricaricare a freddo con `localStorage` vuoto: deve comparire la schermata di setup; salvare un relay e connettersi; ricaricare: deve riconnettersi da sola.
- [ ] **Step 6: commit**

```bash
git add web/js/connection.js web/index.html relay/herdr_relay.py
git commit -m "refactor(web): extract connection and shared state into js/connection.js"
```

---

### Task 9: Chiudere i buchi lasciati nei test dallo split

Due controlli hanno perso i denti perché cercavano in `index.html` codice che
ora sta altrove, e passerebbero a vuoto.

**Files:**
- Modify: `tests/run.sh:~"9. web app key elements"` e `~"10. web app no hardcoded secrets"`
- Modify: `tests/test_web_assets.py::test_dashboard_has_no_esm_sh_executable_script`
- Modify: `tests/test_herdr_relay.py` (accanto al test dei content-type, riga ~296)

- [ ] **Step 1: `tests/run.sh` — estendere i grep a tutti gli asset web**

Il check 9 fa `grep -q "WebSocket"` e `grep -q "sendKey"` su `index.html`: entrambi i simboli sono usciti. Il check 10 cerca segreti solo in `index.html`. Far puntare entrambi all'insieme dei file:

```sh
WEB="$DIR/web/index.html"
WEB_ALL="$DIR/web/index.html $DIR/web/js/*.js"

echo "9. web app key elements"
grep -qh "WebSocket" $WEB_ALL && grep -q "theme" "$WEB" && grep -qh "sendKey" $WEB_ALL
assert_eq "$?" "0" "has WebSocket, themes, keyboard"

echo "10. web app no hardcoded secrets"
! grep -qh "c4a2385e" $WEB_ALL && ! grep -qh "graffold" $WEB_ALL
assert_eq "$?" "0" "no secrets in web app"
```

- [ ] **Step 2: verificare che il check 9 fallisca se un simbolo sparisce davvero**

Run:
```bash
cd /home/maxto/projects/personal/herdr-remote
cp web/js/input.js /tmp/input.bak
sed -i 's/function sendKey(/function sendKeyRENAMED(/' web/js/input.js
tests/run.sh 2>&1 | grep "9\." -A2
cp /tmp/input.bak web/js/input.js
```
Expected: il check 9 **fallisce** con la rinomina e torna verde dopo il ripristino. Se resta verde in entrambi i casi, il grep non sta guardando i file giusti.

- [ ] **Step 3: ridare i denti al controllo su esm.sh**

`test_dashboard_has_no_esm_sh_executable_script` ispeziona `src + body` degli
script di `index.html`. I body ora sono nei file esterni, quindi passerebbe a
vuoto. Aggiungere il sorgente ricomposto al materiale ispezionato:

```python
    def test_dashboard_has_no_esm_sh_executable_script(self):
        parser = ScriptCollector()
        parser.feed(dashboard_markup())
        sources = [
            script["attrs"].get("src", "") + script["body"]
            for script in parser.scripts
        ]
        sources.append(dashboard_script())

        for source in sources:
            self.assertNotIn("esm.sh", source)
```

Mantenere il nome della classe parser già in uso nel file. L'aggiunta di
`dashboard_script()` copre il codice che ora vive nei file esterni; il parsing
dei tag continua a coprire gli attributi `src`.

- [ ] **Step 4: coprire i nuovi content-type nel relay**

In `tests/test_herdr_relay.py`, accanto alla lista alla riga ~296 (`("/index.html", "text/html")`), aggiungere i nuovi asset e il loro content-type atteso:

```python
("/app.css", "text/css"),
("/js/connection.js", "application/javascript"),
("/js/ansi.js", "application/javascript"),
("/js/dashboard.js", "application/javascript"),
("/js/terminal.js", "application/javascript"),
("/js/input.js", "application/javascript"),
("/js/push.js", "application/javascript"),
```

- [ ] **Step 5: suite verde** — `tests/run.sh`, `0 failed`.

- [ ] **Step 6: controllo finale di integrità**

Run:
```bash
wc -l web/index.html web/app.css web/js/*.js
git diff --stat main -- web/
```
Expected: `index.html` ~370 righe; la somma dei nuovi file ~1390; il totale complessivo entro poche righe delle 1930 iniziali. Uno scarto ampio significa codice perso o duplicato.

- [ ] **Step 7: commit**

```bash
git add tests/run.sh tests/test_web_assets.py tests/test_herdr_relay.py
git commit -m "test(web): cover the split assets in the shell and relay checks"
```
