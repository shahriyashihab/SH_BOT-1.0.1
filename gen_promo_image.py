"""Generate a promo banner image for the Telegram Session Verification Bot post.

Run: python gen_promo_image.py
Output: bot_promo_image.png  (1200x675, Telegram-friendly 16:9)
"""
import math
import os
import random

from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H = 1200, 675
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_promo_image.png")

BG_TOP = (12, 18, 38)
BG_BOTTOM = (8, 34, 60)
ACCENT = (34, 211, 238)
ACCENT2 = (59, 130, 246)
GREEN = (34, 197, 94)
WHITE = (240, 248, 255)
MUTED = (148, 180, 210)


def load_font(size, bold=False):
    candidates = [
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/calibrib.ttf" if bold else "C:/Windows/Fonts/calibri.ttf",
    ]
    for path in candidates:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def vertical_gradient():
    img = Image.new("RGB", (W, H), BG_TOP)
    px = img.load()
    for y in range(H):
        t = y / (H - 1)
        r = int(BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * t)
        g = int(BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * t)
        b = int(BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * t)
        for x in range(W):
            px[x, y] = (r, g, b)
    return img


def add_glow(img, center, radius, color, max_alpha=90):
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(glow)
    steps = 40
    for i in range(steps, 0, -1):
        r = int(radius * i / steps)
        a = int(max_alpha * (1 - i / steps) ** 1.6)
        d.ellipse(
            [center[0] - r, center[1] - r, center[0] + r, center[1] + r],
            fill=color + (a,),
        )
    glow = glow.filter(ImageFilter.GaussianBlur(30))
    img.alpha_composite(glow)


def draw_grid(draw):
    step = 45
    for x in range(0, W, step):
        draw.line([(x, 0), (x, H)], fill=(255, 255, 255, 8))
    for y in range(0, H, step):
        draw.line([(0, y), (W, y)], fill=(255, 255, 255, 8))


def rounded_panel(draw, box, radius=22, fill=(255, 255, 255, 18),
                  outline=(120, 200, 255, 90), width=2):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def main():
    base = vertical_gradient().convert("RGBA")
    add_glow(base, (150, 90), 420, ACCENT2)
    add_glow(base, (W - 120, H - 80), 460, ACCENT)

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    draw_grid(od)
    base.alpha_composite(overlay)

    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    # --- Top badge -----------------------------------------------------
    badge_font = load_font(22, bold=True)
    badge_txt = "PRODUCTION-READY  •  ENTERPRISE GRADE"
    bw = d.textlength(badge_txt, font=badge_font)
    d.rounded_rectangle([60, 48, 60 + bw + 48, 100], radius=26,
                        fill=(34, 211, 238, 40), outline=ACCENT, width=2)
    d.text((84, 60), badge_txt, font=badge_font, fill=ACCENT)

    # --- Title ---------------------------------------------------------
    title_font = load_font(64, bold=True)
    sub_font = load_font(30, bold=True)
    d.text((60, 140), "TELEGRAM", font=title_font, fill=WHITE)
    d.text((60, 212), "SESSION VERIFICATION", font=title_font, fill=WHITE)
    d.text((60, 284), "BOT", font=title_font, fill=ACCENT)

    d.text((62, 372), "Auto-Verify  •  Spam Check  •  API Rotation  •  High Scale",
           font=sub_font, fill=MUTED)

    # --- Feature chips -------------------------------------------------
    chip_font = load_font(22, bold=True)
    chips = [
        ("Multi-Layer Verify", GREEN),
        ("TRUE Round-Robin APIs", ACCENT),
        ("Hybrid Proxy Engine", ACCENT2),
        ("Atomic Balance", GREEN),
        ("Anti-Freeze v4.0", ACCENT),
        ("15-25K / Day", ACCENT2),
    ]
    x, y = 60, 440
    for label, col in chips:
        tw = d.textlength(label, font=chip_font)
        box = [x, y, x + tw + 34, y + 44]
        if box[2] > W - 60:
            x = 60
            y += 56
            box = [x, y, x + tw + 34, y + 44]
        d.rounded_rectangle(box, radius=12, fill=col + (35,),
                            outline=col, width=2)
        d.text((x + 17, y + 10), label, font=chip_font, fill=WHITE)
        x = box[2] + 14

    base.alpha_composite(layer)

    # --- Small phone / lock illustration on the right ------------------
    art = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ad = ImageDraw.Draw(art)
    px, py = 900, 150
    rounded_panel(ad, [px, py, px + 210, py + 360], radius=28,
                  fill=(8, 24, 48, 170), outline=ACCENT, width=3)
    ad.rounded_rectangle([px + 88, py + 14, px + 122, py + 22], radius=4,
                         fill=ACCENT)

    # shield / check mark inside phone
    scx, scy = px + 105, py + 180
    pts = []
    for i in range(3):
        ang = math.radians(-90 + i * 120)
        pts.append((scx + 78 * math.cos(ang), scy + 78 * math.sin(ang)))
    ad.polygon(pts, fill=(34, 197, 94, 55), outline=GREEN)
    ad.line([(scx - 34, scy + 2), (scx - 8, scy + 30), (scx + 40, scy - 28)],
            fill=GREEN, width=12, joint="curve")

    for i in range(3):
        ly = py + 300 + i * 0
    ad.line([(px + 36, py + 320), (px + 174, py + 320)],
            fill=(120, 200, 255, 90), width=3)
    ad.line([(px + 36, py + 336), (px + 140, py + 336)],
            fill=(120, 200, 255, 60), width=3)
    base.alpha_composite(art)

    # --- Bottom footer -------------------------------------------------
    foot = load_font(20)
    d2 = ImageDraw.Draw(base)
    d2.line([(60, H - 70), (W - 60, H - 70)], fill=(120, 200, 255, 70), width=1)
    d2.text((60, H - 56), "Secure  •  Scalable  •  Crash-Safe Recovery  •  24/7",
            font=foot, fill=MUTED)

    # subtle noise
    noise = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    nd = noise.load()
    for _ in range(9000):
        nx = random.randint(0, W - 1)
        ny = random.randint(0, H - 1)
        v = random.randint(0, 26)
        nd[nx, ny] = (255, 255, 255, v)
    base.alpha_composite(noise)

    base.convert("RGB").save(OUT, "PNG")
    print("Saved:", OUT)


if __name__ == "__main__":
    main()
