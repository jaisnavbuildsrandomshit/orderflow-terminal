"""Headless UI smoke tests (network disabled; status shows OFFLINE, never LIVE)."""
import asyncio
import csv

from orderflow.app import OrderflowApp
from orderflow.feed import Status
from tests.test_state import depth_msg, mk_trade
from orderflow.state import parse_message


def run(coro):
    return asyncio.run(coro)


def test_app_starts_renders_and_isolates_symbols(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def scenario():
        app = OrderflowApp(connect=False)
        async with app.run_test(size=(140, 42)) as pilot:
            assert app.display_status(0) == "OFFLINE"
            old_gen = app.gen
            app._on_event(old_gen, parse_message(depth_msg()))
            app._on_event(old_gen, mk_trade(1))
            await pilot.pause(0.2)
            assert app.state.midprice() is not None
            await pilot.press("e")
            await pilot.pause(0.5)
            await pilot.press("2")
            await pilot.pause(0.1)
            assert app.state.symbol == "ETHUSDT" and not app.state.trades and not app.state.bids
            # a late message from the old BTC feed generation must be ignored
            app._on_event(old_gen, mk_trade(2))
            app._on_status(old_gen, Status.LIVE, "x")
            assert not app.state.trades and app.feed_status != Status.LIVE
            await pilot.press("p")
            assert app.paused_at is not None
            await pilot.press("p")
            assert app.paused_at is None
            await pilot.press("question_mark")
            await pilot.pause(0.1)
            await pilot.press("escape")
            await pilot.resize_terminal(100, 30)
            await pilot.pause(0.2)
            assert app.screen.has_class("narrow")
            await pilot.press("q")

    run(scenario())
    files = list((tmp_path / "exports").glob("orderflow_BTCUSDT_*.csv"))
    assert len(files) == 1
    rows = list(csv.reader(files[0].open(encoding="utf-8")))
    assert rows[0][0] == "trade_id" and rows[1][7] == "BUY"
