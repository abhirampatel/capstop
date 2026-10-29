import json
import logging
from decimal import Decimal, InvalidOperation

import aiohttp
from aiohttp import web

from .ledger import NANO, Ledger, usd

log = logging.getLogger("capstop")
FORWARD = {
    "authorization", "content-type", "accept", "openai-organization", "openai-project", "openai-beta",
    "user-agent", "idempotency-key",
}  # fmt: skip
PART_TOKENS = 4000


def load_prices(path: str) -> dict:
    with open(path) as f:
        data = json.load(f)
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, dict) or not all(isinstance(p, dict) for p in models.values()):
        raise ValueError('expected {"models": {name: {"input_per_1m": .., "output_per_1m": ..}}}')
    per_token = {}
    for name, p in models.items():
        per_token[name] = tuple(
            int(Decimal(str(p[k])) * NANO / 1_000_000) for k in ("input_per_1m", "output_per_1m")
        )
        if min(per_token[name]) < 0:
            raise ValueError(f"negative price for {name}")
    if not per_token:
        raise ValueError("no models")
    return per_token


def estimate_tokens(text: str) -> int:
    return len(text.encode("utf-8"))  # 1 token per byte: never below the BPE count


def prompt_estimate(body: dict) -> int:
    n = 3
    for m in body.get("messages") or []:
        c = m.get("content") if isinstance(m, dict) else m
        parts = c if isinstance(c, list) else [c]
        n += 4
        for part in parts:
            kind = part.get("type") if isinstance(part, dict) else None
            if kind == "file":
                raise ValueError("unsupported_part")
            if kind in ("image_url", "input_audio"):
                n += PART_TOKENS
            elif kind == "text" and isinstance(part.get("text"), str):
                n += estimate_tokens(part["text"])
            else:
                n += estimate_tokens(part if isinstance(part, str) else json.dumps(part))
    if body.get("tools"):
        n += estimate_tokens(json.dumps(body["tools"]))
    return n


def err(status: int, type_: str, **extra) -> web.Response:
    return web.json_response({"error": {"type": type_, **extra}}, status=status)


def over_cap_body(run_id: str, row: dict) -> dict:
    return {
        "type": "run_budget_exceeded",
        "run_id": run_id,
        "spent_usd": usd(row["spent_nano"]),
        "cap_usd": usd(row["cap_nano"]),
    }


def pos_int(v, hi: int) -> bool:
    return type(v) is int and 0 < v <= hi


