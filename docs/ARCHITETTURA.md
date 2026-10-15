# Come è fatta l'app

Questa pagina spiega com'è costruito Husky locale: i componenti, cosa succede quando parli,
come è organizzato il codice e come è stato verificato.

## 1. Componenti

```mermaid
flowchart TB
    subgraph client["Chi parla con il server"]
        WEB["Browser<br/>web/index.html<br/>(microfono PCM 16 kHz)"]
        DEV["Dispositivo xiaozhi<br/>ESP32 / HuskyLens<br/>(audio Opus)"]
    end

    subgraph server["server.py: un solo processo Python"]
        HTTP["app.py<br/>HTTP + WebSocket (aiohttp)<br/>/ · /xiaozhi/ota/ · /xiaozhi/v1/"]
        SES["session.py<br/>una sessione per connessione:<br/>ascolto, turni, interruzioni"]
        AUD["audio.py<br/>Opus ⇄ PCM (PyAV)<br/>rilevamento fine frase"]
        ASR["engines.py · ASR<br/>faster-whisper"]
        TTS["engines.py · TTS<br/>Piper (voce italiana)"]
        LLM["llm.py<br/>ciclo domanda → strumenti → risposta"]
        TOOLS["tools.py<br/>strumenti MCP"]
    end

    OLL["Ollama<br/>Qwen3 4B<br/>(solo 127.0.0.1)"]
    HUSKY["HuskyLens 2<br/>server MCP (SSE)<br/>http://IP:3000/sse"]

    WEB <-->|WebSocket| HTTP
    DEV <-->|OTA + WebSocket| HTTP
    HTTP --> SES
    SES <--> AUD
    SES --> ASR
    SES --> LLM
    SES --> TTS
    LLM <-->|API OpenAI| OLL
    LLM --> TOOLS
    TOOLS <-->|MCP via WebSocket| DEV
    TOOLS <-->|MCP via SSE| HUSKY
```

