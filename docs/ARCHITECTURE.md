# How the app is built

🇮🇹 [Italiano](ARCHITETTURA.md) · 🇬🇧 English

This page explains how Husky locale is built: its components, what happens when you speak, how the code is
organised and how it was verified.

## 1. Components

```mermaid
flowchart TB
    subgraph client["Who talks to the server"]
        WEB["Browser<br/>web/index.html<br/>(microphone, PCM 16 kHz)"]
        DEV["xiaozhi device<br/>ESP32 / HuskyLens<br/>(Opus audio)"]
    end

    subgraph server["server.py: a single Python process"]
        HTTP["app.py<br/>HTTP + WebSocket (aiohttp)<br/>/ · /xiaozhi/ota/ · /xiaozhi/v1/"]
        SES["session.py<br/>one session per connection:<br/>listening, turns, interruptions"]
        AUD["audio.py<br/>Opus ⇄ PCM (PyAV)<br/>end-of-sentence detection"]
        ASR["engines.py · ASR<br/>faster-whisper"]
        TTS["engines.py · TTS<br/>Piper"]
        LLM["llm.py<br/>question → tools → answer loop"]
        TOOLS["tools.py<br/>MCP tools"]
        HIST["history.py<br/>history on disk<br/>data/history"]
        AUTH["auth.py<br/>users, 2FA, sessions,<br/>devices, anti-spam"]
        UPD["updater.py<br/>updates"]
    end

    OLL["Ollama<br/>Qwen3 4B<br/>(127.0.0.1 only)"]
    HUSKY["HuskyLens 2<br/>MCP server (SSE)<br/>http://IP:3000/sse"]

    WEB <-->|WebSocket| HTTP
    DEV <-->|OTA + WebSocket| HTTP
    HTTP --> SES
    SES <--> AUD
    SES --> ASR
    SES --> LLM
    SES --> TTS
    LLM <-->|OpenAI API| OLL
    LLM --> TOOLS
    TOOLS <-->|MCP over WebSocket| DEV
    TOOLS <-->|MCP over SSE| HUSKY
    SES --> HIST
    HTTP --> AUTH
    HTTP --> UPD
```

| Module | What it does |
|---|---|
| `app.py` | Runs the web server: client page, OTA for devices, WebSocket, vision endpoint, history API, `/health`. Checks the token. |
| `session.py` | Implements the xiaozhi protocol for one connection: `hello`, `listen`, `abort`, `mcp`. Collects audio, decides when a sentence has ended, runs the turn and streams the voice back. |
| `audio.py` | Opus decoding and encoding (60 ms frames, 16 kHz in and 24 kHz out), resampling and end-of-sentence detection. |
| `engines.py` | Whisper (on the GPU if available, otherwise on the CPU) and Piper. Strips emoji and markdown before reading text aloud. |
| `llm.py` | Talks to the language model, runs the tools the model asks for, and repeats until there is an answer. With Ollama it downloads and preloads the model. |
| `tools.py` | Gathers MCP tools: the device's own (over WebSocket) and the HuskyLens 2 ones (over SSE, with automatic reconnection). |
| `history.py` | Saves questions and answers on the server's disk, one file per day, with the user who asked, and serves them to the `/cronologia` page. |
| `auth.py` | Users and passwords (scrypt), TOTP 2FA, sessions, device approval by code, anti-spam limits. |
| `config.py` | Merges `config.yaml` (defaults) and `data/config.yaml` (your settings); migrates the `data/` format. |
| `updater.py` | Checks GitHub releases and installs the new version keeping `data/`; tests it before restarting, confirms it afterwards and rolls back by itself if it does not start. |
| `privacy.py` | Checks that the language model and MCP servers are on this computer or the local network; otherwise the server does not start. |
| `cryptobox.py` | AES-256-GCM encryption of history and 2FA secrets. |
| `custom_tools.py` | Custom tools (commands, web, MQTT) created from the Admin page, off by default. |
| `notes.py`, `faq.py` | Per-user reminders (encrypted) and instant answers to frequent questions. |
| `mqtt_proto.py`, `mqtt_broker.py`, `mqtt_client.py` | MQTT 3.1.1 protocol, built-in broker (accounts, per-topic permissions, limits), client for external brokers. |
| `mqtt_hub.py` | Switching on from the Admin page, accounts, external brokers (LoRaWAN), last values, ESPHome discovery and auto-created tools. |
| `xiaozhi_mqtt.py` | xiaozhi MQTT+UDP protocol: commands over MQTT, AES-128-CTR encrypted voice over UDP, same Session as WebSocket. |
| `alerts.py` | Alerts to devices (Ogg Opus voice notification for MQTT devices), also scheduled. |
| `events.py` | Encrypted security log (logins, failed attempts, users, devices, updates, tools). |
| `tls.py` | HTTPS with an automatically generated self-signed certificate (or one you provide). |

