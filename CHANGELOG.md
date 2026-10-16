# Novità delle versioni / Changelog

Ogni versione viene pubblicata come *release* su GitHub: la pagina **Admin** del server mostra queste note
prima di aggiornare. / Each version is published as a GitHub release: the server's **Admin** page shows these notes
before updating.

## 1.6.0

Terza fase della [roadmap](docs/ROADMAP.md): ESPHome ed ecosistema.

**Novità**
- **ESPHome con l'API nativa** (la stessa di Home Assistant, cifrata), senza MQTT: ricerca dei dispositivi in rete
  (mDNS), approvazione dalla pagina Admin, ricollegamento automatico. Sensori, interruttori, luci, pulsanti, numeri e
  selettori diventano strumenti di Husky.
- **Schede vocali ESPHome** (ESP32-S3-BOX, Atom Echo, Voice PE...) come microfono e altoparlante di Husky: parola di
  attivazione sulla scheda, risposta con la voce in streaming o come file WAV, chiusura se nessuno parla. Stessa
  conversazione, strumenti e permessi dei dispositivi xiaozhi. Gli **Avvisi** arrivano anche a queste schede.
- **Qualsiasi server MCP** dalla pagina Admin: in rete locale (SSE o Streamable HTTP) o come programma su questo
  computer (stdio, da permettere con un interruttore). Ognuno ha i suoi ruoli, parte spento, e gli indirizzi su
  Internet richiedono una conferma e compaiono nella tabella Privacy.
- **Punti di accesso MCP** compatibili con xiaozhi.me: un programma su un altro computer (es. `mcp_pipe.py`) si
  collega a Husky con un indirizzo e un token e porta i suoi strumenti.
- **Installazione del firmware dal browser** (Chrome/Edge, cavo USB): l'admin carica il file .bin completo e lo
  installa sugli ESP32 ed ESP8266. ESP Web Tools è incluso nel server, nessun file da Internet.
- Pagina Admin: sezioni **Server MCP**, **ESPHome** e **Installa firmware** (italiano e inglese).

**Test**: nuovi `tests/test_mcp.py` (19) e `tests/test_esphome.py` (25, con schede ESPHome finte che parlano il
protocollo vero, anche la voce); componenti 59.


## 1.5.0

Seconda fase della [roadmap](docs/ROADMAP.md): MQTT, sensori e dispositivi.

**Novità**
- **Broker MQTT integrato** (porta 1883), acceso dalla pagina Admin: account per sensori e dispositivi con argomenti
  in cui possono scrivere e leggere, limiti di connessioni e messaggi, scollegamento immediato quando si elimina un
  account. Provato con paho-mqtt, il client MQTT più diffuso.
- **Sensori e attuatori** (ESP8266, ESP32): gli strumenti "leggi un sensore" e "comanda un dispositivo" funzionano
  con il broker integrato e con quelli esterni; dai messaggi JSON si può leggere un solo campo.
- **ESPHome via MQTT**: i dispositivi si annunciano da soli; sensori, interruttori, luci (anche luminosità),
  pulsanti, numeri e selettori diventano strumenti di Husky. Elenco nella pagina Admin.
- **Broker esterni** per i sensori **LoRaWAN** (The Things Network, ChirpStack, Mosquitto), con TLS; password cifrate.
- **Protocollo xiaozhi MQTT+UDP**: i dispositivi approvati possono collegarsi in MQTT, con la voce cifrata AES-128
  via UDP come nel firmware ufficiale. Stessa conversazione, stessi permessi e revoca immediata.
- **Avvisi ai dispositivi** dalla pagina Admin, subito o a orario: notifica vocale con sottotitolo per i dispositivi
  MQTT (anche a riposo), messaggio e voce per le conversazioni aperte e la pagina dell'assistente.
- Pagina Admin: sezioni **MQTT e sensori** e **Avvisi** (italiano e inglese).

**Correzioni**
- `requirements/dev.txt` puntava al file sbagliato dopo il riordino della 1.4.0.
- La pagina Admin si apriva scorrendo verso il basso quando l'indirizzo conteneva una sezione (#…).

