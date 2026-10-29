import asyncio
import json
import time

from aiohttp import web


class Fake:
    """Fake OpenAI Chat Completions upstream for tests."""

    def __init__(
        self,
        prompt_tokens=1000,
        completion_tokens=500,
        text="Hello from the fake.",
        stream_tokens=None,
        tokens_per_chunk=1,
        chunk_delay=0.0,
        delta_key="content",
        redirect=None,
        fail=None,
    ):
        self.delta_key, self.redirect, self.fail = delta_key, redirect, fail
        self.prompt_tokens, self.completion_tokens, self.text = prompt_tokens, completion_tokens, text
        self.stream_tokens, self.tokens_per_chunk, self.chunk_delay = (
            stream_tokens,
            tokens_per_chunk,
            chunk_delay,
        )
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self.chunks_sent = 0

    @property
    def count(self):
        return len(self.requests)

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self.chat)
        return app

    async def chat(self, req: web.Request):
        body = await req.json()
        self.requests.append(body)
        self.headers.append(dict(req.headers))
        if self.fail == "reset":
            req.transport.close()
            return web.Response()
        if self.fail == "hang":
            await asyncio.sleep(5)
        if self.redirect:
            raise web.HTTPTemporaryRedirect(self.redirect)
        base = {"id": "chatcmpl-fake", "created": int(time.time()), "model": body.get("model")}
        if not body.get("stream"):
            if self.chunk_delay:
                await asyncio.sleep(self.chunk_delay)
            usage = {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.prompt_tokens + self.completion_tokens,
            }
            msg = {"role": "assistant", "content": self.text}
            return web.json_response(
                {
                    **base,
                    "object": "chat.completion",
                    "usage": usage,
                    "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
                }
            )
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(req)
        base["object"] = "chat.completion.chunk"
        n = self.stream_tokens if self.stream_tokens is not None else self.completion_tokens

        async def send(d):
            await resp.write(b"data: " + json.dumps(d).encode() + b"\n\n")
            self.chunks_sent += 1

        try:
            await send({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]})
            for i in range(0, n, self.tokens_per_chunk):
                k = min(self.tokens_per_chunk, n - i)
                await send({**base, "choices": [{"index": 0, "delta": {self.delta_key: "tok " * k}}]})
                if self.chunk_delay:
                    await asyncio.sleep(self.chunk_delay)
                if self.fail == "stream_reset" and i >= 20:
                    req.transport.close()
                    return resp
            await send({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            if (body.get("stream_options") or {}).get("include_usage"):
                await send(
                    {
                        **base,
                        "choices": [],
                        "usage": {
                            "prompt_tokens": self.prompt_tokens,
                            "completion_tokens": n,
                            "total_tokens": self.prompt_tokens + n,
                        },
                    }
                )
            await resp.write(b"data: [DONE]\n\n")
        except (ConnectionResetError, RuntimeError):
            pass
        return resp
