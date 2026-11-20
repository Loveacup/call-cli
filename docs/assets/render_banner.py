"""Render README artwork (optional Pillow; no runtime dependency)."""
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError:  # pragma: no cover - Pillow is only needed for artwork regeneration
    Image = ImageDraw = ImageFilter = ImageFont = None

ROOT = Path(__file__).resolve().parent

# Palette
BG = "#0b1120"
GRID = "#131f33"
CARD_DARK = "#0e1726"
CARD = "#111b2d"
CARD_INNER = "#152238"
CARD_AMBER = "#181e2e"
BORDER = "#28364f"
BORDER_BRIGHT = "#384b6b"

TEAL = "#73dcca"
LAVENDER = "#b7a5ff"
BLUE = "#8cbaff"
AMBER = "#f5c27a"
LINE_MUTED = "#6581a3"

TEXT_PRIMARY = "#f3f6ff"
TEXT_SECONDARY = "#d1dcf0"
TEXT_CAPTION = "#98abc4"

# Verified macOS system font paths & face indices
FONT_PATHS = {
    "en_sans": "/System/Library/Fonts/Avenir Next.ttc",
    "zh_sans": "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "zh_fallback": "/System/Library/Fonts/STHeiti Medium.ttc",
    "mono": "/System/Library/Fonts/Menlo.ttc",
}

FONT_INDICES = {
    "en_bold": 0,      # ('Avenir Next', 'Bold')
    "en_demi": 2,      # ('Avenir Next', 'Demi Bold')
    "en_medium": 5,    # ('Avenir Next', 'Medium')
    "en_regular": 7,   # ('Avenir Next', 'Regular')
    "zh_regular": 0,   # ('Hiragino Sans GB', 'W3')
    "zh_bold": 2,      # ('Hiragino Sans GB', 'W6')
    "zh_fb": 1,        # ('Heiti SC', 'Medium')
    "mono_regular": 0, # ('Menlo', 'Regular')
    "mono_bold": 1,    # ('Menlo', 'Bold')
}

MIN_FONT_SIZES = {
    "title": 28,
    "heading": 24,
    "body": 20,
    "caption": 17,
}
MIN_HPAD = 16


def require_pillow():
    if Image is None:
        raise SystemExit("Pillow is required to regenerate README artwork.")


def get_font(size, lang="en", weight="body", mono=False):
    """Load a verified font face with explicit TTC index selection."""
    if mono:
        idx = FONT_INDICES["mono_bold"] if weight in ("bold", "demi", "heading") else FONT_INDICES["mono_regular"]
        candidates = [(FONT_PATHS["mono"], idx), ("DejaVuSansMono.ttf", 0)]
    elif lang == "zh":
        if weight in ("bold", "demi", "heading", "title"):
            candidates = [
                (FONT_PATHS["zh_sans"], FONT_INDICES["zh_bold"]),
                (FONT_PATHS["zh_fallback"], FONT_INDICES["zh_fb"]),
            ]
        else:
            candidates = [
                (FONT_PATHS["zh_sans"], FONT_INDICES["zh_regular"]),
                (FONT_PATHS["zh_fallback"], FONT_INDICES["zh_fb"]),
            ]
    else:
        idx_key = {
            "title": "en_bold",
            "bold": "en_bold",
            "heading": "en_demi",
            "demi": "en_demi",
            "body": "en_medium",
            "medium": "en_medium",
            "regular": "en_regular",
            "caption": "en_medium",
        }.get(weight, "en_medium")
        candidates = [(FONT_PATHS["en_sans"], FONT_INDICES[idx_key]), ("DejaVuSans.ttf", 0)]

    for path, idx in candidates:
        try:
            return ImageFont.truetype(path, size, index=idx)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def make_font(size, role="body", lang="en", weight=None, mono=False):
    min_sz = MIN_FONT_SIZES[role]
    assert size >= min_sz, f"Font size {size}px for role={role!r} violates minimum {min_sz}px"
    return get_font(size, lang=lang, weight=weight or role, mono=mono)


