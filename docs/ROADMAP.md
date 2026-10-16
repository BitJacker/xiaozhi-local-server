# Roadmap

Funzioni scelte per le prossime versioni, in ordine di realizzazione. Ogni fase esce come una release che si
installa dal pulsante **"Aggiorna"** della pagina Admin. Idee e richieste: [Issues](https://github.com/BitJacker/xiaozhi-local-server/issues)
o giacomo.dev@icloud.com.

## Fase A: strumenti (fatta: versione 1.4.0)
- **Strumenti personalizzati dalla pagina Admin**: comandi sul server, chiamate a siti web, sensori MQTT. Tutti
  spenti finché l'admin non li accende, con ruoli, limiti e registro.
- **Il modello sceglie quali gruppi di strumenti aprire**: quando gli strumenti sono tanti, il modello vede l'elenco
  dei gruppi (telecamera, dispositivo, sensori, web…) e apre solo quelli che gli servono.
- **Strumenti italiani pronti**: meteo, allerte meteo della regione (MeteoAlarm, avvisi ufficiali), qualità
  dell'aria, titoli delle notizie, promemoria e note salvati sul server.
- **Risposte immediate per domande frequenti**: domande e risposte fisse scritte dal prof (orari, regole, date).

## Fase B: MQTT e dispositivi (fatta: versione 1.5.0)
- **Broker MQTT integrato** e **ponte per sensori e attuatori**: ESP8266, ESP32, gateway LoRaWAN (The Things
  Network, ChirpStack).
- **ESPHome via MQTT** con riconoscimento automatico dei dispositivi.
- **Protocollo xiaozhi MQTT+UDP** per gli ESP32 che lo usano (audio UDP cifrato AES).
- **Avvisi dal server ai dispositivi**: messaggi letti o mostrati su tutti i dispositivi o su uno solo, anche a orario.

## Fase C: ESPHome ed ecosistema
- **Dispositivi ESPHome** con il collegamento ufficiale (cifrato): sensori e comandi diventano strumenti da soli.
- **ESPHome come microfono e altoparlante** di Husky (schede vocali ESPHome).
- **Qualsiasi server MCP**: oltre a SSE anche stdio e Streamable HTTP; programmi esterni possono registrare strumenti.
- **Installare il firmware sugli ESP32 dal browser** (Chrome/Edge, Web Serial).

## Fase D: scuola
- **Base di conoscenza (RAG)**: documenti della classe (PDF, appunti) caricati dalla pagina Admin, ricerca in locale.
- **Modalità lezione**: trascrizione della lezione senza risposte e riassunto automatico (argomento, punti chiave, compiti).
- **Memoria a lungo termine per utente**, visibile e cancellabile dall'utente.
- **Più assistenti con personalità diverse** (istruzioni, voce e strumenti propri), anche scelti a voce.

## Fase E: voce e modelli
- **Gestione dei modelli dalla pagina Admin**: scaricare e cambiare modello senza riavviare.
- **Rilevamento migliore della fine del parlato** (modello VAD locale, meno errori con il rumore della classe).
- **Riconoscimento di chi parla** (impronta vocale), con consenso.
- **Più dispositivi nella stessa aula: risponde uno solo.**
- **Musica e audio** dalla cartella del server.

## Fase F: gestione
- **Metriche per Prometheus / Grafana.**
- **Test di carico**: quanti dispositivi insieme regge il server.
- **Backup automatico** e cifrato dei dati.
- **Docker, docker compose e Kubernetes**, verificati automaticamente su GitHub.

## Non previste per ora
- Quiz e schede di vocaboli, regole fisse per i minori nel prompt, app per telefono, voce clonata.
- Scelta degli strumenti fatta dal server (al suo posto: decide il modello, vedi Fase A).
