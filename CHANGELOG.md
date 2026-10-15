# Novità delle versioni / Changelog

Ogni versione viene pubblicata come *release* su GitHub: la pagina **Admin** del server mostra queste note
prima di aggiornare. / Each version is published as a GitHub release: the server's **Admin** page shows these notes
before updating.

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
