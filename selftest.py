# -*- coding: utf-8 -*-
"""最小自检：python selftest.py（无需安装 AstrBot，astrbot 以桩模块代替）。

覆盖：pid 识别、入库重命名、pid 记录、关键词清洗与防穿越、删除逻辑、水印渲染。
"""
import io
import json
import shutil
import sys
import tempfile
import time
import types
import zipfile
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
filter_ns.EventMessageType = types.SimpleNamespace(ALL=0, GROUP_MESSAGE=1, PRIVATE_MESSAGE=2)
filter_ns.PermissionType = types.SimpleNamespace(ADMIN=0)


def _passthrough(*a, **k):
    def deco(f):
        return f
    return deco


for n in ("event_message_type", "command", "permission_type", "on_llm_request"):
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
     error_response=lambda message="", **k: {"status": "error", "message": message,
                                             "status_code": k.get("status_code", 400),
                                             "data": k.get("data") or {}},
     request=object())  # 处理器测试时会被假 request 覆盖

sys.path.insert(0, str(Path(__file__).parent))
import main  # noqa: E402

# 0a) 惰性拼音表（性能）：import 时不该展开 1.2 万字大表；首次走 GBK 退路才构建，结果不变
assert main._PY_TBL is None, "import 时不应已构建 GBK 逐字表（_PY_TBL 应惰性）"
_k = main._gbk_pinyin_key("薇薇安")  # 直接测 GBK 退路，与装没装 pypinyin 无关
assert _k == ("w", "w", "a"), _k
assert main._PY_TBL is not None and main._py_tbl() is main._PY_TBL, "首次使用后应构建并缓存同一张表"
assert main._py_tbl()[main._PY_TBL_RAW[0][1][0]] == main._PY_TBL_RAW[0][0]  # 表内容 = 生成数据块
assert main._gbk_pinyin_key("橘福福") == ("j", "f", "f"), main._gbk_pinyin_key("橘福福")

# 0c) 关键词入口监听群聊和私聊，但不抢占其他事件。
_main_text = (Path(__file__).parent / "main.py").read_text("utf-8")
assert "EventMessageType.GROUP_MESSAGE" in _main_text
assert "EventMessageType.PRIVATE_MESSAGE" in _main_text
assert "EventMessageType.ALL" not in _main_text

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

# 5c) LLM 请求前剥离历史上下文里的旧图片（0.3.0）：
# 历史 image_url 段剥掉、文本保留、全图消息补占位；字符串/空表/None 不动；语音段不剥（需求只管图片）
import asyncio  # noqa: E402  （第 6 节还有一处正式 import，重复无害）

