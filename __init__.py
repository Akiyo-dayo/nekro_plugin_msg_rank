"""nekro_plugin_msg_rank - 群发言排行榜插件 v2

参照「每日发言榜」版式：顶部随机鸣潮背景图 + 前三名领奖台 + 明细列表，
支持 /发言榜 指令（默认今日自然日），Agent 工具可查询本周/本月自然区间。
头像强制拉取真实 QQ 头像，文案样式统一内置。
"""
from __future__ import annotations

import asyncio
import io
import os
import random
import re
import time
from pathlib import Path
from typing import Annotated, Any, Dict, List, Optional, Tuple

from nekro_agent.core import logger
from nekro_agent.core.core_utils import ConfigBase
from nekro_agent.services.command.base import CommandPermission
from nekro_agent.services.command.ctl import CmdCtl
from nekro_agent.services.command.schemas import (
    Arg,
    CommandExecutionContext,
    CommandOutputSegment,
    CommandOutputSegmentType,
    CommandResponse,
    CommandResponseStatus,
)
from nekro_agent.services.plugin.base import NekroPlugin, SandboxMethodType
from nekro_agent.api.message import send_image
from nekro_agent.api.schemas import AgentCtx
from pydantic import Field

# 插件实例
plugin = NekroPlugin(
    name="群发言排行榜",
    module_name="nekro_plugin_msg_rank",
    description="统计群成员发言条数生成排行榜图片：/发言榜 指令默认查今日，Agent 可查本周/本月",
    version="1.1.0",
    author="Akiyo_dayo",
    url="https://github.com/Akiyo-dayo/nekro_plugin_msg_rank",
    support_adapter=["onebot_v11"],
)


@plugin.mount_config()
class MsgRankConfig(ConfigBase):
    """群发言排行榜配置"""

    TOP_N: int = Field(
        default=20,
        ge=3,
        le=60,
        title="榜单人数",
        description="排行榜展示的总人数（含前三名领奖台）",
    )
    EXCLUDE_SYSTEM: bool = Field(
        default=True,
        title="排除机器人与系统消息",
        description="排除 Bot 自身与系统产生的消息（sender_id 为 -1 或空）",
    )


config = plugin.get_config(MsgRankConfig)

_PLUGIN_DIR = Path(__file__).resolve().parent
_BANNER_DIR = _PLUGIN_DIR / "banners"

_SCOPE_ALIASES = {
    "today": "today", "今日": "today", "天": "today", "day": "today", "日": "today",
    "week": "week", "本周": "week", "周": "week",
    "month": "month", "本月": "month", "月": "month",
}


def _load_render():
    """加载渲染模块（render.py 仅依赖 Pillow，缺失时动态安装）"""
    try:
        import PIL  # noqa: F401
    except ImportError:
        from nekro_agent.services.plugin.packages import dynamic_import_pkg

        logger.info("msg_rank: 检测到缺少 Pillow，开始动态安装...")
        dynamic_import_pkg("pillow>=10.0")

    try:
        from . import render
    except ImportError:
        import importlib.util

        spec = importlib.util.spec_from_file_location("nekro_plugin_msg_rank_render", str(_PLUGIN_DIR / "render.py"))
        assert spec is not None and spec.loader is not None
        render = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(render)
    return render


def _resolve_top_n(top_n: int) -> int:
    n = config.TOP_N if top_n is None or top_n <= 0 else top_n
    return max(3, min(60, n))


def _resolve_scope(scope: Optional[str]) -> str:
    return _SCOPE_ALIASES.get((scope or "today").strip().lower(), "today")


def _scope_bounds(key: str) -> Tuple[int, int]:
    """返回 (start_ts, end_ts)，自然区间按本地时区（Asia/Shanghai）"""
    now = int(time.time())
    lt = time.localtime(now)
    if key == "week":
        # 本周一 00:00
        days_since_monday = lt.tm_wday
        start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)) - days_since_monday * 86400
    elif key == "month":
        start = time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))
    else:
        start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    return int(start), now


_SCOPE_LABELS = {
    "today": ("今日发言榜", "今日"),
    "week": ("本周发言榜", "本周"),
    "month": ("本月发言榜", "本月"),
}


def _fmt_md(ts: int) -> str:
    return time.strftime("%m/%d", time.localtime(ts))


def _fmt_full(ts: int) -> str:
    return time.strftime("%m/%d %H:%M", time.localtime(ts))


