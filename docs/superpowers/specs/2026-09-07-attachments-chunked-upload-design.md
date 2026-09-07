# Allegati: upload a blocchi su WebSocket

Data: 2026-09-07
Stato: approvato, pronto per il piano di implementazione
Revisione di progetto: Codex (gpt-5.6-sol)

## Obiettivo

Reintrodurre il pulsante `+` della composer, esteso da sole immagini a **testo,
immagini e PDF**, sostituendo il trasporto a messaggio singolo con un upload a
blocchi che elimina il limite di dimensione del frame.

## Perché la prima versione è morta

La feature fu rimossa per decisione di prodotto, ma dopo un bug mai localizzato:
l'immagine restava in `pending` e non arrivava mai al relay — nessuna voce
nell'audit, nessuna directory creata.

La causa è stata individuata e verificata sul codice di `HEAD`: il client valida
il testo e lo stato del socket, ma **non controlla mai `draft.file.size`**.

```js
if (draft.text.length > 1000) { ... }   // il testo si controlla
// draft.file.size non si controlla mai
```

Il limite di 5 MiB vive in `attachments.py`, cioè **dopo** l'arrivo del
messaggio. Con `WS_MAX_SIZE = 8 MiB`, una foto da telefono di 6+ MiB diventa
~8 MiB in base64, supera il limite del frame, e `websockets` **chiude la
connessione** invece di consegnare. Le foto dei telefoni stanno quasi sempre
sopra quella soglia, quindi il difetto era sistematico, non occasionale.

Il bug di layout a 852×190 **non** c'entrava: quel caso passava prima di
`b13100c`.

## Vincoli verificati

1. **`process_request` non può leggere un body HTTP.** In `websockets` 17.1
   `Request` espone `path`, `headers`, `method`, `protocol` — nessun body: la
   libreria legge solo riga di richiesta e header per l'handshake. Un upload via
   HTTP POST richiederebbe un secondo server su un'altra porta, da esporre anche
   nel tunnel Tailscale/Cloudflare: complicazione di deployment sproporzionata.
2. **Il WebSocket garantisce l'ordine dei messaggi**, quindi il riassemblaggio
   fuori ordine non serve e va anzi rifiutato.
3. **`ws.send()` nel browser bufferizza e non blocca.** Spedire i blocchi in un
   ciclo stretto accoda l'intero file in memoria, annullando il chunking: serve
   controllo di flusso esplicito.
4. **La memoria dei buffer di `websockets` è legata a `max_size` e `max_queue`.**
   Blocchi piccoli permettono un `max_size` piccolo, che riduce anche la
   superficie DoS rispetto a oggi.
5. **Il relay non spedisce il file all'agente**: lo salva su disco e gli passa il
   percorso assoluto. Estendere i tipi è quindi una questione di whitelist e di
   copy, non di architettura.

## Protocollo

Quattro messaggi client → server, ciascuno correlato da `request_id`.

