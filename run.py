"""One-command demo launcher: starts the Acme sandbox (port 8000) and the AI Worker UI (port 8001).

    python run.py            # then open http://127.0.0.1:8001
"""
import os
import subprocess
import sys
import time
import webbrowser

PROCS = [
    ("Acme sandbox ", [sys.executable, "-m", "uvicorn", "acme.app:app", "--host", "127.0.0.1", "--port", "8000",
                       "--log-level", "warning"]),
    ("AI Worker UI ", [sys.executable, "-m", "worker", "serve", "--port", "8001"]),
]


def main() -> None:
    from worker.config import Settings
    from worker.llm import PROVIDERS, has_credentials

    settings = Settings()
    if not has_credentials(settings.provider):
        cfg = PROVIDERS[settings.provider]
        print(f"WARNING: no API key for provider '{settings.provider}'. Set {' or '.join(cfg['key_env'])} "
              f"(free key: {cfg['key_url']}).\n")
    procs = [subprocess.Popen(cmd, cwd=os.path.dirname(os.path.abspath(__file__))) for _, cmd in PROCS]
    time.sleep(2)
    print("Acme sandbox:  http://127.0.0.1:8000   (the simulated company)")
    print("AI Worker UI:  http://127.0.0.1:8001   (give the worker tasks here)")
    print("Press Ctrl+C to stop.")
    if "--no-browser" not in sys.argv:
        webbrowser.open("http://127.0.0.1:8001")
    try:
        while all(p.poll() is None for p in procs):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()


if __name__ == "__main__":
    main()
