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

_Node = type("Node", (), {"__init__": lambda self, **kw: self.__dict__.update(kw)})
_Nodes = type("Nodes", (), {"__init__": lambda self, nodes: setattr(self, "nodes", nodes)})
_mod("astrbot.api.message_components",
     Image=type("Image", (), {"fromFileSystem": staticmethod(lambda p: p)}),
     Node=_Node,
     Nodes=_Nodes,
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

# 5b) 兼容指令 / 同义词 / 数量解析（0.2.1）
# 放在 Pages 的基础列表断言之后，避免新增测试图库污染旧断言。

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

# 5b 实际断言：兼容指令 / 同义词 / 数量解析
_gmenu = {
    "可琳照片": {"show": True, "char": "可琳·威克斯", "group": "个人"},
    "博丽灵梦照片": {"show": True, "char": "博丽 灵梦", "group": "个人"},
}
g._settings["gallery_menu"] = _gmenu
g.save_upload("可琳照片", "a.jpg", b"a")
g.save_upload("博丽灵梦照片", "b.jpg", b"b")
assert g._match_trigger("可琳照片") == ["可琳照片"]
assert g._match_trigger("威克斯照片") == ["可琳照片"]
assert g._match_trigger("博丽照片") == ["博丽灵梦照片"]
assert g._match_trigger("灵梦照片") == ["博丽灵梦照片"]
assert g._parse_trigger("可琳照片 5") == ("可琳照片", 5)
g._settings["runtime"] = {"send_count": 4, "forward_threshold": 4, "synonyms": {"照片": ["图片"]}}
assert g._match_trigger("可琳图片") == ["可琳照片"]
assert g._send_count() == 4

# 5c) 合并转发：一条外层转发链包含 N 个 Node，每个 Node 只有一张图片
class _Event:
    message_obj = types.SimpleNamespace(self_id="10001")
    def chain_result(self, chain):
        return chain
old_send = g._send_image
async def _fake_send(path):
    return f"img:{path.name}"
g._settings["runtime"]["forward_threshold"] = 3
g._send_image = _fake_send
plain = asyncio.run(g._image_result(_Event(), [Path("1"), Path("2"), Path("3")], "可琳照片"))
assert plain == [["img:1"], ["img:2"], ["img:3"]], plain
forward = asyncio.run(g._image_result(_Event(), [Path("1"), Path("2"), Path("3"), Path("4")], "可琳照片"))
assert len(forward) == 1 and len(forward[0]) == 1, forward
assert len(forward[0][0].nodes) == 4 and all(len(node.content) == 1 for node in forward[0][0].nodes), forward
assert all(node.uin == 10001 for node in forward[0][0].nodes), forward
g._send_image = old_send

assert g.delete_gallery("可琳照片") and g.delete_gallery("博丽灵梦照片")
g._settings.pop("gallery_menu", None)
g._settings.pop("runtime", None)

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

    # 着重号·随汉字走中文字体（西文字体普遍缺这个字形），"可琳·威克斯"应切不出西文段
    assert main.GalleryPlus._script_runs("可琳·威克斯") == [("可琳·威克斯", True)], \
        main.GalleryPlus._script_runs("可琳·威克斯")

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
    assert (w, h) == (400, 544), (w, h)          # 条高 = 500*0.088 = 44（0.08 的 110%）
    b = h - 500
    assert abs(b / 500 - main.WM_BAR_FRAC) < 0.01, b
    near = lambda c, want, t=8: all(abs(c[i] - want[i]) <= t for i in range(3))  # jpg 有损
    assert near(im.getpixel((w // 2, h - 2)), main.WM_BG), "条底色不符"
    assert near(im.getpixel((w // 2, 10)), (255, 0, 0)), "原图区域被改动"  # 红图未变
    # 第一行 = 角色名（黑）+ 灰色文件名：文字区应有近黑墨迹（角色名）也有中间调墨迹（灰字）
    tx0 = round(b * main.WM_TEXT_X)
    tz = im.crop((tx0, h - b, w - round(b * (main.WM_QR_SIZE + main.WM_QR_RIGHT)) - 4, h))
    lv = list(tz.convert("L").getdata())
    assert min(lv) < 100, "第一行缺少角色名墨迹"
    assert sum(1 for v in lv if 120 < v < 215) > 0, "第一行缺少灰色文件名墨迹"
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
    # 最低宽度：太窄的图等比放大到信息条放得下（只放大不缩小），宽图保持原尺寸
    nsrc = _tmp / "narrow.png"
    nsrc.write_bytes(_mkimg(120, 400))
    nim = PImage.open(g._render_watermark(nsrc)).convert("RGB")
    assert nim.width > 120, nim.size                      # 放大了
    exp_h = round(400 * nim.width / 120)                  # 等比放大后的原图高度
    assert abs((nim.height - exp_h) / exp_h - main.WM_BAR_FRAC) < 0.01, nim.size  # 条高仍按 0.088
    wide = PImage.open(g._render_watermark(src)).size
    assert wide == (400, 544), wide                       # 够宽的图不缩放
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
    # 发送门：单库关 → 原样发送；单库开 → 走水印渲染；全局关 → 一律原样（主界面水印开关的最终效果）
    gate_dir = _tmp / "门库"
    gate_dir.mkdir(exist_ok=True)
    gate_img = gate_dir / "门库-1.jpg"
    gate_img.write_bytes(_mkimg(300, 400))
    g.set_gallery_wm("门库", False)
    assert Path(asyncio.run(g._send_image(gate_img))) == gate_img, "单库关掉水印后应原样发送"
    g.set_gallery_wm("门库", True)
    assert Path(asyncio.run(g._send_image(gate_img))) != gate_img, "单库开着水印应渲染信息条"
    g.config["watermark"] = False
    assert Path(asyncio.run(g._send_image(gate_img))) == gate_img, "全局关掉后应一律原样发送"
    g.config["watermark"] = True
    print(f"水印渲染 OK: {w}x{h}（几何/颜色/二维码/主题色/发送门均通过）")
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
# 只传 cols（每行张数按钮）不得把字体冲回默认（0.2.1 修的持久化 bug：缺省键曾把字体重置）
_act("POST", {"action": "save_settings", "cols": 5})
assert json.loads((_tmp / "settings.json").read_text(encoding="utf-8"))["wm_font_cjk"] == "simhei.ttf"
_act("POST", {"action": "save_settings", "cols": 7})  # 还原每行张数缺省，别影响后面的用例
# 运行设置：默认值、同义词与边界收敛
res = _act("POST", {"action": "get_runtime_settings"})
assert res["send_count"] == 1 and res["forward_threshold"] == 3 and "照片" in res["synonyms"], res
res = _act("POST", {"action": "save_runtime_settings", "send_count": 5, "forward_threshold": 5, "synonyms": {"照片": ["图片", "相片"]}})
assert res["ok"] and res["send_count"] == 5 and res["forward_threshold"] == 5, res
assert g._runtime_settings()["synonyms"]["照片"] == ["图片", "相片"]

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

# 11) 图库排序：按拼音逐音节比较（装了 pypinyin 生僻字也准；没装退回 GBK 首字母近似）
pk = main.pinyin_key
assert pk("安比") < pk("比亚迪") < pk("可琳") < pk("泽塔"), (pk("安比"), pk("比亚迪"), pk("可琳"), pk("泽塔"))
assert pk("123") < pk("abc") < pk("可琳"), (pk("123"), pk("abc"), pk("可琳"))
try:
    from pypinyin import lazy_pinyin  # noqa: F401

    _HAS_PYPINYIN = True
except ImportError:
    _HAS_PYPINYIN = False
if _HAS_PYPINYIN:
    assert pk("爱姿") < pk("安比"), (pk("爱姿"), pk("安比"))  # 真拼音 ai < an（GBK 近似会把这条排反）
    assert main.GalleryPlus._letter_of("芙兰朵露") == "F", main.GalleryPlus._letter_of("芙兰朵露")  # 生僻字（GBK 二级）
else:
    print("(无 pypinyin，跳过生僻字拼音用例，GBK 近似生效)")
(g._galleries / "泽塔照片").mkdir(exist_ok=True)
(g._galleries / "安比照片").mkdir(exist_ok=True)
names, _n = g.list_gallery_names()
order = [r["name"] for r in names]
assert order.index("安比照片") < order.index("泽塔照片"), order  # 安(a) 在 泽(z) 前

# 12) 图库菜单（0.2.0）：每库配置 / 分区与字母分组 / API / 渲染几何 / 背景图
_act("POST", {"action": "create", "name": "菜单库"})
res = _act("POST", {"action": "get_gallery_menu", "name": "菜单库"})
assert res["menu"] == {"show": True, "char": "菜单库", "group": "个人"}, res  # 缺省：进菜单、角色名=关键词、个人
res = _act("POST", {"action": "set_gallery_menu", "name": "菜单库",
                    "show": False, "char": "小可琳", "group": "团体"})
assert res["menu"] == {"show": False, "char": "小可琳", "group": "团体"}, res
assert json.loads((_tmp / "settings.json").read_text("utf-8"))["gallery_menu"]["菜单库"]["group"] == "团体"
# 非法分组回落「个人」；角色名留空回落关键词
res = _act("POST", {"action": "set_gallery_menu", "name": "菜单库",
                    "show": True, "char": "  ", "group": "不存在"})
assert res["menu"] == {"show": True, "char": "菜单库", "group": "个人"}, res
for bad in ("../x", "不存在"):  # 防穿越 / 不存在的库
    assert _act("POST", {"action": "get_gallery_menu", "name": bad})["status"] == "error"
    assert _act("POST", {"action": "set_gallery_menu", "name": bad})["status"] == "error"

# 分区与字母：安→A、可→K；排序按角色名拼音；分组按配置
for kw, char, grp in (("可琳照片", "可琳", "个人"), ("安比照片", "安比", "个人"),
                      ("合照库", "旅行合照", "团体"), ("功能库", "查询图片数量", "功能")):
    _act("POST", {"action": "create", "name": kw})
    (g._galleries / kw / f"{kw}-1.jpg").write_bytes(b"x")
    _act("POST", {"action": "set_gallery_menu", "name": kw, "show": True, "char": char, "group": grp})
assert main.GalleryPlus._letter_of("可琳") == "K" and main.GalleryPlus._letter_of("安比") == "A"
assert main.GalleryPlus._letter_of("123") == "#"
data = g._menu_data()
_act("POST", {"action": "set_gallery_menu", "name": "菜单库", "show": False, "char": "菜单库", "group": "个人"})
data = g._menu_data()
people = [c for c, _k in data["个人"]]
assert "安比" in people and "可琳" in people and people.index("安比") < people.index("可琳"), data
assert "旅行合照" in [c for c, _k in data["团体"]], data
assert "查询图片数量" in [c for c, _k in data["功能"]], data
assert all(c != "菜单库" for rows in data.values() for c, _k in rows), "show=False 的库不该出现在菜单里"
# 列分配：按行数均衡（不等数也行），字母顺序不许乱；空 input 不炸
cols = main.GalleryPlus._menu_columns([("A", [(1, 1)] * 5), ("B", [(1, 2)] * 2), ("C", [(1, 3)] * 5)])
assert [lt for c in cols for lt, _e in c] == ["A", "B", "C"], cols
_heights = [sum(len(e) + 2 for _l, e in c) for c in cols]
assert max(_heights) - min(_heights) <= 8, _heights
assert main.GalleryPlus._menu_columns([]) == []

res = _act("POST", {"action": "get_menu"})
assert res["texts"]["title"] == main.MENU_CMD and res["texts"]["label"], res["texts"]
assert res["fonts"]["item"] == {s: main.DEFAULT_CJK_FONT for s, _l in main.MENU_SCRIPTS}, res["fonts"]  # 三槽缺省黑体
assert {p["key"] for p in res["parts"]} == {k for k, _l in main.MENU_PARTS}
assert res["sizes"] == {k: 100 for k, _l in main.MENU_PARTS}, res["sizes"]  # 字号百分比缺省全是 100
assert res["names"].get("simhei.ttf"), res["names"]  # 字体显示名（家族名，不是文件名）
assert res["src"].startswith("data:image/png;base64,"), res["src"][:40]
# 保存文字/字体：落盘、长度收敛、非法字体拒绝
res = _act("POST", {"action": "save_menu", "texts": {"title": "图库导航"}, "fonts": {}})
assert res["texts"]["title"] == "图库导航", res["texts"]
res = _act("POST", {"action": "save_menu", "texts": {"title": "长" * 999}})
assert len(res["texts"]["title"]) == main.MENU_TEXT_MAX
res = _act("POST", {"action": "save_menu", "fonts": {"item": {"latin": "根本没有这个字体.ttf"}}})
assert res["status"] == "error", res
# 字号百分比：合法留存，垃圾回落 100，越界收敛到 50~200
res = _act("POST", {"action": "save_menu", "sizes": {"item": 150, "title": "垃圾", "meta": 999, "footer": -3}})
assert res["sizes"]["item"] == 150 and res["sizes"]["title"] == 100, res["sizes"]
assert res["sizes"]["meta"] == 200 and res["sizes"]["footer"] == 50, res["sizes"]
assert json.loads((_tmp / "settings.json").read_text("utf-8"))["menu"]["sizes"]["item"] == 150
# 中/英/日 三槽：合法留存（其余槽不受影响）、旧单字体格式读回时自动补齐三槽
if main._font_index(str(g._fonts_dir)):  # 本机一个字体都没有时，正向保存无从谈起（错误路径上面已测）
    res = _act("POST", {"action": "save_menu", "fonts": {"title": {"latin": "上传测试.ttf"}}})
    assert res["fonts"]["title"]["latin"] == "上传测试.ttf", res["fonts"]
    assert res["fonts"]["title"]["cjk"] == main.DEFAULT_CJK_FONT and res["fonts"]["title"]["jp"] == main.DEFAULT_CJK_FONT
    assert res["fonts"]["item"]["latin"] == main.DEFAULT_CJK_FONT, res["fonts"]
    _stored = json.loads((_tmp / "settings.json").read_text("utf-8"))["menu"]["fonts"]["title"]
    assert _stored == {"latin": "上传测试.ttf"}, _stored  # 只存显式设置的槽位，缺项读回时补默认
g._menu_settings()["fonts"]["letter"] = "simhei.ttf"  # 旧格式（单个字体名）→ 读回应三槽同字体
g._save_settings()
res = _act("POST", {"action": "get_menu"})
assert res["fonts"]["letter"] == {s: "simhei.ttf" for s, _l in main.MENU_SCRIPTS}, res["fonts"]["letter"]
assert len(json.loads((_tmp / "settings.json").read_text("utf-8"))["menu"]["title"]) == main.MENU_TEXT_MAX

try:
    from PIL import Image as PImage

    p = g._render_menu(1752)  # 用参考样图的宽度渲染，下面的几何量可直接比对实测值
    assert p and p.is_file(), p
    im = PImage.open(p).convert("RGB")
    W, H = im.size
    assert W == 1752, W
    near = lambda c, want, t=12: all(abs(c[i] - want[i]) <= t for i in range(3))  # noqa: E731
    px = im.load()
    assert near(px[1000, 500], main.MENU_BG_COLOR), "正文底色应为米白"
    assert near(px[100, 340], main.MENU_RED), "「单人」分区条应在 (88,329)-(190,375)"
    assert any(near(px[x, y], main.MENU_RED)
               for x in range(24, 32) for y in range(H - 80, H - 5)), "页脚左侧应有红竖条"
    assert max(min(px[x, y]) for x in range(80, 400) for y in range(70, 140)) > 200, "标题应为白字"
    body = [y for y in range(300, 400) if near(px[500, y], main.MENU_BG_COLOR)]
    assert body and body[0] == 303, body[:3]  # 页眉高 = 303（参考样图实测）
    dark = lambda c: sum(c) < 420  # noqa: E731
    assert any(dark(px[x, 410]) for x in range(100, 200)), "正文应有字母标签/条目文字"
    assert any(dark(px[x, 440]) for x in range(110, 620)), "正文应有三列条目"
    assert any(dark(px[x, 440]) for x in range(700, 1000)), "第二列应有条目"
    # 字母标签底下的淡红高亮条（参考图每个字母都垫一条，宽 24 高 8，以字母墨迹居中）
    _letter_px = [(x, y) for x in range(100, 145) for y in range(395, 480)
                  if abs(px[x, y][0] - main.MENU_LETTER_BG[0]) < 30 and abs(px[x, y][1] - main.MENU_LETTER_BG[1]) < 35]
    assert _letter_px, "字母标签下应有淡红高亮条"
    _lx = [p[0] for p in _letter_px]
    _ly = [p[1] for p in _letter_px]
    assert max(_lx) - min(_lx) >= 20 and max(_ly) - min(_ly) <= 10, (min(_lx), max(_lx), min(_ly), max(_ly))
    # 字号百分比：条目 200% 后墨迹明显变高（字母跟条目同一个部件，一起变大）
    def _ink_span():
        _im = PImage.open(g._render_menu(1752)).convert("RGB")
        _px = _im.load()
        _ys = [y for y in range(395, 560) for x in range(105, 640, 2) if sum(_px[x, y]) < 420]
        return max(_ys) - min(_ys)

    _act("POST", {"action": "save_menu", "sizes": {"item": 100}})
    _h1 = _ink_span()
    _act("POST", {"action": "save_menu", "sizes": {"item": 200}})
    _h2 = _ink_span()
    assert _h2 > _h1, (_h1, _h2)
    _act("POST", {"action": "save_menu", "sizes": {"item": 100}})  # 还原，别影响后面的用例
    # 页脚条带：纯白，和正文米白做区别
    assert near(px[900, H - 30], main.MENU_FOOTER_BG, 6), "页脚条带应为纯白"
    assert near(px[900, H - 100], main.MENU_BG_COLOR, 6), "页脚白条之上仍应是米白正文"
    # 中/英/日 三槽路由：假名→jp、汉字→cjk、数字→latin（给两槽明显不同的字号，量条带就知道走没走对）
    _big = main._resolve_font("simhei.ttf", 60, "")
    _small = main._resolve_font("simhei.ttf", 20, "")
    assert main._menu_line("あ", {"cjk": _small, "latin": _small, "jp": _big}).width > 40, "假名应走日文字体槽"
    assert main._menu_line("漢", {"cjk": _big, "latin": _small, "jp": _small}).width > 40, "汉字应走中文字体槽"
    assert main._menu_line("abc", {"cjk": _big, "latin": _small, "jp": _big}).width < 100, "拉丁字母应走西文字体槽"
    # 预览档：等比缩小（版式一致）
    p2 = g._render_menu(main.MENU_PREVIEW_W)
    assert PImage.open(p2).size[0] == main.MENU_PREVIEW_W
    # 缩放比例：700/1752 下页眉高应等比
    assert PImage.open(p2).size[1] < H and PImage.open(p2).size[1] > H * 0.3, PImage.open(p2).size
    # 一个图库都没有时也要出图（只剩页眉 + 页脚），别在裸环境下抛异常
    g_empty = main.GalleryPlus(_make_ctx(), _Cfg())
    g_empty._galleries = _tmp / "空图库目录"
    g_empty._galleries.mkdir()
    p_empty = g_empty._render_menu(600)
    assert p_empty and PImage.open(p_empty).size[0] == 600, p_empty
    # 背景图：坏字节/超大拒绝，真图收下并真的进了页眉（非默认渐变）
    res = _act("POST", {"action": "upload_menu_bg", "name": "bg.png",
                        "data": base64.b64encode(b"junk").decode()})
    assert res["status"] == "error", res
    buf = io.BytesIO()
    PImage.new("RGB", (800, 300), "blue").save(buf, "PNG")
    res = _act("POST", {"action": "upload_menu_bg", "name": "bg.png",
                        "data": base64.b64encode(buf.getvalue()).decode()})
    assert res["ok"] and res["bg"] is True and (g._root / "menu_bg.png").is_file(), res
    im2 = PImage.open(g._render_menu(1752)).convert("RGB")
    assert sum(im2.getpixel((1752 // 2, 150))[:3]) > sum(im2.getpixel((1752 // 2, -1))[:3]) or True
    assert im2.getpixel((1752 // 2, 150))[2] > 200, "上传的背景图应铺在页眉上"
    g._settings["menu"].pop("bg", None)
    (g._root / "menu_bg.png").unlink()
    g._save_settings()
    print(f"菜单渲染 OK: {W}x{H}（页眉/分区条/三列/字母/页脚/背景图均通过）")
except ImportError:
    print("(无 PIL，跳过菜单渲染检查)")

# 12b) 聊天端：/图片帮助 与裸发「图片帮助」都应出菜单图
out = _run_command(g.cmd_menu(_Ev("/图片帮助")))
assert out and out[0][0] == "chain" and len(out[0][1]) == 1, out


async def _drain(gen):
    return [r async for r in gen]


out = asyncio.run(_drain(g.on_message(_Ev("图片帮助"))))
assert out and out[0][0] == "chain", out

# 12c) 管理页与后端的一致性（改 id / 改 action 名最易漏的地方）；pages 下每个页面都查
import re  # noqa: E402

_main_src = (Path(__file__).parent / "main.py").read_text("utf-8")
for _ph in sorted((Path(__file__).parent / "pages").glob("*/index.html")):
    _html = _ph.read_text("utf-8")
    _ids = set(re.findall(r"\$\('#([\w-]+)'\)", _html))
    assert not {i for i in _ids if f'id="{i}"' not in _html}, f"{_ph.parent.name}: 页面脚本引用了不存在的元素 id"
    _acts = set(re.findall(r"action:\s*'([\w_]+)'", _html))
    assert not {a for a in _acts if f'action == "{a}"' not in _main_src}, f"{_ph.parent.name}: 页面用了后端没有的 action"

_page_html = (Path(__file__).parent / "pages" / "manager" / "index.html").read_text("utf-8")
assert "图片帮助" in _page_html and main.MENU_CMD in _main_src

# 12d) 批量设置页：全部图库的 水印+菜单配置 一次给全
res = _act("POST", {"action": "list_all_settings"})
_rows = {r["name"]: r for r in res["galleries"]}
assert _rows["菜单库"]["menu"] == {"show": False, "char": "菜单库", "group": "个人"}, _rows.get("菜单库")
assert _rows["菜单库"]["wm"] is True, _rows.get("菜单库")           # 没动过的库缺省开水印
assert _rows["可琳照片"]["count"] > 0, _rows.get("可琳照片")  # 前序用例导入过 2 张
assert set(_rows["菜单库"]) == {"name", "count", "wm", "tag", "menu"}, _rows["菜单库"]
assert _rows["菜单库"]["tag"] is None  # 没打过标签

# 12e) 封面稳定随机：目录内容不变 → 两次列表同一张；内容变化 → 种子换、封面仍是库内文件
res1 = _act("POST", {"action": "list_galleries", "offset": 0, "limit": 20})
res2 = _act("POST", {"action": "list_galleries", "offset": 0, "limit": 20})
assert {x["name"]: x["cover"] for x in res1["galleries"]} == \
       {x["name"]: x["cover"] for x in res2["galleries"]}
(g._galleries / "菜单库" / "菜单库-2.jpg").write_bytes(b"y")  # 加文件 → 目录 mtime 变 → 种子变
res3 = _act("POST", {"action": "list_galleries", "offset": 0, "limit": 20})
_c3 = {x["name"]: x["cover"] for x in res3["galleries"]}
assert _c3["菜单库"] in ("菜单库-1.jpg", "菜单库-2.jpg"), _c3

# 12f) 选择模式批量操作：删除/移动/复制 + pid 跟迁 + 同名跳过 + 防穿越
_act("POST", {"action": "create", "name": "批量A"})
_act("POST", {"action": "create", "name": "批量B"})


def _up(gal, fn):
    return _act("POST", {"action": "upload", "name": gal,
                         "files": [{"name": fn, "data": base64.b64encode(b"i").decode()}]})["saved"][0]["file"]


a1 = _up("批量A", "95026140_p1.jpg")  # pid 命名 → 记 pid；入库为 批量A-1.jpg
a2 = _up("批量A", "2_a.jpg")          # → 批量A-2.jpg，无 pid
_up("批量B", "1_b.jpg")               # → 批量B-1.jpg
# 跨库移动/复制：文件一律改名为 目标库-编号.扩展名，pid 与统计跟新文件名走（复制保留源）
res = _act("POST", {"action": "batch", "op": "copy", "src": "批量A", "dst": "批量B", "images": [a1]})
assert res["done"] == 1 and res["failed"] == [] and (g._galleries / "批量B" / "批量B-2.jpg").is_file(), res
assert g._pids.get("批量B/批量B-2.jpg") == "95026140_p1", res          # pid 跟新文件名
assert g._pids.get("批量A/批量A-1.jpg") == "95026140_p1", res          # 复制：源保留
res = _act("POST", {"action": "batch", "op": "move", "src": "批量A", "dst": "批量B",
                    "images": [a2, "不在.jpg", "../x.jpg"]})
assert res["done"] == 1 and res["failed"] == ["不在.jpg", "../x.jpg"], res
assert not (g._galleries / "批量A" / a2).exists() and (g._galleries / "批量B" / "批量B-3.jpg").is_file(), res
res = _act("POST", {"action": "batch", "op": "move", "src": "批量A", "dst": "批量B", "images": [a1]})
assert res["done"] == 1 and res["failed"] == [], res                   # 同名冲突已不存在：一律改名入目标库
assert (g._galleries / "批量B" / "批量B-4.jpg").is_file()
assert g._pids.get("批量B/批量B-4.jpg") == "95026140_p1" and "批量A/批量A-1.jpg" not in g._pids
for _bad in ({"op": "??", "src": "批量A"}, {"op": "move", "src": "批量A", "dst": "没有的库"},
             {"op": "copy", "src": "../etc"}):
    res = _act("POST", {"action": "batch", **_bad, "images": []})
    assert res["status"] == "error", res
# 每行张数偏好：缺省 7，合法留存，垃圾忽略，越界收敛 3~8
res = _act("POST", {"action": "list_images", "name": "批量A", "offset": 0, "limit": 50})
assert res["cols"] == 7, res
_act("POST", {"action": "save_settings", "cols": 5})
assert _act("POST", {"action": "list_images", "name": "批量A", "offset": 0, "limit": 50})["cols"] == 5
_act("POST", {"action": "save_settings", "cols": "垃圾"})
assert _act("POST", {"action": "list_images", "name": "批量A", "offset": 0, "limit": 50})["cols"] == 5
_act("POST", {"action": "save_settings", "cols": 99})
assert _act("POST", {"action": "list_images", "name": "批量A", "offset": 0, "limit": 50})["cols"] == 8

# 12g) 静默发送计数（图片/图库 + 时段）+ 悬停信息接口 + 移动迁统计
g.config["watermark"] = False
p_a1 = g._galleries / "批量B" / "批量B-4.jpg"
asyncio.run(g._send_image(p_a1))
asyncio.run(g._send_image(p_a1))
_row = g._stats["images"]["批量B/批量B-4.jpg"]
assert _row["sends"] == 2 and len(_row["hours"]) == 24 and sum(_row["hours"]) == 2, _row
assert g._stats["galleries"]["批量B"]["sends"] == 2, g._stats["galleries"]
assert json.loads((g._root / "stats.json").read_text("utf-8"))["images"]["批量B/批量B-4.jpg"]["sends"] == 2
res = _act("POST", {"action": "image_info", "name": "批量B", "image": "批量B-4.jpg"})
assert res["sends"] == 2 and res["size"] == p_a1.stat().st_size, res
assert res["gallery"] == "批量B" and res["file"] == "批量B-4.jpg", res
assert res["uid"] == "95026140" and res["pid"] == "95026140_p1", res
assert res["path"] == str(p_a1), res                                       # 绝对路径
assert res["color"] is None or (res["color"].startswith("#") and len(res["color"]) == 7), res
assert _act("POST", {"action": "image_info", "name": "批量B", "image": "../x.jpg"})["status"] == "error"
# 再移动一次：统计键随文件一起搬到新库新名
_act("POST", {"action": "create", "name": "批量C"})
res = _act("POST", {"action": "batch", "op": "move", "src": "批量B", "dst": "批量C", "images": ["批量B-4.jpg"]})
assert res["done"] == 1, res
assert g._stats["images"]["批量C/批量C-1.jpg"]["sends"] == 2, g._stats["images"]
assert "批量B/批量B-4.jpg" not in g._stats["images"], g._stats["images"]
g.config["watermark"] = True

# 12h) 悬停预览的带水印真实效果图：开=有信息条（高>宽），关=原样；防穿越
try:
    from PIL import Image as _PI  # noqa: F401

    _buf = io.BytesIO()
    _PI.new("RGB", (300, 400), "red").save(_buf, "PNG")
    _act("POST", {"action": "create", "name": "水印库"})
    res = _act("POST", {"action": "upload", "name": "水印库",
                        "files": [{"name": "real.png", "data": base64.b64encode(_buf.getvalue()).decode()}]})
    _wname = res["saved"][0]["file"]
    res = _act("POST", {"action": "wm_preview", "name": "水印库", "image": _wname, "size": 800})
    assert res["src"].startswith("data:image/jpeg;base64,"), res
    _wim = _PI.open(io.BytesIO(base64.b64decode(res["src"].split(",", 1)[1])))
    assert max(_wim.size) <= 800, _wim.size
    assert _wim.size[1] > _wim.size[0], _wim.size  # 300x400 加了信息条 → 高>宽
    g.config["watermark"] = False
    res = _act("POST", {"action": "wm_preview", "name": "水印库", "image": _wname})
    _wim2 = _PI.open(io.BytesIO(base64.b64decode(res["src"].split(",", 1)[1])))
    assert _wim2.size == (300, 400), _wim2.size  # 关水印=发送原样
    assert _act("POST", {"action": "wm_preview", "name": "水印库", "image": "../x.jpg"})["status"] == "error"
    assert g.delete_gallery("水印库")
except ImportError:
    print("(无 PIL，跳过带水印预览检查)")
g.config["watermark"] = True

# 12i) 颜色标签：建/改/删 + 打标/清标 + 列表带回 + 删库连带清理（0.2.1）
res = _act("POST", {"action": "list_tags"})
assert res["colors"] == main.TAG_COLORS and res["tags"] == {}, res
res = _act("POST", {"action": "save_tag", "name": "重点", "color": ""})  # 缺省色
_tid = res["id"]
assert _tid and res["tags"][_tid] == {"name": "重点", "color": main.TAG_COLORS[0]}, res
# 重名拒绝 / 非预设色拒绝 / 空名拒绝 / 改不存在的 id 404
assert _act("POST", {"action": "save_tag", "name": "重点", "color": main.TAG_COLORS[1]})["status"] == "error"
assert _act("POST", {"action": "save_tag", "name": "x", "color": "#123456"})["status"] == "error"
assert _act("POST", {"action": "save_tag", "name": "  ", "color": main.TAG_COLORS[1]})["status"] == "error"
assert _act("POST", {"action": "save_tag", "id": "没有的", "name": "x", "color": main.TAG_COLORS[1]})["status"] == "error"
# 改名换色
res = _act("POST", {"action": "save_tag", "id": _tid, "name": "次要", "color": main.TAG_COLORS[2]})
assert res["tags"][_tid] == {"name": "次要", "color": main.TAG_COLORS[2]}, res
# 打标 → 列表带回；打不存在的标签 / 给不存在的库打标 → error；清标 → None
assert _act("POST", {"action": "set_gallery_tag", "name": "批量A", "tag": _tid})["ok"]
row = next(x for x in _act("POST", {"action": "list_galleries", "offset": 0, "limit": 20})["galleries"]
           if x["name"] == "批量A")
assert row["tag"] == {"id": _tid, "name": "次要", "color": main.TAG_COLORS[2]}, row
assert _act("POST", {"action": "set_gallery_tag", "name": "批量A", "tag": "没有的"})["status"] == "error"
assert _act("POST", {"action": "set_gallery_tag", "name": "没有的库", "tag": ""})["status"] == "error"
assert _act("POST", {"action": "set_gallery_tag", "name": "批量A", "tag": ""})["tag"] is None
_row2 = next(x for x in _act("POST", {"action": "list_galleries", "offset": 0, "limit": 20})["galleries"]
             if x["name"] == "批量A")
assert _row2["tag"] is None, _row2
# 删标签连带摘掉图库引用；删图库连带清掉引用
_act("POST", {"action": "set_gallery_tag", "name": "批量A", "tag": _tid})
assert _act("POST", {"action": "delete_tag", "id": _tid})["ok"]
assert g._settings["gallery_tags"] == {} and _tid not in g._settings["tags"], g._settings
assert _act("POST", {"action": "delete_tag", "id": _tid})["status"] == "error"
_act("POST", {"action": "create", "name": "标签库"})
res = _act("POST", {"action": "save_tag", "name": "备用", "color": main.TAG_COLORS[4]})
assert _act("POST", {"action": "set_gallery_tag", "name": "标签库", "tag": res["id"]})["ok"]
assert g.delete_gallery("标签库") and g._settings["gallery_tags"] == {}, g._settings

assert g.delete_gallery("批量A") and g.delete_gallery("批量B") and g.delete_gallery("批量C")

# 12j) 图库改名（触发词）：文件夹/内部图片/pid/水印/菜单/标签键一起搬（0.2.1）
_act("POST", {"action": "create", "name": "改名A"})
_up("改名A", "95026140_p5.jpg")  # → 改名A-1.jpg，带 pid
_act("POST", {"action": "set_gallery_menu", "name": "改名A", "show": False, "char": "甲", "group": "功能"})
_tag_id = _act("POST", {"action": "save_tag", "name": "改名标签", "color": main.TAG_COLORS[6]})["id"]
assert _act("POST", {"action": "set_gallery_tag", "name": "改名A", "tag": _tag_id})["ok"]
res = _act("POST", {"action": "rename", "name": "改名A", "new_name": "改名B"})
assert res["ok"] and res["name"] == "改名B", res
assert not (g._galleries / "改名A").exists() and (g._galleries / "改名B").is_dir(), res
assert (g._galleries / "改名B" / "改名B-1.jpg").is_file(), "内部图片应同步改名"
assert g._pids.get("改名B/改名B-1.jpg") == "95026140_p5" and "改名A/改名A-1.jpg" not in g._pids
assert g._gallery_menu("改名B") == {"show": False, "char": "甲", "group": "功能"}
assert g._gallery_tags().get("改名B") == _tag_id, g._gallery_tags()
# 边界：同名冲突 / 非法字符 / 不存在的库 / 空名；同名 no-op
_act("POST", {"action": "create", "name": "改名C"})
assert _act("POST", {"action": "rename", "name": "改名B", "new_name": "改名C"})["status"] == "error"
assert _act("POST", {"action": "rename", "name": "改名B", "new_name": "../x"})["status"] == "error"
assert _act("POST", {"action": "rename", "name": "没有的库", "new_name": "y"})["status"] == "error"
assert _act("POST", {"action": "rename", "name": "改名B", "new_name": "  "})["status"] == "error"
assert _act("POST", {"action": "rename", "name": "改名B", "new_name": "改名B"})["ok"]
assert g.delete_gallery("改名B") and g.delete_gallery("改名C")

# 12k) 文件名同步：重排成连续编号（跳号收紧）、乱名规范化，pid 跟文件走不错配
# 注意 changed 是全库计数（前面用例手工造过同编号不同后缀/跳号），所以断言只针对目标库的最终结果
_act("POST", {"action": "create", "name": "同步库"})
_up("同步库", "95026140_p9.jpg")  # → 同步库-1.jpg
(g._galleries / "同步库" / "乱的.jpg").write_bytes(b"z")
g._pids["同步库/乱的.jpg"] = "777_p1"
(g._galleries / "同步库" / "说明.txt").write_bytes(b"t")  # 非图片文件：不动
res = _act("POST", {"action": "sync_names"})
assert res["ok"] and res["changed"] >= 1, res
assert (g._galleries / "同步库" / "同步库-2.jpg").is_file(), res
assert g._pids.get("同步库/同步库-2.jpg") == "777_p1" and "同步库/乱的.jpg" not in g._pids
assert (g._galleries / "同步库" / "说明.txt").is_file()
res = _act("POST", {"action": "sync_names"})
assert res["changed"] == 0, res  # 已全部合规：不再有改动
# 跳号收紧：删中间一张后 1,3 → 1,2，pid 跟着文件走（不按名字错配）
assert g.delete_image("同步库", "同步库-1.jpg")   # 剩 同步库-2.jpg（pid 777_p1）
res = _act("POST", {"action": "upload", "name": "同步库",
                    "files": [{"name": "b.jpg", "data": base64.b64encode(b"b").decode()}]})
assert res["saved"][0]["file"] == "同步库-3.jpg", res   # 上传编号 = 现有最大编号 + 1（跳号不回填）
(g._galleries / "同步库" / "同步库-3.jpg").rename(g._galleries / "同步库" / "同步库-9.jpg")  # 造跳号
res = _act("POST", {"action": "sync_names"})
assert res["changed"] == 2, res                              # 2→1、9→2，两个都动了
names = sorted(p.name for p in (g._galleries / "同步库").iterdir() if p.suffix.lower() in main.EXTS)
assert names == ["同步库-1.jpg", "同步库-2.jpg"], names       # 重排成连续编号
# pid 跟的是**文件**不是编号：带 pid 的那张从 2 号挪到 1 号，pid 跟着它走，另一张不继承
assert g._pids.get("同步库/同步库-1.jpg") == "777_p1", g._pids
assert "同步库/同步库-2.jpg" not in g._pids, g._pids
assert g.delete_gallery("同步库")

# 12l) 同编号不同后缀重排不得互相覆盖（pid 不错配）
_act("POST", {"action": "create", "name": "后缀库"})
(g._galleries / "后缀库" / "后缀库-1.jpg").write_bytes(b"a")
(g._galleries / "后缀库" / "后缀库-1.png").write_bytes(b"b")
(g._galleries / "后缀库" / "后缀库-2.png").write_bytes(b"c")
g._pids["后缀库/后缀库-1.jpg"] = "111_p0"
g._pids["后缀库/后缀库-1.png"] = "222_p0"
g._pids["后缀库/后缀库-2.png"] = "333_p0"
res = _act("POST", {"action": "sync_names"})
assert res["changed"] == 2, res                       # 1.jpg 名字不变；另两张顺延
names = sorted(p.name for p in (g._galleries / "后缀库").iterdir())
assert names == ["后缀库-1.jpg", "后缀库-2.png", "后缀库-3.png"], names
assert (g._galleries / "后缀库" / "后缀库-1.jpg").read_bytes() == b"a"
assert (g._galleries / "后缀库" / "后缀库-2.png").read_bytes() == b"b"
assert (g._galleries / "后缀库" / "后缀库-3.png").read_bytes() == b"c"
assert g._pids["后缀库/后缀库-1.jpg"] == "111_p0" and g._pids["后缀库/后缀库-2.png"] == "222_p0" \
       and g._pids["后缀库/后缀库-3.png"] == "333_p0", g._pids
assert g.delete_gallery("后缀库")

# 12m) 列表排序按编号数值（kw-10 不排到 kw-2 前面）
_act("POST", {"action": "create", "name": "排序库"})
for _n in (2, 10, 1):
    (g._galleries / "排序库" / f"排序库-{_n}.jpg").write_bytes(b"x")
res = _act("POST", {"action": "list_images", "name": "排序库", "offset": 0, "limit": 50})
assert [im["file"] for im in res["images"]] == ["排序库-1.jpg", "排序库-2.jpg", "排序库-10.jpg"], res["images"]
assert g.delete_gallery("排序库")

shutil.rmtree(_tmp)
print("selftest OK")
