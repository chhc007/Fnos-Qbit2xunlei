#!/usr/bin/env python3
"""
迅雷下载 Playwright 版本
通过 Playwright 操作迅雷 Web 界面来添加下载任务
支持两种模式：iframe 内操作（NAS桌面嵌入）和直接页面操作（独立URL）

依赖: pip install playwright && playwright install chromium
"""

import re
import time
import logging

log = logging.getLogger("xunlei_pw")


class XunleiPlaywright:
    """Playwright 版迅雷下载客户端"""

    VIDEO_EXTENSIONS = {
        '.mkv', '.mp4', '.avi', '.rmvb', '.rm', '.wmv', '.flv',
        '.mov', '.ts', '.m4v', '.webm', '.vob', '.mpg', '.mpeg',
        '.3gp', '.f4v', '.ogv', '.iso',
    }
    SUBTITLE_EXTENSIONS = {
        '.srt', '.ass', '.ssa', '.sub', '.idx', '.sup',
    }
    INFO_EXTENSIONS = {
        '.nfo', '.txt', '.jpg', '.jpeg', '.png',
    }

    def __init__(self, xunlei_url: str, fnos_token: str, download_path: str = "",
                 filter_files: bool = False):
        self.xunlei_url = xunlei_url
        self.fnos_token = fnos_token
        self.download_path = download_path
        self.filter_files = filter_files

    def add_download(self, magnet: str, name: str = "", max_retry: int = 2) -> bool:
        from playwright.sync_api import sync_playwright

        attempt = 0
        while attempt <= max_retry:
            attempt += 1
            log.info(f"尝试第 {attempt} 次 Playwright 下载...")

            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    context = browser.new_context()

                    # 设置 fnos-token cookie
                    cookie_domain = self.xunlei_url.split("//")[1].split(":")[0]
                    context.add_cookies([{
                        "name": "fnos-token",
                        "value": self.fnos_token,
                        "domain": cookie_domain,
                        "path": "/",
                    }])

                    page = context.new_page()

                    # 导航到迅雷 Web 页面
                    log.info(f"打开迅雷 Web: {self.xunlei_url}")
                    page.goto(self.xunlei_url, wait_until="networkidle", timeout=30000)
                    time.sleep(5)

                    target = page

                    time.sleep(2)

                    # --- 点击新建任务 ---
                    task_button = None
                    for _ in range(10):
                        task_button = target.query_selector('div.create__task')
                        if task_button:
                            break
                        time.sleep(1)

                    if not task_button:
                        log.error("新建任务按钮未找到")
                        browser.close()
                        continue

                    target.evaluate('(el) => el.scrollIntoView()', task_button)
                    target.evaluate('(el) => el.click()', task_button)
                    log.info("点击新建任务")
                    time.sleep(2)

                    # --- 填写磁力链接 ---
                    textarea_selector = 'textarea.el-textarea__inner[placeholder*="请添加下载链接"]'
                    target.wait_for_selector(textarea_selector, timeout=5000)
                    target.fill(textarea_selector, magnet)
                    log.info(f"填写下载链接: {magnet[:60]}...")

                    # --- 确定解析链接 ---
                    submit_selector = 'button.task-parse-btn:has-text("确定")'
                    target.wait_for_selector(submit_selector, timeout=5000)
                    target.evaluate('(el) => el.click()', target.query_selector(submit_selector))
                    log.info("点击确定解析链接")
                    time.sleep(4)

                    # --- 文件过滤（可选） ---
                    if self.filter_files:
                        if not self._filter_files(target):
                            log.error("没有视频文件，放弃此任务")
                            # 关闭弹窗
                            close_btn = target.query_selector('.nas-task-dialog .el-dialog__headerbtn')
                            if close_btn:
                                close_btn.click()
                            browser.close()
                            continue

                    # --- 选择下载目录（如有） ---
                    if self.download_path:
                        if not self._select_download_dir(target):
                            log.warning("目录选择失败，使用默认目录继续")

                    # --- 等待目录选择弹窗关闭（只等内层弹窗，主弹窗要保留） ---
                    self._wait_dir_dialog_closed(target)
                    time.sleep(1)

                    # --- 调试：截图看当前状态 ---
                    try:
                        page.screenshot(path="/tmp/xunlei_before_dl.png")
                        log.info("已保存下载前截图到 /tmp/xunlei_before_dl.png")
                        # 打印当前所有可见按钮
                        btns = target.query_selector_all('button')
                        for b in btns:
                            if b.is_visible():
                                log.info(f"  可见按钮: [{b.inner_text().strip()}] class={b.get_attribute('class')}")
                    except Exception as e:
                        log.debug(f"调试信息获取失败: {e}")

                    # --- 点击立即下载 ---
                    # 尝试多种选择器
                    dl_btn = None
                    selectors = [
                        'button.task-parse-btn:has-text("立即下载")',
                        'button:has-text("立即下载")',
                        '.nas-task-dialog button:has-text("立即下载")',
                        '.el-dialog__footer button:has-text("立即下载")',
                        'footer button:has-text("立即下载")',
                    ]
                    for sel in selectors:
                        try:
                            dl_btn = target.query_selector(sel)
                            if dl_btn and dl_btn.is_visible():
                                log.info(f"找到下载按钮: {sel}")
                                break
                            dl_btn = None
                        except Exception:
                            dl_btn = None

                    if dl_btn:
                        dl_btn.click(force=True)
                        log.info("点击立即下载，任务已提交")
                    else:
                        log.error("立即下载按钮未找到")
                        browser.close()
                        continue

                    time.sleep(5)

                    browser.close()
                    log.info("Playwright 下载任务提交成功")
                    return True

            except Exception as e:
                log.warning(f"Playwright 异常: {e}")

        log.error(f"达到最大重试次数 {max_retry}，任务失败")
        return False

    def _select_download_dir(self, target) -> bool:
        """
        选择下载目录。
        目录树中每个节点显示完整路径，如 /存储空间5/shield的文件/影视库/下载/迅雷下载影视
        需要找到基础路径节点，展开后逐层点入子目录。
        """
        try:
            # 点击目录选择图标
            dir_icon = target.query_selector('i.icon-a-xuanzemulu3x')
            if not dir_icon:
                log.warning("目录选择图标未找到")
                return False
            dir_icon.click()
            time.sleep(1)

            # 等待目录树弹窗
            target.wait_for_selector('.dialog-folder', timeout=5000)
            time.sleep(1)

            # 拆分下载路径
            path_parts = [p for p in self.download_path.strip("/").split("/") if p]
            if not path_parts:
                log.warning("下载路径为空")
                return False

            log.info(f"目标路径: {self.download_path}")
            log.info(f"路径片段: {path_parts}")

            # 策略：找到包含路径第一个片段的树节点（完整路径），然后逐层展开子目录
            # 树节点的 file_title 显示完整路径如 /存储空间5/shield的文件/影视库/下载/迅雷下载影视

            all_tree_nodes = target.query_selector_all('div.el-tree-node')
            base_node = None

            # 找基础路径节点：遍历所有节点，找 file_title 包含第一个路径片段的
            for node in all_tree_nodes:
                title_elem = node.query_selector('a.file_title')
                if not title_elem:
                    continue
                title_text = title_elem.inner_text().strip()
                # 匹配：标题包含路径的第一个片段
                if path_parts[0] in title_text:
                    base_node = node
                    log.info(f"找到基础路径节点: {title_text}")
                    break

            if not base_node:
                # 尝试匹配其他片段
                for part in path_parts:
                    for node in all_tree_nodes:
                        title_elem = node.query_selector('a.file_title')
                        if not title_elem:
                            continue
                        title_text = title_elem.inner_text().strip()
                        if part in title_text:
                            base_node = node
                            log.info(f"找到匹配节点 (片段 '{part}'): {title_text}")
                            break
                    if base_node:
                        break

            if not base_node:
                log.error(f"未找到匹配的基础路径节点，路径: {self.download_path}")
                # 打印所有可用节点方便调试
                for node in all_tree_nodes:
                    title_elem = node.query_selector('a.file_title')
                    if title_elem:
                        log.info(f"  可用节点: {title_elem.inner_text().strip()}")
                return False

            # 点击基础路径节点展开
            expand_icon = base_node.query_selector('i.el-tree-node__expand-icon')
            if expand_icon:
                # 检查是否已展开
                is_expanded = 'expanded' in (expand_icon.get_attribute('class') or '')
                if not is_expanded:
                    target.evaluate('(el) => el.click()', expand_icon)
                    log.info("展开基础路径节点")
                    time.sleep(1)

            # 点击基础路径节点标题（选中它）
            base_title = base_node.query_selector('a.file_title')
            if base_title:
                target.evaluate('(el) => el.click()', base_title)
                time.sleep(0.5)

            # 逐层点入子目录
            # 找到基础路径后面剩余的目录片段
            base_title_text = base_node.query_selector('a.file_title').inner_text().strip()
            # 去掉开头的 /
            base_path_clean = base_title_text.strip("/")

            remaining_parts = path_parts.copy()
            # 去掉已经在基础路径中匹配到的部分
            base_parts = [p for p in base_path_clean.split("/") if p]
            for bp in base_parts:
                if remaining_parts and remaining_parts[0] == bp:
                    remaining_parts.pop(0)
                elif remaining_parts and bp in remaining_parts[0]:
                    # 模糊匹配：base 里包含 part（比如 "影视库" 在 "影视库" 中）
                    remaining_parts.pop(0)

            log.info(f"剩余子目录: {remaining_parts}")

            for subdir in remaining_parts:
                time.sleep(0.5)
                # 重新获取所有节点（DOM 可能已更新）
                all_nodes = target.query_selector_all('div.el-tree-node')
                found = False
                for node in all_nodes:
                    title_elem = node.query_selector('a.file_title')
                    if not title_elem:
                        continue
                    title_text = title_elem.inner_text().strip()
                    # 精确匹配子目录名（不含路径）
                    node_name = title_text.strip("/").split("/")[-1] if "/" in title_text else title_text
                    if node_name == subdir or title_text == subdir:
                        # 展开（如果有子目录）
                        exp = node.query_selector('i.el-tree-node__expand-icon')
                        if exp:
                            is_exp = 'expanded' in (exp.get_attribute('class') or '')
                            if not is_exp:
                                target.evaluate('(el) => el.click()', exp)
                                time.sleep(0.8)
                        # 选中
                        target.evaluate('(el) => el.click()', title_elem)
                        found = True
                        log.info(f"  选中子目录: {subdir}")
                        break

                if not found:
                    log.warning(f"  子目录 '{subdir}' 未找到")
                    return False

            # 点击确定按钮（在目录选择弹窗的 footer 中）
            time.sleep(0.5)
            # 精确查找目录弹窗中的确定按钮
            confirm_btn = target.query_selector('.dialog-folder .el-dialog__footer button.cinema__button.primary')
            if not confirm_btn:
                # 兜底：找目录弹窗 footer 中所有按钮，找"确定"
                footer_btns = target.query_selector_all('.dialog-folder .el-dialog__footer button')
                for btn in footer_btns:
                    text = btn.inner_text().strip()
                    if text == "确定":
                        confirm_btn = btn
                        break
            if confirm_btn:
                confirm_btn.click()
                log.info("目录选择确认")
            else:
                log.warning("目录确定按钮未找到")

            time.sleep(1)
            return True

        except Exception as e:
            log.warning(f"选择下载目录异常: {e}")
            return False

    def _wait_dir_dialog_closed(self, target, timeout=5):
        """等待目录选择弹窗（dialog-folder）关闭，不影响主弹窗"""
        start = time.time()
        while time.time() - start < timeout:
            dir_dialog = target.query_selector('.dialog-folder')
            if not dir_dialog or not dir_dialog.is_visible():
                return
            time.sleep(0.3)
        log.warning("目录选择弹窗未关闭，强制继续")

    def _filter_files(self, target) -> bool:
        """
        过滤文件列表：只保留视频文件、字幕文件和信息文件。
        取消勾选不需要的文件。如果没有视频文件，返回 False。
        """
        try:
            time.sleep(1)
            # 获取文件列表弹窗中的所有文件节点
            file_nodes = target.query_selector_all('.result-nas-task-dialog .el-tree-node')
            if not file_nodes:
                log.warning("未找到文件列表")
                return True

            video_count = 0
            kept_count = 0
            removed_count = 0

            for node in file_nodes:
                title_elem = node.query_selector('a.file_title')
                if not title_elem:
                    continue
                filename = title_elem.get_attribute('title') or title_elem.inner_text().strip()
                ext = self._get_extension(filename)

                is_video = ext in self.VIDEO_EXTENSIONS
                is_subtitle = ext in self.SUBTITLE_EXTENSIONS
                is_info = ext in self.INFO_EXTENSIONS
                keep = is_video or is_subtitle or is_info

                if is_video:
                    video_count += 1

                # 检查当前勾选状态
                checkbox = node.query_selector('input.el-checkbox__original')
                if not checkbox:
                    continue
                is_checked = target.evaluate('(el) => el.checked', checkbox)

                if keep:
                    if not is_checked:
                        target.evaluate('(el) => el.click()', checkbox)
                    kept_count += 1
                else:
                    if is_checked:
                        target.evaluate('(el) => el.click()', checkbox)
                        removed_count += 1

            log.info(f"文件过滤: 保留 {kept_count} 个, 移除 {removed_count} 个, 视频 {video_count} 个")

            if video_count == 0:
                log.warning("没有视频文件")
                return False

            return True

        except Exception as e:
            log.warning(f"文件过滤异常: {e}")
            return True

    @staticmethod
    def _get_extension(filename: str) -> str:
        """获取文件扩展名（小写，带点）"""
        idx = filename.rfind('.')
        if idx >= 0:
            return filename[idx:].lower()
        return ''

    def list_tasks(self) -> list:
        """
        通过 Playwright 读取迅雷网页上的任务列表。
        返回格式与 API list_tasks 兼容：[{"id", "name", "file_name", "phase", "params": {"speed": ...}}]
        """
        from playwright.sync_api import sync_playwright

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context()

                cookie_domain = self.xunlei_url.split("//")[1].split(":")[0]
                context.add_cookies([{
                    "name": "fnos-token",
                    "value": self.fnos_token,
                    "domain": cookie_domain,
                    "path": "/",
                }])

                page = context.new_page()
                page.goto(self.xunlei_url, wait_until="networkidle", timeout=30000)
                time.sleep(5)

                # 等待任务列表加载
                try:
                    page.wait_for_selector('li.task-item.pan-list-item', timeout=5000)
                except Exception:
                    # 没有任务
                    browser.close()
                    return []

                task_items = page.query_selector_all('li.task-item.pan-list-item')
                tasks = []
                for item in task_items:
                    try:
                        task = self._parse_task_item(item)
                        if task:
                            tasks.append(task)
                    except Exception as e:
                        log.debug(f"解析任务项异常: {e}")

                browser.close()
                log.info(f"Playwright 读取到 {len(tasks)} 个迅雷任务")
                return tasks

        except Exception as e:
            log.warning(f"Playwright 读取任务列表异常: {e}")
            return []

    def _parse_task_item(self, item) -> dict:
        """解析单个任务 DOM 节点"""
        # ID: 从 li#task_item_XXX 提取
        item_id = item.get_attribute("id") or ""
        if item_id.startswith("task_item_"):
            item_id = item_id[len("task_item_"):]
        else:
            item_id = item_id

        # 名称
        name_elem = item.query_selector('.task-item__info a')
        name = name_elem.inner_text().strip() if name_elem else ""

        # 状态文本（可能是速度 "12.9MB/s"，也可能是 "校验中"、"等待中" 等）
        status_elem = item.query_selector('.task-item__status')
        status_text = status_elem.inner_text().strip() if status_elem else ""

        # 大小
        size_elem = item.query_selector('.task-item__size')
        size_text = size_elem.inner_text().strip() if size_elem else ""

        # 解析速度
        speed = self._parse_speed(status_text)

        # 映射 phase
        phase = self._map_phase(status_text)

        return {
            "id": item_id,
            "name": name,
            "file_name": name,
            "phase": phase,
            "params": {"speed": str(speed)},
        }

    @staticmethod
    def _parse_speed(text: str) -> int:
        """从状态文本中解析速度，返回 bytes/s"""
        import re
        match = re.search(r'(\d+(?:\.\d+)?)\s*(B/s|KB/s|MB/s|GB/s)', text)
        if not match:
            return 0
        value = float(match.group(1))
        unit = match.group(2)
        multipliers = {"B/s": 1, "KB/s": 1024, "MB/s": 1024 * 1024, "GB/s": 1024 * 1024 * 1024}
        return int(value * multipliers.get(unit, 1))

    @staticmethod
    def _map_phase(status_text: str) -> str:
        """将状态文本映射为 API 兼容的 phase"""
        if re.search(r'\d+(\.\d+)?\s*(B/s|KB/s|MB/s|GB/s)', status_text):
            return "PHASE_TYPE_RUNNING"
        if "等待中" in status_text:
            return "PHASE_TYPE_PENDING"
        if "校验中" in status_text:
            return "PHASE_TYPE_RUNNING"
        if "下载失败" in status_text or "失败" in status_text:
            return "PHASE_TYPE_ERROR"
        if "完成" in status_text:
            return "PHASE_TYPE_COMPLETE"
        return "PHASE_TYPE_RUNNING"
