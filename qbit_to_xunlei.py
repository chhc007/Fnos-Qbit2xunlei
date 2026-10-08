#!/usr/bin/env python3
"""
qBittorrent → 迅雷 自动转存脚本

功能：
  1. 监听 qBit 中带特定标签的任务，自动用迅雷下载
  2. 比速后决定保留哪个
  3. 持续监控所有下载任务，0速度超时自动清理

用法:
  1. 配置 config.ini
  2. 运行: python3 qbit_to_xunlei.py
  3. 在 qBit 中给任务添加"迅雷"标签即可自动转存
"""

import os
import re
import sys
import json
import time
import logging
import configparser
import urllib.parse
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

try:
    import requests
except ImportError:
    print("❌ 需要安装 requests: pip3 install requests")
    sys.exit(1)

try:
    from xunlei_downloader import XunleiDownloader, XunleiAuthError
except ImportError:
    print("❌ 找不到 xunlei_downloader.py，请确保在同一目录")
    sys.exit(1)

# ============ 日志 ============

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("qbit2xunlei")

# ============ 配置 ============

CONFIG_PATH = os.environ.get("CONFIG_PATH", os.path.join(os.path.dirname(__file__), "./config/config.ini"))

if not os.path.exists(CONFIG_PATH):
    log.error(f"配置文件不存在: {CONFIG_PATH}")
    log.info("请复制 config.ini.example 为 config.ini 并填写配置")
    sys.exit(1)

config = configparser.ConfigParser()
config.read(CONFIG_PATH, encoding="utf-8")

# qBittorrent
QB_HOST = config.get("qbittorrent", "QB_HOST").rstrip("/")
QB_USER = config.get("qbittorrent", "QB_USER")
QB_PASS = config.get("qbittorrent", "QB_PASS")

# NAS / 迅雷
NAS_HOST = config.get("nas", "NAS_HOST")
NAS_PORT = config.getint("nas", "NAS_PORT", fallback=5666)
NAS_USER = config.get("nas", "NAS_USER")
NAS_PASS = config.get("nas", "NAS_PASS")
XUNLEI_DOWNLOAD_PATH = config.get("nas", "XUNLEI_DOWNLOAD_PATH", fallback="")
QBIT_SAVE_PATH_PREFIX = config.get("nas", "QBIT_SAVE_PATH_PREFIX", fallback="").rstrip("/")
XUNLEI_BASE_PATH = config.get("nas", "XUNLEI_BASE_PATH", fallback="").rstrip("/")

# 通用
CHECK_INTERVAL = config.getint("general", "CHECK_INTERVAL", fallback=30)
TARGET_LABEL = config.get("general", "TARGET_LABEL", fallback="迅雷")
DELETE_FILES = config.getboolean("general", "DELETE_FILES", fallback=False)
MAX_CONCURRENT = config.getint("general", "MAX_CONCURRENT", fallback=3)

# 速度比较参数
SPEED_CHECK_DURATION = config.getint("general", "SPEED_CHECK_DURATION", fallback=60)
SPEED_CHECK_INTERVAL = config.getint("general", "SPEED_CHECK_INTERVAL", fallback=10)
INITIAL_WAIT = config.getint("general", "INITIAL_WAIT", fallback=10)
MIN_SPEED_BYTES = config.getint("general", "MIN_SPEED_BYTES", fallback=1024)

# 预检查参数
PRE_CHECK_WAIT = config.getint("general", "PRE_CHECK_WAIT", fallback=30)
PRE_CHECK_SPEED_THRESHOLD = config.getint("general", "PRE_CHECK_SPEED_THRESHOLD", fallback=500) * 1024  # KB/s → bytes/s

# 0速度超时
ZERO_SPEED_ENABLED = config.getboolean("general", "ZERO_SPEED_ENABLED", fallback=True)
ZERO_SPEED_TIMEOUT = config.getint("general", "ZERO_SPEED_TIMEOUT", fallback=10) * 60  # 转为秒

# 文件过滤
FILTER_FILES = config.getboolean("general", "FILTER_FILES", fallback=False)

# 调试模式
DEBUG = config.getboolean("general", "DEBUG", fallback=False)

# 任务读取方式
TASK_SOURCE = config.get("general", "TASK_SOURCE", fallback="api")

# 并发限制（迅雷「同时下载任务数」上限）
# 迅雷同时下载任务数有上限，超出会排队；排队任务 speed=0，
# 若不加区分会被误判为「卡死」而删除。此值即上限，0=不限制。
MAX_CONCURRENT_TASKS = config.getint("general", "MAX_CONCURRENT_TASKS", fallback=0)
# 队列轮询间隔（秒）：迅雷任务满时，隔多久再看一次是否有空位
QUEUE_POLL_INTERVAL = config.getint("general", "QUEUE_POLL_INTERVAL", fallback=30)
# 排队任务最长等待时间（分钟），超时则放弃转存并保留 qBit 任务
QUEUE_MAX_WAIT_MINUTES = config.getint("general", "QUEUE_MAX_WAIT_MINUTES", fallback=60)