_llm_ctx = [
    {"role": "user", "content": [{"type": "text", "text": "看看这张"},
                                 {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}]},
    {"role": "assistant", "content": "好的"},
    {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://x/y.jpg"}}]},
    {"role": "user", "content": "纯文本历史"},
    {"role": "user", "content": [{"type": "text", "text": "带语音"},
                                 {"type": "audio_url", "audio_url": {"url": "data:audio;aac;base64,BBBB"}}]},
]
assert main.strip_history_images(_llm_ctx) is True
assert _llm_ctx[0]["content"] == [{"type": "text", "text": "看看这张"}], _llm_ctx[0]
assert _llm_ctx[1]["content"] == "好的"
assert _llm_ctx[2]["content"] == [{"type": "text", "text": "[图片]"}], _llm_ctx[2]  # 空内容部分平台会报错
assert _llm_ctx[3]["content"] == "纯文本历史"
assert _llm_ctx[4]["content"][1]["type"] == "audio_url", _llm_ctx[4]
assert main.strip_history_images([]) is False
assert main.strip_history_images(None) is False
assert main.strip_history_images("[]") is False

# 钩子本体：req.contexts 被就地剥掉；req.image_urls（本次引用/上传的图）不许动；异常不外抛
class _LlmReq:
    def __init__(self, contexts):
        self.contexts = contexts
        self.image_urls = ["http://fresh/quoted.jpg"]

_req_llm = _LlmReq(json.loads(json.dumps(_llm_ctx)))
asyncio.run(g.prune_llm_history_images(object(), _req_llm))
for _m in _req_llm.contexts:
    if isinstance(_m.get("content"), list):
        assert all(p.get("type") != "image_url" for p in _m["content"]), _m
assert _req_llm.image_urls == ["http://fresh/quoted.jpg"], "本次引用的图不许动"
asyncio.run(g.prune_llm_history_images(object(), _LlmReq([{"role": "u", "content": 12345}])))  # 奇形怪状的历史不炸

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

# 7b) 批量导入设计稿：{间隔符} 通配 + 固定内容可缺席（图库名123 = 图库名）
rx3, _ = main.build_filename_regex("{图库名}123{间隔符}{编号}{间隔符}{扩展名}")
m3 = rx3.fullmatch("可琳123-123.jpg")
assert m3 and m3.group("kw") == "可琳" and m3.group("num") == "123", m3
assert rx3.fullmatch("可琳-123.jpg")          # 固定内容 123 缺席 → 仍然命中
m4 = rx3.fullmatch("可琳+9.jpg")
assert m4 and m4.group("num") == "9", m4      # 间隔符号含 + . 空格等特殊符号
m5 = rx3.fullmatch("可琳123.9.jpg")
assert m5 and m5.group("kw") == "可琳" and m5.group("num") == "9", m5
assert not rx3.fullmatch("可琳.jpg")          # 缺编号
assert main.build_filename_regex("{图库名} {编号}")[0].fullmatch("可琳 8")

# 7c) 验收流：stage 暂存 → import_list → import_finish 转正 / 同名自动合并 / 暂存间合并
_png1px = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
res = g.import_files("{图库名}-{编号}.{扩展名}", [
    {"name": "暂存库A-1.jpg", "data": _png1px},  # 真图：暂存封面缩略图用例要用 PIL 打开
    {"name": "暂存库B-1.png", "data": _b64("2")},
], stage=True)
assert [i["gallery"] for i in res["imported"]] == ["暂存库A", "暂存库B"], res
_a = g._imp_dir("暂存库A")
assert _a.is_dir() and (_a / "__导入_暂存库A-1.jpg").is_file()
assert not (g._galleries / "暂存库A").exists()          # 暂存≠正式：返回前主页看不见
rows = _act("POST", {"action": "import_list"})["galleries"]
rowA = next(r for r in rows if r["name"] == "暂存库A")
assert rowA["raw"] == "__导入_暂存库A" and rowA["count"] == 1 and rowA["cover"] == "__导入_暂存库A-1.jpg"
assert g._thumb("__导入_暂存库A", "__导入_暂存库A-1.jpg") is not None  # 暂存封面缩略图可用
r = _act("POST", {"action": "import_finish", "name": "__导入_暂存库A", "target": "暂存库A"})
assert r["ok"] and r["moved"] == 1 and r["name"] == "暂存库A", r
assert (g._galleries / "暂存库A" / "暂存库A-1.jpg").is_file()           # 转正：文件去掉暂存前缀
assert not g._imp_dir("暂存库A").exists()
g.import_files("{图库名}-{编号}.{扩展名}", [{"name": "暂存库A-5.jpg", "data": _b64("3")}], stage=True)
r = _act("POST", {"action": "import_finish", "name": "__导入_暂存库A", "target": "暂存库A"})
assert r["moved"] == 1 and (g._galleries / "暂存库A" / "暂存库A-2.jpg").is_file(), r  # 同名自动合并
g.import_files("{图库名}-{编号}.{扩展名}", [
    {"name": "暂存库B-9.jpg", "data": _b64("4")}, {"name": "暂存库C-1.jpg", "data": _b64("5")}], stage=True)
r = _act("POST", {"action": "import_finish", "name": "__导入_暂存库C", "target": "__导入_暂存库B"})
assert r["moved"] == 1 and r["name"] == "__导入_暂存库B", r             # 验收页「合并图库」= 暂存间并入
assert (g._imp_dir("暂存库B") / "__导入_暂存库B-2.jpg").is_file()
assert not g._imp_dir("暂存库C").exists()
r = _act("POST", {"action": "import_finish", "name": "__导入_暂存库B", "target": "改名目标"})
assert r["moved"] == 3 and (g._galleries / "改名目标" / "改名目标-3.jpg").is_file(), r  # 改名转正（含早前并入的 3 张）
assert _act("POST", {"action": "import_finish", "name": "__导入_没有", "target": "x"})["status"] == "error"
assert _act("POST", {"action": "import_finish", "name": "__导入_暂存库B", "target": ""})["status"] == "error"
assert g.delete_gallery("暂存库A") and g.delete_gallery("改名目标")
assert g._pids.get("暂存库A/暂存库A-1.jpg") is None  # 上面没播种 pid，这里只断言键不残留

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
    # 二维码缓存：同 (作品号, px) 复用同一对象（同图重复发送不再重算 segno），不同 px 各自正确；
    # 缓存有上限（64 条），超限按插入顺序淘汰，不会无限涨。
    assert q_p0 is main.GalleryPlus._qr_image("95026140_p0", 64), "相同 pid/px 应命中缓存"
    assert main.GalleryPlus._qr_image("95026140", 64) is q_p0, "带不带 _p 的数字主体相同 → 同一缓存条目"
    _q16 = main.GalleryPlus._qr_image("95026140_p0", 16)
    assert _q16 is not None and _q16.size == (16, 16), _q16.size
    assert main._QR_CACHE_MAX == 64 and len(main._QR_CACHE) <= main._QR_CACHE_MAX
    for _qi in range(main._QR_CACHE_MAX + 2):  # 塞满并溢出：上限必须守住
        assert main.GalleryPlus._qr_image(f"8800000{_qi}", 8) is not None
    assert len(main._QR_CACHE) <= main._QR_CACHE_MAX, len(main._QR_CACHE)
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
_act("POST", {"action": "save_settings", "cols": 4})  # 还原每行张数缺省，别影响后面的用例
# 运行设置：默认值、同义词与边界收敛
res = _act("POST", {"action": "get_runtime_settings"})
assert res["send_count"] == 1 and res["forward_threshold"] == 3 and "照片" in res["synonyms"], res
res = _act("POST", {"action": "save_runtime_settings", "send_count": 5, "forward_threshold": 5, "synonyms": {"照片": ["图片", "相片"]}})
assert res["ok"] and res["send_count"] == 5 and res["forward_threshold"] == 5, res
assert g._runtime_settings()["synonyms"]["照片"] == ["图片", "相片"]

# 9a) 运行设置新增 max_send（单次发送上限，0.3.0 同版）：缺省 10、save clamp 1..10、垃圾值回落缺省、回读一致
assert res["max_send"] == main.DEFAULT_MAX_SEND == 10, res
res = _act("POST", {"action": "save_runtime_settings", "send_count": 5, "forward_threshold": 5,
                    "max_send": 0, "synonyms": {"照片": ["图片", "相片"]}})
assert res["ok"] and res["max_send"] == 1, res
assert _act("POST", {"action": "get_runtime_settings"})["max_send"] == 1
res = _act("POST", {"action": "save_runtime_settings", "send_count": 5, "forward_threshold": 5,
                    "max_send": 99, "synonyms": {"照片": ["图片", "相片"]}})
assert res["max_send"] == 10, res
assert json.loads((_tmp / "settings.json").read_text(encoding="utf-8"))["runtime"]["max_send"] == 10
res = _act("POST", {"action": "save_runtime_settings", "send_count": 5, "forward_threshold": 5,
                    "max_send": "垃圾", "synonyms": {"照片": ["图片", "相片"]}})
assert res["max_send"] == 10, res
# 指令尾数字按当前 max_send 截断（旧实现硬套 MAX_SEND_COUNT=10）
_act("POST", {"action": "save_runtime_settings", "send_count": 5, "forward_threshold": 5,
              "max_send": 4, "synonyms": {"照片": ["图片", "相片"]}})
assert g._parse_trigger("可琳照片 99") == ("可琳照片", 4)
assert g._parse_trigger("可琳照片 3") == ("可琳照片", 3)
assert g._parse_trigger("可琳照片") == ("可琳照片", None)
# _runtime_settings 自身兜底：手改 settings.json 的脏数据也不能破界
g._settings["runtime"]["max_send"] = "垃圾"
assert g._runtime_settings()["max_send"] == main.DEFAULT_MAX_SEND
g._settings["runtime"]["max_send"] = 999
assert g._runtime_settings()["max_send"] == main.MAX_SEND_COUNT
g._settings["runtime"]["max_send"] = -3
assert g._runtime_settings()["max_send"] == 1
# 缺键的旧调用（不带 max_send）沿用已存上限，不把新设置冲回缺省
_act("POST", {"action": "save_runtime_settings", "send_count": 5, "forward_threshold": 5,
              "max_send": 4, "synonyms": {"照片": ["图片", "相片"]}})
res = _act("POST", {"action": "save_runtime_settings", "send_count": 5, "forward_threshold": 5,
                    "synonyms": {"照片": ["图片", "相片"]}})
assert res["max_send"] == 4 and res["send_count"] == 5, res
# 还原运行设置到本节开始时的值，别影响后续用例
res = _act("POST", {"action": "save_runtime_settings", "send_count": 5, "forward_threshold": 5,
                    "max_send": 10, "synonyms": {"照片": ["图片", "相片"]}})
assert res["max_send"] == 10, res

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

# 9c) 界面字体（ui_font）：设置读写 / 缺省键不动 / 空串恢复；候选列表拆到 ui_font_options；
#     内嵌 OPPO Sans 字节接口（bridge 兜底）
# 主页启动只调 get_ui_font：必须只回 ui 值、绝不扫字体目录（探针证明候选列表函数没被调用）。
_orig_ui_options = main._ui_font_options
_ui_probe = []


def _ui_probe_fn(d):
    _ui_probe.append(d)
    return ["探针字体"]


main._ui_font_options = _ui_probe_fn
try:
    res = _act("POST", {"action": "get_ui_font"})
    assert set(res) == {"ui"} and res["ui"] == "", res  # 缺省=跟随内置字体，且只有 ui 一个键
    assert _ui_probe == [], "get_ui_font 不该再扫字体目录（候选列表走 ui_font_options）"
    res = _act("POST", {"action": "ui_font_options"})  # 打开「WebUI 自定义」弹窗时才取
    assert res["options"] == ["探针字体"] and len(_ui_probe) == 1, (res, _ui_probe)
finally:
    main._ui_font_options = _orig_ui_options
# 真实候选列表：列表类型、只给显示名不泄路径、本机有字体时非空
res = _act("POST", {"action": "ui_font_options"})
assert isinstance(res["options"], list), res
assert not any(("/" in o or "\\" in o) for o in res["options"]), res  # 只给显示名，不泄路径
if main._font_index(str(g._fonts_dir)):
    assert res["options"], "本机有字体（或上传池）时候选列表不应为空"
_act("POST", {"action": "save_settings", "ui_font": "Microsoft YaHei"})
assert _act("POST", {"action": "get_ui_font"})["ui"] == "Microsoft YaHei"
assert json.loads((_tmp / "settings.json").read_text("utf-8"))["ui_font"] == "Microsoft YaHei"
_act("POST", {"action": "save_settings", "cols": 5})  # 只传别的键（每行张数）：ui_font 不能被冲掉
assert _act("POST", {"action": "get_ui_font"})["ui"] == "Microsoft YaHei"
_act("POST", {"action": "save_settings", "cols": 4})
_act("POST", {"action": "save_settings", "ui_font": ""})  # 显式空串=恢复内置字体
assert _act("POST", {"action": "get_ui_font"})["ui"] == ""
assert json.loads((_tmp / "settings.json").read_text("utf-8"))["ui_font"] == ""
_act("POST", {"action": "save_settings", "ui_font": "字" * 300})  # 信任边界：截断到 200 字
assert _act("POST", {"action": "get_ui_font"})["ui"] == "字" * 200
_act("POST", {"action": "save_settings", "ui_font": ""})  # 还原，别影响后续用例
# 内嵌字体字节：先测缺文件 404（成功结果会进进程内缓存，顺序不能反）；非法 weight → 400；两个字重都能取
_font_file = Path(__file__).parent / "pages" / "manager" / "fonts" / "OPPOSans-M.woff2"
_font_bak = _font_file.with_name(_font_file.name + ".bak")
_font_file.rename(_font_bak)
try:
    res = _act("POST", {"action": "ui_font", "weight": "M"})
    assert res["status"] == "error" and res["message"] == "内嵌字体不存在", res
finally:
    _font_bak.rename(_font_file)  # 记得恢复，后面用例还要读
for _w in ("M", "B"):
    res = _act("POST", {"action": "ui_font", "weight": _w})
    assert res["src"].startswith("data:font/woff2;base64,") and len(res["src"]) > 1000, (_w, res)
assert _act("POST", {"action": "ui_font"})["src"].startswith("data:font/woff2;base64,"), "缺省 weight=M"
assert _act("POST", {"action": "ui_font", "weight": "X"})["status"] == "error"

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
else:
    print("(无 pypinyin，跳过生僻字拼音用例，GBK 法生效)")
# GBK 逐字表：薇/芙（二级字）、橘（扩展区）旧区间法全部错归 z，装不装 pypinyin 都必须归对字母
assert main.GalleryPlus._letter_of("薇薇安") == "W", main.GalleryPlus._letter_of("薇薇安")
assert main.GalleryPlus._letter_of("橘福福") == "J", main.GalleryPlus._letter_of("橘福福")
assert main.GalleryPlus._letter_of("芙兰朵露") == "F", main.GalleryPlus._letter_of("芙兰朵露")
(g._galleries / "泽塔照片").mkdir(exist_ok=True)
(g._galleries / "安比照片").mkdir(exist_ok=True)
names, _n = g.list_gallery_names()
order = [r["name"] for r in names]
assert order.index("安比照片") < order.index("泽塔照片"), order  # 安(a) 在 泽(z) 前

# 11b) 批量管理页排序键 role_sort_key：按「角色名每个字的拼音首字母」逐字 A→Z
_rk = main.role_sort_key
# 中文多字：姓/名前缀逐字比首字母（阿a < 白b < 陈c < 可k < 泽z）
assert _rk("阿慈谷 日富美") < _rk("白花") < _rk("陈") < _rk("可琳") < _rk("泽塔"), (
    _rk("阿慈谷 日富美"), _rk("白花"), _rk("陈"), _rk("可琳"), _rk("泽塔"))
# 空白忽略：有无空格同键；纯粹空白 = 空键
assert _rk("阿慈谷 日富美") == _rk("阿慈谷日富美") == _rk(" 阿慈谷 日富美 ")
assert _rk("阿慈谷 日富美")[:3] == ("a", "c", "g"), _rk("阿慈谷 日富美")  # 每字一个首字母
assert _rk("") == () and _rk("   ") == () and _rk("\t\n") == ()
# 拉丁串拆成单字符逐个比（不是整串当一个元素）；大小写不敏感
assert _rk("abc") == ("a", "b", "c"), _rk("abc")
assert _rk("AbC") == _rk("abc") == ("a", "b", "c")
assert _rk("12AB") == ("1", "2", "a", "b"), _rk("12AB")
assert _rk("a") < _rk("ab") < _rk("b")
# 第二字不同按第二字首字母（首字母相同的多字名也有确定顺序）
assert _rk("阿白") < _rk("阿慈"), (_rk("阿白"), _rk("阿慈"))
# 排序整串名字：逐字首字母即字典序
assert sorted(["陈", "阿慈谷 日富美", "白花", "泽塔", "可琳"], key=_rk) == [
    "阿慈谷 日富美", "白花", "陈", "可琳", "泽塔"]

# 11b+) 模拟「装了 pypinyin」：role_sort_key 必须优先走 pypinyin 分支（不是只靠 GBK 表），
#       逐字取音节首字母；测完恢复真实环境（本机可能没装 pypinyin）
_real_pypinyin = sys.modules.get("pypinyin")
_fake_pypinyin = types.ModuleType("pypinyin")
_FAKE_SYL = {"阿": "a", "慈": "ci", "谷": "gu", "日": "ri", "富": "fu", "美": "mei",
             "白": "bai", "花": "hua", "陈": "chen", "可": "ke", "琳": "lin",
             "泽": "ze", "塔": "ta", "比": "bi", "安": "an"}
_fake_pypinyin.lazy_pinyin = lambda s: [_FAKE_SYL.get(ch, ch) for ch in (s or "")]
_saved_lazy = main._PINYIN_LAZY
try:
    sys.modules["pypinyin"] = _fake_pypinyin
    main._PINYIN_LAZY = None  # 重新探测（拿到刚塞进去的假模块）
    main.pinyin_key.cache_clear()
    main._first_letter.cache_clear()
    main.role_sort_key.cache_clear()
    assert main.role_sort_key("阿慈谷 日富美") == ("a", "c", "g", "r", "f", "m"), main.role_sort_key("阿慈谷 日富美")
    assert main.role_sort_key("白花") < main.role_sort_key("陈") < main.role_sort_key("可琳")
    assert main.pinyin_key("阿比")[:2] == ("a", "b"), main.pinyin_key("阿比")  # 真拼音路径逐音节
finally:
    main._PINYIN_LAZY = _saved_lazy
    main.pinyin_key.cache_clear()
    main._first_letter.cache_clear()
    main.role_sort_key.cache_clear()
    if _real_pypinyin is not None:
        sys.modules["pypinyin"] = _real_pypinyin
    else:
        sys.modules.pop("pypinyin", None)

# 11c) 字体对象缓存：同一 (绝对路径, 字号) 复用同一 ImageFont 对象（每张图不再重新 truetype），
#      换字号才重新加载；改字体池（_font_index.cache_clear）后仍按绝对路径正确命中/回退
_font_now = next(iter(main._font_index(str(g._fonts_dir))), None)
if _font_now:
    _f1 = main._resolve_font(_font_now, 40, str(g._fonts_dir))
    _f2 = main._resolve_font(_font_now, 40, str(g._fonts_dir))
    _f3 = main._resolve_font(_font_now, 41, str(g._fonts_dir))
    assert _f1 is _f2, "同 (路径, 字号) 应复用缓存字体对象"
    assert _f1 is not _f3, "不同字号应是不同字体对象"
    _w1, _w2 = g._wm_fonts(40)
    _w3, _w4 = g._wm_fonts(40)
    assert (_w1 is _w3) and (_w2 is _w4), "水印字体链路上同字号也应复用对象"
    assert _f1.getmetrics() == _f2.getmetrics()  # 缓存对象行为与重新加载一致


# 12) 图库菜单（0.2.0）：每库配置 / 分区与字母分组 / API / 渲染几何 / 背景图
_act("POST", {"action": "create", "name": "菜单库"})
res = _act("POST", {"action": "get_gallery_menu", "name": "菜单库"})
assert res["menu"] == {"show": True, "char": "菜单库", "group": "个人",
                       "y_start": "", "y_end": "", "y_month": False}, res  # 缺省：进菜单、角色名=关键词、个人、无年份
res = _act("POST", {"action": "set_gallery_menu", "name": "菜单库",
                    "show": False, "char": "小可琳", "group": "团体"})
assert res["menu"] == {"show": False, "char": "小可琳", "group": "团体",
                       "y_start": "", "y_end": "", "y_month": False}, res
assert json.loads((_tmp / "settings.json").read_text("utf-8"))["gallery_menu"]["菜单库"]["group"] == "团体"
# 非法分组回落「个人」；角色名留空回落关键词；不带 y_* 字段=保留原值（批量页整行保存不抹年份）
res = _act("POST", {"action": "set_gallery_menu", "name": "菜单库",
                    "show": True, "char": "  ", "group": "不存在"})
assert res["menu"] == {"show": True, "char": "菜单库", "group": "个人",
                       "y_start": "", "y_end": "", "y_month": False}, res
for bad in ("../x", "不存在"):  # 防穿越 / 不存在的库
    assert _act("POST", {"action": "get_gallery_menu", "name": bad})["status"] == "error"
    assert _act("POST", {"action": "set_gallery_menu", "name": bad})["status"] == "error"

# 12a) 图库年份（0.2.3）：设置/读回/非法拒绝；_year_text 与 years 菜单数据
res = _act("POST", {"action": "set_gallery_menu", "name": "菜单库", "show": True, "char": "菜单库",
                    "group": "个人", "y_start": "2024", "y_end": "2025"})
assert res["menu"]["y_start"] == "2024" and res["menu"]["y_end"] == "2025" and res["menu"]["y_month"] is False, res
assert _act("POST", {"action": "set_gallery_menu", "name": "菜单库", "show": True,
                     "char": "菜单库", "group": "个人"})["menu"]["y_start"] == "2024"  # 不带 y_* 保留
res = _act("POST", {"action": "set_gallery_menu", "name": "菜单库", "show": True, "char": "菜单库",
                    "group": "个人", "y_start": 2024, "y_end": 2025})
assert res["menu"]["y_start"] == "2024" and res["menu"]["y_end"] == "2025", res  # JSON 数字也认
for bad in ({"y_start": "2024", "y_end": "abc"}, {"y_start": "2024", "y_end": ""}, {"y_start": "2025", "y_end": "2024"},
            {"y_start": "2024-01", "y_end": "2024-05"}):
    r = _act("POST", {"action": "set_gallery_menu", "name": "菜单库", "show": True, "char": "菜单库",
                      "group": "个人", **bad})
    assert r["status"] == "error", (bad, r)  # 非法 / 单边 / 起晚于止 / 没开月却填月
res = _act("POST", {"action": "set_gallery_menu", "name": "菜单库", "show": True, "char": "菜单库",
                    "group": "个人", "y_start": "2024-1", "y_end": "2024-05", "y_month": True})
assert res["menu"]["y_start"] == "2024-01" and res["menu"]["y_end"] == "2024-05" and res["menu"]["y_month"] is True, res
assert main.GalleryPlus._year_text(res["menu"]) == "2024.01~2024.05"
assert main.GalleryPlus._year_text({"y_start": "2024", "y_end": "2025"}) == "2024-2025"
assert main.GalleryPlus._year_text({"y_start": "2024", "y_end": ""}) is None
data = g._menu_data(years=True)
assert ("菜单库", "2024.01~2024.05") in data["个人"] and all(data[g2] == [] for g2 in ("团体", "功能")), data
assert g._menu_data()["个人"][0][1] != "2024.01~2024.05"  # 普通菜单第二项仍是关键词

# 分区与字母：安→A、可→K；排序按角色名拼音；分组按配置
for kw, char, grp in (("可琳照片", "可琳", "个人"), ("安比照片", "安比", "个人"),
                      ("合照库", "旅行合照", "团体"), ("功能库", "查询图片数量", "功能")):
    _act("POST", {"action": "create", "name": kw})
    (g._galleries / kw / f"{kw}-1.jpg").write_bytes(b"x")
    _act("POST", {"action": "set_gallery_menu", "name": kw, "show": True, "char": char, "group": grp})
assert main.GalleryPlus._letter_of("可琳") == "K" and main.GalleryPlus._letter_of("安比") == "A"
_act("POST", {"action": "set_gallery_menu", "name": "可琳照片", "show": True, "char": "可琳",
              "group": "个人", "y_start": "2024", "y_end": "2025-02", "y_month": True})  # 年份菜单有内容可画
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
    assert g._render_menu(1752, years=True) is not None, "年份菜单（图库时间段）应能渲染"
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

# 12b2) 运行设置的 max_send 同样约束 on_message 实际发送张数（send_count 更大也发不出去）
_act("POST", {"action": "save_runtime_settings", "send_count": 5, "forward_threshold": 8,
              "max_send": 3, "synonyms": {"照片": ["图片", "相片"]}})
_act("POST", {"action": "create", "name": "上限库"})
for _i in range(1, 8):
    (g._galleries / "上限库" / f"上限库-{_i}.jpg").write_bytes(b"x")
_old_send_image = g._send_image


async def _fake_send_image(path):
    return f"img:{path.name}"


g._send_image = _fake_send_image
try:
    out = asyncio.run(_drain(g.on_message(_Ev("上限库"))))
    assert len(out) == 3, out              # send_count=5，但上限 3
    out = asyncio.run(_drain(g.on_message(_Ev("上限库 99"))))
    assert len(out) == 3, out              # 指令尾数字被上限截断到 3
    out = asyncio.run(_drain(g.on_message(_Ev("上限库 1"))))
    assert len(out) == 1, out
finally:
    g._send_image = _old_send_image
    assert g.delete_gallery("上限库")
    _act("POST", {"action": "save_runtime_settings", "send_count": 5, "forward_threshold": 5,
                  "max_send": 10, "synonyms": {"照片": ["图片", "相片"]}})

# 12b+) 图库时间段：带前缀与裸发都应出年份菜单图
out = _run_command(g.cmd_year(_Ev("/图库时间段")))
assert out and out[0][0] == "chain", out
out = asyncio.run(_drain(g.on_message(_Ev("图库时间段"))))
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

# 12c1b) 分组枚举只认 BGROUPS 常量：曾把 `GROUPS.includes(...)` 写成未定义变量（该常量已改名 BGROUPS），
#        表现为「改图库角色名后保存」时页面报 GROUPS is not defined、设置存不上。别再写裸 GROUPS。
assert "const BGROUPS" in _page_html, "页面缺少 BGROUPS 常量（分组枚举的唯一来源）"
assert not re.search(r"(?<![\w$])GROUPS\b", _page_html), \
    "页面引用了未定义的 GROUPS（分组枚举统一用 BGROUPS，否则角色名保存会 ReferenceError）"

# 12c1c) 名称类改动就地更新、不整页重拉：图库改名后按新名改已渲染格子的文件名（保留滚动/已加载范围），
# 菜单设置保存后立刻把角色名写到页头/主页卡片。别再改回「改名后 loadImages(true) 重拉网格」——
# 那会把显示区域重置到第一页（需求方明确要求不丢显示区）。
assert "it._im.file = mapFile(" in _page_html, "改名必须就地更新已渲染图片的文件名（mapFile），别整页重拉网格"
assert "applyMenuDisplay(mmGal" in _page_html, "菜单设置保存后必须就地更新角色名（applyMenuDisplay）"


# 12c2) XSS 防线：页面所有 HTML 拼接（innerHTML / insertAdjacentHTML）里的动态插值必须 esc() 转义。
# 返回已过 esc()/内部自转义的 HTML 片段（slot()/hl()）用 /*html*/ 显式标记；新增裸插值会在这里打红。
# 说明：两页都没有用 eval/new Function/document.write，innerHTML 是唯一 HTML 注入面，守住它就守住 XSS。
def _html_sink_interps(html: str):
    interps, bad = 0, []
    for m in re.finditer(r"\.innerHTML\s*\+?=|insertAdjacentHTML\s*\(", html):
        i = m.end()  # 已越过 `=` / `(`，从右侧表达式开始
        depth, quote = 0, None
        while i < len(html):
            c = html[i]
            if quote:
                if c == "\\":
                    i += 2
                    continue
                if c == quote:
                    quote = None
            elif c in "'\"`":
                quote = c
            elif c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            elif c == ";" and depth == 0:
                break
            i += 1
        for t in re.finditer(r"\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", html[m.start():i]):
            interps += 1
            expr = t.group(1).strip()
            if "esc(" not in expr and not expr.startswith("/*html*/"):
                bad.append((html.count("\n", 0, m.start()) + 1, expr[:120]))
    return interps, bad


for _ph in sorted((Path(__file__).parent / "pages").glob("*/index.html")):
    _html = _ph.read_text("utf-8")
    _n, _bad = _html_sink_interps(_html)
    assert _n >= 5, f"{_ph.parent.name}: XSS 扫描器失效（只找到 {_n} 个插值），检查 _html_sink_interps"
    assert not _bad, (
        f"{_ph.parent.name}: HTML 拼接里有未转义的插值（用户/服务端数据必须 esc(...)，"
        f"已转义的 HTML 片段才用 /*html*/ 标记）: {_bad[:5]}"
    )
    # 这些 API 能把字符串当代码跑，页面上一个都不该出现（HTML 拼接是唯一注入面，守住它就够）
    assert not re.search(r"\b(?:eval\s*\(|new\s+Function\s*\(|document\.write\s*\()", _html), \
        f"{_ph.parent.name}: 出现 eval/new Function/document.write，禁止使用"

# 12d) 批量设置页：全部图库的 水印+菜单配置 一次给全
res = _act("POST", {"action": "list_all_settings"})
_rows = {r["name"]: r for r in res["galleries"]}
assert _rows["菜单库"]["menu"] == {"show": False, "char": "菜单库", "group": "个人",
                                  "y_start": "2024-01", "y_end": "2024-05", "y_month": True}, _rows.get("菜单库")  # 批量保存只动三件套，年份保留
assert _rows["菜单库"]["wm"] is True, _rows.get("菜单库")           # 没动过的库缺省开水印
assert _rows["可琳照片"]["count"] > 0, _rows.get("可琳照片")  # 前序用例导入过 2 张
assert set(_rows["菜单库"]) == {"name", "count", "wm", "tag", "menu"}, _rows["菜单库"]
assert _rows["菜单库"]["tag"] is None  # 没打过标签

# 12d2) 批量管理页排序：list_all_settings 按角色名的逐字拼音首字母 A→Z（role_sort_key），
#       同键时用图库名拼音兜底；字段集合保持不变
for _skw, _schar in (("排序乙照片", "白花"), ("排序丙照片", "陈"), ("排序甲照片", "阿慈谷 日富美")):
    _r = _act("POST", {"action": "create", "name": _skw, "char": _schar})
    assert _r["ok"] and _r["menu"]["char"] == _schar, _r
_sorted_rows = _act("POST", {"action": "list_all_settings"})["galleries"]
_sorted_names = [r["name"] for r in _sorted_rows]
assert _sorted_names.index("排序甲照片") < _sorted_names.index("排序乙照片") \
       < _sorted_names.index("排序丙照片"), _sorted_names
# 整表严格按 (角色名首字母序列, 图库名拼音) 升序：后端确实排序，而不是碰巧
_sort_keys = [(main.role_sort_key(r["menu"]["char"] or r["name"]), main.pinyin_key(r["name"]))
              for r in _sorted_rows]
assert _sort_keys == sorted(_sort_keys), "list_all_settings 未按 role_sort_key 排序"
_srow = next(r for r in _sorted_rows if r["name"] == "排序甲照片")
assert set(_srow) == {"name", "count", "wm", "tag", "menu"}, _srow  # 返回字段没变
# 改角色名 → 下一次请求顺序立即跟着变（不缓存旧顺序）
_act("POST", {"action": "set_gallery_menu", "name": "排序甲照片", "show": True,
              "char": "紫阳", "group": "个人"})
_sorted_names = [r["name"] for r in _act("POST", {"action": "list_all_settings"})["galleries"]]
assert _sorted_names.index("排序乙照片") < _sorted_names.index("排序丙照片") \
       < _sorted_names.index("排序甲照片"), _sorted_names  # 白(b) < 陈(c) < 紫(z)
for _skw in ("排序甲照片", "排序乙照片", "排序丙照片"):
    assert g.delete_gallery(_skw), _skw

# 12e) 封面稳定随机：目录内容不变 → 两次列表同一张；内容变化 → 种子换、封面仍是库内文件
res1 = _act("POST", {"action": "list_galleries", "offset": 0, "limit": 20})
res2 = _act("POST", {"action": "list_galleries", "offset": 0, "limit": 20})
assert {x["name"]: x["cover"] for x in res1["galleries"]} == \
       {x["name"]: x["cover"] for x in res2["galleries"]}
(g._galleries / "菜单库" / "菜单库-2.jpg").write_bytes(b"y")  # 加文件 → 目录 mtime 变 → 种子变
res3 = _act("POST", {"action": "list_galleries", "offset": 0, "limit": 20})
_c3 = {x["name"]: x["cover"] for x in res3["galleries"]}
assert _c3["菜单库"] in ("菜单库-1.jpg", "菜单库-2.jpg"), _c3

# 12e+) 主页卡片字段（0.3.0）：q 过滤（分页前）+ 全量 stats + 每行 char/years/theme
_full = _act("POST", {"action": "list_galleries", "offset": 0, "limit": 200})
_full_stats = _full["stats"]
assert _full_stats["galleries"] == _full["total"] == len(_full["galleries"]), _full
assert _full_stats["images"] == sum(x["count"] for x in _full["galleries"]), _full_stats
_all_names = [x["name"] for x in _full["galleries"]]
assert _all_names == sorted(_all_names, key=main.pinyin_key)  # q 过滤不改变原有拼音排序
for _row in _full["galleries"]:  # 卡片所需三键 + 旧四键都在，theme 是小写 hex
    assert {"name", "count", "wm", "tag", "cover", "char", "years", "theme"} <= set(_row), _row
    assert re.fullmatch(r"#[0-9a-f]{6}", _row["theme"]), _row
    assert _row["char"] == g._gallery_menu(_row["name"])["char"], _row
    assert _row["years"] == main.GalleryPlus._year_text(g._gallery_menu(_row["name"])), _row
# 已配置年份/角色名的库：值来自 _gallery_menu（菜单库在前面的年份用例里设成 2024-01~2024-05）
assert next(x for x in _full["galleries"] if x["name"] == "菜单库")["years"] == "2024.01~2024.05"
assert next(x for x in _full["galleries"] if x["name"] == "可琳照片")["char"] == "可琳"
# q 命中：分页前过滤，total=过滤后数量；stats 与 q 无关（始终全量）。
_hit = _act("POST", {"action": "list_galleries", "offset": 0, "limit": 200, "q": "照片"})
# 0.3.0 起 q 是「图库名 / 角色名 / 标签名」任一命中的子串搜索，期望值按 _full 行现算。
_want = [
    x["name"] for x in _full["galleries"]
    if "照片" in x["name"].lower()
    or "照片" in (x["char"] or "").lower()
    or (x["tag"] and "照片" in x["tag"]["name"].lower())
]
assert _want and [x["name"] for x in _hit["galleries"]] == _want, _hit
assert _hit["total"] == len(_want) and _hit["stats"] == _full_stats, _hit
_hit1 = _act("POST", {"action": "list_galleries", "offset": 0, "limit": 1, "q": "照片"})
assert _hit1["total"] == len(_want) and len(_hit1["galleries"]) == 1, _hit1
assert _hit1["galleries"][0]["name"] == _want[0], _hit1  # limit=1 切的是过滤后的集合
# 空/缺省 q 不过滤
assert _act("POST", {"action": "list_galleries", "offset": 0, "limit": 200, "q": "  "})["total"] == len(_all_names)
# 不命中：空列表 + total==0，stats 仍全量
_miss = _act("POST", {"action": "list_galleries", "offset": 0, "limit": 20, "q": "不可能存在的库ZZZ"})
assert _miss["galleries"] == [] and _miss["total"] == 0 and _miss["stats"] == _full_stats, _miss
# 大小写不敏感（英文图库名建完即删）+ 空图库：无封面/无年份，theme 回退 WM_FALLBACK_COLOR
_act("POST", {"action": "create", "name": "Abc主题库"})
_ci = _act("POST", {"action": "list_galleries", "offset": 0, "limit": 20, "q": "abc"})
assert [x["name"] for x in _ci["galleries"]] == ["Abc主题库"], _ci
assert _ci["total"] == 1 and _ci["stats"] == {"galleries": _full_stats["galleries"] + 1,
                                              "images": _full_stats["images"]}, _ci
_empty = _ci["galleries"][0]
assert _empty["cover"] is None and _empty["years"] is None and _empty["count"] == 0, _empty
assert _empty["theme"] == "#%02x%02x%02x" % main.WM_FALLBACK_COLOR == "#678d86", _empty
assert g.delete_gallery("Abc主题库")
# 真图封面：theme 与水印方块同算法（纯红聚类后压暗 ×0.75）；结果进缓存，列表不重复解码
try:
    from PIL import Image as _PT

    _pbuf = io.BytesIO()
    _PT.new("RGB", (300, 200), (255, 0, 0)).save(_pbuf, "PNG")
    _act("POST", {"action": "create", "name": "主题库"})
    _act("POST", {"action": "upload", "name": "主题库",
                  "files": [{"name": "red.png", "data": base64.b64encode(_pbuf.getvalue()).decode()}]})
    _trow = next(x for x in _act("POST", {"action": "list_galleries", "offset": 0, "limit": 200})["galleries"]
                 if x["name"] == "主题库")
    assert _trow["theme"] == "#bf0000", _trow
    _tp = g._galleries / "主题库" / _trow["cover"]
    assert ("主题库", _trow["cover"], _tp.stat().st_mtime) in g._theme_cache, g._theme_cache
    _again = _act("POST", {"action": "list_galleries", "offset": 0, "limit": 20, "q": "主题库"})
    assert _again["galleries"][0]["theme"] == "#bf0000", _again
    assert g.delete_gallery("主题库")
except ImportError:
    print("(无 PIL，跳过封面主题色取色检查)")

# 12e2) 封面主色落盘缓存（temp/themes.json）：重启后的冷进程不再重复解码封面
try:
    from PIL import Image as _PD

    # 隔离数据根：本用例只含一个图库，才能断言「一次解码都不该发生」
    _orig_data_dir = main.StarTools.get_data_dir
    _theme_root = _tmp / "主题磁盘根"
    _theme_root.mkdir(parents=True, exist_ok=True)
    main.StarTools.get_data_dir = lambda *a, **k: _theme_root
    try:
        _gbuf = io.BytesIO()
        _PD.new("RGB", (300, 200), (255, 0, 0)).save(_gbuf, "PNG")
        _g1 = main.GalleryPlus(_make_ctx(), _Cfg())
        _g1.save_upload("磁盘主题库", "red.png", _gbuf.getvalue())
        _pg1, _tot1, _st1 = _g1._list_gallery_page(0, 20, "")
        assert _pg1[0]["theme"] == "#bf0000", _pg1
        _themes_file = _theme_root / "temp" / "themes.json"
        assert _themes_file.is_file(), "列表算出的封面主色应落盘 temp/themes.json（temp/ 可清、不进导出）"
        _cover = _pg1[0]["cover"]
        _dkey = "%s\x1f%s\x1f%d" % (
            "磁盘主题库", _cover,
            int((_theme_root / "galleries" / "磁盘主题库" / _cover).stat().st_mtime),
        )
        _disk_data = json.loads(_themes_file.read_text("utf-8"))
        assert _disk_data.get(_dkey) == "#bf0000", (list(_disk_data), _dkey)
        # 新实例（同数据根、内存缓存全空）：主题色仍对，且一次封面解码都不该发生（全走磁盘缓存）
        _g2 = main.GalleryPlus(_make_ctx(), _Cfg())
        assert _g2._theme_cache == {}
        _calls = []
        _orig_tc = main.GalleryPlus.__dict__["_theme_color"]  # staticmethod 对象，恢复时原样放回
        try:
            def _tc_probe(im):
                _calls.append(1)
                return _orig_tc.__func__(im)

            main.GalleryPlus._theme_color = staticmethod(_tc_probe)
            _pg2, _tot2, _st2 = _g2._list_gallery_page(0, 20, "")
            _calls_hit = len(_calls)
            # 反证：删掉盘上缓存 + 清内存缓存后再列一次，探针必须数得到解码（证明计数本身有效）
            _themes_file.unlink()
            _g2._theme_cache.clear()
            _g2._theme_disk = None
            _g2._list_gallery_page(0, 20, "")
            _calls_miss = len(_calls)
        finally:
            main.GalleryPlus._theme_color = _orig_tc
        assert _pg2[0]["theme"] == "#bf0000", _pg2
        assert _calls_hit == 0, f"磁盘主题缓存应命中，不该再解码封面（实际解码 {_calls_hit} 次）"
        assert _calls_miss == 1, f"删掉盘上缓存后应重新解码一次（证明计数有效），实际 {_calls_miss}"
    finally:
        main.StarTools.get_data_dir = _orig_data_dir
except ImportError:
    print("(无 PIL，跳过封面主题色磁盘缓存检查)")

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
# 每行张数偏好：缺省 4，合法留存，垃圾忽略，越界收敛 3~8
res = _act("POST", {"action": "list_images", "name": "批量A", "offset": 0, "limit": 50})
assert res["cols"] == 4, res
_act("POST", {"action": "save_settings", "cols": 5})
assert _act("POST", {"action": "list_images", "name": "批量A", "offset": 0, "limit": 50})["cols"] == 5
_act("POST", {"action": "save_settings", "cols": "垃圾"})
assert _act("POST", {"action": "list_images", "name": "批量A", "offset": 0, "limit": 50})["cols"] == 5
_act("POST", {"action": "save_settings", "cols": 99})
assert _act("POST", {"action": "list_images", "name": "批量A", "offset": 0, "limit": 50})["cols"] == 8


# 12f2) 全库统计缓存：目录没变不重扫（主页角标加载快），任何数量变化（含手改目录）后立刻是新值
def _stats_deep_scans(fn):
    """fn 期间对「图库子目录」的 scandir 次数（统计缓存的扫盘成本 = 每库一次 scandir）。"""
    _orig = main.os.scandir
    _deep = []

    def _counted(path=".", *a, **k):
        try:
            _p = Path(path)
        except TypeError:  # os.scandir(fd)
            _p = None
        if _p is not None and _p != g._galleries and _p.parent == g._galleries:
            _deep.append(str(_p))
        return _orig(path, *a, **k)

    main.os.scandir = _counted
    try:
        fn()
    finally:
        main.os.scandir = _orig
    return len(_deep)


_base_stats = g._gallery_stats()
assert g._stats_cache is not None
assert _stats_deep_scans(lambda: g._gallery_stats()) == 0, "目录没变时统计不该再逐库 scandir"
_act("POST", {"action": "create", "name": "统计缓存库"})  # 建库：图库数 +1
assert g._gallery_stats() == {"galleries": _base_stats["galleries"] + 1,
                              "images": _base_stats["images"]}, g._gallery_stats()
_up("统计缓存库", "a.jpg")
_up("统计缓存库", "b.jpg")  # 上传 2 张：图片数 +2
assert g._gallery_stats() == {"galleries": _base_stats["galleries"] + 1,
                              "images": _base_stats["images"] + 2}, g._gallery_stats()
assert g.delete_image("统计缓存库", "统计缓存库-1.jpg")  # 删 1 张：-1
assert g._gallery_stats()["images"] == _base_stats["images"] + 1
# 绕过 API 直接往图库目录里写一张（手改场景）：目录 mtime 指纹必须让统计失效，不能返回旧值。
# 先睡过文件系统的时间戳刻度（Windows 文件时间分辨率有限，同一刻度内的两次改动 mtime 会相同），
# 否则测到的是「测时机的假阴性」而不是「缓存没失效」。
time.sleep(0.02)
(g._galleries / "统计缓存库" / "统计缓存库-9.jpg").write_bytes(b"x")
assert g._gallery_stats()["images"] == _base_stats["images"] + 2, "手改图库目录后统计不能是旧值"
assert g.delete_gallery("统计缓存库")
assert g._gallery_stats() == _base_stats, (g._gallery_stats(), _base_stats)

# 12g) 静默发送计数（图片/图库 + 时段）+ 悬停信息接口 + 移动迁统计
g.config["watermark"] = False
p_a1 = g._galleries / "批量B" / "批量B-4.jpg"
asyncio.run(g._send_image(p_a1))
asyncio.run(g._send_image(p_a1))
_row = g._stats["images"]["批量B/批量B-4.jpg"]
assert _row["sends"] == 2 and len(_row["hours"]) == 24 and sum(_row["hours"]) == 2, _row
assert g._stats["galleries"]["批量B"]["sends"] == 2, g._stats["galleries"]
asyncio.run(g._flush_stats())  # 防抖落盘：发送路径只标记脏位，读文件前显式冲一次
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
assert len(res["colors"]) == 16, "标签管理色板 = 16 个马卡龙色（随机一个的取值池）"
res = _act("POST", {"action": "save_tag", "name": "重点", "color": ""})  # 缺省色
_tid = res["id"]
assert _tid and res["tags"][_tid] == {"name": "重点", "color": main.TAG_COLORS[0]}, res
# 重名拒绝 / 非法色值拒绝 / 空名拒绝 / 改不存在的 id 404
assert _act("POST", {"action": "save_tag", "name": "重点", "color": main.TAG_COLORS[1]})["status"] == "error"
assert _act("POST", {"action": "save_tag", "name": "x", "color": "red"})["status"] == "error"
assert _act("POST", {"action": "save_tag", "name": "x", "color": "#12345"})["status"] == "error"
assert _act("POST", {"action": "save_tag", "name": "  ", "color": main.TAG_COLORS[1]})["status"] == "error"
assert _act("POST", {"action": "save_tag", "id": "没有的", "name": "x", "color": main.TAG_COLORS[1]})["status"] == "error"
# 任取十六进制色（取色器/自定义）：允许省略 #、大小写归一成小写 #rrggbb
res = _act("POST", {"action": "save_tag", "id": _tid, "name": "次要", "color": "548235"})
assert res["tags"][_tid] == {"name": "次要", "color": "#548235"}, res
# 改名换色
res = _act("POST", {"action": "save_tag", "id": _tid, "name": "次要", "color": main.TAG_COLORS[2].upper()})
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
_act("POST", {"action": "set_gallery_menu", "name": "改名A", "show": False, "char": "甲", "group": "功能",
              "y_start": "2023", "y_end": "2024"})
_tag_id = _act("POST", {"action": "save_tag", "name": "改名标签", "color": main.TAG_COLORS[6]})["id"]
assert _act("POST", {"action": "set_gallery_tag", "name": "改名A", "tag": _tag_id})["ok"]
res = _act("POST", {"action": "rename", "name": "改名A", "new_name": "改名B"})
assert res["ok"] and res["name"] == "改名B", res
assert not (g._galleries / "改名A").exists() and (g._galleries / "改名B").is_dir(), res
assert (g._galleries / "改名B" / "改名B-1.jpg").is_file(), "内部图片应同步改名"
assert g._pids.get("改名B/改名B-1.jpg") == "95026140_p5" and "改名A/改名A-1.jpg" not in g._pids
assert g._gallery_menu("改名B") == {"show": False, "char": "甲", "group": "功能",
                                    "y_start": "2023", "y_end": "2024", "y_month": False}  # 菜单键（含年份）随改名一起搬
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

# 12m2) 目录列举缓存（_gallery_files）：判据 = 图库目录 mtime_ns，API 与手改目录都立刻失效
_act("POST", {"action": "create", "name": "列举缓存库"})
for _n in (2, 10, 1):
    (g._galleries / "列举缓存库" / f"列举缓存库-{_n}.jpg").write_bytes(b"x")
_files0 = g._gallery_files("列举缓存库")
assert _files0 == ["列举缓存库-1.jpg", "列举缓存库-2.jpg", "列举缓存库-10.jpg"], _files0  # 编号数值序
# 第二次命中缓存：返回的还是同一份列表（没有重新 scandir + 排序）
assert g._gallery_files("列举缓存库") is _files0, "目录没变时应命中缓存（同一列表对象）"
# upload（走 API：显式作废缓存，不依赖目录 mtime）→ count 立刻 +1
_up("列举缓存库", "a.jpg")  # → 列举缓存库-11.jpg
assert g._gallery_files("列举缓存库")[-1] == "列举缓存库-11.jpg"
assert _act("POST", {"action": "list_images", "name": "列举缓存库", "offset": 0, "limit": 50})["total"] == 4
# 绕过 API 直接往目录里写一张：这次只能靠目录 mtime 失效。
# 先睡过文件系统的时间戳刻度（同一刻度内两次改动 mtime 相同，否则测到的是假阴性）
time.sleep(0.02)
(g._galleries / "列举缓存库" / "列举缓存库-99.jpg").write_bytes(b"x")
assert "列举缓存库-99.jpg" in g._gallery_files("列举缓存库"), "手改目录后列举缓存必须失效"
# delete_image（走 API：显式作废）→ count 立刻 -1
assert g.delete_image("列举缓存库", "列举缓存库-2.jpg")
assert "列举缓存库-2.jpg" not in g._gallery_files("列举缓存库")
# 缓存条目数超上限整体 clear（防无界增长）
_saved_dir_cache = g._dir_cache
try:
    g._dir_cache = {f"假库{i}": (0, []) for i in range(5000)}
    g._gallery_files("列举缓存库")
    assert len(g._dir_cache) == 1, len(g._dir_cache)  # 超 4096 → 全清后再写入本条
finally:
    g._dir_cache = _saved_dir_cache
assert g.delete_gallery("列举缓存库")

# 12m3) 图库名拼音排序缓存（_sorted_gallery_names）：判据 = galleries 目录 mtime_ns
_names0 = g._sorted_gallery_names()
assert _names0 == sorted(_names0, key=main.pinyin_key), _names0
assert g._sorted_gallery_names() is _names0, "目录没变时应命中缓存（同一列表对象）"
# create → 名字列表立刻有它，且整体仍是拼音序
_act("POST", {"action": "create", "name": "排甲缓存库"})
_names1 = g._sorted_gallery_names()
assert "排甲缓存库" in _names1 and _names1 == sorted(_names1, key=main.pinyin_key), _names1
# rename → 旧名消失、新名出现
assert _act("POST", {"action": "rename", "name": "排甲缓存库", "new_name": "排乙缓存库"})["ok"]
_names2 = g._sorted_gallery_names()
assert "排甲缓存库" not in _names2 and "排乙缓存库" in _names2, _names2
assert _names2 == sorted(_names2, key=main.pinyin_key), _names2
# delete → 名字立刻消失
assert g.delete_gallery("排乙缓存库")
assert "排乙缓存库" not in g._sorted_gallery_names()
# 绕过 API 直接 mkdir/rmdir（手改目录）：父目录 mtime 变，缓存同样失效。
# 每次手改前睡过一个时间戳刻度：同一刻度内的两次改动 mtime 相同，测到的是假阴性。
time.sleep(0.02)
(g._galleries / "手建缓存库").mkdir(exist_ok=True)
assert "手建缓存库" in g._sorted_gallery_names(), "手改 galleries 目录后名字缓存必须失效"
time.sleep(0.02)
(g._galleries / "手建缓存库").rmdir()
assert "手建缓存库" not in g._sorted_gallery_names()

# 12m4) 缩略图并发安全（thumb 进线程池后的回归防线）：同一 (kw, 图, 档位) 可能被多线程同时生成，
# 必须原子替换——否则可能读到半截 JPEG，且半成品 mtime 比源图新会被当成合法缓存永久命中。
try:
    from PIL import Image as _PTh
    import concurrent.futures

    _act("POST", {"action": "create", "name": "并发缩略图库"})
    _tbuf = io.BytesIO()
    _PTh.new("RGB", (1200, 900), (30, 120, 200)).save(_tbuf, "PNG")
    _tfile = g.save_upload("并发缩略图库", "big.png", _tbuf.getvalue()).name
    _tdir = g._tmp / "thumbs"
    _tout = _tdir / f"并发缩略图库_{Path(_tfile).stem}.640.thumb.jpg"
    for _stale in _tdir.glob(f"并发缩略图库_{Path(_tfile).stem}.*"):
        _stale.unlink(missing_ok=True)  # 清掉同档旧缓存，保证 4 个线程是真的在并发生成
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as _ex:
        _paths = list(_ex.map(lambda _i: g._thumb("并发缩略图库", _tfile, 640), range(4)))
    assert all(pt is not None and pt.is_file() for pt in _paths), _paths  # 4 个都成功
    assert all(pt == _tout for pt in _paths), _paths                       # 都指向同一份缓存
    _rim = _PTh.open(_tout)
    _rim.load()          # load() 会把像素解完：半截 JPEG 在这里会抛（证明不是半成品）
    assert max(_rim.size) == 640, _rim.size
    _rim.close()
    assert not list(_tdir.glob("*.tmp")), list(_tdir.glob("*.tmp"))  # 原子替换不残留临时文件
    # 失败分支：save 写了一半再抛 → 返回 None、不留下目标文件、不留 tmp（不能污染缓存）
    _tout.unlink()
    _orig_save = _PTh.Image.save
    _saved_args = []

    def _boom_save(self, fp, *a, **k):
        _saved_args.append(str(fp))
        Path(fp).write_bytes(b"half-written-jpeg")  # 模拟写到一半
        raise OSError("模拟写盘失败")

    _PTh.Image.save = _boom_save
    try:
        assert g._thumb("并发缩略图库", _tfile, 640) is None, "save 失败必须返回 None"
    finally:
        _PTh.Image.save = _orig_save
    assert _saved_args and _saved_args[0].endswith(".tmp"), _saved_args  # 先写临时名，没直接写最终路径
    assert not _tout.exists(), "save 失败不能留下半成品缩略图（否则会被当成合法缓存永久命中）"
    assert not list(_tdir.glob("*.tmp")), list(_tdir.glob("*.tmp"))  # 失败分支也清临时文件
    # 失败没污染缓存：下一次正常调用能重新生成，内容仍是合法 JPEG
    assert g._thumb("并发缩略图库", _tfile, 640) == _tout and _tout.is_file()
    _ok = _PTh.open(_tout)
    _ok.load()
    _ok.close()
    assert g.delete_gallery("并发缩略图库")
except ImportError:
    print("(无 PIL，跳过缩略图并发安全用例)")

# 12n) 主页搜索扩展到角色名/标签名 + create 一次带初始属性（0.3.0 同版，后端）
# q 命中角色名：char 与图库名没有公共子串，只有 char 里有「白花」
res = _act("POST", {"action": "create", "name": "角色搜索库", "char": "白花"})
assert res["ok"] and res["menu"]["char"] == "白花", res
_hit_char = _act("POST", {"action": "list_galleries", "offset": 0, "limit": 200, "q": "白花"})
assert [x["name"] for x in _hit_char["galleries"]] == ["角色搜索库"], _hit_char

# q 命中标签名：save_tag 建标签、set_gallery_tag 打标，name/char 都不含「搜索标签」；
# 顺带确认 total/stats 语义
_tid_search = _act("POST", {"action": "save_tag", "name": "搜索标签", "color": main.TAG_COLORS[3]})["id"]
_act("POST", {"action": "create", "name": "标签搜索库"})
assert _act("POST", {"action": "set_gallery_tag", "name": "标签搜索库", "tag": _tid_search})["ok"]
_now = _act("POST", {"action": "list_galleries", "offset": 0, "limit": 200})
_hit_tag = _act("POST", {"action": "list_galleries", "offset": 0, "limit": 200, "q": "搜索标签"})
assert [x["name"] for x in _hit_tag["galleries"]] == ["标签搜索库"], _hit_tag
assert _hit_tag["galleries"][0]["tag"]["id"] == _tid_search, _hit_tag
assert _hit_tag["total"] == 1, _hit_tag                        # total=过滤后的数量
assert _hit_tag["stats"] == _now["stats"], _hit_tag            # stats=全量统计，与 q 无关
assert _now["total"] == len(_now["galleries"])
# 标签 id 指向不存在的标签（手改脏数据）→ _tag_of 视为无标签，q 不命中
g._gallery_tags()["角色搜索库"] = "没有的标签id"
_hit_dirty = _act("POST", {"action": "list_galleries", "offset": 0, "limit": 200, "q": "搜索标签"})
assert [x["name"] for x in _hit_dirty["galleries"]] == ["标签搜索库"], _hit_dirty
g._gallery_tags().pop("角色搜索库", None)

# create 带全套初始属性：wm=False、角色名、不进菜单、标签、年份（y_start 故意传数字测 str 化）
_tid_full = _act("POST", {"action": "save_tag", "name": "初始标签", "color": main.TAG_COLORS[1]})["id"]
res = _act("POST", {"action": "create", "name": "全属性库", "wm": False, "char": "全属性角色",
                    "show": False, "tag": _tid_full, "y_start": 2024, "y_end": "2025"})
assert res["ok"] and res["name"] == "全属性库", res
assert res["existed"] is False, res  # 首次创建：同名库不存在
assert res["wm"] is False, res
assert res["tag"] == {"id": _tid_full, "name": "初始标签", "color": main.TAG_COLORS[1]}, res
assert res["menu"] == {"show": False, "char": "全属性角色", "group": "个人",
                       "y_start": "2024", "y_end": "2025", "y_month": False}, res
assert res["years"] == "2024-2025", res
_row = next(x for x in _act("POST", {"action": "list_galleries", "offset": 0, "limit": 200})["galleries"]
            if x["name"] == "全属性库")
assert _row["wm"] is False and _row["char"] == "全属性角色" and _row["years"] == "2024-2025", _row
assert _row["tag"] == {"id": _tid_full, "name": "初始标签", "color": main.TAG_COLORS[1]}, _row
_batch = next(r for r in _act("POST", {"action": "list_all_settings"})["galleries"] if r["name"] == "全属性库")
assert _batch["wm"] is False and _batch["tag"]["id"] == _tid_full, _batch
assert _batch["menu"]["show"] is False and _batch["menu"]["char"] == "全属性角色" \
       and _batch["menu"]["y_start"] == "2024" and _batch["menu"]["y_end"] == "2025", _batch

# 同名重复创建 = 旧版幂等 no-op：不能拿弹窗里的初始属性把现有库的水印/菜单/标签悄悄改掉
res = _act("POST", {"action": "create", "name": "全属性库", "wm": True, "char": "改名",
                    "show": True, "tag": "", "y_start": "", "y_end": ""})
assert res["ok"] and res["wm"] is False and res["menu"]["char"] == "全属性角色" \
       and res["menu"]["show"] is False and res["years"] == "2024-2025", res
assert res["existed"] is True, res  # 同名已存在：前端据此不重复插卡/加统计（仍是幂等 no-op）

# create 的 existed 字段：首次 False / 第二次 True，且第二次不改变 stats、不重写已有属性
_first = _act("POST", {"action": "create", "name": "存在标记库"})
assert _first["ok"] and _first["existed"] is False, _first
assert _first.keys() >= {"ok", "existed", "name", "wm", "tag", "menu", "years"}, _first  # 只增字段不改旧字段
_stats_before = g._gallery_stats()
_second = _act("POST", {"action": "create", "name": "存在标记库"})
assert _second["ok"] and _second["existed"] is True, _second
assert _second["name"] == _first["name"] == "存在标记库"
assert g._gallery_stats() == _stats_before, (g._gallery_stats(), _stats_before)  # 同名 no-op：统计不变
assert g.delete_gallery("存在标记库")

# create 不带属性：完全等于旧行为（不写设置），缺省值 = 开/无标签/角色名=图库名/无年份
res = _act("POST", {"action": "create", "name": "默认属性库"})
assert res["ok"] and res["wm"] is True and res["tag"] is None and res["years"] is None, res
assert res["menu"] == {"show": True, "char": "默认属性库", "group": "个人",
                       "y_start": "", "y_end": "", "y_month": False}, res
assert "默认属性库" not in (g._settings.get("gallery_wm") or {}), g._settings.get("gallery_wm")
assert "默认属性库" not in (g._settings.get("gallery_menu") or {}), g._settings.get("gallery_menu")
# 显式空串年份 = 未设置（合法，不报错）
res = _act("POST", {"action": "create", "name": "空年份库", "y_start": "", "y_end": ""})
assert res["ok"] and res["years"] is None and res["menu"]["y_start"] == res["menu"]["y_end"] == "", res

# 非法初始属性：四种都 error 且不得留下目录（年份/标签校验必须在 mkdir 之前）
_bad_creates = (
    ("非法标签库", {"tag": "没有的标签"}, 404, "标签不存在"),
    ("非法单边库", {"y_start": "2024"}, 400, "一起填"),
    ("非法月库", {"y_start": "2024-01", "y_end": "2025"}, 400, "年份格式"),
    ("非法区间库", {"y_start": "2025", "y_end": "2024"}, 400, "不能晚于"),
)
for _bn, _extra, _code, _msg in _bad_creates:
    _r = _act("POST", {"action": "create", "name": _bn, **_extra})
    assert _r["status"] == "error" and _r.get("status_code") == _code, (_bn, _r)
    assert _msg in _r["message"], (_bn, _r)
    assert not (g._galleries / _bn).is_dir(), f"非法请求不应留下半成品目录: {_bn}"

for _n in ("角色搜索库", "标签搜索库", "全属性库", "默认属性库", "空年份库"):
    assert g.delete_gallery(_n), _n
for _t in (_tid_search, _tid_full):
    assert _act("POST", {"action": "delete_tag", "id": _t})["ok"], _t

# 12n2) 触发词缓存失效（性能优化不许改变匹配语义）：改角色名/同义词、建库/删库/改名都要即时生效
_act("POST", {"action": "save_runtime_settings", "send_count": 5, "forward_threshold": 5,
              "max_send": 10, "synonyms": {"照片": ["图片"]}})  # 先固定一个可控的同义词表
_act("POST", {"action": "create", "name": "缓存库照片"})
(g._galleries / "缓存库照片" / "缓存库照片-1.jpg").write_bytes(b"x")
_act("POST", {"action": "set_gallery_menu", "name": "缓存库照片", "show": True,
              "char": "旧角色", "group": "个人"})
assert g._match_trigger("旧角色照片") == ["缓存库照片"]
assert g._trigger_cache and g._query_cache, "匹配一次后缓存里应有条目"
assert g._match_trigger("旧角色照片") == ["缓存库照片"]  # 第二次走缓存，结果必须一致
# 改角色名：settings 落盘 → 缓存清空；新角色名即时命中，旧的不再命中
_act("POST", {"action": "set_gallery_menu", "name": "缓存库照片", "show": True,
              "char": "新角色", "group": "个人"})
assert g._trigger_cache == {} and g._query_cache == {}, "settings 落盘后触发词缓存必须清空"
assert g._match_trigger("新角色照片") == ["缓存库照片"]
assert g._match_trigger("旧角色照片") == []
assert g._match_trigger("缓存库照片") == ["缓存库照片"]  # 关键词本身仍然命中
# 同义词改动即时生效（缓存键里的同义词快照跟着变，不会命中旧展开）
assert g._match_trigger("缓存库相片") == []
_act("POST", {"action": "save_runtime_settings", "send_count": 5, "forward_threshold": 5,
              "max_send": 10, "synonyms": {"照片": ["图片", "相片"]}})
assert g._match_trigger("缓存库相片") == ["缓存库照片"]
# 建库后新关键词立即可命中
_act("POST", {"action": "create", "name": "新建触发照片"})
assert g._match_trigger("新建触发照片") == ["新建触发照片"]
# 改名后新关键词命中、旧关键词不再命中（缓存必须清）
_act("POST", {"action": "create", "name": "改名触发照片"})
_act("POST", {"action": "set_gallery_menu", "name": "改名触发照片", "show": True,
              "char": "名号", "group": "个人"})
assert g._match_trigger("改名触发照片") == ["改名触发照片"]
assert _act("POST", {"action": "rename", "name": "改名触发照片",
                    "new_name": "改名触发二号照片"})["ok"]
assert g._trigger_cache == {}, "改名后触发词缓存必须清空"
assert g._match_trigger("改名触发二号照片") == ["改名触发二号照片"]
assert g._match_trigger("改名触发照片") == []  # 旧关键词不能继续命中
# 删库后旧关键词不再命中，缓存必须清
assert g.delete_gallery("缓存库照片")
assert g._trigger_cache == {}, "删库后触发词缓存必须清空"
assert g._match_trigger("新角色照片") == []
assert g.delete_gallery("新建触发照片") and g.delete_gallery("改名触发二号照片")
_act("POST", {"action": "save_runtime_settings", "send_count": 5, "forward_threshold": 5,
              "max_send": 10, "synonyms": {"照片": ["图片", "相片"]}})  # 还原运行设置

# 12x) 选择模式重构（0.3.0 同版）：list_names + 移动/复制后台任务（进度/暂停/取消回退）+ 磁盘实时监控


def _disk_ok(v):  # 磁盘监控字段宽松判据：None（拿不到）或非负实数（0.0 是空闲时的合法读数）
    return v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 and v == v)


