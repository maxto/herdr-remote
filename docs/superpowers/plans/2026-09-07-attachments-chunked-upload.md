# Allegati a blocchi — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reintrodurre il pulsante `+` della composer per testo, immagini e PDF, con upload a blocchi sul WebSocket esistente.

**Architecture:** Il file sale in blocchi da 256 KiB base64, scritti in streaming su un temporaneo; il relay lo valida al `begin` (tipo, dimensione, quota) e al `commit` (firma del contenuto, totale esatto), poi passa all'agente il percorso assoluto. Ogni ack di blocco fa da controllo di flusso.

**Tech Stack:** Python (`websockets`), HTML/JS senza build step, unittest + node `vm` + Playwright.

**Spec:** `docs/superpowers/specs/2026-09-07-attachments-chunked-upload-design.md`

## Global Constraints

- **`WS_MAX_SIZE` scende a 512 KiB.** Nessun messaggio del protocollo può superarlo: il blocco è 256 KiB di base64 più metadati.
- **Il MIME del browser non è affidabile.** Il relay valida sempre estensione e contenuto.
- **Nessun allegato viene servito via HTTP né scritto nei log.** L'audit registra solo nome, tipo e dimensione.
- **Indici rigorosamente sequenziali.** Il WebSocket garantisce l'ordine: duplicati, salti e sforamenti abortiscono l'upload.
- **Quota prenotata atomicamente al `begin`**, rilasciata al commit, abort, timeout o disconnessione.
- **Un solo upload attivo per connessione**, 64 MiB pendenti globali.
- Limiti per tipo: testo 1 MiB, immagini 10 MiB, PDF 25 MiB. Archivio permanente 200 MiB / 100 file.
- **Nessun resume**: un upload interrotto riparte da zero.

## Stato di partenza

La rimozione degli allegati è **committata** in `fe8fc34`. Il punto di partenza da recuperare è `b2b7640`, l'ultimo commit che li conteneva: non c'è nulla da riscrivere da zero. Contiene già quote, scrittura atomica, rifiuto dei symlink e pulizia su fallimento parziale, tutto coperto da test.

## Verifica ricorrente

Dopo ogni task:

```bash
tests/run.sh
HERDR_BROWSER_TESTS=1 tests/run.sh    # dal Task 7 in poi
```

---

### Task 1: Separare validazione della dichiarazione da validazione del contenuto

Col chunking i due momenti si allontanano: al `begin` non esiste ancora un byte, al `commit` c'è un file completo. `decode_attachment` di `HEAD` fa entrambe le cose su un blob unico e va spezzato.

