# Run Capstop

Needs Python >= 3.11 (`pyproject.toml`) and bash. Tested on Linux only (box: Python 3.13.5).
**Windows: UNTESTED and not supported by these scripts.** `run.sh` is bash; no `.ps1` exists.

## One command
```
git clone https://github.com/abhirampatel/capstop
cd capstop
./run.sh setup    # only build/refresh .venv
./run.sh fake     # no-key demo: fake upstream on 127.0.0.1:9999 + Capstop on 127.0.0.1:8787
./run.sh          # real: capstop serve --upstream ${CAPSTOP_UPSTREAM:-https://api.openai.com} on 127.0.0.1:8787
```
`run.sh` creates `.venv`, installs `requirements.lock` with `pip --require-hashes`, then the project with `--no-deps`.
Re-runs skip the install unless the lock or code changed. Extra args go to `capstop serve`, e.g. `./run.sh fake --port 8800`.
Stop with Ctrl-C (fake mode also stops the fake upstream).

## Defaults (from `capstop/cli.py` DEFAULTS)
- Port **8787**, host **127.0.0.1**. Non-loopback bind needs `--allow-remote`.
- DB `./capstop.db` in the working directory (`--db` to change). Default cap $1.00, max cap $10, global cap $100.
- `capstop fake` listens on **9999**.

## Headers (`capstop/proxy.py`)
- `X-Run-Id` (required): missing -> **400** `{"error":{"type":"missing_run_id"}}`, not forwarded. Truncated to 200 chars.
- `X-Run-Cap-USD` (optional, default 1.0): not a number, negative or non-finite -> 400 `invalid_cap`;
  above `max_cap_usd` -> 400 `cap_above_max`. The cap is stored on the run's **first** call; later values are ignored for that run.
- Over cap -> **402** `run_budget_exceeded`. Global cap -> 402 `global_budget_exceeded`.
- Your provider key goes in `Authorization: Bearer ...`; Capstop forwards it and does not store it.

## Upstream timeouts (`capstop/proxy.py` make_app, `sock_read=(120, 600)`)
connect 10 s; read **120 s** between chunks for streamed requests, **600 s** for non-streamed. No total timeout.
A timeout after connect charges the worst-case estimate.

## DB file mode (`capstop/ledger.py`)
The DB and its -wal/-shm are set to 0600 on start.

## Example against the fake (placeholder key)
```
curl -s http://127.0.0.1:8787/v1/chat/completions \
  -H 'Authorization: Bearer sk-PLACEHOLDER' -H 'Content-Type: application/json' \
  -H 'X-Run-Id: demo-1' -H 'X-Run-Cap-USD: 0.05' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}],"max_tokens":100}'
curl -s http://127.0.0.1:8787/runs/demo-1
```

## Docker (UNTESTED: no Docker on the build box)
```
docker build -t capstop .
docker run --rm -p 127.0.0.1:8787:8787 -v capstop-data:/data capstop
```
Non-root user, base `python:3.13.5-slim-bookworm`, no secrets in the image.

## Regenerate the lock
`uv pip compile requirements.in --universal --python-version 3.11 --generate-hashes -o requirements.lock`
