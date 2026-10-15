"""Test del client web in Chrome vero, con microfono simulato da un file WAV.

Chrome riceve l'audio del file come se arrivasse dal microfono; si verifica che il
client web lo invii al server, che arrivino trascrizione e risposta e che l'audio
di risposta venga riprodotto nella pagina.

Uso:  .venv\\Scripts\\python tests\\browser_mic_test.py [http://localhost:8079/?token=test123]
"""
import sys
import time
import wave
from pathlib import Path

import numpy as np
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from xiaozhi_locale.engines import TTS  # noqa: E402

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8079/?token=test123"
PHRASE = "Quante zampe ha un gatto?"

tts = TTS({"voice": str(ROOT / "models/piper/it_IT-paola-medium.onnx")})
pcm = tts._run(PHRASE)
sr = tts.sample_rate
audio = np.concatenate([np.zeros(sr // 2, np.int16), pcm, np.zeros(sr * 4, np.int16)])
wav_path = ROOT / "tests" / "_mic.wav"
with wave.open(str(wav_path), "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes(audio.tobytes())
speech_s = 0.5 + len(pcm) / sr

# conta l'audio riprodotto dalla pagina
INIT = """
window.__played = 0;
const orig = AudioBufferSourceNode.prototype.start;
AudioBufferSourceNode.prototype.start = function(...a){ window.__played += this.buffer ? this.buffer.duration : 0; return orig.apply(this, a); };
"""

ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(f"[{'OK ' if ok else 'KO '}] {name}  {detail}", flush=True)


with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True, args=[
        "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
        f"--use-file-for-fake-audio-capture={wav_path}", "--autoplay-policy=no-user-gesture-required",
    ])
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: m.type == "error" and errors.append(m.text))
    page.add_init_script(INIT)
    page.goto(URL)
    page.wait_for_function("document.getElementById('state').textContent === 'connesso'", timeout=10000)
    check("Pagina caricata e connessa", True)

    # --- 1. tieni premuto per parlare
    box = page.locator("#talk").bounding_box()
    page.mouse.move(box["x"] + 20, box["y"] + 10)
    page.mouse.down()
    time.sleep(speech_s + 1.0)
    page.mouse.up()
    page.wait_for_selector(".msg.bot", timeout=60000)
    time.sleep(4)
    user = page.locator(".msg.user").first.text_content()
    bot = " ".join(page.locator(".msg.bot").all_text_contents())
    played = page.evaluate("window.__played")
    page.screenshot(path=str(ROOT / "tests" / "_web_desktop.png"))
    check("Push-to-talk: trascrizione", "zamp" in user.lower(), f"utente='{user}'")
    check("Push-to-talk: risposta", len(bot.strip()) > 5, f"risposta='{bot}'")  # la precisione dipende dalla voce sintetica
    check("Push-to-talk: audio riprodotto", played > 1.0, f"{played:.1f}s")

    # --- 2. conversazione continua (VAD lato server)
    n_bot = page.locator(".msg.bot").count()
    page.reload()
    page.wait_for_function("document.getElementById('state').textContent === 'connesso'", timeout=10000)
    page.click("#auto")
    page.wait_for_function("document.querySelectorAll('.msg.bot').length > 0", timeout=60000)
    time.sleep(3)
    page.click("#auto")
    user2 = page.locator(".msg.user").first.text_content()
    played2 = page.evaluate("window.__played")
    check("Conversazione continua: frase rilevata da sola", "zamp" in user2.lower(), f"utente='{user2}'")
    check("Conversazione continua: audio riprodotto", played2 > 1.0, f"{played2:.1f}s")

    # --- 3. testo scritto
    page.fill("#text", "Rispondi solo con la parola ciao.")
    page.press("#text", "Enter")
    page.wait_for_function("[...document.querySelectorAll('.msg.bot')].some(e => /ciao/i.test(e.textContent))", timeout=60000)
    check("Domanda scritta", True)

    # --- 4. interruzione: mentre racconta una storia, premo per parlare
    page.fill("#text", "Raccontami una storia lunga almeno dieci frasi su un drago.")
    page.press("#text", "Enter")
    page.wait_for_function("document.getElementById('state').textContent === 'parla…'", timeout=60000)
    time.sleep(2.5)
    played_before = page.evaluate("window.__played")
    n_user = page.locator(".msg.user").count()
    box = page.locator("#talk").bounding_box()
    page.mouse.move(box["x"] + 20, box["y"] + 10)
    page.mouse.down()
    still_playing = page.evaluate("sources.length")
    time.sleep(speech_s + 1.0)
    page.mouse.up()
    page.wait_for_function(f"document.querySelectorAll('.msg.user').length > {n_user}", timeout=30000)
    new_q = page.locator(".msg.user").nth(n_user).text_content()
    check("Interruzione: la voce si ferma subito", still_playing == 0, f"sorgenti attive={still_playing}")
    check("Interruzione: ascolta la nuova domanda", "zamp" in new_q.lower() or "gatto" in new_q.lower(), f"utente='{new_q}'")

    page.set_viewport_size({"width": 375, "height": 740})
    page.screenshot(path=str(ROOT / "tests" / "_web_mobile.png"))
    check("Nessun errore JavaScript", not errors, "; ".join(errors[:3]))
    browser.close()

wav_path.unlink(missing_ok=True)
print("ESITO:", "OK" if ok_all else "FALLITO")
sys.exit(0 if ok_all else 1)
