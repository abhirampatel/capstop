# Capstop

A per-run dollar cap for AI agents that call OpenAI.

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)

Capstop is a small local proxy. It sits between your agent and OpenAI. Give each agent run an id and a dollar cap. When the run reaches its cap, the next call gets HTTP 402 and is never sent to OpenAI.

Project site: https://capstop.pages.dev

## Why

An agent loop or a retry storm can burn through an API budget before anyone notices. OpenAI's spending limits are monthly and are not enforced instantly.

Capstop checks every call against its run's cap before forwarding it. A run that hits its cap stops on the next call, not at the end of the month.

## Quick start

You need Linux, bash and Python 3.11 or newer with `venv` (on Debian or Ubuntu: `apt install python3-venv`).

### Try it without a key

```
git clone https://github.com/abhirampatel/capstop
cd capstop
./run.sh setup
./run.sh fake
```

`./run.sh fake` starts a fake OpenAI server on `127.0.0.1:9999` and Capstop on `127.0.0.1:8787`. Leave it running. The first run takes a few seconds to build `.venv`. No real calls are made.

In a second terminal, in the same folder, run the example agent. It is a loop that keeps calling `gpt-4o-mini` with a $0.005 cap on its run (`--cap` changes it). It needs the `openai` package. Most Linux systems block a plain `pip install`, so use a throwaway venv:

```
python3 -m venv /tmp/capstop-example
/tmp/capstop-example/bin/pip install openai
/tmp/capstop-example/bin/python3 examples/agent.py
```

Output. The numbers are computed from the fake server, which reports 1,000 prompt and 500 completion tokens per call ($0.00045 at the bundled prices):

```
run runaway-20261029-182028, cap $0.005
call   1  ok   spent $0.000450
call   2  ok   spent $0.000900
call   3  ok   spent $0.001350
call   4  ok   spent $0.001800
call   5  ok   spent $0.002250
call   6  ok   spent $0.002700
call   7  ok   spent $0.003150
call   8  ok   spent $0.003600
call   9  ok   spent $0.004050
call  10  ok   spent $0.004500
call  11  ok   spent $0.004950
call  12  STOPPED: run_budget_exceeded (spent $0.004950 of $0.005)
```

Call 12 would have gone over the cap, so Capstop refused it with HTTP 402 and the loop ended. Stop the demo with Ctrl-C in the first terminal.

### Use it with real OpenAI

Stop the demo first; both modes use port 8787. Then:

```
./run.sh
```

This forwards to `https://api.openai.com`. Put your own API key in your client, not in Capstop. Point the client at Capstop and name the run:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8787/v1",
    default_headers={"X-Run-Id": "agent-run-1", "X-Run-Cap-USD": "0.50"},
)
```

`setup` is optional; the other modes install on first run. Stop with Ctrl-C. Extra arguments go to `capstop serve`, for example `./run.sh --port 8800`.

## Repository layout

| Path | What it is |
|---|---|
| `run.sh` | Setup and run script |
| `capstop/` | The proxy (`cli.py`, `proxy.py`, `ledger.py`, `fake.py`, `prices.json`) |
| `examples/agent.py` | Runaway-agent demo for `./run.sh fake` |
| `tests/` | Test suite |
| `RUN.md`, `USER-GUIDE.md`, `SECURITY.md` | More docs |

## Usage

Every call needs an `X-Run-Id` header. `X-Run-Cap-USD` sets that run's cap.

| Header | Required | Meaning |
|---|---|---|
| `X-Run-Id` | Yes | Names the run. Without it: 400 `missing_run_id`, nothing forwarded. Cut to 200 characters. |
| `X-Run-Cap-USD` | No | The run's cap in US dollars. Default 1.00, max 10. Above the max: 400 `cap_above_max`. Not a number, negative or infinite: 400 `invalid_cap`. |

The cap is fixed on a run's first call. Later values for the same run are ignored. Use a new run id for each agent run.

Example against `./run.sh fake`, with a placeholder key. All numbers are computed from the fake server, not real OpenAI usage. The fake always reports 1,000 prompt and 500 completion tokens, which is $0.00045 per call for gpt-4o-mini at the bundled prices.

```
curl -s http://127.0.0.1:8787/v1/chat/completions \
  -H 'Authorization: Bearer sk-PLACEHOLDER' -H 'Content-Type: application/json' \
  -H 'X-Run-Id: agent-run-1' -H 'X-Run-Cap-USD: 0.001' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}],"max_tokens":100}'
