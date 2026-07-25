#!/usr/bin/env python3
"""
NAS 迅雷下载 API 客户端
纯 HTTP API，不依赖浏览器，可放 Docker 运行
"""

import json
import time
import logging
import asyncio
from pathlib import Path
from typing import Optional
import urllib.request
import urllib.parse
import urllib.error
import http.cookiejar

# ============ 配置 ============

NAS_HOST = "192.168.123.146"
NAS_PORT = 5666
NAS_USER = "shield"
NAS_PASS = "2261112Chd.."

XUNLEI_BASE = f"http://{NAS_HOST}:{NAS_PORT}/cgi/ThirdParty/xunlei/index.cgi"

# ============ 日志 ============

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("xunlei")


class NasXunleiClient:
    """NAS 迅雷下载 API 客户端"""

    def __init__(self, data_dir: str = None):
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

        # 加载已保存的凭据
        self._load_saved()

    # ============ 凭据管理 ============

    def _cred_file(self) -> Path:
        return self.data_dir / "credentials.json"

    def _load_saved(self):
        """从文件加载已保存的凭据"""
        f = self._cred_file()
        if not f.exists():
            return
        try:
            data = json.loads(f.read_text())
            self.fnos_token = data.get("fnos_token")
            self.xla_ci = data.get("xla_ci")
            self.pan_auth = data.get("pan_auth")
            self.device_space = data.get("device_space", "")
            log.info("已加载保存的凭据")
        except Exception as e:
            log.warning(f"加载凭据失败: {e}")

    def _save_cred(self):
        """保存凭据到文件"""
        data = {
            "fnos_token": self.fnos_token,
            "xla_ci": self.xla_ci,
            "pan_auth": self.pan_auth,
            "device_space": self.device_space,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._cred_file().write_text(json.dumps(data, indent=2))
        log.info("凭据已保存")

    # ============ 完整初始化 ============

    def init(self) -> bool:
        """完整初始化：登录 NAS + 获取 pan_auth"""
        if not self.login_nas():
            return False
        if not self.get_pan_auth():
            return False
        self._save_cred()
        return True

    # ============ NAS 登录 ============

    def login_nas(self) -> bool:
        """
        通过 WebSocket 登录 fnOS，获取 fnos-token。
        纯 API 方式，无需浏览器。
        """
        log.info("尝试 NAS 登录...")

        if self.fnos_token:
            log.info("已有 NAS cookie，尝试验证...")
            if self._test_nas_cookie():
                log.info("NAS cookie 有效")
                return True
            else:
                log.warning("NAS cookie 已过期，重新登录...")

        # 通过 WebSocket 登录
        try:
            self.fnos_token = self._ws_login()
            log.info("NAS 登录成功")
            return True
        except Exception as e:
            log.error(f"NAS 登录失败: {e}")
            return False

    def _ws_login(self) -> str:
        """通过 WebSocket 登录 fnOS，返回 fnos-token"""
        try:
            import websockets
        except ImportError:
            raise Exception("需要安装 websockets: pip install websockets")

        async def _do_login():
            url = f'ws://{NAS_HOST}:{NAS_PORT}/websocket?type=main'
            headers = {'Origin': f'http://{NAS_HOST}:{NAS_PORT}'}

            async with websockets.connect(url, additional_headers=headers) as ws:
                # 获取 SI
                await ws.send(json.dumps({'req': 'util.getSI', 'reqid': '1'}))
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                si = resp['si']

                # 登录（不加密）
                await ws.send(json.dumps({
                    'req': 'user.login',
                    'user': NAS_USER,
                    'password': NAS_PASS,
                    'si': si,
                    'stay': 2,
                    'deviceType': 'web',
                    'deviceName': 'Python-Client',
                    'did': '',
                    'reqid': '2',
                }))
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))

                if resp.get('result') == 'succ':
                    return resp['token']
                raise Exception(f"登录失败: {resp}")

        import asyncio
        return asyncio.run(_do_login())

    def _test_nas_cookie(self) -> bool:
        """测试 NAS cookie 是否有效"""
        try:
            req = urllib.request.Request(f"{XUNLEI_BASE}/device/now")
            req.add_header("Cookie", f"fnos-token={self.fnos_token}; XLA_CI={self.xla_ci}")
            resp = self.opener.open(req, timeout=10)
            data = json.loads(resp.read())
            return "now" in data
        except Exception:
            return False

    # ============ 迅雷 Token ============

    def get_pan_auth(self) -> Optional[str]:
        """
        获取迅雷 pan_auth token。
        从迅雷 HTML 页面提取 uiauth 函数返回的 JWT。
        """
        if self.pan_auth and self._test_pan_auth():
            return self.pan_auth

        log.info("pan_auth 无效或过期，从页面提取...")
        try:
            self.pan_auth = self._fetch_pan_auth()
            log.info("pan_auth 获取成功")
            return self.pan_auth
        except Exception as e:
            log.error(f"pan_auth 获取失败: {e}")
            return None

    def _fetch_pan_auth(self) -> str:
        """从迅雷 HTML 页面提取 pan_auth JWT"""
        import re
        url = f'{XUNLEI_BASE}/'
        req = urllib.request.Request(url)
        req.add_header('Cookie', f'fnos-token={self.fnos_token}')
        resp = self.opener.open(req, timeout=10)
        html = resp.read().decode('utf-8')

        match = re.search(r'function uiauth\(value\)\{\s*return\s*"([^"]+)"', html)
        if match:
            return match.group(1)
        raise Exception("pan_auth 提取失败")

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
            url = f"{XUNLEI_BASE}/drive/v1/tasks?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(url)
            req.add_header("Cookie", f"fnos-token={self.fnos_token}; XLA_CI={self.xla_ci}")
            resp = self.opener.open(req, timeout=10)
            data = json.loads(resp.read())
            return "tasks" in data
        except Exception:
            return False

    # ============ API 调用 ============

    def _api_get(self, path: str, extra_params: dict = None) -> dict:
        """GET 请求迅雷 API"""
        params = {
            "pan_auth": self.pan_auth,
            "device_space": "",
        }
        if extra_params:
            params.update(extra_params)

        url = f"{XUNLEI_BASE}{path}?{urllib.parse.urlencode(params, doseq=True)}"
        req = urllib.request.Request(url)
        req.add_header("Cookie", f"fnos-token={self.fnos_token}; XLA_CI={self.xla_ci}")

        resp = self.opener.open(req, timeout=30)
        return json.loads(resp.read())

    def _api_post(self, path: str, body: dict = None, extra_params: dict = None) -> dict:
        """POST 请求迅雷 API"""
        params = {
            "pan_auth": self.pan_auth,
            "device_space": "",
        }
        if extra_params:
            params.update(extra_params)

        url = f"{XUNLEI_BASE}{path}?{urllib.parse.urlencode(params, doseq=True)}"
        data = json.dumps(body or {}).encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Cookie", f"fnos-token={self.fnos_token}; XLA_CI={self.xla_ci}")
        req.add_header("Content-Type", "application/json")

        resp = self.opener.open(req, timeout=30)
        return json.loads(resp.read())

    # ============ 业务功能 ============

    def list_tasks(self, status: str = "active") -> list:
        """
        获取下载任务列表

        status:
          - "active": 进行中+暂停+出错的
          - "completed": 已完成的
          - "all": 全部
        """
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
            "space": self.device_space or f"device_id#{self._get_device_id()}",
            "limit": "100",
            "filters": json.dumps(filters),
        }

        data = self._api_get("/drive/v1/tasks", params)
        return data.get("tasks", [])

    def get_task(self, task_id: str) -> dict:
        """获取单个任务详情"""
        data = self._api_get(f"/drive/v1/tasks/{task_id}")
        return data

    def add_task(self, url: str, download_path: str = "") -> dict:
        """
        添加磁力/链接下载任务

        url: 磁力链接或 HTTP 下载链接
        download_path: 下载目录 (可选，默认用迅雷配置的默认目录)
        """
        body = {
            "type": "user#download-url",
            "params": {
                "url": url,
            },
        }
        if download_path:
            body["params"]["download_path"] = download_path

        try:
            data = self._api_post("/drive/v1/tasks", body)
            return data
        except Exception as e:
            log.error(f"添加任务失败: {e}")
            return {"error": str(e)}

    def delete_task(self, task_id: str, delete_files: bool = False) -> dict:
        """删除下载任务"""
        params = {}
        if delete_files:
            params["delete_files"] = "true"

        try:
            data = self._api_get(f"/drive/v1/tasks/{task_id}/delete", params)
            return data
        except Exception as e:
            log.error(f"删除任务失败: {e}")
            return {"error": str(e)}

    def pause_task(self, task_id: str) -> dict:
        """暂停任务"""
        try:
            return self._api_post(f"/drive/v1/tasks/{task_id}/pause")
        except Exception as e:
            log.error(f"暂停任务失败: {e}")
            return {"error": str(e)}

    def resume_task(self, task_id: str) -> dict:
        """恢复任务"""
        try:
            return self._api_post(f"/drive/v1/tasks/{task_id}/resume")
        except Exception as e:
            log.error(f"恢复任务失败: {e}")
            return {"error": str(e)}

    def get_device_info(self) -> dict:
        """获取设备信息"""
        try:
            return self._api_get("/device/info/watch", {
                "space": "",
            })
        except Exception as e:
            log.warning(f"获取设备信息失败: {e}")
            return {"error": str(e)}

    def get_config(self) -> dict:
        """获取迅雷配置"""
        return self._api_get("/device/v1/get_config", {
            "space": f"device_id#{self._get_device_id()}",
            "play_scenes": ["nfo", "download_list"],
        })

    def get_drive_about(self) -> dict:
        """获取存储空间信息"""
        return self._api_get("/drive/v1/about")

    def get_flow_about(self) -> dict:
        """获取流量信息"""
        return self._api_get("/flow/v1/about", {"scene": "DownloadUrl"})

    def _get_device_id(self) -> str:
        """获取设备 ID"""
        if self.device_space and "#" in self.device_space:
            return self.device_space.split("#")[1]
        # 从 API 获取
        try:
            data = self._api_get("/device/info/watch")
            # 从返回数据推断
            return self.device_space.split("#")[1] if "#" in self.device_space else ""
        except Exception:
            return ""

    # ============ 完整初始化流程 ============

    def init_from_browser(self) -> bool:
        """
        从浏览器提取凭据（需要先用浏览器登录 NAS 并打开迅雷）
        这是一次性操作，之后的 cookie 和 token 都会保存复用
        """
        import subprocess

        log.info("从浏览器提取凭据...")

        # 获取 cookies
        try:
            result = subprocess.run(
                ["openclaw", "browser", "evaluate", "--fn", "return document.cookie;"],
                capture_output=True, text=True, timeout=10
            )
            cookie_str = result.stdout.strip().strip('"')

            cookies = {}
            for item in cookie_str.split("; "):
                if "=" in item:
                    # 只在第一个 = 处分割，避免值中有 = 的情况
                    k, v = item.split("=", 1)
                    cookies[k] = v

            self.fnos_token = cookies.get("fnos-token") or cookies.get("fnos_token")
            self.xla_ci = cookies.get("XLA_CI")
            log.info(f"提取到 NAS cookies: fnos_token={bool(self.fnos_token)}, XLA_CI={bool(self.xla_ci)}")
        except Exception as e:
            log.error(f"提取 cookies 失败: {e}")
            return False

        # 获取 pan_auth (从 performance entries)
        try:
            js = '''
            const entries = performance.getEntriesByType("resource");
            const match = entries.map(e => e.name.match(/pan_auth=([^&]+)/)).find(m => m);
            return match ? match[1] : "";
            '''
            result = subprocess.run(
                ["openclaw", "browser", "evaluate", "--fn", js],
                capture_output=True, text=True, timeout=10
            )
            self.pan_auth = result.stdout.strip().strip('"')
            if self.pan_auth:
                log.info(f"提取到 pan_auth: {self.pan_auth[:30]}...")
        except Exception as e:
            log.warning(f"提取 pan_auth 失败: {e}")

        # 获取 device_space
        try:
            js = '''
            const entries = performance.getEntriesByType("resource");
            const match = entries.map(e => e.name.match(/space=([^&]+)/)).find(m => m);
            return match ? decodeURIComponent(match[1]) : "";
            '''
            result = subprocess.run(
                ["openclaw", "browser", "evaluate", "--fn", js],
                capture_output=True, text=True, timeout=10
            )
            self.device_space = result.stdout.strip().strip('"')
            if self.device_space:
                log.info(f"提取到 device_space: {self.device_space}")
        except Exception as e:
            log.warning(f"提取 device_space 失败: {e}")

        if self.fnos_token and self.pan_auth:
            self._save_cred()
            log.info("✅ 凭据提取并保存成功")
            return True

        log.error("凭据提取不完整")
        return False

    def status(self) -> dict:
        """获取综合状态"""
        result = {
            "has_fnos_token": bool(self.fnos_token),
            "has_xla_ci": bool(self.xla_ci),
            "has_pan_auth": bool(self.pan_auth),
            "device_space": self.device_space,
        }

        if self.fnos_token and self.pan_auth:
            try:
                result["nas_cookie_valid"] = self._test_nas_cookie()
                result["pan_auth_valid"] = self._test_pan_auth()
            except Exception:
                result["nas_cookie_valid"] = False
                result["pan_auth_valid"] = False

        return result


