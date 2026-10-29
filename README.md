# Capstop

Capstop is a small local proxy that sits between your agent and OpenAI. It puts a hard dollar cap on each agent run. Once a run reaches its cap, the next call is refused with HTTP 402 before it reaches OpenAI.

OpenAI's own spending limits are monthly and are not enforced instantly. Capstop works per run, one request at a time.

It supports OpenAI Chat Completions only (`POST /v1/chat/completions`), streamed and non-streamed. Other endpoints return 404.

## How it works

- You point your OpenAI client's `base_url` at Capstop and add an `X-Run-Id` header.
- Before forwarding a call, Capstop estimates its worst-case cost. That is the prompt size plus `max_completion_tokens`, `max_tokens` or a default of 4096 output tokens, priced from `capstop/prices.json`. If the run's spend plus that estimate would go over the cap, it returns 402 and does not forward the call.
- It forwards the output limit it reserved as `max_completion_tokens`, so OpenAI cannot return more than the estimate allows.
- After OpenAI answers, it records the actual cost from the `usage` OpenAI reports. If that puts the run over its cap, the answer is still returned (it is already paid for), and every later call on that run gets 402.
- For streams, it asks OpenAI to include usage (`stream_options.include_usage`). It closes the stream if the running output estimate would go over the cap.
- A model with no entry in the price file is refused with 400 `unpriced_model`.
- Spend is kept in a local SQLite file, `capstop.db`, so it survives restarts.

## Your API key

Your agent sends its own OpenAI key as usual, in `Authorization: Bearer ...`. Capstop forwards it to OpenAI and does not store or log it. It never reads a key from a file or the environment. The key stays on your machine.

## Install

You need Linux, bash and Python 3.11 or newer with `venv` (on Debian or Ubuntu: `apt install python3-venv`).

There is no separate install step. `run.sh` creates `.venv` in `backend/`, installs the pinned dependencies from `requirements.lock` with hash checking, then installs the project. Later runs skip this unless the lock file or code changed. To only set up the environment:

```
cd backend
./run.sh setup
```

## Run

No-key demo, with a fake OpenAI server on port 9999:

```
cd backend
./run.sh fake
```

Real use, forwarding to `https://api.openai.com`:

```
cd backend
./run.sh
```

Both listen on `127.0.0.1:8787`. Stop with Ctrl-C. In fake mode this also stops the fake server. Extra arguments are passed to `capstop serve`, for example `./run.sh fake --port 8800`.

Environment variables read by `run.sh`:

| Variable | Default | Use |
|---|---|---|
| `CAPSTOP_UPSTREAM` | `https://api.openai.com` | Where `./run.sh` forwards to |
| `CAPSTOP_FAKE_PORT` | `9999` | Fake server port in fake mode |
| `CAPSTOP_VENV` | `backend/.venv` | Virtual environment path |
| `PYTHON` | `python3` | Python used to create the venv |

`.env.example` lists them. `run.sh` does not load it on its own.

Then point your client at Capstop:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8787/v1",
    default_headers={"X-Run-Id": "my-run-1", "X-Run-Cap-USD": "0.50"},
)
```

## Headers

| Header | Required | Meaning |
|---|---|---|
| `X-Run-Id` | Yes | Names the run. Without it: 400 `missing_run_id`, nothing forwarded. Cut to 200 characters. |
| `X-Run-Cap-USD` | No | The run's cap in US dollars. Default 1.00. Above the max cap: 400 `cap_above_max`. Not a number, negative or infinite: 400 `invalid_cap`. |

The cap is fixed on a run's first call. Later `X-Run-Cap-USD` values for the same run are ignored. Use a new run id for each agent run.

## Responses

| Status | Body `error.type` | When |
|---|---|---|
| 402 | `run_budget_exceeded` | The run is over its cap, or this call's worst case would take it over |
| 402 | `global_budget_exceeded` | Total spend across all runs would pass the global cap |
| 400 | `missing_run_id` | No `X-Run-Id` |
| 400 | `unpriced_model` | The model is not in the price file |
| 400 | `cap_above_max`, `invalid_cap` | Bad `X-Run-Cap-USD` |
| 400 | `unsupported_part` | A message contains a `file` part |
| 400 | `invalid_request` | The body is not a valid JSON request |

Errors from OpenAI itself are passed through unchanged and are not charged.

## Defaults

From `capstop/cli.py`:

| Setting | Default | Flag |
|---|---|---|
| Host | `127.0.0.1` | `--host` (a non-loopback address also needs `--allow-remote`) |
| Port | 8787 | `--port` |
| Run cap | $1.00 | `--default-cap-usd` |
| Max cap a run can ask for | $10 | `--max-cap-usd` |
| Global cap, all runs together | $100 | `--global-cap-usd` |
| Default output tokens | 4096 | `--default-max-tokens` |
| Database | `capstop.db` in the working directory | `--db` |
| Price file | bundled `capstop/prices.json` | `--prices` |

There is no config file by default. `--config FILE` loads a JSON file with any of the keys `db`, `prices`, `default_cap_usd`, `default_max_tokens`, `host`, `port`, `max_cap_usd`, `global_cap_usd`. Flags override the file. Logs go to stderr only.

## Timeouts

Upstream connect timeout is 10 s. The read timeout, the longest wait for the next data from OpenAI, is 120 s for streamed calls and 600 s for non-streamed calls. There is no total timeout. None of these can be changed from the command line or config.

If the connection to OpenAI fails before the request is sent, the call costs $0. If the connection resets or times out after the request was sent, the call is charged at its worst-case estimate, because OpenAI may have billed it. A 200 response without usage is also charged at the worst case.

## Prices

`capstop/prices.json` holds USD prices per 1M tokens for gpt-4o, gpt-4o-mini, gpt-4.1, gpt-4.1-mini, gpt-4.1-nano, gpt-5, gpt-5-mini and gpt-5-nano. Its `_status` field says it was verified on 2026-10-29 against https://platform.openai.com/docs/pricing (Standard tier, text tokens). It also notes that cached input is billed at a lower rate by OpenAI, while Capstop charges all input at the full rate, which overestimates. Cached and reasoning tokens are not priced separately.

Prices change. Edit the file, or pass your own with `--prices`.

## Examples

These were run against `./run.sh fake`. All numbers below are computed from the fake server, not real OpenAI usage. The fake always reports 1,000 prompt and 500 completion tokens, which is $0.00045 for gpt-4o-mini at the bundled prices. The key is a placeholder.

A run with a $0.001 cap:

```
curl -s http://127.0.0.1:8787/v1/chat/completions \
  -H 'Authorization: Bearer sk-PLACEHOLDER' -H 'Content-Type: application/json' \
  -H 'X-Run-Id: demo-1' -H 'X-Run-Cap-USD: 0.001' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}],"max_tokens":100}'
