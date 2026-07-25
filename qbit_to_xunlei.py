#!/usr/bin/env python3
"""
qBittorrent → 迅雷 自动转存脚本
功能：监听 qBit 中带特定标签的任务，自动用迅雷下载

流程：
  1. 发现带"迅雷"标签的 qBit 任务 → 提交迅雷下载
  2. 等待 10 秒，检查迅雷任务是否真的开始了
  3. 如果失败 → 保留 qBit，移除迅雷标签，标记"迅雷失败"
  4. 如果开始下载 → 观察 60 秒，比较迅雷和 qBit 的平均速度
  5. 迅雷更快 → 删除 qBit 任务
  6. 迅雷更慢/没速度 → 删除迅雷任务，保留 qBit

用法:
  1. 配置 config.ini
  2. 运行: python3 qbit_to_xunlei.py
  3. 在 qBit 中给任务添加"迅雷"标签即可自动转存
"""

import os
import sys
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
    from xunlei_downloader import XunleiDownloader
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

CONFIG_PATH = os.environ.get("CONFIG_PATH", os.path.join(os.path.dirname(__file__), "config.ini"))

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
SPEED_CHECK_DURATION = 60   # 观察时长（秒）
SPEED_CHECK_INTERVAL = 10   # 采样间隔（秒）
INITIAL_WAIT = 10           # 提交后等待迅雷开始的秒数
MIN_SPEED_BYTES = 1024      # 最低有效速度（1KB/s），低于此视为"没速度"

log.info("✅ 配置加载完成")
log.info(f"  qBit: {QB_HOST}")
log.info(f"  NAS:  {NAS_HOST}:{NAS_PORT}")
log.info(f"  标签: {TARGET_LABEL}")
log.info(f"  间隔: {CHECK_INTERVAL}s")
log.info(f"  比速观察: {SPEED_CHECK_DURATION}s")


# ============ qBittorrent API ============

class QBitClient:
    """qBittorrent Web API 客户端"""

    def __init__(self, host: str, user: str, password: str):
        self.host = host
        self.session = requests.Session()
        self._login(user, password)

    def _login(self, user: str, password: str):
        url = f"{self.host}/api/v2/auth/login"
        resp = self.session.post(url, data={"username": user, "password": password}, verify=False)
        if resp.status_code not in (200, 204):
            raise Exception(f"qBit 登录失败 (HTTP {resp.status_code})，请检查用户名密码")
        log.info("qBit 登录成功")

    def get_torrents(self) -> list:
        url = f"{self.host}/api/v2/torrents/info"
        resp = self.session.get(url, verify=False)
        resp.raise_for_status()
        return resp.json()

    def get_torrent(self, hash_: str) -> dict:
        """获取单个任务信息"""
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

    def get_speed(self, hash_: str) -> int:
        """获取任务当前下载速度（bytes/s）"""
        t = self.get_torrent(hash_)
        return t.get("dlspeed", 0)

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

def generate_magnet(hash_: str, name: str) -> str:
    name_encoded = urllib.parse.quote(name)
    return f"magnet:?xt=urn:btih:{hash_}&dn={name_encoded}"


def fmt_speed(bps: int) -> str:
    """格式化速度显示"""
    if bps < 1024:
        return f"{bps} B/s"
    elif bps < 1024 * 1024:
        return f"{bps / 1024:.1f} KB/s"
    else:
        return f"{bps / (1024 * 1024):.2f} MB/s"


def fmt_size(size: int) -> str:
    """格式化大小显示"""
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    elif size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    else:
        return f"{size / (1024 * 1024 * 1024):.2f} GB"


# ============ 迅雷任务状态检查 ============

def check_xunlei_task_status(xunlei: XunleiDownloader, task_id: str) -> dict:
    """
    检查迅雷任务状态
    返回: {"phase": str, "speed": int, "progress": int, "error": str}
    """
    try:
        data = xunlei._api_get(f"/drive/v1/tasks/{task_id}")
        phase = data.get("phase", "")
        progress = data.get("progress", {})
        speed = progress.get("speed", 0)
        pct = progress.get("progress", 0)
        error = data.get("error", "")

        return {
            "phase": phase,
            "speed": speed,
            "progress": pct,
            "error": error,
        }
    except Exception as e:
        return {"phase": "unknown", "speed": 0, "progress": 0, "error": str(e)}


# ============ 核心比速逻辑 ============