def pick_font(size, text, role="body", lang="en", weight=None, mono=False):
    """Use crisp Avenir Next for pure-ASCII strings even inside ZH diagrams."""
    effective_lang = "en" if text.isascii() else lang
    return make_font(size, role=role, lang=effective_lang, weight=weight, mono=mono)


def base_canvas(w, h, glows):
    im = Image.new("RGB", (w, h), BG).convert("RGBA")
    glow = Image.new("RGBA", (w, h))
    g = ImageDraw.Draw(glow)
    for box, color in glows:
        g.ellipse(box, fill=color)
    im = Image.alpha_composite(im, glow.filter(ImageFilter.GaussianBlur(88)))
    d = ImageDraw.Draw(im)
    for x in range(0, w, 44):
        d.line((x, 0, x, h), fill=GRID, width=1)
    for y in range(0, h, 44):
        d.line((0, y, w, y), fill=GRID, width=1)
    return im, d


def measure(d, text, f):
    x0, y0, x1, y1 = d.textbbox((0, 0), text, font=f)
    return x1 - x0, y1 - y0, x0, y0, x1, y1


def center_text(d, w, text, y, f, color, max_w=None):
    tw, th, x0, y0, _, _ = measure(d, text, f)
    limit = w - 2 * MIN_HPAD if max_w is None else max_w
    assert tw <= limit, f"Centered text {text!r} width {tw}px exceeds max {limit}px"
    d.text(((w - tw) / 2 - x0, y - y0), text, font=f, fill=color)
    return th


def fit_text(d, xy, text, f, color, max_w, align="left"):
    tw, th, x0, y0, _, _ = measure(d, text, f)
    assert tw <= max_w, f"Text {text!r} width {tw}px exceeds max {max_w}px"
    x, y = xy
    if align == "center":
        x = x + (max_w - tw) / 2
    elif align == "right":
        x = x + (max_w - tw)
    d.text((x - x0, y - y0), text, font=f, fill=color)
    return th


def draw_text_centered_in_box(d, box, text, f, color, pad_x=MIN_HPAD):
    left, top, right, bottom = box
    assert pad_x >= MIN_HPAD, f"Horizontal padding {pad_x}px < {MIN_HPAD}px"
    max_w = (right - left) - 2 * pad_x
    tw, th, x0, y0, _, _ = measure(d, text, f)
    assert tw <= max_w, f"Text {text!r} width {tw}px exceeds box limit {max_w}px"
    tx = left + (right - left - tw) / 2 - x0
    ty = top + (bottom - top - th) / 2 - y0
    d.text((tx, ty), text, font=f, fill=color)


def draw_two_tone_text_in_box(d, box, lead_text, rest_text, lead_font, rest_font, lead_color, rest_color, pad_x=28):
    left, top, right, bottom = box
    assert pad_x >= MIN_HPAD, f"Horizontal padding {pad_x}px < {MIN_HPAD}px"
    lw, lh, lx0, ly0, _, _ = measure(d, lead_text, lead_font)
    rw, rh, rx0, ry0, _, _ = measure(d, rest_text, rest_font)
    total_w = lw + rw
    max_w = (right - left) - 2 * pad_x
    assert total_w <= max_w, f"Combined text {(lead_text + rest_text)!r} width {total_w}px exceeds {max_w}px"
    tx = left + pad_x
    ty_lead = top + (bottom - top - lh) / 2 - ly0
    ty_rest = top + (bottom - top - rh) / 2 - ry0
    d.text((tx - lx0, ty_lead), lead_text, font=lead_font, fill=lead_color)
    d.text((tx + lw - rx0, ty_rest), rest_text, font=rest_font, fill=rest_color)


def draw_card(d, box, fill=CARD, outline=BORDER, radius=16, width=1, accent=None, accent_pad=16):
    d.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)
    if accent is not None:
        left, top, _, bottom = box
        d.rounded_rectangle(
            (left + accent_pad, top + accent_pad, left + accent_pad + 5, bottom - accent_pad),
            radius=3,
            fill=accent,
        )