| Modulo | Cosa fa |
|---|---|
| `app.py` | Avvia il server web: pagina del client, OTA per i dispositivi, WebSocket, endpoint della visione, `/health`. Controlla il token. |
| `session.py` | Implementa il protocollo xiaozhi per una connessione: `hello`, `listen`, `abort`, `mcp`. Raccoglie l'audio, decide quando la frase è finita, esegue il turno e invia la voce. |
| `audio.py` | Decodifica e codifica Opus (frame da 60 ms, 16 kHz in ingresso e 24 kHz in uscita), ricampionamento e rilevamento della fine della frase. |
| `engines.py` | Whisper (sulla GPU se c'è, altrimenti sulla CPU) e Piper. Pulisce il testo da emoji e markdown prima di leggerlo. |
| `llm.py` | Parla con il modello linguistico, esegue gli strumenti che il modello chiede e ripete finché c'è una risposta. Con Ollama scarica e precarica il modello. |
| `tools.py` | Raccoglie gli strumenti MCP: quelli del dispositivo (via WebSocket) e quelli di HuskyLens 2 (via SSE, con riconnessione automatica). |

## 2. Cosa succede quando parli

```mermaid
sequenceDiagram
    autonumber
    actor U as Utente
    participant C as Browser / dispositivo
    participant S as Server (session.py)
    participant W as Whisper
    participant O as Ollama (Qwen3)
    participant H as HuskyLens (MCP)
    participant P as Piper

    C->>S: hello (formato audio)
    S-->>C: hello (session_id)
    U->>C: tiene premuto e parla
    C->>S: listen start + audio a pacchetti da 60 ms
    C->>S: listen stop (o fine frase rilevata dal server)
    S->>W: audio della frase
    W-->>S: "Cosa vedi?"
    S-->>C: stt "Cosa vedi?"
    S->>O: domanda + elenco strumenti
    O-->>S: chiama lo strumento get_recognition_result
    S->>H: tools/call
    H-->>S: {"faces":[{"name":"Alex"}]}
    S->>O: risultato dello strumento
    O-->>S: "Vedo Alex davanti a me."
    loop per ogni frase della risposta
        S->>P: frase
        P-->>S: audio
        S-->>C: tts sentence_start + audio a ritmo reale
    end
    S-->>C: tts stop
    Note over U,C: Se l'utente preme di nuovo mentre l'assistente parla:<br/>abort → la voce si ferma subito e ascolta la nuova domanda
```

## 3. Stati di una sessione

```mermaid
stateDiagram-v2
    [*] --> Connesso: hello
    Connesso --> Ascolto: listen start
    Ascolto --> Trascrizione: listen stop / fine frase
    Trascrizione --> Ascolto: niente di comprensibile
    Trascrizione --> Ragionamento: testo
    Ragionamento --> Strumenti: il modello chiede uno strumento
    Strumenti --> Ragionamento: risultato
    Ragionamento --> Parla: risposta
    Parla --> Ascolto: fine risposta (modalità continua)
    Parla --> Connesso: fine risposta (modalità tieni premuto)
    Parla --> Ascolto: abort / nuova pressione
    Connesso --> [*]: disconnessione (memoria cancellata)
```

## 4. Installazione e avvio

```mermaid
flowchart TB
    A["install.sh (Linux) / install.ps1 (Windows)"] --> B["Pacchetti di sistema mancanti<br/>(solo Linux, con apt)"]
    B --> C["Ambiente Python .venv<br/>+ requirements.txt"]
    C --> D{"GPU NVIDIA?"}
    D -- sì --> E["+ librerie CUDA<br/>Whisper large-v3-turbo"]
    D -- no --> F["Whisper small su CPU"]
    E --> G["Ollama già installato?"]
    F --> G
    G -- no --> H["Ollama portatile<br/>in runtime/"]
    G -- sì --> I["usa quello del sistema"]
    H --> J["Scarica modelli: voce, Whisper, Qwen3"]
    I --> J
    J --> K{"--service / -Service?"}
    K -- sì --> L["systemd / attività all'accesso"]
    K -- no --> M["avvio manuale:<br/>start.sh / avvia.bat"]
```

All'avvio (`start.sh` / `start.ps1`) viene acceso Ollama, se non è già attivo, solo su `127.0.0.1`, con il
contesto da 8192 token. Poi parte `server.py`, che lavora offline perché i modelli sono già sul disco.

## 5. Come è stato verificato

```mermaid
flowchart LR
    subgraph unit["Componenti"]
        T1["Opus: codifica / decodifica"]
        T2["Whisper GPU vs CPU"]
        T3["MCP: SDK ufficiale +<br/>server scritto a mano (2024-11-05)"]
    end
    subgraph e2e["tests/test_suite.py: 24 controlli"]
        E1["OTA, token, hello"]
        E2["voce → strumento<br/>protocollo v1 / v2 / v3"]
        E3["tieni premuto, continua,<br/>interruzione, memoria"]
        E4["HuskyLens, visione,<br/>latenza, 3 dispositivi"]
    end
    subgraph browser["tests/browser_mic_test.py"]
        B1["Chrome vero con<br/>microfono simulato"]
    end
    subgraph install["Installazione da zero"]
        I1["Windows 11 + GPU"]
        I2["Ubuntu 24.04 senza GPU"]
    end
    unit --> e2e --> browser --> install
```

Tutti i test si lanciano con un comando: `python tests/run_all.py` (serve Ollama acceso).

## 6. Struttura dei file

```
xiaozhi-local-server/
├── server.py                 avvio (attiva la modalità offline se i modelli ci sono)
├── config.yaml               configurazione
├── install.sh / install.ps1  installazione automatica Linux / Windows
├── start.sh / start.ps1      avvio (Ollama + server); avvia.bat = doppio clic su Windows
├── verifica_privacy.ps1      controllo delle connessioni di rete
├── xiaozhi_locale/           codice del server
├── web/index.html            client web
├── scripts/download_models.py
├── tests/                    test automatici e simulatori (HuskyLens, dispositivo, LLM)
└── docs/ARCHITETTURA.md      questa pagina
```
