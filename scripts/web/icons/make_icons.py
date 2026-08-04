"""Generates the Vault Chat app icon set (PNG sizes + favicon.ico) with Pillow.

Run once with: C:/Python313/python.exe make_icons.py
Regenerate after editing the colors/shape constants below; there is no SVG
source of truth because no SVG rasterizer (cairo/resvg) is available in this
environment.
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).parent
S = 1024  # master render size, downsampled afterward for crisp edges

BG_TOP = (34, 34, 34)
BG_BOTTOM = (22, 22, 22)
BUBBLE_TOP = (63, 116, 181)
BUBBLE_BOTTOM = (37, 74, 128)
KEY_COLOR = (238, 243, 250)


def diagonal_gradient(size, top_left, bottom_right):
    grad = Image.new("RGB", (size, size))
    px = grad.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * size)
            r = round(top_left[0] + (bottom_right[0] - top_left[0]) * t)
            g = round(top_left[1] + (bottom_right[1] - top_left[1]) * t)
            b = round(top_left[2] + (bottom_right[2] - top_left[2]) * t)
            px[x, y] = (r, g, b)
    return grad


def rounded_rect_mask(size, box, radius):
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle(box, radius=radius, fill=255)
    return mask


def build_master():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # background: rounded square, diagonal dark gradient
    bg_grad = diagonal_gradient(S, BG_TOP, BG_BOTTOM).convert("RGBA")
    bg_mask = rounded_rect_mask(S, (0, 0, S - 1, S - 1), radius=int(S * 0.22))
    img.paste(bg_grad, (0, 0), bg_mask)

    # chat bubble: rounded rect + small tail triangle, blue gradient
    bubble_mask = Image.new("L", (S, S), 0)
    bd = ImageDraw.Draw(bubble_mask)
    bx0, by0, bx1, by1 = int(S * 0.132), int(S * 0.172), int(S * 0.868), int(S * 0.79)
    bd.rounded_rectangle((bx0, by0, bx1, by1), radius=int(S * 0.27), fill=255)
    # tail pointing down-left from the bubble, tucked under the main body
    tail = [
        (int(S * 0.305), int(S * 0.706)),
        (int(S * 0.46), int(S * 0.706)),
        (int(S * 0.255), int(S * 0.876)),
    ]
    bd.polygon(tail, fill=255)

    bubble_grad = diagonal_gradient(S, BUBBLE_TOP, BUBBLE_BOTTOM).convert("RGBA")
    img.paste(bubble_grad, (0, 0), bubble_mask)

    # keyhole (vault lock) centered in the upper bubble
    kd = ImageDraw.Draw(img)
    cx, cy = S * 0.5, S * 0.408
    r = S * 0.082
    kd.ellipse((cx - r, cy - r, cx + r, cy + r), fill=KEY_COLOR)
    shaft = [
        (cx - r * 0.62, cy + r * 0.62),
        (cx + r * 0.62, cy + r * 0.62),
        (cx + r * 1.28, cy + r * 2.55),
        (cx, cy + r * 3.05),
        (cx - r * 1.28, cy + r * 2.55),
    ]
    kd.polygon(shaft, fill=KEY_COLOR)

    return img


def export_all(master):
    master.save(OUT / "icon-master.png")

    png_sizes = [16, 32, 48, 64, 128, 180, 192, 256, 384, 512]
    for sz in png_sizes:
        resized = master.resize((sz, sz), Image.LANCZOS)
        resized.save(OUT / f"icon-{sz}.png")

    # favicon.ico bundling common sizes
    ico_sizes = [16, 32, 48]
    imgs = [master.resize((sz, sz), Image.LANCZOS) for sz in ico_sizes]
    imgs[0].save(
        OUT / "favicon.ico",
        format="ICO",
        sizes=[(s, s) for s in ico_sizes],
        append_images=imgs[1:],
    )

    # apple-touch-icon: iOS ignores alpha and fills transparency with black,
    # so flatten onto the background color first.
    apple = Image.new("RGB", master.size, BG_BOTTOM)
    apple.paste(master, (0, 0), master)
    apple.resize((180, 180), Image.LANCZOS).save(OUT / "apple-touch-icon.png")


if __name__ == "__main__":
    m = build_master()
    export_all(m)
    print("done")