```

```
{"id": "chatcmpl-fake", "created": 1793265900, "model": "gpt-4o-mini", "object": "chat.completion", "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}, "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello from the fake."}, "finish_reason": "stop"}]}
```

The first three calls on `demo-1` returned 200. Each one's estimate fit under the cap, but the actual usage was higher, so the third took the run past its cap. Its answer was still returned. The fourth call was refused without being forwarded:

```
HTTP 402
{"error": {"type": "run_budget_exceeded", "run_id": "demo-1", "spent_usd": 0.00135, "cap_usd": 0.001}}
```

Check a run's spend:

```
curl -s http://127.0.0.1:8787/runs/demo-1
{"run_id": "demo-1", "spent_usd": 0.00135, "cap_usd": 0.001, "calls": 3, "over_cap": true}
```

Or from the command line, with the venv `run.sh` created (`capstop runs` with no id lists every run):

```
.venv/bin/capstop runs demo-1
{
  "run_id": "demo-1",
  "spent_usd": 0.00135,
  "cap_usd": 0.001,
  "calls": 3,
  "over_cap": true
}
```

No run id:

```
HTTP 400
{"error": {"type": "missing_run_id"}}
```

A model with no price:

```
HTTP 400
{"error": {"type": "unpriced_model", "model": "gpt-3.5-turbo"}}
```

A cap above the max:

```
HTTP 400
{"error": {"type": "cap_above_max", "max_cap_usd": 10.0}}
```

A streamed call (`"stream": true`) returns the normal SSE chunks. The last ones are the usage chunk Capstop asked for, then `[DONE]`:

```
data: {"id": "chatcmpl-fake", "created": 1793265900, "model": "gpt-4o-mini", "object": "chat.completion.chunk", "choices": [], "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}}
data: [DONE]
```

## Limits

- Tested on Linux only. Windows is untested, and `run.sh` is bash. The `Dockerfile` is untested; it has not been built. The container binds 0.0.0.0 inside, so publish it on loopback only:
  `docker build -t capstop . && docker run -p 127.0.0.1:8787:8787 -v capstop-data:/data capstop` (untested).
- This is an early version. It handles OpenAI chat completion calls only.
- The prompt estimate counts one token per UTF-8 byte, with no tokenizer. It is meant to be on the safe side, but it can still overshoot on unusual prompts. A run can end up above its cap by what the calls in flight at that moment under-estimated.
- When a stream is cut at the cap, the spend recorded is the local estimate, and it can be one chunk over the cap.
- If a stream is reset mid-way, it looks like a normal end to the client. There is no error event and no `[DONE]`, so an agent may not notice the answer was cut short.
- Dependencies are pinned with hashes in `requirements.lock` only. There is no other hashed lock file.
- Capstop's numbers have not been checked against a real OpenAI bill. The live accuracy check against real OpenAI has not been run, so the prices and costs are unconfirmed against an invoice.
- A hung call holds the client for up to 600 s (non-streamed) or 120 s between chunks (streamed).
- One Capstop process per database. Several processes sharing one `capstop.db` are not supported.
- The global cap limits a caller that uses a new run id for every request. Below that, each new run id gets a fresh cap.
- Image and audio parts are counted as 4,000 tokens each. `file` parts are refused.

## License

Apache-2.0. See [LICENSE](LICENSE).
