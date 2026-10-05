# -*- coding: utf-8 -*-
"""最小自检：python selftest.py（无需安装 AstrBot，astrbot 以桩模块代替）。

覆盖：pid 识别、入库重命名、pid 记录、关键词清洗与防穿越、删除逻辑、水印渲染。
"""
import io
import json
import shutil
import sys
import tempfile
import types
from pathlib import Path


def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _Cfg(dict):
    pass


_tmp = Path(tempfile.mkdtemp(prefix="gallery_plus_selftest_"))

# ---- astrbot 桩 ----
api = _mod(
    "astrbot.api",
    AstrBotConfig=_Cfg,
    logger=types.SimpleNamespace(
        error=lambda *a, **k: print("[logger.error]", *a),
        warning=lambda *a, **k: print("[logger.warning]", *a),
        info=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    ),
)
_mod("astrbot")

event_mod = _mod("astrbot.api.event", AstrMessageEvent=object, filter=types.SimpleNamespace())
filter_ns = event_mod.filter
filter_ns.EventMessageType = types.SimpleNamespace(ALL=0)


def _passthrough(*a, **k):
    def deco(f):
        return f
    return deco


for n in ("event_message_type", "command", "permission_type"):
    setattr(filter_ns, n, _passthrough)

_mod("astrbot.api.message_components",
     Image=type("Image", (), {"fromFileSystem": staticmethod(lambda p: p)}))
_mod("astrbot.api.star",
     Context=object,
     Star=type("Star", (), {"__init__": lambda self, ctx: setattr(self, "context", ctx)}),
     StarTools=types.SimpleNamespace(get_data_dir=lambda name: _tmp),
     register=lambda *a, **k: (lambda cls: cls))
_mod("astrbot.api.web",
     json_response=lambda data=None, **k: data,
     error_response=lambda message="", **k: {"status": "error", "message": message, "data": k.get("data") or {}},
     request=object())  # 处理器测试时会被假 request 覆盖

sys.path.insert(0, str(Path(__file__).parent))
import main  # noqa: E402

# 1) pid 识别：纯数字 或 数字_p数字
assert main.PID_RE.fullmatch("95026140") and main.PID_RE.fullmatch("95026140_p3")
assert not main.PID_RE.fullmatch("abc123") and not main.PID_RE.fullmatch("123p4")
assert not main.PID_RE.fullmatch("") and not main.PID_RE.fullmatch("_p3")

# 0) 插件 Pages 后端 API 注册：单端点、GET+POST。
#    插件标识以运行时元数据为准（实测部分版本 star.name = metadata.yaml 的 name），
#    因此按 root_dir_name/module_path 找到自己，把 name/display_name/root_dir_name 全部注册。
def _make_ctx(extra=None):
    attrs = {"register_web_api": lambda self, *a: regs.append(a)}
    if extra:
        attrs.update(extra)
    return type("Ctx", (), attrs)()

regs = []
g = main.GalleryPlus(_make_ctx(), _Cfg())
assert len(regs) == 1, regs
route, handler, methods, _desc = regs[0]
assert route == f"/{main.PLUGIN_NAME}/galleries", route
assert set(methods) == {"GET", "POST"}, methods
assert handler.__name__ == "api_galleries"

# 模拟用户构建：star.name = "图库plus"（metadata.yaml name），root_dir_name = 目录名
regs.clear()
meta = types.SimpleNamespace(name="图库plus", display_name="图库plus",
                             root_dir_name=main.PLUGIN_NAME,
                             module_path=f"astrbot.plugins.{main.PLUGIN_NAME}.main")
g2 = main.GalleryPlus(_make_ctx({"get_all_stars": lambda self: [meta]}), _Cfg())
assert {r[0] for r in regs} == {
    "/图库plus/galleries",
    f"/{main.PLUGIN_NAME}/galleries",
}, regs
assert all(set(r[2]) == {"GET", "POST"} for r in regs)

# 极端场景：插件目录名与注册名都不同，只能靠 star_cls_type 认领自己的元数据
regs.clear()
meta2 = types.SimpleNamespace(name="图库plus", display_name=None,
                              root_dir_name="图库plus",
                              module_path="data.plugins.图库plus.main",
                              star_cls_type=main.GalleryPlus)
g3 = main.GalleryPlus(_make_ctx({"get_all_stars": lambda self: [meta2]}), _Cfg())
assert {r[0] for r in regs} == {
    "/图库plus/galleries",
    f"/{main.PLUGIN_NAME}/galleries",
}, regs