def _resolve_chat_key(_ctx: AgentCtx, chat_key: str) -> str:
    ck = (chat_key or "").strip() or _ctx.chat_key
    if not ck:
        raise RuntimeError("无法确定目标群聊：当前上下文缺少聊天标识，请显式传入 chat_key")
    if "group" not in ck and (_ctx.channel_type or "").lower() not in ("group",):
        raise RuntimeError("发言排行榜仅支持群聊频道，不支持私聊")
    return ck


_RANK_SQL_BASE = """
SELECT t.sender_id AS sender_id, MAX(t.nick) AS nick, COUNT(*) AS cnt,
       COUNT(*) FILTER (WHERE t.cls = 'video') AS video_n,
       COUNT(*) FILTER (WHERE t.cls = 'voice') AS voice_n,
       COUNT(*) FILTER (WHERE t.cls = 'forward') AS forward_n,
       COUNT(*) FILTER (WHERE t.cls = 'image') AS image_n,
       COUNT(*) FILTER (WHERE t.cls = 'other') AS other_n
FROM (
    SELECT m.sender_id, m.sender_nickname AS nick,
        CASE
            WHEN m.content_data LIKE '%"type": "video"%' THEN 'video'
            WHEN m.content_data LIKE '%"type": "record"%' THEN 'voice'
            WHEN m.content_data LIKE '%"type": "forward"%' THEN 'forward'
            WHEN m.content_data LIKE '%"type": "image"%' THEN 'image'
            WHEN m.content_data LIKE '%"type": "file"%' THEN 'other'
            WHEN m.content_data LIKE '%"type": "json_card"%' THEN 'other'
            WHEN m.content_data LIKE '%"type": "poke"%' THEN 'other'
            WHEN m.content_text LIKE '[Image:%' THEN 'image'
            ELSE 'text'
        END AS cls
    FROM chat_message m
    WHERE m.chat_key = $1 AND m.is_recalled = FALSE{excl}{ts}
) t
GROUP BY t.sender_id
ORDER BY cnt DESC, t.sender_id
LIMIT {lim}
"""
_TOTAL_SQL_BASE = """
SELECT COUNT(*) AS total_msgs, COUNT(DISTINCT m.sender_id) AS total_users
FROM chat_message m
WHERE m.chat_key = $1 AND m.is_recalled = FALSE{excl}{ts}
"""
_EXCLUDE_COND = " AND m.sender_id <> '-1' AND m.sender_id <> ''"


async def _query_rank(chat_key: str, start_ts: int, top_n: int) -> Tuple[List[Dict[str, Any]], int, int]:
    """查询发言排行（含 文字/图片/其他 分项），返回 (行, 人数, 总条数)"""
    from tortoise import Tortoise

    conn = Tortoise.get_connection("default")
    excl = _EXCLUDE_COND if config.EXCLUDE_SYSTEM else ""
    if start_ts > 0:
        ts = " AND m.send_timestamp >= $2"
        rank_sql = _RANK_SQL_BASE.format(excl=excl, ts=ts, lim="$3")
        rank_values: list = [chat_key, start_ts, top_n]
        total_sql = _TOTAL_SQL_BASE.format(excl=excl, ts=ts)
        total_values: list = [chat_key, start_ts]
    else:
        rank_sql = _RANK_SQL_BASE.format(excl=excl, ts="", lim="$2")
        rank_values = [chat_key, top_n]
        total_sql = _TOTAL_SQL_BASE.format(excl=excl, ts="")
        total_values = [chat_key]

    rows = await conn.execute_query_dict(rank_sql, rank_values)
    totals = await conn.execute_query_dict(total_sql, total_values)
    total_msgs = int(totals[0]["total_msgs"]) if totals else 0
    total_users = int(totals[0]["total_users"]) if totals else 0
    return rows, total_users, total_msgs


async def _fetch_avatars(rows: List[Dict[str, Any]]) -> None:
    """并发拉取榜单成员真实 QQ 头像（失败时渲染回退字母头像）"""
    need = [
        r
        for r in rows
        if str(r.get("sender_id", "")).isdigit() and 5 <= len(str(r["sender_id"])) <= 12
    ]
    if not need:
        return
    try:
        try:
            import httpx  # noqa: F401
        except ImportError:
            from nekro_agent.services.plugin.packages import dynamic_import_pkg

            logger.info("msg_rank: 检测到缺少 httpx，开始动态安装...")
            dynamic_import_pkg("httpx")
        import httpx
    except Exception as e:
        logger.warning(f"msg_rank: httpx 不可用，头像将使用字母替代: {e}")
        return

    async def one(r: Dict[str, Any]) -> None:
        url = f"https://q1.qlogo.cn/g?b=qq&nk={r['sender_id']}&s=140"
        try:
            async with httpx.AsyncClient(timeout=6, follow_redirects=True) as client:
                resp = await client.get(url)
                if resp.status_code == 200 and len(resp.content) > 100:
                    r["avatar_bytes"] = resp.content
        except Exception:
            pass

    await asyncio.gather(*(one(r) for r in need))


