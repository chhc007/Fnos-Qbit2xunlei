# 适用于飞牛的qBit → 迅雷 自动转存

自动将 qBittorrent 中的任务转存到迅雷下载，比速后保留更快的那一个。

## 工作原理

```
qBit 任务带"迅雷"标签
        ↓
  提交到迅雷下载
        ↓
  等待 10s 检查状态
        ↓
  ┌─ 失败 → 标记"迅雷失败"，保留 qBit
  ├─ 秒下完成 → 删除 qBit
  └─ 开始下载 → 比速 60s
                  ├─ 迅雷更快 → 删除 qBit
                  └─ qBit 更快 → 删除迅雷任务
```

同时监控所有下载任务（qBit + 迅雷），0 速度超过指定时间自动清理。

## 快速开始

### 1. 准备配置文件

```bash
cp config/config.ini.example config/config.ini
vim config/config.ini
```

填写：
- qBit Web UI 地址、账号密码
- NAS 地址、账号密码

### 2. Docker 部署

```bash
docker compose up -d --build
```

### 3. 使用

在 qBittorrent 中给任务添加 **`迅雷`** 标签，脚本会自动：
1. 提取磁力链接（含 tracker）
2. 提交到迅雷下载
3. 比速决定保留哪个

## 配置说明

### qBit

| 配置项 | 说明 | 示例 |
|--------|------|------|
| `QB_HOST` | Web UI 地址 | `http://192.168.1.100:8080` |
| `QB_USER` | 用户名 | `admin` |
| `QB_PASS` | 密码 | `adminadmin` |

### NAS / 迅雷

| 配置项 | 说明 | 示例 |
|--------|------|------|
| `NAS_HOST` | NAS 地址 | `192.168.123.146` |
| `NAS_PORT` | NAS 端口 | `5666` |
| `NAS_USER` | NAS 账号 | `admin` |
| `NAS_PASS` | NAS 密码 | `password` |
| `XUNLEI_DOWNLOAD_PATH` | 迅雷固定下载路径（留空用默认） | `/vol5/1000/影视库/下载` |

### 路径映射

将 qBit 的保存路径映射到迅雷的下载目录：

```ini
QBIT_SAVE_PATH_PREFIX = /downloads
XUNLEI_BASE_PATH = /vol5/1000/影视库/下载/迅雷下载影视
```

| qBit 路径 | 迅雷路径 |
|-----------|---------|
| `/downloads/电影/华语电影` | `/vol5/1000/影视库/下载/迅雷下载影视/电影/华语电影` |
| `/downloads/电视剧` | `/vol5/1000/影视库/下载/迅雷下载影视/电视剧` |

留空则不映射，直接用 `XUNLEI_DOWNLOAD_PATH` 或迅雷默认路径。

### 比速参数

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `SPEED_CHECK_DURATION` | 比速观察时长（秒） | `60` |
| `SPEED_CHECK_INTERVAL` | 采样间隔（秒） | `10` |
| `INITIAL_WAIT` | 提交后等待迅雷开始（秒） | `10` |
| `MIN_SPEED_BYTES` | 最低有效速度（bytes/s） | `1024` |

### 0 速度监控

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `ZERO_SPEED_ENABLED` | 是否启用 0 速度自动清理 | `true` |
| `ZERO_SPEED_TIMEOUT` | 0 速度超时时间（分钟） | `10` |

监控范围：所有正在下载的 qBit 任务 + 迅雷活跃任务（不限标签）。

### 其他

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `TARGET_LABEL` | 触发转存的 qBit 标签 | `迅雷` |
| `CHECK_INTERVAL` | 主循环检查间隔（秒） | `30` |
| `MAX_CONCURRENT` | 最大同时转存任务数 | `3` |
| `DELETE_FILES` | 删除 qBit 任务时是否同时删文件 | `false` |

## 文件结构

```
fn-xunlei/
├── qbit_to_xunlei.py      # 主脚本
├── xunlei_downloader.py    # 迅雷 API 核心模块
├── config/
│   ├── config.ini          # 实际配置
│   └── config.ini.example  # 配置模板
├── xunlei_data/            # 凭据缓存（自动生成）
├── Dockerfile
├── docker-compose.yml
└── README.md
```

## 本地运行（不用 Docker）

```bash
pip install requests websockets
cp config/config.ini.example config/config.ini
# 编辑 config.ini
python3 qbit_to_xunlei.py
```

## 技术细节

- **NAS 登录**: WebSocket (`ws://<nas>:5666/websocket?type=main`)，明文 `user.login`
- **迅雷认证**: 从迅雷 HTML 页面提取 `pan_auth` JWT（硬编码在页面里）
- **迅雷 API**: `POST /drive/v1/task` 创建任务，需要 `space` + `target` 参数
- **磁力链接**: 从 qBit 获取完整 tracker 列表，拼接到磁力链接里
