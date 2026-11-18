"""Pure render helpers: state -> rich.Text. No Textual dependency."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from rich.text import Text

from . import charts as C
from .state import DEPTH_LEVELS, FLOW_WINDOW_S, RATE_WINDOW_S, MarketState

# palette: cyan HUD, amber only for warnings and the large-print highlight
BG = "#04070b"
CYAN = "#4fd8ff"
AMBER = "#ffb454"
GREEN = "#3ce58a"
RED = "#ff5c6c"
VIOLET = "#8fa8ff"
BLUE = "#5aa9ff"
DIM = "#3c5566"
MUTED = "#6c8ea3"
FG = "#d4f1ff"

BLOCKS_UNI = C.BLOCKS_UNI
BLOCKS_ASCII = C.BLOCKS_ASCII
column_chart = C.column_chart  # re-export (tests / callers)


def fmt_num(x: Decimal | float | None, dp: int, signed: bool = False, na: str = "--") -> str:
    if x is None:
        return na
    return f"{x:+,.{dp}f}" if signed else f"{x:,.{dp}f}"


def fmt_compact(x: Decimal | float | None, signed: bool = False) -> str:
    if x is None:
        return "--"
    v = float(x)
    a = abs(v)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            s = f"{v / div:,.2f}{suf}"
            break
    else:
        s = f"{v:,.0f}"
    return ("+" + s) if signed and v > 0 else s


def price_dp_for(v: float | None) -> int:
    if v is None:
        return 2
    a = abs(v)
    return 2 if a >= 1 else 4 if a >= 0.01 else 8


def hms(wall_ms: int) -> str:
    return datetime.fromtimestamp(wall_ms / 1000, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]


def _bar(frac: float, width: int, ascii_mode: bool) -> str:
    frac = max(0.0, min(1.0, frac))
    if ascii_mode:
        return "#" * round(frac * width)
    eighths = round(frac * width * 8)
    full, rem = divmod(eighths, 8)
    return "█" * full + ("▏▎▍▌▋▊▉"[rem - 1] if rem else "")


def sign_color(x) -> str:
    if x is None:
        return FG
    return GREEN if x > 0 else RED if x < 0 else FG


# ---------------------------------------------------------------- order book
def book_text(st: MarketState, width: int, height: int, ascii_mode: bool) -> Text:
    spec = st.spec
    t = Text(no_wrap=True, overflow="crop")
    pw, qw, nw = 12, 11, 8
    barw = max(0, width - pw - qw - nw - 3)
    rows_per_side = max(1, (height - 2) // 2)
    t.append(f"{'PRICE':>{pw}} {'QTY ' + spec.base:>{qw}} {'NOTION.':>{nw}} ", style=DIM)
    t.append("SIZE"[:barw] + "\n", style=DIM)
    if not st.bids and not st.asks:
        t.append("\n  waiting for order-book snapshot...", style=DIM)
        return t
    asks = st.asks[:rows_per_side]
    bids = st.bids[:rows_per_side]
    qmax = max((q for _, q in asks + bids), default=Decimal(1)) or Decimal(1)

    def row(p, q, color, best):
        frac = float(q / qmax)
        bg = f" on {C.mix(color, 0.16)}" if best else ""
        t.append(f"{p:>{pw},.{spec.price_dp}f} ", style=f"bold {color}{bg}" if best else color)
        t.append(f"{q:>{qw},.{spec.qty_dp}f} ", style=(f"bold {FG}" if frac > 0.5 else C.mix(FG, 0.55 + 0.45 * frac)) + bg)
        t.append(f"{fmt_compact(p * q):>{nw}} ", style=MUTED + bg)
        t.append(_bar(frac, barw, ascii_mode), style=C.mix(color, 0.35 + 0.65 * frac))
        t.append("\n")

    for _ in range(rows_per_side - len(asks)):
        t.append("\n")
    for i, (p, q) in enumerate(reversed(asks)):
        row(p, q, RED, i == len(asks) - 1)
    sp, bps, mid = st.spread(), st.spread_bps(), st.midprice()
    label = f" mid {fmt_num(mid, spec.price_dp + 1)}  spr {fmt_num(sp, spec.price_dp)} / {fmt_num(bps, 3)}bp "
    side = max(0, (width - len(label)) // 2)
    rule = "-" if ascii_mode else "─"
    t.append(rule * side, style=C.mix(AMBER, 0.4))
    t.append(label, style=f"bold {AMBER}")
    t.append(rule * max(0, width - side - len(label)) + "\n", style=C.mix(AMBER, 0.4))
    for i, (p, q) in enumerate(bids):
        row(p, q, GREEN, i == 0)
    t.rstrip()
    return t


# ---------------------------------------------------------------- trade tape
def tape_text(st: MarketState, width: int, height: int) -> Text:
    spec = st.spec
    t = Text(no_wrap=True, overflow="crop")
    t.append(f"{'TIME UTC':<12} {'PRICE':>10} {'QTY':>9} {'NOTION.':>8} SIDE\n", style=DIM)
    if not st.trades:
        t.append("\n  waiting for trades...", style=DIM)
        return t
    n = max(0, height - 1)
    trades = list(st.trades)[-(n + 1):] if n else []
    thr = float(st.large_quote) or 1.0
    first = 1 if len(trades) > n else 0  # extra oldest trade only provides the tick reference
    for i in range(len(trades) - 1, first - 1, -1):
        tr = trades[i]
        color = GREEN if tr.is_buy else RED
        notional = float(tr.notional)
        big = notional >= thr
        heat = min(1.0, notional / thr)
        bg = f" on {C.mix(color, 0.22)}" if big else ""
        prev = trades[i - 1].price if i > 0 else tr.price
        tick = " " if tr.price == prev else ("^" if tr.price > prev else "v")
        t.append(f"{hms(tr.trade_time_ms):<12} ", style=DIM + bg)
        t.append(f"{tr.price:>10,.{spec.price_dp}f}", style=color + bg)
        t.append(tick, style=C.mix(color, 0.6) + bg)
        t.append(f"{tr.qty:>9,.{spec.qty_dp}f} ", style=C.mix(FG, 0.45 + 0.55 * heat) + bg)
        t.append(f"{fmt_compact(tr.notional):>8} ", style=(f"bold {AMBER}" if big else C.mix(AMBER, 0.3 + 0.7 * heat)) + bg)
        t.append("BUY " if tr.is_buy else "SELL", style=f"bold {color}{bg}")
        t.append("\n")
    t.rstrip()
    return t


# ---------------------------------------------------------------- charts
PRICE_WINDOW_S = 300


def price_chart_text(st: MarketState, width: int, height: int, ascii_mode: bool, now_wall_s: int) -> Text:
    spec = st.spec
    t = Text(no_wrap=True, overflow="crop")
    series = [float(v) for v in st.price_series(PRICE_WINDOW_S, now_wall_s)]
    if len(series) < 2:
        t.append("collecting trade prices (1 close / second)...", style=DIM)
        return t
    axis_w = len(fmt_num(max(series), spec.price_dp)) + 1
    cw = max(4, width - axis_w)
    pts = C.resample(series, cw * 2)
    hi, lo = max(series), min(series)
    chg = series[-1] - series[0]
    color = GREEN if chg > 0 else RED if chg < 0 else CYAN
    vwap = st.vwap()
    ref = float(vwap) if vwap is not None else None
    span = f"{len(series) // 60}m{len(series) % 60:02d}s" if len(series) >= 60 else f"{len(series)}s"
    t.append(f"{span} ", style=DIM)
    t.append(fmt_num(chg, spec.price_dp, signed=True), style=f"bold {color}")
    t.append(f" ({chg / series[0] * 100:+.3f}%)", style=color)
    if ref is not None:
        t.append("  ┄ vwap" if not ascii_mode else "  - vwap", style=AMBER)
    t.append("\n")
    h = max(1, height - 1)
    lo_v = min(lo, ref) if ref is not None and abs(ref - lo) < (hi - lo) * 2 + 1e-9 else lo
    hi_v = max(hi, ref) if ref is not None and abs(ref - hi) < (hi - lo) * 2 + 1e-9 else hi
    lines = C.line_chart(pts, cw, h, color, ref=ref, lo=lo_v, hi=hi_v, ascii_mode=ascii_mode)
    for r, line in enumerate(lines):
        t.append_text(line)
        if r == 0:
            t.append(" " + fmt_num(hi_v, spec.price_dp), style=MUTED)
        elif r == h - 1:
            t.append(" " + fmt_num(lo_v, spec.price_dp), style=MUTED)
        elif r == h // 2:
            t.append(" " + fmt_num(series[-1], spec.price_dp), style=f"bold {color}")
        t.append("\n")
    t.rstrip()
    return t


def depth_chart_text(st: MarketState, width: int, height: int, ascii_mode: bool) -> Text:
    spec = st.spec
    t = Text(no_wrap=True, overflow="crop")
    cb, ca = st.cumulative_depth()
    if not cb or not ca:
        t.append("waiting for order-book snapshot...", style=DIM)
        return t
    t.append(f"cum {spec.base} top{DEPTH_LEVELS}  ", style=DIM)
    t.append("bid " + fmt_num(cb[-1], 2), style=GREEN)
    t.append("  ", style=DIM)
    t.append("ask " + fmt_num(ca[-1], 2) + "\n", style=RED)
    half = max(4, width // 2)
    left_vals = [float(v) for v in reversed(cb)]
    right_vals = [float(v) for v in ca]
    top = max(left_vals + right_vals) or 1.0
    h = max(2, height - 2)
    left = C.line_chart(C.stretch(left_vals, half * 2), half, h, GREEN, lo=0.0, hi=top, ascii_mode=ascii_mode)
    right = C.line_chart(C.stretch(right_vals, half * 2), half, h, RED, lo=0.0, hi=top, ascii_mode=ascii_mode)
    for l, r in zip(left, right):
        t.append_text(l)
        t.append_text(r)
        t.append("\n")
    lo_lbl = fmt_num(st.bids[-1][0], spec.price_dp)
    hi_lbl = fmt_num(st.asks[-1][0], spec.price_dp)
    mid_lbl = "|" + fmt_num(st.midprice(), spec.price_dp) + "|"
    gap = max(1, half * 2 - len(lo_lbl) - len(hi_lbl) - len(mid_lbl))
    t.append(lo_lbl, style=C.mix(GREEN, 0.7))
    t.append(" " * (gap // 2) + mid_lbl + " " * (gap - gap // 2), style=AMBER)
    t.append(hi_lbl, style=C.mix(RED, 0.7))
    return t


def analytics_text(st: MarketState, now: float, width: int, ascii_mode: bool, now_wall_s: int | None = None) -> Text:
    spec = st.spec
    imb = st.imbalance()
    cov = st.window_coverage(now)
    flow = st.signed_flow(now)
    bv, sv = st.buy_vol.total(now), st.sell_vol.total(now)
    rate = st.trade_rate(now)
    b, a = st.depth_totals()

    def line(*parts) -> Text:
        ln = Text(no_wrap=True, overflow="crop")
        for s, style in parts:
            ln.append(s, style=style)
        return ln

    stats = [line(("IMBALANCE t20", DIM))]
    if imb is None:
        stats.append(line(("--", DIM)))
    else:
        side = "bid heavy" if imb > 0 else "ask heavy" if imb < 0 else "balanced"
        stats.append(line((f"{imb:+.3f}", f"bold {sign_color(imb)}"), (f"  {side}", MUTED)))
    flow_line = [("FLOW 60s ", DIM), (f"{fmt_compact(flow, signed=True)} {spec.quote}", f"bold {sign_color(flow)}")]
    if st.window_start is None:
        flow_line.append(("  no trades yet", DIM))
    elif cov < FLOW_WINDOW_S:
        flow_line.append((f"  warm {cov:,.0f}/60s", AMBER))
    stats.append(line(*flow_line))
    bs = [("  buy ", DIM), (fmt_compact(bv), GREEN), ("  sell ", DIM), (fmt_compact(sv), RED)]
    if bv + sv > 0:
        bs.append((f"  {float(bv / (bv + sv)) * 100:.0f}% buy", MUTED))
    stats.append(line(*bs))
    stats.append(line(("TRADE RATE ", DIM), ("--" if rate is None else f"{rate:,.1f}/s", f"bold {BLUE}"),
                      (f"  ({int(RATE_WINDOW_S)}s)", DIM)))
    stats.append(line(("DEPTH t20 ", DIM), (fmt_num(b, 3), GREEN), (" / ", DIM), (fmt_num(a, 3), RED),
                      (f" {spec.base}", DIM)))
    if st.gaps or st.missed_trades:
        stats.append(line(("CONTINUITY ", DIM), (f"{st.gaps} gap(s), {st.missed_trades:,} missed ids", AMBER)))
    else:
        stats.append(line(("CONTINUITY ", DIM), ("continuous", C.mix(GREEN, 0.6))))

    t = Text(no_wrap=True, overflow="crop")
    if ascii_mode:
        for s in stats:
            t.append_text(s)
            t.append("\n")
    else:
        ring = C.ring_gauge(0.0 if imb is None else float(imb), 14, GREEN, RED, C.mix(CYAN, 0.45))
        blank = " " * (ring[0].cell_len if ring else 0)
        for i in range(max(len(ring), len(stats))):
            if i < len(ring):
                t.append_text(ring[i])
            else:
                t.append(blank)
            t.append("  ")
            if i < len(stats):
                t.append_text(stats[i])
            t.append("\n")

    hist_w = max(4, width - 1)
    if now_wall_s is None:
        now_wall_s = int(time.time())
    fs = [float(v) for v in st.flow_series(hist_w, now_wall_s)]
    for ln in C.diverging_bars(fs, hist_w, 4, GREEN, RED, ascii_mode):
        t.append_text(ln)
        t.append("\n")
    t.append(f"{'net flow / second, last ' + str(hist_w) + 's':>{hist_w}}", style=DIM)
    return t


ALERT_STYLE = {"LARGE": AMBER, "CONN": CYAN, "STALE": AMBER, "ERROR": RED, "EXPORT": VIOLET, "INFO": MUTED}


def alerts_text(st: MarketState, height: int) -> Text:
    t = Text(no_wrap=True, overflow="crop")
    t.append(f"large >= {fmt_compact(st.large_quote)} {st.spec.quote}  ([ ] adjust)\n", style=DIM)
    for a in list(st.alerts)[: max(0, height - 1)]:
        t.append(f"{hms(a.wall_ms)[:8]} ", style=DIM)
        t.append(f"{a.kind:<6} ", style=f"bold {ALERT_STYLE.get(a.kind, FG)}")
        style = (GREEN if a.side == "BUY" else RED) if a.side else FG
        t.append(a.text + "\n", style=style)
    t.rstrip()
    return t


# ---------------------------------------------------------------- markets
GROUP_TITLES = {
    "CRYPTO": ("CRYPTO", "Binance Spot · real-time 24h mini-ticker", CYAN),
    "INDIA": ("INDIA", "NSE/BSE via Yahoo Finance · delayed", AMBER),
    "US": ("UNITED STATES", "via Yahoo Finance · delayed", BLUE),
    "GLOBAL": ("GLOBAL INDICES", "via Yahoo Finance · delayed", VIOLET),
}


def range_track(price, lo, hi, width: int, ascii_mode: bool) -> Text:
    t = Text()
    if price is None or lo is None or hi is None or hi <= lo:
        t.append(("-" if ascii_mode else "─") * width, style=DIM)
        return t
    pos = round((price - lo) / (hi - lo) * (width - 1))
    pos = max(0, min(width - 1, pos))
    for i in range(width):
        frac = i / max(1, width - 1)
        col = C.gradient(RED, GREEN, frac)
        if i == pos:
            t.append("O" if ascii_mode else "●", style=f"bold {FG}")
        else:
            t.append("-" if ascii_mode else "━", style=C.mix(col, 0.55))
    return t


def watch_rows(board, filt: str = "") -> list:
    """Rows in display order: ('group', key) headers and ('quote', Quote)."""
    out = []
    f = filt.lower()
    for g in ("CRYPTO", "INDIA", "US", "GLOBAL"):
        qs = [q for q in board.quotes.values() if q.group == g
              and (not f or f in q.symbol.lower() or f in q.name.lower())]
        if qs:
            out.append(("group", g))
            out.extend(("quote", q) for q in qs)
    return out


def watchlist_text(board, rows, cursor: int, width: int, height: int, ascii_mode: bool,
                   now: float, current: str = "") -> Text:
    t = Text(no_wrap=True, overflow="crop")
    spark_w = max(8, min(36, width - 98))
    hdr = (f"  {'SYMBOL':<14}{'NAME':<22}{'LAST':>14}{'CHG':>12}{'CHG%':>9}  "
           f"{'LOW':>11} {'DAY/24H RANGE':^14} {'HIGH':<11}{'TREND':<{spark_w}} {'VOLUME':>8}  AGE")
    t.append(hdr[:width] + "\n", style=DIM)
    body_h = max(1, height - 1)
    start = 0
    if cursor >= body_h:
        start = cursor - body_h + 1
    for idx in range(start, min(len(rows), start + body_h)):
        kind, item = rows[idx]
        sel = idx == cursor
        if kind == "group":
            title, sub, col = GROUP_TITLES[item]
            t.append(f" {title} ", style=f"bold {BG} on {col}")
            t.append(f"  {sub}", style=C.mix(col, 0.8))
            t.append("\n")
            continue
        q = item
        bg = f" on {C.mix(CYAN, 0.16)}" if sel else ""
        dp = price_dp_for(q.price)
        chg, pct = q.change, q.change_pct
        col = sign_color(chg)
        mark = ">" if sel else ("*" if q.symbol == current else " ")
        t.append(f"{mark} ", style=f"bold {CYAN}{bg}")
        t.append(f"{q.symbol:<14}", style=f"bold {FG}{bg}")
        t.append(f"{q.name[:21]:<22}", style=MUTED + bg)
        t.append(f"{fmt_num(q.price, dp):>14}", style=f"bold {col}{bg}")
        t.append(f"{fmt_num(chg, dp, signed=True):>12}", style=col + bg)
        pct_s = "--" if pct is None else f"{pct:+.2f}%"
        heat = min(1.0, abs(pct or 0) / 5)
        t.append(" " + f"{pct_s:>7} ", style=f"bold {BG} on {C.mix(col, 0.35 + 0.65 * heat)}" if pct is not None else DIM + bg)
        t.append(f"  {fmt_num(q.low, dp):>11} ", style=C.mix(RED, 0.7) + bg)
        t.append_text(range_track(q.price, q.low, q.high, 14, ascii_mode))
        t.append(f" {fmt_num(q.high, dp):<11}", style=C.mix(GREEN, 0.7) + bg)
        sp = C.sparkline(q.series, spark_w, ascii_mode) if len(q.series) > 1 else ""
        trend_col = sign_color((q.series[-1] - q.series[0]) if len(q.series) > 1 else None)
        t.append(f"{sp:<{spark_w}}", style=trend_col)
        t.append(f" {fmt_compact(q.volume):>8}", style=MUTED + bg)
        if q.market_time:
            age = time.time() - q.market_time
            age_s = f"{age:.0f}s" if age < 120 else f"{age / 60:.0f}m" if age < 7200 else f"{age / 3600:.0f}h"
        else:
            age_s = "--"
        t.append(f"  {age_s:>4}", style=(C.mix(GREEN, 0.7) if q.source == "BINANCE" else AMBER) + bg)
        t.append("\n")
    t.rstrip()
    return t


# ---------------------------------------------------------------- equity
def equity_metrics_text(ch, now_s: float) -> Text:
    t = Text(no_wrap=True, overflow="crop")
    if ch is None:
        t.append("loading delayed quote from Yahoo Finance...", style=DIM)
        return t
    dp = price_dp_for(ch.price)
    chg = None if ch.price is None or not ch.prev_close else ch.price - ch.prev_close
    pct = None if chg is None else chg / ch.prev_close * 100
    col = sign_color(chg)

    def cell(label, value, style=FG):
        t.append(label + " ", style=DIM)
        t.append(value, style=f"bold {style}")
        t.append("   ")

    cell("LAST", fmt_num(ch.price, dp) + (f" {ch.currency}" if ch.currency else ""), col)
    cell("CHG", fmt_num(chg, dp, signed=True) + ("" if pct is None else f" ({pct:+.2f}%)"), col)
    cell("PREV CLOSE", fmt_num(ch.prev_close, dp))
    cell("DAY LO", fmt_num(ch.day_low, dp), RED)
    cell("DAY HI", fmt_num(ch.day_high, dp), GREEN)
    if ch.volume:
        cell("VOL", fmt_compact(ch.volume), BLUE)
    return t


def equity_chart_text(ch, width: int, height: int, ascii_mode: bool) -> Text:
    t = Text(no_wrap=True, overflow="crop")
    if ch is None or not ch.bars:
        t.append("no intraday bars yet (market may not have opened today)", style=DIM)
        return t
    dp = price_dp_for(ch.price)
    axis_w = len(fmt_num(max(b.h for b in ch.bars), dp)) + 1
    cw = max(10, width - axis_w)
    h = max(3, height - 2)
    ohlc = [(b.o, b.h, b.l, b.c) for b in ch.bars]
    lines, lo, hi = C.candle_chart(ohlc, cw, h, GREEN, RED, ref=ch.prev_close, ascii_mode=ascii_mode)
    t.append(f"1-minute candles, {len(ch.bars)} bars  ", style=DIM)
    t.append("┄ prev close" if not ascii_mode else "- prev close", style=AMBER)
    t.append("\n")
    for r, line in enumerate(lines):
        t.append_text(line)
        v = hi - (hi - lo) * r / max(1, h - 1)
        if r % max(1, h // 6) == 0 or r == h - 1:
            t.append(" " + fmt_num(v, dp), style=MUTED)
        t.append("\n")
    # time axis in exchange local time
    tz = timezone(timedelta(seconds=ch.gmtoffset))
    first, last = ch.bars[0].t, ch.bars[-1].t
    n_bars = min(len(ch.bars), cw)
    x0 = cw - n_bars
    axis = [" "] * cw
    ticks = 5
    for k in range(ticks + 1):
        x = x0 + round(k * (n_bars - 1) / ticks)
        ts = first + (last - first) * (x - x0) / max(1, n_bars - 1)
        lbl = datetime.fromtimestamp(ts, tz).strftime("%H:%M")
        s = max(0, min(cw - len(lbl), x - len(lbl) // 2))
        if all(c == " " for c in axis[max(0, s - 1):s + len(lbl) + 1]):
            axis[s:s + len(lbl)] = list(lbl)
    t.append("".join(axis), style=DIM)
    return t


def equity_info_text(ch, now_s: float, err: str, width: int, ascii_mode: bool) -> Text:
    t = Text(no_wrap=True, overflow="crop")
    if ch is None:
        if err:
            t.append("ERROR ", style=f"bold {RED}"); t.append(err, style=FG)
        return t
    dp = price_dp_for(ch.price)
    open_ = ch.market_open(now_s)
    t.append(" MARKET OPEN " if open_ else " MARKET CLOSED ",
             style=f"bold {BG} on {GREEN if open_ else MUTED}")
    t.append(f"  {ch.exchange}  {ch.tz_name}", style=MUTED)
    if ch.market_time:
        tz = timezone(timedelta(seconds=ch.gmtoffset))
        lt = datetime.fromtimestamp(ch.market_time, tz)
        age = now_s - ch.market_time
        t.append(f"   last price at {lt:%Y-%m-%d %H:%M:%S} exchange time", style=FG)
        t.append(f"  ({age / 60:,.0f} min ago)" if age >= 60 else f"  ({age:.0f}s ago)", style=AMBER)
    t.append("\n")
    if ch.bars:
        o = ch.bars[0].o
        t.append("SESSION RANGE ", style=DIM)
        t.append(fmt_num(ch.day_low, dp) + " ", style=RED)
        t.append_text(range_track(ch.price, ch.day_low, ch.day_high, max(10, min(40, width - 60)), ascii_mode))
        t.append(" " + fmt_num(ch.day_high, dp), style=GREEN)
        if o and ch.price:
            r = (ch.price - o) / o * 100
            t.append("   from first bar ", style=DIM)
            t.append(f"{r:+.2f}%", style=f"bold {sign_color(r)}")
        t.append("\n")
    if err:
        t.append("last refresh failed: ", style=AMBER); t.append(err, style=FG)
    return t