**Test**: nuovi `tests/test_mqtt.py` (26) e `tests/test_xiaozhi_mqtt.py` (17, con un dispositivo simulato che fa
esattamente quello che fa il firmware).

## 1.4.0

Prima fase della [roadmap](docs/ROADMAP.md): strumenti.

**Novità**
- **Strumenti personalizzati dalla pagina Admin**: comandi sul server, chiamate a siti web e sensori MQTT. Tutti
  spenti finché l'admin non li accende (per tipo e per strumento), con ruoli, prova con un clic e registro.
  Sicurezza: i comandi non usano mai una shell e il programma è fisso; i siti hanno l'indirizzo fisso; i valori
  scelti dal modello devono rispettare tipo, valori ammessi e limiti; le chiavi API sono cifrate.
- **Strumenti già pronti**: meteo, qualità dell'aria, allerte meteo della regione, titoli delle notizie, Wikipedia,
  spazio su disco, temperatura del processore, ping di un dispositivo, sensore e comando MQTT (MQTT arriva nella 1.5).
- **Promemoria e note** per utente, cifrati sul server.
- **Risposte immediate** a domande frequenti scritte dal prof, senza passare dal modello.
- **Gruppi di strumenti scelti dal modello** quando gli strumenti sono tanti (provato col modello vero: apre il
  gruppo giusto e usa lo strumento giusto 24 volte su 24).
- Feed RSS e Atom (notizie, allerte) leggibili dagli strumenti web.
- **Sito rifatto da capo**: pagine più chiare, amministrazione divisa in sezioni (panoramica, utenti, dispositivi,
  risposte immediate, strumenti, privacy, registro, aggiornamenti), **italiano e inglese**, tema **chiaro, scuro o
  automatico**. Stile "quaderno a quadretti" (chiaro) e "lavagna" (scuro), carattere ad alta leggibilità incluso nel
  server (nessun font scaricato da Internet).
- **Primo accesso più semplice**: utente `admin`, password `Passw0rd!`, con **cambio obbligatorio** al primo accesso
  (prima non si può usare nient'altro). Il vecchio metodo col codice del terminale resta con `auth.default_admin: false`.
- **File riordinati**: `requirements/` (GPU e test), README inglese in `docs/`, fine riga uniformi (`.gitattributes`).

## 1.3.0

**Più veloce**
- **Risposte in streaming**: la voce parte appena il modello ha scritto la prima frase, mentre continua con le altre
  (prima si aspettava l'intera risposta). Si può disattivare con `llm.stream: false`.
- L'inizio della conversazione resta identico da un turno all'altro (nel prompt c'è solo la data; l'ora la dà lo
  strumento interno `ora_attuale`), così Ollama riusa i calcoli già fatti e risponde prima nelle conversazioni lunghe.
- Se il dispositivo sta ancora inviando l'elenco dei suoi strumenti, la prima domanda aspetta un attimo invece di
  rispondere senza poterli usare.
- Una risposta interrotta viene salvata in cronologia con quello che il modello aveva scritto, segnata "(interrotta)".

**Più sicuro**
- **Registro di sicurezza** nella pagina Admin: accessi, tentativi falliti, account bloccati, modifiche a utenti e
  dispositivi, possibili impersonazioni, aggiornamenti e strumenti che cambiano qualcosa. Mai il contenuto delle
  conversazioni; cifrato sul disco.
- **Argomenti degli strumenti controllati** con lo schema prima della chiamata (obbligatori, tipi, valori ammessi,
  limiti): un valore sbagliato non arriva al dispositivo e il modello può correggersi.
- Servizio Linux (`install.sh --service`) con protezioni di systemd: niente privilegi aggiuntivi, sistema in sola
  lettura, file creati leggibili solo dall'utente del server. Avviso se si installa come root.
- Guida al firewall nel README (Linux e Windows).

**Pagina Admin**
- Sezione **Prestazioni**: connessioni aperte, tempo fino alla prima voce (a voce e scritta), riconoscimento vocale,
  prime parole del modello.

**Test**: componenti 31 (streaming, controllo degli argomenti, registro eventi, statistiche), funzionamento 28,
sicurezza 55, aggiornamento 23, aggiornamento reale dalla 1.1.0, 1.2.0 e 1.2.1.

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
