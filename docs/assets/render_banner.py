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


def draw_right_arrow(d, x1, x2, y, line_color="#57718f", head_color="#8da8c9", width=3):
    d.line((x1, y, x2, y), fill=line_color, width=width)
    d.line((x2 - 9, y - 6, x2, y, x2 - 9, y + 6), fill=head_color, width=width)


def fit_text(d, xy, text, size, color, max_w=None, mono=False):
    f = font(size, mono)
    box = d.textbbox((0, 0), text, font=f)
    tw = box[2] - box[0]
    if max_w is not None:
        assert tw <= max_w, f"Text {text!r} width {tw}px exceeds max {max_w}px"
    d.text(xy, text, font=f, fill=color)


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
        (64, 394, "INITIATING CLI", "Plan · ask · intervene", "#73dcca"),
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
    center_text(d, w, "ARCHITECTURE · PARALLEL EXECUTION TARGETS (FAN-OUT, NEVER SERIAL)", 22, 18, "#73dcca", True)

    # Column 1: Initiating CLI / Entry Points (Parallel fan-in to coordinator)
    d.rounded_rectangle((28, 68, 366, 508), radius=16, fill="#0e1726", outline="#223047", width=1)
    d.text((50, 84), "1. INITIATING CLI", font=font(19, True), fill="#73dcca")
    d.text((50, 110), "Originating session stays in control", font=font(16), fill="#a3b1c7")

    origins = [
        (146, "POSIX Shell / Script", "python3 collab.py", "#73dcca"),
        (230, "OMP · Codex · Pi", "skill/ · OMP RPC (exp.)", "#73dcca"),
        (314, "Remote SSH Client", "remote-client.cjs / .ps1", "#8cbaff"),
        (398, "Hermes Host (Opt.)", "call_cli plugin", "#b7a5ff"),
    ]
    for top, title, sub, accent in origins:
        d.rounded_rectangle((46, top, 348, top + 72), radius=11, fill="#131f33", outline="#28364f", width=1)
        d.rounded_rectangle((60, top + 14, 64, top + 58), radius=2, fill=accent)
        d.text((78, top + 11), title, font=font(20), fill="#f3f6ff")
        d.text((78, top + 40), sub, font=font(15, True), fill="#a3b1c7")

    # Column 2: Call CLI Coordinator Hub
    d.rounded_rectangle((468, 68, 926, 508), radius=16, fill="#111b2d", outline="#384b6b", width=1)
    d.rounded_rectangle((488, 86, 492, 132), radius=2, fill="#b7a5ff")
    d.text((506, 84), "2. CALL CLI BRIDGE", font=font(19, True), fill="#b7a5ff")
    d.text((506, 110), "Single POSIX host · No daemon", font=font(16), fill="#e2e9f7")

    hub_boxes = [
        (146, "SQLite Store & Runner", "seq events · atomic turns", "#b7a5ff"),
        (230, "Live Control & Q&A", "wait · reply · dialogue · intervene", "#73dcca"),
        (314, "Pinned Skill Staging", "SHA-256 snapshot · receipts", "#8cbaff"),
        (398, "Scope & Clean Release", "--write lock · fenced close", "#b7a5ff"),
    ]
    for top, title, sub, accent in hub_boxes:
        d.rounded_rectangle((488, top, 906, top + 72), radius=11, fill="#162338", outline="#2c3d59", width=1)
        d.rounded_rectangle((502, top + 14, 506, top + 58), radius=2, fill=accent)
        d.text((520, top + 11), title, font=font(20), fill="#f3f6ff")
        d.text((520, top + 40), sub, font=font(15, True), fill="#a3b1c7")

    # Column 3: Parallel Target CLIs (Fan-out, side-by-side peers)
    d.rounded_rectangle((1046, 68, 1412, 508), radius=16, fill="#0e1726", outline="#223047", width=1)
    d.text((1068, 84), "3. PARALLEL TARGET CLIS", font=font(19, True), fill="#8cbaff")
    d.text((1068, 110), "Native autonomy · Local or SSH", font=font(16), fill="#a3b1c7")

    targets = [
        (146, "OMP", "ACP · default native-skip", "#73dcca"),
        (230, "Codex", "native-skip · Win codex-ssh", "#8cbaff"),
        (314, "Pi", "ACP · local or SSH", "#8cbaff"),
        (398, "AGY", "default ocs · 4 modes", "#b7a5ff"),
    ]
    for top, title, sub, accent in targets:
        d.rounded_rectangle((1064, top, 1394, top + 72), radius=11, fill="#131f33", outline="#28364f", width=1)
        d.rounded_rectangle((1078, top + 14, 1082, top + 58), radius=2, fill=accent)
        d.text((1096, top + 10), title, font=font(21, True), fill="#f3f6ff")
        d.text((1096, top + 40), sub, font=font(15, True), fill="#a3b1c7")

    # Bidirectional arrow between Initiating CLI (Col 1) and Bridge (Col 2)
    draw_bidi_arrow(d, 378, 456, 288)
    f_lbl = font(14, True)
    lbl_box = d.textbbox((0, 0), "JSON / seq", font=f_lbl)
    d.text((417 - (lbl_box[2] - lbl_box[0]) / 2, 260), "JSON / seq", font=f_lbl, fill="#8da8c9")

    # Parallel fan-out bus from Bridge (Col 2) to each Target CLI (Col 3)
    bus_x = 986
    d.line((934, 288, bus_x, 288), fill="#57718f", width=2)
    d.line((942, 283, 934, 288, 942, 293), fill="#8da8c9", width=2)
    target_ys = [182, 266, 350, 434]
    d.line((bus_x, target_ys[0], bus_x, target_ys[-1]), fill="#57718f", width=2)
    for y in target_ys:
        d.line((bus_x, y, 1056, y), fill="#57718f", width=2)
        d.line((1048, y - 5, 1056, y, 1048, y + 5), fill="#8da8c9", width=2)

    lbl1_box = d.textbbox((0, 0), "local / SSH", font=f_lbl)
    lbl2_box = d.textbbox((0, 0), "fan-out", font=f_lbl)
    d.text((bus_x - (lbl1_box[2] - lbl1_box[0]) / 2, 130), "local / SSH", font=f_lbl, fill="#8da8c9")
    d.text((bus_x - (lbl2_box[2] - lbl2_box[0]) / 2, 150), "fan-out", font=f_lbl, fill="#73dcca")

    center_text(d, w, "Conceptual diagram: parallel isolated sessions (never chained target-to-target) · Fresh sessions only", 522, 15, "#8da8c9", True)

    im.convert("RGB").save(ROOT / "call-cli-architecture.png", optimize=True)


