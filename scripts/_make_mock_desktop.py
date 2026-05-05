#!/usr/bin/env python3
"""Generate a privacy-safe mock 'desktop screenshot' for the README demo.

The image is a plausible-looking Mac desktop — gradient wallpaper, menu bar,
a few window frames with abstract content, dock — but reveals nothing real
about the host machine. Used by scripts/demo.py to fake `screencapture`
when recording the README GIF.
"""
from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parent.parent / "docs" / "assets" / "screenshots" / "mock-desktop.png"
W, H = 1440, 900


def make() -> Path:
    img = Image.new("RGB", (W, H), (12, 16, 24))
    d = ImageDraw.Draw(img)

    # Wallpaper — diagonal gradient with a soft blue glow
    for y in range(H):
        t = y / H
        r = int(12 + 16 * t)
        g = int(16 + 28 * t)
        b = int(28 + 80 * (1 - abs(0.5 - t) * 2))
        d.line([(0, y), (W, y)], fill=(r, g, b))

    # Soft glow blob
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([(W * 0.55, H * 0.1), (W * 1.1, H * 0.7)], fill=(80, 140, 220, 60))
    glow = glow.filter(ImageFilter.GaussianBlur(80))
    img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")
    d = ImageDraw.Draw(img)

    # Menu bar
    d.rectangle([(0, 0), (W, 28)], fill=(20, 22, 30, 220))
    try:
        font_bold = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", 14)
        font_mono = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 12)
    except Exception:
        font_bold = ImageFont.load_default()
        font_mono = font_bold
    d.text((16, 6), "", fill="white", font=font_bold)
    d.text((40, 7), "Finder", fill="white", font=font_bold)
    d.text((W - 90, 7), "12:34 PM", fill="white", font=font_bold)

    # Window 1: code-editor-ish (no real content — just visual texture)
    win = (140, 110, 760, 540)
    _window(d, win, "kernel/claude.py — alfred-assistant")
    # Mock code lines
    cy = win[1] + 56
    for line in [
        ("from", (200, 120, 240)), (" ", None), ("kernel.runner", (210, 230, 250)),
        (" ", None), ("import", (200, 120, 240)), (" ", None), ("Context", (130, 200, 230)),
    ]:
        # Dummy: just show a faint code-like horizontal bar set
        pass
    for i in range(14):
        y = cy + i * 22
        widths = [(40, 200, 220), (60, 200, 220), (160, 220, 240), (90, 200, 220)]
        x = win[0] + 16
        for w, c in [(40, (90, 110, 140)), (180, (160, 200, 220)), (60, (200, 130, 200))]:
            d.rectangle([(x, y), (x + w, y + 6)], fill=c)
            x += w + 12

    # Window 2: browser-ish on the right
    win2 = (790, 110, 1330, 540)
    _window(d, win2, "Alfred — TiGi-cloud")
    # Mock a stylised "graph" inside
    bx, by, bw, bh = win2[0] + 40, win2[1] + 80, win2[2] - win2[0] - 80, 220
    d.rectangle([(bx, by), (bx + bw, by + bh)], outline=(60, 80, 110), width=1)
    import math
    pts = []
    for i in range(60):
        x = bx + (bw * i / 59)
        y = by + bh * (0.5 - 0.4 * math.sin(i * 0.3))
        pts.append((x, y))
    for i in range(len(pts) - 1):
        d.line([pts[i], pts[i + 1]], fill=(120, 180, 240), width=2)

    # Window 3 — a lower terminal-ish window
    win3 = (320, 580, 1120, 820)
    _window(d, win3, "Terminal — alfred")
    # Mock prompt lines
    py = win3[1] + 56
    d.text((win3[0] + 18, py),     "$ python3 app.py", fill=(200, 220, 220), font=font_mono)
    d.text((win3[0] + 18, py + 22), "🎩 Web chat:   http://127.0.0.1:8765/?token=…", fill=(180, 200, 200), font=font_mono)
    d.text((win3[0] + 18, py + 44), "   Dashboard:  http://127.0.0.1:8765/dashboard?token=…", fill=(180, 200, 200), font=font_mono)
    d.text((win3[0] + 18, py + 66), "12:34:01 INFO  alfred.adapters.telegram — Telegram adapter started", fill=(140, 180, 140), font=font_mono)
    d.text((win3[0] + 18, py + 88), "12:34:02 INFO  alfred.kernel.scheduler — Scheduler started (every 30s)", fill=(140, 180, 140), font=font_mono)
    d.text((win3[0] + 18, py + 110), "12:34:02 INFO  alfred.kernel.metrics — Metrics collector started (every 60s)", fill=(140, 180, 140), font=font_mono)

    # Dock
    dock_y = H - 70
    d.rounded_rectangle([(W // 2 - 280, dock_y), (W // 2 + 280, dock_y + 56)],
                         radius=18, fill=(35, 40, 55, 220),
                         outline=(80, 90, 120, 80), width=1)
    icons = [(64, 132, 220), (240, 80, 80), (90, 200, 130), (220, 180, 80),
             (180, 90, 200), (60, 180, 200), (200, 110, 80)]
    for i, c in enumerate(icons):
        x = W // 2 - 280 + 28 + i * 78
        d.rounded_rectangle([(x, dock_y + 8), (x + 40, dock_y + 48)], radius=8, fill=c)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, optimize=True)
    return OUT


def _window(d: ImageDraw.ImageDraw, box, title: str) -> None:
    x1, y1, x2, y2 = box
    d.rounded_rectangle(box, radius=10, fill=(28, 32, 42), outline=(70, 80, 100), width=1)
    # Title bar
    d.rounded_rectangle([(x1, y1), (x2, y1 + 30)], radius=10, fill=(40, 44, 56))
    d.rectangle([(x1, y1 + 22), (x2, y1 + 30)], fill=(40, 44, 56))
    # Traffic lights
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        cx = x1 + 14 + i * 18
        d.ellipse([(cx, y1 + 9), (cx + 12, y1 + 21)], fill=c)
    # Title text
    try:
        f = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", 12)
    except Exception:
        f = ImageFont.load_default()
    d.text((x1 + 80, y1 + 9), title, fill=(180, 190, 210), font=f)


if __name__ == "__main__":
    p = make()
    print(f"wrote {p}  ({p.stat().st_size:,} bytes)")