if DEBUG:
    logging.getLogger().setLevel(logging.DEBUG)
    log.debug("调试模式已开启")

log.info("✅ 配置加载完成")
log.info(f"  qBit: {QB_HOST}")
log.info(f"  NAS:  {NAS_HOST}:{NAS_PORT}")
log.info(f"  标签: {TARGET_LABEL}")
log.info(f"  间隔: {CHECK_INTERVAL}s")
log.info(f"  比速: {SPEED_CHECK_DURATION}s (每{SPEED_CHECK_INTERVAL}s采样)")
log.info(f"  预检查: 等待{PRE_CHECK_WAIT}s, 阈值 {PRE_CHECK_SPEED_THRESHOLD // 1024} KB/s")
if ZERO_SPEED_ENABLED:
    log.info(f"  0速度超时: {ZERO_SPEED_TIMEOUT // 60}分钟")
else:
    log.info("  0速度超时: 已禁用")
log.info(f"  文件过滤: {'启用' if FILTER_FILES else '禁用'}")
log.info(f"  调试模式: {'开启' if DEBUG else '关闭'}")
log.info(f"  任务读取: {TASK_SOURCE}")


# ============ qBittorrent API ============

class QBitClient:
    """qBittorrent Web API 客户端"""

    def __init__(self, host: str, user: str, password: str):
        self.host = host
        self.session = requests.Session()
        self._login(user, password)

    def _login(self, user: str, password: str):
        url = f"{self.host}/api/v2/auth/login"
        if DEBUG:
            log.debug(f"[DEBUG] qBit 登录 POST {url}")
        resp = self.session.post(url, data={"username": user, "password": password}, verify=False)
        if DEBUG:
            log.debug(f"[DEBUG] qBit 登录响应: HTTP {resp.status_code}, body={resp.text[:200]}")
        if resp.status_code not in (200, 204):
            raise Exception(f"qBit 登录失败 (HTTP {resp.status_code})，请检查用户名密码")
        log.info("qBit 登录成功")

    def get_torrents(self) -> list:
        url = f"{self.host}/api/v2/torrents/info"
        if DEBUG:
            log.debug(f"[DEBUG] qBit GET {url}")
        resp = self.session.get(url, verify=False)
        if DEBUG:
            log.debug(f"[DEBUG] qBit torrents 响应: HTTP {resp.status_code}, {len(resp.json())} 个任务")
        resp.raise_for_status()
        return resp.json()

    def get_torrent(self, hash_: str) -> dict:
        torrents = self.get_torrents()
        for t in torrents:
            if t["hash"] == hash_:
                return t
        return {}

    def get_torrent_files(self, hash_: str) -> list:
        url = f"{self.host}/api/v2/torrents/files"
        resp = self.session.get(url, params={"hash": hash_}, verify=False)
        resp.raise_for_status()
        return [f["name"] for f in resp.json()]

    def get_torrent_trackers(self, hash_: str) -> list:
        """获取任务的 tracker 列表"""
        url = f"{self.host}/api/v2/torrents/trackers"
        resp = self.session.get(url, params={"hash": hash_}, verify=False)
        resp.raise_for_status()
        trackers = []
        for t in resp.json():
            tracker_url = t.get("url", "")
            # 过滤掉通配符和私有 tracker
            if tracker_url and tracker_url not in ("** [DHT] **", "** [PeX] **", "** [LSD] **"):
                trackers.append(tracker_url)
        return trackers

    def get_speed(self, hash_: str) -> int:
        """获取任务当前下载速度（bytes/s）"""
        t = self.get_torrent(hash_)
        speed = t.get("dlspeed", 0)
        if DEBUG:
            log.debug(f"[DEBUG] qBit 速度 {hash_[:8]}: {speed} bytes/s ({fmt_speed(speed)})")
        return speed

    def delete_torrent(self, hash_: str, delete_files: bool = False):
        url = f"{self.host}/api/v2/torrents/delete"
        data = {"hashes": hash_, "deleteFiles": str(delete_files).lower()}
        resp = self.session.post(url, data=data, verify=False)
        resp.raise_for_status()
        log.info(f"已删除 qBit 任务: {hash_[:8]}...")

    def remove_tag(self, hash_: str, tag: str):
        url = f"{self.host}/api/v2/torrents/removeTags"
        data = {"hashes": hash_, "tags": tag}
        resp = self.session.post(url, data=data, verify=False)
        resp.raise_for_status()

    def add_tag(self, hash_: str, tag: str):
        url = f"{self.host}/api/v2/torrents/addTags"
        data = {"hashes": hash_, "tags": tag}
        resp = self.session.post(url, data=data, verify=False)
        resp.raise_for_status()