def _disk_contract(d, tag=""):
    assert isinstance(d, dict) and set(d) == {"usage", "r_bps", "w_bps", "resp_ms"}, (tag, d)
    assert d["usage"] is None or (isinstance(d["usage"], (int, float)) and 0 <= d["usage"] <= 100), (tag, d)
    assert all(_disk_ok(d[k]) for k in ("r_bps", "w_bps", "resp_ms")), (tag, d)


_act("POST", {"action": "create", "name": "任务源"})
_act("POST", {"action": "create", "name": "任务目标"})
for _n in (1, 2, 3):
    (g._galleries / "任务源" / f"任务源-{_n}.jpg").write_bytes(b"x" * (100 * _n))
g._pids["任务源/任务源-1.jpg"] = "111_p0"
g._save_pids()

# list_names：全量文件名按编号数值排序（「全选」的数据源）；非法图库 404
res = _act("POST", {"action": "list_names", "name": "任务源"})
assert res["names"] == ["任务源-1.jpg", "任务源-2.jpg", "任务源-3.jpg"], res
assert _act("POST", {"action": "list_names", "name": "不存在的库"})["status"] == "error"

def _xfer_wait():
    st = {"state": "running"}
    for _ in range(300):
        st = _act("POST", {"action": "xfer_status"})
        if st["state"] in ("done", "cancelled", "error"):
            return st
        time.sleep(0.02)
    return st

