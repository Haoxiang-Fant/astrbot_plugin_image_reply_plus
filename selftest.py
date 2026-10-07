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
filter_ns.PermissionType = types.SimpleNamespace(ADMIN=0)


def _passthrough(*a, **k):
    def deco(f):
        return f
    return deco


for n in ("event_message_type", "command", "permission_type"):
    setattr(filter_ns, n, _passthrough)

_mod("astrbot.api.message_components",
     Image=type("Image", (), {"fromFileSystem": staticmethod(lambda p: p)}),
     Reply=type("Reply", (), {}))
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

# 1b) 真实下载文件名里的 pid：pximg 的 _master1200、PixEz 模板的作者/标题前后缀、illust_/pixiv_ 前缀
assert main.GalleryPlus.extract_pid("95026140.jpg") == "95026140"
assert main.GalleryPlus.extract_pid("95026140_p0.jpg") == "95026140_p0"
assert main.GalleryPlus.extract_pid("95026140_p0_master1200.jpg") == "95026140_p0"
assert main.GalleryPlus.extract_pid("作者名_95026140_p0.jpg") == "95026140_p0"
assert main.GalleryPlus.extract_pid("标题_95026140_p0_标题.jpg") == "95026140_p0"
assert main.GalleryPlus.extract_pid("illust_95026140_20230101.jpg") == "95026140"
assert main.GalleryPlus.extract_pid("pixiv_95026140.png") == "95026140"
assert main.GalleryPlus.extract_pid("IMG_20231006_123456.jpg") is None
assert main.GalleryPlus.extract_pid("photo.png") is None

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

# 0b) metadata.yaml 必须是 AstrBot 认的格式：name=英文标识符（=目录名=@register 名），展示名走 display_name
# 官方字段表见 https://docs.astrbot.app/dev/star/plugin-publish.html
try:
    import yaml
    _md = yaml.safe_load((Path(__file__).parent / "metadata.yaml").read_text("utf-8"))
    assert _md["name"] == main.PLUGIN_NAME, _md
    assert _md["display_name"] == "图库plus", _md
    assert _md["author"] == main.PLUGIN_AUTHOR, _md
    assert _md["repo"] == main.PLUGIN_REPO and main.PLUGIN_NAME in _md["repo"], _md  # 指向本插件仓库
    assert _md["desc"], _md
    assert _md["version"] == main.PLUGIN_VERSION, _md  # 与 @register 的版本不许漂移
    assert "id" not in _md, _md                        # id 不是 AstrBot 的字段（标识走 name）
except ImportError:
    print("(无 PyYAML，跳过 metadata.yaml 检查)")

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

# 2b) 真实下载文件名入库：pid 绑定进 pids.json，编号照样接着该图库已有编号排
p4 = g.save_upload("命名库", "作者名_95026140_p0_master1200.jpg", b"d")
assert p4.name == "命名库-1.jpg" and g._pids["命名库/命名库-1.jpg"] == "95026140_p0"
p5 = g.save_upload("命名库", "IMG_20231006_123456.jpg", b"e")
assert p5.name == "命名库-2.jpg" and "命名库/命名库-2.jpg" not in g._pids
assert json.loads((_tmp / "pids.json").read_text(encoding="utf-8"))["命名库/命名库-1.jpg"] == "95026140_p0"
assert g.delete_gallery("命名库") and "命名库/命名库-1.jpg" not in g._pids

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
# thumb（有 PIL 时：真图 → 缩略图 + 磁盘缓存；size 可选（封面要高清）；坏图 → 404）
try:
    from PIL import Image as _P  # noqa: F401
    buf = io.BytesIO()
    _P.new("RGB", (1000, 800), "red").save(buf, "PNG")  # 大图：才能验证 size 档位
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
    assert (g._tmp / "thumbs" / f"测试库_{Path(real).stem}.240.thumb.jpg").is_file()
    # size=640（主页封面档）：尺寸被采纳，且与默认档各留一份缓存
    res = _act("POST", {"action": "thumb", "name": "测试库", "image": real, "size": 640})
    cw, ch = _P.open(io.BytesIO(base64.b64decode(res["src"].split(",", 1)[1]))).size
    assert max(cw, ch) == 640, (cw, ch)
    assert (g._tmp / "thumbs" / f"测试库_{Path(real).stem}.640.thumb.jpg").is_file()
    # 越界/垃圾 size 收敛到安全范围
    for bad, want in ((99999, 1000), (-5, 80), ("垃圾", 240)):
        res = _act("POST", {"action": "thumb", "name": "测试库", "image": real, "size": bad})
        bw, bh = _P.open(io.BytesIO(base64.b64decode(res["src"].split(",", 1)[1]))).size
        assert max(bw, bh) == want, (bad, bw, bh)
    # 删图后各档缓存一起清（delete_image 的缓存清理已改为按档位通配）
    assert g.delete_image("测试库", real)
    assert not list((g._tmp / "thumbs").glob(f"测试库_{Path(real).stem}.*.thumb.jpg"))
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

