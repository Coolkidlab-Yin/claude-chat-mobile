# -*- coding: utf-8 -*-
"""產生 App icon：深宇宙底 + 發光四芒星。"""
from PIL import Image, ImageDraw, ImageFilter

def star(draw, cx, cy, r_long, r_short, fill):
    pts = []
    for i in range(8):
        import math
        ang = math.pi / 4 * i - math.pi / 2
        r = r_long if i % 2 == 0 else r_short
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    draw.polygon(pts, fill=fill)

def make(size):
    img = Image.new("RGB", (size, size), "#0b0e17")
    d = ImageDraw.Draw(img)
    # 垂直漸層
    for y in range(size):
        t = y / size
        r = int(11 + t * 18)
        g = int(14 + t * 22)
        b = int(23 + t * 48)
        d.line([(0, y), (size, y)], fill=(r, g, b))
    # 散落小星
    import random
    random.seed(7)
    for _ in range(int(size * 0.12)):
        x, y = random.randint(0, size - 1), random.randint(0, size - 1)
        v = random.randint(60, 150)
        img.putpixel((x, y), (v, v, min(255, v + 40)))
    # 中央光暈
    glow = Image.new("RGB", (size, size), "#000000")
    gd = ImageDraw.Draw(glow)
    star(gd, size / 2, size / 2, size * 0.34, size * 0.10, "#7fb0ff")
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.06))
    img = Image.blend(img, Image.composite(glow, Image.new("RGB", img.size, "#000"), glow.convert("L").point(lambda p: min(255, p * 2))), 0.55)
    d = ImageDraw.Draw(img)
    star(d, size / 2, size / 2, size * 0.30, size * 0.085, "#eaf2ff")
    star(d, size / 2 + size * 0.21, size / 2 - size * 0.22, size * 0.09, size * 0.026, "#cfe0ff")
    return img

for s, name in [(512, "icon-512.png"), (192, "icon-192.png"), (180, "icon-180.png")]:
    make(s).save(f"static/{name}")
print("icons ok")