# 复制任务：目标库按接号改名、源保留、pid 跟走；使用率是 0~100 的实数、累计字节/平均响应可用
res = _act("POST", {"action": "xfer_start", "op": "copy", "src": "任务源", "dst": "任务目标",
                    "images": ["任务源-1.jpg", "任务源-2.jpg"]})
assert res["ok"] and res["total"] == 2, res
st = _xfer_wait()
assert st["state"] == "done" and st["done"] == 2 and st["failed"] == [], st
assert (g._galleries / "任务目标" / "任务目标-1.jpg").is_file() and (g._galleries / "任务目标" / "任务目标-2.jpg").is_file()
assert (g._galleries / "任务源" / "任务源-1.jpg").is_file(), "复制应保留源文件"
assert g._pids.get("任务目标/任务目标-1.jpg") == "111_p0", g._pids
assert st["ops"] == 2 and st["bytes_r"] > 0 and st["bytes_w"] > 0 and st["op_ms"] >= 0, st
assert st["usage"] is None or 0 <= st["usage"] <= 100, st
# xfer_status 必须带 disk（任务弹窗四格实时数据的唯一来源），旧键（usage/bytes_*/ops/op_ms）原样保留
assert "disk" in st, st
_disk_contract(st["disk"], "xfer_status.disk")

