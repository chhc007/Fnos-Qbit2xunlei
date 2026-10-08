#!/usr/bin/env python3
"""
本地单测：迅雷并发/排队适配
验证：排队识别、并发统计、比速保护、0速度监控排除排队任务。
不联网。
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib.util
spec = importlib.util.spec_from_file_location(
    "q2x", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "qbit_to_xunlei.py"))
# qbit_to_xunlei 在 import 时会读配置，先造一个最小配置
import tempfile, configparser
tmpdir = tempfile.mkdtemp()
cfgpath = os.path.join(tmpdir, "config.ini")
with open(cfgpath, "w", encoding="utf-8") as f:
    f.write("""[qbittorrent]
QB_HOST = http://127.0.0.1:1
QB_USER = u
QB_PASS = p

[nas]
NAS_HOST = 127.0.0.1
NAS_PORT = 5666
NAS_USER = u
NAS_PASS = p

[general]
TARGET_LABEL = 迅雷
MAX_CONCURRENT_TASKS = 3
QUEUE_POLL_INTERVAL = 1
QUEUE_MAX_WAIT_MINUTES = 1
""")
os.environ["CONFIG_PATH"] = cfgpath

PASS = FAIL = 0
def check(label, got, want):
    global PASS, FAIL
    ok = got == want
    if ok: PASS += 1
    else: FAIL += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"         期望: {want}")
        print(f"         实际: {got}")

import qbit_to_xunlei as Q

def task(phase, status=None, speed=0, tid="T1"):
    p = {"speed": str(speed)}
    if status is not None:
        p["status"] = json.dumps(status)
    return {"id": tid, "phase": phase, "params": p, "name": "n"}

print("=" * 64)
print("测试 1: is_task_queued —— 排队识别")
print("=" * 64)
check("PENDING → 排队", Q.is_task_queued(task("PHASE_TYPE_PENDING")), True)
check("RUNNING + status.running → 不排队",
      Q.is_task_queued(task("PHASE_TYPE_RUNNING", {"phase": "running"})), False)
check("RUNNING 无 status → 排队（未真正开始）",
      Q.is_task_queued(task("PHASE_TYPE_RUNNING")), True)
check("RUNNING + status.pause → 排队（未在下载）",
      Q.is_task_queued(task("PHASE_TYPE_RUNNING", {"phase": "pause"})), True)
check("COMPLETE → 不排队",
      Q.is_task_queued(task("PHASE_TYPE_COMPLETE")), False)
check("ERROR → 不排队", Q.is_task_queued(task("PHASE_TYPE_ERROR")), False)
check("status 非法 JSON → 保守判为排队",
      Q.is_task_queued({"id": "x", "phase": "PHASE_TYPE_RUNNING",
                        "params": {"status": "{bad json"}}), True)

print()
print("=" * 64)
print("测试 2: get_task_speed")
print("=" * 64)
check("正常速度", Q.get_task_speed(task("PHASE_TYPE_RUNNING", speed=12345)), 12345)
check("缺失 speed", Q.get_task_speed({"id": "x", "params": {}}), 0)
check("非法 speed", Q.get_task_speed({"id": "x", "params": {"speed": "abc"}}), 0)
check("None speed", Q.get_task_speed({"id": "x", "params": {"speed": None}}), 0)

print()
print("=" * 64)
print("测试 3: count_active_xunlei_tasks（占用并发位）")
print("=" * 64)
tasks = [
    task("PHASE_TYPE_RUNNING", {"phase": "running"}, 1000, "A"),   # 占位
    task("PHASE_TYPE_PENDING", None, 0, "B"),                      # 占位（排队）
    task("PHASE_TYPE_RUNNING", {"phase": "pause"}, 0, "C"),        # 占位
    task("PHASE_TYPE_COMPLETE", None, 0, "D"),                     # 不占
    task("PHASE_TYPE_ERROR", None, 0, "E"),                        # 不占
]
check("活动任务数 = 3", Q.count_active_xunlei_tasks(tasks), 3)
check("空列表 = 0", Q.count_active_xunlei_tasks([]), 0)

print()
print("=" * 64)
print("测试 4: ZeroSpeedMonitor 排除排队任务")
print("=" * 64)
mon = Q.ZeroSpeedMonitor(timeout_seconds=1, xunlei=None)
# 模拟主循环：排队任务应被跳过（不进入 tracking）
def simulate(t):
    if Q.is_task_queued(t):
        mon.tracking.pop(f"xunlei:{t['id']}", None)
        return "skipped"
    if Q.get_task_speed(t) > 0:
        mon.tracking.pop(f"xunlei:{t['id']}", None)
        return "skipped"
    mon.check_xunlei_task_by_speed(t["id"], t["name"], Q.get_task_speed(t))
    return "tracked"

check("排队任务被跳过", simulate(task("PHASE_TYPE_PENDING", None, 0, "P1")), "skipped")
check("排队任务不在 tracking", "xunlei:P1" in mon.tracking, False)
check("真正下载中(有速度)被跳过",
      simulate(task("PHASE_TYPE_RUNNING", {"phase": "running"}, 500, "R1")), "skipped")
check("下载中但0速度 → 进入跟踪",
      simulate(task("PHASE_TYPE_RUNNING", {"phase": "running"}, 0, "Z1")), "tracked")
check("0速度任务在 tracking", "xunlei:Z1" in mon.tracking, True)

print()
print("=" * 64)
print("测试 5: check_xunlei_task_by_speed 的 queued 参数")
print("=" * 64)
mon2 = Q.ZeroSpeedMonitor(timeout_seconds=9999, xunlei=None)
mon2.check_xunlei_task_by_speed("Q1", "n", 0, queued=True)
check("queued=True 不进入跟踪", "xunlei:Q1" in mon2.tracking, False)
mon2.check_xunlei_task_by_speed("Q2", "n", 0, queued=False)
check("queued=False 进入跟踪", "xunlei:Q2" in mon2.tracking, True)

print()
print("=" * 64)
print("测试 6: 配置项读取")
print("=" * 64)
check("MAX_CONCURRENT_TASKS", Q.MAX_CONCURRENT_TASKS, 3)
check("QUEUE_POLL_INTERVAL", Q.QUEUE_POLL_INTERVAL, 1)
check("QUEUE_MAX_WAIT_MINUTES", Q.QUEUE_MAX_WAIT_MINUTES, 1)

print()
print("=" * 64)
print(f"结果: {PASS} 通过 / {FAIL} 失败")
print("=" * 64)
sys.exit(1 if FAIL else 0)
