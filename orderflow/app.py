"""Textual UI. Ingestion runs as asyncio tasks; rendering runs on fixed timers
(~8 Hz for order flow), so message bursts never drive redraws."""
from __future__ import annotations

import asyncio
import contextlib
import csv
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Input, Static

from . import charts as C
from . import render as R
from .feed import ENDPOINTS, Feed, Status
from .markets import CHART_POLL_S, EQUITY_BY_SYMBOL, MarketBoard, fetch_equity_chart
from .state import DEPTH_LEVELS, SYMBOLS, BookSnapshot, MarketState, Trade

log = logging.getLogger("orderflow.app")

REFRESH_S = 0.125
STALE_S = 5.0
THRESHOLDS = [Decimal(x) for x in (10_000, 25_000, 50_000, 100_000, 250_000,
                                   500_000, 1_000_000, 2_500_000, 5_000_000)]
QUICK = {"1": "BTCUSDT", "2": "ETHUSDT", "3": "SOLUSDT"}
STATUS_COLORS = {"LIVE": R.GREEN, "STALE": R.AMBER, "CONNECTING": R.CYAN, "RECONNECTING": R.AMBER,
                 "OFFLINE": R.RED, "STOPPED": R.DIM, "DELAYED": R.AMBER, "LOADING": R.CYAN,
                 "RETRYING": R.AMBER, "ERROR": R.RED}

HELP = f"""\
[b {R.CYAN}]ORDERFLOW - metric definitions[/]

[b]Crypto data[/]  Binance Spot public WebSockets, real time: <sym>@trade and
<sym>@depth20@100ms. Each depth message REPLACES the displayed top-20 book;
it is not the full exchange order book.
[b]Stocks/indices[/]  Yahoo Finance chart endpoints (unofficial, polled, typically
delayed ~15 min for NSE/BSE). Price/OHLC only: no order book or trade tape.

[b]midprice[/]      (best ask + best bid) / 2
[b]spread[/]        best ask - best bid;   [b]spread bps[/] = spread / mid x 10,000
[b]VWAP[/]          sum(price x qty) / sum(qty) since connection / symbol reset.
[b]imbalance t20[/] (sum bid qty - sum ask qty) / (sum bid qty + sum ask qty), displayed
              top 20 levels only. Descriptive, not predictive.
[b]flow 60s[/]      buyer-initiated minus seller-initiated quote notional, last 60s.
              Binance field m (buyer is maker): true = SELL aggressor, false = BUY.
[b]net flow / s[/]  per-second signed notional histogram (green buy, red sell).
[b]recv age[/]      seconds since the last message was received locally (not latency).

[b]Keys[/]  M markets (/ filter, Enter open)   1/2/3 BTC/ETH/SOL   P pause   E export CSV
      [ / ] large-trade threshold   ? help   Q quit

[{R.DIM}]press any key to close[/]"""


def header_line(width: int, source: str, symbol: str, name: str, status: str,
                extra: list[tuple[str, str]] | None = None) -> Text:
    t = Text(no_wrap=True, overflow="crop")
    t.append(" ORDERFLOW ", style=f"bold {R.BG} on {R.CYAN}")
    t.append(f" {source} ", style=f"bold {R.CYAN} on {C.mix(R.CYAN, 0.18)}")
    t.append("  ")
    t.append(f" {symbol} ", style=f"bold {R.BG} on {R.AMBER}")
    if name:
        t.append(f" {name}", style=R.FG)
    t.append("  ")
    t.append(f" {status} ", style=f"bold {R.BG} on {STATUS_COLORS.get(status, R.FG)}")
    for txt, style in extra or []:
        t.append(txt, style=style)
    clock = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC ")
    pad = width - t.cell_len - len(clock)
    if pad > 0:
        t.append(" " * pad)
        t.append(clock, style=f"bold {R.CYAN}")
    return t


def key_hints(pairs) -> Text:
    t = Text(no_wrap=True, overflow="crop")
    for k, v in pairs:
        t.append(f" {k} ", style=f"bold {R.BG} on {C.mix(R.CYAN, 0.75)}")
        t.append(f" {v}  ", style=R.MUTED)
    return t


class HelpScreen(ModalScreen):
    def compose(self) -> ComposeResult:
        yield Static(HELP, id="help")

    def on_key(self, event) -> None:
        event.stop()
        self.dismiss()