# ============ 工具函数 ============

def generate_magnet(hash_: str, name: str, trackers: list = None) -> str:
    name_encoded = urllib.parse.quote(name)
    magnet = f"magnet:?xt=urn:btih:{hash_}&dn={name_encoded}"
    if trackers:
        for tr in trackers:
            magnet += f"&tr={urllib.parse.quote(tr, safe='')}"
    return magnet


def fmt_speed(bps: int) -> str:
    if bps < 1024:
        return f"{bps} B/s"
    elif bps < 1024 * 1024:
        return f"{bps / 1024:.1f} KB/s"
    else:
        return f"{bps / (1024 * 1024):.2f} MB/s"


def fmt_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    elif size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    else:
        return f"{size / (1024 * 1024 * 1024):.2f} GB"


def fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m{seconds % 60}s"
    else:
        return f"{seconds // 3600}h{(seconds % 3600) // 60}m"


def find_xunlei_task(xl_tasks, qbit_hash, qbit_name):
    """
    在迅雷任务列表中找到匹配的任务。
    优先用 torrent hash 匹配（稳定），兜底用名称匹配。
    """
    # 1. Hash 匹配：从迅雷任务的 params.url 中提取 btih
    for task in xl_tasks:
        params = task.get("params", {})
        url = params.get("url", "") if isinstance(params, dict) else ""
        match = re.search(r'btih:([a-fA-F0-9]{40})', url, re.IGNORECASE)
        if match and match.group(1).lower() == qbit_hash.lower():
            return task

    # 2. 名称兜底匹配
    for task in xl_tasks:
        tname = task.get("file_name", task.get("name", ""))
        if tname and qbit_name and qbit_name in tname:
            return task

    return None


def get_task_speed(task: dict) -> int:
    """从迅雷任务对象取速度（bytes/s）"""
    params = task.get("params", {}) if isinstance(task, dict) else {}
    try:
        return int(params.get("speed", 0) or 0)
    except (TypeError, ValueError):
        return 0


def is_task_queued(task: dict) -> bool:
    """
    判断迅雷任务是否处于「排队等待」状态（尚未真正开始下载）。

    背景：迅雷有「同时下载任务数」上限（客户端侧 task_run_nums_limit）。
    超出上限的新任务会停在排队状态 —— 表现为 phase=PHASE_TYPE_PENDING 且 speed=0，
    与「真正开始下载但暂时无速度」的任务无法用 speed 区分。

    判定依据（实测）：
      - 真正下载中：phase=PHASE_TYPE_RUNNING，且 params.status={"phase":"running"}
      - 排队中    ：phase=PHASE_TYPE_PENDING，params.status 为空或 phase 非 running
      - 暂停      ：params.status={"phase":"pause"}

    注意：这些任务绝不能被当作「0 速度」删除，否则会造成丢任务 + 反复重试。
    """
    if not isinstance(task, dict):
        return False
    phase = task.get("phase", "")
    if phase == "PHASE_TYPE_PENDING":
        return True
    if phase != "PHASE_TYPE_RUNNING":
        return False
    # RUNNING 但尚未进入实际下载（status 里没有 running 标记）→ 视为排队
    params = task.get("params", {}) or {}
    raw = params.get("status")
    if not raw:
        return True
    try:
        st = json.loads(raw) if isinstance(raw, str) else raw
        return st.get("phase") != "running"
    except (ValueError, TypeError):
        return True


def count_active_xunlei_tasks(xl_tasks) -> int:
    """统计迅雷当前「占用并发位」的任务数（下载中 + 排队中）"""
    return sum(1 for t in xl_tasks
               if t.get("phase") in ("PHASE_TYPE_RUNNING", "PHASE_TYPE_PENDING"))


# ============ 迅雷任务状态检查 ============

def check_xunlei_task(xunlei: XunleiDownloader, task_id: str) -> dict:
    """检查迅雷任务状态（从列表中查找）"""
    try:
        tasks = xunlei.list_tasks("all")
        for t in tasks:
            if t.get("id") == task_id:
                phase = t.get("phase", "")
                params = t.get("params", {})
                # speed 在 params 里，是字符串
                speed = int(params.get("speed", 0)) if isinstance(params, dict) else 0
                progress = t.get("progress", 0)
                pct = progress if isinstance(progress, (int, float)) else 0
                return {"phase": phase, "speed": speed, "progress": pct, "error": ""}
        return {"phase": "unknown", "speed": 0, "progress": 0, "error": "not found"}
    except Exception as e:
        return {"phase": "unknown", "speed": 0, "progress": 0, "error": str(e)}


