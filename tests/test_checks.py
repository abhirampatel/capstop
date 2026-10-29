import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import aiohttp
import openai
from aiohttp import web

from capstop.ledger import Ledger
from capstop.proxy import make_app

from .conftest import AUTH, Capstop, get_run, post

BODY = {"model": "test-model", "messages": [{"role": "user", "content": "Say hi"}], "max_tokens": 500}


def test_check1_openai_client_passthrough(capstop, up):
    client = openai.OpenAI(
        api_key=AUTH.split()[1], base_url=capstop.url + "/v1", default_headers={"X-Run-Id": "r1"}
    )
    r = client.chat.completions.create(model="test-model", messages=BODY["messages"], max_tokens=500)
    assert r.choices[0].message.content == "Hello from the fake."
    assert up.headers[0]["Authorization"] == AUTH
    assert not any(k.lower().startswith("x-run-") for k in up.headers[0])


def test_check2_cost_http_and_cli(capstop, up):
    # check 1's call on r1: fake usage 1000 prompt + 500 completion
    assert f"{get_run(capstop.url, 'r1')['spent_usd']:.6f}" == "0.002000"
    assert f"{json.loads(capstop.cli('runs', 'r1'))['spent_usd']:.6f}" == "0.002000"
    assert any(r["run_id"] == "r1" for r in json.loads(capstop.cli("runs")))


def test_check3_cap_blocks_third_call(capstop, up):
    codes = [post(capstop.url, BODY, "r3", 0.005) for _ in range(3)]
    assert [c for c, _ in codes] == [200, 200, 402]
    e = json.loads(codes[2][1])["error"]
    assert e == {"type": "run_budget_exceeded", "run_id": "r3", "spent_usd": 0.004, "cap_usd": 0.005}
    assert up.count == 2
    assert b"SECRET123" not in codes[2][1]


def test_check4_preflight(capstop, up):
    code, body = post(capstop.url, {**BODY, "max_tokens": 100000}, "r4", 0.001)
    assert code == 402 and json.loads(body)["error"]["type"] == "run_budget_exceeded"
    assert up.count == 0


def test_check5_streaming_usage(capstop, up):
    up.completion_tokens = 50
    client = openai.OpenAI(
        api_key=AUTH.split()[1], base_url=capstop.url + "/v1", default_headers={"X-Run-Id": "r5"}
    )
    chunks = list(client.chat.completions.create(model="test-model", messages=BODY["messages"], stream=True))
    assert up.requests[0]["stream_options"] == {"include_usage": True}
    assert len(chunks) == up.chunks_sent and len(chunks) == 1 + 50 + 1 + 1
    assert "".join(c.choices[0].delta.content or "" for c in chunks if c.choices) == "tok " * 50
    usage = chunks[-1].usage
    assert get_run(capstop.url, "r5")["spent_usd"] == round(
        (usage.prompt_tokens * 1 + usage.completion_tokens * 2) / 1e6, 9
    )


def test_check6_midstream_cutoff(capstop, up):
    up.stream_tokens, up.chunk_delay = 10_000, 0.0005
    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 400,
        "stream": True,
    }
    req = urllib.request.Request(
        capstop.url + "/v1/chat/completions",
        json.dumps(body).encode(),
        {
            "Content-Type": "application/json",
            "Authorization": AUTH,
            "X-Run-Id": "r6",
            "X-Run-Cap-USD": "0.001",
        },
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req) as r:
        data = r.read().decode()
    elapsed = time.monotonic() - t0
    run = get_run(capstop.url, "r6")
    tokens = data.count("tok ")
    print(f"\ncheck6: {tokens} tokens forwarded, closed+marked in {elapsed * 1000:.0f} ms, run={run}")
    assert tokens < 1000 and elapsed < 1 and run["over_cap"]
    assert '"run_budget_exceeded"' in data and "[DONE]" not in data
    assert post(capstop.url, BODY, "r6", 0.001)[0] == 402


def test_check7_unpriced(capstop, up):
    code, body = post(capstop.url, {**BODY, "model": "unknown-model"}, "r7")
    assert code == 400 and json.loads(body)["error"]["type"] == "unpriced_model"
    assert up.count == 0