def make_app(
    upstream: str,
    ledger: Ledger,
    prices: dict,
    default_cap_usd: float,
    default_max_tokens: int,
    max_cap_usd: float = 10.0,
    global_cap_usd: float = 100.0,
    hosts: frozenset = frozenset({"127.0.0.1", "localhost", "::1"}),
    sock_read: tuple = (120, 600),
) -> web.Application:
    stream_timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=sock_read[0])
    slow_timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=sock_read[1])
    max_cap = Decimal(str(max_cap_usd))
    global_nano = int(Decimal(str(global_cap_usd)) * NANO)
    base = upstream.rstrip("/")
    url = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")

    async def startup(app):
        app["http"] = aiohttp.ClientSession(
            trust_env=False, timeout=aiohttp.ClientTimeout(total=None, connect=10, sock_read=120)
        )

    async def cleanup(app):
        await app["http"].close()

    async def chat(req: web.Request):
        if req.headers.get("Content-Encoding", "identity").strip().lower() not in (
            "identity",
            "gzip",
            "deflate",
        ):
            return err(415, "unsupported_content_encoding")
        try:
            raw = await req.read()
        except web.HTTPRequestEntityTooLarge:
            return err(413, "request_too_large")
        except Exception:  # noqa: BLE001 (corrupt gzip/deflate body)
            return err(415, "unsupported_content_encoding")
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return err(400, "invalid_request")
        model = body.get("model")
        if model not in prices:
            return err(400, "unpriced_model", model=str(model)[:100])
        p_in, p_out = prices[model]
        run_id = req.headers.get("X-Run-Id", "")[:200]
        if not run_id:
            return err(400, "missing_run_id")
        try:
            cap = Decimal(req.headers.get("X-Run-Cap-USD", str(default_cap_usd)))
        except InvalidOperation:
            cap = Decimal(-1)
        if not cap.is_finite() or cap < 0:
            return err(400, "invalid_cap")
        if cap > max_cap:
            return err(400, "cap_above_max", max_cap_usd=float(max_cap))
        cap_nano = int(cap * NANO)
        a, b = body.get("max_completion_tokens"), body.get("max_tokens")
        if a is not None and b is not None and a != b:
            return err(400, "invalid_request", param="max_tokens")
        max_out = default_max_tokens if a is None and b is None else (a if a is not None else b)
        n = body.get("n", 1)
        if not pos_int(max_out, 10**9) or not pos_int(n, 128):
            return err(400, "invalid_request", param="max_tokens/n")
        body.pop("max_tokens", None)
        body["max_completion_tokens"] = max_out
        try:
            n_in = prompt_estimate(body)
        except ValueError:
            return err(400, "unsupported_part", part="file")
        worst = n_in * p_in + max_out * p_out * n
        ok, row, headroom = ledger.reserve(run_id, cap_nano, worst, global_nano)
        if row is None:
            log.info("refused run=%s global ceiling", run_id)
            return err(402, "global_budget_exceeded", global_cap_usd=usd(global_nano))
        if not ok:
            log.info("refused run=%s model=%s worst_usd=%s", run_id, model, usd(worst))
            return web.json_response({"error": over_cap_body(run_id, row)}, status=402)

        headers = {k: v for k, v in req.headers.items() if k.lower() in FORWARD}
        stream = bool(body.get("stream"))
        if stream:
            body["stream_options"] = {**(body.get("stream_options") or {}), "include_usage": True}
        settled, resp, out_est = False, None, 0

        def settle(actual: int, over: bool = False):
            nonlocal settled
            if not settled:
                settled = True
                over = ledger.settle(run_id, worst, actual, over)
                log.info("settled run=%s model=%s cost_usd=%s over_cap=%s", run_id, model, usd(actual), over)

        def cost(usage: dict) -> int:
            def tok(k):
                v = usage.get(k) or 0
                if type(v) is not int:
                    raise TypeError(k)
                return max(0, v)

            return tok("prompt_tokens") * p_in + tok("completion_tokens") * p_out

        try:
            async with app["http"].post(
                url,
                json=body,
                headers=headers,
                allow_redirects=False,
                timeout=stream_timeout if stream else slow_timeout,
            ) as up:
                if up.status != 200 or not stream:
                    data = await up.read()
                    if up.status == 200:
                        try:
                            usage = json.loads(data)["usage"]
                            settle(cost(usage))
                        except (ValueError, KeyError, TypeError, AttributeError):
                            settle(worst)
                    else:
                        settle(0)
                    ct = up.headers.get("Content-Type", "application/json")
                    return web.Response(body=data, status=up.status, headers={"Content-Type": ct})
                resp = web.StreamResponse(
                    headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
                )
                await resp.prepare(req)
                buf = b""
                async for chunk in up.content.iter_any():
                    buf += chunk
                    while b"\n\n" in buf:
                        event, buf = buf.split(b"\n\n", 1)
                        line = event.strip()
                        if line.startswith(b"data:") and line[5:].strip() != b"[DONE]":
                            try:
                                d = json.loads(line[5:])
                            except ValueError:
                                d = {}
                            if d.get("usage"):
                                try:
                                    settle(cost(d["usage"]))
                                except (TypeError, AttributeError):
                                    settle(worst)
                            for ch in d.get("choices") or []:
                                delta = ch.get("delta") or {}
                                tc = delta.get("tool_calls")
                                text = "".join(
                                    v if isinstance(v, str) else ""
                                    for v in (
                                        delta.get(k) for k in ("content", "reasoning_content", "reasoning")
                                    )
                                ) + (json.dumps(tc) if tc else "")
                                out_est += estimate_tokens(text) if text else 0
                            if not settled and n_in * p_in + out_est * p_out > headroom:
                                settle(n_in * p_in + out_est * p_out, over=True)
                                info = over_cap_body(run_id, ledger._raw(run_id))
                                await resp.write(b"data: " + json.dumps({"error": info}).encode() + b"\n\n")
                                return resp
                        await resp.write(event + b"\n\n")
                if buf:
                    await resp.write(buf)
                settle(n_in * p_in + out_est * p_out)
                return resp
        except aiohttp.ClientConnectorError:
            settle(0)
            return err(502, "upstream_unavailable")
        except (aiohttp.ClientError, TimeoutError):
            settle(max(worst, n_in * p_in + out_est * p_out))
            return resp if resp is not None and resp.prepared else err(502, "upstream_unavailable")
        finally:
            settle(worst)

    async def run(req: web.Request):
        r = ledger.get(req.match_info["id"])
        return web.json_response(r) if r else err(404, "run_not_found")

    @web.middleware
    async def host_check(req, handler):
        h = req.host.rsplit("]", 1)[0].lstrip("[") if req.host.startswith("[") else req.host.split(":")[0]
        if h not in hosts:
            return err(421, "misdirected_request")
        return await handler(req)

    app = web.Application(client_max_size=32 * 1024 * 1024, middlewares=[host_check])
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    app.router.add_post("/v1/chat/completions", chat)
    app.router.add_get("/runs/{id}", run)
    return app