# 6b) 单图 PID 编辑 + 单图库水印开关 + 封面（0.1.3）
_act("POST", {"action": "create", "name": "WM库"})
res = _act("POST", {"action": "upload", "name": "WM库",
                    "files": [{"name": "a.jpg", "data": base64.b64encode(b"z").decode()}]})
fname = res["saved"][0]["file"]
assert g._pids == {}, g._pids  # 前序用例清干净了，下面 set_pid 才可断言
# set_pid：写入 → 落盘 → 清除
res = _act("POST", {"action": "set_pid", "name": "WM库", "image": fname, "pid": "95026140_p0"})
assert res["ok"] and res["pid"] == "95026140_p0", res
assert g._pids[f"WM库/{fname}"] == "95026140_p0"
assert json.loads((_tmp / "pids.json").read_text(encoding="utf-8"))[f"WM库/{fname}"] == "95026140_p0"
res = _act("POST", {"action": "set_pid", "name": "WM库", "image": fname, "pid": ""})
assert res["ok"] and res["pid"] is None and g._pids == {}, res
# set_pid：防穿越 / 图片不存在
res = _act("POST", {"action": "set_pid", "name": "WM库", "image": "../x.jpg", "pid": "1"})
assert res["status"] == "error", res
res = _act("POST", {"action": "set_pid", "name": "WM库", "image": "没有这张.jpg", "pid": "1"})
assert res["status"] == "error", res
# 分页列表带 wm 与随机封面
res = _act("POST", {"action": "list_galleries", "offset": 0, "limit": 20})
row = next(x for x in res["galleries"] if x["name"] == "WM库")
assert row["wm"] is True and row["cover"] == fname, res
# set_wm：单库关闭 → 落盘；缺省（未设置过的库）仍为开
res = _act("POST", {"action": "set_wm", "name": "WM库", "enabled": False})
assert res["ok"] and res["wm"] is False, res
assert g._gallery_wm("WM库") is False and g._gallery_wm("别的库") is True
assert json.loads((_tmp / "settings.json").read_text(encoding="utf-8"))["gallery_wm"]["WM库"] is False
res = _act("POST", {"action": "set_wm", "name": "不存在的库", "enabled": True})
assert res["status"] == "error", res
# 空库无封面；删库时水印开关一并清理
_act("POST", {"action": "create", "name": "空库"})
res = _act("POST", {"action": "list_galleries", "offset": 0, "limit": 20})
row = next(x for x in res["galleries"] if x["name"] == "空库")
assert row["cover"] is None and row["wm"] is True, res
assert g.delete_gallery("WM库")
assert "WM库" not in json.loads((_tmp / "settings.json").read_text(encoding="utf-8"))["gallery_wm"]
assert g.delete_gallery("空库")

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