```

Calls 1 to 3 return 200:

```
{"id": "chatcmpl-fake", "created": 1793273275, "model": "gpt-4o-mini", "object": "chat.completion", "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}, "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello from the fake."}, "finish_reason": "stop"}]}
```

Each call's estimate fit under the cap, but actual usage was higher. The third call took the run past its cap, and its answer was still returned. Call 4 is refused and not forwarded:

```
HTTP 402
{"error": {"type": "run_budget_exceeded", "run_id": "agent-run-1", "spent_usd": 0.00135, "cap_usd": 0.001}}
```

Check what a run spent:

```
$ curl -s http://127.0.0.1:8787/runs/agent-run-1
{"run_id": "agent-run-1", "spent_usd": 0.00135, "cap_usd": 0.001, "calls": 3, "over_cap": true}
```

`.venv/bin/capstop runs` lists every run. `.venv/bin/capstop runs <id>` shows one.

Other responses:

| Status | `error.type` | When |
|---|---|---|
| 402 | `run_budget_exceeded` | The run is over its cap, or this call's worst case would take it over |
| 402 | `global_budget_exceeded` | Total spend across all runs would pass the global cap |
| 400 | `missing_run_id` | No `X-Run-Id` |
| 400 | `unpriced_model` | The model is not in the price file |
| 400 | `cap_above_max`, `invalid_cap` | Bad `X-Run-Cap-USD` |
| 400 | `unsupported_part` | A message contains a `file` part |
| 400 | `invalid_request` | The body is not a valid JSON request |

Errors from OpenAI are passed through unchanged and are not charged. Streaming works too. Capstop asks OpenAI for a final usage chunk and forwards it to the client.

## How it works

- Only `POST /v1/chat/completions` is proxied, streamed or not. Other paths return 404.
- Before forwarding, Capstop estimates the worst-case cost: prompt size plus `max_completion_tokens`, `max_tokens` or 4096 output tokens, priced from `capstop/prices.json`. If spent plus the estimate would pass the cap, it returns 402.
- It forwards that output limit as `max_completion_tokens`, so the reply can't be longer than the estimate allows.
- After the reply, it records the actual cost from OpenAI's `usage`. If that puts the run over its cap, the reply is still returned, and later calls get 402.
- On a stream, it cuts the stream if the running output estimate would pass the cap.
- A reset or timeout after the request was sent is charged at the worst case. A failure before sending costs $0.
- A model missing from the price file is refused.
- Spend is kept in SQLite (`capstop.db`), so it survives restarts.

## Configuration

Flags for `capstop serve` (pass them after the `run.sh` mode). Defaults come from `capstop/cli.py`.

| Setting | Default | Flag |
|---|---|---|
| Host | `127.0.0.1` | `--host` (non-loopback also needs `--allow-remote`) |
| Port | 8787 | `--port` |
| Run cap | $1.00 | `--default-cap-usd` |
| Max cap a run can ask for | $10 | `--max-cap-usd` |
| Global cap, all runs | $100 | `--global-cap-usd` |
| Default output tokens | 4096 | `--default-max-tokens` |
| Database | `capstop.db` in the working directory | `--db` |
| Price file | bundled `capstop/prices.json` | `--prices` |
| Upstream connect timeout | 10 s | not configurable |
| Upstream read timeout, streamed | 120 s between chunks | not configurable |
| Upstream read timeout, non-streamed | 600 s | not configurable |

`--config FILE` loads a JSON file with any of `db`, `prices`, `default_cap_usd`, `default_max_tokens`, `host`, `port`, `max_cap_usd`, `global_cap_usd`. Flags override it. Logs go to stderr.

Environment variables read by `run.sh`:

| Variable | Default | Use |
|---|---|---|
| `CAPSTOP_UPSTREAM` | `https://api.openai.com` | Where `./run.sh` forwards to |
| `CAPSTOP_FAKE_PORT` | `9999` | Fake server port in fake mode |
| `CAPSTOP_VENV` | `.venv` in the repo folder | Virtual environment path |
| `PYTHON` | `python3` | Python used to create the venv |

`.env.example` lists them. `run.sh` does not load it on its own.

### Prices

`capstop/prices.json` has USD per 1M tokens for gpt-4o, gpt-4o-mini, gpt-4.1, gpt-4.1-mini, gpt-4.1-nano, gpt-5, gpt-5-mini and gpt-5-nano. Its `_status` field says it was verified on 2026-10-29 against https://platform.openai.com/docs/pricing (Standard tier, text tokens). All input is charged at the full input rate, so cached input is overestimated. Reasoning tokens are not priced separately. Prices change: edit the file or pass `--prices`.

## Limitations

- Early stage.
- Tested on Linux only. macOS and Windows are untested. `run.sh` is bash.
- The `Dockerfile` is untested. If you try it, publish on loopback only: `docker run -p 127.0.0.1:8787:8787 ...`.
- OpenAI chat completion calls only.
- Not checked against a real OpenAI bill. The live accuracy check against real OpenAI has not been run.
- The prompt estimate counts one token per UTF-8 byte, with no tokenizer. It can still be off on unusual prompts. A run can end above its cap by what the calls in flight under-estimated.
- A stream cut at the cap is charged the local estimate and can end one chunk over the cap.
- A mid-stream reset looks like a normal end to the client: no error event and no `[DONE]`.
- Timeouts are not configurable. A hung call holds the client for up to 600 s (non-streamed) or 120 s between chunks (streamed).
- One Capstop process per database.
- Each new run id gets a fresh cap. Only the global cap limits a caller that rotates run ids.
- Image and audio parts count as 4,000 tokens each. `file` parts are refused.
- Dependencies are hash-pinned in `requirements.lock` only.

## Security

Your API key goes only to OpenAI. It's never stored or logged. (If you set `CAPSTOP_UPSTREAM`, it goes to that address instead.)

Capstop has no caller authentication. Anyone who can reach the port can use it, so keep it on `127.0.0.1`. See [SECURITY.md](SECURITY.md) for the threat model and how to report a vulnerability.

## License

Apache-2.0. See [LICENSE](LICENSE).
