import argparse
import json
import logging
import os
from importlib.resources import files
from urllib.parse import urlparse

from aiohttp import web

from .ledger import Ledger
from .proxy import load_prices, make_app

DEFAULTS = {
    "db": "capstop.db",
    "prices": str(files("capstop") / "prices.json"),
    "default_cap_usd": 1.0,
    "default_max_tokens": 4096,
    "host": "127.0.0.1",
    "port": 8787,
    "max_cap_usd": 10.0,
    "global_cap_usd": 100.0,
}
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def config(args) -> dict:
    cfg = dict(DEFAULTS)
    if args.config:
        with open(args.config) as f:
            cfg.update(json.load(f))
    cfg.update({k: v for k, v in vars(args).items() if v is not None and k in DEFAULTS})
    return cfg


def main(argv=None):
    ap = argparse.ArgumentParser(prog="capstop")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "runs", "fake"):
        p = sub.add_parser(name)
        p.add_argument("--config")
        p.add_argument("--db")
        p.add_argument("--port", type=int)
        p.add_argument("--host")
    s = sub.choices["serve"]
    s.add_argument("--upstream", required=True)
    s.add_argument("--prices")
    s.add_argument("--default-cap-usd", type=float)
    s.add_argument("--default-max-tokens", type=int)
    s.add_argument("--max-cap-usd", type=float)
    s.add_argument("--global-cap-usd", type=float)
    s.add_argument("--allow-remote", action="store_true")
    sub.choices["runs"].add_argument("run_id", nargs="?")
    sub.choices["fake"].add_argument("--prompt-tokens", type=int, default=1000)
    sub.choices["fake"].add_argument("--completion-tokens", type=int, default=500)
    sub.choices["fake"].add_argument("--stream-tokens", type=int)
    sub.choices["fake"].add_argument("--chunk-delay", type=float, default=0.0, help="seconds")
    args = ap.parse_args(argv)
    cfg = config(args)

    if args.cmd == "runs":
        if not os.path.exists(cfg["db"]):
            raise SystemExit(f"no ledger at {cfg['db']}")
        led = Ledger(cfg["db"])
        rows = [led.get(args.run_id)] if args.run_id else led.all()
        if args.run_id and rows[0] is None:
            raise SystemExit(f"run not found: {args.run_id}")
        print(json.dumps(rows[0] if args.run_id else rows, indent=2))
        return
    if args.cmd == "fake":
        from .fake import Fake

        fake = Fake(
            args.prompt_tokens,
            args.completion_tokens,
            stream_tokens=args.stream_tokens,
            chunk_delay=args.chunk_delay,
        )
        web.run_app(fake.app(), host=cfg["host"], port=args.port or 9999, print=None)
        return
    up = urlparse(args.upstream)
    if up.scheme != "https" and up.hostname not in LOOPBACK:
        raise SystemExit("capstop: --upstream must be https unless it is 127.0.0.1/localhost")
    if cfg["host"] not in LOOPBACK and not args.allow_remote:
        raise SystemExit(f"capstop: refusing non-loopback bind {cfg['host']} without --allow-remote")
    try:
        prices = load_prices(cfg["prices"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ArithmeticError) as e:
        raise SystemExit(
            f"capstop: cannot load prices file {cfg['prices']}: {type(e).__name__}: {e}"
        ) from None
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.server").setLevel(logging.CRITICAL)
    app = make_app(
        args.upstream,
        Ledger(cfg["db"]),
        prices,
        cfg["default_cap_usd"],
        cfg["default_max_tokens"],
        cfg["max_cap_usd"],
        cfg["global_cap_usd"],
        frozenset(LOOPBACK | {cfg["host"]}),
    )
    if cfg["host"] not in LOOPBACK:
        logging.getLogger("capstop").warning("binding non-loopback %s: /runs is unauthenticated", cfg["host"])
    logging.getLogger("capstop").info("listening on %s:%s -> %s", cfg["host"], cfg["port"], args.upstream)
    web.run_app(app, host=cfg["host"], port=cfg["port"], print=None)


if __name__ == "__main__":
    main()