# 移动任务 + 取消：一次性选 603 张（rename 很快，靠数量保证暂停/取消落在任务中间），
# 回退后目标库 = 任务开始前的快照（比暂停时再拍的还干净——暂停前移过去的也要滚回来）
for _n in range(4, 604):
    (g._galleries / "任务源" / f"任务源-{_n}.jpg").write_bytes(b"x")
_pre = sorted(p.name for p in (g._galleries / "任务目标").iterdir())  # 任务开始前（=上一步复制任务的产物）
_names = [f"任务源-{_n}.jpg" for _n in range(1, 604)]
res = _act("POST", {"action": "xfer_start", "op": "move", "src": "任务源", "dst": "任务目标", "images": _names})
assert res["ok"] and res["total"] == 603, res
_ok_pause = False
for _ in range(200):
    r = _act("POST", {"action": "xfer_pause", "value": True})
    if r.get("ok"):
        _ok_pause = True
        break
    if _act("POST", {"action": "xfer_status"})["state"] in ("done", "cancelled", "error"):
        break
    time.sleep(0.005)
assert _ok_pause, "600 张 rename 期间暂停应成功（若失败说明机器快到任务已完成，加大数量重试）"
_act("POST", {"action": "xfer_cancel"})
st = _xfer_wait()
assert st["state"] == "cancelled", st
assert st["rb_total"] == st["rb_done"], st  # 回退必须走完全部已完成项
_left = sorted(p.name for p in (g._galleries / "任务目标").iterdir())
assert _left == _pre, (_left, _pre)  # 取消后目标库 = 任务开始前的样子，本次移动的文件一张不留
for _n in range(1, 604):
    assert (g._galleries / "任务源" / f"任务源-{_n}.jpg").is_file(), _n  # 源库全部还原
