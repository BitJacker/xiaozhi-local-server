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
```

| Module | What it does |
|---|---|
| `app.py` | Runs the web server: client page, OTA for devices, WebSocket, vision endpoint, history API, `/health`. Checks the token. |
| `session.py` | Implements the xiaozhi protocol for one connection: `hello`, `listen`, `abort`, `mcp`. Collects audio, decides when a sentence has ended, runs the turn and streams the voice back. |
| `audio.py` | Opus decoding and encoding (60 ms frames, 16 kHz in and 24 kHz out), resampling and end-of-sentence detection. |
| `engines.py` | Whisper (on the GPU if available, otherwise on the CPU) and Piper. Strips emoji and markdown before reading text aloud. |
| `llm.py` | Talks to the language model, runs the tools the model asks for, and repeats until there is an answer. With Ollama it downloads and preloads the model. |
| `tools.py` | Gathers MCP tools: the device's own (over WebSocket) and the HuskyLens 2 ones (over SSE, with automatic reconnection). |
| `history.py` | Saves questions and answers on the server's disk, one file per day, and serves them to the `/cronologia` page. |

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
    O-->>S: "I can see Alex in front of me."
    loop for each sentence of the answer
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
    end
    subgraph e2e["tests/test_suite.py: 28 checks"]
        E1["OTA, token, hello"]
        E2["voice → tool<br/>protocol v1 / v2 / v3"]
        E3["push-to-talk, hands-free,<br/>interruption, memory"]
        E4["HuskyLens, vision,<br/>latency, 3 devices"]
        E5["history on disk,<br/>token-protected"]
    end
    subgraph browser["tests/browser_mic_test.py"]
        B1["real Chrome with<br/>simulated microphone"]
    end
    subgraph net["Network check"]
        N1["no connections<br/>to the Internet"]
    end
    subgraph install["Clean installation"]
        I1["Windows 11 + GPU"]
        I2["Ubuntu 24.04 without GPU"]
    end
    unit --> e2e --> browser --> net --> install
```

All tests run with one command: `python tests/run_all.py` (Ollama must be running).

## 6. File layout

```
xiaozhi-local-server/
├── server.py                 entry point (switches to offline mode if the models are present)
├── config.yaml               configuration
├── install.sh / install.ps1  automatic installation, Linux / Windows
├── start.sh / start.ps1      start (Ollama + server); avvia.bat = double-click on Windows
├── verifica_privacy.ps1      network connection check
├── xiaozhi_locale/           server code
├── web/index.html            web client
├── web/cronologia.html       history page
├── data/history/             history (created on use, stays on the server only, excluded from Git)
├── PRIVACY.md / .en.md       privacy and how to verify it
├── scripts/download_models.py
├── tests/                    automated tests and simulators (HuskyLens, device, LLM)
└── docs/                     this page (ARCHITECTURE.md) and the Italian version (ARCHITETTURA.md)
```
