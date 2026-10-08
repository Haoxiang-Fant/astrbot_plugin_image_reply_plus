# -*- coding: utf-8 -*-
"""图库plus：关键词图库插件。

- 一个关键词一个文件夹，消息首词命中关键词即随机回一张图
- 上传时识别 pixiv 作品号（纯数字 / 数字_p数字 / 名字里嵌的作品号 / illust_·pixiv_ 前缀）并记录 pid 到 pids.json
- 文件统一重命名为 关键词-编号.扩展名
- 发送时可选择在图片下方附加信息条（水印）：主色方块 + 文件名/PID + pixiv 二维码，
  仅发送环节生成，几何按条高占比缩放，任何图片尺寸视觉一致；中/西文字体可在 WebUI 分别设置；
  是否加水印可按图库单独关闭（缺省开）
- 原插件指令回归：收集（引用消息入库）/ 查看图片 / 删除图片指令，走 AstrBot 命令系统需命令前缀；
  而群里发关键词随机取图仍然不需要任何前缀
- 图库菜单：群里发「图片帮助」得到一张汇总所有图库的菜单图（分区/字母分组/条目=角色名-关键词），
  顶部背景图与各部件字体可在 WebUI「菜单预览」页里改（所见即所得）；每个图库可单独设置
  是否进菜单 / 角色名称 / 所属分组（个人/团体/功能）
- WebUI 为 AstrBot 插件 Pages（仪表盘内管理页），后端 API 经 context.register_web_api 注册
- 二维码生成内嵌 segno（BSD-3-Clause，https://github.com/heuer/segno，见 _segno/LICENSE）
- 全部数据按 AstrBot 规范存放在 data/plugin_data/astrbot_plugin_image_reply_plus/

参考插件：https://github.com/cumany/astrbot_plugin_image_replay
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import random
import re
import shutil
import time
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Reply
from astrbot.api.star import Context, Star, StarTools, register

try:  # 新版 AstrBot（FastAPI 时代）提供 astrbot.api.web
    from astrbot.api.web import error_response, json_response, request
except ImportError:
    # 较早的 Pages 构建没有 web 模块，但 Dashboard 仍是 Quart 上下文，做一层语义一致的薄垫片
    try:
        import quart as _quart

        class _QuartRequestProxy:
            @property
            def method(self):
                return _quart.request.method

            async def json(self, default=None):
                try:
                    data = await _quart.request.get_json(silent=True)
                except Exception:
                    data = None
                return default if data is None else data

        request = _QuartRequestProxy()

        def json_response(data=None, *, status_code=200):
            resp = _quart.jsonify(data if data is not None else {})
            resp.status_code = status_code
            return resp

        def error_response(message, *, status_code=400, data=None):
            return json_response(
                {"status": "error", "message": message, "data": data or {}},
                status_code=status_code,
            )

    except ImportError:  # 连 Quart 都没有：仅保留关键词回复
        request = None

        def json_response(data=None, *, status_code=200):
            return data

        def error_response(message, *, status_code=400, data=None):
            return {"status": "error", "message": message, "data": data or {}}

PLUGIN_NAME = "astrbot_plugin_image_reply_plus"
PLUGIN_VERSION = "0.2.0"  # 唯一出处：@register 与 metadata.yaml 的 version 都用它（selftest 校验一致）
PLUGIN_AUTHOR = "Haoxiang-Fant"  # 同上：@register 与 metadata.yaml 的 author 都用它
PLUGIN_REPO = "https://github.com/Haoxiang-Fant/astrbot_plugin_image_reply_plus"
PID_RE = re.compile(r"^\d+(_p\d+)?$")  # 纯数字 或 数字_p数字（整名即 pid）
PID_ANY_RE = re.compile(r"(\d+)_p(\d+)")  # 名字里嵌的 pixiv 签名（pximg 的 _master1200、PixEz 模板的作者/标题前后缀）
PID_PREFIX_RE = re.compile(r"^(?:illust|pixiv)[_-](\d+)", re.I)  # illust_/pixiv_ 前缀
EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
# 批量导入的"积木"：占位符 → 正则片段。/ {格式} 与 {扩展名} 等价。
FILENAME_BLOCKS = {
    "图库名": r"(?P<kw>.+?)",
    "编号": r"(?P<num>\d+)",
    "pid": r"(?P<pid>\d+(?:_p\d+)?)",
    "扩展名": r"(?P<ext>[A-Za-z0-9]+)",
    "格式": r"(?P<ext>[A-Za-z0-9]+)",
}
BLOCK_RE = re.compile(r"\{(" + "|".join(FILENAME_BLOCKS) + r")\}")


def build_filename_regex(pattern: str):
    """把搭积木式文件名格式编译成正则，返回 (regex, 是否含扩展名块)。

    例："{图库名}-{编号}.{扩展名}" → 可琳照片-1.jpg → kw=可琳照片
    匹配时：含扩展名块匹配完整文件名，否则匹配去扩展名的 stem。
    """
    has_ext = "{扩展名}" in pattern or "{格式}" in pattern
    chunks, pos = [], 0
    for m in BLOCK_RE.finditer(pattern):
        chunks.append(re.escape(pattern[pos : m.start()]))
        chunks.append(FILENAME_BLOCKS[m.group(1)])
        pos = m.end()
    chunks.append(re.escape(pattern[pos:]))
    return re.compile("".join(chunks)), has_ext
MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp",
}
# GBK 区间法取汉字拼音首字母：够排序用（多音字/生僻字可能归错字母，要精确再换 pypinyin）
_PY_RANGE = (
    (0xB0A1, "a"), (0xB0C5, "b"), (0xB2C1, "c"), (0xB4EE, "d"), (0xB6EA, "e"),
    (0xB7A2, "f"), (0xB8C1, "g"), (0xB9FE, "h"), (0xBBF7, "j"), (0xBFA6, "k"),
    (0xC0AC, "l"), (0xC2E8, "m"), (0xC4C3, "n"), (0xC5B6, "o"), (0xC5BE, "p"),
    (0xC6DA, "q"), (0xC8BB, "r"), (0xC8F6, "s"), (0xCBFA, "t"), (0xCDDA, "w"),
    (0xCEF4, "x"), (0xD1B9, "y"), (0xD4D1, "z"),
)


def _gbk_pinyin_key(name: str):
    """pypinyin 没装时的退路：GBK 区间法取拼音首字母（GB2312 一级字准，生僻字会归错）。"""
    key = []
    for ch in name or "":
        c = ch.lower()
        try:
            b = ch.encode("gbk")
        except Exception:
            key.append(c)
            continue
        if len(b) == 1:
            key.append(chr(b[0]).lower())
            continue
        code = (b[0] << 8) | b[1]
        if code < 0xB0A1:  # GBK 符号区，不参与拼音，按原字符排
            key.append(c)
            continue
        for start, letter in _PY_RANGE:
            if code >= start:
                c = letter
            else:
                break
        key.append(c)
    return tuple(key)


def pinyin_key(name: str):
    """图库排序键：逐音节取小写拼音。装了 pypinyin（requirements 自带）生僻字也准；
    没装退回 GBK 区间近似（多音字/生僻字可能归错，见 _gbk_pinyin_key）。"""
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        return _gbk_pinyin_key(name)
    key = []
    for syll in lazy_pinyin(name or ""):
        key.extend(syll.lower())
    return tuple(key)


def _stable_cover(kw: str, d: Path, names: List[str]) -> Optional[str]:
    """封面稳定随机：种子 = 图库名 + 目录 mtime。

    目录内容不变（增删/改名都会动 mtime）→ 每次列表取到同一张，前端缓存不失效；
    内容一变 → 自然换种子，重新随机一次。零存储、零失效钩子。
    """
    if not names:
        return None
    return random.Random(f"{kw}:{int(d.stat().st_mtime)}").choice(names)

FONT_CANDIDATES = (
    "simhei.ttf", "msyh.ttc", "simsun.ttc",
    "notosanscjk-regular.ttc", "notosanssc-regular.otf", "wqy-microhei.ttc", "pingfang.ttc",
)
DEFAULT_CJK_FONT = "simhei.ttf"   # 默认黑体（WebUI 可改）
DEFAULT_LATIN_FONT = "simhei.ttf"
FONT_DIRS = [
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
    Path("/System/Library/Fonts"),
    Path("/System/Library/Fonts/Supplemental"),
]

# 水印条设计常量：均为"占信息条高度的占比"，来自参考样图逐像素测量。
# 条高 = 图片高 × WM_BAR_FRAC，任何尺寸的图输出视觉比例一致。
WM_BAR_FRAC = 0.08
WM_BG = (246, 244, 236)      # 条底色（米白）
WM_FG = (0, 0, 0)            # 文字/二维码
WM_SQUARE = 0.49             # 主色方块边长
WM_SQUARE_X = 0.303          # 方块左边距
WM_TEXT_X = 0.942            # 文字左边距
WM_TEXT1_SIZE = 0.2656       # 第一行（文件名）字号
WM_TEXT2_SIZE = 0.2199       # 第二行（PID 行）字号
WM_TEXT_LINE_GAP = 0.361     # 两行墨迹顶部的间距
WM_QR_SIZE = 0.71            # 二维码边长
WM_QR_RIGHT = 0.361          # 二维码右边距
WM_QR_URL = "https://www.pixiv.net/artworks/{pid}"
WM_FALLBACK_COLOR = (103, 141, 134)  # 样图占位主色，提取不到饱和主色时兜底
# 中日韩字符段（含全角标点/全角字母）→ 用中文字体绘制，其余交给西文字体
CJK_RUN_RE = re.compile(r"[\u1100-\u11ff\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef\u3000-\u303f]+")

# ---------------- 图库菜单设计常量（0.2.0）----------------
# 下面所有数字都是参考样图上的实测像素（样图宽 MENU_REF_W），渲染时统一乘 宽度/MENU_REF_W；
# 改版式只改这里的比例，别在渲染函数里写死像素。
MENU_CMD = "图片帮助"                 # 群里触发菜单的指令名
MENU_REF_W = 1752.0                   # 参考样图宽度（测量基准）
MENU_WIDTH = 1200                     # 发送用菜单图宽度
MENU_PREVIEW_W = 700                  # WebUI 预览宽度（小图省带宽，版式按比例一致）
MENU_BG_COLOR = (255, 251, 239)       # 正文底色（米白）
MENU_RED = (191, 0, 1)                # 主题红（标题块/分区条/右上标签条）
MENU_FG = (30, 30, 30)                # 正文文字
MENU_DIM = (205, 205, 205)            # 右上信息框第一行（浅灰）
MENU_HEADER_H = 303                   # 页眉高
MENU_TITLE_BOX = (66, 0, 327, 152)    # 标题红块 x,y,w,h
MENU_TITLE_XY = (89, 72)              # 标题文字墨迹左上角
MENU_SUB_XY = (84, 159)               # 副标题第一行墨迹左上角
MENU_SUB_PITCH = 30.5                 # 副标题行距
MENU_META_BOX = (1350, 76, 234, 96)   # 右上信息框 x,y,w,h
MENU_META_XY = (1369, 87)             # 信息框第一行墨迹左上角
MENU_META_PITCH = 26.5                # 信息框行距
MENU_LABEL_BAR = (1349, 256, 282, 46) # 右上红标签条 x,y,w,h
MENU_SEC_BAR_X = 88                   # 分区条左边距
MENU_SEC_BAR_H = 47                   # 分区条高
MENU_SEC_BAR_PAD = 17                 # 分区条左右内边距
MENU_COL_X = (123, 718, 1271)         # 三列文字左边（条目）
MENU_COL_NUM_OFF = 12                 # 字母标签比条目更靠左的量
MENU_LETTER_BG = (255, 176, 176)      # 字母标签底下的淡红高亮条（参考图每个字母都垫一条）
MENU_LETTER_W = 24                    # 高亮条宽（固定宽，以字母墨迹中心居中）
MENU_LETTER_H = 8                     # 高亮条高
MENU_LINE_PITCH = 34                  # 正文行距
MENU_GAP_BODY = 26                    # 页眉底/上一分区 → 分区条
MENU_GAP_SEC = 27                     # 分区条 → 第一行文字
MENU_GAP_GROUP = 8                    # 字母组之间的额外间距
MENU_GAP_FOOTER = 67                  # 正文末行 → 页脚
MENU_FOOTER_BAR = (24, 7, 13, 40)     # 页脚左侧红竖条 x,w,y偏移,h
MENU_FOOTER_TX = 40                   # 页脚左栏文字左边
MENU_FOOTER_PITCH = 25                # 页脚行距
MENU_FOOTER_BG = (255, 255, 255)      # 页脚条带底色（纯白，和正文米白做区别）
MENU_FOOTER_BG_PAD = 18               # 白条上沿高出页脚首行墨迹顶的量
MENU_NOTE_X = 1298                    # 右下备注左边
MENU_NOTE_DY = 12                     # 备注首行相对页脚首行的下移
MENU_NOTE_PITCH = 24
MENU_FOOTER_PAD = 25                  # 页脚底 → 图片底边
MENU_FONT_SIZE = {                    # 各部件字号（参考样图像素）
    "title": 52, "subtitle": 25, "meta1": 22, "meta2": 28, "meta3": 25,
    "section": 34, "item": 28, "letter": 26, "label": 24, "footer": 22, "footnote": 22,
}
MENU_GROUPS = (("个人", "单人"), ("团体", "合照"), ("功能", "功能"))  # 配置值 → 分区标题
MENU_GROUP_NAMES = tuple(g for g, _t in MENU_GROUPS)
MENU_COLS = 3                          # 个人/团体 两区的列数
MENU_PARTS = (                         # WebUI 可分别选字体/字号的部件
    ("title", "标题"), ("subtitle", "副标题"), ("meta", "右上信息"),
    ("section", "分区标题"), ("letter", "首字母"), ("item", "图库条目"), ("footer", "底部信息"),
)
MENU_SCRIPTS = (                       # 每个部件再按文字系统分三套字体
    ("cjk", "中文"), ("latin", "英文/数字"), ("jp", "日文"),
)
# 菜单一行字的三段切分：假名→日文字体槽，其余 CJK/全角→中文字体槽，其余（数字/半角标点等）→西文字体槽
MENU_RUN_RE = re.compile(
    r"(?P<jp>[\u3041-\u30ff\u31f0-\u31ff\uff66-\uff9f]+)"
    r"|(?P<cjk>[\u1100-\u11ff\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef\u3000-\u303f]+)"
    r"|(?P<latin>[^\u3041-\u30ff\u31f0-\u31ff\uff66-\uff9f\u1100-\u11ff\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef\u3000-\u303f]+)"
)
MENU_TEXT_KEYS = ("title", "subtitle", "label", "footer", "note")
MENU_TEXT_DEFAULT = {
    "title": MENU_CMD,
    "subtitle": "如果展示的图片涉嫌侵权，请及时联系管理员\n发送指令可以随机获得对应角色的图片\n图源-pixiv",
    "label": "人物名称-对应指令",
    "footer": "基于AstrBot插件图库plus实现",
    "note": "需要添加新的关键词请联系管理员",
}
MENU_TEXT_MAX = 400                    # 信任边界：单条菜单文字上限（防一次粘贴把图撑爆）
MENU_MAX_LINES = 6                     # 多行文字最多渲染几行
MENU_BG_MAX_MB = 10                    # 顶部背景图大小上限
MENU_BG_NAME = "menu_bg"               # 背景图文件名前缀（存插件数据目录根）
MENU_SIZE_MIN, MENU_SIZE_MAX = 50, 200  # 各部件字号的缩放百分比上下限（100 = 参考样式实测值）


def _menu_size_pct(value) -> int:
    """字号百分比：非数字/越界一律收敛（显示旋钮，不是安全边界，收紧点没坏处）。"""
    try:
        return min(max(int(value), MENU_SIZE_MIN), MENU_SIZE_MAX)
    except (TypeError, ValueError):
        return 100


@register(
    PLUGIN_NAME,
    PLUGIN_AUTHOR,
    "图库plus：关键词图库，一关键词一文件夹，WebUI 管理上传，随机回复并附加文件名/PID 水印。",
    PLUGIN_VERSION,
    PLUGIN_REPO,
)
class GalleryPlus(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        try:
            root = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except TypeError:  # ponytail: 旧版 StarTools 若无参签名，退回全局数据目录
            root = Path(StarTools.get_data_dir()) / PLUGIN_NAME
        # data/plugin_data/astrbot_plugin_image_reply_plus/
        self._root = root
        self._galleries = root / "galleries"  # 每个关键词一个文件夹
        self._pid_file = root / "pids.json"   # {"关键词/文件名": "pid"}
        self._tmp = root / "temp"             # 发送用水印临时文件
        self._settings_file = root / "settings.json"  # 水印字体等 WebUI 设置
        self._stats_file = root / "stats.json"  # 发送计数（静默，供后期数据面板）
        self._fonts_dir = root / "fonts"      # 管理员上传的字体文件池
        self._galleries.mkdir(parents=True, exist_ok=True)
        self._tmp.mkdir(parents=True, exist_ok=True)
        self._fonts_dir.mkdir(parents=True, exist_ok=True)
        self._pids: Dict[str, str] = self._load_pids()
        self._settings: Dict[str, Any] = self._load_settings()
        self._stats: Dict[str, Any] = self._load_stats()
        # ponytail: 入库全局锁（并行上传时"算编号+写盘"必须原子，否则同名互覆）；瓶颈出现再拆图库级锁
        self._save_lock = asyncio.Lock()
        self._register_page_apis()

    # ---------------- 消息响应 ----------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        try:
            if self.config.get("require_wake", False) and not getattr(
                event, "is_at_or_wake_command", False
            ):
                return
            msg = (event.message_str or "").strip()
            if not msg:
                return
            if msg == MENU_CMD:  # 菜单：裸发「图片帮助」也出图（下面 @filter.command 是带前缀那条路）
                yield await self._menu_result(event)
                return
            names = {d.name for d in self._galleries.iterdir() if d.is_dir()}
            if not names:
                return
            kw = msg if msg in names else (msg.split()[0] if msg.split() else "")
            if kw not in names:
                return
            folder = self._galleries / kw
            imgs = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in EXTS]
            if not imgs:
                logger.debug(f"关键词“{kw}”图库为空")
                return
            path = random.choice(imgs)
            yield event.chain_result([await self._send_image(path)])
        except Exception as e:
            logger.error(f"图库plus 响应失败: {e}", exc_info=True)

    # ---------------- 原插件指令回归（0.1.3）----------------
    # 指令走 AstrBot 命令系统，需要命令前缀（如 /收集）；关键词回图仍是裸消息触发，无需前缀。

    @filter.command("收集", alias={"添加图片", "添加表情"})
    async def cmd_collect(self, event: AstrMessageEvent):
        """收集 <图库名>：引用一条含图片的消息，把图片收进该图库。"""
        _, args = self._split_command_args(event.message_str or "")
        kw = self._safe_kw(args or "临时")
        datas = await self._images_from_event(event)
        if not datas:
            yield event.plain_result("未在引用消息中找到图片，请先引用需要收集的图片。")
            return
        saved = []
        async with self._save_lock:
            for data in datas:
                p = self.save_upload(kw, f"collect{self._detect_ext(data)}", data)
                saved.append(p.name)
        yield event.plain_result(
            f"已收集 {len(saved)} 张图片进图库「{kw}」：{'、'.join(saved)}"
        )

    @filter.command(MENU_CMD)
    async def cmd_menu(self, event: AstrMessageEvent):
        """图片帮助：发一张汇总所有图库的菜单图（内容/字体/背景在 WebUI「菜单预览」里配）。"""
        yield await self._menu_result(event)

    async def _menu_result(self, event):
        """菜单图的两种返回（出图失败退化成一句提示，别把整个事件打挂）。"""
        path = await asyncio.to_thread(self._render_menu)
        if path is None:
            return event.plain_result("菜单生成失败：请确认已安装 Pillow。")
        return event.chain_result([Image.fromFileSystem(str(path))])

    @filter.command("查看图片")
    async def cmd_view(self, event: AstrMessageEvent):
        """查看图片：从所有图库随机发一张。"""
        imgs = [
            p
            for d in self._galleries.iterdir() if d.is_dir()
            for p in d.iterdir() if p.is_file() and p.suffix.lower() in EXTS
        ]
        if not imgs:
            yield event.plain_result("未找到可用图片，请先创建图库并上传。")
            return
        yield event.chain_result([await self._send_image(random.choice(imgs))])

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删除图片指令", alias={"删除图片"})
    async def cmd_delete(self, event: AstrMessageEvent):
        """删除图片指令 <图库名> [ALL]：删该库最近一张；带 ALL 连整个图库一起删。"""
        _, args = self._split_command_args(event.message_str or "")
        parts = args.split()
        if not parts:
            yield event.plain_result("用法：删除图片指令 <图库名> [ALL]")
            return
        kw = parts[0]
        all_flag = len(parts) > 1 and parts[1].upper() == "ALL"
        d = self._kw_dir(kw)
        imgs = (
            [p for p in d.iterdir() if p.is_file() and p.suffix.lower() in EXTS]
            if d and d.is_dir() else []
        )
        if not imgs:
            yield event.plain_result(f"未找到与「{kw}」匹配的图片。")
            return
        if all_flag:
            self.delete_gallery(d.name)
            yield event.plain_result(f"已删除图库「{d.name}」全部 {len(imgs)} 张图片。")
            return
        target = max(imgs, key=lambda p: p.stat().st_mtime)  # 最近添加的
        self.delete_image(d.name, target.name)
        yield event.plain_result(f"已删除最近添加的图片：{target.name}")

    async def _send_image(self, path: Path):
        """发送一张图库图片，按全局开关与该图库开关决定是否附加信息条。"""
        self._count_send(path.parent.name, path.name)  # 静默计数：图片与图库各一次
        if self.config.get("watermark", True) and self._gallery_wm(path.parent.name):
            path = await asyncio.to_thread(self._render_watermark, path)
        return Image.fromFileSystem(str(path))

    @staticmethod
    def _split_command_args(message: str):
        """首词与其余参数；无参数时第二项为空串。"""
        parts = (message or "").strip().split(maxsplit=1)
        return (parts[0], parts[1]) if len(parts) > 1 else (parts[0] if parts else "", "")

    async def _images_from_event(self, event) -> List[bytes]:
        """收集：取消息本体/引用消息里的全部图片字节。"""
        datas = []
        for seg in self._gather_image_segments(event):
            data = await self._image_bytes(seg)
            if data:
                datas.append(data)
        return datas or await self._reply_images_via_api(event)

    @staticmethod
    def _gather_image_segments(event) -> List:
        segments, seen = [], set()

        def collect(items) -> bool:
            found = False
            for seg in items or []:
                if isinstance(seg, Image) and id(seg) not in seen:
                    seen.add(id(seg))
                    segments.append(seg)
                    found = True
                elif (
                    isinstance(seg, Reply)
                    and getattr(seg, "chain", None)
                    and collect(seg.chain)
                ):
                    found = True
            return found

        chain = event.get_messages()
        reply = next((s for s in chain if isinstance(s, Reply)), None)
        if reply and collect(reply.chain or []):
            return segments
        collect(chain)
        if not segments:  # 某些适配器图片不在 get_messages 里，翻原始消息
            raw = getattr(getattr(event, "message_obj", None), "message", None)
            if isinstance(raw, (list, tuple)):
                collect(raw)
        return segments

    async def _image_bytes(self, seg) -> Optional[bytes]:
        """Image 段 → 字节。convert_to_base64 是 AstrBot 原生方法，统一处理 url/file/base64。"""
        try:
            b64 = seg.convert_to_base64()
            if asyncio.iscoroutine(b64):
                b64 = await b64
            if isinstance(b64, bytes):
                return b64
            if isinstance(b64, str):
                return base64.b64decode(b64[9:] if b64.startswith("base64://") else b64)
        except Exception as e:
            logger.warning(f"读取图片段失败: {e}")
        return None

    async def _reply_images_via_api(self, event) -> List[bytes]:
        """aiocqhttp 兜底：引用链里没有图片段时，用 get_msg 拉原消息取图。"""
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )
        except Exception:
            return []
        if not isinstance(event, AiocqhttpMessageEvent):
            return []
        reply = next((s for s in event.get_messages() if isinstance(s, Reply)), None)
        mid = getattr(reply, "id", None) or getattr(reply, "message_id", None)
        client = getattr(event, "bot", None)
        if not mid or not client:
            return []
        try:
            res = await client.api.call_action("get_msg", message_id=str(mid))
        except Exception as e:
            logger.warning(f"获取引用消息失败: {e}")
            return []
        datas = []
        for node in res.get("message") or []:
            if not isinstance(node, Mapping) or node.get("type") != "image":
                continue
            url = (node.get("data") or {}).get("url")
            if isinstance(url, str) and url:
                data = await self._download(url)
                if data:
                    datas.append(data)
        return datas

    async def _download(self, url: str) -> Optional[bytes]:
        try:
            import aiohttp

            async with aiohttp.ClientSession() as s:
                async with s.get(url) as r:
                    r.raise_for_status()
                    return await r.read()
        except Exception as e:
            logger.warning(f"图片下载失败: {e}")
            return None

    # ---------------- 存储 ----------------

    def _load_pids(self) -> Dict[str, str]:
        try:
            return json.loads(self._pid_file.read_text("utf-8"))
        except Exception:
            return {}

    def _save_pids(self) -> None:
        self._pid_file.write_text(
            json.dumps(self._pids, ensure_ascii=False, indent=2), "utf-8"
        )

    def _load_stats(self) -> Dict[str, Any]:
        try:
            data = json.loads(self._stats_file.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _count_send(self, kw: str, file: str) -> None:
        """静默发送计数：图片/图库各 +1，并记 24 小时时段分布（stats.json，暂无界面）。"""
        hour = time.localtime().tm_hour
        for table, key in (
            (self._stats.setdefault("images", {}), f"{kw}/{file}"),
            (self._stats.setdefault("galleries", {}), kw),
        ):
            row = table.setdefault(key, {"sends": 0, "hours": [0] * 24})
            row["sends"] += 1
            row["hours"][hour] += 1
        try:
            self._save_stats()
        except OSError:
            pass  # ponytail: 每次发送全量落盘，bot 量级无碍；高频了再改防抖/追加日志

    def _save_stats(self) -> None:
        self._stats_file.write_text(json.dumps(self._stats, ensure_ascii=False), "utf-8")

    @staticmethod
    def _safe_kw(kw: str) -> str:
        """清洗成合法文件夹名，同时杜绝路径穿越。"""
        cleaned = "".join(
            "_" if (c in '<>:"/\\|?*' or ord(c) < 32) else c for c in (kw or "").strip()
        )
        return cleaned.strip("-. ")[:48] or "未命名"

    def _kw_dir(self, kw: str) -> Optional[Path]:
        safe = self._safe_kw(kw)
        if safe != kw:  # 有改动说明输入含非法字符/穿越企图
            return None
        return self._galleries / safe

    def _next_num(self, folder: Path, kw: str) -> int:
        nums = [
            int(m.group(1))
            for p in folder.iterdir()
            if p.is_file() and (m := re.fullmatch(rf"{re.escape(kw)}-(\d+)", p.stem))
        ]
        return max(nums) + 1 if nums else 1

    @staticmethod
    def _detect_ext(data: bytes) -> str:
        try:
            from PIL import Image
            fmt = Image.open(io.BytesIO(data)).format
            return {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "BMP": ".bmp", "WEBP": ".webp"}.get(fmt, ".jpg")
        except Exception:
            return ".jpg"

    @staticmethod
    def extract_pid(filename: str) -> Optional[str]:
        """从文件名取 pixiv 作品号：整名即 pid → 名字里嵌 `数字_p数字` → illust_/pixiv_ 前缀；取不到返回 None。

        例：95026140_p0.jpg / 95026140.jpg / 95026140_p0_master1200.jpg /
            作者名_95026140_p0.jpg / illust_95026140_20230101.jpg 都能取到。
        """
        stem = Path(filename).stem
        if m := PID_ANY_RE.search(stem):
            return f"{m.group(1)}_p{m.group(2)}"
        if m := PID_PREFIX_RE.match(stem):
            return m.group(1)
        return stem if PID_RE.fullmatch(stem) else None

    def save_upload(
        self, kw: str, filename: str, data: bytes, pid: Optional[str] = None
    ) -> Path:
        """入库：识别 pid、按 关键词-编号 重命名、记录 pid 映射。显式传入 pid 时优先（批量导入用）。"""
        folder = self._galleries / self._safe_kw(kw)
        folder.mkdir(parents=True, exist_ok=True)
        kw = folder.name
        ext = Path(filename).suffix.lower()
        if ext not in EXTS:
            ext = self._detect_ext(data)
        if pid is None:
            pid = self.extract_pid(filename)
        n = self._next_num(folder, kw)
        target = folder / f"{kw}-{n}{ext}"
        while target.exists():
            n += 1
            target = folder / f"{kw}-{n}{ext}"
        target.write_bytes(data)
        if pid:
            self._pids[f"{kw}/{target.name}"] = pid
            self._save_pids()
        return target

    def import_files(self, pattern: str, files: List[dict]) -> dict:
        """批量导入：按搭积木式文件名格式解析，自动分库到对应关键词。

        不符合格式/格式不支持的文件不猜、直接跳过并在结果里说明原因。
        """
        rx, has_ext = build_filename_regex(pattern)
        imported: List[dict] = []
        skipped: List[dict] = []
        for f in files or []:
            name = str(f.get("name", ""))
            b64 = str(f.get("data", ""))
            if "," in b64:  # 容忍 dataURL 前缀
                b64 = b64.split(",", 1)[1]
            if not name or not b64:
                skipped.append({"name": name or "(空)", "reason": "缺少文件名或内容"})
                continue
            if Path(name).suffix.lower() not in EXTS:
                skipped.append({"name": name, "reason": "不支持的图片格式"})
                continue
            m = rx.fullmatch(name if has_ext else Path(name).stem)
            if not m:
                skipped.append({"name": name, "reason": "文件名不符合设定格式"})
                continue
            try:
                data = base64.b64decode(b64)
            except Exception:
                skipped.append({"name": name, "reason": "内容解码失败"})
                continue
            if not data:
                skipped.append({"name": name, "reason": "内容为空"})
                continue
            kw = self._safe_kw(m.group("kw"))
            p = self.save_upload(kw, name, data, pid=m.groupdict().get("pid"))
            imported.append(
                {"file": p.name, "gallery": kw, "pid": self._pids.get(f"{kw}/{p.name}")}
            )
        return {"ok": True, "imported": imported, "skipped": skipped}

    def delete_image(self, kw: str, name: str) -> bool:
        d = self._kw_dir(kw)
        if not d or Path(name).name != name:
            return False
        p = d / name
        if not p.is_file():
            return False
        p.unlink()
        for t in (self._tmp / "thumbs").glob(f"{d.name}_{p.stem}.*.thumb.jpg"):
            t.unlink(missing_ok=True)  # 各档尺寸的缓存一起清（. 后接尺寸，不会误伤同前缀文件名）
        self._pids.pop(f"{d.name}/{name}", None)
        self._save_pids()
        return True

    def delete_gallery(self, kw: str) -> bool:
        d = self._kw_dir(kw)
        if not d or not d.is_dir():
            return False
        shutil.rmtree(d)
        for t in (self._tmp / "thumbs").glob(f"{d.name}_*.thumb.jpg"):
            t.unlink(missing_ok=True)
        for key in [k for k in self._pids if k.startswith(f"{d.name}/")]:
            self._pids.pop(key, None)
        self._save_pids()
        if isinstance(self._settings.get("gallery_wm"), dict):  # 库没了，水印开关一并清掉
            self._settings["gallery_wm"].pop(d.name, None)
            self._save_settings()
        if isinstance(self._settings.get("gallery_menu"), dict):  # 菜单配置同理，别留脏键
            self._settings["gallery_menu"].pop(d.name, None)
            self._save_settings()
        return True

    def list_galleries(self) -> List[dict]:
        out: List[dict] = []
        for d in sorted(self._galleries.iterdir(), key=lambda p: pinyin_key(p.name)):
            if not d.is_dir():
                continue
            names = sorted(
                p.name for p in d.iterdir()
                if p.is_file() and p.suffix.lower() in EXTS
            )
            out.append(
                {
                    "name": d.name,
                    "count": len(names),
                    "wm": self._gallery_wm(d.name),
                    "cover": _stable_cover(d.name, d, names),
                    "images": [
                        {"file": n, "pid": self._pids.get(f"{d.name}/{n}")} for n in names
                    ],
                }
            )
        return out

    def list_gallery_names(self, offset: int = 0, limit: Optional[int] = None):
        """分页列出图库（名称、张数、水印开关、随机封面），供 WebUI 懒加载。返回 (页数据, 总数)。"""
        names = sorted((d.name for d in self._galleries.iterdir() if d.is_dir()), key=pinyin_key)
        page = names if limit is None else names[offset : offset + limit]
        out = []
        for name in page:
            d = self._galleries / name
            files = [
                p.name for p in d.iterdir()
                if p.is_file() and p.suffix.lower() in EXTS
            ]
            out.append(
                {
                    "name": name,
                    "count": len(files),
                    "wm": self._gallery_wm(name),
                    "cover": _stable_cover(name, d, files),
                }
            )
        return out, len(names)

    def list_images(self, kw: str, offset: int = 0, limit: Optional[int] = None):
        """分页列出某图库的图片（文件名+pid），供 WebUI 懒加载。返回 (页数据, 总数)。"""
        d = self._kw_dir(kw)
        if not d or not d.is_dir():
            return [], 0
        names = sorted(
            p.name for p in d.iterdir()
            if p.is_file() and p.suffix.lower() in EXTS
        )
        page = names if limit is None else names[offset : offset + limit]
        return (
            [{"file": n, "pid": self._pids.get(f"{d.name}/{n}")} for n in page],
            len(names),
            self._settings.get("thumb_cols", 7),  # 顺带带回每行张数偏好，二级页开页即用
        )

    # ---------------- 水印 ----------------

    def _load_settings(self) -> Dict[str, str]:
        try:
            data = json.loads(self._settings_file.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_settings(self) -> None:
        self._settings_file.write_text(
            json.dumps(self._settings, ensure_ascii=False, indent=2), "utf-8"
        )

    def _gallery_wm(self, kw: str) -> bool:
        """单图库水印开关：settings.json 里的 gallery_wm 表，缺省开。"""
        m = self._settings.get("gallery_wm")
        return bool(m.get(kw, True)) if isinstance(m, dict) else True

    def set_gallery_wm(self, kw: str, enabled: bool) -> None:
        self._settings.setdefault("gallery_wm", {})[kw] = bool(enabled)
        self._save_settings()

    def _wm_fonts(self, size: int):
        """按设置取 (中文字体, 西文字体)，各自带回退链（含管理员上传池）。"""
        d = str(self._fonts_dir)
        return (
            _resolve_font(self._settings.get("wm_font_cjk") or DEFAULT_CJK_FONT, size, d),
            _resolve_font(self._settings.get("wm_font_latin") or DEFAULT_LATIN_FONT, size, d),
        )

    @staticmethod
    def _theme_color(im):
        """图片主色：量化聚类后取 数量×饱和度 最高的中间调簇，压暗成样图那种低饱和调。"""
        try:
            t = im.convert("RGB").resize((120, 120))
            q = t.quantize(16).convert("RGB")
            cnt = Counter(q.get_flattened_data() if hasattr(q, "get_flattened_data") else q.getdata())
            sat = lambda c: max(c) - min(c)  # noqa: E731
            cands = [
                (c, n) for c, n in cnt.items()
                if sat(c) >= 25 and not (max(c) > 217 and sat(c) < 60)  # 排除过亮的近中性色（地板/留白）
            ]
            if not cands:
                return WM_FALLBACK_COLOR
            c, _ = max(cands, key=lambda cn: cn[1] * sat(cn[0]))
            return tuple(int(v * 0.75) for v in c)
        except Exception:
            return WM_FALLBACK_COLOR

    @staticmethod
    def _load_segno():
        """按文件位置加载内嵌 segno（不依赖插件目录在 sys.path，也不与 pip 版冲突）。"""
        import importlib.util
        import sys

        if "_segno" in sys.modules:
            return sys.modules["_segno"]
        init = Path(__file__).parent / "_segno" / "__init__.py"
        spec = importlib.util.spec_from_file_location(
            "_segno", init, submodule_search_locations=[str(init.parent)]
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["_segno"] = mod
        spec.loader.exec_module(mod)
        return mod

    @staticmethod
    def _qr_image(pid: str, px: int):
        """pixiv 作品页二维码（内嵌 segno，BSD-3-Clause，见 _segno/LICENSE）。

        pid 形如 95026140_p3 时取数字主体作 URL；生成失败返回 None（信息条照常，无码）。
        """
        from PIL import Image, ImageDraw

        try:
            segno = GalleryPlus._load_segno()

            m = re.match(r"\d+", pid)
            qr = segno.make_qr(WM_QR_URL.format(pid=m.group() if m else pid), error="m")
            matrix = qr.matrix
            n = len(matrix)
            s = 8  # 先放大渲染再 NEAREST 收缩到目标像素，避免模块缝隙
            img = Image.new("L", (n * s, n * s), 255)
            d = ImageDraw.Draw(img)
            for y, row in enumerate(matrix):
                for x, v in enumerate(row):
                    if v:
                        d.rectangle((x * s, y * s, (x + 1) * s - 1, (y + 1) * s - 1), fill=0)
            return img.resize((px, px), Image.NEAREST)
        except Exception as e:
            logger.warning(f"二维码生成失败（信息条不含码）: {e}")
            return None

    @staticmethod
    def _script_runs(text: str):
        """按 中日韩/西文 切分文字，各自选用字体。返回 [(片段, 是否CJK)]。"""
        runs, i = [], 0
        for m in CJK_RUN_RE.finditer(text):
            if m.start() > i:
                runs.append((text[i : m.start()], False))
            runs.append((m.group(), True))
            i = m.end()
        if i < len(text):
            runs.append((text[i:], False))
        return runs or [(text, False)]

    @staticmethod
    def _line_image(text: str, size: int, f_cjk, f_lat, color=WM_FG):
        """把一行混排文字渲染成透明条带图（墨迹紧贴边缘，便于按墨迹顶部定位）。"""
        from PIL import Image, ImageDraw

        fonts = []
        for run, is_cjk in GalleryPlus._script_runs(text):
            f = f_cjk if is_cjk else f_lat
            if f not in fonts:
                fonts.append(f)
        asc = max(f.getmetrics()[0] for f in fonts)
        desc = max(f.getmetrics()[1] for f in fonts)
        img = Image.new("RGBA", (int(size * len(text) * 1.6) + 8, asc + desc + 8), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        x = 4.0
        for run, is_cjk in GalleryPlus._script_runs(text):
            f = f_cjk if is_cjk else f_lat
            d.text((x, asc), run, font=f, fill=color + (255,), anchor="ls")
            x += d.textlength(run, font=f)
        bb = img.getbbox()
        return img.crop(bb) if bb else img

    def _render_watermark(self, path: Path) -> Path:
        """发送时在图片下方拼信息条：主色方块 + 文件名/PID + pixiv 二维码。

        几何全部按条高占比换算（条高=图高×8%），任何尺寸视觉一致；动图跳过。
        """
        if path.suffix.lower() == ".gif":
            return path
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            return path
        try:
            pid = self._pids.get(f"{path.parent.name}/{path.name}")
            im = Image.open(path).convert("RGB")
            w, h = im.size
            b = max(round(h * WM_BAR_FRAC), 1)
            canvas = Image.new("RGB", (w, h + b), WM_BG)
            canvas.paste(im, (0, 0))
            d = ImageDraw.Draw(canvas)
            top = h  # 信息条 y 原点 = 原图底边
            # 主色方块（垂直居中）
            sq = round(b * WM_SQUARE)
            d.rectangle(
                (round(b * WM_SQUARE_X), top + (b - sq) // 2,
                 round(b * WM_SQUARE_X) + sq - 1, top + (b - sq) // 2 + sq - 1),
                fill=self._theme_color(im),
            )
            # 两行文字（中/西文字体可分设；行宽超出可用空间时逐级缩字号）
            qr_px = round(b * WM_QR_SIZE)
            qr_right = round(b * WM_QR_RIGHT)
            max_text_w = w - round(b * WM_TEXT_X) - (qr_px + qr_right + round(b * 0.2) if pid else round(b * 0.3))
            line1 = self._fit_line(path.name, round(b * WM_TEXT1_SIZE), max_text_w)
            line2_text = f"PID {pid}" if pid else "暂无 PID信息"
            line2 = self._fit_line(line2_text, round(b * WM_TEXT2_SIZE), max_text_w)
            # 两行作为一整块垂直居中（gap 是两行墨迹顶部的距离，块高 = gap + 第二行高）
            gap = round(b * WM_TEXT_LINE_GAP)
            t_top = (b - (gap + line2.height)) // 2
            canvas.paste(line1, (round(b * WM_TEXT_X), top + t_top), line1)
            canvas.paste(line2, (round(b * WM_TEXT_X), top + t_top + gap), line2)
            # 二维码（有 pid 才画，垂直居中）
            if pid:
                qr = self._qr_image(pid, qr_px)
                if qr is not None:
                    canvas.paste(
                        qr,
                        (w - qr_px - qr_right, top + (b - qr_px) // 2),
                    )
            fmt = "png" if str(self.config.get("output_format", "jpg")).lower() == "png" else "jpg"
            out = self._tmp / f"{path.parent.name}_{path.stem}.wm.{fmt}"
            if fmt == "png":
                canvas.save(out, "PNG")
            else:
                canvas.save(out, "JPEG", quality=85)
            return out
        except Exception as e:
            logger.warning(f"水印生成失败，发送原图: {e}")
            return path

    def _fit_line(self, text: str, size: int, max_w: int):
        """渲染一行文字；超宽时缩小字号重渲（最长以可用宽度为限）。"""
        f_cjk, f_lat = self._wm_fonts(size)
        img = self._line_image(text, size, f_cjk, f_lat)
        while img.width > max_w and size > 8:
            size -= 2
            f_cjk, f_lat = self._wm_fonts(size)
            img = self._line_image(text, size, f_cjk, f_lat)
        return img

    # ---------------- 图库菜单（0.2.0）----------------

    def _menu_settings(self) -> dict:
        """settings.json 里的 menu 表（不是 dict 就地修正，省得各处判断）。"""
        m = self._settings.get("menu")
        if not isinstance(m, dict):
            m = {}
            self._settings["menu"] = m
        return m

    def _gallery_menu(self, kw: str) -> Dict[str, Any]:
        """单图库的菜单配置。缺省：进菜单、角色名=关键词、分组=个人。"""
        m = self._settings.get("gallery_menu")
        row = m.get(kw) if isinstance(m, dict) else None
        row = row if isinstance(row, dict) else {}
        group = row.get("group") if row.get("group") in MENU_GROUP_NAMES else MENU_GROUP_NAMES[0]
        return {
            "show": bool(row.get("show", True)),
            "char": str(row.get("char") or kw),
            "group": group,
        }

    def set_gallery_menu(self, kw: str, show: bool, char: str, group: str) -> Dict[str, Any]:
        if group not in MENU_GROUP_NAMES:  # 信任边界：分组只认三个枚举值
            group = MENU_GROUP_NAMES[0]
        table = self._settings.get("gallery_menu")
        if not isinstance(table, dict):  # settings.json 被手改坏时也别炸
            table = {}
            self._settings["gallery_menu"] = table
        table[kw] = {
            "show": bool(show),
            "char": (char or kw).strip()[:24] or kw,
            "group": group,
        }
        self._save_settings()
        return self._gallery_menu(kw)

    def _menu_config(self) -> Dict[str, Any]:
        """菜单文字 + 各部件字体（中/英/日三槽）/字号（缺项回落默认值）。"""
        m = self._settings.get("menu")
        m = m if isinstance(m, dict) else {}
        fonts = m.get("fonts") if isinstance(m.get("fonts"), dict) else {}
        sizes = m.get("sizes") if isinstance(m.get("sizes"), dict) else {}
        out: Dict[str, Any] = {k: str(m.get(k) or MENU_TEXT_DEFAULT[k]) for k in MENU_TEXT_KEYS}

        def slots(v) -> Dict[str, str]:
            if isinstance(v, dict):  # 新格式 {"cjk":…,"latin":…,"jp":…}
                return {s: str(v.get(s) or DEFAULT_CJK_FONT) for s, _lbl in MENU_SCRIPTS}
            name = str(v or DEFAULT_CJK_FONT)  # 旧格式（单个字体名）→ 三槽同字体
            return {s: name for s, _lbl in MENU_SCRIPTS}

        out["fonts"] = {p: slots(fonts.get(p)) for p, _lbl in MENU_PARTS}
        out["sizes"] = {p: _menu_size_pct(sizes.get(p)) for p, _lbl in MENU_PARTS}
        return out

    def _menu_bg_path(self) -> Optional[Path]:
        """管理员上传的页眉背景图（settings 里只存文件名，防路径穿越）。"""
        m = self._settings.get("menu")
        name = str((m or {}).get("bg") or "") if isinstance(m, dict) else ""
        if not name or Path(name).name != name:
            return None
        p = self._root / name
        return p if p.is_file() else None

    def set_menu_bg(self, data: bytes, ext: str) -> str:
        """保存页眉背景图：同名旧图先删（只留一张），文件名固定前缀 + 扩展名。"""
        for old in self._root.glob(f"{MENU_BG_NAME}.*"):
            old.unlink(missing_ok=True)
        target = self._root / f"{MENU_BG_NAME}{ext}"
        target.write_bytes(data)
        self._menu_settings()["bg"] = target.name
        self._save_settings()
        return target.name

    def _menu_data(self) -> Dict[str, List[tuple]]:
        """按分区列出进菜单的 (角色名, 关键词)，分区内按拼音排序。"""
        out: Dict[str, List[tuple]] = {g: [] for g in MENU_GROUP_NAMES}
        for d in sorted(self._galleries.iterdir(), key=lambda p: pinyin_key(p.name)):
            if not d.is_dir():
                continue
            m = self._gallery_menu(d.name)
            if m["show"]:
                out.setdefault(m["group"], []).append((m["char"], d.name))
        for rows in out.values():
            rows.sort(key=lambda ck: (pinyin_key(ck[0]), pinyin_key(ck[1])))
        return out

    def _menu_stats(self):
        """(图片总数, 最后上传时间戳)；无图时时间戳为 0。"""
        total, newest = 0, 0.0
        for d in self._galleries.iterdir():
            if not d.is_dir():
                continue
            for p in d.iterdir():
                if p.is_file() and p.suffix.lower() in EXTS:
                    total += 1
                    newest = max(newest, p.stat().st_mtime)
        return total, newest

    @staticmethod
    def _letter_of(name: str) -> str:
        """角色的字母标签：拼音首字母大写；取不到字母的归到 "#"。"""
        k = pinyin_key(name)
        c = k[0] if k else "#"
        return c.upper() if c.isalpha() else "#"

    @staticmethod
    def _menu_columns(groups, n: int = MENU_COLS):
        """字母组按行数均衡切成 n 列（贪心：攒够 总行数/n 换列），保持字母顺序。"""
        if not groups:
            return []
        # 每组的行数 = 字母行 + 条目行 + 组间距；总高除以列数就是每列的目标行数
        per = sum(len(e) + 2 for _l, e in groups) / n
        cols, cur, h = [], [], 0
        for g in groups:
            cur.append(g)
            h += len(g[1]) + 2
            if len(cols) < n - 1 and h >= per:
                cols.append(cur)
                cur, h = [], 0
        cols.append(cur)
        return [c for c in cols if c]

    def _render_menu(self, width: Optional[int] = None) -> Optional[Path]:
        """渲染图库菜单图：版式按参考样图复刻，全部尺寸随宽度等比缩放。返回文件路径。"""
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            logger.warning("未安装 Pillow，图库菜单不可用")
            return None
        try:
            W = min(max(int(width or MENU_WIDTH), 400), 2000)
            u = W / MENU_REF_W
            P = lambda v: int(round(v * u))  # noqa: E731  参考样图像素 → 输出像素
            cfg = self._menu_config()
            fdir = str(self._fonts_dir)

            def f(part: str, size: int) -> Dict[str, Any]:
                """部件在该参考字号（×百分比）下的 中/英/日 三套字体。"""
                size = max(round(size * cfg["sizes"].get(part, 100) / 100), 6)
                slots = cfg["fonts"][part]
                return {s: _resolve_font(slots[s], size, fdir) for s, _lbl in MENU_SCRIPTS}

            def fit(text: str, part: str, size: int, max_w: int, color=MENU_FG):
                """按可用宽度逐级缩字号（菜单文字可配置，防溢出）。返回墨迹条带图。"""
                while size > 8:
                    strip = _menu_line(text, f(part, P(size)), color)
                    if strip.width <= max_w:
                        return strip
                    size -= 2
                return _menu_line(text, f(part, P(size)), color)

            # ---- 内容 ----
            data = self._menu_data()
            total, newest = self._menu_stats()
            import datetime as _dt

            up = _dt.datetime.fromtimestamp(newest) if newest else _dt.datetime.now()
            meta = (
                ("图库更新日期", "meta1", MENU_DIM),
                (f"CST {up.year:04d}/{up.month:02d}/{up.day:02d}", "meta2", (255, 255, 255)),
                (f"总 {total:,}张", "meta3", (255, 255, 255)),
            )
            now = _dt.datetime.now()
            subtitle = [ln.strip() for ln in cfg["subtitle"].splitlines() if ln.strip()][:MENU_MAX_LINES]
            note = [ln.strip() for ln in cfg["note"].splitlines() if ln.strip()][:MENU_MAX_LINES]
            foot = (
                cfg["footer"].strip() or MENU_TEXT_DEFAULT["footer"],
                f"生成时间 中国标准时间(CST) {now.year}/{now.month}/{now.day} {now:%H:%M}",
            )

            # ---- 先排正文算总高（y 全部用参考像素，最后一起缩放）----
            ops: List[tuple] = []  # (kind, text, x_ref, y_ref)
            y = MENU_HEADER_H
            for gname, sec_title in MENU_GROUPS:
                rows = data.get(gname) or []
                if not rows:
                    continue
                y += MENU_GAP_BODY
                ops.append(("section", sec_title, MENU_SEC_BAR_X, y))
                cursor = y + MENU_SEC_BAR_H + MENU_GAP_SEC
                if gname == MENU_GROUPS[-1][0]:  # 功能分区不按字母分组，单列
                    for char, kw in rows:
                        ops.append(("item", f"{char}-{kw}", MENU_COL_X[0], cursor))
                        cursor += MENU_LINE_PITCH
                else:
                    groups: List[tuple] = []
                    for char, kw in rows:  # rows 已排序 → 同字母天然相邻
                        lt = self._letter_of(char)
                        if not groups or groups[-1][0] != lt:
                            groups.append((lt, []))
                        groups[-1][1].append((char, kw))
                    top = cursor  # 各列都从分区正文首行开始（cursor 是"本区已用最大高度"）
                    for ci, col in enumerate(self._menu_columns(groups)):
                        cy = top
                        for lt, entries in col:
                            ops.append(("letter", lt, MENU_COL_X[ci] - MENU_COL_NUM_OFF, cy))
                            cy += MENU_LINE_PITCH
                            for char, kw in entries:
                                ops.append(("item", f"{char}-{kw}", MENU_COL_X[ci], cy))
                                cy += MENU_LINE_PITCH
                            cy += MENU_GAP_GROUP
                        cursor = max(cursor, cy)
                y = cursor
            footer_y = y + MENU_GAP_FOOTER
            H = P(footer_y + MENU_FOOTER_PITCH + MENU_FONT_SIZE["footnote"] + MENU_FOOTER_PAD)

            # ---- 画 ----
            canvas = Image.new("RGB", (W, H), MENU_BG_COLOR)
            d = ImageDraw.Draw(canvas)
            hh = P(MENU_HEADER_H)
            bg = self._load_menu_bg()
            # ponytail: 背景图不加压暗蒙版（样图也没有）；白字看不清就换张深色背景图
            canvas.paste(_cover_crop(bg, W, hh) if bg is not None else _menu_default_bg(W, hh), (0, 0))

            tb = MENU_TITLE_BOX  # 标题红块 + 白色标题（超宽逐级缩字号）
            d.rectangle((P(tb[0]), P(tb[1]), P(tb[0] + tb[2]) - 1, P(tb[1] + tb[3]) - 1), fill=MENU_RED)
            title = cfg["title"].strip() or MENU_TEXT_DEFAULT["title"]
            tf = fit(title, "title", MENU_FONT_SIZE["title"], P(tb[2] - 46), (255, 255, 255))
            canvas.paste(tf, (P(MENU_TITLE_XY[0]), P(MENU_TITLE_XY[1])), tf)

            for i, ln in enumerate(subtitle):
                sf = _menu_line(ln, f("subtitle", P(MENU_FONT_SIZE["subtitle"])), (255, 255, 255))
                canvas.paste(sf, (P(MENU_SUB_XY[0]), P(MENU_SUB_XY[1] + i * MENU_SUB_PITCH)), sf)

            mb = MENU_META_BOX  # 右上信息框：半透明暗底 + 浅色细框 + 三行
            box = Image.new("RGBA", (P(mb[2]), P(mb[3])), (18, 14, 15, 150))
            ImageDraw.Draw(box).rectangle(
                (0, 0, box.width - 1, box.height - 1), outline=(210, 210, 210, 220), width=1
            )
            canvas.paste(box, (P(mb[0]), P(mb[1])), box)
            meta_max_w = P(mb[2] - 2 * (MENU_META_XY[0] - mb[0]))
            for i, (txt, key, color) in enumerate(meta):
                mf = fit(txt, "meta", MENU_FONT_SIZE[key], meta_max_w, color)
                canvas.paste(mf, (P(MENU_META_XY[0]), P(MENU_META_XY[1] + i * MENU_META_PITCH)), mf)

            lb = MENU_LABEL_BAR  # 右上红标签条（居中白字）
            d.rectangle((P(lb[0]), P(lb[1]), P(lb[0] + lb[2]) - 1, P(lb[1] + lb[3]) - 1), fill=MENU_RED)
            ltext = cfg["label"].strip() or MENU_TEXT_DEFAULT["label"]
            lf = fit(ltext, "section", MENU_FONT_SIZE["label"], P(lb[2] - 20), (255, 255, 255))
            canvas.paste(lf, (P(lb[0]) + (P(lb[2]) - lf.width) // 2,
                              P(lb[1]) + (P(lb[3]) - lf.height) // 2), lf)

            for kind, text, x, yr in ops:
                if kind == "section":
                    st = _menu_line(text, f("section", P(MENU_FONT_SIZE["section"])), (255, 255, 255))
                    bw = st.width + P(2 * MENU_SEC_BAR_PAD)
                    d.rectangle((P(x), P(yr), P(x) + bw - 1, P(yr + MENU_SEC_BAR_H) - 1), fill=MENU_RED)
                    canvas.paste(st, (P(x) + P(MENU_SEC_BAR_PAD),
                                      P(yr) + (P(MENU_SEC_BAR_H) - st.height) // 2), st)
                elif kind == "letter":
                    lt = _menu_line(text, f("letter", P(MENU_FONT_SIZE["letter"])))
                    bx = P(x) + lt.width // 2 - P(MENU_LETTER_W) // 2  # 高亮条以字母墨迹中心居中
                    by = P(yr) + lt.height // 2 - P(MENU_LETTER_H) // 2
                    d.rectangle((bx, by, bx + P(MENU_LETTER_W) - 1, by + P(MENU_LETTER_H) - 1),
                                fill=MENU_LETTER_BG)
                    canvas.paste(lt, (P(x), P(yr)), lt)
                else:
                    it = fit(text, "item", MENU_FONT_SIZE["item"], P(MENU_COL_X[1] - MENU_COL_X[0] - 20))
                    canvas.paste(it, (P(x), P(yr)), it)

            # 页脚条带：纯白，和正文米白做区别（画在页脚文字之前，正文内容够不到这里）
            d.rectangle((0, P(footer_y - MENU_FOOTER_BG_PAD), W, H - 1), fill=MENU_FOOTER_BG)

            fb = MENU_FOOTER_BAR  # 页脚：左侧红竖条 + 两行说明；右下角备注
            d.rectangle((P(fb[0]), P(int(footer_y) + fb[2]), P(fb[0] + fb[1]) - 1, P(int(footer_y) + fb[2] + fb[3]) - 1), fill=MENU_RED)
            f1 = _menu_line(foot[0], f("footer", P(MENU_FONT_SIZE["footer"])))
            canvas.paste(f1, (P(MENU_FOOTER_TX), P(footer_y)), f1)
            f2 = _menu_line(foot[1], f("footer", P(MENU_FONT_SIZE["footnote"])))
            canvas.paste(f2, (P(MENU_FOOTER_TX), P(footer_y + MENU_FOOTER_PITCH)), f2)
            for i, ln in enumerate(note):
                ns = _menu_line(ln, f("footer", P(MENU_FONT_SIZE["footnote"])))
                canvas.paste(ns, (P(MENU_NOTE_X), P(footer_y + MENU_NOTE_DY + i * MENU_NOTE_PITCH)), ns)

            out = self._tmp / f"menu_{W}.png"
            canvas.save(out, "PNG")
            return out
        except Exception as e:
            logger.error(f"图库菜单渲染失败: {e}", exc_info=True)
            return None

    def _render_menu_src(self, width: int) -> Optional[str]:
        """菜单图 data URI（预览用）。键名必须是 src：bridge 会剥掉顶层 data 键。"""
        p = self._render_menu(width)
        if p is None:
            return None
        return "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()

    def _menu_payload(self) -> Dict[str, Any]:
        """菜单预览页要的全部数据（文字/字体/可用字体/预览图）。"""
        cfg = self._menu_config()
        return {
            "texts": {k: cfg[k] for k in MENU_TEXT_KEYS},
            "fonts": cfg["fonts"],
            "sizes": cfg["sizes"],
            "parts": [{"key": k, "label": lbl} for k, lbl in MENU_PARTS],
            "available": sorted(_font_index(str(self._fonts_dir))),
            "names": _font_display(str(self._fonts_dir)),  # 文件名 → 字体显示名（UI 显示/搜索用）
            "bg": bool(self._menu_bg_path()),
            "src": self._render_menu_src(MENU_PREVIEW_W),
        }

    def _load_menu_bg(self):
        p = self._menu_bg_path()
        if p is None:
            return None
        try:
            from PIL import Image

            return Image.open(p).convert("RGB")
        except Exception:
            return None

    @staticmethod
    def _image_ext(data: bytes) -> Optional[str]:
        """信任边界：PIL 验证字节确实是图片（且不是超大图），返回安全扩展名。"""
        try:
            from PIL import Image
        except ImportError:
            return None
        try:
            im = Image.open(io.BytesIO(data))
            fmt, size = im.format, im.size
            im.verify()
            if size[0] * size[1] > 40_000_000:
                return None
            return {
                "JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "BMP": ".bmp", "GIF": ".gif",
            }.get(fmt or "")
        except Exception:
            return None

    # ---------------- WebUI（AstrBot 插件 Pages 后端）----------------

    def _thumb(self, kw: str, image: str, px: int = 240) -> Optional[Path]:
        """生成（带磁盘缓存）最长边 px 的缩略图，返回路径；非法路径或 PIL 失败返回 None。

        主页封面要高清（前端 size=640），图库内网格用 240，各档各留一份缓存。
        """
        d = self._kw_dir(kw)
        if not d or Path(image).name != image:
            return None
        p = d / image
        if not d.is_dir() or not p.is_file():
            return None
        try:  # 信任边界：size 来自前端，收敛到 80~1600
            px = min(max(int(px or 240), 80), 1600)
        except (TypeError, ValueError):
            px = 240
        tdir = self._tmp / "thumbs"
        tdir.mkdir(exist_ok=True)
        out = tdir / f"{kw}_{p.stem}.{px}.thumb.jpg"
        if out.is_file() and out.stat().st_mtime >= p.stat().st_mtime:
            return out
        try:
            from PIL import Image
            im = Image.open(p)
            im.thumbnail((px, px))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.save(out, "JPEG", quality=80)
            return out
        except Exception:
            return None  # ponytail: 无 PIL/坏图时前端退回 raw 原图

    @staticmethod
    def _page(payload: dict):
        """解析分页参数，越界/非法值一律收敛到安全范围。"""
        try:
            off = max(int(payload.get("offset") or 0), 0)
            lim = min(max(int(payload.get("limit") or 20), 1), 200)
        except (TypeError, ValueError):
            off, lim = 0, 20
        return off, lim


    def _register_page_apis(self):
        reg = getattr(self.context, "register_web_api", None)
        if not callable(reg) or request is None:
            logger.warning(
                "当前 AstrBot 不支持插件 WebUI API，管理页不可用（关键词回复不受影响）"
            )
            return
        # 关键：Pages/bridge 转发的插件标识 = 本模块 StarMetadata 里的名字
        #（AstrBot 源码注释：metadata.name "matches the name the dashboard uses"），
        # 它不一定是 @register 的注册名（实测为 metadata.yaml 的 name，如 "图库plus"）。
        # 精确定位到"我自己"的元数据，把全部可能的标识都注册为路由前缀。
        idents = {PLUGIN_NAME}
        try:
            mine = getattr(self, "name", None)
            if isinstance(mine, str) and mine:
                idents.add(mine)
        except Exception:
            pass
        try:
            module = self.__class__.__module__
            for m in self.context.get_all_stars():
                is_me = (
                    getattr(m, "star_cls_type", None) is type(self)
                    or getattr(m, "module_path", None) == module
                    or any(
                        PLUGIN_NAME in str(getattr(m, attr, "") or "")
                        for attr in ("root_dir_name", "module_path")
                    )
                )
                if not is_me:
                    continue
                for attr in ("name", "display_name", "root_dir_name"):
                    v = getattr(m, attr, None)
                    if isinstance(v, str) and v:
                        idents.add(v)
                break
        except Exception:
            pass
        try:
            for ident in idents:
                reg(
                    f"/{ident}/galleries",
                    self.api_galleries,
                    ["GET", "POST"],
                    "图库plus：图库管理（GET 列表，POST 按 action 操作）",
                )
            # 出现这行日志 = 新代码已加载且路由注册成功
            logger.info(f"图库plus WebUI API 已注册: {sorted(idents)}/galleries")
        except Exception as e:  # 注册失败只损失 WebUI，不拖垮插件加载
            logger.error(f"图库plus Pages API 注册失败: {e}", exc_info=True)

    async def api_galleries(self):
        if request.method == "GET":
            return json_response({"galleries": self.list_galleries()})
        payload = await request.json(default={})
        action = payload.get("action")

        if action == "list_galleries":  # 分页：图库名+张数，不含图片（WebUI 懒加载）
            off, lim = self._page(payload)
            page, total = self.list_gallery_names(off, lim)
            return json_response({"galleries": page, "total": total})

        if action == "list_images":  # 分页：单图库内图片列表
            kw = str(payload.get("name", ""))
            if not self._kw_dir(kw):
                return error_response("关键词不存在或非法", status_code=400)
            off, lim = self._page(payload)
            imgs, total, cols = self.list_images(kw, off, lim)
            return json_response({"images": imgs, "total": total, "cols": cols})

        if action == "thumb":  # 缩略图（服务端生成缓存，避免整图 base64 过桥）；size 可选
            t = self._thumb(
                str(payload.get("name", "")),
                str(payload.get("image", "")),
                payload.get("size") or 240,
            )
            if t is None:
                return error_response("缩略图生成失败", status_code=404)
            b64 = base64.b64encode(t.read_bytes()).decode()
            # 键名必须是 src：bridge 会把顶层 data 键当信封剥掉（PluginPagePage.vue 的
            # response.data?.data ?? response.data），用 data 键页面只会收到裸字符串
            return json_response({"src": f"data:image/jpeg;base64,{b64}"})

        if action == "create":
            name = str(payload.get("name", ""))
            if not name.strip():
                return error_response("关键词不能为空")
            kw = self._safe_kw(name)
            (self._galleries / kw).mkdir(parents=True, exist_ok=True)
            return json_response({"ok": True, "name": kw})

        if action == "delete":
            if not self.delete_gallery(str(payload.get("name", ""))):
                return error_response("图库不存在", status_code=404)
            return json_response({"ok": True})

        if action == "upload":
            d = self._kw_dir(str(payload.get("name", "")))
            if not d or not d.is_dir():
                return error_response("关键词不存在或非法", status_code=400)
            saved = []
            async with self._save_lock:  # 并行上传时串行化入库，编号不撞车
                for f in payload.get("files", []):
                    fname = str(f.get("name", ""))
                    b64 = str(f.get("data", ""))
                    if "," in b64:  # 容忍 dataURL 前缀
                        b64 = b64.split(",", 1)[1]
                    if not fname or not b64:
                        continue
                    try:
                        data = base64.b64decode(b64)
                    except Exception:
                        continue
                    if not data:
                        continue
                    p = self.save_upload(d.name, fname, data)
                    saved.append({"file": p.name, "pid": self._pids.get(f"{d.name}/{p.name}")})
            return json_response({"ok": True, "saved": saved})

        if action == "delete_image":
            if not self.delete_image(
                str(payload.get("name", "")), str(payload.get("image", ""))
            ):
                return error_response("图片不存在或非法", status_code=404)
            return json_response({"ok": True})

        if action == "raw":
            d = self._kw_dir(str(payload.get("name", "")))
            image = str(payload.get("image", ""))
            if not d or Path(image).name != image:
                return error_response("非法路径", status_code=400)
            p = d / image
            if not d.is_dir() or not p.is_file():
                return error_response("图片不存在", status_code=404)
            b64 = base64.b64encode(p.read_bytes()).decode()
            return json_response(
                {"src": f"data:{MIME.get(p.suffix.lower(), 'application/octet-stream')};base64,{b64}"}
            )

        if action == "import":
            pattern = str(payload.get("pattern", "")).strip()
            if "{图库名}" not in pattern:
                return error_response("文件名格式必须包含 {图库名}")
            async with self._save_lock:
                return json_response(self.import_files(pattern, payload.get("files", [])))

        if action == "get_settings":  # 水印设置：当前字体 + 可用字体列表（系统 + 上传池）
            return json_response(
                {
                    "fonts": {
                        "cjk": self._settings.get("wm_font_cjk") or DEFAULT_CJK_FONT,
                        "latin": self._settings.get("wm_font_latin") or DEFAULT_LATIN_FONT,
                    },
                    "available": sorted(_font_index(str(self._fonts_dir))),
                }
            )

        if action == "save_settings":  # 保存水印字体设置（名称必须是可用的字体文件）；顺带存每行张数偏好
            idx = _font_index(str(self._fonts_dir))
            for key, default in (("cjk", DEFAULT_CJK_FONT), ("latin", DEFAULT_LATIN_FONT)):
                name = str(payload.get(key, "")).strip()
                if name and name.lower() not in idx:
                    return error_response(f"系统中未找到字体文件: {name}", status_code=400)
                self._settings[f"wm_font_{key}"] = name or default
            try:  # 二级页每行张数（3~8），垃圾值忽略
                self._settings["thumb_cols"] = min(max(int(payload.get("cols")), 3), 8)
            except (TypeError, ValueError):
                pass
            self._save_settings()
            return json_response({"ok": True})

        if action == "upload_font":  # 管理员上传字体文件进字体池，之后可在下拉里选
            fname = Path(str(payload.get("name", ""))).name  # 只取文件名，防路径穿越
            if fname.lower().rsplit(".", 1)[-1] not in ("ttf", "ttc", "otf"):
                return error_response("仅支持 .ttf/.ttc/.otf 字体文件", status_code=400)
            b64 = str(payload.get("data", ""))
            if "," in b64:
                b64 = b64.split(",", 1)[1]
            try:
                data = base64.b64decode(b64)
            except Exception:
                return error_response("内容解码失败", status_code=400)
            if not data or len(data) > 30 * 1024 * 1024:
                return error_response("字体文件为空或超过 30MB", status_code=400)
            target = self._fonts_dir / fname
            target.write_bytes(data)
            try:  # 信任边界：必须真的是 PIL 能加载的字体才收下
                from PIL import ImageFont

                ImageFont.truetype(str(target), 16)
            except Exception:
                target.unlink(missing_ok=True)
                return error_response("不是有效的字体文件", status_code=400)
            _font_index.cache_clear()  # 新字体进入索引
            _font_display.cache_clear()
            return json_response({"ok": True, "name": fname})

        if action == "set_pid":  # 改/清单张图片的 PID（WebUI 悬停编辑）
            d = self._kw_dir(str(payload.get("name", "")))
            image = str(payload.get("image", ""))
            if not d or Path(image).name != image or not (d / image).is_file():
                return error_response("图片不存在或非法", status_code=404)
            pid = str(payload.get("pid", "")).strip()[:64]
            if pid:
                self._pids[f"{d.name}/{image}"] = pid
            else:
                self._pids.pop(f"{d.name}/{image}", None)
            self._save_pids()
            return json_response({"ok": True, "pid": pid or None})

        if action == "wm_preview":  # 悬停预览：带发送时水印条的真实效果，等比缩到 size 内
            d = self._kw_dir(str(payload.get("name", "")))
            image = str(payload.get("image", ""))
            if not d or Path(image).name != image:
                return error_response("非法路径", status_code=400)
            p = d / image
            if not d.is_dir() or not p.is_file():
                return error_response("图片不存在", status_code=404)
            try:
                px = min(max(int(payload.get("size") or 800), 80), 1600)
            except (TypeError, ValueError):
                px = 800
            src = p
            try:  # 与 _send_image 同一开关逻辑：全局/图库关了就是原样
                if self.config.get("watermark", True) and self._gallery_wm(d.name):
                    src = await asyncio.to_thread(self._render_watermark, p)
            except Exception:
                src = p
            try:
                from PIL import Image

                with Image.open(src) as im:
                    im.thumbnail((px, px))
                    if im.mode not in ("RGB", "L"):
                        im = im.convert("RGB")
                    buf = io.BytesIO()
                    im.save(buf, "JPEG", quality=85)
                b64 = base64.b64encode(buf.getvalue()).decode()
            except Exception:  # 无 PIL：原样回（base64 直传）
                b64 = base64.b64encode(src.read_bytes()).decode()
            return json_response({"src": f"data:image/jpeg;base64,{b64}"})

        if action == "image_info":  # 悬停预览卡：图库/文件名/大小/UID/发送次数/绝对路径/主色淡化底色
            d = self._kw_dir(str(payload.get("name", "")))
            image = str(payload.get("image", ""))
            if not d or Path(image).name != image:
                return error_response("非法路径", status_code=400)
            p = d / image
            if not d.is_dir() or not p.is_file():
                return error_response("图片不存在", status_code=404)
            row = self._stats.get("images", {}).get(f"{d.name}/{image}", {})
            pid = self._pids.get(f"{d.name}/{image}") or ""
            color = None
            try:  # 主色提取（同水印条）后向白色淡化 90%，做悬停卡底色
                from PIL import Image

                with Image.open(p) as im:
                    c = self._theme_color(im)
                color = "#%02x%02x%02x" % tuple(round(v + (255 - v) * 0.9) for v in c)
            except Exception:
                pass  # 无 PIL/坏图：前端用默认底色
            return json_response(
                {
                    "gallery": d.name,
                    "file": image,
                    "size": p.stat().st_size,
                    "uid": pid.split("_", 1)[0],
                    "pid": pid or None,
                    "sends": row.get("sends", 0),
                    "path": str(p),
                    "color": color,
                }
            )

        if action == "set_wm":  # 单图库水印开关（缺省开）
            d = self._kw_dir(str(payload.get("name", "")))
            if not d or not d.is_dir():
                return error_response("图库不存在或非法", status_code=404)
            self.set_gallery_wm(d.name, bool(payload.get("enabled", True)))
            return json_response({"ok": True, "wm": self._gallery_wm(d.name)})

        if action == "get_menu":  # 菜单预览页：文字/字体/可用字体/预览图 一次给全
            return json_response(self._menu_payload())

        if action == "save_menu":  # 保存菜单文字/字体/字号，并把最新预览图一起返回（所见即所得）
            texts = payload.get("texts")
            if isinstance(texts, dict):
                for k in MENU_TEXT_KEYS:
                    if k in texts:
                        self._menu_settings()[k] = str(texts[k])[:MENU_TEXT_MAX]
            fonts = payload.get("fonts")
            if isinstance(fonts, dict):
                idx = _font_index(str(self._fonts_dir))
                table = self._menu_settings().setdefault("fonts", {})
                if not isinstance(table, dict):  # 手改坏的 settings 别炸
                    table = {}
                    self._menu_settings()["fonts"] = table
                for part, _lbl in MENU_PARTS:
                    slots = fonts.get(part)
                    if not isinstance(slots, dict):
                        continue
                    cur = table.get(part)
                    if not isinstance(cur, dict):  # 旧格式存单个字体名，第一次保存升级成三槽
                        cur = {}
                        table[part] = cur
                    for script, _lbl in MENU_SCRIPTS:
                        name = str(slots.get(script, "")).strip()
                        if not name:
                            continue
                        if name.lower() not in idx:
                            return error_response(f"系统中未找到字体文件: {name}", status_code=400)
                        cur[script] = name
            sizes = payload.get("sizes")
            if isinstance(sizes, dict):  # 字号百分比：垃圾值在 _menu_size_pct 里收敛
                store = self._menu_settings().setdefault("sizes", {})
                for part, _lbl in MENU_PARTS:
                    if part in sizes:
                        store[part] = _menu_size_pct(sizes[part])
            self._save_settings()
            return json_response(self._menu_payload())

        if action == "upload_menu_bg":  # 管理员自定义菜单页眉背景图
            b64 = str(payload.get("data", ""))
            if "," in b64:
                b64 = b64.split(",", 1)[1]
            try:
                data = base64.b64decode(b64)
            except Exception:
                return error_response("内容解码失败", status_code=400)
            if not data or len(data) > MENU_BG_MAX_MB * 1024 * 1024:
                return error_response(f"背景图不能为空，且不超过 {MENU_BG_MAX_MB}MB", status_code=400)
            ext = self._image_ext(data)
            if ext is None:
                return error_response("不是有效的图片文件", status_code=400)
            self.set_menu_bg(data, ext)
            return json_response({"ok": True, **self._menu_payload()})

        if action == "get_gallery_menu":  # 单图库菜单配置（图库二级页的弹窗）
            d = self._kw_dir(str(payload.get("name", "")))
            if not d or not d.is_dir():
                return error_response("图库不存在或非法", status_code=404)
            return json_response({"menu": self._gallery_menu(d.name)})

        if action == "set_gallery_menu":
            d = self._kw_dir(str(payload.get("name", "")))
            if not d or not d.is_dir():
                return error_response("图库不存在或非法", status_code=404)
            return json_response(
                {
                    "ok": True,
                    "menu": self.set_gallery_menu(
                        d.name,
                        bool(payload.get("show", True)),
                        str(payload.get("char", "")),
                        str(payload.get("group", "")),
                    ),
                }
            )

        if action == "batch":  # 选择模式批量操作：删除 / 移动 / 复制（dst 仅 move/copy 需要）
            src = self._kw_dir(str(payload.get("src", "")))
            op = str(payload.get("op", ""))
            if op not in ("delete", "move", "copy"):
                return error_response("未知操作", status_code=400)
            if not src or not src.is_dir():
                return error_response("图库不存在或非法", status_code=404)
            dst = self._kw_dir(str(payload.get("dst", "")))
            if op in ("move", "copy") and (not dst or not dst.is_dir()):
                return error_response("目标图库不存在或非法", status_code=404)
            done, failed = 0, []
            for image in payload.get("images", []):
                image = str(image)
                p = src / image if Path(image).name == image else None  # 信任边界：只认裸文件名
                if p is None or not p.is_file():
                    failed.append(image)
                    continue
                try:
                    if op == "delete":
                        self.delete_image(src.name, image)
                    elif op in ("move", "copy"):
                        if (dst / image).exists():  # 同名不覆盖：跳过并上报
                            failed.append(image)
                            continue
                        (shutil.move if op == "move" else shutil.copy2)(p, dst / image)
                        pid = self._pids.get(f"{src.name}/{image}")
                        if pid:
                            self._pids[f"{dst.name}/{image}"] = pid
                            if op == "move":
                                self._pids.pop(f"{src.name}/{image}", None)
                        if op == "move":  # 发送统计跟着文件走（复制是新文件、重新计数）
                            row = self._stats.get("images", {}).pop(f"{src.name}/{image}", None)
                            if row:
                                self._stats.setdefault("images", {})[f"{dst.name}/{image}"] = row
                    done += 1
                except OSError:
                    failed.append(image)
            if op in ("move", "copy"):
                self._save_pids()
            if op == "move":
                try:
                    self._save_stats()
                except OSError:
                    pass
            return json_response({"ok": True, "done": done, "failed": failed})

        if action == "list_all_settings":  # 批量设置页：全部图库的 水印+菜单配置 一次给全
            out = []
            for d in sorted(self._galleries.iterdir(), key=lambda p: pinyin_key(p.name)):
                if not d.is_dir():
                    continue
                # ponytail: 不分页（只读目录名+settings，不碰图片内容）；图库上几百个再翻页
                n = sum(1 for p in d.iterdir() if p.is_file() and p.suffix.lower() in EXTS)
                out.append(
                    {
                        "name": d.name,
                        "count": n,
                        "wm": self._gallery_wm(d.name),
                        "menu": self._gallery_menu(d.name),
                    }
                )
            return json_response({"galleries": out})

        return error_response("未知操作", status_code=400)


def _menu_line(text: str, fonts: Dict[str, Any], color=(0, 0, 0)):
    """按 中/英/日 字体槽把一行字画成墨迹紧贴的透明条带（位置/居中/测宽全用这张图）。

    runs：假名→'jp'，其余 CJK/全角→'cjk'，其余→'latin'（见 MENU_RUN_RE）。
    """
    from PIL import Image, ImageDraw

    runs = [(m.group(), fonts.get(m.lastgroup)) for m in MENU_RUN_RE.finditer(text)]
    runs = runs or [(text, fonts.get("latin"))]
    used = []
    for _t, fnt in runs:
        if fnt is not None and fnt not in used:
            used.append(fnt)
    if not used:
        used = [fonts.get("latin")]
    asc = max(f.getmetrics()[0] for f in used)
    desc = max(f.getmetrics()[1] for f in used)
    img = Image.new("RGBA", (int(sum(f.getlength(t) for t, f in runs)) + 8, asc + desc + 8), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x = 4.0
    for t, fnt in runs:
        d.text((x, asc), t, font=fnt, fill=tuple(color) + (255,), anchor="ls")  # 共用基线，混排才齐
        x += fnt.getlength(t)
    bb = img.getbbox()
    return img.crop(bb) if bb else img


def _cover_crop(im, w: int, h: int):
    """等比放大到铺满 w×h 后居中裁剪（页眉背景图用）。"""
    from PIL import Image

    k = max(w / im.width, h / im.height)
    big = im.resize(
        (max(int(round(im.width * k)), w), max(int(round(im.height * k)), h)), Image.LANCZOS
    )
    x, y = (big.width - w) // 2, (big.height - h) // 2
    return big.crop((x, y, x + w, y + h))


def _menu_default_bg(w: int, h: int):
    """没上传背景图时的默认页眉底：上暗下红的竖向渐变（保证白字可读）。"""
    from PIL import Image

    top, bottom = (32, 33, 38), (86, 20, 24)
    grad = Image.new("RGB", (1, h))
    for y in range(h):
        k = y / max(h - 1, 1)
        grad.putpixel((0, y), tuple(round(top[i] + (bottom[i] - top[i]) * k) for i in range(3)))
    return grad.resize((w, h))


@lru_cache(maxsize=None)
def _font_index(extra_dir: str = "") -> Dict[str, str]:
    """字体索引：小写文件名 → 完整路径。

    系统字体目录 + extra_dir（管理员上传池，同名覆盖系统字体）。
    """
    out: Dict[str, str] = {}
    dirs = [Path(p) for p in FONT_DIRS]
    if extra_dir:
        dirs.append(Path(extra_dir))
    for d in dirs:
        if not d.is_dir():
            continue
        try:
            for p in d.rglob("*"):
                if p.suffix.lower() in (".ttf", ".ttc", ".otf"):
                    out[p.name.lower()] = str(p)  # 后扫描的目录覆盖前者（上传池优先）
        except OSError:
            continue
    return out


@lru_cache(maxsize=None)
def _font_display(extra_dir: str = "") -> Dict[str, str]:
    """字体文件名（小写）→ 显示名（PIL 从字体文件里读家族名+样式；读不出用去后缀的文件名）。"""
    from PIL import ImageFont

    out: Dict[str, str] = {}
    for name, path in _font_index(extra_dir).items():
        disp = name.rsplit(".", 1)[0]
        try:
            family, style = ImageFont.truetype(path, 16).getname()
            disp = f"{family} {style}".strip()
        except Exception:
            pass
        out[name] = disp
    return out


@lru_cache(maxsize=None)
def _font_cached(path: str, size: int):
    from PIL import ImageFont

    return ImageFont.truetype(path, size)


def _resolve_font(name: str, size: int, extra_dir: str = ""):
    """按设置名解析字体；缺文件时沿回退链找到第一个可用的，最终退回 PIL 默认。"""
    from PIL import ImageFont

    idx = _font_index(extra_dir)
    chain = [name.lower(), *FONT_CANDIDATES]
    for n in chain:
        p = idx.get(n)
        if p:
            try:
                return _font_cached(p, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()