assert g._pids.get("任务源/任务源-1.jpg") == "111_p0", g._pids  # pid 键跟着搬回

# 边界：源=目标 / 空名单 / 非法 op / 不存在的图库 → 全部拒绝且不开任务
for _bad in (
    {"op": "copy", "src": "任务源", "dst": "任务源", "images": ["任务源-1.jpg"]},
    {"op": "copy", "src": "任务源", "dst": "任务目标", "images": []},
    {"op": "rename", "src": "任务源", "dst": "任务目标", "images": ["任务源-1.jpg"]},
    {"op": "copy", "src": "没有的库", "dst": "任务目标", "images": ["x.jpg"]},
):
    r = _act("POST", {"action": "xfer_start", **_bad})
    assert r["status"] == "error" and _act("POST", {"action": "xfer_status"})["state"] != "running", (_bad, r)

# 模块级实时磁盘监控 disk_monitor：四键契约、类型宽松（None 或非负实数）、不抛异常、Windows 实测有值。
# usage = 磁盘活动时间占比（不是空间占用），由 PDH % Disk Time / Linux diskstats 忙碌时间算。
_d1 = main.disk_monitor()
_disk_contract(_d1, "disk_monitor#1")
assert main._DISK_IO_GAP >= 1.0, main._DISK_IO_GAP
# 连续第二次采样：Windows PDH 需两个样本才出速率（第一次 None 是契约允许的，不依赖瞬时 IO）
time.sleep(0.15)
_d2 = main.disk_monitor()
_disk_contract(_d2, "disk_monitor#2")
if sys.platform.startswith("win"):
    assert any(_d2[k] is not None for k in ("usage", "r_bps", "w_bps", "resp_ms")), \
        f"Windows 本机 PDH 采样应给出实时活动时间/读写/响应之一（空闲时 0.0 也算有效）: {_d2}"