def delete_xunlei_task(xunlei: XunleiDownloader, task_id: str):
    """删除迅雷任务（需带 space，与网页端一致）"""
    try:
        space = xunlei.target or ""
        xunlei._api_post(
            f"/drive/v1/tasks?space={urllib.parse.quote(space)}&task_ids={task_id}",
            method="DELETE")
        log.info(f"已删除迅雷任务: {task_id[:16]}...")
    except Exception as e:
        log.warning(f"删除迅雷任务失败: {e}")


# ============ 核心比速逻辑 ============

def observe_and_compare(xunlei: XunleiDownloader, qbit: QBitClient,
                        task_id: str, qbit_hash: str) -> str:
    """
    观察迅雷任务，与 qBit 比较速度

    返回: "xunlei_faster" / "qbit_faster" / "xunlei_failed"
    """
    log.info(f"开始比速观察 ({SPEED_CHECK_DURATION}s)...")

    xunlei_speeds = []
    qbit_speeds = []
    samples = SPEED_CHECK_DURATION // SPEED_CHECK_INTERVAL

    for i in range(samples):
        time.sleep(SPEED_CHECK_INTERVAL)

        status = check_xunlei_task(xunlei, task_id)
        xl_speed = status["speed"]
        xunlei_speeds.append(xl_speed)

        qb_speed = qbit.get_speed(qbit_hash)
        qbit_speeds.append(qb_speed)

        phase = status["phase"]
        log.info(f"  [{(i+1)*SPEED_CHECK_INTERVAL}s] "
                 f"迅雷: {fmt_speed(xl_speed)} | qBit: {fmt_speed(qb_speed)} | "
                 f"迅雷: {phase}")

        if phase in ("PHASE_TYPE_ERROR", "PHASE_TYPE_FAIL"):
            log.warning(f"迅雷任务失败: {phase}")
            return "xunlei_failed"

        if phase == "PHASE_TYPE_COMPLETE":
            log.info("迅雷任务已完成")
            return "xunlei_faster"

    avg_xunlei = sum(xunlei_speeds) / len(xunlei_speeds) if xunlei_speeds else 0
    avg_qbit = sum(qbit_speeds) / len(qbit_speeds) if qbit_speeds else 0

    log.info(f"平均速度 → 迅雷: {fmt_speed(int(avg_xunlei))} | qBit: {fmt_speed(int(avg_qbit))}")

    if avg_xunlei < MIN_SPEED_BYTES:
        log.info("迅雷平均速度过低，判定 qBit 更优")
        return "qbit_faster"

    if avg_qbit < MIN_SPEED_BYTES and avg_xunlei >= MIN_SPEED_BYTES:
        log.info("qBit 没速度，迅雷有速度，判定迅雷更优")
        return "xunlei_faster"

    if avg_xunlei > avg_qbit:
        log.info(f"迅雷更快 ({fmt_speed(int(avg_xunlei))} > {fmt_speed(int(avg_qbit))})")
        return "xunlei_faster"
    else:
        log.info(f"qBit 更快或持平")
        return "qbit_faster"


# ============ 0速度监控 ============

