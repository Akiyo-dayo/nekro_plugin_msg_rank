"""nekro_plugin_msg_rank - 排行榜卡片渲染模块 v2

参照「每日发言榜」版式：顶部随机背景图 + 徽章/日期，群名标题与统计摘要，
前三名领奖台卡片，第 4 名起明细列表（文字/图片/其他分项 + 进度条）。

仅依赖 Pillow，可独立于 NekroAgent 环境进行本地预览与测试。
字体优先使用插件自带 fonts/ 目录下的 NotoSansCJK（Regular/Bold）。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

FONT_DIR = Path(__file__).resolve().parent / "fonts"

_REGULAR_CANDIDATES = [
    FONT_DIR / "NotoSansCJK-Regular.ttc",
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
]
_BOLD_CANDIDATES = [
    FONT_DIR / "NotoSansCJK-Bold.ttc",
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc"),
]

_font_cache: Dict[Tuple[int, bool], object] = {}


def find_font_path(bold: bool = False) -> Optional[str]:
    for p in (_BOLD_CANDIDATES if bold else _REGULAR_CANDIDATES):
        try:
            if Path(p).exists():
                return str(p)
        except OSError:
            continue
    return None


def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    key = (size, bold)
    if key not in _font_cache:
        path = find_font_path(bold)
        if path is None:
            raise RuntimeError(
                "未找到 CJK 字体：请将 NotoSansCJK-Regular.ttc / NotoSansCJK-Bold.ttc "
                "放入插件 fonts/ 目录"
            )
        _font_cache[key] = ImageFont.truetype(path, size, index=0)
    return _font_cache[key]


# ---------------- 配色（清新绿主题，参照样例） ----------------
BG_COLOR = (229, 233, 240)        # 页面底色
CARD_COLOR = (255, 255, 255)      # 卡片
TITLE_COLOR = (31, 42, 55)        # 标题深灰
SUB_COLOR = (107, 114, 128)       # 副标题灰
FAINT_COLOR = (138, 148, 163)     # 弱化灰
ACCENT = (34, 178, 116)           # 主题绿（条形/第一名数字）
BAR_TRACK = (238, 241, 245)       # 条形轨道
RANK_GRAY = (154, 164, 178)       # 列表名次灰
P1_BG = (231, 248, 239)           # 第一名卡片底
P2_BG = (244, 246, 249)           # 第二名卡片底
P3_BG = (251, 246, 234)           # 第三名卡片底
BADGE_GOLD = (255, 197, 61)
BADGE_SILVER = (201, 210, 222)
BADGE_BRONZE = (227, 161, 107)
AVATAR_FALLBACK_PALETTE = [
    (127, 179, 245), (143, 211, 182), (245, 185, 127), (215, 159, 240),
    (240, 154, 154), (143, 208, 240), (185, 201, 111), (240, 160, 200),
]

CARD_R = 34
BANNER_H = 280


def _hex(c: str) -> Tuple[int, int, int]:
    c = c.strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _lerp(c1, c2, t: float) -> Tuple[int, int, int]:
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))  # type: ignore[return-value]


def _vgradient_rgba(w: int, h: int, c1, c2, a1: int, a2: int):
    from PIL import Image

    strip = Image.new("RGBA", (1, max(1, h)))
    px = strip.load()
    for y in range(max(1, h)):
        t = y / max(1, h - 1)
        px[0, y] = (*_lerp(c1, c2, t), int(a1 + (a2 - a1) * t))
    return strip.resize((w, h))


def _cover_crop(img, w: int, h: int, focus_y: float = 0.42):
    from PIL import Image

    scale = max(w / img.width, h / img.height)
    nw, nh = int(img.width * scale + 0.5), int(img.height * scale + 0.5)
    img = img.resize((nw, nh), Image.LANCZOS)
    x = (nw - w) // 2
    y = int((nh - h) * focus_y)
    return img.crop((x, y, x + w, y + h))


def _sanitize_nick(text: str) -> str:
    """去掉无法用 CJK 字体渲染的 emoji/生僻字，避免方块"""
    out = []
    for ch in text:
        o = ord(ch)
        if o in (0x200D, 0xFE0F, 0x20E3):
            continue
        if o > 0xFFFF:
            continue
        if 0x1F000 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF or 0x2190 <= o <= 0x21FF:
            continue
        out.append(ch)
    return "".join(out).strip() or text


def _truncate(draw, text: str, size: int, bold: bool, max_w: int) -> str:
    if not text:
        return ""
    font = _font(size, bold)
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def _circle_mask(size: int):
    from PIL import Image, ImageDraw

    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).ellipse([0, 0, size - 1, size - 1], fill=255)
    return m


def _round_top_mask(w: int, h: int, r: int):
    from PIL import Image, ImageDraw

    m = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=255)
    d.rectangle([0, r, w - 1, h - 1], fill=255)
    return m


def _paste_avatar(img, avatar, cx: int, cy: int, d: int, nickname: str, seed: int, ring: bool = False):
    """圆形头像；avatar 为 None 时用首字母圆形兜底"""
    from PIL import Image, ImageDraw

    mask = _circle_mask(d * 2).resize((d, d))
    box = (cx - d // 2, cy - d // 2)
    if ring:
        draw = ImageDraw.Draw(img)
        ring_box = (box[0] - 3, box[1] - 3, box[0] + d + 3, box[1] + d + 3)
        draw.ellipse(ring_box, fill=(255, 255, 255), outline=(226, 232, 240), width=1)
    if avatar is not None:
        try:
            av = avatar.convert("RGBA").resize((d, d))
            img.paste(av, box, mask)
            return
        except Exception:
            pass
    color = AVATAR_FALLBACK_PALETTE[seed % len(AVATAR_FALLBACK_PALETTE)]
    av = Image.new("RGBA", (d * 2, d * 2), (0, 0, 0, 0))
    ad = ImageDraw.Draw(av)
    ad.ellipse([0, 0, d * 2 - 1, d * 2 - 1], fill=color + (255,))
    clean = _sanitize_nick(nickname)
    ch = next((c for c in clean if c.isprintable() and not c.isspace()), None)
    if ch is None or ord(ch) >= 0x10000:
        ch = "友"
    ad.text((d, d), ch, font=_font(int(d * 1.05), bold=True), fill=(255, 255, 255, 255), anchor="mm")
    small = av.resize((d, d))
    img.paste(small, box, small)


def _draw_rank_badge(img, cx: int, cy: int, rank: int) -> None:
    """头像角落的名次徽章：第 1 名画皇冠，其余画数字"""
    from PIL import Image, ImageDraw

    d = 40
    color = {1: BADGE_GOLD, 2: BADGE_SILVER, 3: BADGE_BRONZE}.get(rank, BADGE_GOLD)
    badge = Image.new("RGBA", (d * 2, d * 2), (0, 0, 0, 0))
    bd = ImageDraw.Draw(badge)
    bd.ellipse([0, 0, d * 2 - 1, d * 2 - 1], fill=color + (255,), outline=(255, 255, 255, 255), width=4)
    if rank == 1:
        s = d * 2 / 40.0
        crown = [
            (8 * s, 27 * s), (8 * s, 13 * s), (15 * s, 19 * s), (20 * s, 9 * s),
            (25 * s, 19 * s), (32 * s, 13 * s), (32 * s, 27 * s),
        ]
        bd.polygon(crown, fill=(255, 255, 255, 255))
        bd.rectangle([8 * s, 29 * s, 32 * s, 32 * s], fill=(255, 255, 255, 255))
    else:
        bd.text((d, d), str(rank), font=_font(24, bold=True), fill=(255, 255, 255, 255), anchor="mm")
    small = badge.resize((d, d))
    img.paste(small, (cx - d // 2, cy - d // 2), small)


def _fmt_parts(parts: List[Tuple[str, int]]) -> str:
    seg = [f"{label} {v}" for label, v in parts if v > 0]
    return " · ".join(seg) if seg else "水群 1 条"


def _draw_calendar_icon(draw, x: int, y: int, s: int = 22) -> None:
    draw.rounded_rectangle([x, y + s * 0.15, x + s, y + s], radius=3, outline=(255, 255, 255), width=2)
    draw.line([x + s * 0.28, y + s * 0.05, x + s * 0.28, y + s * 0.3], fill=(255, 255, 255), width=2)
    draw.line([x + s * 0.72, y + s * 0.05, x + s * 0.72, y + s * 0.3], fill=(255, 255, 255), width=2)
    draw.line([x, y + s * 0.42, x + s, y + s * 0.42], fill=(255, 255, 255), width=2)


def render_rank_card(
    rows: List[Dict],
    total_users: int,
    total_msgs: int,
    badge_text: str,
    date_text: str,
    title: str,
    range_text: str,
    ongoing_text: str = "",
    banner_img=None,
    out_path: Optional[str] = None,
):
    """渲染发言榜卡片

    rows: [{nickname, count, text_n, img_n, other_n, avatar(optional)}, ...] 按 count 降序
    返回 PIL.Image（若提供 out_path 则同时保存 PNG）
    """
    import random  # noqa: F401
    from PIL import Image, ImageDraw

    W = 1000
    if not rows:
        # 空数据兜底卡片
        H = 420
        canvas = Image.new("RGB", (W, H), BG_COLOR)
        canvas.paste(Image.new("RGB", (W - 20, H - 20), CARD_COLOR), (10, 10))
        d = ImageDraw.Draw(canvas)
        if banner_img is not None:
            try:
                banner = _cover_crop(banner_img.convert("RGB"), W - 20, BANNER_H, focus_y=0.4)
                canvas.paste(banner, (10, 10), _round_top_mask(W - 20, BANNER_H, CARD_R))
            except Exception:
                pass
        d.text((W // 2, 250), "统计区间内暂无发言数据", font=_font(34, bold=True), fill=SUB_COLOR, anchor="mm")
        d.text((W // 2, 300), badge_text, font=_font(24), fill=FAINT_COLOR, anchor="mm")
        if out_path:
            canvas.save(out_path, "PNG")
        return canvas
    PAD = 36
    ROW_H = 68
    n = len(rows)
    n_podium = min(3, n)
    n_list = n - n_podium

    header_h = 132
    podium_h = 268 if n_podium >= 3 else (222 if n_podium == 2 else 190)
    list_h = n_list * ROW_H
    footer_h = 74
    H = int(10 + BANNER_H + header_h + podium_h + (28 if n_list else 12) + list_h + footer_h + 10)

    canvas = Image.new("RGB", (W, H), BG_COLOR)
    card = Image.new("RGB", (W - 20, H - 20), CARD_COLOR)
    canvas.paste(card, (10, 10))
    draw = ImageDraw.Draw(canvas)
    cx0, cy0 = 10, 10
    cw = W - 20

    # ---------- 顶部背景图 ----------
    if banner_img is None:
        # 无素材时回退为青绿渐变
        banner_img = Image.new("RGB", (cw, BANNER_H))
        grad = _vgradient_rgba(cw, BANNER_H, (15, 118, 110), (13, 148, 136), 255, 255)
        banner_img.paste(grad.convert("RGB"), (0, 0))
    banner = _cover_crop(banner_img.convert("RGB"), cw, BANNER_H, focus_y=0.4)
    # 顶部压暗渐变，保证徽章/日期可读
    scrim = _vgradient_rgba(cw, 110, (0, 0, 0), (0, 0, 0), 130, 0)
    banner.paste(scrim, (0, 0), scrim)
    canvas.paste(banner, (cx0, cy0), _round_top_mask(cw, BANNER_H, CARD_R))

    # 左上徽章
    bf = _font(24, bold=True)
    icon_w = 30
    bw = int(draw.textlength(badge_text, font=bf)) + icon_w + 34
    ov = Image.new("RGBA", (bw, 44), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    od.rounded_rectangle([0, 0, bw - 1, 43], radius=22, fill=(17, 24, 33, 150))
    _draw_calendar_icon(od, 18, 11)
    od.text((icon_w + 26, 22), badge_text, font=bf, fill=(255, 255, 255, 255), anchor="lm")
    canvas.paste(ov, (cx0 + 24, cy0 + 22), ov)

    # 右上日期
    df = _font(32, bold=True)
    draw.text((cx0 + cw - 26 + 2, cy0 + 44 + 2), date_text, font=df, fill=(0, 0, 0), anchor="rm")
    draw.text((cx0 + cw - 26, cy0 + 44), date_text, font=df, fill=(255, 255, 255), anchor="rm")

    # ---------- 标题与统计摘要 ----------
    ty = cy0 + BANNER_H + 46
    draw.text((cx0 + PAD, ty), _truncate(draw, title or "群发言排行榜", 40, True, cw - PAD * 2 - 300),
              font=_font(40, bold=True), fill=TITLE_COLOR, anchor="lm")
    sub = f"共 {total_users:,} 人发言 · {total_msgs:,} 条消息{ongoing_text}"
    draw.text((cx0 + PAD, ty + 48), sub, font=_font(25), fill=SUB_COLOR, anchor="lm")

    # ---------- 领奖台 ----------
    py = cy0 + BANNER_H + header_h
    podium_rows = rows[:n_podium]
    gap = 16
    if n_podium == 3:
        w_side, w_mid = 296, 328
        x2 = cx0 + PAD
        x1 = cx0 + (cw - w_mid) // 2
        x3 = cx0 + cw - PAD - w_side
        placed = [(2, podium_rows[1], x2, w_side, 196, py + 56, P2_BG),
                  (1, podium_rows[0], x1, w_mid, 240, py + 16, P1_BG),
                  (3, podium_rows[2], x3, w_side, 196, py + 56, P3_BG)]
    elif n_podium == 2:
        w_each = (cw - PAD * 2 - gap) // 2
        placed = [(1, podium_rows[0], cx0 + PAD, w_each, 196, py + 20, P1_BG),
                  (2, podium_rows[1], cx0 + PAD + w_each + gap, w_each, 196, py + 20, P2_BG)]
    else:
        w_each = cw - PAD * 2
        placed = [(1, podium_rows[0], cx0 + PAD, w_each, 170, py + 20, P1_BG)]

    for rank, r, px, pw, ph, pyy, bg in placed:
        draw.rounded_rectangle([px, pyy, px + pw, pyy + ph], radius=22, fill=bg)
        ccx = px + pw // 2
        if rank == 1:
            av_d, av_gap = 118, 6
            av_cy = pyy + av_gap + av_d // 2
            name_f, name_y = 28, pyy + av_gap + av_d + 30
            cnt_f, cnt_y = 50, pyy + av_gap + av_d + 72
            part_f, part_y = 19, pyy + av_gap + av_d + 108
            cnt_color = ACCENT
        elif n_podium == 3:
            av_d, av_gap = 92, 10
            av_cy = pyy + av_gap + av_d // 2
            name_f, name_y = 25, pyy + av_gap + av_d + 28
            cnt_f, cnt_y = 38, pyy + av_gap + av_d + 64
            part_f, part_y = 18, pyy + av_gap + av_d + 96
            cnt_color = TITLE_COLOR
        else:
            av_d, av_gap = 100, 10
            av_cy = pyy + av_gap + av_d // 2
            name_f, name_y = 26, pyy + av_gap + av_d + 30
            cnt_f, cnt_y = 40, pyy + av_gap + av_d + 68
            part_f, part_y = 19, pyy + av_gap + av_d + 100
            cnt_color = TITLE_COLOR

        _paste_avatar(canvas, r.get("avatar"), ccx, av_cy, av_d, r.get("nickname", ""), rank, ring=True)
        _draw_rank_badge(canvas, ccx - av_d // 2 + 6, av_cy + av_d // 2 - 26, rank)

        nick = _truncate(draw, _sanitize_nick(r.get("nickname", "未知")), name_f, True, pw - 24)
        draw.text((ccx, name_y), nick, font=_font(name_f, bold=True), fill=TITLE_COLOR, anchor="mm")

        cnt_text = f"{int(r['count']):,}"
        cw_cnt = draw.textlength(cnt_text, font=_font(cnt_f, bold=True))
        tiao_w = draw.textlength(" 条", font=_font(22))
        total_w = cw_cnt + tiao_w
        draw.text((ccx - total_w / 2, cnt_y), cnt_text, font=_font(cnt_f, bold=True), fill=cnt_color, anchor="lm")
        draw.text((ccx - total_w / 2 + cw_cnt + 4, cnt_y + cnt_f * 0.12), "条", font=_font(22), fill=SUB_COLOR, anchor="lm")

        parts = _fmt_parts([("文字", int(r.get("text_n", 0))), ("图片", int(r.get("img_n", 0))), ("其他", int(r.get("other_n", 0)))])
        parts = _truncate(draw, parts, part_f, False, pw - 20)
        draw.text((ccx, part_y), parts, font=_font(part_f), fill=FAINT_COLOR, anchor="mm")

    # ---------- 明细列表 ----------
    ly = py + podium_h + 26 if n_list else 0
    max_cnt = max(1, max((int(r["count"]) for r in rows), default=1))
    bar_x0 = cx0 + 600
    bar_x1 = cx0 + cw - PAD - 96
    bar_w_full = bar_x1 - bar_x0
    green_bar = _vgradient_rgba(bar_w_full, 12, (46, 196, 146), (27, 168, 108), 255, 255).convert("RGB")

    for idx in range(n_podium, n):
        r = rows[idx]
        rank = idx + 1
        ry = ly + (idx - n_podium) * ROW_H
        cyc = ry + ROW_H // 2

        draw.text((cx0 + PAD + 8, cyc), str(rank), font=_font(24, bold=True), fill=RANK_GRAY, anchor="lm")
        _paste_avatar(canvas, r.get("avatar"), cx0 + PAD + 92, cyc, 46, r.get("nickname", ""), rank)

        nick = _truncate(draw, _sanitize_nick(r.get("nickname", "未知")), 26, True, 380)
        draw.text((cx0 + PAD + 128, ry + 20), nick, font=_font(26, bold=True), fill=TITLE_COLOR, anchor="lm")
        parts = _fmt_parts([("文字", int(r.get("text_n", 0))), ("图片", int(r.get("img_n", 0))), ("其他", int(r.get("other_n", 0)))])
        parts = _truncate(draw, parts, 19, False, 400)
        draw.text((cx0 + PAD + 128, ry + 48), parts, font=_font(19), fill=FAINT_COLOR, anchor="lm")

        draw.rounded_rectangle([bar_x0, cyc - 6, bar_x0 + bar_w_full, cyc + 6], radius=6, fill=BAR_TRACK)
        w = max(14, int(bar_w_full * int(r["count"]) / max_cnt))
        seg = green_bar.crop((0, 0, w, 12))
        seg_mask = Image.new("L", (w, 12), 0)
        ImageDraw.Draw(seg_mask).rounded_rectangle([0, 0, w - 1, 11], radius=6, fill=255)
        canvas.paste(seg, (bar_x0, cyc - 6), seg_mask)

        draw.text((cx0 + cw - PAD, cyc), f"{int(r['count']):,}", font=_font(26, bold=True), fill=TITLE_COLOR, anchor="rm")

    # ---------- 页脚 ----------
    fy = H - 10 - footer_h // 2
    draw.line([cx0 + PAD, fy - 24, cx0 + cw - PAD, fy - 24], fill=(234, 238, 244), width=2)
    draw.text((cx0 + PAD, fy), f"统计区间 {range_text}", font=_font(21), fill=FAINT_COLOR, anchor="lm")
    draw.text((cx0 + cw - PAD, fy), "Data by NekroAgent", font=_font(21), fill=FAINT_COLOR, anchor="rm")

    if out_path:
        canvas.save(out_path, "PNG")
    return canvas


def render_from_render_rows(rows_meta, out_path: str, banner_img=None, **kwargs):
    """便捷入口：rows_meta = [(nickname, count, text_n, img_n, other_n, avatar_bytes|None), ...]"""
    import io
    from PIL import Image

    rows = []
    for item in rows_meta:
        nickname, count, text_n, img_n, other_n, avatar_bytes = item
        avatar = None
        if avatar_bytes:
            try:
                avatar = Image.open(io.BytesIO(avatar_bytes))
            except Exception:
                avatar = None
        rows.append(
            {
                "nickname": nickname,
                "count": count,
                "text_n": text_n,
                "img_n": img_n,
                "other_n": other_n,
                "avatar": avatar,
            }
        )
    return render_rank_card(rows, banner_img=banner_img, out_path=out_path, **kwargs)