def test_check8_concurrency(capstop, up):
    up.prompt_tokens = 0
    with ThreadPoolExecutor(20) as ex:
        codes = list(ex.map(lambda _: post(capstop.url, BODY, "r8", 0.01)[0], range(20)))
    r = get_run(capstop.url, "r8")
    print(f"\ncheck8: forwarded={up.count} 200s={codes.count(200)} 402s={codes.count(402)} run={r}")
    assert up.count <= 9 and codes.count(200) == up.count and codes.count(402) == 20 - up.count
    assert r["spent_usd"] <= 0.01 and not r["over_cap"]


def test_check8b_underestimate(capstop, up):
    up.chunk_delay = 0.3  # upstream latency so the 20 calls overlap, as with a real provider
    with ThreadPoolExecutor(20) as ex:
        codes = list(ex.map(lambda _: post(capstop.url, BODY, "r8b2", 0.011)[0], range(20)))
    r, n = get_run(capstop.url, "r8b2"), up.count
    print(f"\ncheck8b: forwarded={n} 200s={codes.count(200)} 402s={codes.count(402)} run={r}")
    assert codes.count(200) == n and codes.count(402) == 20 - n
    assert r["spent_usd"] == round(n * 0.002, 9) and r["over_cap"] and r["calls"] == n
    code, body = post(capstop.url, BODY, "r8b2", 0.011)
    assert code == 402 and json.loads(body)["error"]["type"] == "run_budget_exceeded" and up.count == n


def test_exactly_at_cap_not_over(capstop, up):
    assert post(capstop.url, BODY, "rx", 0.002)[0] == 200
    r = get_run(capstop.url, "rx")
    assert r["spent_usd"] == 0.002 and not r["over_cap"]
    assert post(capstop.url, BODY, "rx", 0.002)[0] == 402 and up.count == 1


def test_check13_missing_run_id(capstop, up):
    before = len(json.loads(capstop.cli("runs")))
    code, body = post(capstop.url, BODY)
    assert code == 400 and json.loads(body)["error"]["type"] == "missing_run_id"
    assert up.count == 0 and len(json.loads(capstop.cli("runs"))) == before


def test_h1_max_tokens_enforced_upstream(capstop, up):
    body = {"model": "test-model", "messages": BODY["messages"]}
    assert post(capstop.url, body, "h1")[0] == 200
    assert up.requests[-1]["max_completion_tokens"] == 1000 and "max_tokens" not in up.requests[-1]
    assert post(capstop.url, {**BODY, "max_completion_tokens": 400}, "h1")[0] == 400
    assert post(capstop.url, {**BODY, "max_completion_tokens": 500}, "h1")[0] == 200
    assert up.count == 2 and up.requests[-1]["max_completion_tokens"] == 500


