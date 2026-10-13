import socket
import threading
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import uvicorn

from worker.config import DEFAULT_ENV_FILE, Environment, Settings


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def acme_url():
    """Runs the real Acme sandbox on a free port for the whole test session."""
    from acme.app import app
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            httpx.get(url + "/", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield url
    server.should_exit = True


@pytest.fixture
def acme(acme_url):
    """Fresh sandbox state for each test; returns a small control helper."""
    httpx.post(acme_url + "/admin/reset")

    class Acme:
        url = acme_url

        @staticmethod
        def chaos(spec: str):
            httpx.post(acme_url + "/admin/chaos", json={"spec": spec})

        @staticmethod
        def state() -> dict:
            return httpx.get(acme_url + "/admin/state").json()

    return Acme


@pytest.fixture
def env(acme_url) -> Environment:
    e = Environment.load(DEFAULT_ENV_FILE)
    return replace(e, start_url=acme_url + "/", allowed_origins=[acme_url])


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(model="scripted", runs_dir=tmp_path / "runs", headless=True, max_steps=40)
