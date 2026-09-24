from __future__ import annotations
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

def make(template, cover, hook, out, cfg):
    im = Image.open(template).convert("RGBA")
    d = ImageDraw.Draw(im)
    if cover and Path(cover).exists():
        c = Image.open(cover).convert("RGBA")
        w = int(cfg["cover_width"])
        h = cfg.get("cover_height", -1)
        if h and h > 0:
            c.thumbnail((w, int(h)))
        else:
            c.thumbnail((w, im.height))
        im.alpha_composite(c, (cfg["cover_x"], cfg["cover_y"]))
    font = ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        int(cfg["hook_font_size"])
    )
    hook = " ".join(hook.split()[:5])
    # Respect configured hook width with simple word wrapping.
    words = hook.split()
    lines, line = [], ""
    max_width = int(cfg.get("hook_width", im.width - cfg["hook_x"] - 40))
    for word in words:
        candidate = f"{line} {word}".strip()
        if d.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            line = candidate
        else:
            if line: lines.append(line)
            line = word
    if line: lines.append(line)
    d.multiline_text(
        (cfg["hook_x"], cfg["hook_y"]), "\n".join(lines),
        font=font, fill="white", stroke_width=3, stroke_fill="black",
        spacing=8
    )
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    im.convert("RGB").save(out, quality=95)