assert g.delete_gallery("任务源") and g.delete_gallery("任务目标")

# 12y) 导出全部数据：后台线程打包 ZIP（含图库图片、排除 temp/）+ 状态轮询 + 分块拼回字节
_export_fields = {"state", "name", "size", "path", "error"}
res = _act("POST", {"action": "export_status"})
assert res["state"] == "idle" and set(res) == _export_fields, res
# idle 时 export_cancel：原样返回快照，不改状态/不报错（也没有 zip 可删）
res = _act("POST", {"action": "export_cancel"})
assert res["state"] == "idle" and set(res) == _export_fields, res
# 未打包完成前不能取块
assert _act("POST", {"action": "export_chunk", "offset": 0, "size": 1024})["status"] == "error"
res = _act("POST", {"action": "export_data"})
# 数据量小时打包线程可能在返回前就结束（state 直接 done），两种都合法；running 才是常态
assert res["state"] in ("running", "done") and set(res) == _export_fields, res
assert res["name"].startswith("图库plus_数据导出_") and res["name"].endswith(".zip"), res
_zip_path = Path(res["path"])
assert _zip_path.parent == g._tmp, res  # 导出文件落在 temp/（数据根目录下，被自身排除）


def _export_wait():
    st = {"state": "running"}
    for _ in range(500):
        st = _act("POST", {"action": "export_status"})
        if st["state"] in ("done", "error"):
            return st
        time.sleep(0.02)
    return st


