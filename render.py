"""nekro_plugin_msg_rank - 排行榜卡片渲染模块 v3

参照「每日发言榜」版式：
- 顶部随机背景图 + 徽章/日期（顶部压暗渐变保证可读）
- 群名标题与统计摘要
- 前三名领奖台卡片（软阴影、间隙、中央浮起、彩色头像环、名次徽章）
- 第 4 名起明细列表（细进度条、无底轨、文字/图片/视频/语音/转发/其他 取最多前三类）
- 页脚：统计区间 + 署名

仅依赖 Pillow，可独立于 NekroAgent 环境进行本地预览与测试。
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
                "未找到 CJK 字体：请将 NotoSansCJK-Regular.ttc / NotoSansCJK-Bold.ttc 放入插件 fonts/ 目录"
            )
        _font_cache[key] = ImageFont.truetype(path, size, index=0)
    return _font_cache[key]


# ---------------- 配色（清新绿主题） ----------------
BG_COLOR = (232, 236, 242)
CARD_COLOR = (255, 255, 255)
TITLE_COLOR = (31, 42, 55)
SUB_COLOR = (120, 130, 145)
FAINT_COLOR = (150, 158, 172)
ACCENT = (34, 186, 122)
BAR_GREEN_1 = (52, 199, 143)
BAR_GREEN_2 = (26, 168, 106)
RANK_GRAY = (168, 176, 190)
P1_BG = (233, 248, 240)
P2_BG = (250, 251, 253)
P3_BG = (253, 250, 244)
BADGE_GOLD = (255, 200, 64)
BADGE_SILVER = (176, 187, 202)
BADGE_BRONZE = (235, 168, 100)
RING_GOLD = (245, 196, 69)
RING_SILVER = (168, 180, 196)
RING_BRONZE = (240, 164, 107)
AVATAR_FALLBACK_PALETTE = [
    (127, 179, 245), (143, 211, 182), (245, 185, 127), (215, 159, 240),
    (240, 154, 154), (143, 208, 240), (185, 201, 111), (240, 160, 200),
]

CARD_R = 36
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


def _shadow(canvas, x0: int, y0: int, x1: int, y1: int, radius: int = 24, blur: int = 12, alpha: int = 50, dy: int = 7):
    """在 RGBA 画布上为矩形区域绘制软阴影"""
    from PIL import Image, ImageDraw, ImageFilter

    m = blur * 3
    lw, lh = (x1 - x0) + m * 2, (y1 - y0) + m * 2
    layer = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(
        [m, m + dy, m + (x1 - x0), m + (y1 - y0) + dy], radius=radius, fill=(23, 36, 60, alpha)
    )
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    canvas.alpha_composite(layer, (int(x0 - m), int(y0 - m)))


def _paste_avatar(img, avatar, cx: int, cy: int, d: int, nickname: str, seed: int, ring_color=None):
    """圆形头像（可选彩色描边环）；avatar 为 None 时用首字母圆形兜底"""
    from PIL import Image, ImageDraw

    mask = _circle_mask(d * 2).resize((d, d))
    box = (cx - d // 2, cy - d // 2)
    draw = ImageDraw.Draw(img)
    if ring_color is not None:
        pad = 4
        draw.ellipse(
            (box[0] - pad, box[1] - pad, box[0] + d + pad, box[1] + d + pad),
            fill=(255, 255, 255), outline=ring_color, width=3,
        )
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


def _top3_parts(parts: Dict[str, int]) -> str:
    """取非零且最多的三类：文字/图片/视频/语音/转发/其他"""
    order = sorted(parts.items(), key=lambda kv: kv[1], reverse=True)
    seg = [f"{label} {v}" for label, v in order[:3] if v > 0]
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

    rows: [{nickname, count, text_n, image_n, video_n, voice_n, forward_n, other_n, avatar}, ...]
    按 count 降序。返回 PIL.Image（若提供 out_path 则同时保存 PNG）。
    """
    from PIL import Image, ImageDraw

    W = 1000
    if not rows:
        H = 420
        canvas = Image.new("RGBA", (W, H), BG_COLOR + (255,))
        d = ImageDraw.Draw(canvas)
        if banner_img is not None:
            try:
                banner = _cover_crop(banner_img.convert("RGB"), W - 28, BANNER_H, focus_y=0.4)
                canvas.paste(banner, (14, 14), _round_top_mask(W - 28, BANNER_H, CARD_R))
            except Exception:
                pass
        d.text((W // 2, 260), "统计区间内暂无发言数据", font=_font(34, bold=True), fill=SUB_COLOR, anchor="mm")
        d.text((W // 2, 310), badge_text, font=_font(24), fill=FAINT_COLOR, anchor="mm")
        if out_path:
            canvas.convert("RGB").save(out_path, "PNG")
        return canvas.convert("RGB")

    PAD = 44
    ROW_H = 64
    n = len(rows)
    n_podium = min(3, n)
    n_list = n - n_podium

    header_h = 128
    p1_h, p2_h = 244, 204
    podium_h = (p1_h + 8) if n_podium >= 1 else 0
    list_h = n_list * ROW_H
    footer_h = 72
    H = int(14 + BANNER_H + header_h + podium_h + (30 if n_list else 14) + list_h + footer_h + 14)

    canvas = Image.new("RGBA", (W, H), BG_COLOR + (255,))

    # 外层卡片（含软阴影）
    cx0, cy0 = 14, 14
    cw, chh = W - 28, H - 28
    _shadow(canvas, cx0, cy0, cx0 + cw, cy0 + chh, radius=CARD_R, blur=16, alpha=70, dy=8)
    ImageDraw.Draw(canvas).rounded_rectangle(
        [cx0, cy0, cx0 + cw, cy0 + chh], radius=CARD_R, fill=CARD_COLOR + (255,)
    )
    draw = ImageDraw.Draw(canvas)

    # ---------- 顶部背景图 ----------
    if banner_img is not None:
        banner = _cover_crop(banner_img.convert("RGB"), cw, BANNER_H, focus_y=0.4)
    else:
        banner = _vgradient_rgba(cw, BANNER_H, (15, 118, 110), (13, 148, 136), 255, 255).convert("RGB")
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
    canvas.alpha_composite(ov, (cx0 + 26, cy0 + 24))

    # 右上日期
    df = _font(32, bold=True)
    draw.text((cx0 + cw - 28 + 2, cy0 + 46 + 2), date_text, font=df, fill=(0, 0, 0), anchor="rm")
    draw.text((cx0 + cw - 28, cy0 + 46), date_text, font=df, fill=(255, 255, 255), anchor="rm")

    # ---------- 标题与统计摘要 ----------
    ty = cy0 + BANNER_H + 44
    draw.text((cx0 + PAD, ty), _truncate(draw, title or "群发言排行榜", 40, True, cw - PAD * 2 - 280),
              font=_font(40, bold=True), fill=TITLE_COLOR, anchor="lm")
    draw.text((cx0 + PAD, ty + 46), f"共 {total_users:,} 人发言 · {total_msgs:,} 条消息{ongoing_text}",
              font=_font(24), fill=SUB_COLOR, anchor="lm")

    # ---------- 领奖台 ----------
    podium_rows = rows[:n_podium]
    gap = 18
    inner_w = cw - PAD * 2
    w_each = (inner_w - gap * 2) // 3
    py = cy0 + BANNER_H + header_h
    side_dy = (p1_h - p2_h) - 18  # 两侧卡片相对中央的下沉量

    for rank, r in enumerate(podium_rows, 1):
        if rank == 1 and n_podium == 3:
            px, pw, ph, pyy = cx0 + PAD + w_each + gap, w_each, p1_h, py
        elif n_podium == 3:
            px = cx0 + PAD if rank == 2 else cx0 + cw - PAD - w_each
            pw, ph, pyy = w_each, p2_h, py + side_dy
        elif n_podium == 2:
            pw = (inner_w - gap) // 2
            px = cx0 + PAD if rank == 1 else cx0 + PAD + pw + gap
            ph, pyy = p1_h, py + 10
        else:
            px, pw, ph, pyy = cx0 + PAD, inner_w, p1_h, py

        bg = P1_BG if rank == 1 else (P2_BG if rank == 2 else P3_BG)
        ring = RING_GOLD if rank == 1 else (RING_SILVER if rank == 2 else RING_BRONZE)
        cnt_color = ACCENT if rank == 1 else TITLE_COLOR
        cnt_f = 44 if rank == 1 else 34
        av_d = 88 if rank == 1 else 70

        _shadow(canvas, px, pyy, px + pw, pyy + ph, radius=22, blur=10, alpha=46, dy=5)
        draw.rounded_rectangle([px, pyy, px + pw, pyy + ph], radius=22, fill=bg + (255,))

        ccx = px + pw // 2
        av_cy = pyy + 20 + av_d // 2
        _paste_avatar(canvas, r.get("avatar"), ccx, av_cy, av_d, r.get("nickname", ""), rank, ring_color=ring)
        _draw_rank_badge(canvas, ccx - av_d // 2 + 4, av_cy + av_d // 2 - 24, rank)

        ny = pyy + 20 + av_d + 24
        nick = _truncate(draw, _sanitize_nick(r.get("nickname", "未知")), 24, True, pw - 22)
        draw.text((ccx, ny), nick, font=_font(24, bold=True), fill=TITLE_COLOR, anchor="mm")

        cnt_text = f"{int(r['count']):,}"
        cw_cnt = draw.textlength(cnt_text, font=_font(cnt_f, bold=True))
        tiao_w = draw.textlength("条", font=_font(19))
        total_w = cw_cnt + tiao_w + 6
        draw.text((ccx - total_w / 2, ny + 34), cnt_text, font=_font(cnt_f, bold=True), fill=cnt_color, anchor="lm")
        draw.text((ccx - total_w / 2 + cw_cnt + 6, ny + 34 + cnt_f * 0.14), "条", font=_font(19), fill=SUB_COLOR, anchor="lm")

        parts = _top3_parts({
            "文字": int(r.get("text_n", 0)), "图片": int(r.get("image_n", 0)), "视频": int(r.get("video_n", 0)),
            "语音": int(r.get("voice_n", 0)), "转发": int(r.get("forward_n", 0)), "其他": int(r.get("other_n", 0)),
        })
        parts = _truncate(draw, parts, 17, False, pw - 18)
        draw.text((ccx, ny + 34 + cnt_f // 2 + 20), parts, font=_font(17), fill=FAINT_COLOR, anchor="mm")

    # ---------- 明细列表 ----------
    ly = py + podium_h + 26 if n_list else 0
    # 进度条按列表可见行自身的最大值缩放（顶部留 1/3 余量更接近参考图的观感）
    list_max = max(1, max((int(r["count"]) for r in rows[n_podium:]), default=1))
    bar_scale = list_max * 1.5
    bar_x0 = cx0 + 596
    bar_x1 = cx0 + cw - PAD - 104
    bar_w_full = bar_x1 - bar_x0
    green_bar = _vgradient_rgba(bar_w_full, 8, BAR_GREEN_1, BAR_GREEN_2, 255, 255).convert("RGB")

    for idx in range(n_podium, n):
        r = rows[idx]
        rank = idx + 1
        ry = ly + (idx - n_podium) * ROW_H
        cyc = ry + ROW_H // 2

        # 名次与昵称行对齐（参考图样式）
        draw.text((cx0 + PAD, ry + 20), str(rank), font=_font(22, bold=True), fill=RANK_GRAY, anchor="lm")
        _paste_avatar(canvas, r.get("avatar"), cx0 + PAD + 88, cyc, 46, r.get("nickname", ""), rank)

        nick = _truncate(draw, _sanitize_nick(r.get("nickname", "未知")), 25, True, 400)
        draw.text((cx0 + PAD + 124, ry + 18), nick, font=_font(25, bold=True), fill=TITLE_COLOR, anchor="lm")
        parts = _top3_parts({
            "文字": int(r.get("text_n", 0)), "图片": int(r.get("image_n", 0)), "视频": int(r.get("video_n", 0)),
            "语音": int(r.get("voice_n", 0)), "转发": int(r.get("forward_n", 0)), "其他": int(r.get("other_n", 0)),
        })
        parts = _truncate(draw, parts, 18, False, 420)
        draw.text((cx0 + PAD + 124, ry + 46), parts, font=_font(18), fill=FAINT_COLOR, anchor="lm")

        w = max(12, min(bar_w_full, int(bar_w_full * int(r["count"]) / bar_scale)))
        seg = green_bar.crop((0, 0, w, 8))
        seg_mask = Image.new("L", (w, 8), 0)
        ImageDraw.Draw(seg_mask).rounded_rectangle([0, 0, w - 1, 7], radius=4, fill=255)
        canvas.paste(seg, (bar_x0, cyc - 4), seg_mask)

        draw.text((cx0 + cw - PAD, cyc), f"{int(r['count']):,}", font=_font(25, bold=True), fill=TITLE_COLOR, anchor="rm")

    # ---------- 页脚 ----------
    fy = H - 14 - footer_h // 2
    draw.line([cx0 + PAD, fy - 22, cx0 + cw - PAD, fy - 22], fill=(235, 239, 245), width=2)
    draw.text((cx0 + PAD, fy), f"统计区间 {range_text}", font=_font(21), fill=FAINT_COLOR, anchor="lm")
    draw.text((cx0 + cw - PAD, fy), "群发言排行榜 · By Akiyo-dayo", font=_font(21), fill=FAINT_COLOR, anchor="rm")

    if out_path:
        canvas.convert("RGB").save(out_path, "PNG")
    return canvas.convert("RGB")


def render_from_render_rows(rows_meta, out_path: str, banner_img=None, **kwargs):
    """便捷入口：rows_meta = [(nickname, cnt, text_n, image_n, video_n, voice_n, forward_n, other_n, avatar_bytes|None), ...]"""
    import io
    from PIL import Image

    rows = []
    for item in rows_meta:
        nickname, cnt, text_n, image_n, video_n, voice_n, forward_n, other_n, avatar_bytes = item
        avatar = None
        if avatar_bytes:
            try:
                avatar = Image.open(io.BytesIO(avatar_bytes))
            except Exception:
                avatar = None
        rows.append(
            {
                "nickname": nickname, "count": cnt, "text_n": text_n, "image_n": image_n,
                "video_n": video_n, "voice_n": voice_n, "forward_n": forward_n,
                "other_n": other_n, "avatar": avatar,
            }
        )
    return render_rank_card(rows, banner_img=banner_img, out_path=out_path, **kwargs)