def render_modes():
    w, h = 1440, 520
    im, d = base_canvas(w, h, [
        ((-180, -240, 580, 440), (16, 185, 169, 28)),
        ((500, 40, 960, 540), (183, 165, 255, 22)),
        ((940, -180, 1620, 460), (140, 186, 255, 28)),
    ])
    center_text(d, w, "AGY FOUR NATIVE MODES & PERMISSION BOUNDARIES · SINGLE-OWNER SETTLEMENT", 20, 18, "#73dcca", True)

    modes = [
        (28, 352, "#73dcca", "1. SWARM (DEFAULT: ocs)", "ocs · s (native swarm)", "Orchestrator + native Swarm", "Parallel internal workers"),
        (380, 704, "#8cbaff", "2. SINGLE", "oc · oa · 4c · 4a · 4h", "--agy-swarm off (single)", "Direct single-agent turn"),
        (732, 1056, "#b7a5ff", "3. TEAMWORK", "t  (--agy-mode teamwork)", "Native /teamwork command", "Autonomous multi-role board"),
        (1084, 1408, "#8cbaff", "4. DYNAMIC", "--agy-mode dynamic", "Native /run-workflow", "Scripted staged fan-out"),
    ]
    for left, right, accent, title, alias, sub1, sub2 in modes:
        d.rounded_rectangle((left, 64, right, 304), radius=15, fill="#111b2d", outline="#28364f", width=1)
        d.rounded_rectangle((left + 16, 84, left + 20, 130), radius=2, fill=accent)
        max_w = (right - left) - 44
        fit_text(d, (left + 30, 82), title, 18, accent, max_w=max_w, mono=True)
        fit_text(d, (left + 30, 114), alias, 16, "#e2e9f7", max_w=max_w, mono=True)
        d.line((left + 18, 156, right - 18, 156), fill="#223047", width=1)
        fit_text(d, (left + 30, 178), sub1, 19, "#f3f6ff", max_w=max_w)
        fit_text(d, (left + 30, 224), sub2, 18, "#a3b1c7", max_w=max_w)

    bounds = [
        (28, 704, "#73dcca", "APPROVAL POLICY: default native-skip",
         "Skips target CLI prompts/sandboxes (--agy-approval opts back in)",
         "Does NOT lift ACPX Shared Runtime fs/* cwd subtree limit"),
        (732, 1408, "#b7a5ff", "SCOPE & LOCK: --write vs. write: false",
         "Single-writer workspace lock (concurrent write:false readers OK)",
         "NOT an OS/filesystem sandbox · Global CLI configs preserved"),
    ]
    for left, right, accent, title, line1, line2 in bounds:
        d.rounded_rectangle((left, 330, right, 464), radius=15, fill="#0e1726", outline="#2c3d59", width=1)
        d.rounded_rectangle((left + 18, 350, left + 22, 444), radius=2, fill=accent)
        max_w = (right - left) - 56
        fit_text(d, (left + 36, 346), title, 18, accent, max_w=max_w, mono=True)
        fit_text(d, (left + 36, 382), line1, 18, "#f3f6ff", max_w=max_w)
        fit_text(d, (left + 36, 418), line2, 18, "#bcc8dd", max_w=max_w)

    center_text(d, w, "Conceptual diagram (not a live screenshot) · Single native owner settles back to Call CLI", 484, 15, "#8da8c9", True)
    im.convert("RGB").save(ROOT / "call-cli-modes.png", optimize=True)


