"""Render the original README artwork (optional Pillow; no runtime dependency)."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H = 1440, 440
ROOT = Path(__file__).resolve().parent

def font(size, mono=False):
    choices = (["/System/Library/Fonts/Menlo.ttc", "DejaVuSansMono.ttf"] if mono else
               ["/System/Library/Fonts/Avenir Next.ttc", "DejaVuSans.ttf"])
    for name in choices:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)

def main():
    im = Image.new("RGB", (W, H), "#0b1120")
    glow = Image.new("RGBA", (W, H))
    g = ImageDraw.Draw(glow)
    g.ellipse((-230, -370, 630, 390), fill=(16, 185, 169, 32))
    g.ellipse((950, -210, 1660, 470), fill=(119, 91, 255, 30))
    im = Image.alpha_composite(im.convert("RGBA"), glow.filter(ImageFilter.GaussianBlur(85)))
    d = ImageDraw.Draw(im)
    for x in range(0, W, 44):
        d.line((x, 0, x, H), fill="#142033")
    for y in range(0, H, 44):
        d.line((0, y, W, y), fill="#142033")
    def center(text, y, size, color, mono=False):
        f = font(size, mono)
        box = d.textbbox((0, 0), text, font=f)
        d.text(((W-(box[2]-box[0]))/2, y), text, font=f, fill=color)
    center("CROSS-CLI COLLABORATION", 32, 17, "#73dcca", True)
    center("Call CLI", 58, 88, "#f3f6ff")
    center("Different CLIs. One conversation.", 174, 29, "#bcc8dd")
    center("OMP  /  Codex  /  Pi     +     Hermes optional", 225, 18, "#a3b1c7", True)
    cards = [(72, 380, "YOUR CLI", "Plan and coordinate", "#73dcca"),
             (532, 908, "CALL CLI", "Ask · follow · steer", "#b7a5ff"),
             (1060, 1368, "OTHER CLIS", "Local or over SSH", "#8cbaff")]
    for left, right, title, subtitle, accent in cards:
        d.rounded_rectangle((left, 296, right, 400), radius=15,
                            fill="#111b2d", outline="#28364f", width=1)
        d.rounded_rectangle((left+22, 320, left+26, 373), radius=2, fill=accent)
        d.text((left+46, 313), title, font=font(17, True), fill=accent)
        d.text((left+46, 345), subtitle, font=font(21), fill="#e2e9f7")
    for left, right in [(402, 510), (930, 1038)]:
        y = 348
        d.line((left, y, right, y), fill="#57718f", width=2)
        d.line((right-8, y-5, right, y, right-8, y+5), fill="#8da8c9", width=2)
        d.line((left+8, y-5, left, y, left+8, y+5), fill="#8da8c9", width=2)
    im.convert("RGB").save(ROOT/"call-cli-banner.png", optimize=True)

if __name__ == "__main__":
    main()
