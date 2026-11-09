"""Render README artwork (optional Pillow; no runtime dependency)."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

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


def base_canvas(w, h, glows):
    im = Image.new("RGB", (w, h), "#0b1120")
    glow = Image.new("RGBA", (w, h))
    g = ImageDraw.Draw(glow)
    for box, color in glows:
        g.ellipse(box, fill=color)
    im = Image.alpha_composite(im.convert("RGBA"), glow.filter(ImageFilter.GaussianBlur(85)))
    d = ImageDraw.Draw(im)
    for x in range(0, w, 44):
        d.line((x, 0, x, h), fill="#142033")
    for y in range(0, h, 44):
        d.line((0, y, w, y), fill="#142033")
    return im, d


def center_text(d, w, text, y, size, color, mono=False):
    f = font(size, mono)
    box = d.textbbox((0, 0), text, font=f)
    d.text(((w - (box[2] - box[0])) / 2, y), text, font=f, fill=color)


def draw_bidi_arrow(d, x1, x2, y, line_color="#57718f", head_color="#8da8c9"):
    d.line((x1, y, x2, y), fill=line_color, width=2)
    d.line((x2 - 8, y - 5, x2, y, x2 - 8, y + 5), fill=head_color, width=2)
    d.line((x1 + 8, y - 5, x1, y, x1 + 8, y + 5), fill=head_color, width=2)


def render_banner():
    w, h = 1440, 440
    im, d = base_canvas(w, h, [
        ((-230, -370, 630, 390), (16, 185, 169, 32)),
        ((950, -210, 1660, 470), (119, 91, 255, 30)),
    ])
    center_text(d, w, "CROSS-CLI & CROSS-DEVICE COLLABORATION", 30, 17, "#73dcca", True)
    center_text(d, w, "Call CLI", 56, 86, "#f3f6ff")
    center_text(d, w, "Different CLIs. One conversation.", 172, 29, "#bcc8dd")
    center_text(d, w, "OMP  /  Codex  /  Pi  /  AGY     +     Hermes optional", 224, 18, "#a3b1c7", True)

    cards = [
        (64, 394, "INITIATING CLI", "Plan · ask · steer", "#73dcca"),
        (516, 924, "CALL CLI BRIDGE", "Seq events · Q&A · skills", "#b7a5ff"),
        (1046, 1376, "PARALLEL TARGET CLIS", "OMP · Codex · Pi · AGY", "#8cbaff"),
    ]
    for left, right, title, subtitle, accent in cards:
        d.rounded_rectangle((left, 294, right, 400), radius=15,
                            fill="#111b2d", outline="#28364f", width=1)
        d.rounded_rectangle((left + 22, 318, left + 26, 375), radius=2, fill=accent)
        d.text((left + 44, 312), title, font=font(16, True), fill=accent)
        d.text((left + 44, 344), subtitle, font=font(21), fill="#e2e9f7")

    for left, right in [(414, 496), (944, 1026)]:
        draw_bidi_arrow(d, left, right, 347)

    im.convert("RGB").save(ROOT / "call-cli-banner.png", optimize=True)


def render_architecture():
    w, h = 1440, 560
    im, d = base_canvas(w, h, [
        ((-180, -240, 580, 480), (16, 185, 169, 28)),
        ((520, 60, 960, 580), (183, 165, 255, 22)),
        ((940, -180, 1620, 520), (140, 186, 255, 28)),
    ])
    center_text(d, w, "ARCHITECTURE · PARALLEL EXECUTION TARGETS (FAN-OUT, NEVER SERIAL CHAINING)", 24, 15, "#73dcca", True)

    # Column 1: Initiating CLI / Entry Points (Parallel fan-in to coordinator)
    d.rounded_rectangle((36, 68, 384, 508), radius=16, fill="#0e1726", outline="#223047", width=1)
    d.text((60, 86), "1. INITIATING ENTRY", font=font(16, True), fill="#73dcca")
    d.text((60, 110), "One active caller owns the task", font=font(15), fill="#a3b1c7")

    origins = [
        (146, "Terminal / Script", "python3 collab.py (--client)", "#73dcca"),
        (230, "OMP · Codex · Pi", "skill/ bundle + CLI entry", "#73dcca"),
        (314, "Remote Client (SSH)", "remote-client.cjs / .ps1", "#8cbaff"),
        (398, "Hermes Host (Optional)", "call_cli plugin + origin bind", "#b7a5ff"),
    ]
    for top, title, sub, accent in origins:
        d.rounded_rectangle((56, top, 364, top + 72), radius=11, fill="#131f33", outline="#28364f", width=1)
        d.rounded_rectangle((70, top + 14, 74, top + 58), radius=2, fill=accent)
        d.text((88, top + 13), title, font=font(17), fill="#f3f6ff")
        d.text((88, top + 41), sub, font=font(13, True), fill="#a3b1c7")

    # Column 2: Call CLI Coordinator Hub
    d.rounded_rectangle((468, 68, 932, 508), radius=16, fill="#111b2d", outline="#384b6b", width=1)
    d.rounded_rectangle((490, 88, 494, 132), radius=2, fill="#b7a5ff")
    d.text((508, 86), "2. CALL CLI COORDINATOR", font=font(16, True), fill="#b7a5ff")
    d.text((508, 110), "Single POSIX host · No always-on daemon", font=font(15), fill="#e2e9f7")

    hub_boxes = [
        (146, "SQLite Store & Turn Runner", "bridge.py · runner.py · seq cursors", "#b7a5ff"),
        (230, "Live Control & Settlement", "wait · reply · cancel+resume intervene", "#73dcca"),
        (314, "Pinned Skill Snapshots", "origin/target SHA-256 · CALLCLI_SKILL", "#8cbaff"),
        (398, "Writer Lock & Backends", "--write / --resource · ACPX 0.22+ / custom", "#b7a5ff"),
    ]
    for top, title, sub, accent in hub_boxes:
        d.rounded_rectangle((490, top, 910, top + 72), radius=11, fill="#162338", outline="#2c3d59", width=1)
        d.rounded_rectangle((504, top + 14, 508, top + 58), radius=2, fill=accent)
        d.text((522, top + 13), title, font=font(17), fill="#f3f6ff")
        d.text((522, top + 41), sub, font=font(13, True), fill="#a3b1c7")

    # Column 3: Parallel Target CLIs (Fan-out, side-by-side peers)
    d.rounded_rectangle((1036, 68, 1404, 508), radius=16, fill="#0e1726", outline="#223047", width=1)
    d.text((1060, 86), "3. PARALLEL TARGET CLIS", font=font(16, True), fill="#8cbaff")
    d.text((1060, 110), "Independent sessions · Local or SSH", font=font(15), fill="#a3b1c7")

    targets = [
        (146, "OMP", "omp acp · default approve-reads", "#73dcca"),
        (230, "Codex", "ACP adapter / Win codex-ssh.cjs", "#8cbaff"),
        (314, "Pi", "ACP adapter · local or over SSH", "#8cbaff"),
        (398, "AGY", "agy acp · model ID · approve-all", "#b7a5ff"),
    ]
    for top, title, sub, accent in targets:
        d.rounded_rectangle((1056, top, 1384, top + 72), radius=11, fill="#131f33", outline="#28364f", width=1)
        d.rounded_rectangle((1070, top + 14, 1074, top + 58), radius=2, fill=accent)
        d.text((1088, top + 12), title, font=font(18, True), fill="#f3f6ff")
        d.text((1162, top + 15), "parallel target", font=font(12, True), fill=accent)
        d.text((1088, top + 41), sub, font=font(12, True), fill="#a3b1c7")

    # Bidirectional arrow between Initiating Entry (Col 1) and Coordinator (Col 2)
    draw_bidi_arrow(d, 396, 456, 288)
    f_lbl = font(12, True)
    lbl_box = d.textbbox((0, 0), "JSON / seq", font=f_lbl)
    d.text((426 - (lbl_box[2] - lbl_box[0]) / 2, 262), "JSON / seq", font=f_lbl, fill="#8da8c9")

    # Parallel fan-out bus from Coordinator (Col 2) to each Target CLI (Col 3)
    bus_x = 984
    d.line((936, 288, bus_x, 288), fill="#57718f", width=2)
    d.line((944, 283, 936, 288, 944, 293), fill="#8da8c9", width=2)
    target_ys = [182, 266, 350, 434]
    d.line((bus_x, target_ys[0], bus_x, target_ys[-1]), fill="#57718f", width=2)
    for y in target_ys:
        d.line((bus_x, y, 1048, y), fill="#57718f", width=2)
        d.line((1040, y - 5, 1048, y, 1040, y + 5), fill="#8da8c9", width=2)

    d.text((942, 136), "local / SSH", font=font(12, True), fill="#8da8c9")
    d.text((942, 154), "fan-out", font=font(12, True), fill="#73dcca")

    center_text(d, w, "Conceptual architecture: targets run in parallel isolated sessions (never chained target-to-target) · Fresh sessions only, no manual terminal takeover", 524, 13, "#8da8c9", True)

    im.convert("RGB").save(ROOT / "call-cli-architecture.png", optimize=True)


def main():
    render_banner()
    render_architecture()


if __name__ == "__main__":
    main()