def render_lifecycle():
    w, h = 1440, 520
    im, d = base_canvas(w, h, [
        ((-180, -240, 580, 440), (16, 185, 169, 28)),
        ((520, 40, 960, 540), (183, 165, 255, 22)),
        ((940, -180, 1620, 460), (140, 186, 255, 28)),
    ])
    center_text(d, w, "ONLINE COLLABORATION LIFECYCLE · ORIGIN-BOUND RETURN & LIVE CONTROL", 20, 18, "#73dcca", True)

    stages = [
        (32, 346, "#73dcca", "1. START & PIN", "collab.py start", [
            "Acquire device / --write lock",
            "Fresh session (native-skip)",
            "SHA-256 skill snapshot",
        ]),
        (384, 698, "#b7a5ff", "2. STREAM & REVIEW", "wait / events --after", [
            "CALLCLI_STAGE & tool events",
            "CALLCLI_QUESTION Q1..Qn",
            "600s review checkpoint",
        ]),
        (736, 1050, "#8cbaff", "3. LIVE CONTROL", "In-flight coordination", [
            "3a. reply --question-id",
            "3b. dialogue (serial on AGY)",
            "3c. intervene cancel+resume",
        ]),
        (1088, 1402, "#73dcca", "4. SETTLE & CLOSE", "Final answer + receipt", [
            "Initiator verifies result",
            "close checks no-session",
            "unknown: probe/release_only",
        ]),
    ]
    for left, right, accent, title, sub, bullets in stages:
        d.rounded_rectangle((left, 64, right, 354), radius=15, fill="#111b2d", outline="#28364f", width=1)
        d.rounded_rectangle((left + 16, 84, left + 20, 130), radius=2, fill=accent)
        max_w = (right - left) - 48
        fit_text(d, (left + 30, 82), title, 18, accent, max_w=max_w, mono=True)
        fit_text(d, (left + 30, 114), sub, 16, "#e2e9f7", max_w=max_w, mono=True)
        d.line((left + 18, 154, right - 18, 154), fill="#223047", width=1)
        for idx, line in enumerate(bullets):
            fit_text(d, (left + 26, 178 + idx * 50), line, 18, "#f3f6ff", max_w=right - left - 44)

    for x1, x2 in [(350, 380), (702, 732), (1054, 1084)]:
        draw_right_arrow(d, x1, x2, 208)

    # Return loop from Stage 3 back into Stage 2 wait --after seq
    d.line((893, 356, 893, 382), fill="#73dcca", width=2)
    d.line((541, 382, 541, 358), fill="#73dcca", width=2)
    d.line((535, 366, 541, 358, 547, 366), fill="#73dcca", width=2)
    d.rounded_rectangle((384, 382, 1050, 456), radius=13, fill="#0e1726", outline="#384b6b", width=1)
    fit_text(d, (414, 396), "LIVE LOOP: reply / dialogue / intervene feeds back to Stage 2", 18, "#73dcca", max_w=610)
    fit_text(d, (414, 424), "Caller resumes wait --after seq until task settles · never in-turn ACPX steer", 16, "#bcc8dd", max_w=610)

    center_text(d, w, "Conceptual diagram (not a live screenshot) · Initiating CLI coordinates start-to-close in one conversation", 484, 15, "#8da8c9", True)
    im.convert("RGB").save(ROOT / "call-cli-lifecycle.png", optimize=True)


def main():
    render_banner()
    render_architecture()
    render_modes()
    render_lifecycle()


if __name__ == "__main__":
    main()