class ZeroSpeedMonitor:
    """监控下载任务，0速度超时自动清理"""

    def __init__(self, timeout_seconds: int, xunlei: XunleiDownloader = None):
        self.timeout = timeout_seconds
        self.xunlei = xunlei
        # {key: {"first_zero": timestamp, "type": "qbit"/"xunlei", "task_id": ..., "hash": ...}}
        self.tracking = {}

    def check_qbit(self, qbit: QBitClient, hash_: str, name: str) -> bool:
        """检查 qBit 任务，返回 True 表示已处理（删除/标记）"""
        key = f"qbit:{hash_}"
        t = qbit.get_torrent(hash_)
        if not t:
            self.tracking.pop(key, None)
            return False

        state = t.get("state", "")
        if state not in ("downloading", "stalledDL", "metaDL"):
            self.tracking.pop(key, None)
            return False

        speed = t.get("dlspeed", 0)
        progress = t.get("progress", 0)

        # 有速度 → 清除跟踪
        if speed > 0:
            self.tracking.pop(key, None)
            return False

        # 0速度
        now = time.time()
        if key not in self.tracking:
            self.tracking[key] = {"first_zero": now, "type": "qbit", "hash": hash_, "name": name}
            return False

        elapsed = now - self.tracking[key]["first_zero"]
        if elapsed >= self.timeout:
            log.warning(f"⏰ qBit 0速度超时 ({fmt_duration(int(elapsed))}): {name}")
            qbit.delete_torrent(hash_, delete_files=DELETE_FILES)
            self.tracking.pop(key, None)
            return True

        remaining = self.timeout - elapsed
        log.debug(f"qBit 0速度跟踪: {name} 已 {fmt_duration(int(elapsed))}，"
                  f"剩余 {fmt_duration(int(remaining))}")
        return False

    def check_xunlei_task_by_speed(self, task_id: str, name: str, speed: int,
                                   queued: bool = False) -> bool:
        """
        检查迅雷任务（直接传入速度），返回 True 表示已处理（删除）

        queued=True 表示该任务处于「排队等待」（受同时下载数限制），
        此时不计入 0 速度跟踪 —— 否则排队任务会被误判为卡死而删除。
        """
        key = f"xunlei:{task_id}"

        # 排队中 或 有速度 → 清除跟踪（都不算卡死）
        if queued or speed > 0:
            self.tracking.pop(key, None)
            return False

        # 0速度
        now = time.time()
        if key not in self.tracking:
            self.tracking[key] = {"first_zero": now, "type": "xunlei", "task_id": task_id, "name": name}
            return False

        elapsed = now - self.tracking[key]["first_zero"]
        if elapsed >= self.timeout:
            log.warning(f"⏰ 迅雷 0速度超时 ({fmt_duration(int(elapsed))}): {name}")
            delete_xunlei_task(self.xunlei, task_id)
            self.tracking.pop(key, None)
            return True

        remaining = self.timeout - elapsed
        log.debug(f"迅雷 0速度跟踪: {name} 已 {fmt_duration(int(elapsed))}，"
                  f"剩余 {fmt_duration(int(remaining))}")
        return False

    def cleanup(self, active_qbit_hashes: set, active_xunlei_ids: set):
        """清理已不存在的任务跟踪"""
        for key in list(self.tracking.keys()):
            t = self.tracking[key]
            if t["type"] == "qbit" and t["hash"] not in active_qbit_hashes:
                self.tracking.pop(key, None)
            elif t["type"] == "xunlei" and t["task_id"] not in active_xunlei_ids:
                self.tracking.pop(key, None)


# ============ 主逻辑 ============