**Files:**
- Create: `relay/attachments.py` (da `b2b7640`, l'ultimo commit prima della rimozione)
- Create: `tests/test_attachments.py` (da `git show b2b7640:tests/test_attachments.py`)

- [ ] **Step 1: recuperare i file da HEAD**

```bash
cd /home/maxto/projects/personal/herdr-remote
git show b2b7640:relay/attachments.py > relay/attachments.py
git show b2b7640:tests/test_attachments.py > tests/test_attachments.py
```

- [ ] **Step 2: verificare che i test recuperati passino**

Run: `uv run --with 'websockets>=14.0' python -m unittest tests.test_attachments -v`
Expected: tutti verdi. Questo è il punto di partenza noto-buono; se fallisce, fermarsi e capire perché prima di modificare.

- [ ] **Step 3: scrivere i test della nuova superficie**

Aggiungere in `tests/test_attachments.py`:

```python
    def test_a_declaration_is_judged_before_any_byte_arrives(self):
        """The begin handshake has a name, a type and a size — no data yet."""
        from attachments import AttachmentError, validate_declaration
        self.assertEqual(validate_declaration("a.pdf", "application/pdf", 1024), ".pdf")
        self.assertEqual(validate_declaration("a.png", "image/png", 1024), ".png")
        with self.assertRaises(AttachmentError):
            validate_declaration("a.pdf", "application/pdf", 26 * 1024 * 1024)
        with self.assertRaises(AttachmentError):
            validate_declaration("a.zip", "application/zip", 10)

    def test_each_type_carries_its_own_ceiling(self):
        from attachments import AttachmentError, validate_declaration
        validate_declaration("a.txt", "text/plain", 1024 * 1024)
        with self.assertRaises(AttachmentError):
            validate_declaration("a.txt", "text/plain", 1024 * 1024 + 1)
        validate_declaration("a.png", "image/png", 10 * 1024 * 1024)
        with self.assertRaises(AttachmentError):
            validate_declaration("a.png", "image/png", 10 * 1024 * 1024 + 1)

    def test_pdf_content_needs_a_header_and_a_trailer(self):
        from attachments import AttachmentError, verify_content
        verify_content(b"%PDF-1.4\n" + b"x" * 100 + b"\n%%EOF\n", "application/pdf")
        with self.assertRaises(AttachmentError):
            verify_content(b"not a pdf at all", "application/pdf")
        with self.assertRaises(AttachmentError):
            verify_content(b"%PDF-1.4\n" + b"x" * 2000, "application/pdf")

    def test_text_must_be_utf8_without_null_bytes_and_json_must_parse(self):
        from attachments import AttachmentError, verify_content
        verify_content("ciao è".encode("utf-8"), "text/plain")
        with self.assertRaises(AttachmentError):
            verify_content(b"ciao\x00mondo", "text/plain")
        with self.assertRaises(AttachmentError):
            verify_content(b"\xff\xfe not utf8", "text/markdown")
        verify_content(b'{"a": 1}', "application/json")
        with self.assertRaises(AttachmentError):
            verify_content(b'{"a": ', "application/json")
```

- [ ] **Step 4: eseguire i test e vederli fallire**

Run: `uv run --with 'websockets>=14.0' python -m unittest tests.test_attachments -v`
Expected: FAIL con `ImportError: cannot import name 'validate_declaration'`.

- [ ] **Step 5: implementare**

In `relay/attachments.py`, sostituire le costanti e aggiungere le due funzioni. `decode_attachment` non serve più al nuovo protocollo: rimuoverla insieme ai suoi test, che le funzioni nuove sostituiscono.

```python
MAX_STORAGE_BYTES = 200 * 1024 * 1024
MAX_STORAGE_FILES = 100

EXTENSIONS = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
    "application/pdf": ".pdf",
    "text/plain": ".txt", "text/markdown": ".md",
    "text/csv": ".csv", "application/json": ".json",
}
LIMITS = {
    "image/png": 10 * 1024 * 1024, "image/jpeg": 10 * 1024 * 1024,
    "image/webp": 10 * 1024 * 1024, "application/pdf": 25 * 1024 * 1024,
    "text/plain": 1024 * 1024, "text/markdown": 1024 * 1024,
    "text/csv": 1024 * 1024, "application/json": 1024 * 1024,
}


def validate_declaration(name, mime, size):
    """Judge a file before a byte of it arrives; return its extension."""
    if not isinstance(name, str) or not name or len(name) > 255:
        raise AttachmentError("Invalid attachment filename")
    if not isinstance(mime, str) or mime not in EXTENSIONS:
        raise AttachmentError("Choose an image, a PDF or a text file")
    if not isinstance(size, int) or size <= 0:
        raise AttachmentError("Invalid attachment size")
    if size > LIMITS[mime]:
        mib = LIMITS[mime] // (1024 * 1024)
        raise AttachmentError(f"This file type is limited to {mib} MiB")
    return EXTENSIONS[mime]
```

`verify_content(data, mime)` riusa **invariate** le firme immagine già presenti in `decode_attachment`, e aggiunge:

```python
def verify_content(data, mime):
    """Judge assembled bytes against the type their sender claimed."""
    if mime == "application/pdf":
        if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-1024:]:
            raise AttachmentError("This file does not look like a PDF")
        return
    if mime in {"text/plain", "text/markdown", "text/csv", "application/json"}:
        if b"\x00" in data:
            raise AttachmentError("A text attachment cannot contain null bytes")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise AttachmentError("A text attachment must be UTF-8") from None
        if mime == "application/json":
            import json as _json
            try:
                _json.loads(text)
            except ValueError:
                raise AttachmentError("This file is not valid JSON") from None
        return
    # Immagini: spostare qui, invariate, le tre verifiche di firma che oggi
    # stanno dentro decode_attachment (PNG con IHDR/IEND e dimensioni non nulle,
    # JPEG con SOI/EOI, WebP con RIFF/WEBP e lunghezza coerente), sostituendo il
    # dizionario `valid` con un if per mime e alzando lo stesso AttachmentError.
    if mime not in EXTENSIONS:
        raise AttachmentError("Choose an image, a PDF or a text file")
```

- [ ] **Step 6: eseguire i test e vederli passare**

Run: `uv run --with 'websockets>=14.0' python -m unittest tests.test_attachments -v`
Expected: OK.

- [ ] **Step 7: commit**

```bash
git add relay/attachments.py tests/test_attachments.py
git commit -m "feat(relay): judge attachments by declaration and by content"
```

---

### Task 2: Raccogliere i blocchi in streaming su un temporaneo

**Files:**
- Modify: `relay/attachments.py`
- Modify: `tests/test_attachments.py`

- [ ] **Step 1: scrivere i test**

```python
    def test_chunks_land_in_order_and_abort_on_any_break(self):
        from attachments import AttachmentError, UploadSink
        with tempfile.TemporaryDirectory() as d:
            sink = UploadSink(Path(d), ".txt", total=6)
            sink.write(0, b"abc")
            sink.write(1, b"def")
            self.assertEqual(sink.received, 6)

            gap = UploadSink(Path(d), ".txt", total=6)
            gap.write(0, b"abc")
            with self.assertRaises(AttachmentError):
                gap.write(2, b"def")          # salto
            self.assertFalse(gap.temp_path.exists())

            dup = UploadSink(Path(d), ".txt", total=6)
            dup.write(0, b"abc")
            with self.assertRaises(AttachmentError):
                dup.write(0, b"abc")          # duplicato
            self.assertFalse(dup.temp_path.exists())

    def test_a_sender_cannot_exceed_what_it_declared(self):
        from attachments import AttachmentError, UploadSink
        with tempfile.TemporaryDirectory() as d:
            sink = UploadSink(Path(d), ".txt", total=4)
            with self.assertRaises(AttachmentError):
                sink.write(0, b"toolong")
            self.assertFalse(sink.temp_path.exists())

    def test_abort_and_finish_both_leave_no_temporary_behind(self):
        from attachments import UploadSink
        with tempfile.TemporaryDirectory() as d:
            sink = UploadSink(Path(d), ".txt", total=3)
            sink.write(0, b"abc")
            final = sink.finish()
            self.assertTrue(final.exists())
            self.assertFalse(sink.temp_path.exists())

            other = UploadSink(Path(d), ".txt", total=3)
            other.write(0, b"ab")
            other.abort()
            self.assertFalse(other.temp_path.exists())

    def test_finishing_short_of_the_declared_total_is_refused(self):
        from attachments import AttachmentError, UploadSink
        with tempfile.TemporaryDirectory() as d:
            sink = UploadSink(Path(d), ".txt", total=10)
            sink.write(0, b"abc")
            with self.assertRaises(AttachmentError):
                sink.finish()
            self.assertFalse(sink.temp_path.exists())
```

- [ ] **Step 2: eseguirli e vederli fallire** — `ImportError: cannot import name 'UploadSink'`.

- [ ] **Step 3: implementare `UploadSink`**

Una classe che apre un temporaneo con `tempfile.mkstemp` nella directory di destinazione (stesso filesystem, così il rename finale è atomico), scrive in append, tiene `next_index` e `received`, e cancella il temporaneo su qualunque errore. `finish()` verifica `received == total`, chiama `verify_content` sui byte riletti, poi rinomina. Riusare i permessi owner-only già impostati da `AttachmentStore`.

- [ ] **Step 4: test verdi.**

- [ ] **Step 5: commit**

```bash
git add relay/attachments.py tests/test_attachments.py
git commit -m "feat(relay): collect attachment chunks into a streamed temporary"
```

---

### Task 3: Prenotare la quota al begin

Senza prenotazione atomica, N upload concorrenti superano il controllo nello stesso istante e insieme saturano il disco.

**Files:**
- Modify: `relay/attachments.py`
- Modify: `tests/test_attachments.py`

- [ ] **Step 1: test**

```python
    def test_pending_reservations_are_atomic_across_threads(self):
        from attachments import AttachmentError, UploadQuota
        quota = UploadQuota(max_pending_bytes=10 * 1024 * 1024)
        granted, refused = [], []

        def reserve():
            try:
                granted.append(quota.reserve(4 * 1024 * 1024))
            except AttachmentError:
                refused.append(1)

        threads = [threading.Thread(target=reserve) for _ in range(5)]
        for t in threads: t.start()
        for t in threads: t.join()

        self.assertEqual(len(granted), 2, "10 MiB must fit exactly two 4 MiB uploads")
        self.assertEqual(len(refused), 3)
        for token in granted:
            quota.release(token)
        self.assertEqual(quota.pending_bytes, 0)
```

- [ ] **Step 2: eseguirli e vederli fallire.**
- [ ] **Step 3: implementare `UploadQuota`** con un `threading.Lock`, `reserve(size)` che alza `AttachmentError` se lo spazio pendente non basta, e `release(token)` idempotente.
- [ ] **Step 4: test verdi.**
- [ ] **Step 5: commit**

```bash
git add relay/attachments.py tests/test_attachments.py
git commit -m "feat(relay): reserve upload quota atomically"
```

---

### Task 4: I quattro handler di protocollo

**Files:**
- Modify: `relay/herdr_relay.py`
- Modify: `tests/test_herdr_relay.py`

- [ ] **Step 1: test del percorso felice e dei rifiuti**

Sul modello di `_dispatch` già presente nel file, con una sequenza di messaggi:

```python
    def test_an_upload_walks_begin_chunk_commit(self):
        with loaded_relay() as relay:
            self._register_sessions(relay)
            png = base64.b64encode(_TINY_PNG).decode()
            _, result_run, ws = self._dispatch_many(relay, [
                {"type": "attachment_begin", "request_id": "u1", "pane_id": "mxdb:w1:p1",
                 "name": "a.png", "mime": "image/png", "size": len(_TINY_PNG)},
                {"type": "attachment_chunk", "index": 0, "data": png},
                {"type": "attachment_commit", "text": "guarda"},
            ])
            sent = [json.loads(m) for m in ws.sent]
            self.assertTrue(any(m.get("command") == "attachment_begin" for m in sent))
            self.assertTrue(any(m.get("command") == "attachment_commit" and m.get("ok") for m in sent))
            prompt = result_run.call_args[0]
            self.assertIn("agent", prompt)

    def test_a_chunk_without_a_begin_is_refused(self):
        with loaded_relay() as relay:
            self._register_sessions(relay)
            _, _, ws = self._dispatch(relay, {
                "type": "attachment_chunk", "upload_id": "never-issued",
                "index": 0, "data": "AAAA",
            })
            self.assertEqual(json.loads(ws.sent[-1])["type"], "error")

    def test_an_out_of_order_index_aborts_the_upload(self):
        with loaded_relay() as relay:
            self._register_sessions(relay)
            _, _, ws = self._dispatch_many(relay, [
                {"type": "attachment_begin", "request_id": "u2", "pane_id": "mxdb:w1:p1",
                 "name": "a.txt", "mime": "text/plain", "size": 6},
                {"type": "attachment_chunk", "index": 0, "data": _b64("abc")},
                {"type": "attachment_chunk", "index": 2, "data": _b64("def")},
            ])
            self.assertEqual(json.loads(ws.sent[-1])["type"], "error")
            self.assertEqual(relay.active_uploads, {})

    def test_a_second_upload_on_one_connection_is_refused(self):
        with loaded_relay() as relay:
            self._register_sessions(relay)
            begin = {"type": "attachment_begin", "pane_id": "mxdb:w1:p1",
                     "name": "a.txt", "mime": "text/plain", "size": 3}
            _, _, ws = self._dispatch_many(relay, [
                {**begin, "request_id": "u3"}, {**begin, "request_id": "u4"},
            ])
            last = json.loads(ws.sent[-1])
            self.assertEqual(last["type"], "error")
            self.assertIn("one upload", last["message"].lower())

    def test_abort_frees_the_reservation_and_the_temporary(self):
        with loaded_relay() as relay:
            self._register_sessions(relay)
            _, _, ws = self._dispatch_many(relay, [
                {"type": "attachment_begin", "request_id": "u5", "pane_id": "mxdb:w1:p1",
                 "name": "a.txt", "mime": "text/plain", "size": 6},
                {"type": "attachment_chunk", "index": 0, "data": _b64("abc")},
                {"type": "attachment_abort"},
            ])
            self.assertTrue(json.loads(ws.sent[-1])["ok"])
            self.assertEqual(relay.active_uploads, {})
            self.assertEqual(relay.upload_quota.pending_bytes, 0)
```

`_dispatch_many` è un helper nuovo, gemello di `_dispatch`, che accetta una
lista di messaggi, li invia sulla stessa connessione, e **riscrive l'`upload_id`**
dei messaggi successivi con quello restituito dal `begin` — così i test non
devono conoscere un id generato a runtime. `_b64(s)` è una scorciatoia per
`base64.b64encode(s.encode()).decode()`.

- [ ] **Step 2: eseguirli e vederli fallire.**

- [ ] **Step 3: implementare gli handler** nel dispatcher di `handle_client`, accanto ad `agent_prompt`. Un dizionario `active_uploads` per connessione, con `upload_id` da `secrets.token_urlsafe`. Il `commit` costruisce il prompt e riusa **lo stesso percorso di `agent_prompt`**, inclusa la conferma anticipata su agente occupato già implementata:

```python
prompt = f"Inspect the attached local file at this absolute path:\n{path}"
if text:
    prompt += f"\n\n{text}"
```

Se il prompt fallisce **prima** dell'accettazione, cancellare il file finale; **dopo** l'accettazione conservarlo.

- [ ] **Step 4: test verdi.**
- [ ] **Step 5: commit**

```bash
git add relay/herdr_relay.py tests/test_herdr_relay.py
git commit -m "feat(relay): accept attachments as begin, chunk, commit and abort"
```

---

### Task 5: Ciclo di vita — timeout, disconnessione, avvio

**Files:**
- Modify: `relay/herdr_relay.py`
- Modify: `tests/test_herdr_relay.py`

- [ ] **Step 1: test**

```python
    def test_a_dropped_connection_takes_its_temporaries_with_it(self):
        """handle_client returns when the socket ends; nothing may survive it."""
        with loaded_relay() as relay:
            self._register_sessions(relay)
            _, _, _ = self._dispatch_many(relay, [
                {"type": "attachment_begin", "request_id": "u6", "pane_id": "mxdb:w1:p1",
                 "name": "a.txt", "mime": "text/plain", "size": 6},
                {"type": "attachment_chunk", "index": 0, "data": _b64("abc")},
            ])
            # _dispatch_many exhausts the message list, so the client is gone.
            self.assertEqual(relay.active_uploads, {})
            self.assertEqual(relay.upload_quota.pending_bytes, 0)
            leftovers = list(Path(relay.attachment_store.directory).glob("*.part"))
            self.assertEqual(leftovers, [])

    def test_an_idle_upload_expires_and_frees_its_reservation(self):
        with loaded_relay() as relay:
            relay.UPLOAD_IDLE_TIMEOUT = 0
            self._register_sessions(relay)
            self._dispatch_many(relay, [
                {"type": "attachment_begin", "request_id": "u7", "pane_id": "mxdb:w1:p1",
                 "name": "a.txt", "mime": "text/plain", "size": 6},
            ])
            asyncio.run(relay.expire_stale_uploads())
            self.assertEqual(relay.active_uploads, {})
            self.assertEqual(relay.upload_quota.pending_bytes, 0)

    def test_startup_sweeps_temporaries_left_by_a_crash(self):
        with loaded_relay() as relay:
            directory = Path(relay.attachment_store.directory)
            directory.mkdir(parents=True, exist_ok=True)
            orphan = directory / "crashed.part"
            orphan.write_bytes(b"half a file")
            relay.sweep_partial_uploads()
            self.assertFalse(orphan.exists())
    def test_the_frame_ceiling_only_has_to_fit_one_chunk(self):
        with loaded_relay() as relay:
            self.assertLessEqual(relay.WS_MAX_SIZE, 1024 * 1024)
            self.assertGreater(relay.WS_MAX_SIZE, relay.CHUNK_BYTES * 4 // 3)
```

- [ ] **Step 2: eseguirli e vederli fallire.**
- [ ] **Step 3: implementare.** `WS_MAX_SIZE = 512 * 1024`, `CHUNK_BYTES = 256 * 1024`. Nel `finally` di `handle_client`, abortire ogni upload della connessione. Un `asyncio` task periodico scade gli upload fermi da oltre 30 s o aperti da oltre 5 min. All'avvio, cancellare i temporanei rimasti nella directory allegati.
- [ ] **Step 4: test verdi.**
- [ ] **Step 5: commit**

```bash
git add relay/herdr_relay.py tests/test_herdr_relay.py
git commit -m "feat(relay): bound attachment uploads in time and clean up after them"
```

---

### Task 6: Il pulsante `+` e la macchina a stati del client

**Files:**
- Modify: `web/index.html`
- Modify: `tests/web_controls_harness.js` (se serve un nuovo id di blocco)
- Create: `tests/test_web_attachments.js`

- [ ] **Step 1: test nell'harness `vm`**

```js
// la guardia che mancava e che uccise la prima versione
assert.equal(sentMessages.length, 0, 'an oversized file must never reach the socket');
assert.match(status.textContent, /limited to 10 MiB/);
```

Più: la sequenza begin→chunk→commit; un solo blocco in volo per volta; `attachment_abort` alla rimozione del file durante l'upload; l'avanzamento che cresce con gli ack.

- [ ] **Step 2: eseguirli e vederli fallire.**

- [ ] **Step 3: implementare.** Un blocco `<script id="terminalUpload">` accanto agli altri già presenti, così l'harness lo estrae per id. Il pulsante `+` con `accept` allineato alla whitelist. **Controllo di `file.size` prima di qualunque lettura**, con il limite del tipo scelto nel messaggio. Anteprima: miniatura per le immagini, icona più nome e dimensione per PDF e testo. Un blocco in volo alla volta, spedito solo dopo l'ack precedente e con `socket.bufferedAmount` sotto soglia. Il testo viaggia nel `commit`, così resta modificabile durante l'upload.

- [ ] **Step 4: test verdi.**
- [ ] **Step 5: commit**

```bash
git add web/index.html tests/test_web_attachments.js tests/web_controls_harness.js
git commit -m "feat(web): attach images, PDFs and text from the composer"
```

---

### Task 7: La composer non deve sfondare il viewport

Reintrodurre l'anteprima e aggiungere una barra di avanzamento fa ricrescere la colonna. In una misura precedente, a 852×190 con tastiera aperta la composer chiedeva 251.5 px contro 190 disponibili.

**Files:**
- Modify: `web/index.html` (CSS)
- Modify: `tests/check_terminal_layout.py`

- [ ] **Step 1: aggiungere i casi di layout**

In `check_terminal_layout.py`, casi a 852×190 con: anteprima immagine, anteprima PDF, upload in corso con barra di avanzamento, e stato di errore.

- [ ] **Step 2: eseguire e misurare**

Run: `HERDR_BROWSER_TESTS=1 tests/run.sh`
Se l'input finisce sotto il viewport, l'output mostra le metriche: annotare il deficit.

- [ ] **Step 3: applicare la cascata di degrado in `.compact`**, nell'ordine deciso in precedenza — lo stato diventa un overlay sopra l'output invece di una riga di flusso; l'anteprima perde la miniatura e resta nome più pulsante; il pavimento di `.term-content` scende a 26 px. Non nascondere `input-modes`: è funzionale proprio dove serve.

- [ ] **Step 4: il layout check passa.**
- [ ] **Step 5: commit**

```bash
git add web/index.html tests/check_terminal_layout.py
git commit -m "fix(web): keep the composer inside short landscape viewports"
```

---

### Task 8: Documentare il protocollo

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: aggiornare la sezione WebSocket Protocol** aggiungendo ai messaggi client→server `attachment_begin`, `attachment_chunk`, `attachment_commit`, `attachment_abort`, con una riga che dica che il file sale a blocchi e che il relay passa all'agente un percorso, non i byte.
- [ ] **Step 2: `tests/run.sh` verde.**
- [ ] **Step 3: commit**

```bash
git add CLAUDE.md
git commit -m "docs: describe the chunked attachment protocol"
```