| Messaggio | Campi | Risposta |
|---|---|---|
| `attachment_begin` | `request_id`, `name`, `mime`, `size` (byte grezzi), `pane_id` | `command_result` con `upload_id`, oppure `error` |
| `attachment_chunk` | `upload_id`, `index`, `data` (base64) | `command_result` con `index` accettato (è l'ack del controllo di flusso) |
| `attachment_commit` | `upload_id`, `text` (prompt facoltativo) | `command_result` `ok`, oppure `error` |
| `attachment_abort` | `upload_id` | `command_result` `ok` |

**La validazione avviene al `begin`, prima di ricevere un solo byte**: tipo,
dimensione dichiarata, quota disponibile, esistenza del pane. Un PDF da 40 MiB
viene rifiutato con un messaggio chiaro invece di uccidere la connessione dopo
averlo caricato tutto.

Ogni ack di blocco autorizza il client a spedire il successivo. Il client tiene
al massimo un blocco in volo e controlla comunque `bufferedAmount` prima di
spedire.

## Limiti

| Voce | Valore |
|---|---|
| Testo (`txt` `md` `csv` `json`) | 1 MiB |
| Immagini (`png` `jpg` `webp`) | 10 MiB |
| PDF | 25 MiB |
| Blocco | 256 KiB di base64 (~192 KiB grezzi) |
| `WS_MAX_SIZE` | **512 KiB** (era 8 MiB) |
| Upload attivi | 1 per connessione |
| Byte pendenti globali | 64 MiB |
| Archivio permanente | **200 MiB** / 100 file (era 50 MiB) |
| Timeout fra blocchi | 30 s |
| Durata massima di un upload | 5 min |

L'archivio permanente sale da 50 a 200 MiB: con PDF da 25 MiB il vecchio
limite ne conteneva due, ereditato da quando passavano solo immagini.

La quota è **prenotata atomicamente al `begin`** e rilasciata al commit, all'abort,
al timeout o alla disconnessione. Senza prenotazione, upload concorrenti
passerebbero tutti i controlli nello stesso istante.

## Validazione dei tipi

**Il MIME dichiarato dal browser non è affidabile** — è spesso vuoto o
incoerente. Il relay valida estensione **e contenuto**:

| Tipo | Verifica sul contenuto |
|---|---|
| `png` `jpg` `webp` | firme già implementate in `attachments.py` |
| `pdf` | header `%PDF-` e `%%EOF` negli ultimi 1024 byte |
| `txt` `md` `csv` | UTF-8 valido, nessun byte nullo |
| `json` | come sopra, e deve decodificare senza errori |

Nessun allegato viene mai servito via HTTP né scritto nei log. L'audit registra
solo metadati: nome, tipo, dimensione.

## Ciclo di vita e pulizia

- I blocchi si scrivono **in streaming** su un file temporaneo, mai accumulati in
  memoria.
- Indici **rigorosamente sequenziali**. Duplicato, salto, base64 non valido,
  blocco sovradimensionato o totale oltre il dichiarato → l'upload aborta e il
  temporaneo viene cancellato.
- Ogni upload è legato alla sua connessione: **alla disconnessione i temporanei
  di quella connessione si cancellano**.
- **Nessun resume** dopo una riconnessione in questa versione: richiederebbe
  identità persistente, offset, hash e protezione contro il riuso dell'`upload_id`,
  per un beneficio scarso. Si riparte da zero.
- All'avvio il relay **ripulisce i temporanei** sopravvissuti a un crash.

Al `commit` il relay verifica che il totale ricevuto **coincida esattamente** con
quello dichiarato e che il pane esista ancora. Se il prompt all'agente fallisce
**prima** dell'accettazione il file finale viene cancellato; **dopo**
l'accettazione si conserva, perché l'agente potrebbe leggerlo in asincrono.

## Client

Il pulsante `+` apre un selettore con `accept` allineato alla whitelist. Dopo la
scelta:

- **Controllo di dimensione immediato**, prima di qualunque lettura — è la
  guardia che mancava e che ha ucciso la prima versione. Messaggio esplicito con
  il limite del tipo scelto.
- Anteprima: miniatura per le immagini, icona più nome e dimensione per PDF e
  testo.
- Barra di avanzamento alimentata dagli ack, utile su un PDF da 25 MiB.
- Annullamento durante l'upload: manda `attachment_abort` e libera la UI.

Il testo resta facoltativo e viaggia nel `commit`, non nel `begin`, così può
essere modificato mentre il file sale.

## Cosa cambia, file per file

| File | Cosa |
|---|---|
| `relay/attachments.py` | Ripristinare da `HEAD` (quote, atomicità, rifiuto symlink, cleanup su scrittura parziale sono già scritti e testati). Estendere `EXTENSIONS` a PDF e testo, aggiungere le verifiche di contenuto, i limiti per tipo, e alzare `MAX_STORAGE_BYTES` a 200 MiB. |
| `relay/herdr_relay.py` | I quattro handler, il registro degli upload attivi con prenotazione della quota, i timeout, la pulizia alla disconnessione e all'avvio, `WS_MAX_SIZE` a 512 KiB. |
| `web/index.html` | Pulsante `+`, guardia di dimensione, anteprima per tipo, avanzamento, annullamento, la macchina a stati dell'upload. |
| `tests/test_attachments.py` | Ripristinare da `HEAD` ed estendere ai nuovi tipi e alle verifiche di contenuto. |
| `tests/test_herdr_relay.py` | Protocollo: sequenza corretta, indici fuori ordine, sforamento del totale, abort, timeout, disconnessione a metà, quota esaurita, upload concorrenti. |
| `CLAUDE.md` | Documentare i quattro messaggi nella sezione protocollo. |

## Fuori scope

- File Office (`docx` `xlsx` `pptx`), archivi ed eseguibili.
- Resume di un upload interrotto.
- Allegati multipli in un solo invio.
- Anteprima del contenuto dei PDF nella dashboard.