def observe_and_compare(xunlei: XunleiDownloader, qbit: QBitClient,
                        task_id: str, qbit_hash: str) -> str:
    """
    观察迅雷任务 60 秒，与 qBit 比较速度

    返回:
      "xunlei_faster"  → 迅雷更快，删 qBit
      "qbit_faster"    → qBit 更快或迅雷没速度，删迅雷
      "xunlei_failed"  → 迅雷任务失败
    """
    log.info(f"开始比速观察 ({SPEED_CHECK_DURATION}s)...")

    xunlei_speeds = []
    qbit_speeds = []
    samples = SPEED_CHECK_DURATION // SPEED_CHECK_INTERVAL

    for i in range(samples):
        time.sleep(SPEED_CHECK_INTERVAL)

        # 迅雷速度
        status = check_xunlei_task_status(xunlei, task_id)
        xl_speed = status["speed"]
        xunlei_speeds.append(xl_speed)

        # qBit 速度
        qb_speed = qbit.get_speed(qbit_hash)
        qbit_speeds.append(qb_speed)

        phase = status["phase"]
        log.info(f"  [{(i+1)*SPEED_CHECK_INTERVAL}s] "
                 f"迅雷: {fmt_speed(xl_speed)} | qBit: {fmt_speed(qb_speed)} | "
                 f"迅雷状态: {phase}")

        # 迅雷任务失败/出错
        if phase in ("PHASE_TYPE_ERROR", "PHASE_TYPE_FAIL"):
            log.warning(f"迅雷任务失败: {phase} - {status.get('error', '')}")
            return "xunlei_failed"

        # 迅雷任务已完成（小文件秒下）
        if phase == "PHASE_TYPE_COMPLETE":
            log.info("迅雷任务已完成（秒下）")
            return "xunlei_faster"

    # 计算平均速度
    avg_xunlei = sum(xunlei_speeds) / len(xunlei_speeds) if xunlei_speeds else 0
    avg_qbit = sum(qbit_speeds) / len(qbit_speeds) if qbit_speeds else 0

    log.info(f"平均速度 → 迅雷: {fmt_speed(int(avg_xunlei))} | qBit: {fmt_speed(int(avg_qbit))}")

    # 迅雷没速度
    if avg_xunlei < MIN_SPEED_BYTES:
        log.info("迅雷平均速度过低，判定 qBit 更优")
        return "qbit_faster"

    # qBit 没速度但迅雷有速度 → 迅雷赢
    if avg_qbit < MIN_SPEED_BYTES and avg_xunlei >= MIN_SPEED_BYTES:
        log.info("qBit 没速度，迅雷有速度，判定迅雷更优")
        return "xunlei_faster"

    # 都有速度，比较
    if avg_xunlei > avg_qbit:
        log.info(f"迅雷更快 ({fmt_speed(int(avg_xunlei))} > {fmt_speed(int(avg_qbit))})")
        return "xunlei_faster"
    else:
        log.info(f"qBit 更快或持平 ({fmt_speed(int(avg_qbit))} >= {fmt_speed(int(avg_xunlei))})")
        return "qbit_faster"


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
    )

    log.info("正在初始化迅雷下载器...")
    if not xunlei.init():
        log.error("迅雷下载器初始化失败")
        sys.exit(1)
    log.info("✅ 迅雷下载器初始化完成")

    # 正在处理的任务（防止重复提交）
    processing = set()
    # 已失败的任务（防止无限重试）
    failed = set()

    log.info("开始监听 qBit 任务...")
    while True:
        try:
            torrents = qbit.get_torrents()

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

                # 获取磁力链接
                magnet = t.get("magnet_uri") or t.get("magnetUri") or generate_magnet(t["hash"], t["name"])

                # 计算迅雷下载路径
                target_dir = ""
                if QBIT_SAVE_PATH_PREFIX and XUNLEI_BASE_PATH:
                    save_path = t.get("save_path", "").rstrip("/")
                    if save_path.startswith(QBIT_SAVE_PATH_PREFIX):
                        sub_path = save_path[len(QBIT_SAVE_PATH_PREFIX):].lstrip("/")
                        target_dir = f"{XUNLEI_BASE_PATH}/{sub_path}" if sub_path else XUNLEI_BASE_PATH
                        log.info(f"  路径映射: {save_path} → {target_dir}")
                    else:
                        log.warning(f"  路径前缀不匹配: {save_path} (期望前缀: {QBIT_SAVE_PATH_PREFIX})")
                elif XUNLEI_DOWNLOAD_PATH:
                    target_dir = XUNLEI_DOWNLOAD_PATH

                # 标记为处理中
                processing.add(t["hash"])

                try:
                    # ====== Step 1: 提交迅雷下载 ======
                    log.info("提交迅雷下载...")
                    task_id = xunlei.add_download(magnet, name=t["name"], target_dir=target_dir)

                    if not task_id:
                        log.error("❌ 迅雷任务创建失败")
                        failed.add(t["hash"])
                        qbit.remove_tag(t["hash"], TARGET_LABEL)
                        qbit.add_tag(t["hash"], "迅雷失败")
                        continue

                    log.info(f"迅雷任务已创建: {task_id}")

                    # ====== Step 2: 等待迅雷开始下载 ======
                    log.info(f"等待 {INITIAL_WAIT}s 观察迅雷是否开始下载...")
                    time.sleep(INITIAL_WAIT)

                    status = check_xunlei_task_status(xunlei, task_id)
                    phase = status["phase"]
                    log.info(f"迅雷状态: {phase} | 速度: {fmt_speed(status['speed'])}")

                    # 迅雷失败
                    if phase in ("PHASE_TYPE_ERROR", "PHASE_TYPE_FAIL"):
                        log.warning(f"❌ 迅雷任务失败: {phase}")
                        failed.add(t["hash"])
                        qbit.remove_tag(t["hash"], TARGET_LABEL)
                        qbit.add_tag(t["hash"], "迅雷失败")
                        # 删除失败的迅雷任务
                        try:
                            xunlei._api_post(f"/drive/v1/tasks?task_ids={task_id}", method="DELETE")
                        except:
                            pass
                        continue

                    # 迅雷已完成（秒下）
                    if phase == "PHASE_TYPE_COMPLETE":
                        log.info("✅ 迅雷秒下完成！删除 qBit 任务")
                        qbit.delete_torrent(t["hash"], delete_files=DELETE_FILES)
                        qbit.remove_tag(t["hash"], TARGET_LABEL)
                        continue

                    # 迅雷还在 pending/queued → 可能排队中，再等一轮
                    if phase in ("PHASE_TYPE_PENDING",):
                        log.info("迅雷任务排队中，再等 10 秒...")
                        time.sleep(10)
                        status = check_xunlei_task_status(xunlei, task_id)
                        phase = status["phase"]
                        if phase == "PHASE_TYPE_PENDING":
                            log.warning("迅雷任务仍在排队，放弃")
                            failed.add(t["hash"])
                            qbit.remove_tag(t["hash"], TARGET_LABEL)
                            qbit.add_tag(t["hash"], "迅雷排队超时")
                            continue

                    # ====== Step 3: 比速观察 ======
                    result = observe_and_compare(xunlei, qbit, task_id, t["hash"])

                    if result == "xunlei_faster":
                        # 迅雷赢 → 删 qBit
                        log.info("🏆 迅雷胜出，删除 qBit 任务")
                        qbit.delete_torrent(t["hash"], delete_files=DELETE_FILES)
                        qbit.remove_tag(t["hash"], TARGET_LABEL)

                    elif result == "qbit_faster":
                        # qBit 赢 → 删迅雷任务
                        log.info("🏆 qBit 更快，删除迅雷任务，保留 qBit")
                        try:
                            xunlei._api_post(f"/drive/v1/tasks?task_ids={task_id}", method="DELETE")
                            log.info("已删除迅雷任务")
                        except Exception as e:
                            log.warning(f"删除迅雷任务失败: {e}")
                        qbit.remove_tag(t["hash"], TARGET_LABEL)
                        qbit.add_tag(t["hash"], "qBit更快")

                    elif result == "xunlei_failed":
                        # 迅雷失败 → 保留 qBit
                        log.warning("迅雷任务失败，保留 qBit")
                        failed.add(t["hash"])
                        qbit.remove_tag(t["hash"], TARGET_LABEL)
                        qbit.add_tag(t["hash"], "迅雷失败")

                except Exception as e:
                    log.error(f"❌ 处理异常: {e}")
                    failed.add(t["hash"])
                    qbit.remove_tag(t["hash"], TARGET_LABEL)
                    qbit.add_tag(t["hash"], "迅雷异常")

                finally:
                    processing.discard(t["hash"])

        except KeyboardInterrupt:
            log.info("用户中断，退出")
            break
        except Exception as e:
            log.error(f"循环异常: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