def draw_arrow_h(d, x1, x2, y, color, width=3, head_len=10, head_half=6, bidi=False):
    direction = 1 if x2 >= x1 else -1
    line_start = x1 + direction * (head_len - 2) if bidi else x1
    line_end = x2 - direction * (head_len - 2)
    d.line((line_start, y, line_end, y), fill=color, width=width)
    d.polygon(
        [
            (x2, y),
            (x2 - direction * head_len, y - head_half),
            (x2 - direction * head_len, y + head_half),
        ],
        fill=color,
    )
    if bidi:
        d.polygon(
            [
                (x1, y),
                (x1 + direction * head_len, y - head_half),
                (x1 + direction * head_len, y + head_half),
            ],
            fill=color,
        )


def render_banner_one(lang="en"):
    w, h = 1200, 460
    im, d = base_canvas(w, h, [
        ((-220, -320, 560, 400), (16, 185, 169, 34)),
        ((760, -220, 1440, 440), (119, 91, 255, 32)),
        ((380, 180, 860, 540), (140, 186, 255, 18)),
    ])

    if lang == "zh":
        eyebrow = "跨 CLI · 跨设备实时协同"
        subtitle = "不同 CLI 协同工作，始终留在你原来的对话里。"
        runtimes = "OMP  ·  Codex  ·  Pi  ·  AGY     |     可选接入 Hermes"
        cards = [
            (TEAL, "协调者（发起端）", "原对话始终主导", "派发 · 答疑 · 验收"),
            (LAVENDER, "Call CLI 协作桥", "轻量协作 · 无需常驻服务", "进展 · 提问 · 收尾"),
            (BLUE, "执行端 · 本机 / SSH", "OMP · Codex · Pi · AGY", "全新隔离会话"),
        ]
        footer = "概念示意图（非真实截图）"
        out_name = "call-cli-banner.zh-CN.png"
    else:
        eyebrow = "CROSS-CLI & CROSS-DEVICE COLLABORATION"
        subtitle = "Different CLIs. One conversation."
        runtimes = "OMP  ·  Codex  ·  Pi  ·  AGY     |     Hermes optional"
        cards = [
            (TEAL, "Coordinator", "Your chat stays in charge", "Delegate · answer · verify"),
            (LAVENDER, "Call CLI Bridge", "Lightweight · No daemon", "Progress · questions · close"),
            (BLUE, "Executors · Local / SSH", "OMP · Codex · Pi · AGY", "Fresh isolated sessions"),
        ]
        footer = "Illustrative diagram (not a live screenshot)"
        out_name = "call-cli-banner.png"

    f_eye = make_font(18, role="caption", lang=lang, weight="bold", mono=(lang == "en"))
    f_hero = make_font(74, role="title", lang="en", weight="bold")
    f_sub = make_font(29, role="title", lang=lang, weight="heading" if lang == "zh" else "medium")
    f_run = pick_font(20, runtimes, role="body", lang=lang, weight="medium")
    f_card_h = make_font(25, role="heading", lang=lang, weight="heading")
    f_foot = make_font(18, role="caption", lang=lang, weight="caption")

    center_text(d, w, eyebrow, 24, f_eye, TEAL)
    center_text(d, w, "Call CLI", 52, f_hero, TEXT_PRIMARY)
    center_text(d, w, subtitle, 140, f_sub, TEXT_SECONDARY)
    center_text(d, w, runtimes, 188, f_run, TEXT_CAPTION)

    card_w = 356
    gap = 40
    start_x = (w - (3 * card_w + 2 * gap)) // 2  # 26
    top_y, bot_y = 238, 398

    for idx, (accent, title, line1, line2) in enumerate(cards):
        left = start_x + idx * (card_w + gap)
        right = left + card_w
        draw_card(d, (left, top_y, right, bot_y), fill=CARD, outline=BORDER_BRIGHT, radius=16, width=1, accent=accent, accent_pad=16)
        text_x = left + 34
        max_w = right - text_x - 16
        fit_text(d, (text_x, top_y + 22), title, pick_font(25, title, role="heading", lang=lang, weight="heading"), accent, max_w=max_w)
        fit_text(d, (text_x, top_y + 70), line1, pick_font(22, line1, role="body", lang=lang, weight="body"), TEXT_PRIMARY, max_w=max_w)
        fit_text(d, (text_x, top_y + 108), line2, pick_font(22, line2, role="body", lang=lang, weight="body"), TEXT_SECONDARY, max_w=max_w)

    mid_y = (top_y + bot_y) // 2
    for idx in (0, 1):
        ax1 = start_x + card_w + idx * (card_w + gap) + 7
        ax2 = ax1 + gap - 14
        draw_arrow_h(d, ax1, ax2, mid_y, LINE_MUTED, width=3, head_len=9, head_half=6, bidi=True)

    center_text(d, w, footer, 418, f_foot, TEXT_CAPTION)
    im.convert("RGB").save(ROOT / out_name, optimize=True)


