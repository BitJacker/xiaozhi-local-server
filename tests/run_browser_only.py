"""Esegue un solo test (predefinito: quello nel browser) (con HuskyLens simulata e server di prova), senza il resto della suite."""
import subprocess
import sys

sys.path.insert(0, "tests")
from run_all import PY, ROOT, env, make_config, stop, wait_health  # noqa: E402

(ROOT / "tests" / "config.suite.yaml").write_text(
    make_config(8079, "tests/_data_test", auth=False, turns=1000, huskylens=True), encoding="utf-8")
h = subprocess.Popen([PY, "tests/fake_huskylens.py"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
s = subprocess.Popen([PY, "server.py", "-c", "tests/config.suite.yaml"], cwd=ROOT, env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    if not wait_health("http://127.0.0.1:8079/health", '"huskylens": 2'):
        print("server di prova non partito")
        sys.exit(1)
    script = sys.argv[1] if len(sys.argv) > 1 else "tests/browser_mic_test.py"
    sys.exit(subprocess.call([PY, script], cwd=ROOT, env=env))
finally:
    stop([h, s])