def write_csv(path: Path, trades: list[Trade]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    iso = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds")
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["trade_id", "trade_time_utc", "recv_time_utc", "symbol", "price",
                    "qty", "quote_notional", "aggressor"])
        for t in trades:
            w.writerow([t.trade_id, iso(t.trade_time_ms), iso(t.recv_wall_ms) if t.recv_wall_ms else "",
                        t.symbol, str(t.price), str(t.qty), str(t.notional), t.side])
    return len(trades)


# ====================================================================== markets
class MarketsScreen(Screen):
    AUTO_FOCUS = None  # keep keys for navigation until "/" focuses the filter
    BINDINGS = [
        Binding("up,k", "move(-1)", show=False), Binding("down,j", "move(1)", show=False),
        Binding("pageup", "move(-10)", show=False), Binding("pagedown", "move(10)", show=False),
        Binding("home", "move(-999)", show=False), Binding("end", "move(999)", show=False),
        Binding("enter", "open", show=False), Binding("slash", "filter", show=False),
        Binding("escape,m", "back", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.cursor = 1
        self.filter = ""

    def compose(self) -> ComposeResult:
        yield Static(id="mk-header", classes="bar")
        yield Input(placeholder="/ filter by symbol or name (e.g. nifty, reliance, sol)", id="filter")
        yield Static(id="watch", classes="panel")
        yield Static(id="mk-footer", classes="bar")

    def on_mount(self) -> None:
        w = self.query_one("#watch")
        w.border_title = "MARKETS"
        w.border_subtitle = "crypto real-time · stocks & indices delayed (Yahoo)"
        self.set_interval(0.5, self.refresh_view)
        self.refresh_view()

    def on_screen_resume(self) -> None:
        self.call_after_refresh(self.set_focus, None)  # Textual restores prior focus after resume

    @property
    def rows(self):
        return R.watch_rows(self.app.board, self.filter)

    def _clamp(self, rows) -> None:
        quotes = [i for i, r in enumerate(rows) if r[0] == "quote"]
        if not quotes:
            self.cursor = 0
            return
        if self.cursor not in quotes:
            self.cursor = min(quotes, key=lambda i: abs(i - self.cursor))

    def refresh_view(self) -> None:
        app: OrderflowApp = self.app
        b = app.board
        rows = self.rows
        self._clamp(rows)
        status = "LIVE" if b.crypto_status == "LIVE" else b.crypto_status
        extra = [("  stocks ", R.DIM), (f" {b.yahoo_status} ", f"bold {R.BG} on {STATUS_COLORS.get(b.yahoo_status, R.FG)}")]
        if b.yahoo_error:
            extra.append((f" {b.yahoo_error}", R.RED))
        self.query_one("#mk-header", Static).update(
            header_line(self.size.width, "MARKETS", f"{len(b.quotes)} instruments", "", status, extra))
        w = self.query_one("#watch", Static)
        cs = w.content_size
        w.update(R.watchlist_text(b, rows, self.cursor, cs.width, cs.height, app.ascii_mode,
                                  time.monotonic(), app.state.symbol))
        self.query_one("#mk-footer", Static).update(key_hints(
            [("↑↓", "move"), ("Enter", "open"), ("/", "filter"), ("Esc", "back to order flow"),
             ("?", "help"), ("Q", "quit")]))

    def action_move(self, d: int) -> None:
        rows = self.rows
        quotes = [i for i, r in enumerate(rows) if r[0] == "quote"]
        if not quotes:
            return
        self._clamp(rows)
        pos = quotes.index(self.cursor) if self.cursor in quotes else 0
        self.cursor = quotes[max(0, min(len(quotes) - 1, pos + d))]
        self.refresh_view()

    def action_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.value.startswith("/"):  # the "/" that focused the box can leak in
            event.input.value = event.value.lstrip("/")
            return
        self.filter = event.value.strip()
        self.cursor = 1
        self.refresh_view()

    def on_input_submitted(self, _event) -> None:
        self.set_focus(None)
        self.action_open()

    def on_key(self, event) -> None:
        if event.key == "escape" and isinstance(self.focused, Input):
            self.set_focus(None)
            event.stop()

    def action_open(self) -> None:
        rows = self.rows
        self._clamp(rows)
        if not rows or rows[self.cursor][0] != "quote":
            return
        q = rows[self.cursor][1]
        if q.source == "BINANCE":
            self.app.pop_screen()
            self.app.run_worker(self.app.switch_symbol(q.symbol))
        else:
            self.app.push_screen(EquityScreen(q.symbol))

    def action_back(self) -> None:
        self.app.pop_screen()


# ====================================================================== equity
class EquityScreen(Screen):
    BINDINGS = [Binding("escape,backspace", "back", show=False), Binding("m", "back", show=False),
                Binding("r", "reload", show=False)]

    def __init__(self, symbol: str) -> None:
        super().__init__()
        self.symbol = symbol
        self.chart = None
        self.err = ""
        self.loading = True
        self._wake = asyncio.Event()

    def compose(self) -> ComposeResult:
        yield Static(id="eq-header", classes="bar")
        yield Static(id="eq-metrics")
        yield Static(id="eq-chart", classes="panel")
        yield Static(id="eq-info", classes="panel")
        yield Static(id="eq-footer", classes="bar")

    def on_mount(self) -> None:
        name = EQUITY_BY_SYMBOL[self.symbol].name if self.symbol in EQUITY_BY_SYMBOL else self.symbol
        self.query_one("#eq-chart").border_title = f"{name}  INTRADAY"
        self.query_one("#eq-info").border_title = "SESSION"
        self.run_worker(self._poll(), exclusive=True, group="equity")  # cancelled with the screen
        self.set_interval(0.5, self.refresh_view)
        self.refresh_view()

    async def _poll(self) -> None:
        while True:
            try:
                self.chart = await fetch_equity_chart(self.symbol)
                self.err = ""
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.err = str(e)[:140]
                log.warning("equity %s: %s", self.symbol, self.err)
            self.loading = False
            self.refresh_view()
            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), CHART_POLL_S)

    def refresh_view(self) -> None:
        now_s = time.time()
        ch = self.chart
        status = "LOADING" if self.loading else ("ERROR" if ch is None else "DELAYED")
        name = EQUITY_BY_SYMBOL[self.symbol].name if self.symbol in EQUITY_BY_SYMBOL else ""
        extra = [("  Yahoo Finance (unofficial), polled every 15s", R.DIM)]
        self.query_one("#eq-header", Static).update(
            header_line(self.size.width, "EQUITY", self.symbol, name, status, extra))
        self.query_one("#eq-metrics", Static).update(R.equity_metrics_text(ch, now_s))
        c = self.query_one("#eq-chart", Static)
        cs = c.content_size
        c.update(R.equity_chart_text(ch, cs.width, cs.height, self.app.ascii_mode))
        i = self.query_one("#eq-info", Static)
        i.update(R.equity_info_text(ch, now_s, self.err, i.content_size.width, self.app.ascii_mode))
        self.query_one("#eq-footer", Static).update(key_hints(
            [("Esc", "back to markets"), ("R", "refresh now"), ("?", "help"), ("Q", "quit")]))

    def action_reload(self) -> None:
        self._wake.set()

    def action_back(self) -> None:
        self.app.pop_screen()