def _save_dir() -> Path:
    data_dir = Path(os.environ.get("NEKRO_DATA_DIR", "/app/uploads"))
    out = data_dir / "uploads" / "msg_rank"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _safe_name(chat_key: str) -> str:
    return re.sub(r"[^0-9A-Za-z_]", "", chat_key)[-48:] or "chat"


async def _query_channel_name(chat_key: str) -> str:
    """从 chat_channel 表取群名作为卡片标题，失败时用统一标题"""
    try:
        from tortoise import Tortoise

        conn = Tortoise.get_connection("default")
        rows = await conn.execute_query_dict(
            "SELECT channel_name FROM chat_channel WHERE chat_key = $1 LIMIT 1", [chat_key]
        )
        name = (rows[0].get("channel_name") or "").strip() if rows else ""
        return name or "群发言排行榜"
    except Exception:
        return "群发言排行榜"


def _pick_banner():
    files = sorted(_BANNER_DIR.glob("banner_*.jpg")) or sorted(_BANNER_DIR.glob("*.jpg"))
    if not files:
        return None
    import PIL.Image  # noqa: F401  确保 PIL 可用后再打开

    return random.choice(files)


async def build_rank_image(chat_key: str, scope: str, top_n: int) -> Tuple[Path, List[Dict[str, Any]], int, int]:
    """统计 + 拉头像 + 渲染，返回 (图片路径, 行, 人数, 总条数)"""
    start_ts, end_ts = _scope_bounds(scope)
    rows, total_users, total_msgs = await _query_rank(chat_key, start_ts, top_n)
    if not rows:
        raise RuntimeError("该群在统计区间内没有发言记录，无法生成排行榜")

    title = await _query_channel_name(chat_key)
    await _fetch_avatars(rows)

    render = _load_render()
    badge_text, _ = _SCOPE_LABELS[scope]
    if scope == "today":
        date_text = _fmt_md(end_ts)
    else:
        date_text = f"{_fmt_md(start_ts)}-{_fmt_md(end_ts)}"
    range_text = f"{_fmt_full(start_ts)} — {_fmt_full(end_ts)}"
    ongoing_text = f" · 统计中 · 截至 {time.strftime('%H:%M', time.localtime(end_ts))}" if scope == "today" else ""

    banner_path = _pick_banner()
    banner_img = None
    if banner_path:
        try:
            from PIL import Image

            banner_img = Image.open(banner_path)
        except Exception:
            banner_img = None

    out_path = _save_dir() / f"rank_{_safe_name(chat_key)}_{int(time.time())}.png"
    rows_meta = []
    for r in rows:
        cnt = int(r.get("cnt") or 0)
        video_n = int(r.get("video_n") or 0)
        voice_n = int(r.get("voice_n") or 0)
        forward_n = int(r.get("forward_n") or 0)
        image_n = int(r.get("image_n") or 0)
        other_n = int(r.get("other_n") or 0)
        text_n = max(0, cnt - video_n - voice_n - forward_n - image_n - other_n)
        rows_meta.append(
            (
                r.get("nick") or "未知", cnt, text_n, image_n,
                video_n, voice_n, forward_n, other_n,
                r.get("avatar_bytes"),
            )
        )
    render.render_from_render_rows(
        rows_meta,
        total_users=total_users,
        total_msgs=total_msgs,
        badge_text=badge_text,
        date_text=date_text,
        title=title,
        range_text=range_text,
        ongoing_text=ongoing_text,
        banner_img=banner_img,
        out_path=str(out_path),
    )
    return out_path, rows, total_users, total_msgs


def _summary(rows: List[Dict[str, Any]], total_users: int, total_msgs: int, scope: str) -> str:
    _, label = _SCOPE_LABELS[scope]
    top3 = "、".join(f"{r.get('nick') or '未知'}({int(r.get('cnt') or 0)}条)" for r in rows[:3])
    return f"{label}共 {total_users} 人发言、{total_msgs} 条消息，前三名：{top3}"