# 8) 水印渲染（本机有 PIL 时）：几何占比、条底色、二维码有/无、输出格式
try:
    from PIL import Image as PImage

    def _mkimg(w, h, color="red"):
        buf = io.BytesIO()
        PImage.new("RGB", (w, h), color).save(buf, "PNG")
        return buf.getvalue()

    src = _tmp / "t.png"
    src.write_bytes(_mkimg(400, 500))
    g._pids[f"{_tmp.name}/t.png"] = "87775536"  # 播种 pid → 应渲染二维码
    out = g._render_watermark(src)
    assert out.exists() and out != src
    assert out.suffix == ".jpg", out  # 默认输出 jpg（体积更小）
    im = PImage.open(out).convert("RGB")
    w, h = im.size
    assert (w, h) == (400, 540), (w, h)          # 条高 = 500*0.08 = 40
    b = h - 500
    assert abs(b / 500 - main.WM_BAR_FRAC) < 0.01, b
    near = lambda c, want, t=8: all(abs(c[i] - want[i]) <= t for i in range(3))  # jpg 有损
    assert near(im.getpixel((w // 2, h - 2)), main.WM_BG), "条底色不符"
    assert near(im.getpixel((w // 2, 10)), (255, 0, 0)), "原图区域被改动"  # 红图未变
    # 条内内容整体垂直居中（二维码是最高的元素，其中心即条中心）
    bar = im.crop((0, h - b, w, h)).convert("RGB")
    bg = main.WM_BG
    ys = [y for y in range(bar.height)
          for x in range(0, bar.width, 2)
          if sum(abs(bar.getpixel((x, y))[i] - bg[i]) for i in range(3)) > 60]
    assert ys, "信息条里没有内容"
    assert abs((min(ys) + max(ys)) / 2 - b / 2) <= 2, (min(ys), max(ys), b)
    # 有 pid → 二维码区域应有暗像素
    # 有 pid → 信息条右上二维码区域应有暗像素
    qr_x0 = w - round(b * main.WM_QR_SIZE) - round(b * main.WM_QR_RIGHT) - 2
    qr_zone = im.crop((qr_x0, h - b, w, h - b + round(b * 0.75)))
    assert min(qr_zone.convert("L").getdata()) < 100, "有 pid 却没有二维码"
    # 无 pid → 无二维码
    src2 = _tmp / "nopid.png"
    src2.write_bytes(_mkimg(400, 500))
    out2 = g._render_watermark(src2)
    im2 = PImage.open(out2).convert("RGB")
    w2, h2 = im2.size
    b2 = h2 - 500
    qr_zone2 = im2.crop((w2 - round(b2 * main.WM_QR_SIZE) - round(b2 * main.WM_QR_RIGHT) - 2,
                         h2 - b2, w2, h2 - b2 + round(b2 * 0.75)))
    assert min(qr_zone2.convert("L").getdata()) > 200, "无 pid 不应出现二维码"
    # 主题色：纯红图 → 压暗后的红色调
    tc = main.GalleryPlus._theme_color(PImage.new("RGB", (60, 60), (255, 0, 0)))
    assert isinstance(tc, tuple) and len(tc) == 3 and tc[0] > tc[1] and tc[0] > tc[2], tc
    # 二维码矩阵：示例 URL 应为 29×29（v3，同参考样图）
    mx = main.GalleryPlus._load_segno().make_qr(
        main.WM_QR_URL.format(pid="87775536"), error="m").matrix
    assert len(mx) == 29 and len(mx[0]) == 29 and mx[0][0] == 1, (len(mx), mx[0][0])
    # 地址只用 pid 里 _p 前面的数字：95026140_p0 与 95026140 必须生成同一张码，不同作品号则不同
    q_p0 = main.GalleryPlus._qr_image("95026140_p0", 64)
    q_num = main.GalleryPlus._qr_image("95026140", 64)
    q_other = main.GalleryPlus._qr_image("95026141_p0", 64)
    assert q_p0 is not None and q_p0.tobytes() == q_num.tobytes(), "带 _p 的 pid 生成地址时应忽略 _p 后的内容"
    assert q_p0.tobytes() != q_other.tobytes(), "不同作品号应生成不同二维码"
    # 输出格式 png
    g.config["output_format"] = "png"
    out3 = g._render_watermark(src)
    assert out3.suffix == ".png" and out3.exists(), out3
    g.config["output_format"] = "jpg"
    print(f"水印渲染 OK: {w}x{h}（几何/颜色/二维码/主题色均通过）")
except ImportError:
    print("(无 PIL，跳过水印渲染检查)")

# 9) 水印字体设置：读取/保存/非法字体拒绝
res = _act("POST", {"action": "get_settings"})
assert "fonts" in res and "available" in res and isinstance(res["available"], list), res
assert res["fonts"]["cjk"] == "simhei.ttf", res  # 默认黑体
res = _act("POST", {"action": "save_settings", "cjk": "simhei.ttf", "latin": "simhei.ttf"})
assert res["ok"], res
res = _act("POST", {"action": "save_settings", "cjk": "不存在的字体.ttf"})
assert res["status"] == "error", res
assert json.loads((_tmp / "settings.json").read_text(encoding="utf-8"))["wm_font_cjk"] == "simhei.ttf"

# 9b) 上传字体：扩展名/内容校验（信任边界），有效字体进字体池并可被选中
res = _act("POST", {"action": "upload_font", "name": "../evil.ttf", "data": base64.b64encode(b"x").decode()})
assert res["status"] == "error", res  # 路径穿越被清洗成 evil.ttf 后仍会因内容非法被拒
assert not list(g._fonts_dir.rglob("*")), "不应留下文件"
res = _act("POST", {"action": "upload_font", "name": "x.exe", "data": base64.b64encode(b"x").decode()})
assert res["status"] == "error", res
res = _act("POST", {"action": "upload_font", "name": "fake.ttf", "data": base64.b64encode(b"not a font").decode()})
assert res["status"] == "error" and res["message"] == "不是有效的字体文件", res
assert not (g._fonts_dir / "fake.ttf").exists(), "校验失败的文件应被删除"
sys_fonts = main._font_index(str(g._fonts_dir))
if sys_fonts:  # 本机有字体时：上传一份真实字体文件 → 进索引 → 可被选为水印字体
    src_font = Path(next(iter(sys_fonts.values())))
    res = _act("POST", {"action": "upload_font", "name": "上传测试.ttf",
                        "data": base64.b64encode(src_font.read_bytes()).decode()})
    assert res["ok"] and res["name"] == "上传测试.ttf", res
    assert "上传测试.ttf" in main._font_index(str(g._fonts_dir)), "上传后应进入字体索引"
    res = _act("POST", {"action": "save_settings", "cjk": "上传测试.ttf"})
    assert res["ok"], res
    res = _act("POST", {"action": "get_settings"})
    assert res["fonts"]["cjk"] == "上传测试.ttf", res
else:
    print("(本机无字体文件，跳过上传字体正向用例)")

# 10) 聊天端指令（0.1.3）：收集 / 查看图片 / 删除图片指令


class _Ev:
    """够用的假事件：只要 message_str / get_messages / plain_result / chain_result。"""

    def __init__(self, text, messages=None):
        self.message_str = text
        self._msgs = messages or []

    def get_messages(self):
        return self._msgs

    def plain_result(self, text):
        return ("text", text)

    def chain_result(self, chain):
        return ("chain", chain)


def _img_seg(payload=b"img-bytes"):
    seg = main.Image()
    seg.convert_to_base64 = lambda: base64.b64encode(payload).decode()
    return seg


def _reply(*segs):
    r = main.Reply()
    r.chain = list(segs)
    return r


def _run_command(gen):
    async def collect():
        return [r async for r in gen]

    return asyncio.run(collect())


_act("POST", {"action": "create", "name": "指令库"})
# 收集：引用消息里的图片 → 入库（复用 save_upload 的命名/编号）
out = _run_command(g.cmd_collect(_Ev("/收集 指令库", [_reply(_img_seg())])))
assert out and out[0][0] == "text" and "已收集 1 张" in out[0][1], out
files = [p.name for p in (g._galleries / "指令库").iterdir()]
assert len(files) == 1 and files[0].startswith("指令库-"), files
assert (g._galleries / "指令库" / files[0]).read_bytes() == b"img-bytes"
# 收集：引用里没图 → 只提示，不入库
out = _run_command(g.cmd_collect(_Ev("/收集 指令库")))
assert "未在引用消息中找到图片" in out[0][1], out
assert len(list((g._galleries / "指令库").iterdir())) == 1
# 查看图片：全库随机一张
out = _run_command(g.cmd_view(_Ev("/查看图片")))
assert out and out[0][0] == "chain" and len(out[0][1]) == 1, out
# 删除图片指令：默认删该库最近添加的一张
newest = max((g._galleries / "指令库").iterdir(), key=lambda p: p.stat().st_mtime)
out = _run_command(g.cmd_delete(_Ev("/删除图片指令 指令库")))
assert f"已删除最近添加的图片：{newest.name}" in out[0][1], out
assert not newest.exists()
# 删除图片指令 <库> ALL：连库一起删
_run_command(g.cmd_collect(_Ev("/收集 指令库", [_reply(_img_seg(b"x2"))])))
out = _run_command(g.cmd_delete(_Ev("/删除图片指令 指令库 ALL")))
assert "已删除图库「指令库」全部 1 张图片" in out[0][1], out
assert not (g._galleries / "指令库").exists()
# 删除图片指令：库不存在 / 缺参数
out = _run_command(g.cmd_delete(_Ev("/删除图片指令 不存在的库")))
assert "未找到与「不存在的库」匹配的图片" in out[0][1], out
out = _run_command(g.cmd_delete(_Ev("/删除图片指令")))
assert "用法" in out[0][1], out

shutil.rmtree(_tmp)
print("selftest OK")
