#!/usr/bin/env python3
"""
本地单测：验证纯 API 版的解析/拍平/过滤/建任务体组装逻辑。
用真实抓取到的树结构做 fixture，不联网。
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from xunlei_downloader import XunleiDownloader

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

def mk(host="192.168.1.1"):
    return XunleiDownloader(nas_host=host, nas_port=5666, nas_user="u", nas_pass="p")

# ---------- fixture: 45 文件的剧集种子（真实结构，节选）----------
def file_node(name, fid, idx=None, mime="video/x-matroska"):
    n = {"id": fid, "name": name, "file_size": 1000, "file_count": 1,
         "meta": {"hash": "H", "mime_type": mime, "status": "1"}}
    if idx is not None:
        n["file_index"] = idx
    return n

FIXTURE = [{
    "id": "TOP.0", "name": "[Moozzi2] BD-BOX", "file_size": 999, "file_count": 45,
    "meta": {"bt_infohash": "7A4E", "bt_subfiles_num": "45", "url": "magnet:?xt=urn:btih:7a4e"},
    "is_dir": True,
    "dir": {"page_size": 29, "resources": [
        {"id": "TOP.0.0", "name": "EXTRA", "file_size": 500, "file_count": 5, "is_dir": True,
         "dir": {"page_size": 5, "resources": [
             file_node("[SP08] Special.mkv", "TOP.0.0.0"),          # 全局 0（无 file_index）
             file_node("[SP06] Lecture.mkv", "TOP.0.0.1", 1),
             file_node("[SP01] NCOP.mkv", "TOP.0.0.2", 2),
             file_node("readme.txt", "TOP.0.0.3", 3, mime="text/plain"),
             file_node("poster.jpg", "TOP.0.0.4", 4, mime="image/jpeg"),
         ]}},
        file_node("EP01.mkv", "TOP.0.1", 5),
        file_node("EP02.mkv", "TOP.0.2", 6),
        file_node("EP03.mkv", "TOP.0.3", 7),
        file_node("sample.mkv", "TOP.0.4", 8),
        file_node("EP01.srt", "TOP.0.5", 9, mime="application/x-subrip"),
        file_node("info.nfo", "TOP.0.6", 10, mime="text/plain"),
        file_node("movie.exe", "TOP.0.7", 11, mime="application/octet-stream"),
        file_node("data.bin", "TOP.0.8", 12, mime="application/octet-stream"),
    ]}
}]

print("=" * 64)
print("测试 1: 拍平 (DFS 先序) + file_index 编号")
print("=" * 64)
x = mk()
flat = x.flatten_resources(FIXTURE)
files = [n for n in flat if not x._is_dir_node(n)]
check("节点总数 (2目录+13文件)", len(flat), 15)
check("文件数", len(files), 13)
check("第一个文件 = SP08 (全局0)", files[0]["name"], "[SP08] Special.mkv")
check("最后文件 = data.bin", files[-1]["name"], "data.bin")
# 关键：file_index 与全局序号必须一致
idxs = [n.get("file_index") for n in files]
check("file_index 序列(首项缺省=None)", idxs, [None,1,2,3,4,5,6,7,8,9,10,11,12])

print()
print("=" * 64)
print("测试 2: FILTER_FILES=true —— 只留 视频/字幕/nfo-txt-jpg-png")
print("=" * 64)
x = mk(); x.filter_files = True
sel = x.select_file_indices(flat)
# 期望保留: SP08(0) SP06(1) NCOP(2) readme.txt(3) poster.jpg(4)
#           EP01(5) EP02(6) EP03(7) sample(8) EP01.srt(9) info.nfo(10)
# 过滤掉: movie.exe(11) data.bin(12)
check("选中索引", sel, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10])

print()
print("=" * 64)
print("测试 3: FILTER_FILES=false —— 全部保留")
print("=" * 64)
x = mk(); x.filter_files = False
sel = x.select_file_indices(flat)
check("选中索引", sel, list(range(13)))

print()
print("=" * 64)
print("测试 4: 无视频文件 → 放弃任务 (None)")
print("=" * 64)
x = mk(); x.filter_files = True
no_video = [{"id": "T.0", "name": "docs", "is_dir": True,
             "dir": {"resources": [
                 file_node("a.txt", "T.0.0", 0, mime="text/plain"),
                 file_node("b.pdf", "T.0.1", 1, mime="application/pdf"),
             ]}}]
check("返回 None", x.select_file_indices(x.flatten_resources(no_video)), None)

print()
print("=" * 64)
print("测试 5: 单文件种子 → 不传 sub_file_index")
print("=" * 64)
x = mk(); x.filter_files = True
single = [file_node("movie.mkv", "S.0", None)]
sflat = x.flatten_resources(single)
check("单文件选中", x.select_file_indices(sflat), [0])
check("文件数", len([n for n in sflat if not x._is_dir_node(n)]), 1)

print()
print("=" * 64)
print("测试 6: 扩展名分类")
print("=" * 64)
x = mk()
for name, want in [("a.mkv", True), ("a.MP4", True), ("a.ass", True),
                   ("a.nfo", True), ("a.jpg", True), ("a.exe", False),
                   ("a.bin", False), ("a.iso", True), ("noext", False)]:
    ext = x._ext(name)
    got = ext in x.video_extensions or ext in x.subtitle_extensions or ext in x.info_extensions
    check(f"{name} 归类", got, want)

print()
print("=" * 64)
print("测试 7: 建任务体组装（含目录/多文件/过滤）")
print("=" * 64)
import unittest.mock as mock
x = mk(); x.filter_files = True; x.target = "device_id#TEST"
with mock.patch.object(x, "parse_url", return_value={"list_id": "L1", "resources": FIXTURE}), \
     mock.patch.object(x, "_resolve_download_dir", return_value=("FOLDERID", "/vol5/.../迅雷下载影视")), \
     mock.patch.object(x, "_api_post", return_value={"id": "NEWTASK1"}) as m:
    r = x.add_download("magnet:?xt=urn:btih:7a4e", name="测试任务", target_dir="/vol5/x")
    body = m.call_args[0][1]
    check("返回 task id（mock 响应 id）", r, "NEWTASK1")
    check("type", body["type"], "user#download-url")
    check("space=target", body["space"], "device_id#TEST")
    check("params.target", body["params"]["target"], "device_id#TEST")
    check("total_file_count=13(全部文件)", body["params"]["total_file_count"], "13")
    check("sub_file_index(过滤后11项)", body["params"]["sub_file_index"],
          ",".join(str(i) for i in [0,1,2,3,4,5,6,7,8,9,10]))
    check("parent_folder_id", body["params"]["parent_folder_id"], "FOLDERID")
    check("parent_folder_path", body["params"]["parent_folder_path"], "/vol5/.../迅雷下载影视")
    check("url 原样传递", body["params"]["url"], "magnet:?xt=urn:btih:7a4e")

print()
print("=" * 64)
print("测试 8: 全选时 sub_file_index = -1")
print("=" * 64)
x = mk(); x.filter_files = False; x.target = "device_id#TEST"
with mock.patch.object(x, "parse_url", return_value={"list_id": "L1", "resources": FIXTURE}), \
     mock.patch.object(x, "_resolve_download_dir", return_value=("", "")), \
     mock.patch.object(x, "_api_post", return_value={"id": "T2"}) as m:
    x.add_download("magnet:x", name="n")
    body = m.call_args[0][1]
    check("sub_file_index=-1", body["params"]["sub_file_index"], "-1")
    check("无目录时不带 parent_folder_id", "parent_folder_id" in body["params"], False)

print()
print("=" * 64)
print("测试 9: list_tasks 相位过滤（本地过滤，避免 API 逗号多值返回 0 条）")
print("=" * 64)
x = mk(); x.target = "device_id#T"
FAKE = {"tasks": [
    {"id": "1", "phase": "PHASE_TYPE_RUNNING"},
    {"id": "2", "phase": "PHASE_TYPE_PENDING"},
    {"id": "3", "phase": "PHASE_TYPE_COMPLETE"},
    {"id": "4", "phase": "PHASE_TYPE_ERROR"},
]}
with mock.patch.object(x, "_api_get", return_value=FAKE):
    check("all 返回全部", [t["id"] for t in x._list_tasks_api("all")], ["1","2","3","4"])
    check("active 只留未完成", [t["id"] for t in x._list_tasks_api("active")], ["1","2","4"])
    check("completed 只留完成", [t["id"] for t in x._list_tasks_api("completed")], ["3"])

print()
print("=" * 64)
print("测试 10: pan_auth 失效自动自愈（invalid token）")
print("=" * 64)
x = mk(); x.target = "device_id#T"
# 验证自愈核心：判定函数 + 重试后成功返回
check("识别 invalid token", x._is_auth_invalid(b"invalid token"), True)
check("识别带空格", x._is_auth_invalid(b"  invalid token\n"), True)
check("正常 JSON 不误判", x._is_auth_invalid(b'{"tasks":[]}'), False)
check("空响应不误判", x._is_auth_invalid(b''), False)

# 真实失效响应形态（实测抓取）
REAL_DENY = b'{"error":"permission_deny: checkAuth failed:token contains an invalid number of segments token:BROKEN","error_code":403,"HttpStatus":0}'
check("识别 JSON permission_deny", x._is_auth_invalid(REAL_DENY), True)
check("识别 error_code=401", x._is_auth_invalid(b'{"error_code":401}'), True)
check("正常任务响应不误判", x._is_auth_invalid(b'{"HttpStatus":0,"tasks":[]}'), True if False else False)

print()
print("=" * 64)
print("测试 11: 视频体积过滤（MIN_VIDEO_SIZE_MB）")
print("=" * 64)
MB = 1024 * 1024
def mkfile(name, size, idx):
    n = {"id": f"F{idx}", "name": name, "file_size": size, "file_count": 1,
         "meta": {"hash": "H", "mime_type": "video/x-matroska", "status": "1"}}
    if idx is not None:
        n["file_index"] = idx
    return n

# 混合场景：正片 2GB / 小样片 0.5MB / 字幕 30KB / nfo 1KB / 广告 .doc 10KB
MIX = [
    mkfile("movie.mkv", 2 * 1024 * MB, 0),
    mkfile("sample.mkv", int(0.5 * MB), 1),          # 小于 1MB → 应过滤
    mkfile("movie.srt", 30 * 1024, 2),               # 字幕 → 保留（不受体积限制）
    mkfile("info.nfo", 1024, 3),                     # nfo → 保留
    mkfile("ad.DOC", 10 * 1024, 4),                  # 非视频 → 过滤
    mkfile("trailer.mp4", int(0.9 * MB), 5),         # 小于 1MB → 应过滤
]
x = mk(); x.filter_files = True
x.min_video_size_mb = 0
check("不启用体积过滤 → 全部视频保留", x.select_file_indices(MIX), [0, 1, 2, 3, 5])
x.min_video_size_mb = 1
check("1MB 阈值 → 过滤 sample/trailer，保留正片+字幕+nfo",
      x.select_file_indices(MIX), [0, 2, 3])

# 边界：恰好等于阈值应保留
B = [mkfile("a.mkv", 1 * MB, 0), mkfile("b.mkv", MB - 1, 1)]
x.min_video_size_mb = 1
check("恰好 1MB 保留、1MB-1 过滤", x.select_file_indices(B), [0])

# 全部视频都太小 → 放弃任务
S = [mkfile("tiny1.mkv", int(0.3 * MB), 0), mkfile("tiny2.mp4", int(0.5 * MB), 1)]
check("视频全部过小 → None", x.select_file_indices(S), None)

# 字幕只有几十 KB，不应被体积过滤误伤
SUB = [mkfile("m.mkv", 500 * MB, 0), mkfile("m.ass", 20 * 1024, 1)]
check("字幕不受体积限制", x.select_file_indices(SUB), [0, 1])

# file_size 缺失/非法时不误删
BAD = [{"id": "F0", "name": "x.mkv", "file_count": 1,
        "meta": {"hash": "H", "mime_type": "video/x-matroska"}},
       {"id": "F1", "name": "y.mkv", "file_size": "abc", "file_count": 1,
        "meta": {"hash": "H", "mime_type": "video/x-matroska"}}]
check("file_size 缺失/非法 → 保守保留", x.select_file_indices(BAD), [0, 1])

print()
print("=" * 64)
print("测试 12: 任务名称为哈希占位时改用迅雷解析名")
print("=" * 64)
check("识别 40 位哈希", x._is_hash_name("d6edfe6e9dc2e1a5d5d4b02139da5752e4bf587f"), True)
check("识别 32 位哈希", x._is_hash_name("a" * 32), True)
check("大写哈希也识别", x._is_hash_name("A" * 40), True)
check("带空格哈希", x._is_hash_name("  " + "a" * 40 + "  "), True)
check("正常名称不误判", x._is_hash_name("飞鹰计划.Operation.Condor.1991"), False)
check("含中文不误判", x._is_hash_name("熟男不結婚"), False)
check("空串不误判", x._is_hash_name(""), False)
check("None 不误判", x._is_hash_name(None), False)
check("31 位不算哈希", x._is_hash_name("a" * 31), False)
check("含非十六进制不算", x._is_hash_name("g" * 40), False)

# 端到端：qBit 传来哈希名 → 任务名应为迅雷解析名
HASH = "d6edfe6e9dc2e1a5d5d4b02139da5752e4bf587f"
RESOLVED = "[MagicStar] Kekkon Dekinai Otoko [WEBDL] [1080p]"
FIX = [{
    "id": "T.0", "name": RESOLVED, "file_size": 43504918594, "file_count": 2,
    "meta": {"bt_infohash": HASH, "url": f"magnet:?xt=urn:btih:{HASH}"},
    "is_dir": True,
    "dir": {"resources": [
        file_node("EP01.mkv", "T.0.0", None),
        file_node("EP02.mkv", "T.0.1", 1),
    ]},
}]
x = mk(); x.filter_files = True; x.target = "device_id#T"
with mock.patch.object(x, "parse_url", return_value={"list_id": "L", "resources": FIX}), \
     mock.patch.object(x, "_resolve_download_dir", return_value=("", "")), \
     mock.patch.object(x, "_api_post", return_value={"task": {"id": "NEW"}}) as m:
    x.add_download(f"magnet:?xt=urn:btih:{HASH}", name=HASH)
    body = m.call_args[0][1]
    check("任务名改用迅雷解析名", body["name"], RESOLVED)
    check("file_name 同步修正", body["file_name"], RESOLVED)

# 正常名称应保持原样（不被覆盖）
NORMAL = [{
    "id": "T.0", "name": "迅雷解析名X", "file_size": 1000, "file_count": 1,
    "meta": {}, "is_dir": True,
    "dir": {"resources": [file_node("a.mkv", "T.0.0", None)]},
}]
with mock.patch.object(x, "parse_url", return_value={"list_id": "L", "resources": NORMAL}), \
     mock.patch.object(x, "_resolve_download_dir", return_value=("", "")), \
     mock.patch.object(x, "_api_post", return_value={"task": {"id": "N"}}) as m:
    x.add_download("magnet:?xt=urn:btih:abc", name="qBit正常名")
    body = m.call_args[0][1]
    check("正常名称优先于解析名", body["name"], "qBit正常名")

print()
print("=" * 64)
print(f"结果: {PASS} 通过 / {FAIL} 失败")
print("=" * 64)
sys.exit(1 if FAIL else 0)
