#!/usr/bin/env python3
"""
迅雷下载核心模块（纯 HTTP/WebSocket 版本）
功能：登录NAS → 获取迅雷凭证 → API 调用 → 监控状态
可被其他脚本 import 使用，无需浏览器依赖

依赖: pip install websockets
"""

import json
import time
import logging
import re
import asyncio
import urllib.request
import urllib.parse
import urllib.error
import http.cookiejar
from pathlib import Path
from typing import Optional, List, Dict

log = logging.getLogger("xunlei")


class XunleiDownloader:
    """迅雷下载器（纯 HTTP 版本）"""

    def __init__(self, nas_host: str, nas_port: int, nas_user: str, nas_pass: str,
                 download_path: str = "", data_dir: str = None, filter_files: bool = False,
                 debug: bool = False):
        self.nas_host = nas_host
        self.nas_port = nas_port
        self.nas_user = nas_user
        self.nas_pass = nas_pass
        self.download_path = download_path
        self.filter_files = filter_files
        self.debug = debug

        self.base_url = f"http://{nas_host}:{nas_port}"
        self.xunlei_base = f"{self.base_url}/cgi/ThirdParty/xunlei/index.cgi"

        self.data_dir = Path(data_dir or Path(__file__).parent / "xunlei_data")
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )

        self.fnos_token: Optional[str] = None
        self.xla_ci: Optional[str] = None
        self.pan_auth: Optional[str] = None
        self.device_space: str = ""

        self.video_extensions = {
            '.mkv', '.mp4', '.avi', '.rmvb', '.rm', '.wmv', '.flv',
            '.mov', '.ts', '.m4v', '.webm', '.vob', '.mpg', '.mpeg',
            '.3gp', '.f4v', '.ogv', '.nfo', '.str'
        }

    # ============ 凭据管理 ============

    def _cred_file(self) -> Path:
        return self.data_dir / "credentials.json"

    def _load_saved(self):
        f = self._cred_file()
        if not f.exists():
            return False
        try:
            data = json.loads(f.read_text())
            self.fnos_token = data.get("fnos_token")
            self.xla_ci = data.get("xla_ci")
            self.pan_auth = data.get("pan_auth")
            self.device_space = data.get("device_space", "")
            return bool(self.fnos_token and self.pan_auth)
        except Exception:
            return False

    def _save_cred(self):
        data = {
            "fnos_token": self.fnos_token,
            "xla_ci": self.xla_ci,
            "pan_auth": self.pan_auth,
            "device_space": self.device_space,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._cred_file().write_text(json.dumps(data, indent=2))

    # ============ WebSocket 登录 fnOS ============

    def _ws_login(self) -> str:
        """通过 WebSocket 登录 fnOS，返回 fnos-token"""
        try:
            import websockets
        except ImportError:
            raise Exception("需要安装 websockets: pip install websockets")

        async def _do_login():
            url = f'ws://{self.nas_host}:{self.nas_port}/websocket?type=main'
            headers = {'Origin': f'http://{self.nas_host}:{self.nas_port}'}

            if self.debug:
                log.debug(f"[DEBUG] WebSocket 连接: {url}")

            async with websockets.connect(url, additional_headers=headers) as ws:
                # 获取 SI
                await ws.send(json.dumps({'req': 'util.getSI', 'reqid': '1'}))
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                si = resp['si']
                if self.debug:
                    log.debug(f"[DEBUG] WebSocket SI: {si[:20]}...")

                # 登录（不加密）
                login_msg = {
                    'req': 'user.login',
                    'user': self.nas_user,
                    'password': self.nas_pass,
                    'si': si,
                    'stay': 2,
                    'deviceType': 'web',
                    'deviceName': 'Python-Client',
                    'did': '',
                    'reqid': '2',
                }
                if self.debug:
                    log.debug(f"[DEBUG] WebSocket 登录请求: user={self.nas_user}")
                await ws.send(json.dumps(login_msg))
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                if self.debug:
                    safe_resp = {k: (v[:20] + '...' if isinstance(v, str) and len(v) > 20 else v) for k, v in resp.items()}
                    log.debug(f"[DEBUG] WebSocket 登录响应: {json.dumps(safe_resp, ensure_ascii=False)}")

                if resp.get('result') == 'succ':
                    return resp['token']
                raise Exception(f"登录失败: {resp}")

        return asyncio.run(_do_login())

    # ============ 获取 pan_auth ============

    def _get_pan_auth(self) -> str:
        """从迅雷 HTML 页面提取 pan_auth JWT"""
        url = f'{self.xunlei_base}/'
        if self.debug:
            log.debug(f"[DEBUG] _get_pan_auth GET {url}")
        req = urllib.request.Request(url)
        req.add_header('Cookie', f'fnos-token={self.fnos_token}')
        resp = self.opener.open(req, timeout=10)
        html = resp.read().decode('utf-8')
        if self.debug:
            log.debug(f"[DEBUG] _get_pan_auth HTML 长度: {len(html)}, 前200字: {html[:200]}")

        match = re.search(r'function uiauth\(value\)\{\s*return\s*"([^"]+)"', html)
        if match:
            if self.debug:
                log.debug(f"[DEBUG] _get_pan_auth 提取成功, pan_auth 前20字: {match.group(1)[:20]}...")
            return match.group(1)
        if self.debug:
            log.debug(f"[DEBUG] _get_pan_auth 未找到 uiauth 函数")
        raise Exception("pan_auth 提取失败")

    # ============ 完整初始化 ============

    def init(self) -> bool:
        """
        完整初始化流程：
        1. 尝试加载已保存的凭据
        2. 验证凭据有效性
        3. 如果无效，通过纯 HTTP/WebSocket 获取
        """
        if self._load_saved():
            log.info("已加载保存的凭据，验证中...")
            if self._test_auth():
                log.info("凭据有效")
                return True
            log.warning("凭据已过期，重新获取...")

        # 通过 WebSocket 登录获取 fnos-token
        try:
            log.info("WebSocket 登录 NAS...")
            self.fnos_token = self._ws_login()
            log.info(f"fnos-token 获取成功")
        except Exception as e:
            log.error(f"NAS 登录失败: {e}")
            return False

        # 获取 pan_auth
        try:
            log.info("获取 pan_auth...")
            self.pan_auth = self._get_pan_auth()
            log.info("pan_auth 获取成功")
        except Exception as e:
            log.error(f"pan_auth 获取失败: {e}")
            return False

        self._save_cred()
        log.info("凭据获取并保存成功")
        return True

    # ============ API 调用 ============

    def _test_nas_cookie(self) -> bool:
        """测试 NAS cookie 是否有效"""
        if not self.fnos_token:
            return False
        try:
            url = f"{self.xunlei_base}/device/now"
            if self.debug:
                log.debug(f"[DEBUG] _test_nas_cookie GET {url}")
            req = urllib.request.Request(url)
            req.add_header("Cookie", f"fnos-token={self.fnos_token}")
            resp = self.opener.open(req, timeout=10)
            body = resp.read()
            data = json.loads(body)
            if self.debug:
                log.debug(f"[DEBUG] _test_nas_cookie 响应: {json.dumps(data, ensure_ascii=False)[:500]}")
            return "now" in data
        except Exception as e:
            if self.debug:
                log.debug(f"[DEBUG] _test_nas_cookie 异常: {e}")
            return False

    def _test_auth(self) -> bool:
        """测试整体认证是否有效"""
        return self._test_nas_cookie() and self._test_pan_auth()

    def _test_pan_auth(self) -> bool:
        """测试 pan_auth 是否有效"""
        if not self.pan_auth:
            return False
        try:
            params = {
                "pan_auth": self.pan_auth,
                "device_space": "",
                "type": "user#runner",
            }
            url = f"{self.xunlei_base}/drive/v1/tasks?{urllib.parse.urlencode(params)}"
            if self.debug:
                log.debug(f"[DEBUG] _test_pan_auth GET {url}")
            req = urllib.request.Request(url)
            req.add_header("Cookie", f"fnos-token={self.fnos_token}; XLA_CI=")
            resp = self.opener.open(req, timeout=10)
            body = resp.read()
            data = json.loads(body)
            if self.debug:
                log.debug(f"[DEBUG] _test_pan_auth 响应: {json.dumps(data, ensure_ascii=False)[:500]}")
            return "tasks" in data
        except Exception as e:
            if self.debug:
                log.debug(f"[DEBUG] _test_pan_auth 异常: {e}")
            return False

    def _api_get(self, path: str, extra_params: dict = None) -> dict:
        params = {
            "pan_auth": self.pan_auth,
            "device_space": "",
        }
        if extra_params:
            params.update(extra_params)

        sep = "&" if "?" in path else "?"
        url = f"{self.xunlei_base}{path}{sep}{urllib.parse.urlencode(params, doseq=True)}"
        if self.debug:
            log.debug(f"[DEBUG] API GET {url}")
        req = urllib.request.Request(url)
        req.add_header("Cookie", f"fnos-token={self.fnos_token}; XLA_CI={self.xla_ci or ''}")

        resp = self.opener.open(req, timeout=30)
        body = resp.read()
        if self.debug:
            log.debug(f"[DEBUG] API GET 响应 (HTTP {resp.status}): {body.decode('utf-8', errors='replace')[:500]}")
        return json.loads(body)

    def _api_post(self, path: str, body: dict = None, extra_params: dict = None, method: str = "POST") -> dict:
        params = {
            "pan_auth": self.pan_auth,
            "device_space": "",
        }
        if extra_params:
            params.update(extra_params)

        # 处理 path 已带 ? 的情况（如 /drive/v1/tasks?task_ids=xxx）
        sep = "&" if "?" in path else "?"
        url = f"{self.xunlei_base}{path}{sep}{urllib.parse.urlencode(params, doseq=True)}"
        data = json.dumps(body or {}).encode("utf-8") if body else None
        if self.debug:
            log.debug(f"[DEBUG] API {method} {url}")
            if data:
                log.debug(f"[DEBUG] 请求体: {data.decode('utf-8', errors='replace')[:500]}")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Cookie", f"fnos-token={self.fnos_token}; XLA_CI={self.xla_ci or ''}")
        req.add_header("Content-Type", "application/json")

        resp = self.opener.open(req, timeout=30)
        body = resp.read()
        if self.debug:
            log.debug(f"[DEBUG] API {method} 响应 (HTTP {resp.status}): {body.decode('utf-8', errors='replace')[:500]}")
        return json.loads(body)

    # ============ 业务功能 ============

    def list_tasks(self, status: str = "active") -> List[Dict]:
        """获取下载任务列表"""
        phase_map = {
            "active": "PHASE_TYPE_PENDING,PHASE_TYPE_RUNNING,PHASE_TYPE_PAUSED,PHASE_TYPE_ERROR",
            "completed": "PHASE_TYPE_COMPLETE",
            "all": "",
        }

        filters = {}
        if status in phase_map and phase_map[status]:
            filters["phase"] = {"in": phase_map[status]}
        filters["type"] = {"in": "user#download-url,user#download"}

        params = {
            "space": self.device_space or "device_id#8d842bc20de9d63e49eb0cc7ebaea16e",
            "limit": "100",
            "filters": json.dumps(filters),
        }

        data = self._api_get("/drive/v1/tasks", params)
        return data.get("tasks", [])

    def add_download(self, url: str, name: str = "", target_dir: str = "") -> Optional[str]:
        """
        添加下载任务（通过 Playwright 操作迅雷 Web 界面）

        参数:
            url: 磁力链接或 HTTP 下载链接
            name: 任务名称（可选）
            target_dir: 下载目录（NAS 真实路径，可选）

        返回: "ok" 或 None
        """
        log.info(f"添加下载任务: {url[:60]}...")

        effective_dir = target_dir or self.download_path

        try:
            from xunlei_playwright import XunleiPlaywright
            pw = XunleiPlaywright(
                xunlei_url=self.xunlei_base,
                fnos_token=self.fnos_token or "",
                download_path=effective_dir,
                filter_files=self.filter_files,
            )
            success = pw.add_download(url, name=name)
            if success:
                log.info("Playwright 下载任务提交成功")
                return "ok"
            log.error("Playwright 下载任务提交失败")
            return None
        except Exception as e:
            log.error(f"Playwright 下载异常: {e}")
            return None

    def wait_task(self, task_id: str, timeout: int = 3600, poll_interval: int = 10) -> str:
        """等待任务完成，返回: "completed" / "error" / "timeout" """
        log.info(f"等待任务完成: {task_id}")
        start = time.time()

        while time.time() - start < timeout:
            try:
                data = self._api_get(f"/drive/v1/tasks/{task_id}")
                phase = data.get("phase", "")
                progress = data.get("progress", {})
                pct = progress.get("progress", 0)

                if phase == "PHASE_TYPE_COMPLETE":
                    log.info(f"任务已完成: {task_id}")
                    return "completed"
                elif phase in ("PHASE_TYPE_ERROR", "PHASE_TYPE_FAIL"):
                    log.error(f"任务失败: {phase}")
                    return "error"
                else:
                    log.debug(f"任务进行中: {phase} ({pct}%)")

            except Exception as e:
                log.warning(f"查询任务状态失败: {e}")

            time.sleep(poll_interval)

        log.warning(f"等待超时: {task_id}")
        return "timeout"

    def get_storage_info(self) -> dict:
        """获取存储空间信息"""
        try:
            return self._api_get("/drive/v1/about")
        except Exception as e:
            return {"error": str(e)}

    def get_flow_info(self) -> dict:
        """获取流量信息"""
        try:
            return self._api_get("/flow/v1/about", {"scene": "DownloadUrl"})
        except Exception as e:
            return {"error": str(e)}

    def is_video_file(self, filename: str) -> bool:
        """判断是否为视频文件"""
        lower = filename.lower()
        return any(lower.endswith(ext) for ext in self.video_extensions)


# ============ CLI 测试 ============

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    downloader = XunleiDownloader(
        nas_host="192.168.123.146",
        nas_port=5666,
        nas_user="shield",
        nas_pass="2261112Chd..",
    )

    if not downloader.init():
        print("❌ 初始化失败")
        sys.exit(1)

    print("✅ 初始化成功")

    # 列出下载任务
    tasks = downloader.list_tasks("active")
    print(f"\n活跃任务: {len(tasks)}")
    for t in tasks:
        print(f"  - {t.get('name', 'N/A')}: {t.get('phase', 'N/A')}")

    # 存储信息
    info = downloader.get_storage_info()
    print(f"\n存储信息: {json.dumps(info, indent=2)[:200]}")