# ---------------- Agent 工具 ----------------


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="查询群发言排行榜（图片）",
    description=(
        "统计群聊成员累计发言条数，生成排行榜图片并发送到群聊。"
        "scope 为统计范围：today=今日（自然日，从今天 0 点到现在）；week=本周（自然周，从周一 0 点）；"
        "month=本月（自然月，从 1 号 0 点）。不传默认查今日。仅在群聊中使用。"
    ),
)
async def send_group_message_rank_image(
    _ctx: AgentCtx,
    scope: str = "today",
    chat_key: str = "",
) -> str:
    """统计群成员发言条数并发送排行榜图片到当前群聊

    Args:
        scope: 统计范围。today=今日；week=本周；month=本月。默认 today
        chat_key: 目标群聊标识，通常留空表示当前群聊

    Returns:
        str: 统计摘要文本

    Raises:
        RuntimeError: 非群聊频道或统计区间内无发言数据

    Example:
        send_group_message_rank_image()
        send_group_message_rank_image(scope="week")
        send_group_message_rank_image(scope="month")
    """
    ck = _resolve_chat_key(_ctx, chat_key)
    key = _resolve_scope(scope)
    out_path, rows, total_users, total_msgs = await build_rank_image(ck, key, _resolve_top_n(0))
    await send_image(ck, str(out_path), _ctx)
    return f"已生成并发送排行榜图片（{_SCOPE_LABELS[key][1]}）。{_summary(rows, total_users, total_msgs, key)}"


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="查询群发言排行榜（文本）",
    description=(
        "统计群聊成员累计发言条数，以文本形式返回排行榜（不发图片）。"
        "scope 含义同图片工具：today/今日、week/本周、month/本月，默认今日。"
        "需要展示图片时请改用『查询群发言排行榜（图片）』。仅在群聊中使用。"
    ),
)
async def get_group_message_rank_text(
    _ctx: AgentCtx,
    scope: str = "today",
    top_n: int = 10,
    chat_key: str = "",
) -> str:
    """统计群成员发言条数，以文本形式返回排行榜

    Args:
        scope: 统计范围。today=今日；week=本周；month=本月。默认 today
        top_n: 返回的名次数量，默认 10，范围 3~60
        chat_key: 目标群聊标识，通常留空表示当前群聊

    Returns:
        str: 文本排行榜

    Raises:
        RuntimeError: 非群聊频道或统计区间内无发言数据

    Example:
        get_group_message_rank_text()
        get_group_message_rank_text(scope="week", top_n=15)
    """
    ck = _resolve_chat_key(_ctx, chat_key)
    key = _resolve_scope(scope)
    start_ts, _ = _scope_bounds(key)
    rows, total_users, total_msgs = await _query_rank(ck, start_ts, _resolve_top_n(top_n))
    if not rows:
        raise RuntimeError("该群在统计区间内没有发言记录")

    lines = [f"群发言排行榜（{_SCOPE_LABELS[key][1]}，共 {total_users} 人发言、{total_msgs} 条消息）："]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r.get('nick') or '未知'}：{int(r.get('cnt') or 0)} 条")
    return "\n".join(lines)


# ---------------- /发言榜 指令 ----------------


@plugin.mount_command(
    name="发言榜",
    description="查询群发言排行榜，默认统计今日（自然日），可选本周/本月",
    aliases=["发言排行", "排行榜"],
    permission=CommandPermission.PUBLIC,
    usage="发言榜 [今日|本周|本月]",
)
async def cmd_message_rank(
    context: CommandExecutionContext,
    scope: Annotated[str, Arg("统计范围：今日/本周/本月，默认今日")] = "今日",
) -> CommandResponse:
    """处理 /发言榜 指令，生成排行榜图片并作为富媒体输出返回"""
    try:
        key = _resolve_scope(scope)
        out_path, rows, total_users, total_msgs = await build_rank_image(context.chat_key, key, _resolve_top_n(0))
    except RuntimeError as e:
        return CmdCtl.failed(str(e))
    except Exception as e:
        logger.exception("msg_rank: 生成排行榜失败")
        return CmdCtl.failed(f"生成排行榜失败: {e}")

    return CmdCtl.success(
        [
            CommandOutputSegment(
                type=CommandOutputSegmentType.TEXT,
                text=_summary(rows, total_users, total_msgs, key),
            ),
            CommandOutputSegment(
                type=CommandOutputSegmentType.IMAGE,
                file_path=str(out_path),
                file_name=out_path.name,
                mime_type="image/png",
            ),
        ]
    )


@plugin.mount_cleanup_method()
async def clean_up():
    """清理插件资源"""
    logger.info("msg_rank Plugin Resources Cleaned Up")
