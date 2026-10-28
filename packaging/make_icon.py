"""Deterministic monitor/link app icon; generated locally during native builds."""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw


def create_icon(output: Path) -> Path:
    if output.suffix.lower() not in (".ico", ".icns"):
        raise ValueError("The native icon must be .ico or .icns")
    image = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((12, 12, 1012, 1012), radius=216, fill="#101722")
    for left, color in ((140, "#4bc6b2"), (584, "#73a5e6")):
        draw.rounded_rectangle((left, 294, left + 300, 624), radius=34, fill=color)
        draw.rounded_rectangle((left + 28, 322, left + 272, 568), radius=12, fill="#172b3c")
        draw.line((left + 150, 624, left + 150, 716), fill=color, width=38)
        draw.rounded_rectangle((left + 73, 700, left + 227, 736), radius=18, fill=color)
    draw.line((421, 454, 599, 454), fill="#f2f7ff", width=32)
    draw.polygon(((567, 411), (624, 454), (567, 497)), fill="#f2f7ff")
    draw.line((602, 522, 424, 522), fill="#f2f7ff", width=20)
    draw.polygon(((455, 489), (408, 522), (455, 555)), fill="#f2f7ff")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix.lower() == ".ico":
        image.save(output, format="ICO", sizes=[(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)])
    else:
        image.save(output, format="ICNS")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(create_icon(parser.parse_args().output))
