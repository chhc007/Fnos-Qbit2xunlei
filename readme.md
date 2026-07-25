# qBit → 迅雷 自动转存（飞牛 fnOS）

自动将 qBittorrent 中的任务转存到飞牛 NAS 上的迅雷下载，比速后保留更快的那一个。

## 工作原理

```
qBit 任务带"迅雷"标签
        ↓
  预检查：等 20s 看 qBit 速度
        ↓
  ┌─ qBit 速度快（≥2MB/s）→ 移除标签，不转存
  └─ qBit 速度慢 → 提交到迅雷下载
                      ↓
                  等待 10s 检查状态
                      ↓
                  ┌─ 失败/版权问题 → 标记"迅雷失败"
                  ├─ 秒下完成 → 删除 qBit 任务
                  └─ 开始下载 → 比速 40s
                                ├─ 迅雷更快 → 删除 qBit
                                └─ qBit 更快 → 删除迅雷任务
```

同时支持 0 速度超时自动清理、迅雷端文件过滤（只保留视频/字幕/nfo）。

## 快速开始

### 1. 准备配置文件

在 NAS 上创建一个目录用于存放配置，例如 `/vol4/1000/DockerConfig/fnosqbit2xunlei/config`。

首次运行容器会自动生成 `config.ini`，也可以提前准备好。填写 qBit 和 NAS 的地址、账号密码即可。

### 2. Docker Compose 部署

```yaml
services:
  fnosqbit2xunlei:
    image: chhc007/fnos-qbit2xunlei:latest
    container_name: fnosqbit2xunlei
    restart: unless-stopped
    environment:
      - TZ=Asia/Shanghai
    volumes:
      - /vol4/1000/DockerConfig/fnosqbit2xunlei/config:/app/config
```

### 3. 使用

在 qBittorrent 中给任务添加 **`迅雷`** 标签，脚本会自动处理。

## 配置说明

### 连接配置

| 配置项 | 说明 | 示例 |
|--------|------|------|
| `QB_HOST` | qBit Web UI 地址 | `http://192.168.1.100:8080` |
| `QB_USER` | qBit 用户名 | `admin` |
| `QB_PASS` | qBit 密码 | `adminadmin` |
| `NAS_HOST` | 飞牛 NAS 地址 | `192.168.1.100` |
| `NAS_PORT` | 飞牛 NAS 端口 | `5666` |
| `NAS_USER` | NAS 登录账号 | `admin` |
| `NAS_PASS` | NAS 登录密码 | `password` |

### 迅雷与路径

| 配置项 | 说明 |
|--------|------|
| `XUNLEI_DOWNLOAD_PATH` | 迅雷下载路径（留空用默认） |
| `QBIT_SAVE_PATH_PREFIX` | qBit 保存路径前缀（用于路径映射） |
| `XUNLEI_BASE_PATH` | 映射到迅雷的基础路径 | 

路径映射示例：

```
qBit 保存路径: /downloads/电影/动画电影
QBIT_SAVE_PATH_PREFIX = /downloads
XUNLEI_BASE_PATH = /存储空间5/.../迅雷下载影视
→ 迅雷下载路径: /存储空间5/.../迅雷下载影视/电影/动画电影 （这里用迅雷看到的目录名称而不是文件夹真实路径）
```

### 预检查（推荐）

| 配置项 | 说明 | 推荐值 |
|--------|------|--------|
| `PRE_CHECK_WAIT` | 观察 qBit 速度的等待时间（秒） | `20` |
| `PRE_CHECK_SPEED_THRESHOLD` | qBit 速度阈值（KB/s），高于此值不转存 | `2000` |

先看 qBit 速度，如果 qBit 已经够快（如 2MB/s 以上），直接跳过不转迅雷。

### 比速参数

| 配置项 | 说明 | 推荐值 |
|--------|------|--------|
| `SPEED_CHECK_DURATION` | 比速观察时长（秒） | `40` |
| `SPEED_CHECK_INTERVAL` | 采样间隔（秒） | `5` |
| `INITIAL_WAIT` | 提交后等待迅雷开始（秒） | `10` |
| `MIN_SPEED_BYTES` | 最低有效速度（bytes/s） | `50` |

### 其他

| 配置项 | 说明 | 推荐值 |
|--------|------|--------|
| `TARGET_LABEL` | 触发转存的 qBit 标签 | `迅雷` |
| `CHECK_INTERVAL` | 主循环检查间隔（秒） | `10` |
| `DELETE_FILES` | 删除 qBit 任务时是否同时删文件 | `true` |
| `FILTER_FILES` | 迅雷端过滤非视频/字幕/nfo 文件 | `true` |
| `ZERO_SPEED_ENABLED` | 0 速度超时自动清理 | `false` |
| `ZERO_SPEED_TIMEOUT` | 0 速度超时时间（分钟） | `120` |
| `DEBUG` | 调试模式，打印所有 API 请求/响应详情 | `false` |

## 技术细节

- **NAS 登录**: WebSocket (`ws://<nas>:5666/websocket?type=main`)，明文 `user.login`
- **迅雷操作**: 通过 Playwright 无头浏览器直接操作迅雷 Web 界面（SPA 页面），不依赖迅雷 API 创建任务
- **迅雷 API**: 仅用于查询任务状态（`list_tasks`），创建任务由 Playwright 完成
- **比速逻辑**: 同时采样 qBit 和迅雷速度，比较平均速度决定保留哪个

## 本地运行

```bash
pip install requests websockets playwright
playwright install chromium
cp config/config.ini.example config/config.ini
# 编辑 config.ini
python3 qbit_to_xunlei.py
```
