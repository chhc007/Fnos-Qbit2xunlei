#!/usr/bin/env python3
"""
qBittorrent → 迅雷 自动转存脚本
功能：监听 qBit 中带特定标签的任务，自动用迅雷下载

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

# 添加脚本目录到路径
# 添加脚本目录到路径（支持从任意目录运行）
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

# 通用
CHECK_INTERVAL = config.getint("general", "CHECK_INTERVAL", fallback=30)
TARGET_LABEL = config.get("general", "TARGET_LABEL", fallback="迅雷")
DELETE_FILES = config.getboolean("general", "DELETE_FILES", fallback=False)
WAIT_COMPLETION = config.getboolean("general", "WAIT_COMPLETION", fallback=False)
MAX_CONCURRENT = config.getint("general", "MAX_CONCURRENT", fallback=3)

log.info("✅ 配置加载完成")
log.info(f"  qBit: {QB_HOST}")
log.info(f"  NAS:  {NAS_HOST}:{NAS_PORT}")
log.info(f"  标签: {TARGET_LABEL}")
log.info(f"  间隔: {CHECK_INTERVAL}s")


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
        """获取所有任务"""
        url = f"{self.host}/api/v2/torrents/info"
        resp = self.session.get(url, verify=False)
        resp.raise_for_status()
        return resp.json()

    def get_torrent_files(self, hash_: str) -> list:
        """获取任务文件列表"""
        url = f"{self.host}/api/v2/torrents/files"
        resp = self.session.get(url, params={"hash": hash_}, verify=False)
        resp.raise_for_status()
        return [f["name"] for f in resp.json()]

    def delete_torrent(self, hash_: str, delete_files: bool = False):
        """删除任务"""
        url = f"{self.host}/api/v2/torrents/delete"
        data = {"hashes": hash_, "deleteFiles": str(delete_files).lower()}
        resp = self.session.post(url, data=data, verify=False)
        resp.raise_for_status()
        log.info(f"已删除 qBit 任务: {hash_[:8]}...")

    def remove_tag(self, hash_: str, tag: str):
        """移除任务标签"""
        url = f"{self.host}/api/v2/torrents/removeTags"
        data = {"hashes": hash_, "tags": tag}
        resp = self.session.post(url, data=data, verify=False)
        resp.raise_for_status()
        log.info(f"已移除标签: {hash_[:8]}... → {tag}")

    def add_tag(self, hash_: str, tag: str):
        """添加任务标签"""
        url = f"{self.host}/api/v2/torrents/addTags"
        data = {"hashes": hash_, "tags": tag}
        resp = self.session.post(url, data=data, verify=False)
        resp.raise_for_status()


# ============ 主逻辑 ============

def generate_magnet(hash_: str, name: str) -> str:
    """根据 hash 生成磁力链接"""
    name_encoded = urllib.parse.quote(name)
    return f"magnet:?xt=urn:btih:{hash_}&dn={name_encoded}"


def main():
    # 初始化 qBit 客户端
    try:
        qbit = QBitClient(QB_HOST, QB_USER, QB_PASS)
    except Exception as e:
        log.error(f"qBit 连接失败: {e}")
        sys.exit(1)

    # 初始化迅雷下载器
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

    # 主循环
    log.info("开始监听 qBit 任务...")
    while True:
        try:
            torrents = qbit.get_torrents()
            active_count = len([t for t in torrents if TARGET_LABEL in t.get("tags", "")])

            for t in torrents:
                # 只处理带目标标签的未完成任务
                PROCESS_STATES = ["downloading", "stalledDL", "metaDL"]

                if TARGET_LABEL not in t.get("tags", ""):
                    continue
                if t.get("state") not in PROCESS_STATES:
                    continue
                if t["hash"] in processing or t["hash"] in failed:
                    continue

                # 并发控制
                if active_count >= MAX_CONCURRENT:
                    log.debug(f"达到并发上限 {MAX_CONCURRENT}，跳过")
                    break

                log.info(f"\n{'='*50}")
                log.info(f"发现待转存任务: {t['name']}")
                log.info(f"  Hash: {t['hash'][:16]}...")
                log.info(f"  状态: {t['state']}")

                # 获取磁力链接
                magnet = t.get("magnet_uri") or t.get("magnetUri") or generate_magnet(t["hash"], t["name"])
                log.info(f"  磁力: {magnet[:80]}...")

                # 获取下载路径（使用 qBit 任务的保存路径的最后一级目录名）
                save_path = t.get("save_path", "")
                last_folder = os.path.basename(save_path.rstrip("/\\")) if save_path else ""

                # 获取文件列表
                files = qbit.get_torrent_files(t["hash"])
                log.info(f"  文件数: {len(files)}")
                for f in files[:5]:
                    log.info(f"    - {f}")
                if len(files) > 5:
                    log.info(f"    ... 还有 {len(files)-5} 个文件")

                # 标记为处理中
                processing.add(t["hash"])
                active_count += 1

                # 提交迅雷下载
                try:
                    task_id = xunlei.add_download(magnet)

                    if task_id:
                        log.info(f"✅ 迅雷下载已提交: {task_id}")

                        # 可选：等待下载完成
                        if WAIT_COMPLETION:
                            log.info("等待迅雷下载完成...")
                            result = xunlei.wait_task(task_id, timeout=7200)
                            if result == "completed":
                                log.info("✅ 迅雷下载完成")
                            else:
                                log.warning(f"迅雷下载状态: {result}")

                        # 删除 qBit 任务
                        qbit.delete_torrent(t["hash"], delete_files=DELETE_FILES)
                        log.info(f"✅ 已删除 qBit 任务")
                    else:
                        log.error("❌ 迅雷下载提交失败")
                        failed.add(t["hash"])
                        qbit.remove_tag(t["hash"], TARGET_LABEL)
                        qbit.add_tag(t["hash"], "迅雷失败")
                        log.info("已标记为'迅雷失败'")

                except Exception as e:
                    log.error(f"❌ 迅雷下载异常: {e}")
                    failed.add(t["hash"])
                    qbit.remove_tag(t["hash"], TARGET_LABEL)
                    qbit.add_tag(t["hash"], "迅雷失败")

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
