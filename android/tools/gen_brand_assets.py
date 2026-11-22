#!/usr/bin/env python3
"""生成 SameCast 的品牌素材：应用图标（各密度）与 Android TV 横幅。

用法：python3 android/tools/gen_brand_assets.py
依赖 Pillow；生成结果直接写进 android/app/src/main/res/。
"""
from __future__ import annotations

import os
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "app", "src", "main", "res")

BG_TOP = (10, 16, 32)
BG_BOTTOM = (27, 44, 71)
ACCENT = (124, 224, 255)
BACK_LINE = (92, 124, 158)
SCREEN_FILL = (15, 34, 56)


def _lerp(a: int, b: int, t: float) -> int:
    return int(round(a + (b - a) * t))


def draw_mark(size: int, round_icon: bool = False) -> Image.Image:
    """画一个 size x size 的图标：两块屏幕 + 播放三角。"""
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 背景：竖向渐变 + 圆角（圆形图标用正圆）
    grad = Image.new("RGBA", (s, s))
    gd = ImageDraw.Draw(grad)
    for y in range(s):
        t = y / max(1, s - 1)
        gd.line(
            [(0, y), (s, y)],
            fill=(
                _lerp(BG_TOP[0], BG_BOTTOM[0], t),
                _lerp(BG_TOP[1], BG_BOTTOM[1], t),
                _lerp(BG_TOP[2], BG_BOTTOM[2], t),
                255,
            ),
        )
    mask = Image.new("L", (s, s), 0)
    md = ImageDraw.Draw(mask)
    if round_icon:
        md.ellipse([0, 0, s - 1, s - 1], fill=255)
    else:
        md.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.24), fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)

    # 后面那块屏幕：只描边，暗示“多屏”
    back = [int(s * 0.20), int(s * 0.18), int(s * 0.70), int(s * 0.60)]
    d.rounded_rectangle(
        back, radius=int(s * 0.06), outline=BACK_LINE + (255,), width=max(2, int(s * 0.035))
    )

    # 前面那块屏幕：深色填充 + 高亮描边
    front = [int(s * 0.32), int(s * 0.32), int(s * 0.84), int(s * 0.80)]
    d.rounded_rectangle(
        front,
        radius=int(s * 0.07),
        fill=SCREEN_FILL + (255,),
        outline=ACCENT + (255,),
        width=max(2, int(s * 0.045)),
    )

    # 播放三角
    cx = (front[0] + front[2]) / 2 + s * 0.012
    cy = (front[1] + front[3]) / 2
    half_w, half_h = s * 0.075, s * 0.105
    d.polygon(
        [(cx - half_w * 0.6, cy - half_h), (cx - half_w * 0.6, cy + half_h), (cx + half_w * 1.1, cy)],
        fill=ACCENT + (255,),
    )
    return img.resize((size, size), Image.LANCZOS)


def make_banner(path: str) -> None:
    w, h = 320 * 4, 180 * 4
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / (h - 1)
        d.line(
            [(0, y), (w, y)],
            fill=(
                _lerp(BG_TOP[0], BG_BOTTOM[0], t),
                _lerp(BG_TOP[1], BG_BOTTOM[1], t),
                _lerp(BG_TOP[2], BG_BOTTOM[2], t),
                255,
            ),
        )

    mark = draw_mark(104 * 4)
    img.alpha_composite(mark, (28 * 4, 38 * 4))

    # 右侧文字自动缩放，保证不越界（横幅右侧留 16px 边距）
    text_left = 150 * 4
    max_w = (320 - 16) * 4 - text_left

    def fit_font(path: str, index: int, start_px: int, text: str) -> ImageFont.FreeTypeFont:
        size = start_px * 4
        while size > 8:
            f = ImageFont.truetype(path, size, index=index)
            if d.textlength(text, font=f) <= max_w:
                return f
            size -= 2
        return ImageFont.truetype(path, 8, index=index)

    title_font = fit_font("/System/Library/Fonts/Helvetica.ttc", 1, 42, "SameCast")
    sub_font = fit_font("/System/Library/Fonts/Hiragino Sans GB.ttc", 0, 19, "局域网多屏同步投屏")
    d.text((text_left, 42 * 4), "SameCast", font=title_font, fill=(241, 247, 255, 255))
    d.text((text_left + 2 * 4, 102 * 4), "局域网多屏同步投屏", font=sub_font, fill=(127, 166, 200, 255))

    img.resize((320, 180), Image.LANCZOS).save(path)


def main() -> None:
    densities = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
    for name, px in densities.items():
        folder = os.path.join(RES, "mipmap-" + name)
        os.makedirs(folder, exist_ok=True)
        draw_mark(px).save(os.path.join(folder, "ic_launcher.png"))
        draw_mark(px, round_icon=True).save(os.path.join(folder, "ic_launcher_round.png"))

    banner_dir = os.path.join(RES, "drawable-xhdpi")
    os.makedirs(banner_dir, exist_ok=True)
    make_banner(os.path.join(banner_dir, "app_banner.png"))
    print("generated launcher icons + app_banner.png")


if __name__ == "__main__":
    main()
