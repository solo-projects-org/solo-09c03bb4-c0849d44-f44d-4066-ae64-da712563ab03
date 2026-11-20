#!/usr/bin/env python3
"""浅点筛查图导出试算: 含零水深/缺测的选区 + 65536 米稀疏选区 + 导出期间补测。

对运行中的服务 (默认 http://127.0.0.1:8000) 执行:
1. 上传含零水深、空水深、缺测的后测 CSV (外加远处稀疏测块)
2. 导出含零水深与缺测的选区, 下载 GeoTIFF 并逐像元校验
3. 导出 65536m x 65536m 稀疏选区, 校验全空瓦片不写像元数据
4. 导出进行中补测: 校验文件数据版本与像元内容一致 (同一份已提交快照)
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.tiff_util import TiffFile  # noqa: E402

BASE = "http://127.0.0.1:8000"
NODATA = 4294967295
FAILURES = []


def call(method, path, data=None, files=None, raw=False):
    if files is not None:
        boundary = "----dredgeboundary"
        body = b""
        for field, (filename, content) in files.items():
            body += f"--{boundary}\r\n".encode()
            body += (f'Content-Disposition: form-data; name="{field}"; '
                     f'filename="{filename}"\r\n\r\n').encode()
            body += content.encode() + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            BASE + path, data=body, method=method,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(
            BASE + path, method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            payload = resp.read()
            return resp.status, payload if raw else json.loads(payload)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def make_csv(rows):
    lines = ["x,y,survey_no,depth_cm"]
    for x, y, sno, depth in rows:
        lines.append(f"{x},{y},{sno},{'' if depth is None else depth}")
    return "\n".join(lines) + "\n"


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def wait_job(job_id):
    for _ in range(600):
        time.sleep(0.2)
        status, body = call("GET", f"/api/exports/{job_id}")
        assert status == 200, body
        if body["status"] != "running":
            return body
    raise RuntimeError("导出超时")


def download(job_id):
    status, payload = call("GET", f"/api/exports/{job_id}/file", raw=True)
    assert status == 200, f"下载失败 HTTP {status}"
    return payload


# ---------- 造数: 主测区 32x32 (含零水深/空水深/缺测) + 远处稀疏测块 ----------
def depth_at(x, y):
    return (x * 7 + y * 13) % 90          # 0..89, 天然含零水深


main_cells = {}
for x in range(32):
    for y in range(32):
        if x >= 16 and y >= 16:
            continue                       # 东北象限缺测
        main_cells[(x, y)] = depth_at(x, y)
for hole in ((5, 5), (6, 6), (7, 7)):
    main_cells[hole] = None                # 空水深

far_cells = {(x, y): 800 for x in range(65000, 65008) for y in range(65000, 65008)}

rows = [(x, y, 1, d) for (x, y), d in sorted(main_cells.items())]
rows += [(x, y, 1, d) for (x, y), d in sorted(far_cells.items())]

print("########## 1. 健康检查与上传后测 (含零水深/空水深/缺测) ##########")
status, health = call("GET", "/health")
print(f"health -> {status} {health}")
status, up = call("POST", "/api/surveys/after",
                  files={"file": ("after.csv", make_csv(rows))})
print(f"上传后测 -> {status}: 写入 {up.get('rows_applied')} 格, "
      f"数据版本 v{up.get('data_version')}")
assert status == 200, up
v_base = up["data_version"]

print("########## 2. 导出含零水深与缺测的选区 [0,0]-[32,32] ##########")
status, job = call("POST", "/api/exports",
                   data={"x0": 0, "y0": 0, "x1": 32, "y1": 32})
assert status == 202, job
done = wait_job(job["id"])
print(f"任务 {job['id']} -> {done['status']}, 版本 v{done['data_version']}, "
      f"有效像元 {done['valid_pixels']}, 文件 {done['file_bytes']} 字节")
tiff = TiffFile(download(job["id"]))

expect_min, expect_cnt = {}, {}
for px in range(4):
    for py_from_bottom in range(4):
        vals = [main_cells[(x, y)]
                for x in range(px * 8, px * 8 + 8)
                for y in range(py_from_bottom * 8, py_from_bottom * 8 + 8)
                if main_cells.get((x, y)) is not None]
        row = 3 - py_from_bottom            # 北向上: 行0 = 最北
        expect_min[(row, px)] = min(vals) if vals else NODATA
        expect_cnt[(row, px)] = len(vals) if vals else NODATA

ok_min = all(tiff.pixel(0, r, c) == expect_min[(r, c)] for r in range(4) for c in range(4))
ok_cnt = all(tiff.pixel(1, r, c) == expect_cnt[(r, c)] for r in range(4) for c in range(4))
check("波段1 全部 16 像元 = 最浅有效水深", ok_min)
check("波段2 全部 16 像元 = 有效细格数", ok_cnt)
zero_cells = [(r, c) for (r, c), v in expect_min.items() if v == 0]
check("零水深像元有效 (最浅值=0 而非 NoData)", len(zero_cells) > 0,
      f"零水深像元: {zero_cells}")
check("缺测象限像元两波段均为 NoData",
      tiff.pixel(0, 0, 3) == NODATA and tiff.pixel(1, 0, 3) == NODATA)
check("文件注明数据版本", f"data_version={v_base}" in tiff.ascii(270)
      and f'<Item name="data_version">{v_base}</Item>' in tiff.ascii(42112))
check("北向上地理参考 (左上角=(0,32), 像元8m)",
      tiff.doubles(33922) == [0.0, 0.0, 0.0, 0.0, 32.0, 0.0]
      and tiff.doubles(33550) == [8.0, 8.0, 0.0])

print("########## 3. 65536m x 65536m 稀疏选区: 全空瓦片不写像元数据 ##########")
status, job = call("POST", "/api/exports",
                   data={"x0": 0, "y0": 0, "x1": 65536, "y1": 65536})
assert status == 202, job
done = wait_job(job["id"])
raw = download(job["id"])
tiff = TiffFile(raw)
n_written = sum(1 for o in tiff.longs(324) if o != 0)
print(f"8192x8192 像元, 瓦片 {tiff.tiles_per_plane()} 个/波段, "
      f"写入 {n_written} 块, 文件 {len(raw)} 字节")
# 原点 8x8 像元的期望 (含 3 个空水深格)
origin_vals = [main_cells[(x, y)] for x in range(8) for y in range(8)
               if main_cells.get((x, y)) is not None]
origin_cnt = len(origin_vals)
origin_min_old = min(origin_vals)
origin_min_new = min(5000 if (x, y) == (0, 0) else main_cells[(x, y)]
                     for x in range(8) for y in range(8)
                     if main_cells.get((x, y)) is not None)
check("只写了 2 块非空瓦片 x 2 波段", n_written == 4)
check("文件远小于全量 (稀疏)", len(raw) < 2_000_000,
      f"{len(raw)} 字节 vs 全量约 512MB")
check("原点像元 (行8191,列0) 零水深最浅",
      tiff.pixel(0, 8191, 0) == origin_min_old
      and tiff.pixel(1, 8191, 0) == origin_cnt,
      f"min={origin_min_old}, n={origin_cnt}")
check("远处测块像元 (行66,列8125)",
      tiff.pixel(0, 66, 8125) == 800 and tiff.pixel(1, 66, 8125) == 64)
check("中间全空瓦片未写像元数据", tiff.pixel(0, 4000, 4000) is None)

print("########## 4. 导出进行中补测: 同一份已提交快照 ##########")
status, job = call("POST", "/api/exports",
                   data={"x0": 0, "y0": 0, "x1": 65536, "y1": 65536})
assert status == 202, job
# 导出后台生成期间立即补测: 改浅 (0,0) 并新增远处一格
supp = [(0, 0, 2, 5000), (40000, 40000, 2, 300)]
status, up2 = call("POST", "/api/surveys/after",
                   files={"file": ("supp.csv", make_csv(supp))})
check("导出期间补测照常接收", status == 200, f"HTTP {status}")
done = wait_job(job["id"])
tiff = TiffFile(download(job["id"]))
v_file = done["data_version"]
_, health2 = call("GET", "/health")
print(f"导出文件版本 v{v_file}, 补测后库版本 v{health2['data_version']}")
px00 = (tiff.pixel(0, 8191, 0), tiff.pixel(1, 8191, 0))
px_far = (tiff.pixel(0, 3191, 5000), tiff.pixel(1, 3191, 5000))
if v_file == v_base:
    check("文件取导出开始时的快照: (0,0) 仍为零水深最浅, 新格不可见",
          px00 == (origin_min_old, origin_cnt)
          and px_far == (None, None), f"px00={px00}")  # 空瓦片未写像元
elif v_file == v_base + 1:
    check("文件取补测后的快照: (0,0) 最浅值更新, 新格 300 可见",
          px00 == (origin_min_new, origin_cnt)
          and px_far == (300, 1), f"px00={px00}")
else:
    check("文件数据版本与库版本序列一致", False, f"v_file={v_file}")
check("两波段取自同一版本 (文件内注明)",
      f"data_version={v_file}" in tiff.ascii(270))

print()
if FAILURES:
    print(f"共 {len(FAILURES)} 项失败: {FAILURES}")
    sys.exit(1)
print("全部试算通过。")
