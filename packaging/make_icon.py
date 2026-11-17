"""Рисует иконку программы: стопка карточек-фотографий на синем фоне.

python packaging/make_icon.py  →  autosort/data/icon.png и packaging/autosort.ico
"""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
S = 1024


def card(size, fill, inner):
    w, h = size
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=w // 12, fill=fill)
    m = w // 14
    d.rounded_rectangle((m, m, w - m, h - m * 3), radius=w // 20, fill=inner)
    # «горы» и «солнце» на верхней карточке
    d.polygon([(m, h - m * 3), (w * 0.38, h * 0.42), (w * 0.62, h - m * 3)], fill=(255, 255, 255, 170))
    d.polygon([(w * 0.45, h - m * 3), (w * 0.68, h * 0.52), (w - m, h - m * 3)], fill=(255, 255, 255, 120))
    r = w // 10
    d.ellipse((w * 0.7 - r, h * 0.28 - r, w * 0.7 + r, h * 0.28 + r), fill=(255, 214, 92, 255))
    return img


def main():
    icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    bg = Image.new("RGBA", (S, S))
    top, bottom = (64, 132, 255), (118, 72, 230)
    for y in range(S):
        t = y / S
        ImageDraw.Draw(bg).line([(0, y), (S, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((40, 40, S - 40, S - 40), radius=200, fill=255)
    icon.paste(bg, (0, 0), mask)

    size = (520, 600)
    for angle, offset, inner in ((-14, (-90, 20), (255, 140, 120, 255)),
                                 (8, (80, -10), (120, 210, 160, 255)),
                                 (0, (0, 0), (90, 160, 255, 255))):
        c = card(size, (255, 255, 255, 255), inner).rotate(angle, expand=True, resample=Image.BICUBIC)
        x = (S - c.width) // 2 + offset[0]
        y = (S - c.height) // 2 + offset[1] + 10
        icon.alpha_composite(c, (x, y))

    (ROOT / "autosort" / "data").mkdir(parents=True, exist_ok=True)
    icon.resize((256, 256), Image.LANCZOS).save(ROOT / "autosort" / "data" / "icon.png")
    icon.save(ROOT / "packaging" / "autosort.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                                                          (128, 128), (256, 256)])


if __name__ == "__main__":
    main()
