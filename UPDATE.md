# 更新日志（全量）

> 最新版本详情见 [CHANGELOG.md](CHANGELOG.md)，本文件完整保留所有版本的更新记录。

## 0.1.0（2026-10-04）

首个版本。基于 [astrbot_plugin_image_replay](https://github.com/cumany/astrbot_plugin_image_replay) v1.1.0 改造。

### 新增

- **关键词图库**：一个关键词一个文件夹（`galleries/<关键词>/`），消息首词命中即随机回复该文件夹内一张图片；不再依赖单一 resource 文件夹按文件名前缀区分
- **上传命名与 pid 记录**：
  - 上传时检查原始文件名，纯数字或 `数字_p数字`（pixiv 命名，如 `95026140_p0.jpg`）记为 pid
  - pid 与文件的对应关系存入 `pids.json`
  - 入库后自动重命名为 `关键词-编号.扩展名`（如 `可琳照片-1.png`），编号为文件夹内现有最大编号 + 1
- **发送水印**：仅在发送环节在图片下方临时拼接文件名/PID 信息条（不修改原图；GIF 原样发送；可配置关闭）
- **WebUI 管理页**（AstrBot 仪表盘内插件 Pages，`pages/manager`）：
  - 创建/删除关键词（图库文件夹）
  - 上传图片到机器人服务器（多选），自动应用命名与 pid 记录
  - 缩略图预览、删除单张图片或整个图库
  - **批量导入**：按搭积木式文件名格式（`{图库名}`/`{编号}`/`{pid}`/`{扩展名}` 加自定义分隔符）一次导入大量图片，自动分库、记录 pid、编号续接；不符合格式的文件逐条列出跳过原因，不做猜测
  - 后端为单端点（`/{插件名}/galleries`，GET 列表 / POST action 分发），兼容新版 `astrbot.api.web` 与旧构建 Quart 上下文，鉴权走仪表盘登录会话
- **规范存储**：全部数据存放于 `data/plugin_data/astrbot_plugin_image_reply_plus/`
- **自检脚本** `selftest.py`：无需 AstrBot 环境即可验证 pid 识别、入库重命名、Pages API 注册、防路径穿越、删除逻辑与水印渲染

### 移除（相对原插件）

- 聊天端 `收集/查看图片/删除图片指令` 命令与相关图片下载逻辑（上传统一走 WebUI）
- 指令→前缀映射配置 `commands`（关键词即文件夹名，无需映射）
- 群/用户黑白名单、多图回复模式（`random`/`all`，现固定随机一张）

### 配置项

`require_wake`（默认 false）、`watermark`（默认 true）