st = res if res["state"] == "done" else _export_wait()
assert st["state"] == "done" and st["error"] == "", st
assert st["name"] == res["name"] and st["path"] == res["path"], (st, res)
assert _zip_path.is_file() and _zip_path.stat().st_size == st["size"] > 0, st
with zipfile.ZipFile(_zip_path) as _zf:
    _names = _zf.namelist()
    assert _zf.testzip() is None, "导出 ZIP 完整性校验失败"
assert "settings.json" in _names, _names[:20]                    # 设置/统计/pids 都在根
assert "pids.json" in _names and "stats.json" in _names, _names[:20]
assert "galleries/" in _names and any(
    n.startswith("galleries/") and n != "galleries/" for n in _names), _names[:20]  # 含图库图片
assert not [n for n in _names if n == "temp" or n.startswith("temp/")], "导出不得包含 temp/"
assert not [n for n in _names if "thumbs" in n], "导出不得包含缩略图缓存"
# done 时 export_cancel：状态/大小/路径不变，已完成的 zip 不能被删（下载阶段取消只由前端停）
_r = _act("POST", {"action": "export_cancel"})
assert _r["state"] == "done" and _r["size"] == st["size"] and _r["path"] == st["path"], _r
assert _zip_path.is_file(), "done 态取消不得删已完成 zip"
# 分块读取：分 3 段拼回整个 ZIP；每块 {b64,n,size}，size=文件总字节
_parts, _off, _chunk = [], 0, max(1, st["size"] // 3)
while _off < st["size"]:
    _r = _act("POST", {"action": "export_chunk", "offset": _off, "size": _chunk})
    assert set(_r) == {"b64", "n", "size"}, _r
    assert _r["size"] == st["size"], _r
    _data = base64.b64decode(_r["b64"])
    assert _r["n"] == len(_data) and 1 <= _r["n"] <= _chunk, _r
    _parts.append(_data)
    _off += _r["n"]
assert _off == st["size"] and b"".join(_parts) == _zip_path.read_bytes(), "分块拼回必须等于原 ZIP"
# 精确边界：每块只取 5 字节，两块的字节拼接 = 文件头 10 字节（offset 是真按字节切的）
_head = b""
for _hoff in (0, 5):
    _r = _act("POST", {"action": "export_chunk", "offset": _hoff, "size": 5})
    assert _r["n"] == 5 and _r["size"] == st["size"], _r
    _head += base64.b64decode(_r["b64"])
assert _head == _zip_path.read_bytes()[:10], "offset/size 必须精确到字节"
# 分块 size 上界 clamp 到 8MB（小文件一次读完）；offset 越界回空块 n=0（不算异常）
_r = _act("POST", {"action": "export_chunk", "offset": 0, "size": 10 ** 9})
assert _r["n"] == st["size"] and _r["size"] == st["size"], _r
_r = _act("POST", {"action": "export_chunk", "offset": st["size"], "size": 10})
assert _r["n"] == 0 and _r["b64"] == "" and _r["size"] == st["size"], _r
# 文件被删/丢失 → 4xx（state 仍是 done，前端提示重新导出）
_zip_path.unlink()
assert _act("POST", {"action": "export_chunk", "offset": 0, "size": 10})["status"] == "error"

# 12z) 导出取消（0.3.0 同版）：worker 遍历中响应取消 → state=cancelled、删半成品、绝不落 done
# 12z-1 确定性取消：模拟「打包中」状态（path=temp 下真实半成品），取消旗已置，直接跑 worker（不等线程调度）
_cancel_target = g._tmp / "图库plus_数据导出_取消用例.zip"
_cancel_target.parent.mkdir(parents=True, exist_ok=True)
_cancel_target.write_bytes(b"PK-not-a-real-partial-zip")
with g._export_lock:
    g._export = {"state": "running", "name": _cancel_target.name, "size": 0,
                 "path": str(_cancel_target), "error": ""}
g._export_cancel.set()
g._export_worker(_cancel_target)
assert g._export["state"] == "cancelled", g._export
assert g._export["error"] == "" and g._export["size"] == 0, g._export
assert not _cancel_target.exists(), "取消后必须删半成品 zip"
_r = _act("POST", {"action": "export_status"})
assert _r["state"] == "cancelled" and _r["size"] == 0 and _r["error"] == "", _r
assert _act("POST", {"action": "export_chunk", "offset": 0, "size": 10})["status"] == "error"
# cancelled 态重复取消：幂等，不报错、状态不变
_r = _act("POST", {"action": "export_cancel"})
assert _r["state"] == "cancelled", _r


# 12z-2 worker 中途取消：让取消旗在第 N 次检查后才置位，证明是逐项检查（不是只看开头一次）
class _LateCancel:
    def __init__(self, skip):
        self.skip, self.calls = skip, 0

    def is_set(self):
        self.calls += 1
        return self.calls > self.skip

    def set(self):
        pass

    def clear(self):
        self.calls = 0


_late_target = g._tmp / "图库plus_数据导出_中途取消用例.zip"
_real_cancel = g._export_cancel
g._export = {"state": "running", "name": _late_target.name, "size": 0,
             "path": str(_late_target), "error": ""}
g._export_cancel = _LateCancel(10)
try:
    g._export_worker(_late_target)
    assert g._export["state"] == "cancelled", g._export
    assert g._export_cancel.calls > 10, ("worker 只检查了一次取消旗", g._export_cancel.calls)
    assert not _late_target.exists(), "中途取消也要删半成品 zip"
finally:
    g._export_cancel = _real_cancel
# 12z-3 取消后能重新导出：_export_start 先 clear 取消旗，cancelled → running → done（真实 zip 可用）
assert g._export_cancel.is_set(), "上一轮取消旗应仍是置位状态，用来验证 export_data 会 clear"
_r = _act("POST", {"action": "export_data"})
assert _r["state"] in ("running", "done"), _r
assert not g._export_cancel.is_set(), "export_data 必须先 clear 上一轮的取消旗"
st = _r if _r["state"] == "done" else _export_wait()
assert st["state"] == "done" and st["error"] == "", st
_reexport = Path(st["path"])
assert _reexport.is_file() and _reexport.stat().st_size == st["size"] > 0, st
assert _act("POST", {"action": "export_chunk", "offset": 0, "size": 10})["n"] == 10, "重新导出后可正常下载"

shutil.rmtree(_tmp)
print("selftest OK")
