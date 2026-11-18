"""Terminal chart primitives returning rich.Text lines.

Unicode mode uses braille (2x4 dots per cell) for line charts, eighth blocks
for columns and half blocks for candles. ASCII mode degrades to plain glyphs.
"""
from __future__ import annotations

import math

from rich.text import Text

BG = (4, 7, 11)
BLOCKS_UNI = " ▁▂▃▄▅▆▇█"
BLOCKS_ASCII = " ..::-=+*#"
_BRAILLE_BITS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))  # [row][col]


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def mix(color: str, frac: float, base: tuple[int, int, int] = BG) -> str:
    """Blend `color` toward the background: frac=1 -> color, 0 -> background."""
    frac = max(0.0, min(1.0, frac))
    r, g, b = _rgb(color)
    return "#%02x%02x%02x" % tuple(round(bc + (c - bc) * frac) for c, bc in zip((r, g, b), base))


def gradient(c1: str, c2: str, frac: float) -> str:
    return mix(c2, frac, _rgb(c1))


def resample(values: list[float], n: int) -> list[float]:
    """Downsample to n points by taking each bucket's last value (a 'close')."""
    if n <= 0 or not values:
        return []
    if len(values) <= n:
        return list(values)
    out = []
    for i in range(n):
        end = int(round((i + 1) * len(values) / n)) - 1
        out.append(values[max(0, end)])
    return out


def sparkline(values: list[float], width: int, ascii_mode: bool = False) -> str:
    vals = resample(values, width)
    if not vals:
        return ""
    blocks = BLOCKS_ASCII if ascii_mode else BLOCKS_UNI
    lo, hi = min(vals), max(vals)
    span = hi - lo
    return "".join(blocks[4 if span <= 0 else 1 + round((v - lo) / span * 7)] for v in vals)


def column_chart(values: list[float], height: int, ascii_mode: bool,
                 lo: float | None = None, hi: float | None = None) -> list[str]:
    blocks = BLOCKS_ASCII if ascii_mode else BLOCKS_UNI
    if not values or height <= 0:
        return [""] * max(0, height)
    lo = min(values) if lo is None else lo
    hi = max(values) if hi is None else hi
    span = hi - lo
    levels = [max(1, round((0.5 if span <= 0 else (v - lo) / span) * (height * 8 - 1)) + 1) for v in values]
    rows = []
    for r in range(height - 1, -1, -1):
        rows.append("".join(blocks[8] if lv - r * 8 >= 8 else blocks[max(0, lv - r * 8)] if lv > r * 8 else " "
                            for lv in levels))
    return rows


