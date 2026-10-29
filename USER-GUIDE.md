# Using Capstop

Capstop stops an agent run once it has spent a set amount on OpenAI. This guide covers the common tasks. See `README.md` for details.

## Try it without a key

```
cd backend
./run.sh fake
```

This starts a fake OpenAI server and Capstop on `http://127.0.0.1:8787`. No real calls are made and nothing is charged. Stop it with Ctrl-C.

## Use it with OpenAI

1. Start it:

   ```
   cd backend
   ./run.sh
   ```

2. In your agent, set the OpenAI client's base URL to `http://127.0.0.1:8787/v1`. Keep using your own API key as usual. Capstop passes it on and does not store it.
3. Send `X-Run-Id` on every call. Use a new id for each agent run.
4. Optionally send `X-Run-Cap-USD` to set that run's cap. Without it the cap is $1.00. The most a run can ask for is $10.

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8787/v1",
    default_headers={"X-Run-Id": "nightly-42", "X-Run-Cap-USD": "0.50"},
)
```

Only chat completions go through Capstop.

## Handle the cap in your agent

When a run hits its cap, the next call gets HTTP 402 with `error.type` set to `run_budget_exceeded`. With the `openai` Python client this raises an `APIStatusError` with `status_code == 402`. Catch it and stop the run.

The call that crosses the cap still returns its answer. The refusal comes on the next call.

A run's cap is set by its first call and cannot be raised later. To continue, start a new run id.

## See what a run spent

```
curl -s http://127.0.0.1:8787/runs/nightly-42
```

Or, from `backend/`, `.venv/bin/capstop runs` lists every run.

## Change the defaults

Pass flags after the mode, for example:

```
./run.sh --port 8800 --default-cap-usd 0.25 --global-cap-usd 20
```

The global cap ($100 by default) limits the total across all runs.

## Update prices

Prices are in `capstop/prices.json`, in USD per 1M tokens. They were checked against OpenAI's pricing page on 2026-10-29. Edit the file if prices change, or pass your own with `--prices`. A model missing from the file is refused.

## Keep the database private

Spend is kept in `capstop.db` in the folder you start from. Capstop makes it private to you (0600) each time it starts.
