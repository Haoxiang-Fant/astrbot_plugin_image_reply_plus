# 更新日志（最新版本）

## 0.1.0（2026-10-04）

首个版本。基于 [astrbot_plugin_image_replay](https://github.com/cumany/astrbot_plugin_image_replay) v1.1.0 改造。

### 新增

- **关键词图库**：一个关键词一个文件夹（`galleries/<关键词>/`），消息首词命中即随机回复一张；不再按文件名前缀区分
- **上传命名与 pid 记录**：原始文件名为纯数字或 `数字_p数字` 时记为 pid 并写入 `pids.json`；入库自动重命名为 `关键词-编号.扩展名`（如 `可琳照片-1.png`）
- **发送水印**：仅发送环节在图片下方临时拼接 文件名/PID 信息条（不改原图，GIF 原样发送，可配置关闭）
- **WebUI 管理页**：AstrBot 仪表盘内插件 Pages（插件详情 → `manager` 页），管理关键词与图库、上传图片、缩略图预览与删除；鉴权走仪表盘会话。后端为单端点 + action 协议，兼容新版 `astrbot.api.web` 与旧构建（Quart 垫片）；插件路由前缀按运行时元数据自动适配（注册名/显示名/目录名全部注册）
- **批量导入**：按搭积木式文件名格式（`{图库名}` `{编号}` `{pid}` `{扩展名}` + 自定义分隔符）一次导入大量图片，自动分库、记录 pid、编号续接；不符合格式的文件逐条给出跳过原因，不做猜测
- **规范存储**：数据全部位于 `data/plugin_data/astrbot_plugin_image_reply_plus/`
- **自检**：`python selftest.py` 无需 AstrBot 环境即可验证核心逻辑

### 移除（相对原插件）

- 聊天端 收集/查看/删除 命令与图片下载逻辑（上传统一走 WebUI）
- `commands` 指令映射、群/用户黑白名单、多图回复模式

### 配置项

`require_wake`(false)、`watermark`(true)

> 历史版本见 [UPDATE.md](UPDATE.md)。