def render_walkthrough_one(lang="en"):
    """900x960 mobile-first 360px-legible walkthrough diagram (0.40x scale)."""
    w, h = 900, 960
    im, d = base_canvas(w, h, [
        ((-140, -140, 440, 420), (16, 185, 169, 28)),
        ((460, -100, 1040, 420), (140, 186, 255, 26)),
        ((140, 440, 760, 780), (245, 194, 122, 24)),
        ((260, 660, 660, 980), (183, 165, 255, 22)),
    ])

    if lang == "zh":
        title = "一次协作如何展开"
        subtitle = "提问直达原对话，答复传回同一会话继续执行"
        headers = [
            (TEAL, "你与协调者", "发起端原对话"),
            (LAVENDER, "Call CLI 协作桥", "无需常驻服务"),
            (BLUE, "目标 CLI", "本机或 SSH"),
        ]
        rows = [
            ("standard", TEAL, "right", "1. 委派任务", "任务与范围", "独立新会话"),
            ("standard", LAVENDER, "left", "2. 关键进展", "去噪回传", "持续执行"),
            ("hero", None, None,
             ("3. 原对话决定", "直达原对话", "遇抉择提问"),
             ("答复或调整", "传回执行端", "同一会话继续")),
            ("standard", TEAL, "bidi", "4. 核验并关闭", "实查产物", "干净收尾"),
        ]
        footer = "概念示意图（非真实截图）"
        out_name = "call-cli-walkthrough.zh-CN.png"
    else:
        title = "How a Collaboration Works"
        subtitle = "Questions return to your chat; answers continue the same session"
        headers = [
            (TEAL, "You & Coordinator", "Your active chat"),
            (LAVENDER, "Call CLI Bridge", "No daemon"),
            (BLUE, "Target CLI", "Local / SSH"),
        ]
        rows = [
            ("standard", TEAL, "right", "1. Delegate", "Task & scope", "Fresh session"),
            ("standard", LAVENDER, "left", "2. Milestones", "Noise filtered", "Working"),
            ("hero", None, None,
             ("3. You decide", "Back to your chat", "Asks a question"),
             ("Answer / revise", "Back to target", "Same session")),
            ("standard", TEAL, "bidi", "4. Verify & close", "Real checks", "Clean exit"),
        ]
        footer = "Illustrative flow (not a screenshot)"
        out_name = "call-cli-walkthrough.png"

    f_title = make_font(40, role="title", lang=lang, weight="title")
    f_sub = make_font(22, role="body", lang=lang, weight="body")
    f_hdr_t = make_font(26, role="heading", lang=lang, weight="heading")
    f_hdr_s = make_font(21, role="body", lang=lang, weight="body")
    f_pill = make_font(27, role="heading", lang=lang, weight="heading")
    f_arrow = make_font(24, role="heading", lang=lang, weight="heading")
    f_foot = make_font(20, role="body", lang=lang, weight="caption")

    center_text(d, w, title, 26, f_title, TEXT_PRIMARY)
    center_text(d, w, subtitle, 80, f_sub, TEXT_SECONDARY)

    # Top column headers (symmetric around center spine x=450)
    hdr_y0, hdr_y1 = 128, 206
    hdr_boxes = [(20, hdr_y0, 308, hdr_y1), (324, hdr_y0, 576, hdr_y1), (592, hdr_y0, 880, hdr_y1)]

    # Subtle vertical Bridge spine from center header down through all 4 exchanges
    spine_x = w // 2  # 450
    for y_dash in range(hdr_y1 + 6, 892, 18):
        d.line((spine_x, y_dash, spine_x, min(y_dash + 10, 892)), fill="#3b3763", width=3)

    for box, (accent, h_top, h_sub) in zip(hdr_boxes, headers):
        draw_card(d, box, fill=CARD, outline=accent, radius=16, width=2)
        bl, bt, br, _ = box
        max_w = (br - bl) - 32
        fit_text(d, (bl + 16, bt + 12), h_top, pick_font(26, h_top, role="heading", lang=lang, weight="heading"), accent, max_w=max_w, align="center")
        fit_text(d, (bl + 16, bt + 44), h_sub, pick_font(21, h_sub, role="body", lang=lang, weight="body"), TEXT_SECONDARY, max_w=max_w, align="center")

    # Endpoint card bounds for the 4 rows: Left (20..294 = 274px), Center (294..606 = 312px), Right (606..880 = 274px)
    left_x0, left_x1 = 20, 294
    right_x0, right_x1 = 606, 880
    arrow_x0, arrow_x1 = left_x1 + 12, right_x0 - 12  # 306 .. 594

    def draw_exchange_row(row_top, row_bot, accent, direction, left_txt, mid_txt, right_txt, emphasize=False):
        card_fill = CARD if not emphasize else CARD_INNER
        card_outline = BORDER_BRIGHT if not emphasize else accent
        card_bw = 2 if emphasize else 1

        draw_card(d, (left_x0, row_top, left_x1, row_bot), fill=card_fill, outline=card_outline, radius=16, width=card_bw)
        draw_card(d, (right_x0, row_top, right_x1, row_bot), fill=card_fill, outline=card_outline, radius=16, width=card_bw)

        left_color = accent if emphasize else TEXT_PRIMARY
        right_color = accent if emphasize else TEXT_PRIMARY
        draw_text_centered_in_box(
            d, (left_x0, row_top, left_x1, row_bot), left_txt,
            pick_font(27, left_txt, role="heading", lang=lang, weight="heading"), left_color, pad_x=16,
        )
        draw_text_centered_in_box(
            d, (right_x0, row_top, right_x1, row_bot), right_txt,
            pick_font(27, right_txt, role="heading", lang=lang, weight="heading"), right_color, pad_x=16,
        )

        # Center pill label sits cleanly above the horizontal arrow shaft
        pill_f = pick_font(24, mid_txt, role="heading", lang=lang, weight="heading")
        tw, _, _, _, _, _ = measure(d, mid_txt, pill_f)
        pill_w = max(tw + 36, 156)
        assert pill_w <= (right_x0 - left_x1) - 24, f"Center pill {mid_txt!r} too wide: {pill_w}px"
        pill_x0 = spine_x - pill_w // 2
        pill_x1 = spine_x + pill_w // 2
        row_h = row_bot - row_top
        pill_y0 = row_top + (6 if row_h < 96 else 14)
        pill_y1 = pill_y0 + (40 if row_h < 96 else 44)
        arrow_y = row_bot - (18 if row_h < 96 else 26)

        if direction == "right":
            draw_arrow_h(d, arrow_x0, arrow_x1, arrow_y, accent, width=4, head_len=13, head_half=8)
        elif direction == "left":
            draw_arrow_h(d, arrow_x1, arrow_x0, arrow_y, accent, width=4, head_len=13, head_half=8)
        else:
            draw_arrow_h(d, arrow_x0, arrow_x1, arrow_y, accent, width=4, head_len=12, head_half=8, bidi=True)

        draw_card(d, (pill_x0, pill_y0, pill_x1, pill_y1), fill=CARD_DARK, outline=accent, radius=12, width=2 if emphasize else 1)
        draw_text_centered_in_box(d, (pill_x0, pill_y0, pill_x1, pill_y1), mid_txt, pill_f, accent, pad_x=16)

    # Row 1: Delegate (226..342)
    _, accent1, dir1, l1, m1, r1 = rows[0]
    draw_exchange_row(226, 342, accent1, dir1, l1, m1, r1)

    # Row 2: Milestones (364..480)
    _, accent2, dir2, l2, m2, r2 = rows[1]
    draw_exchange_row(364, 480, accent2, dir2, l2, m2, r2)

    # Row 3: Highlighted AMBER + TEAL hero box (502..742)
    hero_box = (10, 502, w - 10, 742)
    draw_card(d, hero_box, fill=CARD_AMBER, outline=AMBER, radius=20, width=2)
    _, _, _, top_sub, bot_sub = rows[2]
    draw_exchange_row(520, 612, AMBER, "left", top_sub[0], top_sub[1], top_sub[2], emphasize=True)
    draw_exchange_row(630, 722, TEAL, "right", bot_sub[0], bot_sub[1], bot_sub[2], emphasize=True)

    # Row 4: Verify & close (764..880)
    _, accent4, dir4, l4, m4, r4 = rows[3]
    draw_exchange_row(764, 880, accent4, dir4, l4, m4, r4)

    center_text(d, w, footer, 908, f_foot, TEXT_CAPTION)
    im.convert("RGB").save(ROOT / out_name, optimize=True)