# 2) 入库：pid 命名 → 记录 pid；重命名为 关键词-编号
p1 = g.save_upload("可琳照片", "95026140_p0.jpg", b"a")
assert p1.name == "可琳照片-1.jpg", p1.name
assert g._pids["可琳照片/可琳照片-1.jpg"] == "95026140_p0"
p2 = g.save_upload("可琳照片", "photo.png", b"b")  # 非 pid 命名
assert p2.name == "可琳照片-2.png", p2.name
assert "可琳照片/可琳照片-2.png" not in g._pids
p3 = g.save_upload("可琳照片", "12345678.webp", b"c")
assert p3.name == "可琳照片-3.webp" and g._pids["可琳照片/可琳照片-3.webp"] == "12345678"
on_disk = json.loads((_tmp / "pids.json").read_text(encoding="utf-8"))
assert on_disk["可琳照片/可琳照片-1.jpg"] == "95026140_p0"

# 3) 图库列表
lst = g.list_galleries()
assert len(lst) == 1 and lst[0]["name"] == "可琳照片" and lst[0]["count"] == 3
pid_map = {im["file"]: im["pid"] for im in lst[0]["images"]}
assert pid_map == {
    "可琳照片-1.jpg": "95026140_p0",
    "可琳照片-2.png": None,
    "可琳照片-3.webp": "12345678",
}

