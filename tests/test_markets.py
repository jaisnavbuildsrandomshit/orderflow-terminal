import asyncio
import json
from decimal import Decimal as D

from orderflow import charts as C
from orderflow import markets as M
from orderflow.state import MarketState
from tests.test_state import mk_trade

CHART = {"chart": {"result": [{
    "meta": {"symbol": "^NSEI", "currency": "INR", "exchangeName": "NSI", "regularMarketPrice": 25387.4,
             "chartPreviousClose": 25144.3, "regularMarketDayHigh": 25501.35, "regularMarketDayLow": 25267.3,
             "regularMarketVolume": 0, "regularMarketTime": 1794909667, "exchangeTimezoneName": "Asia/Kolkata",
             "gmtoffset": 19800,
             "currentTradingPeriod": {"regular": {"start": 1794887100, "end": 1794909600}}},
    "timestamp": [1794887100, 1794887160, 1794887220],
    "indicators": {"quote": [{"open": [25200, None, 25300], "high": [25250, None, 25350],
                              "low": [25190, None, 25280], "close": [25240, None, 25330],
                              "volume": [0, None, 0]}]}}], "error": None}}

SPARK = {"spark": {"result": [{"symbol": "AAPL", "response": [{
    "meta": {"symbol": "AAPL", "currency": "USD", "regularMarketPrice": 200.5, "chartPreviousClose": 198.0,
             "regularMarketDayHigh": 201, "regularMarketDayLow": 197.5, "regularMarketTime": 1794909667},
    "indicators": {"quote": [{"close": [198.2, None, 199.0, 200.5]}]}}]}]}}


def test_parse_chart_skips_null_bars_and_reads_session():
    ch = M.parse_chart(CHART)
    assert ch.name == "NIFTY 50" and ch.currency == "INR"
    assert len(ch.bars) == 2 and ch.bars[-1].c == 25330
    assert ch.market_open(1794887200) and not ch.market_open(1794909700)


def test_parse_chart_error():
    try:
        M.parse_chart({"chart": {"result": None, "error": {"description": "No data found"}}})
    except ValueError as e:
        assert "No data" in str(e)
    else:
        raise AssertionError


def test_parse_spark_and_quote_change():
    d = M.parse_spark(SPARK)["AAPL"]
    assert d["closes"] == [198.2, 199.0, 200.5]
    q = M.Quote("AAPL", "Apple", "US", "YAHOO", price=d["price"], ref=d["prev"])
    assert round(q.change, 2) == 2.5 and round(q.change_pct, 4) == round(2.5 / 198 * 100, 4)
    assert M.Quote("X", "X", "US", "YAHOO", price=1.0, ref=0.0).change_pct is None


def test_parse_mini_tickers_skips_bad_rows():
    raw = json.dumps({"stream": "!miniTicker@arr", "data": [
        {"e": "24hrMiniTicker", "E": 1700000000000, "s": "BTCUSDT", "c": "100", "o": "90",
         "h": "101", "l": "89", "v": "1", "q": "1000"},
        {"s": "ETHUSDT"}]})
    rows = M.parse_mini_tickers(raw)
    assert len(rows) == 1 and rows[0]["symbol"] == "BTCUSDT" and rows[0]["open"] == 90.0


def test_charts_dimensions_and_ascii_fallback():
    vals = [float(i % 7) for i in range(100)]
    lines = C.line_chart(vals, 20, 5, "#ffffff", ref=3.0)
    assert len(lines) == 5 and all(l.cell_len == 20 for l in lines)
    assert all(ord(ch) < 128 for l in C.line_chart(vals, 20, 5, "#ffffff", ascii_mode=True) for ch in l.plain)
    candles, lo, hi = C.candle_chart([(1, 3, 0.5, 2), (2, 2.5, 1, 1.2)] * 30, 25, 6, "#0f0", "#f00", ref=1.5)
    assert len(candles) == 6 and all(l.cell_len == 25 for l in candles) and lo == 0.5 and hi == 3
    bars = C.diverging_bars([1.0, -2.0, 0.0, 3.0], 10, 4, "#00ff00", "#ff0000")
    assert len(bars) == 4 and all(b.cell_len == 10 for b in bars)
    assert len(C.sparkline(list(range(50)), 12)) == 12
    assert C.resample([1, 2, 3, 4], 2) == [2, 4]


def test_ring_gauge_and_stretch():
    ring = C.ring_gauge(0.6, 14, "#00ff00", "#ff0000", "#335566")
    assert len(ring) == 8 and all(r.cell_len == 15 for r in ring)
    assert any("⠀" < ch <= "⣿" for r in ring for ch in r.plain)
    assert C.stretch([1, 2], 4) == [1, 1, 2, 2]


def test_flow_series_buckets_per_second():
    st = MarketState("BTCUSDT")
    st.apply_trade(mk_trade(1, "100", "1", m=False, T=10_000), 0)
    st.apply_trade(mk_trade(2, "100", "3", m=True, T=10_500), 0)
    st.apply_trade(mk_trade(3, "100", "1", m=False, T=12_000), 0)
    assert st.flow_series(4, 12) == [D(0), D(-200), D(0), D(100)]


def test_markets_and_equity_screens(monkeypatch, tmp_path):
    from orderflow import app as A
    monkeypatch.chdir(tmp_path)

    async def fake_chart(symbol):
        return M.parse_chart(CHART)
    monkeypatch.setattr(A, "fetch_equity_chart", fake_chart)

    async def scenario():
        app = A.OrderflowApp(connect=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.press("m")
            await pilot.pause(0.1)
            assert isinstance(app.screen, A.MarketsScreen)
            await pilot.press("slash")
            for ch in "nifty 50":
                await pilot.press("space" if ch == " " else ch)
            assert app.screen.filter == "nifty 50"
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert isinstance(app.screen, A.EquityScreen) and app.screen.symbol == "^NSEI"
            assert app.screen.chart is not None
            await pilot.press("escape")
            await pilot.pause(0.1)
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert app.screen is app.screen_stack[0]
            await pilot.press("m")
            await pilot.pause(0.1)
            app.screen.filter = "doge"
            app.screen.cursor = 1
            app.screen.action_open()
            await pilot.pause(0.3)
            assert app.state.symbol == "DOGEUSDT"
            await pilot.press("q")

    asyncio.run(scenario())
