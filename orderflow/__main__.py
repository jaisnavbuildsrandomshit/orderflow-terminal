from __future__ import annotations

import argparse
import logging
import sys
from decimal import Decimal, InvalidOperation
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .state import SYMBOLS


def _decimal(s: str) -> Decimal:
    try:
        v = Decimal(s.replace(",", "").replace("_", ""))
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"not a number: {s}") from None
    if v <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return v


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="orderflow", description="Market microstructure terminal (Binance Spot public data)")
    ap.add_argument("--symbol", default="BTCUSDT", type=str.upper, choices=list(SYMBOLS))
    ap.add_argument("--large", type=_decimal, default=None,
                    help="large-trade alert threshold in quote currency (default per symbol)")
    ap.add_argument("--ascii", action="store_true", help="ASCII-only charts and bars")
    ap.add_argument("--endpoint", action="append", help="override WebSocket base URL (repeatable)")
    ap.add_argument("--log", default="logs/orderflow.log", help="diagnostic log file")
    args = ap.parse_args(argv)

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    ascii_mode = args.ascii or "utf" not in enc

    from .app import OrderflowApp
    from .feed import ENDPOINTS

    app = OrderflowApp(args.symbol, args.large, ascii_mode, tuple(args.endpoint or ENDPOINTS))
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