# 4) 关键词清洗 / 防穿越
assert main.GalleryPlus._safe_kw('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"
assert g._kw_dir("..") is None and g._kw_dir("a/../b") is None
assert g._kw_dir("可琳照片") is not None
assert not g.delete_image("..", "x") and not g.delete_gallery("../etc")

# 5) 删除：单图连带 pid；整库连带全部 pid
assert g.delete_image("可琳照片", p3.name)
assert "可琳照片/可琳照片-3.webp" not in g._pids
assert not g.delete_image("不存在的库", "x.png")
assert g.delete_gallery("可琳照片")
assert not (g._galleries / "可琳照片").exists() and g._pids == {}
assert not g.delete_gallery("可琳照片")

# 6) Pages action 协议（假 request 覆盖 main.request，走 api_galleries 全流程）
import asyncio, base64  # noqa: E402

class _FakeReq:
    def __init__(self, method, payload=None):
        self._m, self._p = method, payload
    @property
    def method(self):
        return self._m
    async def json(self, default=None):
        return self._p if self._p is not None else default

def _act(method, payload=None):
    main.request = _FakeReq(method, payload)
    return asyncio.run(g.api_galleries())

# create
res = _act("POST", {"action": "create", "name": "测试库"})
assert res["ok"] and (g._galleries / "测试库").is_dir(), res
# GET 列表
res = _act("GET")
assert {x["name"] for x in res["galleries"]} == {"测试库"}, res
# upload（pid 命名）
res = _act("POST", {"action": "upload", "name": "测试库",
                    "files": [{"name": "95026140_p7.jpg", "data": base64.b64encode(b"img").decode()}]})
assert res["saved"][0] == {"file": "测试库-1.jpg", "pid": "95026140_p7"}, res
assert (g._galleries / "测试库" / "测试库-1.jpg").read_bytes() == b"img"
# raw 预览（注意：键名必须是 src，bridge 会剥掉顶层 data 键）
res = _act("POST", {"action": "raw", "name": "测试库", "image": "测试库-1.jpg"})
assert res["src"].startswith("data:image/jpeg;base64,"), res
# raw 防穿越
res = _act("POST", {"action": "raw", "name": "测试库", "image": "../x.png"})
assert res["status"] == "error", res
# 分页 list_galleries / list_images
res = _act("POST", {"action": "list_galleries", "offset": 0, "limit": 20})
assert res["total"] == 1 and res["galleries"][0]["name"] == "测试库", res
assert res["galleries"][0]["count"] == 1 and "images" not in res["galleries"][0], res
res = _act("POST", {"action": "list_galleries", "offset": 1, "limit": 20})
assert res["galleries"] == [] and res["total"] == 1, res
res = _act("POST", {"action": "list_images", "name": "测试库", "offset": 0, "limit": 50})
assert res["total"] == 1 and res["images"][0]["file"] == "测试库-1.jpg", res
res = _act("POST", {"action": "list_images", "name": "a/../b", "offset": 0, "limit": 50})
assert res["status"] == "error", res
# thumb（有 PIL 时：真图 → 小缩略图 + 磁盘缓存；坏图 → 404）
try:
    from PIL import Image as _P  # noqa: F401
    buf = io.BytesIO()
    _P.new("RGB", (400, 300), "red").save(buf, "PNG")
    _act("POST", {"action": "upload", "name": "测试库",
                  "files": [{"name": "real.png", "data": base64.b64encode(buf.getvalue()).decode()},
                            {"name": "junk.jpg", "data": base64.b64encode(b"junk").decode()}]})
    res = _act("POST", {"action": "list_images", "name": "测试库", "offset": 0, "limit": 50})
    files = {im["file"] for im in res["images"]}
    real = next(f for f in files if f.endswith(".png"))      # real.png 入库后重命名为 测试库-N.png
    junk = next(f for f in files if f.endswith(".jpg") and f != "测试库-1.jpg")
    res = _act("POST", {"action": "thumb", "name": "测试库", "image": real})
    assert res["src"].startswith("data:image/jpeg;base64,"), res
    tw, th = _P.open(io.BytesIO(base64.b64decode(res["src"].split(",", 1)[1]))).size
    assert max(tw, th) <= 240, (tw, th)
    assert (g._tmp / "thumbs" / f"测试库_{Path(real).stem}.thumb.jpg").is_file()
    res = _act("POST", {"action": "thumb", "name": "测试库", "image": junk})
    assert res["status"] == "error", res
    res = _act("POST", {"action": "thumb", "name": "测试库", "image": "../x.jpg"})
    assert res["status"] == "error", res
except ImportError:
    print("(无 PIL，跳过缩略图检查)")
# delete_image（连带 pid）
res = _act("POST", {"action": "delete_image", "name": "测试库", "image": "测试库-1.jpg"})
assert res["ok"] and "测试库/测试库-1.jpg" not in g._pids, res
# delete 整库
res = _act("POST", {"action": "delete", "name": "测试库"})
assert res["ok"] and not (g._galleries / "测试库").exists(), res
# 未知 action
res = _act("POST", {"action": "????"})
assert res["status"] == "error", res

# 7) 批量导入：搭积木式文件名格式 → 解析、分库、pid、跳过原因
rx, has_ext = main.build_filename_regex("{图库名}-{编号}.{扩展名}")
assert has_ext
m = rx.fullmatch("可琳照片-1.jpg")
assert m and m.group("kw") == "可琳照片" and m.group("num") == "1"
assert not rx.fullmatch("可琳照片.jpg")   # 缺编号
assert not rx.fullmatch("可琳照片-1")     # 缺扩展名

rx2, has_ext2 = main.build_filename_regex("{图库名}_{pid}")
assert not has_ext2                       # 无扩展名块 → 用 stem 匹配
m2 = rx2.fullmatch("可琳照片_95026140_p0")
assert m2 and m2.group("pid") == "95026140_p0"

_b64 = lambda s: base64.b64encode(s.encode()).decode()  # noqa: E731
res = g.import_files("{图库名}-{编号}.{扩展名}", [
    {"name": "可琳照片-1.jpg", "data": _b64("a")},
    {"name": "可琳照片-2.jpg", "data": _b64("b")},
    {"name": "安比照片-1.png", "data": _b64("c")},
    {"name": "不符合命名.jpg", "data": _b64("d")},
    {"name": "可琳照片-3.txt", "data": _b64("e")},
    {"name": "坏文件.jpg", "data": "!!!not-base64!!!"},
])
assert [i["gallery"] for i in res["imported"]] == ["可琳照片", "可琳照片", "安比照片"], res
assert [i["file"] for i in res["imported"]] == [
    "可琳照片-1.jpg", "可琳照片-2.jpg", "安比照片-1.png",
], res
assert len(res["skipped"]) == 3, res["skipped"]
assert (g._galleries / "可琳照片" / "可琳照片-1.jpg").read_bytes() == b"a"

# pid 块会被记录；编号接着该图库已有编号递增
res = g.import_files("{图库名}_{pid}.{扩展名}", [
    {"name": "安比照片_95026140_p0.jpg", "data": _b64("x")},
])
assert res["imported"][0]["pid"] == "95026140_p0", res
assert res["imported"][0]["file"] == "安比照片-2.jpg", res
assert g._pids["安比照片/安比照片-2.jpg"] == "95026140_p0"

# 图库名清洗：文件名里的 `..`/`/` 不会造成路径穿越
res = g.import_files("{图库名}-{编号}.{扩展名}", [
    {"name": "../x-1.jpg", "data": _b64("y")},
])
kw = res["imported"][0]["gallery"]
assert "/" not in kw and "\\" not in kw and ".." not in kw, kw
assert (g._galleries / kw).is_dir() and (g._galleries / kw / res["imported"][0]["file"]).is_file()

# 8) 水印渲染（本机有 PIL 时）：默认/指定输出格式 jpg，可切 png
try:
    from PIL import Image as PImage
    buf = io.BytesIO()
    PImage.new("RGB", (80, 60), "red").save(buf, "PNG")
    src = _tmp / "t.png"
    src.write_bytes(buf.getvalue())
    out = g._render_watermark(src)
    assert out.exists() and out != src
    assert out.suffix == ".jpg", out  # 默认输出 jpg（体积更小）
    w, h = PImage.open(out).size
    assert h > 60, f"信息条未加高: {h}"
    g.config["output_format"] = "png"
    out2 = g._render_watermark(src)
    assert out2.suffix == ".png" and out2.exists(), out2
    g.config["output_format"] = "jpg"
    print(f"水印渲染 OK: {w}x{h}（jpg/png 输出均通过）")
except ImportError:
    print("(无 PIL，跳过水印渲染检查)")

shutil.rmtree(_tmp)
print("selftest OK")
