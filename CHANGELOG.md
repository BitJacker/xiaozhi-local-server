# Novità delle versioni / Changelog

Ogni versione viene pubblicata come *release* su GitHub: la pagina **Admin** del server mostra queste note
prima di aggiornare. / Each version is published as a GitHub release: the server's **Admin** page shows these notes
before updating.

## 1.2.1

Correzioni emerse da una seconda revisione esterna del codice.

**Sicurezza**
- Eliminare un utente, cambiargli password o ruolo, uscire o revocare un dispositivo ora **chiude subito** le
  connessioni già aperte; prima restavano attive fino alla disconnessione.
- Anche nella modalità "tieni premuto" l'audio di una frase ha un limite (`vad.max_utterance_s`): un client non può più
  riempire la memoria del server.
- Le richieste di attivazione dei dispositivi scadono dopo 10 minuti e un solo indirizzo può averne al massimo 3 in
  attesa: non si può più bloccare l'attivazione dei dispositivi veri.
- Quando HTTPS è attivo, login e altre API dal browser non sono più accettati in HTTP da altri computer (password mai in
  chiaro sulla rete). I dispositivi ESP32 restano in HTTP.
- Chiave privata HTTPS e chiave delle sessioni leggibili solo dall'utente del server (Linux).

**Aggiornamenti**
- I file tolti da una nuova versione vengono tolti anche dal server; se l'aggiornamento non riesce, i file aggiunti
  dalla versione difettosa vengono rimossi. I file creati da te non vengono mai toccati.

**Altro**
- Repository riordinata: documentazione in `docs/`, script di controllo in `scripts/`.
- Contatto per idee, richieste e segnalazioni di sicurezza: giacomo.dev@icloud.com.
- `tests/run_all.py --mock`: i test di sicurezza e aggiornamento girano anche senza Ollama.
- Test: componenti 21, sicurezza 55, aggiornamento 23.

## 1.2.0

Correzioni di sicurezza e affidabilità emerse da una revisione esterna del codice, più nuove protezioni.

**Correzioni**
- **Sicurezza (alta)**: un dispositivo approvato non si può più impersonare conoscendone solo il MAC. L'autorizzazione è
  legata a MAC + Client-Id (codice casuale del dispositivo); con un Client-Id diverso serve una nuova approvazione.
  I dispositivi approvati con la 1.1.0 vengono legati al loro Client-Id al primo collegamento.
- **Visione**: le foto inviate dai dispositivi approvati per l'analisi venivano rifiutate (401); ora usano il token
  del dispositivo.
- **Cronologia**: le conversazioni scadute vengono cancellate ogni ora, anche se il server non si riavvia mai.
- **Aggiornamenti (alta)**: la nuova versione viene provata prima del riavvio e deve confermarsi dopo; se non parte,
  il server torna da solo alla versione precedente e la pagina Admin lo segnala.

**Novità**
- **HTTPS** su `https://IP:8443` con certificato creato in automatico: password, cookie e voce cifrati, microfono
  utilizzabile da altri PC e dai telefoni. I dispositivi ESP32 restano in HTTP sulla porta 8000.
- **Cifratura** di cronologia e segreti 2FA sul disco (AES-256-GCM).
- **Cronologia conservata 30 giorni** per impostazione predefinita.
- **Controllo degli indirizzi**: se modello linguistico o server MCP puntano a Internet, il server non parte
  (salvo `privacy.allow_external: true`).
- **Permessi sugli strumenti MCP**: gli utenti leggono (risultati, foto), solo admin e dispositivi possono far imparare,
  dimenticare o cambiare modalità e parametri. Tabella nella pagina Admin.
- **Immagini degli strumenti MCP** descritte dal modello visivo locale, se configurato.
- **Profilo leggero per Raspberry Pi** e computer senza GPU, scelto da `install.sh` (`--light` / `--full`).
- Gli argomenti inventati dal modello vengono scartati prima di chiamare uno strumento.
- Pagina Admin: controllo privacy, permessi, HTTPS, cifratura, avviso di aggiornamento annullato.
- Nuovi test: componenti (16), sicurezza (52), aggiornamento (20), test di affidabilità prolungato.

**Aggiornare dalla 1.1.0**: dalla pagina Admin, pulsante "Aggiorna". Al primo avvio viene creata la chiave di cifratura
e il certificato HTTPS; la cronologia già salvata resta leggibile.

## 1.1.0

**Account e sicurezza**
- Accesso con username e password a client web, cronologia e amministrazione; il primo admin si crea con un codice
  mostrato solo nel terminale del server.
- Verifica in due passaggi (2FA) con app di autenticazione (QR code), anche obbligatoria per tutti (`auth.require_2fa`).
- Protezione anti-bot e anti-spam: blocco dopo 5 password sbagliate, limiti di richieste, domande e connessioni.
- Dispositivi xiaozhi (ESP32) autorizzati dall'admin con un codice a 6 cifre; revoca dalla pagina Admin.
- Cronologia separata per utente (l'admin vede tutto).
- Pagine **Account** (password, 2FA) e **Admin** (utenti, dispositivi, aggiornamenti, stato).

**Aggiornamenti che conservano i dati**
- Pulsante **"Aggiorna"** nella pagina Admin: mostra le novità e cosa farà, installa solo se premi. Backup automatico e
  ritorno alla versione precedente in caso di errore. Anche da terminale: `update.sh` / `update.ps1`.
- Tutti i tuoi dati in `data/` (impostazioni personali in `data/config.yaml`, utenti, dispositivi, cronologia):
  gli aggiornamenti non li toccano mai.

**Aggiornare dalla 1.0.0**: la 1.0.0 non ha ancora il pulsante. Esegui una volta `git pull` (o scarica di nuovo il
progetto) e sposta le tue modifiche di `config.yaml` in `data/config.yaml`; dalle versioni successive basta il pulsante.

## 1.0.0

- Server locale compatibile con xiaozhi (OTA, WebSocket v1/v2/v3, Opus, MCP), ponte MCP verso HuskyLens 2.
- Riconoscimento vocale (Whisper), modello linguistico (Ollama) e sintesi vocale (Piper) in locale.
- Client web con microfono, cronologia sul disco del server.
- Installazione automatica per Linux e Windows.