def main():
    # 初始化 qBit
    try:
        qbit = QBitClient(QB_HOST, QB_USER, QB_PASS)
    except Exception as e:
        log.error(f"qBit 连接失败: {e}")
        sys.exit(1)

    # 初始化迅雷
    xunlei = XunleiDownloader(
        nas_host=NAS_HOST,
        nas_port=NAS_PORT,
        nas_user=NAS_USER,
        nas_pass=NAS_PASS,
        download_path=XUNLEI_DOWNLOAD_PATH,
        filter_files=FILTER_FILES,
        debug=DEBUG,
        task_source=TASK_SOURCE,
    )

    log.info("正在初始化迅雷下载器...")
    if not xunlei.init():
        log.error("迅雷下载器初始化失败")
        sys.exit(1)
    log.info("✅ 迅雷下载器初始化完成")

    # 正在处理的任务
    processing = set()
    # 已失败的任务
    failed = set()
    # 0速度监控器
    zero_monitor = ZeroSpeedMonitor(ZERO_SPEED_TIMEOUT, xunlei=xunlei)

    log.info("开始监听 qBit 任务...")
    while True:
        try:
            torrents = qbit.get_torrents()

            # ====== 0速度监控 ======
            if ZERO_SPEED_ENABLED:
                # qBit
                for t in torrents:
                    hash_ = t["hash"]
                    state = t.get("state", "")
                    if state in ("downloading", "stalledDL", "metaDL"):
                        if hash_ not in processing:
                            zero_monitor.check_qbit(qbit, hash_, t["name"])

                # 迅雷（复用列表，避免重复请求）
                try:
                    xl_all_tasks = xunlei.list_tasks("all")
                    for task in xl_all_tasks:
                        tid = task.get("id", "")
                        phase = task.get("phase", "")
                        if phase not in ("PHASE_TYPE_RUNNING", "PHASE_TYPE_PENDING"):
                            continue
                        tname = task.get("file_name", task.get("name", "unknown"))
                        speed = get_task_speed(task)
                        # 排队中的任务不计入 0 速度跟踪（受同时下载数限制，并非卡死）
                        if is_task_queued(task):
                            zero_monitor.tracking.pop(f"xunlei:{tid}", None)
                            continue
                        if speed > 0:
                            zero_monitor.tracking.pop(f"xunlei:{tid}", None)
                            continue
                        if tid and tid not in processing:
                            zero_monitor.check_xunlei_task_by_speed(tid, tname, speed)
                except XunleiAuthError as e:
                    log.warning(f"迅雷凭据失效: {e}（已尝试自动刷新，下一轮重试）")
                except Exception as e:
                    log.debug(f"迅雷任务监控异常: {e}")

            # ====== 转存逻辑 ======
            for t in torrents:
                PROCESS_STATES = ["downloading", "stalledDL", "metaDL"]

                if TARGET_LABEL not in t.get("tags", ""):
                    continue
                if t.get("state") not in PROCESS_STATES:
                    continue
                if t["hash"] in processing or t["hash"] in failed:
                    continue

                log.info(f"\n{'='*60}")
                log.info(f"发现待转存任务: {t['name']}")
                log.info(f"  Hash: {t['hash'][:16]}...")
                log.info(f"  状态: {t['state']} | qBit 速度: {fmt_speed(t.get('dlspeed', 0))}")

                # 获取磁力链接（优先用 qBit 的完整磁力，含 tracker）
                magnet = t.get("magnet_uri") or t.get("magnetUri") or ""
                if not magnet or "&tr=" not in magnet:
                    # qBit 没返回完整磁力，用 hash + tracker 重建
                    trackers = qbit.get_torrent_trackers(t["hash"])
                    magnet = generate_magnet(t["hash"], t["name"], trackers)
                    log.info(f"  Tracker 数: {len(trackers)}")

                # 计算迅雷下载路径
                target_dir = ""
                if QBIT_SAVE_PATH_PREFIX and XUNLEI_BASE_PATH:
                    save_path = t.get("save_path", "").rstrip("/")
                    if save_path.startswith(QBIT_SAVE_PATH_PREFIX):
                        sub_path = save_path[len(QBIT_SAVE_PATH_PREFIX):].lstrip("/")
                        target_dir = f"{XUNLEI_BASE_PATH}/{sub_path}" if sub_path else XUNLEI_BASE_PATH
                        log.info(f"  路径映射: {save_path} → {target_dir}")
                    else:
                        log.warning(f"  路径前缀不匹配: {save_path}")
                elif XUNLEI_DOWNLOAD_PATH:
                    target_dir = XUNLEI_DOWNLOAD_PATH

                # 标记为处理中
                processing.add(t["hash"])

                # 凭证检查：如果过期则刷新（_api_* 也会在失效时自动自愈）
                if not xunlei._test_auth():
                    log.warning("凭证已过期，刷新中...")
                    xunlei.init()

                try:
                    # ====== Step 0: 预检查 qBit 速度 ======
                    if PRE_CHECK_WAIT > 0:
                        log.info(f"预检查: 等待 {PRE_CHECK_WAIT}s 观察 qBit 速度...")
                        pre_samples = max(1, PRE_CHECK_WAIT // SPEED_CHECK_INTERVAL)
                        pre_speeds = []
                        for si in range(pre_samples):
                            time.sleep(SPEED_CHECK_INTERVAL)
                            spd = qbit.get_speed(t["hash"])
                            pre_speeds.append(spd)
                            log.info(f"  预检查 [{(si+1)*SPEED_CHECK_INTERVAL}s] qBit: {fmt_speed(spd)}")
                        avg_pre = sum(pre_speeds) / len(pre_speeds) if pre_speeds else 0
                        if avg_pre >= PRE_CHECK_SPEED_THRESHOLD:
                            log.info(f"qBit 平均速度 {fmt_speed(int(avg_pre))} >= 阈值 {fmt_speed(PRE_CHECK_SPEED_THRESHOLD)}，无需转迅雷")
                            qbit.remove_tag(t["hash"], TARGET_LABEL)
                            continue
                        log.info(f"qBit 平均速度 {fmt_speed(int(avg_pre))} < 阈值 {fmt_speed(PRE_CHECK_SPEED_THRESHOLD)}，转迅雷")

                    # ====== Step 0.5: 并发位检查（迅雷同时下载数上限）======
                    # 迅雷超出「同时下载任务数」的任务会排队，排队任务 speed=0。
                    # 这里先确认有空闲并发位，避免提交后立刻进入排队状态被误判。
                    if MAX_CONCURRENT_TASKS > 0:
                        wait_start = time.time()
                        max_wait = QUEUE_MAX_WAIT_MINUTES * 60
                        timed_out = False
                        while True:
                            try:
                                cur = xunlei.list_tasks("all")
                                active = count_active_xunlei_tasks(cur)
                            except Exception as e:
                                log.warning(f"查询迅雷并发占用失败: {e}，直接尝试提交")
                                break
                            if active < MAX_CONCURRENT_TASKS:
                                log.info(f"并发位充足: {active}/{MAX_CONCURRENT_TASKS}，提交迅雷")
                                break
                            if time.time() - wait_start >= max_wait:
                                log.warning(
                                    f"⏳ 迅雷并发已满 {active}/{MAX_CONCURRENT_TASKS}，"
                                    f"等待超过 {QUEUE_MAX_WAIT_MINUTES} 分钟，"
                                    f"本轮跳过（保留 qBit 任务与标签，下轮重试）")
                                timed_out = True
                                break
                            log.info(f"⏳ 迅雷并发已满 {active}/{MAX_CONCURRENT_TASKS}，"
                                     f"等待 {QUEUE_POLL_INTERVAL}s 后重试...")
                            time.sleep(QUEUE_POLL_INTERVAL)
                        if timed_out:
                            continue

                    # ====== Step 1: 提交迅雷下载 ======
                    log.info("提交迅雷下载...")
                    result = xunlei.add_download(magnet, name=t["name"], target_dir=target_dir)

                    if not result:
                        log.error("迅雷任务创建失败")
                        failed.add(t["hash"])
                        qbit.remove_tag(t["hash"], TARGET_LABEL)
                        qbit.add_tag(t["hash"], "迅雷失败")
                        continue

                    log.info("迅雷任务已提交")

                    # ====== Step 2: 等待迅雷开始下载 ======
                    log.info(f"等待 {INITIAL_WAIT}s 观察迅雷是否开始下载...")
                    time.sleep(INITIAL_WAIT)

                    # 通过 hash/名称查找迅雷任务状态
                    xl_tasks = xunlei.list_tasks("all")
                    xl_task = find_xunlei_task(xl_tasks, t["hash"], t["name"])

                    if xl_task:
                        phase = xl_task.get("phase", "")
                        speed = get_task_speed(xl_task)
                        queued = is_task_queued(xl_task)
                        log.info(f"迅雷状态: {phase} | 速度: {fmt_speed(speed)}"
                                 + ("（排队等待中）" if queued else ""))

                        if phase in ("PHASE_TYPE_ERROR", "PHASE_TYPE_FAIL"):
                            log.warning(f"迅雷任务失败: {phase}")
                            failed.add(t["hash"])
                            qbit.remove_tag(t["hash"], TARGET_LABEL)
                            qbit.add_tag(t["hash"], "迅雷失败")
                            continue

                        if phase == "PHASE_TYPE_COMPLETE":
                            log.info("迅雷秒下完成！删除 qBit 任务")
                            qbit.delete_torrent(t["hash"], delete_files=DELETE_FILES)
                            qbit.remove_tag(t["hash"], TARGET_LABEL)
                            continue

                        # 排队等待：给迅雷时间排到队（不参与比速，避免被误删）
                        if queued:
                            log.info("迅雷任务排队中，等待其开始下载（不计入比速）...")
                            queued_start = time.time()
                            queue_max = QUEUE_MAX_WAIT_MINUTES * 60
                            while True:
                                time.sleep(QUEUE_POLL_INTERVAL)
                                xl_all = xunlei.list_tasks("all")
                                matched = find_xunlei_task(xl_all, t["hash"], t["name"])
                                if not matched:
                                    log.warning("排队任务消失，按失败处理")
                                    failed.add(t["hash"])
                                    qbit.remove_tag(t["hash"], TARGET_LABEL)
                                    qbit.add_tag(t["hash"], "迅雷异常")
                                    break
                                if matched.get("phase") in ("PHASE_TYPE_ERROR", "PHASE_TYPE_FAIL"):
                                    log.warning(f"迅雷任务失败: {matched.get('phase')}")
                                    failed.add(t["hash"])
                                    qbit.remove_tag(t["hash"], TARGET_LABEL)
                                    qbit.add_tag(t["hash"], "迅雷失败")
                                    break
                                if matched.get("phase") == "PHASE_TYPE_COMPLETE":
                                    log.info("迅雷任务已完成，删除 qBit 任务")
                                    qbit.delete_torrent(t["hash"], delete_files=DELETE_FILES)
                                    qbit.remove_tag(t["hash"], TARGET_LABEL)
                                    break
                                if not is_task_queued(matched):
                                    log.info(f"迅雷已开始下载: {fmt_speed(get_task_speed(matched))}，"
                                             f"进入比速")
                                    break
                                if time.time() - queued_start >= queue_max:
                                    log.warning(
                                        f"⏳ 迅雷排队超过 {QUEUE_MAX_WAIT_MINUTES} 分钟仍未开始，"
                                        f"放弃本次转存（保留 qBit 任务与标签，下轮重试）")
                                    break
                                log.info(f"迅雷仍在排队... 已等待 "
                                         f"{fmt_duration(int(time.time() - queued_start))}")
                            # 排队分支已处理完毕，跳过本轮后续比速
                            continue
                    else:
                        log.info("未找到对应迅雷任务，等待更多时间...")
                        time.sleep(10)
                        xl_tasks = xunlei.list_tasks("all")
                        xl_task = find_xunlei_task(xl_tasks, t["hash"], t["name"])

                    # ====== Step 3: 比速观察（用 hash/名称匹配） ======
                    log.info(f"开始比速观察 ({SPEED_CHECK_DURATION}s)...")
                    xunlei_speeds = []
                    qbit_speeds = []
                    queued_samples = 0
                    samples = SPEED_CHECK_DURATION // SPEED_CHECK_INTERVAL

                    for i in range(samples):
                        time.sleep(SPEED_CHECK_INTERVAL)

                        # 查迅雷速度（通过 hash/名称匹配）
                        xl_speed = 0
                        xl_phase = "unknown"
                        xl_queued = False
                        try:
                            xl_all = xunlei.list_tasks("all")
                            matched = find_xunlei_task(xl_all, t["hash"], t["name"])
                            if matched:
                                xl_speed = get_task_speed(matched)
                                xl_phase = matched.get("phase", "")
                                xl_queued = is_task_queued(matched)
                        except Exception as e:
                            log.debug(f"查询迅雷状态异常: {e}")

                        # 排队样本不参与速度统计（排队必然 speed=0，会拉低均值导致误判）
                        if xl_queued:
                            queued_samples += 1
                        else:
                            xunlei_speeds.append(xl_speed)
                        qb_speed = qbit.get_speed(t["hash"])
                        qbit_speeds.append(qb_speed)

                        log.info(f"  [{(i+1)*SPEED_CHECK_INTERVAL}s] "
                                 f"迅雷: {fmt_speed(xl_speed)} | qBit: {fmt_speed(qb_speed)} | "
                                 f"迅雷: {xl_phase}"
                                 + ("（排队中，不计入）" if xl_queued else ""))

                        if xl_phase in ("PHASE_TYPE_ERROR", "PHASE_TYPE_FAIL"):
                            log.warning(f"迅雷任务失败: {xl_phase}")
                            failed.add(t["hash"])
                            qbit.remove_tag(t["hash"], TARGET_LABEL)
                            qbit.add_tag(t["hash"], "迅雷失败")
                            break

                        if xl_phase == "PHASE_TYPE_COMPLETE":
                            log.info("迅雷任务已完成")
                            qbit.delete_torrent(t["hash"], delete_files=DELETE_FILES)
                            qbit.remove_tag(t["hash"], TARGET_LABEL)
                            break
                    else:
                        # 比速结束，比较平均速度
                        avg_xunlei = sum(xunlei_speeds) / len(xunlei_speeds) if xunlei_speeds else 0
                        avg_qbit = sum(qbit_speeds) / len(qbit_speeds) if qbit_speeds else 0
                        log.info(f"平均速度 -> 迅雷: {fmt_speed(int(avg_xunlei))} | "
                                 f"qBit: {fmt_speed(int(avg_qbit))} "
                                 f"(排队样本 {queued_samples}/{samples})")

                        # 保护：迅雷从未真正开始下载（全程排队）→ 不删迅雷任务，
                        # 保留 qBit 任务与标签，等下一轮继续（避免丢任务）
                        if queued_samples == samples or not xunlei_speeds:
                            log.warning("迅雷全程处于排队状态，无法比速；"
                                        "保留 qBit 任务与标签，下轮重试")
                            qbit.remove_tag(t["hash"], TARGET_LABEL)  # 摘掉以免重复处理
                        elif avg_xunlei > avg_qbit and avg_xunlei >= MIN_SPEED_BYTES:
                            log.info("迅雷更快，删除 qBit 任务")
                            qbit.delete_torrent(t["hash"], delete_files=DELETE_FILES)
                            qbit.remove_tag(t["hash"], TARGET_LABEL)
                        else:
                            log.info("qBit 更快或持平，删除迅雷任务")
                            try:
                                xl_all = xunlei.list_tasks("all")
                                matched = find_xunlei_task(xl_all, t["hash"], t["name"])
                                if matched:
                                    delete_xunlei_task(xunlei, matched.get("id", ""))
                            except Exception as e:
                                log.debug(f"删除迅雷任务异常: {e}")
                            qbit.remove_tag(t["hash"], TARGET_LABEL)
                            qbit.add_tag(t["hash"], "qBit更快")

                except Exception as e:
                    log.error(f"处理异常: {e}")
                    failed.add(t["hash"])
                    qbit.remove_tag(t["hash"], TARGET_LABEL)
                    qbit.add_tag(t["hash"], "迅雷异常")

                finally:
                    processing.discard(t["hash"])

            # 清理过期跟踪
            if ZERO_SPEED_ENABLED:
                active_qbit = {t["hash"] for t in torrents}
                try:
                    active_xl = {t["id"] for t in xunlei.list_tasks("active")}
                except:
                    active_xl = set()
                zero_monitor.cleanup(active_qbit, active_xl)

        except KeyboardInterrupt:
            log.info("用户中断，退出")
            break
        except Exception as e:
            log.error(f"循环异常: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
