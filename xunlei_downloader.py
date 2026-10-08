#!/usr/bin/env python3
"""
迅雷下载核心模块（纯 HTTP/WebSocket 版本）
功能：登录NAS → 获取迅雷凭证 → API 调用 → 监控状态
可被其他脚本 import 使用，无需浏览器依赖

依赖: pip install websockets
"""

import json
import os
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


class XunleiAuthError(Exception):
    """迅雷凭据（pan_auth）失效且刷新失败"""


class XunleiDownloader:
    """迅雷下载器（纯 HTTP 版本）"""

    def __init__(self, nas_host: str, nas_port: int, nas_user: str, nas_pass: str,
                 download_path: str = "", data_dir: str = None, filter_files: bool = False,
                 debug: bool = False, task_source: str = "api",
                 min_video_size_mb: float = 0):
        self.nas_host = nas_host
        self.nas_port = nas_port
        self.nas_user = nas_user
        self.nas_pass = nas_pass
        self.download_path = download_path
        self.filter_files = filter_files
        self.debug = debug
        self.task_source = task_source
        # 视频文件最小体积（MB）：低于此值的视频文件不下载（0=不过滤）
        # 仅作用于视频；字幕/nfo 等小文件不受影响
        self.min_video_size_mb = float(min_video_size_mb or 0)

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
        # 迅雷"空间"标识（= 设备 target），形如 device_id#<md5>。
        # 运行时从 /drive/v1/tasks?type=user#runner 自动获取，绝不硬编码。
        self.target: str = ""
        self.device_name: str = ""

        self.video_extensions = {
            '.mkv', '.mp4', '.avi', '.rmvb', '.rm', '.wmv', '.flv',
            '.mov', '.ts', '.m4v', '.webm', '.vob', '.mpg', '.mpeg',
            '.3gp', '.f4v', '.ogv', '.iso',
        }
        self.subtitle_extensions = {
            '.srt', '.ass', '.ssa', '.sub', '.idx', '.sup',
        }
        self.info_extensions = {
            '.nfo', '.txt', '.jpg', '.jpeg', '.png',
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
        4. 解析设备 target（space）
        """
        if self._load_saved():
            log.info("已加载保存的凭据，验证中...")
            if self._test_auth():
                log.info("凭据有效")
                self.target = self._resolve_device_target()
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
        self.target = self._resolve_device_target()
        return True

    # ============ 设备空间（target / space）============

    def _relogin(self) -> bool:
        """
        重新获取 fnos-token 与 pan_auth（pan_auth 约 1 小时过期）。

        返回 True 表示刷新成功。用于 API 调用检测到 "invalid token" 时自动自愈。
        """
        log.warning("pan_auth 失效，重新登录 NAS 刷新凭据...")
        try:
            self.fnos_token = self._ws_login()
            self.pan_auth = self._get_pan_auth()
        except Exception as e:
            log.error(f"刷新凭据失败: {e}")
            return False
        self._save_cred()
        if not self.target:
            self.target = self._resolve_device_target()
        log.info("凭据已刷新")
        return True

    def _resolve_device_target(self) -> str:
        """
        解析迅雷"空间"标识（space / target）。

        取值来源是设备注册任务（type=user#runner）的 params.target，
        形如 device_id#<md5>；不可硬编码（每台设备不同）。
        """
        try:
            data = self._api_get("/drive/v1/tasks", {"type": "user#runner", "limit": "20"})
        except Exception as e:
            log.warning(f"获取设备信息失败: {e}")
            return ""
        for t in data.get("tasks", []):
            params = t.get("params") or {}
            target = params.get("target") or ""
            if target:
                self.device_name = t.get("name", "") or ""
                log.info(f"迅雷设备: {self.device_name} ({target})")
                return target
        log.warning("未找到迅雷设备注册信息（target 为空）")
        return ""

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
                "space": self.target or "",
                "type": "user#runner",
            }
            url = f"{self.xunlei_base}/drive/v1/tasks?{urllib.parse.urlencode(params)}"
            if self.debug:
                log.debug(f"[DEBUG] _test_pan_auth GET {url}")
            req = urllib.request.Request(url)
            req.add_header("Cookie", f"fnos-token={self.fnos_token}; XLA_CI=")
            body = self._open(req)
            if self._is_auth_invalid(body):
                return False
            data = json.loads(body)
            if self.debug:
                log.debug(f"[DEBUG] _test_pan_auth 响应: {json.dumps(data, ensure_ascii=False)[:500]}")
            return "tasks" in data
        except Exception as e:
            if self.debug:
                log.debug(f"[DEBUG] _test_pan_auth 异常: {e}")
            return False

    def _api_get(self, path: str, extra_params: dict = None, _retry: bool = True) -> dict:
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

        body = self._open(req)
        if self.debug:
            log.debug(f"[DEBUG] API GET 响应: {body.decode('utf-8', errors='replace')[:500]}")

        # pan_auth 失效时迅雷返回 403 或纯文本 "invalid token"。
        # 此时自动重新登录并重试一次，避免监控循环被刷屏报错。
        if self._is_auth_invalid(body):
            if _retry and self._relogin():
                return self._api_get(path, extra_params, _retry=False)
            raise XunleiAuthError("pan_auth 失效且刷新失败")
        return json.loads(body)

    def _api_post(self, path: str, body: dict = None, extra_params: dict = None,
                  method: str = "POST", _retry: bool = True) -> dict:
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

        body_bytes = self._open(req)
        if self.debug:
            log.debug(f"[DEBUG] API {method} 响应: {body_bytes.decode('utf-8', errors='replace')[:500]}")

        if self._is_auth_invalid(body_bytes):
            if _retry and self._relogin():
                return self._api_post(path, body, extra_params, method, _retry=False)
            raise XunleiAuthError("pan_auth 失效且刷新失败")
        return json.loads(body_bytes)

    def _open(self, req) -> bytes:
        """
        统一发起请求。

        pan_auth 失效时迅雷返回 HTTP 401/403（响应体可能为空或纯文本 "invalid token"）。
        这里把这类响应体返回给调用方（而不是抛异常），以便自动刷新凭据后重试。
        """
        try:
            resp = self.opener.open(req, timeout=30)
            return resp.read()
        except urllib.error.HTTPError as e:
            body = e.read() if hasattr(e, "read") else b""
            if e.code in (401, 403):
                return body if body.strip() else b"invalid token"
            raise

    @staticmethod
    def _is_auth_invalid(body: bytes) -> bool:
        """
        判断响应是否为凭据失效。

        迅雷实际返回 HTTP 200 + JSON，形如：
          {"error":"permission_deny: checkAuth failed:token contains an invalid
           number of segments token:xxx","error_code":403,"HttpStatus":0}
        兼容纯文本 "invalid token" 与 HTTP 401/403。
        """
        try:
            text = body.decode("utf-8", errors="replace").strip()
        except Exception:
            return False
        if text == "invalid token" or text.startswith("invalid token"):
            return True
        if not text.startswith("{"):
            return False
        try:
            data = json.loads(text)
        except Exception:
            return False
        if not isinstance(data, dict):
            return False
        if data.get("error_code") in (401, 403):
            return True
        err = str(data.get("error") or "")
        return "checkAuth failed" in err or "permission_deny" in err

    # ============ 业务功能 ============

    def list_tasks(self, status: str = "active") -> List[Dict]:
        """获取下载任务列表（根据 task_source 配置选择 API 或 Playwright）"""
        if self.task_source == "playwright":
            return self._list_tasks_playwright()
        return self._list_tasks_api(status)

    def _list_tasks_playwright(self) -> List[Dict]:
        """通过 Playwright 读取任务列表"""
        try:
            from xunlei_playwright import XunleiPlaywright
            pw = XunleiPlaywright(
                xunlei_url=self.xunlei_base,
                fnos_token=self.fnos_token or "",
            )
            return pw.list_tasks()
        except Exception as e:
            log.warning(f"Playwright 读取任务失败: {e}")
            return []

    def _list_tasks_api(self, status: str = "active") -> List[Dict]:
        """
        通过 API 读取任务列表。

        实测坑（2026-10-08 在真机验证）：
          - 顶层 `type` 参数用逗号多值（如 "a,b"）会返回 0 条；只有单值可用。
          - `filters` 里 `phase.in` 用逗号多值同样返回 0 条。
        因此这里只按 `filters.type.in` 过滤（实测 76 条正常），
        相位过滤一律放到本地做，保证结果稳定。
        """
        params = {
            "space": self.target or "",
            "limit": "100",
            "filters": json.dumps({"type": {"in": "user#download-url,user#download"}}),
        }
        data = self._api_get("/drive/v1/tasks", params)
        tasks = data.get("tasks", [])

        if status == "active":
            active = {"PHASE_TYPE_PENDING", "PHASE_TYPE_RUNNING", "PHASE_TYPE_PAUSED",
                      "PHASE_TYPE_ERROR"}
            tasks = [t for t in tasks if t.get("phase") in active]
        elif status == "completed":
            tasks = [t for t in tasks if t.get("phase") == "PHASE_TYPE_COMPLETE"]
        # status == "all" → 原样返回
        return tasks

    # ============ 链接解析 / 文件树 ============

    def parse_url(self, url: str) -> Optional[Dict]:
        """
        解析磁力/下载链接，返回文件树（纯 API，等价于网页"确定解析链接"）。

        返回: {"list_id": str, "resources": [...]} 或 None
        """
        try:
            data = self._api_post("/drive/v1/resource/list",
                                  {"urls": url, "page_size": 2000})
        except Exception as e:
            log.error(f"解析链接失败: {e}")
            return None

        resources = (data.get("list") or {}).get("resources") or []
        if not resources:
            log.error("解析结果为空（链接无效或已被迅雷拒绝）")
            return None

        # 大种子分页：顶层首个资源带 dir.next_page_token 时继续拉取
        first = resources[0] if resources else {}
        d = first.get("dir") or {}
        token = d.get("next_page_token")
        list_id = data.get("list_id") or ""
        while token and list_id:
            try:
                page = self._api_get(f"/drive/v1/resource/list/{list_id}",
                                     {"page_token": token})
            except Exception as e:
                log.warning(f"分页拉取文件树失败: {e}")
                break
            res = (page.get("list") or {}).get("resources") or []
            if not res:
                break
            d["resources"] = (d.get("resources") or []) + res
            token = (page.get("list") or {}).get("next_page_token") or ""
        return {"list_id": list_id, "resources": resources}

    @staticmethod
    def _is_dir_node(node: Dict) -> bool:
        return bool(node.get("is_dir")) or \
            (node.get("meta") or {}).get("mime_type") == "dir" or \
            node.get("kind") == "drive#folder"

    def flatten_resources(self, resources: List[Dict]) -> List[Dict]:
        """
        按 DFS 先序拍平文件树。

        关键：迅雷的 file_index 是「文件在整个种子中的全局顺序（从 0 起）」，
        与网页端 flatten 结果一致；首个文件可能缺省 file_index（= 0）。
        """
        out: List[Dict] = []

        def walk(nodes: List[Dict], root_id: str):
            for n in nodes:
                out.append(n)
                d = n.get("dir") or {}
                if d.get("resources"):
                    walk(d["resources"], root_id)

        for top in resources:
            walk([top], top.get("id") or "")
        return out

    @staticmethod
    def _ext(filename: str) -> str:
        idx = str(filename or "").rfind(".")
        return str(filename)[idx:].lower() if idx >= 0 else ""

    def select_file_indices(self, flat: List[Dict]) -> Optional[List[int]]:
        """
        计算要下载的文件 file_index 列表（等价于网页端勾选文件）。

        规则（与 xunlei_playwright 的 _filter_files 完全一致，另加体积过滤）：
          - 视频 / 字幕 / 信息文件（nfo/txt/jpg/png）保留
          - 视频文件小于 MIN_VIDEO_SIZE_MB 时跳过（如样片、花絮小片段）
          - 其余取消勾选
          - 没有任何「合格」视频文件 → 返回 None（放弃该任务）

        filter_files=False 时全部保留。
        """
        files = [n for n in flat if not self._is_dir_node(n)]
        if not files:
            return None

        min_bytes = int(self.min_video_size_mb * 1024 * 1024)
        video_count = 0
        selected: List[int] = []
        skipped_small: List[str] = []
        for global_idx, node in enumerate(files):
            # 迅雷返回的 file_index 即全局序号；缺省视为 0
            fi = node.get("file_index")
            fi = global_idx if fi in (None, "") else int(fi)
            name = node.get("name") or node.get("file_name") or ""
            ext = self._ext(name)
            if ext in self.video_extensions:
                # 体积过滤：仅对视频生效（字幕/nfo 通常只有几十 KB，不应被误伤）
                size = node.get("file_size")
                try:
                    size = int(size) if size not in (None, "") else 0
                except (TypeError, ValueError):
                    size = 0
                if min_bytes > 0 and 0 < size < min_bytes:
                    skipped_small.append(f"{name} ({size / 1024 / 1024:.2f}MB)")
                    continue
                video_count += 1
                selected.append(fi)
                continue
            if not self.filter_files:
                selected.append(fi)
                continue
            if ext in self.subtitle_extensions or ext in self.info_extensions:
                selected.append(fi)
                continue
            # 其它格式：过滤掉

        if skipped_small:
            log.info(f"体积过滤: 跳过 {len(skipped_small)} 个小于 "
                     f"{self.min_video_size_mb}MB 的视频文件")
            for s in skipped_small[:5]:
                log.info(f"    ✂ {s}")
        log.info(f"文件解析: 共 {len(files)} 个文件，视频 {video_count} 个，"
                 f"选中 {len(selected)} 个")
        if self.filter_files and video_count == 0:
            log.warning("没有符合条件（体积/格式）的视频文件，放弃此任务")
            return None
        return selected

    # ============ 下载目录 ============

    def _list_folders(self, parent_id: str = "") -> List[Dict]:
        """列出指定父目录下的文件夹（纯 API）"""
        params = {
            "space": self.target or "",
            "limit": "200",
            "parent_id": parent_id,
            "filters": json.dumps({"kind": {"eq": "drive#folder"}}),
            "with": "withCategoryDiskMountPath,withCategoryDownloadPath",
        }
        data = self._api_get("/drive/v1/files", params)
        return data.get("files", []) or []

    def _resolve_download_dir(self, path: str) -> tuple:
        """
        把 NAS 路径解析为迅雷的 (parent_folder_id, parent_folder_path)。

        逐层下钻：从迅雷的根下载目录里找到与目标路径前缀匹配的那个，
        再按剩余路径片段逐级匹配子目录。

        注意：返回值必须是「真实存在的那个目录」，id 与 path 必须一一对应。
        中途某级不存在时回退到上一级，并返回该级的真实路径（而非请求路径），
        否则会出现 id/path 不一致导致迅雷把文件放到意料之外的位置。
        """
        if not path:
            return "", ""
        segs = [s for s in path.strip("/").split("/") if s]
        if not segs:
            return "", ""

        try:
            roots = self._list_folders("")
        except Exception as e:
            log.warning(f"获取下载根目录失败: {e}")
            return "", ""

        # 找前缀匹配的根目录（别名路径优先）
        for r in roots:
            p = r.get("params") or {}
            alias = (p.get("AliasPath") or r.get("name") or "").rstrip("/")
            if not alias:
                continue
            a_segs = [s for s in alias.strip("/").split("/") if s]
            if a_segs and segs[:len(a_segs)] == a_segs:
                cur_id = r.get("id")
                cur_path = "/" + "/".join(a_segs)   # 真实已解析路径
                remaining = segs[len(a_segs):]
                log.info(f"匹配根目录: {alias} → id={cur_id}，剩余子目录 {remaining}")
                for seg in remaining:
                    try:
                        children = self._list_folders(cur_id)
                    except Exception as e:
                        log.warning(f"列出子目录失败: {e}")
                        break
                    nxt = None
                    for c in children:
                        cname = (c.get("name") or "").rstrip("/").split("/")[-1]
                        if cname == seg:
                            nxt = c
                            break
                    if not nxt:
                        log.warning(f"子目录 '{seg}' 不存在，回退到 {cur_path}")
                        break
                    cur_id = nxt.get("id")
                    np = (nxt.get("params") or {}).get("AliasPath")
                    cur_path = np.rstrip("/") if np else f"{cur_path}/{seg}"
                return cur_id, cur_path

        # 没匹配到根目录：留空用迅雷默认目录
        log.warning(f"未匹配到下载根目录，使用迅雷默认目录（目标: {path}）")
        return "", ""

    # ============ 添加任务 ============

    def add_download(self, url: str, name: str = "", target_dir: str = "") -> Optional[str]:
        """
        添加下载任务（纯 API，不再依赖浏览器）

        等价于网页端：新建任务 → 填链接 → 确定解析 → 勾选文件 → 选目录 → 立即下载

        参数:
            url: 磁力链接或 HTTP 下载链接
            name: 任务名称（可选）
            target_dir: 下载目录（NAS 真实路径，可选）

        返回: "ok" 或 None
        """
        log.info(f"添加下载任务(API): {url[:60]}...")

        # TASK_SOURCE=playwright 时保留旧的浏览器实现（兜底用）
        if self.task_source == "playwright":
            return self._add_download_playwright(url, name, target_dir)

        if not self.target:
            self.target = self._resolve_device_target()
        if not self.target:
            log.error("缺少迅雷设备标识(target)，无法创建任务")
            return None

        # --- Step 1: 解析链接 ---
        parsed = self.parse_url(url)
        if not parsed:
            return None
        flat = self.flatten_resources(parsed["resources"])
        files = [n for n in flat if not self._is_dir_node(n)]

        # --- Step 2: 文件过滤 ---
        indices = self.select_file_indices(flat)
        if indices is None:
            return None

        # --- Step 3: 下载目录 ---
        effective_dir = target_dir or self.download_path
        parent_folder_id, parent_folder_path = self._resolve_download_dir(effective_dir)

        # --- Step 4: 组装并提交 ---
        top = parsed["resources"][0] if parsed["resources"] else {}
        meta = top.get("meta") or {}
        first_file = files[0] if files else {}
        first_meta = first_file.get("meta") or {}

        task_name = name or top.get("name") or first_file.get("name") or "unnamed"
        task_name = str(task_name).strip().replace("\n", "")
        file_name = str(top.get("name") or first_file.get("name") or task_name).strip()
        file_size = str(top.get("file_size") or first_file.get("file_size") or 0)
        total_file_count = len(files)

        params: Dict = {
            "url": url,
            "target": self.target,
            "total_file_count": str(total_file_count),
            "mime_type": (meta.get("mime_type") or first_meta.get("mime_type") or ""),
        }
        # 单文件任务不传 sub_file_index（与网页端一致）
        if total_file_count > 1:
            if len(indices) == total_file_count:
                params["sub_file_index"] = "-1"
            else:
                params["sub_file_index"] = ",".join(str(i) for i in indices)
        if parent_folder_id:
            params["parent_folder_id"] = parent_folder_id
            params["parent_folder_path"] = parent_folder_path

        body = {
            "type": "user#download-url",
            "name": task_name,
            "file_name": file_name,
            "file_size": file_size,
            "space": self.target,
            "params": params,
        }

        if self.debug:
            log.debug(f"[DEBUG] 创建任务请求体: {json.dumps(body, ensure_ascii=False)[:800]}")

        try:
            resp = self._api_post("/drive/v1/task", body)
        except Exception as e:
            log.error(f"创建任务失败: {e}")
            return None

        if self.debug:
            log.debug(f"[DEBUG] 创建任务响应: {json.dumps(resp, ensure_ascii=False)[:500]}")

        # 迅雷返回 {"HttpStatus":0, "task": {...}}；任务对象嵌在 task 键下
        task = resp.get("task") or {}
        task_id = task.get("id") or resp.get("id") or resp.get("task_id")
        if task_id or resp.get("HttpStatus") == 0:
            log.info(f"迅雷任务创建成功: id={task_id}")
            return task_id or "ok"
        log.error(f"迅雷任务创建被拒绝: {json.dumps(resp, ensure_ascii=False)[:300]}")
        return None

    def _add_download_playwright(self, url: str, name: str = "",
                                 target_dir: str = "") -> Optional[str]:
        """旧的浏览器实现（TASK_SOURCE=playwright 时启用，仅作兜底）"""
        effective_dir = target_dir or self.download_path
        try:
            from xunlei_playwright import XunleiPlaywright
            pw = XunleiPlaywright(
                xunlei_url=self.xunlei_base,
                fnos_token=self.fnos_token or "",
                download_path=effective_dir,
                filter_files=self.filter_files,
            )
            if pw.add_download(url, name=name):
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
        nas_host=os.environ.get("NAS_HOST", "192.168.1.100"),
        nas_port=int(os.environ.get("NAS_PORT", "5666")),
        nas_user=os.environ.get("NAS_USER", "admin"),
        nas_pass=os.environ.get("NAS_PASS", ""),
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