def line_chart(values: list[float], width: int, height: int, color: str,
               ref: float | None = None, ref_color: str = "#e6b450",
               lo: float | None = None, hi: float | None = None,
               ascii_mode: bool = False, fill: bool = True) -> list[Text]:
    """Braille line chart, one dot column per value (newest at the right edge),
    with a shaded area beneath and an optional dotted reference level."""
    if height <= 0 or width <= 0:
        return []
    if ascii_mode:
        rows = column_chart(resample(values, width), height, True, lo, hi)
        return [Text(r, style=color) for r in rows]
    W, H = width * 2, height * 4
    vals = values[-W:]
    if not vals:
        return [Text("") for _ in range(height)]
    lo = min(vals + ([ref] if ref is not None else [])) if lo is None else lo
    hi = max(vals + ([ref] if ref is not None else [])) if hi is None else hi
    span = (hi - lo) or 1.0

    def ypix(v: float) -> int:  # 0 = top dot row
        return H - 1 - max(0, min(H - 1, round((v - lo) / span * (H - 1))))

    x0 = W - len(vals)
    line = [[0] * width for _ in range(height)]
    area = [[0] * width for _ in range(height)]
    refm = [[0] * width for _ in range(height)]

    def dot(grid, x, y):
        grid[y // 4][x // 2] |= _BRAILLE_BITS[y % 4][x % 2]

    prev = None
    for i, v in enumerate(vals):
        x, y = x0 + i, ypix(v)
        a, b = (y, y) if prev is None else (min(prev, y), max(prev, y))
        for yy in range(a, b + 1):
            dot(line, x, yy)
        if fill:
            for yy in range(y + 1, H):
                if (yy + x) % 2 == 0:  # checkerboard shading
                    dot(area, x, yy)
        prev = y
    if ref is not None and lo <= ref <= hi:
        yr = ypix(ref)
        for x in range(0, W, 2):
            dot(refm, x, yr)

    out = []
    for r in range(height):
        t = Text()
        shade = mix(color, 0.18 + 0.22 * (1 - r / max(1, height - 1)))
        for c in range(width):
            if line[r][c]:
                t.append(chr(0x2800 + (line[r][c] | refm[r][c])), style=color)
            elif refm[r][c]:
                t.append(chr(0x2800 + (refm[r][c] | area[r][c])), style=ref_color)
            elif area[r][c]:
                t.append(chr(0x2800 + area[r][c]), style=shade)
            else:
                t.append(" ")
        out.append(t)
    return out


def candle_chart(bars: list[tuple[float, float, float, float]], width: int, height: int,
                 up: str, down: str, ref: float | None = None, ref_color: str = "#e6b450",
                 ascii_mode: bool = False) -> tuple[list[Text], float, float]:
    """OHLC candles, one column each (aggregated to fit), half-row body resolution.
    Returns (lines, lo, hi)."""
    if not bars or width <= 0 or height <= 0:
        return [Text("") for _ in range(max(0, height))], 0.0, 0.0
    if len(bars) > width:  # aggregate consecutive bars into `width` candles
        agg = []
        for i in range(width):
            a = int(i * len(bars) / width)
            b = max(a + 1, int((i + 1) * len(bars) / width))
            grp = bars[a:b]
            agg.append((grp[0][0], max(g[1] for g in grp), min(g[2] for g in grp), grp[-1][3]))
        bars = agg
    lo = min(b[2] for b in bars)
    hi = max(b[1] for b in bars)
    if ref is not None:
        lo, hi = min(lo, ref), max(hi, ref)
    span = (hi - lo) or 1.0
    sub = height * 2

    def sy(v: float) -> int:  # sub-row index from top
        return sub - 1 - max(0, min(sub - 1, int((v - lo) / span * (sub - 1) + 0.5)))

    ref_row = None if ref is None else sy(ref) // 2
    x0 = width - len(bars)
    grid: list[list[tuple[str, str]]] = [[(" ", "")] * width for _ in range(height)]
    for i, (o, h, l, c) in enumerate(bars):
        x = x0 + i
        color = up if c >= o else down
        bt, bb = sy(max(o, c)), sy(min(o, c))
        wt, wb = sy(h), sy(l)
        for r in range(height):
            top, bot = 2 * r, 2 * r + 1
            body_top, body_bot = bt <= top <= bb, bt <= bot <= bb
            wick = wt <= bot and wb >= top
            if body_top and body_bot:
                ch = "#" if ascii_mode else "█"
            elif body_top:
                ch = "#" if ascii_mode else "▀"
            elif body_bot:
                ch = "#" if ascii_mode else "▄"
            elif wick:
                ch = "|" if ascii_mode else "│"
            else:
                continue
            grid[r][x] = (ch, color)
    out = []
    for r in range(height):
        t = Text()
        for ch, color in grid[r]:
            if ch == " " and r == ref_row:
                t.append("-" if ascii_mode else "┄", style=mix(ref_color, 0.7))
            else:
                t.append(ch, style=color)
        out.append(t)
    return out, lo, hi


def diverging_bars(values: list[float], width: int, height: int, pos: str, neg: str,
                   ascii_mode: bool = False) -> list[Text]:
    """Zero-centred histogram: positives rise above the midline, negatives hang below."""
    vals = values[-width:]
    half = max(1, height // 2)
    out_rows = [Text() for _ in range(half * 2)]
    pad = width - len(vals)
    top = max((abs(v) for v in vals), default=0.0) or 1.0
    up_blocks = BLOCKS_ASCII if ascii_mode else BLOCKS_UNI
    for r in range(half * 2):
        out_rows[r].append(" " * pad)
    for v in vals:
        frac = abs(v) / top
        eighths = round(frac * half * 8)
        intensity = 0.45 + 0.55 * frac
        for r in range(half):  # upper half, row 0 = top
            level = half - 1 - r
            fill = eighths - level * 8 if v > 0 else 0
            ch = up_blocks[8] if fill >= 8 else up_blocks[fill] if fill > 0 else " "
            out_rows[r].append(ch, style=mix(pos, intensity))
        for r in range(half):  # lower half, row 0 = just below midline
            fill = eighths - r * 8 if v < 0 else 0
            if ascii_mode:
                ch = "#" if fill >= 4 else " "
            else:
                ch = "█" if fill >= 6 else "▀" if fill >= 2 else " "
            out_rows[half + r].append(ch, style=mix(neg, intensity))
    return out_rows


def stretch(values: list[float], n: int) -> list[float]:
    """Stretch a short series to n points by holding each value (step look)."""
    if not values or n <= 0:
        return []
    last = len(values) - 1
    return [values[min(last, i * len(values) // n)] for i in range(n)]


def ring_gauge(value: float, radius: int, pos: str, neg: str, rim: str) -> list[Text]:
    """Arc-reactor style gauge on a braille canvas. value in [-1, 1]: positive fills the
    right half clockwise from the top, negative fills the left half anticlockwise."""
    value = max(-1.0, min(1.0, value))
    cols = radius + 1
    rows = (2 * radius + 4) // 4
    W, H = cols * 2, rows * 4
    cx, cy = (W - 1) / 2, (H - 1) / 2
    layers = {k: [[0] * cols for _ in range(rows)] for k in ("pos", "neg", "tick", "rim", "inner")}

    def dot(layer: str, x: float, y: float) -> None:
        xi, yi = int(round(x)), int(round(y))
        if 0 <= xi < W and 0 <= yi < H:
            layers[layer][yi // 4][xi // 2] |= _BRAILLE_BITS[yi % 4][xi % 2]

    for k in range(720):
        deg = k / 2
        a = math.radians(deg)
        s, c = math.sin(a), -math.cos(a)
        dot("rim", cx + radius * s, cy + radius * c)
        dot("inner", cx + (radius - 5) * s, cy + (radius - 5) * c)
        if value > 0 and deg <= value * 180:
            for rr in (radius, radius - 1):
                dot("pos", cx + rr * s, cy + rr * c)
        if value < 0 and deg >= 360 + value * 180:
            for rr in (radius, radius - 1):
                dot("neg", cx + rr * s, cy + rr * c)
    for deg in range(0, 360, 30):
        a = math.radians(deg)
        s, c = math.sin(a), -math.cos(a)
        for rr in range(radius - 3, radius + 1):
            dot("tick", cx + rr * s, cy + rr * c)
    inner_col = mix(rim, 0.6)
    out = []
    for r in range(rows):
        t = Text()
        for ci in range(cols):
            cell = {k: layers[k][r][ci] for k in layers}
            bits = 0
            for v in cell.values():
                bits |= v
            if not bits:
                t.append(" ")
                continue
            style = pos if cell["pos"] else neg if cell["neg"] else rim if (cell["tick"] or cell["rim"]) else inner_col
            t.append(chr(0x2800 + bits), style=style)
        out.append(t)
    return out