**Streaming answers**: the model writes the answer word by word; as soon as a sentence is complete it is turned
into speech and sent, while the model keeps writing the next ones. The first word is heard after the first
sentence, not after the whole answer. The system prompt only contains the date (the time comes from the built-in
`ora_attuale` tool), so the start of the conversation stays identical and Ollama reuses the work already done.
Timings (speech recognition, first words from the model, first audio) are shown on the Admin page.

## 2. What happens when you speak

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant C as Browser / device
    participant S as Server (session.py)
    participant W as Whisper
    participant O as Ollama (Qwen3)
    participant H as HuskyLens (MCP)
    participant P as Piper

    C->>S: hello (audio format)
    S-->>C: hello (session_id)
    U->>C: holds the button and speaks
    C->>S: listen start + audio in 60 ms packets
    C->>S: listen stop (or end of sentence detected by the server)
    S->>W: sentence audio
    W-->>S: "What do you see?"
    S-->>C: stt "What do you see?"
    S->>O: question + list of tools
    O-->>S: calls the get_recognition_result tool
    S->>H: tools/call
    H-->>S: {"faces":[{"name":"Alex"}]}
    S->>O: tool result
    O-->>S: "I can see Alex..." (word by word, streaming)
    loop as soon as a sentence is complete (the model keeps writing)
        S->>P: sentence
        P-->>S: audio
        S-->>C: tts sentence_start + audio at real-time pace
    end
    S-->>C: tts stop
    S->>S: saves question and answer to the history (local disk)
    Note over U,C: If the user presses again while the assistant is speaking:<br/>abort → the voice stops immediately and the new question is heard
```

## 3. Session states

```mermaid
stateDiagram-v2
    [*] --> Connected: hello
    Connected --> Listening: listen start
    Listening --> Transcribing: listen stop / end of sentence
    Transcribing --> Listening: nothing understandable
    Transcribing --> Thinking: text
    Thinking --> Tools: the model asks for a tool
    Tools --> Thinking: result
    Thinking --> Speaking: answer
    Speaking --> Listening: answer finished (hands-free mode)
    Speaking --> Connected: answer finished (push-to-talk mode)
    Speaking --> Listening: abort / new press
    Connected --> [*]: disconnect (history stays on disk)
```

## 4. Installation and startup

```mermaid
flowchart TB
    A["install.sh (Linux) / install.ps1 (Windows)"] --> B["Missing system packages<br/>(Linux only, via apt)"]
    B --> C["Python environment .venv<br/>+ requirements.txt"]
    C --> D{"NVIDIA GPU?"}
    D -- yes --> E["+ CUDA libraries<br/>Whisper large-v3-turbo"]
    D -- no --> F["Whisper small on CPU"]
    E --> G["Ollama already installed?"]
    F --> G
    G -- no --> H["Portable Ollama<br/>in runtime/"]
    G -- yes --> I["use the system one"]
    H --> J["Download models: voice, Whisper, Qwen3"]
    I --> J
    J --> K{"--service / -Service?"}
    K -- yes --> L["systemd / task at logon"]
    K -- no --> M["manual start:<br/>start.sh / avvia.bat"]
