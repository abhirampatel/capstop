import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import pytest
from aiohttp import web

from capstop.fake import Fake

AUTH = "Bearer sk-test-SECRET123"
PRICES = {"models": {"test-model": {"input_per_1m": 1, "output_per_1m": 2}}}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"port {port} not up")


@pytest.fixture(scope="session")
def fake():
    f = Fake()
    port = free_port()
    loop = asyncio.new_event_loop()

    async def start():
        runner = web.AppRunner(f.app())
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()

    loop.run_until_complete(start())
    threading.Thread(target=loop.run_forever, daemon=True).start()
    f.url = f"http://127.0.0.1:{port}"
    f.port = port
    return f


@pytest.fixture
def up(fake):
    defaults = Fake()
    for k in (
        "prompt_tokens",
        "completion_tokens",
        "text",
        "stream_tokens",
        "tokens_per_chunk",
        "chunk_delay",
        "delta_key",
        "redirect",
        "fail",
    ):
        setattr(fake, k, getattr(defaults, k))
    fake.requests.clear()
    fake.headers.clear()
    return fake


class Capstop:
    def __init__(self, tmp, upstream, **extra):
        self.dir, self.upstream = tmp, upstream
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        with open(os.path.join(tmp, "prices.json"), "w") as f:
            json.dump(PRICES, f)
        with open(os.path.join(tmp, "config.json"), "w") as f:
            cfg = {
                "db": os.path.join(tmp, "capstop.db"),
                "prices": os.path.join(tmp, "prices.json"),
                "default_cap_usd": 1.0,
                "default_max_tokens": 1000,
                "host": "127.0.0.1",
                "port": self.port,
                **extra,
            }
            json.dump(cfg, f)
        self.proc = None

    def start(self):
        self.log = open(os.path.join(self.dir, "capstop.log"), "a")  # noqa: SIM115
        cmd = [
            sys.executable,
            "-m",
            "capstop.cli",
            "serve",
            "--config",
            os.path.join(self.dir, "config.json"),
            "--upstream",
            self.upstream,
        ]
        self.proc = subprocess.Popen(cmd, stdout=self.log, stderr=subprocess.STDOUT)
        wait_port(self.port)

    def stop(self):
        self.proc.terminate()
        self.proc.wait(10)
        self.log.close()

    def cli(self, *args):
        cmd = [sys.executable, "-m", "capstop.cli", *args, "--config", os.path.join(self.dir, "config.json")]
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


@pytest.fixture(scope="session")
def capstop(fake, tmp_path_factory):
    g = Capstop(str(tmp_path_factory.mktemp("capstop")), fake.url)
    g.start()
    yield g
    if g.proc.poll() is None:
        g.stop()


def post(url, body, run=None, cap=None, auth=AUTH, headers=None):
    h = {"Content-Type": "application/json", "Authorization": auth, **(headers or {})}
    if run:
        h["X-Run-Id"] = run
    if cap is not None:
        h["X-Run-Cap-USD"] = str(cap)
    req = urllib.request.Request(url + "/v1/chat/completions", json.dumps(body).encode(), h)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def get_run(url, run):
    with urllib.request.urlopen(f"{url}/runs/{run}") as r:
        return json.loads(r.read())