# ============ CLI ============

def main():
    import sys

    client = NasXunleiClient()

    if len(sys.argv) < 2:
        print("用法:")
        print("  python3 xunlei_api.py init        # 从浏览器提取凭据")
        print("  python3 xunlei_api.py status       # 查看状态")
        print("  python3 xunlei_api.py list         # 列出下载任务")
        print("  python3 xunlei_api.py add <url>    # 添加下载任务")
        print("  python3 xunlei_api.py info         # 设备/存储/流量信息")
        return

    cmd = sys.argv[1]

    if cmd == "init":
        ok = client.init()
        print("初始化成功" if ok else "初始化失败")

    elif cmd == "status":
        s = client.status()
        print(json.dumps(s, indent=2, ensure_ascii=False))

    elif cmd == "list":
        if not client.get_pan_auth():
            print("需要先 init 获取凭据")
            return
        tasks = client.list_tasks(sys.argv[2] if len(sys.argv) > 2 else "active")
        for t in tasks:
            name = t.get("file_name", t.get("name", "?"))
            phase = t.get("phase", "?")
            size = t.get("file_size", 0)
            tid = t.get("id", "?")
            print(f"  [{phase}] {name} ({size} bytes) id={tid}")
        if not tasks:
            print("  无任务")

    elif cmd == "add":
        if len(sys.argv) < 3:
            print("用法: python3 xunlei_api.py add <magnet_or_url>")
            return
        if not client.get_pan_auth():
            print("需要先 init 获取凭据")
            return
        result = client.add_task(sys.argv[2])
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif cmd == "info":
        if not client.get_pan_auth():
            print("需要先 init 获取凭据")
            return
        print("=== 设备信息 ===")
        print(json.dumps(client.get_device_info(), indent=2, ensure_ascii=False))
        print("\n=== 存储信息 ===")
        print(json.dumps(client.get_drive_about(), indent=2, ensure_ascii=False))
        print("\n=== 流量信息 ===")
        print(json.dumps(client.get_flow_about(), indent=2, ensure_ascii=False))

    else:
        print(f"未知命令: {cmd}")


if __name__ == "__main__":
    main()
