#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""爱迷茫 图标生成器 —— 休闲浪漫风。
输出：256 与 64 两档，RGBA（圆角外透明）。
"""
import math, os, sys
from PIL import Image, ImageDraw, ImageFilter, ImageChops

OUT = "/tmp/opencode/icons"
os.makedirs(OUT, exist_ok=True)

CREAM = (255, 250, 242)
RADIUS_RATIO = 0.18          # 与老图标一致


def lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def bg_gradient(S, stops):
    """对角渐变：左上 → 右下。先在低分辨率算好再放大，保证平滑。"""
    n = 512
    im = Image.new("RGB", (n, n))
    px = im.load()
    seg = len(stops) - 1
    for y in range(n):
        for x in range(n):
            t = (x + y) / (2.0 * (n - 1))
            u = min(t * seg, seg - 1e-9)
            i = int(u)
            px[x, y] = lerp(stops[i], stops[i + 1], u - i)
    return im.resize((S, S), Image.BICUBIC)


def radial(S, cx, cy, r, color, a0, power=1.6):
    """柔光：中心亮 → 边缘透明。低分辨率算完放大。"""
    n = 256
    im = Image.new("L", (n, n))
    px = im.load()
    for y in range(n):
        for x in range(n):
            d = math.hypot(x / n - cx, y / n - cy) / (r if r else 1)
            px[x, y] = 0 if d >= 1 else int(255 * a0 * (1 - d) ** power)
    im = im.resize((S, S), Image.BICUBIC)
    layer = Image.new("RGBA", (S, S), color + (0,))
    layer.putalpha(im)
    return layer


def rounded_mask(S, radius_ratio=RADIUS_RATIO):
    m = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(m)
    r = int(S * radius_ratio)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=r, fill=255)
    return m


def heart_mask(S, width_ratio, cx_ratio, cy_ratio, n=900):
    """经典心形参数方程 -> L 遮罩。"""
    pts = []
    for i in range(n):
        t = 2 * math.pi * i / n
        x = 16 * math.sin(t) ** 3
        y = (13 * math.cos(t) - 5 * math.cos(2 * t)
             - 2 * math.cos(3 * t) - math.cos(4 * t))
        pts.append((x, y))
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    w = max(xs) - min(xs)
    h = max(ys) - min(ys)
    scale = S * width_ratio / w
    cxm = (max(xs) + min(xs)) / 2.0
    cym = (max(ys) + min(ys)) / 2.0
    poly = []
    for x, y in pts:
        px_ = S * cx_ratio + (x - cxm) * scale
        py_ = S * cy_ratio - (y - cym) * scale   # 图像坐标 y 向下
        poly.append((px_, py_))
    m = Image.new("L", (S, S), 0)
    ImageDraw.Draw(m).polygon(poly, fill=255)
    return m, scale, cym, h


def play_mask(S, cx, cy, h, ratio=0.86):
    """右向播放三角。"""
    w = h * ratio
    m = Image.new("L", (S, S), 0)
    ImageDraw.Draw(m).polygon(
        [(cx - w / 2, cy - h / 2), (cx - w / 2, cy + h / 2), (cx + w / 2, cy)],
        fill=255)
    return m


def star_mask(S, cx, cy, r, thin=0.30):
    """四角星（两颗细长菱形叠加）。"""
    m = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(m)
    d.polygon([(cx, cy - r), (cx + r * thin, cy), (cx, cy + r), (cx - r * thin, cy)], fill=255)
    d.polygon([(cx - r, cy), (cx, cy + r * thin), (cx + r, cy), (cx, cy - r * thin)], fill=255)
    return m


def moon_mask(S, cx, cy, r, off):
    """月牙。"""
    m = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(m)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    cut = Image.new("L", (S, S), 0)
    ImageDraw.Draw(cut).ellipse(
        [cx - r + off[0], cy - r + off[1], cx + r + off[0], cy + r + off[1]], fill=255)
    return ImageChops.subtract(m, cut)


def compose(variant, S=1024):
    stops = [(255, 206, 150), (255, 129, 148), (167, 105, 208), (112, 84, 190)]
    bg = bg_gradient(S, stops).convert("RGBA")
    bg.alpha_composite(radial(S, 0.22, 0.16, 0.80, (255, 235, 205), 0.55))
    bg.alpha_composite(radial(S, 0.92, 0.92, 0.75, (90, 60, 170), 0.40))

    # 散景光斑
    bok = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bok)
    import random
    random.seed(7)
    for _ in range(9):
        r = random.randint(40, 150)
        x = random.randint(0, S)
        y = random.randint(0, S)
        a = random.randint(16, 42)
        bd.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, a))
    bg.alpha_composite(bok.filter(ImageFilter.GaussianBlur(28)))

    hwid = 0.54 if variant != "B" else 0.44
    hcx, hcy = (0.5, 0.535) if variant != "B" else (0.44, 0.60)

    heart, scale, cym, hh = heart_mask(S, hwid, hcx, hcy)
    # 播放三角：镂空在心的视觉中心
    tri_h = S * (0.215 if variant != "B" else 0.180)
    tri_cx = (hcx + 0.013) * S
    tri_cy = (hcy + 0.006) * S
    tri = play_mask(S, tri_cx, tri_cy, tri_h)
    content = ImageChops.subtract(heart, tri)
    content = content.filter(ImageFilter.GaussianBlur(S * 0.0016))

    # 光晕（洞内不要光，否则三角被糊掉）
    glow = content.filter(ImageFilter.GaussianBlur(S * 0.045))
    hole = tri.filter(ImageFilter.GaussianBlur(S * 0.022)).point(lambda v: 255 - int(v * 0.85))
    gl = Image.new("RGBA", (S, S), CREAM + (0,))
    gl.putalpha(ImageChops.multiply(glow.point(lambda v: int(v * 0.55)), hole))
    bg.alpha_composite(gl)

    if variant == "B":
        mo = moon_mask(S, S * 0.755, S * 0.255, S * 0.115, (S * 0.052, -S * 0.028))
        mgl = mo.filter(ImageFilter.GaussianBlur(S * 0.02))
        ml = Image.new("RGBA", (S, S), CREAM + (0,))
        ml.putalpha(mgl.point(lambda v: int(v * 0.45)))
        bg.alpha_composite(ml)
        mimg = Image.new("RGBA", (S, S), CREAM + (255,))
        mimg.putalpha(mo)
        bg.alpha_composite(mimg)

    if variant == "C":   # 背景淡月牙（不抢主体）
        mo = moon_mask(S, S * 0.790, S * 0.205, S * 0.098, (S * 0.042, -S * 0.022))
        mo = mo.filter(ImageFilter.GaussianBlur(S * 0.0035))
        ml = Image.new("RGBA", (S, S), CREAM + (0,))
        ml.putalpha(mo.point(lambda v: int(v * 0.42)))
        bg.alpha_composite(ml)

    # 主体
    fg = Image.new("RGBA", (S, S), CREAM + (0,))
    fg.putalpha(content)
    bg.alpha_composite(fg)

    # 星点
    stars = [(0.205, 0.245, 0.055), (0.795, 0.700, 0.036), (0.300, 0.815, 0.028)]
    if variant == "B":
        stars = [(0.255, 0.740, 0.045), (0.735, 0.640, 0.030)]
    st = Image.new("L", (S, S), 0)
    for sx, sy, sr in stars:
        st = ImageChops.lighter(st, star_mask(S, S * sx, S * sy, S * sr))
    st = st.filter(ImageFilter.GaussianBlur(S * 0.002))
    simg = Image.new("RGBA", (S, S), CREAM + (0,))
    simg.putalpha(st.point(lambda v: int(v * 0.95)))
    bg.alpha_composite(simg)

    # 圆角裁切
    bg.putalpha(ImageChops.multiply(bg.getchannel("A"), rounded_mask(S)))
    return bg


def save_all(name, img):
    for size in (256, 64):
        img.resize((size, size), Image.LANCZOS).save(
            os.path.join(OUT, "%s_%d.png" % (name, size)))


if __name__ == "__main__":
    for v in ("A", "B", "C"):
        im = compose(v)
        save_all("cand_" + v, im)
        print("生成 cand_%s" % v)