def render_architecture_one(lang="en"):
    w, h = 1120, 660
    im, d = base_canvas(w, h, [
        ((-180, -220, 520, 440), (16, 185, 169, 30)),
        ((400, 60, 820, 540), (183, 165, 255, 22)),
        ((720, -160, 1340, 480), (140, 186, 255, 30)),
    ])

    if lang == "zh":
        title = "概念架构：协调者 · 协作桥 · 并行执行端"
        subtitle = "协调者全程掌控原对话，Call CLI 向本机或 SSH 远端并行扇出独立会话"
        col1_h = ("1. 协调者", "原会话保持主导")
        col1_cards = [
            (TEAL, "OMP · Codex · Pi", "或可选接入 Hermes"),
            (BLUE, "POSIX 终端 / SSH", "本地脚本或远端接入"),
            (TEAL, "规划与最终验收", "核验文件并决定收尾"),
        ]
        col2_h = ("2. Call CLI 协作桥", "单 POSIX 宿主 · 无需常驻服务")
        col2_cards = [
            (LAVENDER, "SQLite seq 与去噪", "里程碑与 600s 复核"),
            (TEAL, "Q1 回流与在线控制", "reply / dialogue / intervene"),
            (BLUE, "--write 锁与技能快照", "SHA-256 · 核验 close"),
        ]
        col3_h = ("3. 并行执行端", "隔离会话 · 本机或 SSH")
        col3_cards = [
            (TEAL, "OMP", "ACP · 本机或 SSH"),
            (BLUE, "Codex", "本机或 Win SSH"),
            (BLUE, "Pi", "ACP · 本机或 SSH"),
            (LAVENDER, "AGY", "仅执行端 · 四模式"),
        ]
        gap1_labels = ("JSON", "seq")
        gap2_labels = ("本机 / SSH", "并行扇出")
        wt_lead = "可选同级 Git worktree："
        wt_rest = "仅需写隔离时于仓外创建，核验并干净合并后再移除"
        footer = "概念示意图（非真实截图）· 仅新建隔离会话，执行端之间不串联"
        out_name = "call-cli-architecture.zh-CN.png"
    else:
        title = "Coordinator · Call CLI Bridge · Parallel Executors"
        subtitle = "Initiating chat stays in charge while Call CLI fans out in parallel locally or over SSH"
        col1_h = ("1. Coordinator", "Origin chat leads")
        col1_cards = [
            (TEAL, "OMP · Codex · Pi", "+ Hermes (optional)"),
            (BLUE, "POSIX Shell / SSH", "Scripts & remote clients"),
            (TEAL, "Plans & Accepts", "Checks files & tests"),
        ]
        col2_h = ("2. Call CLI Bridge", "POSIX host · No daemon")
        col2_cards = [
            (LAVENDER, "SQLite seq & Filter", "Milestones + 600s check"),
            (TEAL, "Q1 Return & Control", "reply / dialogue / intervene"),
            (BLUE, "--write Lock & Skills", "SHA-256 · verified close"),
        ]
        col3_h = ("3. Executors", "Isolated · Local or SSH")
        col3_cards = [
            (TEAL, "OMP", "ACP · local or SSH"),
            (BLUE, "Codex", "Local or Win SSH"),
            (BLUE, "Pi", "ACP · local or SSH"),
            (LAVENDER, "AGY", "Exec-only · 4 modes"),
        ]
        gap1_labels = ("JSON", "seq")
        gap2_labels = ("Local /", "SSH", "fan-out")
        wt_lead = "Optional sibling Git worktree: "
        wt_rest = "outside repo only when isolation helps; clean merge before removal"
        footer = "Illustrative diagram (not a live screenshot) · Fresh isolated sessions, never target-to-target"
        out_name = "call-cli-architecture.png"

    f_title = make_font(30, role="title", lang=lang, weight="title")
    f_sub = make_font(20, role="body", lang=lang, weight="body")
    f_col_h = make_font(24, role="heading", lang=lang, weight="heading")
    f_col_s = make_font(20, role="body", lang=lang, weight="body")
    f_exec_t = make_font(24, role="heading", lang="en", weight="heading")
    f_gap = make_font(17, role="caption", lang=lang, weight="bold")
    f_note_lead = make_font(20, role="body", lang=lang, weight="heading")
    f_note_rest = make_font(20, role="body", lang=lang, weight="body")
    f_foot = make_font(18, role="caption", lang=lang, weight="caption")

    center_text(d, w, title, 20, f_title, TEXT_PRIMARY)
    center_text(d, w, subtitle, 58, f_sub, TEXT_SECONDARY)

    # Column geometry across 1120px:
    # Col1: 24..312 (288px), Gap1: 312..380 (68px), Col2: 380..720 (340px),
    # Gap2: 720..824 (104px), Col3: 824..1096 (272px)
    c1 = (24, 102, 312, 524)
    c2 = (380, 102, 720, 524)
    c3 = (824, 102, 1096, 524)

    draw_card(d, c1, fill=CARD_DARK, outline=BORDER, radius=18, width=1)
    draw_card(d, c2, fill=CARD, outline=BORDER_BRIGHT, radius=18, width=2)
    draw_card(d, c3, fill=CARD_DARK, outline=BORDER, radius=18, width=1)

    for (cl, top, cr, _), (h_txt, s_txt), accent in [
        (c1, col1_h, TEAL),
        (c2, col2_h, LAVENDER),
        (c3, col3_h, BLUE),
    ]:
        max_w = (cr - cl) - 36
        fit_text(d, (cl + 18, top + 16), h_txt, f_col_h, accent, max_w=max_w)
        fit_text(d, (cl + 18, top + 48), s_txt, f_col_s, TEXT_SECONDARY, max_w=max_w)

    # Left & Middle column cards (3 cards each)
    for col_box, items, inner_fill in [
        (c1, col1_cards, CARD),
        (c2, col2_cards, CARD_INNER),
    ]:
        cl, _, cr, _ = col_box
        for idx, (accent, item_t, item_b) in enumerate(items):
            top = 188 + idx * 108
            bot = top + 92
            draw_card(d, (cl + 14, top, cr - 14, bot), fill=inner_fill, outline=BORDER, radius=12, width=1, accent=accent, accent_pad=12)
            text_x = cl + 38
            max_w = (cr - 14) - text_x - 16
            fit_text(d, (text_x, top + 14), item_t, pick_font(21, item_t, role="body", lang=lang, weight="heading"), TEXT_PRIMARY, max_w=max_w)
            fit_text(d, (text_x, top + 48), item_b, pick_font(20, item_b, role="body", lang=lang, weight="body"), TEXT_SECONDARY, max_w=max_w)

    # Right column: 4 parallel isolated executors (never chained)
    exec_centers = []
    cl3, _, cr3, _ = c3
    for idx, (accent, ex_t, ex_b) in enumerate(col3_cards):
        top = 188 + idx * 82
        bot = top + 74
        exec_centers.append((top + bot) // 2)
        draw_card(d, (cl3 + 14, top, cr3 - 14, bot), fill=CARD, outline=BORDER, radius=12, width=1, accent=accent, accent_pad=12)
        text_x = cl3 + 38
        max_w = (cr3 - 14) - text_x - 16
        fit_text(d, (text_x, top + 8), ex_t, f_exec_t, TEXT_PRIMARY, max_w=max_w)
        fit_text(d, (text_x, top + 39), ex_b, pick_font(20, ex_b, role="body", lang=lang, weight="body"), TEXT_SECONDARY, max_w=max_w)

    # Gap 1: Bidirectional arrow between Coordinator & Bridge
    mid_y = 342
    draw_arrow_h(d, 318, 374, mid_y, TEAL, width=3, head_len=9, head_half=6, bidi=True)
    fit_text(d, (313, mid_y - 52), gap1_labels[0], f_gap, TEXT_SECONDARY, max_w=66, align="center")
    fit_text(d, (313, mid_y - 28), gap1_labels[1], f_gap, TEAL, max_w=66, align="center")

    # Gap 2: Parallel fan-out bus from Bridge to each Executor (with return head into Bridge)
    bus_x = 772
    draw_arrow_h(d, bus_x, 726, mid_y, LINE_MUTED, width=3, head_len=10, head_half=6)
    d.line((bus_x, exec_centers[0], bus_x, exec_centers[-1]), fill=BLUE, width=3)
    for ey in exec_centers:
        draw_arrow_h(d, bus_x, 818, ey, BLUE, width=3, head_len=10, head_half=6)

    if len(gap2_labels) == 3:
        fit_text(d, (724, 138), gap2_labels[0], f_gap, TEXT_SECONDARY, max_w=96, align="center")
        fit_text(d, (724, 160), gap2_labels[1], f_gap, TEXT_SECONDARY, max_w=96, align="center")
        fit_text(d, (724, 184), gap2_labels[2], f_gap, TEAL, max_w=96, align="center")
    else:
        fit_text(d, (724, 160), gap2_labels[0], f_gap, TEXT_SECONDARY, max_w=96, align="center")
        fit_text(d, (724, 184), gap2_labels[1], f_gap, TEAL, max_w=96, align="center")

    # Optional sibling Git worktree strip
    wt_box = (24, 542, w - 24, 600)
    draw_card(d, wt_box, fill=CARD_DARK, outline=BORDER_BRIGHT, radius=12, width=1, accent=TEAL, accent_pad=14)
    draw_two_tone_text_in_box(d, wt_box, wt_lead, wt_rest, f_note_lead, f_note_rest, TEAL, TEXT_SECONDARY, pad_x=44)

    center_text(d, w, footer, 618, f_foot, TEXT_CAPTION)
    im.convert("RGB").save(ROOT / out_name, optimize=True)


def main():
    require_pillow()
    for lang in ("en", "zh"):
        render_banner_one(lang)
        render_walkthrough_one(lang)
        render_architecture_one(lang)


if __name__ == "__main__":
    main()