def test_h1_reasoning_deltas_trip_cutoff(capstop, up):
    up.stream_tokens, up.delta_key = 10_000, "reasoning_content"
    body = {**BODY, "max_tokens": 400, "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    code, data = post(capstop.url, body, "h1r", 0.001)
    assert code == 200 and data.decode().count("tok ") < 1000 and get_run(capstop.url, "h1r")["over_cap"]


def test_m1_bytes_and_parts_estimate(capstop, up):
    telugu = {"model": "test-model", "messages": [{"role": "user", "content": "త" * 1000}], "max_tokens": 1}
    assert post(capstop.url, telugu, "m1a", 0.002)[0] == 402  # 3000 bytes; chars/4 would pass
    img = [{"type": "image_url", "image_url": {"url": "data:x"}}]
    assert (
        post(capstop.url, {**telugu, "messages": [{"role": "user", "content": img}]}, "m1b", 0.0035)[0] == 402
    )
    f = [{"type": "file", "file": {"file_data": "x"}}]
    code, b = post(capstop.url, {**telugu, "messages": [{"role": "user", "content": f}]}, "m1c")
    assert code == 400 and json.loads(b)["error"]["type"] == "unsupported_part"
    assert up.count == 0


def test_m2_max_cap_and_global_ceiling(capstop, up, tmp_path):
    code, body = post(capstop.url, BODY, "m2", 10.01)
    assert code == 400 and json.loads(body)["error"]["type"] == "cap_above_max" and up.count == 0
    g = Capstop(str(tmp_path), up.url, global_cap_usd=0.0025)
    g.start()
    try:
        assert post(g.url, BODY, "g1")[0] == 200
        code, body = post(g.url, BODY, "g2")
        assert code == 402 and json.loads(body)["error"]["type"] == "global_budget_exceeded"
        assert up.count == 1
    finally:
        g.stop()


def test_l1_header_allowlist(capstop, up):
    extra = {"Cookie": "a=b", "Origin": "http://x", "X-Forwarded-For": "1.2.3.4", "OpenAI-Project": "p"}
    assert post(capstop.url, BODY, "l1", headers=extra)[0] == 200
    sent = {k.lower() for k in up.headers[-1]}
    assert "openai-project" in sent and not sent & {"cookie", "origin", "x-forwarded-for", "x-run-id"}


def test_l2_no_redirect_follow(capstop, up):
    up.redirect = up.url + "/elsewhere"
    assert post(capstop.url, BODY, "l2")[0] == 307
    assert up.count == 1 and get_run(capstop.url, "l2")["spent_usd"] == 0


def test_l3_timeouts():
    async def go():
        app = make_app("http://127.0.0.1:1", Ledger(":memory:"), {"m": (1, 2)}, 1.0, 10)
        for f in app.on_startup:
            await f(app)
        t = app["http"].timeout
        await app["http"].close()
        return t.connect, t.sock_read

    assert asyncio.run(go()) == (10, 120)


def test_l4_host_and_bind(capstop, up, tmp_path):
    assert post(capstop.url, BODY, "l4", headers={"Host": "evil.example"})[0] == 421
    assert post(capstop.url, BODY, "l4", headers={"Host": f"localhost:{capstop.port}"})[0] == 200
    for extra in (["--host", "0.0.0.0"], []):
        upstream = "http://10.0.0.1" if not extra else up.url
        r = subprocess.run(
            [
                sys.executable,
                "-m",
                "capstop.cli",
                "serve",
                "--upstream",
                upstream,
                "--db",
                str(tmp_path / "d"),
            ]
            + extra,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert r.returncode != 0 and "Traceback" not in r.stderr and len(r.stderr.strip().splitlines()) == 1


def test_l5_strict_ints(capstop, up):
    for bad in ({"max_tokens": True}, {"max_tokens": 1.5}, {"max_tokens": -1}, {"n": "x"}, {"n": 0}):
        assert post(capstop.url, {**BODY, **bad}, "l5")[0] == 400
    assert up.count == 0


def test_bad_prices_file_one_line_exit(up, tmp_path):
    cases = (
        ("corrupt", "{bad"),
        ("empty", "{}"),
        ("missing", None),
        ("list", "[1]"),
        ("models_list", '{"models": []}'),
    )
    for name, content in cases:
        f = tmp_path / f"{name}.json"
        if content is not None:
            f.write_text(content)
        cmd = [sys.executable, "-m", "capstop.cli", "serve", "--upstream", up.url, "--prices", str(f)]
        r = subprocess.run(
            cmd + ["--db", str(tmp_path / "d")], check=False, capture_output=True, text=True, timeout=10
        )
        print(f"\nprices {name}: rc={r.returncode} {r.stderr.strip()}")
        assert r.returncode == 1 and "Traceback" not in r.stderr and len(r.stderr.strip().splitlines()) == 1


def test_n1_reset_charged(capstop, up):
    up.fail = "reset"
    assert post(capstop.url, BODY, "n1a", 0.002)[0] == 502
    assert get_run(capstop.url, "n1a")["spent_usd"] == 0.001013  # worst: 13 prompt + 500 out
    up.fail, up.stream_tokens = "stream_reset", 1000
    code, data = post(capstop.url, {**BODY, "stream": True}, "n1b", 0.002)
    assert code == 200 and b"tok" in data and get_run(capstop.url, "n1b")["spent_usd"] >= 0.001013
    assert post(capstop.url, BODY, "n1a", 0.002)[0] == 402


def test_n1_timeout_charged(up):
    async def go():
        led = Ledger(":memory:")
        app = make_app(up.url, led, {"test-model": (1000, 2000)}, 1.0, 10, sock_read=(0.2, 0.2))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        async with aiohttp.ClientSession() as s:
            r = await s.post(
                f"http://127.0.0.1:{port}/v1/chat/completions", json=BODY, headers={"X-Run-Id": "t"}
            )
        await runner.cleanup()
        return r.status, led.get("t")

    up.fail = "hang"
    status, run = asyncio.run(go())
    assert status == 502 and run["spent_usd"] == 0.001013


def test_n2_bad_encoding_415(capstop, up):
    req = urllib.request.Request(
        capstop.url + "/v1/chat/completions",
        b"{}",
        {"Content-Type": "application/json", "Content-Encoding": "gzip", "X-Run-Id": "n2"},
    )
    try:
        urllib.request.urlopen(req)
        code = 200
    except urllib.error.HTTPError as e:
        code = e.code
    assert code == 415 and up.count == 0
    with open(os.path.join(capstop.dir, "capstop.log")) as f:
        assert "Traceback" not in f.read()


def _raw_post(url, data, headers):
    req = urllib.request.Request(url + "/v1/chat/completions", data, {"X-Run-Id": "enc", **headers})
    try:
        return urllib.request.urlopen(req).status
    except urllib.error.HTTPError as e:
        return e.code


def test_unknown_encoding_415_and_oversize_413(capstop, up):
    assert _raw_post(capstop.url, b"{}", {"Content-Encoding": "compress"}) == 415
    assert _raw_post(capstop.url, b"{}", {"Content-Encoding": "br"}) in (
        400,
        415,
    )  # aiohttp parser may reject first
    assert _raw_post(capstop.url, b"x" * (33 * 1024 * 1024), {"Content-Type": "application/json"}) == 413
    assert up.count == 0
    with open(os.path.join(capstop.dir, "capstop.log")) as f:
        assert "Traceback" not in f.read()


def test_log_over_cap_matches_runs(capstop, up):
    with open(os.path.join(capstop.dir, "capstop.log")) as f:
        lines = [x for x in f if "settled run=r8b2 " in x or "settled run=rx " in x]
    assert lines and get_run(capstop.url, "r8b2")["over_cap"] and not get_run(capstop.url, "rx")["over_cap"]
    assert any("run=r8b2" in x and x.rstrip().endswith("over_cap=True") for x in lines)
    assert all(x.rstrip().endswith("over_cap=False") for x in lines if "run=rx " in x)


def test_existing_db_chmod(tmp_path):
    db = tmp_path / "old.db"
    Ledger(str(db))
    for f in (db, tmp_path / "old.db-wal", tmp_path / "old.db-shm"):
        if f.exists():
            f.chmod(0o644)
    Ledger(str(db))
    assert all(f.stat().st_mode & 0o077 == 0 for f in tmp_path.iterdir())


def test_db_private_perms(tmp_path):
    Ledger(str(tmp_path / "p.db"))
    assert (tmp_path / "p.db").stat().st_mode & 0o077 == 0


def test_check10_latency(capstop, up):
    small = {"model": "test-model", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10}
    direct, proxied = [], []
    for _ in range(100):
        t = time.perf_counter()
        post(up.url, small)
        direct.append(time.perf_counter() - t)
        t = time.perf_counter()
        assert post(capstop.url, small, "r10", 10)[0] == 200
        proxied.append(time.perf_counter() - t)
    added = (statistics.median(proxied) - statistics.median(direct)) * 1000
    print(
        f"\ncheck10: median direct {statistics.median(direct) * 1000:.2f} ms, "
        f"proxied {statistics.median(proxied) * 1000:.2f} ms, added {added:.2f} ms"
    )
    assert added <= 20


def test_check11_restart(capstop, up):
    capstop.stop()
    capstop.start()
    assert get_run(capstop.url, "r3")["spent_usd"] == 0.004
    assert post(capstop.url, BODY, "r3", 0.005)[0] == 402
    assert up.count == 0


def test_check9_no_secret_on_disk(capstop, up):
    capstop.stop()
    r = subprocess.run(
        ["grep", "-r", "-c", "SECRET123", capstop.dir], check=False, capture_output=True, text=True
    )
    print(f"\ncheck9: grep -r over {capstop.dir}:\n{r.stdout}")
    assert all(line.endswith(":0") for line in r.stdout.splitlines())
    assert {"capstop.db", "capstop.log", "config.json"} <= set(__import__("os").listdir(capstop.dir))
