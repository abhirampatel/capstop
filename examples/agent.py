"""A runaway agent loop that Capstop stops. Run against `./run.sh fake` (no real key needed)."""

import argparse
import json
import os
import time
import urllib.request

import openai

ap = argparse.ArgumentParser()
ap.add_argument("--base-url", default=os.environ.get("CAPSTOP_BASE_URL", "http://127.0.0.1:8787/v1"))
ap.add_argument("--cap", default=os.environ.get("CAPSTOP_CAP_USD", "0.005"))
ap.add_argument("--model", default="gpt-4o-mini")
args = ap.parse_args()

run_id = f"runaway-{time.strftime('%Y%m%d-%H%M%S')}"
client = openai.OpenAI(
    base_url=args.base_url,
    api_key="sk-fake",
    max_retries=0,
    default_headers={"X-Run-Id": run_id, "X-Run-Cap-USD": args.cap},
)
root = args.base_url.rstrip("/").removesuffix("/v1")


def spent() -> float:
    with urllib.request.urlopen(f"{root}/runs/{run_id}") as r:
        return json.load(r)["spent_usd"]


print(f"run {run_id}, cap ${args.cap}")
call = 0
while True:
    call += 1
    try:
        client.chat.completions.create(
            model=args.model,
            messages=[{"role": "user", "content": f"Step {call}: keep going."}],
            max_tokens=500,
        )
    except openai.APIStatusError as e:
        err = (e.body or {}).get("error", e.body) if isinstance(e.body, dict) else e.body
        if e.status_code == 402:
            print(
                f"call {call:>3}  STOPPED: {err['type']} (spent ${err['spent_usd']:.6f} of ${err['cap_usd']})"
            )
            break
        raise
    print(f"call {call:>3}  ok   spent ${spent():.6f}")
