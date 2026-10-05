# -*- coding: utf-8 -*-
"""图库plus：关键词图库插件。

- 一个关键词一个文件夹，消息首词命中关键词即随机回一张图
- 上传时识别 pixiv 命名（纯数字 / 数字_p数字）并记录 pid 到 pids.json
- 文件统一重命名为 关键词-编号.扩展名
- 发送时可选择在图片下方附加 文件名/PID 信息条（水印），仅发送环节，不改原图
- WebUI 为 AstrBot 插件 Pages（仪表盘内管理页），后端 API 经 context.register_web_api 注册
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
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
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
PID_RE = re.compile(r"^\d+(_p\d+)?$")  # 纯数字 或 数字_p数字（pixiv 作品命名）
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
FONT_CANDIDATES = (
    "msyh.ttc", "simhei.ttf", "simsun.ttc",
    "notosanscjk-regular.ttc", "notosanssc-regular.otf", "wqy-microhei.ttc", "pingfang.ttc",
)


@register(
    PLUGIN_NAME,
    "cuman",
    "图库plus：关键词图库，一关键词一文件夹，WebUI 管理上传，随机回复并附加文件名/PID 水印。",
    "0.1.1",
    "https://github.com/cumany/astrbot_plugin_image_replay",
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
        self._galleries = root / "galleries"  # 每个关键词一个文件夹
        self._pid_file = root / "pids.json"   # {"关键词/文件名": "pid"}
        self._tmp = root / "temp"             # 发送用水印临时文件
        self._galleries.mkdir(parents=True, exist_ok=True)
        self._tmp.mkdir(parents=True, exist_ok=True)
        self._pids: Dict[str, str] = self._load_pids()
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
            if self.config.get("watermark", True):
                path = await asyncio.to_thread(self._render_watermark, path)
            yield event.chain_result([Image.fromFileSystem(str(path))])
        except Exception as e:
            logger.error(f"图库plus 响应失败: {e}", exc_info=True)

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
        stem = Path(filename).stem
        if pid is None:
            pid = stem if PID_RE.fullmatch(stem) else None
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
        (self._tmp / "thumbs" / f"{d.name}_{p.stem}.thumb.jpg").unlink(missing_ok=True)
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
        return True

    def list_galleries(self) -> List[dict]:
        out: List[dict] = []
        for d in sorted(self._galleries.iterdir(), key=lambda p: p.name):
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
                    "images": [
                        {"file": n, "pid": self._pids.get(f"{d.name}/{n}")} for n in names
                    ],
                }
            )
        return out

    def list_gallery_names(self, offset: int = 0, limit: Optional[int] = None):
        """分页列出图库（只取名与张数，不列图片），供 WebUI 懒加载。返回 (页数据, 总数)。"""
        names = sorted(d.name for d in self._galleries.iterdir() if d.is_dir())
        page = names if limit is None else names[offset : offset + limit]
        out = []
        for name in page:
            d = self._galleries / name
            out.append(
                {
                    "name": name,
                    "count": sum(
                        1 for p in d.iterdir()
                        if p.is_file() and p.suffix.lower() in EXTS
                    ),
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
        )

    # ---------------- 水印 ----------------

    def _render_watermark(self, path: Path) -> Path:
        """图片下方拼一条深色信息条：文件名 + PID。动图不动（避免只发第一帧）。"""
        if path.suffix.lower() == ".gif":
            return path
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            return path
        try:
            pid = self._pids.get(f"{path.parent.name}/{path.name}")
            text = path.name + (f"   PID:{pid}" if pid else "")
            font = _cjk_font(24)
            im = Image.open(path).convert("RGB")
            w, h = im.size
            probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
            left, top, right, bottom = probe.textbbox((0, 0), text, font=font)
            strip = (bottom - top) + 16
            canvas = Image.new("RGB", (w, h + strip), (18, 18, 18))
            canvas.paste(im, (0, 0))
            ImageDraw.Draw(canvas).text(
                ((w - (right - left)) // 2, h + 8 - top),
                text, fill=(235, 235, 235), font=font,
            )
            # 输出格式可配置：jpg 体积小（默认），png 无损
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

    # ---------------- WebUI（AstrBot 插件 Pages 后端）----------------

    def _thumb(self, kw: str, image: str) -> Optional[Path]:
        """生成（带磁盘缓存）240px 小缩略图，返回路径；非法路径或 PIL 失败返回 None。"""
        d = self._kw_dir(kw)
        if not d or Path(image).name != image:
            return None
        p = d / image
        if not d.is_dir() or not p.is_file():
            return None
        tdir = self._tmp / "thumbs"
        tdir.mkdir(exist_ok=True)
        out = tdir / f"{kw}_{p.stem}.thumb.jpg"
        if out.is_file() and out.stat().st_mtime >= p.stat().st_mtime:
            return out
        try:
            from PIL import Image
            im = Image.open(p)
            im.thumbnail((240, 240))
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
            imgs, total = self.list_images(kw, off, lim)
            return json_response({"images": imgs, "total": total})

        if action == "thumb":  # 小缩略图（服务端生成缓存，避免整图 base64 过桥）
            t = self._thumb(str(payload.get("name", "")), str(payload.get("image", "")))
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
            return json_response(self.import_files(pattern, payload.get("files", [])))

        return error_response("未知操作", status_code=400)


@lru_cache(maxsize=None)
def _cjk_font(size: int):
    """找一个能画中文的字体；找不到就退回 PIL 默认字体。"""
    from PIL import ImageFont

    dirs = [
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path("/System/Library/Fonts"),
        Path("/System/Library/Fonts/Supplemental"),
    ]
    fonts: List[Path] = []
    for d in dirs:
        if d.is_dir():
            fonts += [p for p in d.rglob("*") if p.suffix.lower() in (".ttf", ".ttc", ".otf")]
    for want in FONT_CANDIDATES:
        for p in fonts:
            if p.name.lower() == want:
                return ImageFont.truetype(str(p), size)
    if fonts:
        return ImageFont.truetype(str(fonts[0]), size)
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()