# ====================================================================== app
class OrderflowApp(App):
    TITLE = "ORDERFLOW"
    CSS = f"""
    Screen {{ background: {R.BG}; color: {R.FG}; }}
    Static {{ text-wrap: nowrap; text-overflow: clip; }}
    .bar {{ height: 1; background: #10151b; }}
    #header {{ height: 1; background: #10151b; }}
    #metrics, #eq-metrics {{ height: 3; border: round {C.mix(R.CYAN, 0.35)}; padding: 0 1; }}
    #main {{ height: 1fr; }}
    .panel {{ border: round #26313c; border-title-style: bold; border-subtitle-color: {R.DIM}; }}
    #book {{ width: 46; height: 1fr; border: round {C.mix(R.CYAN, 0.35)}; border-title-color: {R.CYAN}; }}
    #tape {{ width: 49; height: 1fr; border: round {C.mix(R.AMBER, 0.35)}; border-title-color: {R.AMBER}; }}
    #right {{ width: 1fr; min-width: 44; }}
    #price {{ height: 10; border: round {C.mix(R.GREEN, 0.35)}; border-title-color: {R.GREEN}; }}
    #depth {{ height: 9; border: round {C.mix(R.VIOLET, 0.4)}; border-title-color: {R.VIOLET}; }}
    #analytics {{ height: 15; border: round {C.mix(R.BLUE, 0.4)}; border-title-color: {R.BLUE}; }}
    #alerts {{ height: 1fr; min-height: 5; border: round {C.mix(R.AMBER, 0.3)}; border-title-color: {R.AMBER}; }}
    #footer {{ height: 1; background: #10151b; }}
    Screen.narrow #main {{ layout: grid; grid-size: 2; grid-columns: 1fr 1fr;
                           grid-rows: 24 39; overflow-y: auto; }}
    Screen.narrow #book, Screen.narrow #tape {{ width: 1fr; }}
    Screen.narrow #right {{ column-span: 2; height: 39; }}
    #filter {{ height: 1; border: none; padding: 0 1; background: #10151b; color: {R.AMBER}; }}
    #filter:focus {{ background: {C.mix(R.AMBER, 0.15)}; }}
    #watch {{ height: 1fr; border: round {C.mix(R.CYAN, 0.35)}; border-title-color: {R.CYAN}; }}
    #eq-chart {{ height: 1fr; border: round {C.mix(R.GREEN, 0.35)}; border-title-color: {R.GREEN}; }}
    #eq-info {{ height: 5; border: round {C.mix(R.AMBER, 0.35)}; border-title-color: {R.AMBER}; }}
    HelpScreen {{ align: center middle; background: #000000 60%; }}
    #help {{ text-wrap: wrap; width: 88; height: auto; border: round {R.CYAN}; background: #0d1116; padding: 1 2; }}
    """
    BINDINGS = [
        Binding("1", "quick('1')", show=False), Binding("2", "quick('2')", show=False),
        Binding("3", "quick('3')", show=False), Binding("p", "pause", show=False),
        Binding("e", "export", show=False), Binding("question_mark", "help", show=False),
        Binding("m", "markets", show=False),
        Binding("left_square_bracket", "threshold(-1)", show=False),
        Binding("right_square_bracket", "threshold(1)", show=False),
        Binding("q", "shutdown", show=False),
        Binding("ctrl+c", "shutdown", show=False, priority=True),
    ]

    def __init__(self, symbol: str = "BTCUSDT", large: Decimal | None = None,
                 ascii_mode: bool = False, endpoints: tuple[str, ...] = ENDPOINTS,
                 connect: bool = True) -> None:
        super().__init__()
        self.ascii_mode = ascii_mode
        self.endpoints = endpoints
        self.connect = connect
        self.user_large = large
        self.state = MarketState(symbol, large or Decimal(0))
        self.board = MarketBoard(on_error=lambda m: log.warning(m))
        self.gen = 0
        self.feed: Feed | None = None
        self.feed_task: asyncio.Task | None = None
        self.feed_status = Status.CONNECTING
        self.feed_detail = ""
        self.paused_at: float | None = None
        self.stale_flag = False

    # ---- layout ------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static(id="header")
        yield Static(id="metrics")
        with Horizontal(id="main"):
            yield Static(id="book", classes="panel")
            yield Static(id="tape", classes="panel")
            with Vertical(id="right"):
                yield Static(id="price", classes="panel")
                yield Static(id="depth", classes="panel")
                yield Static(id="analytics", classes="panel")
                yield Static(id="alerts", classes="panel")
        yield Static(id="footer")

    def on_mount(self) -> None:
        titles = {"book": "ORDER BOOK", "tape": "TRADE TAPE", "price": "PRICE",
                  "depth": "CUMULATIVE DEPTH", "analytics": "ORDER FLOW", "alerts": "ALERTS"}
        for wid, title in titles.items():
            self.query_one(f"#{wid}").border_title = title
        self.query_one("#price").border_subtitle = "last trade, 5m"
        self.query_one("#tape").border_subtitle = "^ v = uptick / downtick"
        self._apply_responsive()
        self.state.alert(self._wall_ms(), "INFO", "LIVE = exchange data only; no synthetic data")
        if self.connect:
            self._start_feed()
            self.board.start()
        else:
            self.feed_status, self.feed_detail = Status.OFFLINE, "network disabled"
            self.board.crypto_status = self.board.yahoo_status = "OFFLINE"
        self.set_interval(REFRESH_S, self.refresh_view)
        self.refresh_view(force=True)

    def _apply_responsive(self) -> None:
        self.screen.set_class(self.size.width < 128 or self.size.height < 36, "narrow")

    # ---- feed management --------------------------------------------
    @staticmethod
    def _wall_ms() -> int:
        return int(time.time() * 1000)

    def _start_feed(self) -> None:
        self.gen += 1
        self.feed = Feed(self.state.symbol, self.gen, self._on_event, self._on_status,
                         self._on_error, self.endpoints)
        self.feed_status = Status.CONNECTING
        self.feed_task = asyncio.create_task(self.feed.run(), name=f"feed-{self.gen}")

    async def _stop_feed(self) -> None:
        task, self.feed_task = self.feed_task, None
        self.gen += 1  # any late callback from the old feed is now ignored
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=3)

    def _on_event(self, gen: int, ev: Trade | BookSnapshot) -> None:
        if gen != self.gen:
            return
        now = time.monotonic()
        self.state.note_message(now)
        if isinstance(ev, Trade):
            self.state.apply_trade(ev, now)
        else:
            self.state.apply_depth(ev, now)

    def _on_status(self, gen: int, status: Status, detail: str) -> None:
        if gen != self.gen:
            return
        prev = self.feed_status
        if prev == Status.LIVE and status != Status.LIVE:
            self.state.on_reconnect(time.monotonic())
            self.state.alert(self._wall_ms(), "CONN", "feed lost: book cleared, windows restart")
        if status != prev:
            if status == Status.LIVE:
                self.state.alert(self._wall_ms(), "CONN", f"LIVE {self.state.symbol} {detail.split('//')[-1]}")
            elif status == Status.OFFLINE and prev != Status.OFFLINE:
                self.state.alert(self._wall_ms(), "CONN", "OFFLINE: exchange unreachable, retrying")
        self.feed_status, self.feed_detail = status, detail
        self.stale_flag = False if status != Status.LIVE else self.stale_flag

    def _on_error(self, gen: int, msg: str) -> None:
        if gen != self.gen:
            return
        if msg.startswith("parse:"):
            self.state.parse_errors += 1
            if self.state.parse_errors > 5:
                return
        log.warning(msg)
        self.state.alert(self._wall_ms(), "ERROR", msg[:120])

    # ---- rendering ---------------------------------------------------
    def display_status(self, now: float) -> str:
        if self.feed_status == Status.LIVE:
            age = self.state.receive_age(now)
            if age is None or age > STALE_S:
                return "STALE"
        return self.feed_status.value

    def refresh_view(self, force: bool = False) -> None:
        now = time.monotonic()
        status = self.display_status(now)
        if status == "STALE" and not self.stale_flag:
            self.stale_flag = True
            self.state.alert(self._wall_ms(), "STALE", f"no messages for >{STALE_S:.0f}s")
        elif status == "LIVE" and self.stale_flag:
            self.stale_flag = False
            self.state.alert(self._wall_ms(), "CONN", "data flowing again")
        if self.screen is not self.screen_stack[0]:
            return  # another screen is showing; ingestion continues
        self._apply_responsive()
        self.query_one("#header", Static).update(self._header(now, status))
        self.query_one("#footer", Static).update(self._footer(now))
        if self.paused_at is not None and not force:
            return
        st, a = self.state, self.ascii_mode
        wall_s = int(time.time())
        sz = lambda wid: self.query_one(f"#{wid}").content_size
        b = sz("book"); book = self.query_one("#book", Static)
        book.update(R.book_text(st, b.width, b.height, a))
        shown = min(DEPTH_LEVELS, max(1, (b.height - 2) // 2))
        book.border_subtitle = f"{shown}/{DEPTH_LEVELS} lvls shown; analytics: top {DEPTH_LEVELS}"
        tp = sz("tape"); self.query_one("#tape", Static).update(R.tape_text(st, tp.width, tp.height))
        p = sz("price"); self.query_one("#price", Static).update(
            R.price_chart_text(st, p.width, p.height, a, wall_s))
        d = sz("depth"); self.query_one("#depth", Static).update(R.depth_chart_text(st, d.width, d.height, a))
        an = sz("analytics"); self.query_one("#analytics", Static).update(
            R.analytics_text(st, now, an.width, a, wall_s))
        al = sz("alerts"); self.query_one("#alerts", Static).update(R.alerts_text(st, al.height))
        self.query_one("#metrics", Static).update(self._metrics())

    def _header(self, now: float, status: str) -> Text:
        extra: list[tuple[str, str]] = []
        if self.feed_status in (Status.RECONNECTING, Status.OFFLINE) and self.feed and self.feed.retry_at:
            extra.append((f" retry {max(0.0, self.feed.retry_at - now):.0f}s", R.DIM))
        if self.paused_at is not None:
            extra.append(("  ", ""))
            extra.append((f" PAUSED  view age {now - self.paused_at:,.1f}s ", f"bold {R.BG} on {R.VIOLET}"))
        extra.append(("   ", ""))
        for k, s in QUICK.items():
            on = s == self.state.symbol
            extra.append((f"{k}:{SYMBOLS[s].base} ", f"bold {R.AMBER}" if on else R.DIM))
        return header_line(self.size.width, "BINANCE SPOT", self.state.symbol, "", status, extra)

    def _metrics(self) -> Text:
        st, dp = self.state, self.state.spec.price_dp
        t = Text(no_wrap=True, overflow="crop")

        def cell(label: str, value: str, style: str = R.FG) -> None:
            t.append(label + " ", style=R.DIM)
            t.append(value, style=f"bold {style}")
            t.append("  ")

        last = st.last_price
        style = R.FG
        if st.trades:
            style = R.GREEN if st.trades[-1].is_buy else R.RED
        cell("LAST", R.fmt_num(last, dp), style)
        cell("MID", R.fmt_num(st.midprice(), dp + 1), R.FG)
        cell("SPREAD", R.fmt_num(st.spread(), dp), R.AMBER)
        cell("BPS", R.fmt_num(st.spread_bps(), 3), R.AMBER)
        cell("VWAP since connect", R.fmt_num(st.vwap(), dp), R.VIOLET)
        cell("BID", R.fmt_num(st.best_bid, dp), R.GREEN)
        cell("ASK", R.fmt_num(st.best_ask, dp), R.RED)
        q = self.board.quotes.get(st.symbol)
        if q and q.change_pct is not None:
            cell("24H", f"{q.change_pct:+.2f}%", R.sign_color(q.change_pct))
        return t

    def _footer(self, now: float) -> Text:
        st = self.state
        t = key_hints([("M", "markets"), ("1-3", "symbol"), ("P", "pause"), ("E", "export"),
                       ("[ ]", "threshold"), ("?", "help"), ("Q", "quit")])
        age = st.receive_age(now)
        t.append("| ", style=R.DIM)
        t.append(f"{st.message_rate(now):,.1f} msg/s", style=R.CYAN)
        t.append(f"  recv age {'--' if age is None else f'{age:.2f}s'}"
                 f"  reconn {st.reconnects}  dupes {st.duplicates}  parse err {st.parse_errors}",
                 style=R.DIM)
        return t

    # ---- actions -----------------------------------------------------
    async def switch_symbol(self, sym: str) -> None:
        if sym == self.state.symbol or sym not in SYMBOLS:
            return
        await self._stop_feed()
        self.state.reset(sym)
        if self.user_large is None:
            self.state.large_quote = SYMBOLS[sym].large_quote
        self.state.alert(self._wall_ms(), "INFO", f"switched to {sym}: symbol state cleared")
        self.paused_at = None
        self.stale_flag = False
        if self.connect:
            self._start_feed()
        else:
            self.feed_status = Status.OFFLINE
        self.refresh_view(force=True)

    async def action_quick(self, key: str) -> None:
        if self.screen is self.screen_stack[0]:
            await self.switch_symbol(QUICK[key])

    def action_markets(self) -> None:
        if self.screen is self.screen_stack[0]:
            self.push_screen(MarketsScreen())

    def action_pause(self) -> None:
        if self.paused_at is None:
            self.paused_at = time.monotonic()
        else:
            self.paused_at = None
            self.refresh_view(force=True)

    def action_threshold(self, direction: int) -> None:
        cur = self.state.large_quote
        if direction > 0:
            nxt = next((x for x in THRESHOLDS if x > cur), THRESHOLDS[-1])
        else:
            nxt = next((x for x in reversed(THRESHOLDS) if x < cur), THRESHOLDS[0])
        self.state.large_quote = self.user_large = nxt
        self.state.alert(self._wall_ms(), "INFO", f"large-trade threshold {R.fmt_compact(nxt)} {self.state.spec.quote}")
        self.refresh_view(force=True)

    async def action_export(self) -> None:
        trades = list(self.state.trades)
        if not trades:
            self.state.alert(self._wall_ms(), "EXPORT", "nothing to export yet")
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = Path("exports") / f"orderflow_{self.state.symbol}_{stamp}.csv"
        try:
            n = await asyncio.to_thread(write_csv, path, trades)  # never block the UI loop
            self.state.alert(self._wall_ms(), "EXPORT", f"{n:,} trades -> {path}")
        except OSError as e:
            self.state.alert(self._wall_ms(), "ERROR", f"export failed: {e}")
        self.refresh_view(force=True)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    async def action_shutdown(self) -> None:
        await self._stop_feed()
        await self.board.stop()
        self.exit()
