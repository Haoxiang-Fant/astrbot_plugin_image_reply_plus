# 图库plus (astrbot_plugin_image_reply_plus)

关键词图库插件：**一个关键词一个文件夹**，命中关键词即随机回复该文件夹里的一张图片。内置 AstrBot 仪表盘 WebUI 管理页（插件 Pages），支持上传、pixiv pid 记录、发送时附加文件名/PID 信息条。

> 本插件基于 [astrbot_plugin_image_replay](https://github.com/cumany/astrbot_plugin_image_replay)（作者 cuman）改造而来，感谢原插件。
> 主要改动：图库按"一关键词一文件夹"组织、上传自动重命名与 pid 记录、AstrBot 仪表盘内 WebUI 管理页、发送时水印信息条、数据严格存放在 `data/plugin_data/`。

## 功能特点

- 关键词响应图片：消息首词命中关键词（即 `galleries/` 下的文件夹名），随机发送一张
- 一个关键词一个文件夹，图库物理隔离，增删图片无需改配置
- 上传时自动识别 pixiv 命名并记录 pid，文件统一重命名为 `关键词-编号.扩展名`（如 `可琳照片-1.png`）
- 发送时在图片下方附加 文件名/PID 信息条（水印），仅发送环节生成，不修改原图
- WebUI：AstrBot 仪表盘内的插件 Pages 管理页，管理关键词与图库、上传图片到机器人服务器
- 数据按 AstrBot 规范存放于 `data/plugin_data/astrbot_plugin_image_reply_plus/`

## 安装

1. 将本插件文件夹放入 AstrBot 的 `data/plugins/` 目录（或通过插件市场安装）
2. 重启 AstrBot，插件自动创建数据目录
3. 依赖：`pillow`（见 `requirements.txt`）
4. WebUI 管理页需要支持**插件 Pages** 的较新版 AstrBot；过旧版本会在日志中提示 WebUI 不可用，关键词回复不受影响

## 使用

### 聊天端

```
可琳照片          # 首词命中关键词，随机回复一张
可琳照片 来一张    # 同样生效（取第一个词匹配）
```

### WebUI（AstrBot 仪表盘内）

打开 AstrBot WebUI → 插件页 → 图库plus 插件详情 → 打开 Pages 管理页（`pages/manager`）：

- 创建/删除关键词（图库文件夹）
- 向指定关键词上传图片（多选，支持直接用 pixiv 原始文件名如 `95026140_p0.jpg`）
- 缩略图预览、删除单张图片或整个图库
- **批量导入**：按自定义文件名格式一次导入大量图片，自动分库

鉴权走 AstrBot 仪表盘登录会话，无需额外配置端口。

### 批量导入（搭积木式文件名格式）

管理页右上角「批量导入」按钮 → 用积木按钮拼出你的文件名格式 → 选一堆图片 → 一键导入，自动分到各关键词图库。

可用积木（点按钮插入，分隔符自己敲）：

| 积木 | 含义 |
| --- | --- |
| `{图库名}` | 归到哪个图库（**必填**），对应文件夹名 |
| `{编号}` | 数字序号，仅用于匹配文件名（入库时按图库重新编号） |
| `{pid}` | pixiv 作品号（`95026140` / `95026140_p0`），会记录到 `pids.json` |
| `{扩展名}` | 文件后缀（也可写 `{格式}`） |

示例：

```
格式：{图库名}-{编号}.{扩展名}
文件：可琳照片-1.jpg、可琳照片-2.jpg、安比照片-1.png
结果：可琳照片/ 下 2 张，安比照片/ 下 1 张（编号自动续接该图库已有编号）

格式：{图库名}_{pid}.{扩展名}
文件：可琳照片_95026140_p0.jpg
结果：可琳照片/ 下 1 张，并记录 PID:95026140_p0
```

不符合格式的文件**不会被猜测**，会在导入结果里逐条列出跳过原因（格式不符 / 不支持的图片格式 / 内容解码失败等）。

导入完成后，导入界面下方会显示**导入成果**：按图库分组、每张带缩略图与文件名/PID（每库最多预览 60 张），并附跳过明细。导入过程中实时显示 **导入速度与预计剩余时间**，成果面板里也会给出总耗时与平均速度（张/秒）。

## 图片命名与 pid 规则

上传时检查原始文件名：

- 纯数字（如 `95026140.jpg`）或 `数字_p数字`（如 `95026140_p0.jpg`）→ 记为 pid
- pid 记录在 `data/plugin_data/astrbot_plugin_image_reply_plus/pids.json`，键为 `关键词/文件名`
- 文件入库后统一重命名为 `关键词-编号.扩展名`，编号为该关键词文件夹内现有最大编号 + 1

## 水印说明

- 水印**只在发送环节**临时生成（不修改原图、不改变图库文件）：图片下方拼接一条深色信息条，内容为文件名和 pid（如有）
- GIF 动图直接原样发送，避免水印处理只剩第一帧
- 配置 `watermark: false` 可整体关闭

## 目录结构

```
astrbot_plugin_image_reply_plus/          # 插件目录
├── main.py                               # 插件逻辑 + Pages 后端 API
├── pages/manager/index.html              # 仪表盘内 WebUI 管理页
└── ...
data/plugin_data/astrbot_plugin_image_reply_plus/
├── galleries/            # 图库根目录，一个关键词一个文件夹
│   └── 可琳照片/
│       ├── 可琳照片-1.png
│       └── 可琳照片-2.jpg
├── pids.json             # pid 映射 {"关键词/文件名": "pid"}
└── temp/                 # 发送时水印临时文件（可随时清空）
```

## 配置项

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `require_wake` | `false` | 开启后需 @机器人/唤醒前缀 才响应，避免误触发 |
| `watermark` | `true` | 发送时附加 文件名/PID 信息条 |

## 开发与自检

```
python selftest.py    # 无需 AstrBot 环境，覆盖 pid 识别/入库/Pages API 注册/防穿越/删除/水印
```

## 更新日志

见 [UPDATE.md](UPDATE.md)（全量）与 [CHANGELOG.md](CHANGELOG.md)（最新版本）。

## 开源地址

- 本插件改造自：https://github.com/cumany/astrbot_plugin_image_replay