```

At startup (`start.sh` / `start.ps1`) Ollama is started, if it is not already running, on `127.0.0.1` only, with an
8192-token context. Then `server.py` starts and works offline, because the models are already on disk.

## 5. How it was verified

```mermaid
flowchart LR
    subgraph unit["Components"]
        T1["Opus: encode / decode"]
        T2["Whisper GPU vs CPU"]
        T3["MCP: official SDK +<br/>hand-written server (2024-11-05)"]
        T4["test_units.py: 16<br/>history, encryption,<br/>permissions, privacy"]
    end
    subgraph e2e["test_suite.py: 28 checks"]
        E1["OTA, token, hello"]
        E2["voice → tool<br/>protocol v1 / v2 / v3"]
        E3["push-to-talk, hands-free,<br/>interruption, memory"]
        E4["HuskyLens, vision,<br/>latency, 3 devices"]
        E5["history on disk,<br/>token-protected"]
    end
    subgraph browser["tests/browser_mic_test.py"]
        B1["real Chrome with<br/>simulated microphone"]
    end
    subgraph sec["test_auth.py: 64 checks"]
        S1["login, lockout, 2FA,<br/>permissions, CSRF"]
        S2["devices, impersonation,<br/>HTTPS, encrypted data"]
    end
    subgraph upd["test_update.py: 23 checks"]
        U1["identical data<br/>after update"]
        U2["broken release, crash<br/>at startup → rollback"]
    end
    subgraph net["Network check"]
        N1["no connections<br/>to the Internet"]
    end
    subgraph install["Clean installation"]
        I1["Windows 11 + GPU"]
        I2["Ubuntu 24.04 without GPU"]
    end
    unit --> e2e --> browser --> net --> sec --> upd --> install
```

All tests run with one command: `python tests/run_all.py` (Ollama must be running; with `--mock` it also works without).

## 6. Security: login and devices

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant B as Browser
    participant S as Server (app.py + auth.py)
    participant D as data/ (users, sessions)

    U->>B: username + password
    B->>S: POST /api/login
    S->>S: too many failed attempts? → 429 (15 min lockout)
    S->>D: check password (scrypt)
    alt 2FA enabled
        S-->>B: code required
        U->>B: 6-digit code from the app
        B->>S: POST /api/login + code
        S->>S: TOTP valid and not used before?
    end
    S->>D: new session (only the token hash is stored)
    S-->>B: HttpOnly, SameSite=Strict cookie
    B->>S: pages, history, WebSocket with the cookie
```

```mermaid
sequenceDiagram
    participant E as ESP32 device
    participant S as Server
    actor A as Admin
    E->>S: OTA (Device-Id = MAC, Client-Id = secret UUID)
    S-->>E: activation code 123456 (no WebSocket)
    E->>E: shows 123456 on its screen
    A->>S: Admin → Devices → "123456"
    E->>S: OTA
    S-->>E: WebSocket + token bound to MAC + Client-Id (HMAC with the server key)
    Note over S: same MAC with another Client-Id = new device to approve
    E->>S: WebSocket with the token → assistant
```

## 7. Updates

Code and data are kept apart: `config.yaml` and the program are replaced by updates; `data/` (personal settings,
users, devices, sessions, history), `models/` and `runtime/` never are. An update only starts from the Admin page
button (or `update.sh` / `update.ps1`); the flow diagram is in the [README](README.en.md#8-updates).

| Step | Detail |
|---|---|
| Check | `GET api.github.com/repos/<repo>/releases/latest` → version and release notes (`CHANGELOG`) |
| Hand edits | if `config.yaml` was edited (compared with `data/.manifest.json`), a copy is saved in `data/` |
| Backup | zip of the current code in `runtime/backups/` |
| Install | from Git (`git checkout <tag>`) or from the release zip; files are replaced with `os.replace` |
| Packages | `pip install -r requirements.txt` (and `requirements/gpu.txt` if the GPU libraries were installed) |
| Test | `server.py --selftest` on the new version: if it does not load, immediate restore |
| Failure | automatic restore from the backup (code and packages) |
| Restart | the server exits with code 42: `start.sh` / `start.ps1` / systemd restart it |
| Confirmation | `data/.update_pending.json`: removed when the new version stays up for 20 seconds |
| Automatic rollback | if the new version fails to start twice, the third start restores the backup and reports it in Admin |
| Migrations | on restart `config.py` upgrades the `data/` format if needed (`data/schema.json`) |

## 8. File layout

```
xiaozhi-local-server/
├── install.sh / install.ps1   automatic installation (Linux / Windows)
├── start.sh / start.ps1       start (Ollama + server); avvia.bat = double click on Windows
├── update.sh / update.ps1     update from the terminal
├── server.py                  the server
├── config.yaml                default settings (yours go in data/config.yaml)
├── requirements.txt           Python packages (requirements/: GPU and tests)
├── VERSION, CHANGELOG.md      version and release notes
├── xiaozhi_locale/            server code
├── web/                       pages: assistant, history, login, account, admin
├── scripts/                   model download, network connection check
├── docs/                      privacy, security, architecture (with diagrams)
├── tests/                     automated tests and simulators
└── data/                      YOUR DATA: created on first start, never touched by updates
```
