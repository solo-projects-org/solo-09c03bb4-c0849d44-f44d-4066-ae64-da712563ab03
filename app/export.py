"""浅点筛查图导出: 8 米像元双波段 GeoTIFF, 稀疏瓦片, 同一已提交快照。

业务规则
--------
- 选区沿 8 米格线 (坐标为 8 的整数倍, 单位米), 像元 8m x 8m, 北向上
  (图像第 0 行 = 最北), 平面坐标沿用港区米制格线 (x 向东, y 向北)。
- 每个像元汇总对应的 8x8 个后测细格: 波段 1 = 最浅有效水深 (厘米),
  波段 2 = 有效细格数。零水深是有效水深; 空水深 (NULL) 与缺测格不计。
- 没有任何有效细格的像元, 两个波段都为 NoData (4294967295)。
- 选区边长可达 65536 米 (8192 x 8192 像元): 全空瓦片不写像元数据
  (TileOffsets/TileByteCounts 记 0 的稀疏瓦片), 逐瓦片生成,
  内存中同时只保留一个瓦片的像元。
- 整个导出在同一个只读快照事务内取数 (WAL): 生成期间补测可继续提交,
  两个波段看到的都是生成开始时的同一份已提交后测数据;
  数据版本写入文件元数据 (ImageDescription 与 GDALMetadata)。
- 先写隐藏临时文件, 全部写完后原子改名; 任何失败删除残件,
  只交付写完的文件。

实现说明
--------
不依赖 GDAL, 用标准库流式写出 classic TIFF (little-endian, 无压缩,
瓦片, PlanarConfiguration=separate): 两个波段都是 UInt32, 共用同一个
NoData 值, 因此单个 GDAL_NODATA 标签即可让 GIS 正确识别两个波段的
NoData。坐标系为港区局部米制格线, GeoTIFF 记为用户自定义投影坐标系
(ProjectedCSTypeGeoKey=32767, 单位米), 并附 PCSCitation 说明。
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import threading
import uuid
from array import array
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable

from . import config
from .database import connect, get_data_version, read_transaction, resolved_db_path

PIXEL_SIZE_M = 8       # 像元边长 (米): 每像元对应 8x8 个 1 米细格
TILE_SIZE = 256        # TIFF 瓦片边长 (像元)
NODATA = 0xFFFFFFFF    # 两波段共用的 NoData 值 (UInt32)

# 每瓦片聚合 SQL: 在数据库内完成 8x8 细格 -> 像元的汇总 (最浅水深 + 有效数),
# 只返回有有效水深的像元, 天然适配稀疏选区; 零水深照常参与 MIN/COUNT。
_TILE_SQL = """
SELECT (x - :x0) / :ps AS px, (:y1m1 - y) / :ps AS py,
       MIN(depth_cm) AS min_depth, COUNT(*) AS n_valid
FROM readings
WHERE phase = 'after' AND depth_cm IS NOT NULL
  AND x BETWEEN :fx0 AND :fx1 AND y BETWEEN :fy0 AND :fy1
GROUP BY px, py
"""


class ExportError(ValueError):
    """导出选区参数不合法, 路由层映射为 HTTP 400。"""


@dataclass(frozen=True)
class ExportSelection:
    """沿 8 米格线的矩形选区, 半开区间 [x0,x1) x [y0,y1), 单位米。"""

    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def width_px(self) -> int:
        return (self.x1 - self.x0) // PIXEL_SIZE_M

    @property
    def height_px(self) -> int:
        return (self.y1 - self.y0) // PIXEL_SIZE_M


def validate_selection(x0: int, y0: int, x1: int, y1: int) -> ExportSelection:
    """校验并生成导出选区: 坐标必须是 8 的整数倍, 边长不超过上限。"""
    vals = []
    for name, v in (("x0", x0), ("y0", y0), ("x1", x1), ("y1", y1)):
        if isinstance(v, bool) or not isinstance(v, int):
            raise ExportError(f"{name} 必须是整数 (米), 实际: {v!r}")
        if v % PIXEL_SIZE_M != 0:
            raise ExportError(
                f"{name}={v} 不在 {PIXEL_SIZE_M} 米格线上 "
                f"(应为 {PIXEL_SIZE_M} 的整数倍)"
            )
        vals.append(v)
    x0, y0, x1, y1 = vals
    if x1 <= x0 or y1 <= y0:
        raise ExportError(f"选区为空: [{x0},{y0}] - [{x1},{y1}]")
    side_max = config.EXPORT_MAX_SIDE_M
    if x1 - x0 > side_max or y1 - y0 > side_max:
        raise ExportError(
            f"选区边长 {x1 - x0}x{y1 - y0} 米超过上限 {side_max} 米"
        )
    sel = ExportSelection(x0, y0, x1, y1)
    # classic TIFF 为 32 位偏移: 即使全部瓦片写满也不能超过 4GB
    if sel.width_px * sel.height_px * 8 > 0xF0000000:
        raise ExportError("选区过大, 全量像元超出 classic TIFF 4GB 上限")
    return sel


# ---------------------------------------------------------------- GeoTIFF 写出

def _u32_array(fill: int, n: int) -> array:
    a = array("I", [fill]) * n
    if a.itemsize != 4:  # pragma: no cover - 现代平台 unsigned int 均为 32 位
        raise RuntimeError("平台 array('I') 不是 32 位")
    return a


def _u32_bytes(a: array) -> bytes:
    if sys.byteorder == "big":  # pragma: no cover
        a = a[:]
        a.byteswap()
    return a.tobytes()


def write_geotiff(
    fp: BinaryIO,
    conn,
    sel: ExportSelection,
    *,
    data_version: int,
    generated_at: str | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> dict:
    """把选区的浅点筛查图写入已打开的二进制文件对象 (需可 seek)。

    conn 必须处于只读快照事务内: 全部瓦片查询看到同一份已提交数据。
    逐瓦片生成并顺序写盘, 内存中同时只保留一个瓦片的像元;
    全空瓦片只登记 offset=0/count=0, 不写任何像元数据。
    """
    generated_at = generated_at or datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )
    width, height = sel.width_px, sel.height_px
    tiles_across = -(-width // TILE_SIZE)
    tiles_down = -(-height // TILE_SIZE)
    tiles_per_plane = tiles_across * tiles_down
    n_arrays = tiles_per_plane * 2  # 两个波段各一套瓦片索引

    citation = "Harbor metric grid, x east, y north, metre"
    nodata_txt = str(NODATA)
    desc = (
        "shallow-point screening; phase=after; "
        f"pixel_size_m={PIXEL_SIZE_M}; band1=min_valid_depth_cm; "
        f"band2=valid_fine_cell_count; nodata={NODATA}; "
        f"data_version={data_version}; "
        f"selection_m=[{sel.x0},{sel.y0},{sel.x1},{sel.y1}); "
        f"generated_at={generated_at}"
    )
    gdal_meta = (
        "<GDALMetadata>\n"
        f'  <Item name="data_version">{data_version}</Item>\n'
        '  <Item name="survey_phase">after</Item>\n'
        f'  <Item name="pixel_size_m">{PIXEL_SIZE_M}</Item>\n'
        f'  <Item name="selection_m">[{sel.x0},{sel.y0},{sel.x1},{sel.y1})</Item>\n'
        '  <Item name="band1_meaning">shallowest valid depth (cm) '
        "of 8x8 fine cells</Item>\n"
        '  <Item name="band2_meaning">valid fine cell count</Item>\n'
        f'  <Item name="generated_at">{generated_at}</Item>\n'
        "</GDALMetadata>"
    )
    geo_keys = [
        1, 1, 0, 5,                    # 目录头: 版本 1.1.0, 5 个键
        1024, 0, 1, 1,                 # GTModelTypeGeoKey = Projected
        1025, 0, 1, 1,                 # GTRasterTypeGeoKey = PixelIsArea
        3072, 0, 1, 32767,             # ProjectedCSTypeGeoKey = 用户自定义
        3073, 34737, len(citation) + 1, 0,  # PCSCitationGeoKey -> ASCII 参数
        3076, 0, 1, 9001,              # ProjLinearUnitsGeoKey = 米
    ]

    SHORT, LONG, ASCII, DOUBLE = 3, 4, 2, 12

    def shorts(vals):
        return struct.pack(f"<{len(vals)}H", *vals)

    def longs(vals):
        return struct.pack(f"<{len(vals)}I", *vals)

    def doubles(vals):
        return struct.pack(f"<{len(vals)}d", *vals)

    def ascii_z(text):
        return text.encode("ascii") + b"\x00"

    # (tag, type, count, 序列化值); 瓦片索引先置 0, 写完后回填
    tags = [
        (256, LONG, 1, longs([width])),                # ImageWidth
        (257, LONG, 1, longs([height])),               # ImageLength
        (258, SHORT, 2, shorts([32, 32])),             # BitsPerSample
        (259, SHORT, 1, shorts([1])),                  # Compression = 无
        (262, SHORT, 1, shorts([1])),                  # Photometric = BlackIsZero
        (270, ASCII, len(desc) + 1, ascii_z(desc)),    # ImageDescription
        (277, SHORT, 1, shorts([2])),                  # SamplesPerPixel
        (284, SHORT, 1, shorts([2])),                  # PlanarConfiguration
        (322, LONG, 1, longs([TILE_SIZE])),            # TileWidth
        (323, LONG, 1, longs([TILE_SIZE])),            # TileLength
        (324, LONG, n_arrays, b"\x00" * 4 * n_arrays),  # TileOffsets (回填)
        (325, LONG, n_arrays, b"\x00" * 4 * n_arrays),  # TileByteCounts (回填)
        (339, SHORT, 2, shorts([1, 1])),               # SampleFormat = uint
        (33550, DOUBLE, 3,                             # ModelPixelScale
         doubles([float(PIXEL_SIZE_M), float(PIXEL_SIZE_M), 0.0])),
        (33922, DOUBLE, 6,                             # ModelTiepoint: 左上角
         doubles([0.0, 0.0, 0.0, float(sel.x0), float(sel.y1), 0.0])),
        (34735, SHORT, len(geo_keys), shorts(geo_keys)),   # GeoKeyDirectory
        (34737, ASCII, len(citation) + 2,              # GeoAsciiParams
         citation.encode("ascii") + b"|\x00"),
        (42112, ASCII, len(gdal_meta) + 1, ascii_z(gdal_meta)),  # GDALMetadata
        (42113, ASCII, len(nodata_txt) + 1, ascii_z(nodata_txt)),  # NoData
    ]
    tags.sort(key=lambda t: t[0])

    # ---- 布局: 文件头 + IFD + 行外标签值 + 瓦片数据 ----
    ifd_offset = 8
    cursor = ifd_offset + 2 + 12 * len(tags) + 4
    entries = []            # (tag, type, count, 内联 4 字节 | 行外偏移)
    blobs = []              # (文件偏移, 数据)
    array_patch_pos = {}    # 瓦片索引数组的文件位置, 写完瓦片后回填
    for tag, typ, count, data in tags:
        if len(data) <= 4:
            entries.append((tag, typ, count, data.ljust(4, b"\x00")))
        else:
            cursor = (cursor + 3) & ~3
            entries.append((tag, typ, count, cursor))
            blobs.append((cursor, data))
            if tag in (324, 325):
                array_patch_pos[tag] = cursor
            cursor += len(data)
    data_start = (cursor + 3) & ~3

    fp.seek(0)
    fp.write(b"II" + struct.pack("<HI", 42, ifd_offset))
    fp.write(struct.pack("<H", len(entries)))
    for tag, typ, count, val in entries:
        if isinstance(val, int):
            fp.write(struct.pack("<HHII", tag, typ, count, val))
        else:
            fp.write(struct.pack("<HHI", tag, typ, count) + val)
    fp.write(struct.pack("<I", 0))  # 无下一个 IFD
    for pos, data in blobs:
        fp.seek(pos)
        fp.write(data)

    # ---- 逐瓦片: 聚合查询 -> 两个波段缓冲 -> 顺序写盘 ----
    offsets = [0] * n_arrays
    byte_counts = [0] * n_arrays
    tiles_written = 0
    valid_pixels = 0
    done = 0
    fp.seek(data_start)
    y1m1 = sel.y1 - 1
    for tr in range(tiles_down):
        top_row = tr * TILE_SIZE
        bot_row = min(top_row + TILE_SIZE, height) - 1
        fy1 = sel.y1 - top_row * PIXEL_SIZE_M - 1      # 本瓦片最北细格
        fy0 = sel.y1 - (bot_row + 1) * PIXEL_SIZE_M    # 本瓦片最南细格
        for tc in range(tiles_across):
            fx0 = sel.x0 + tc * TILE_SIZE * PIXEL_SIZE_M
            fx1 = min(fx0 + TILE_SIZE * PIXEL_SIZE_M, sel.x1) - 1
            rows = conn.execute(_TILE_SQL, {
                "x0": sel.x0, "y1m1": y1m1, "ps": PIXEL_SIZE_M,
                "fx0": fx0, "fx1": fx1, "fy0": fy0, "fy1": fy1,
            }).fetchall()
            if rows:
                band1 = _u32_array(NODATA, TILE_SIZE * TILE_SIZE)
                band2 = _u32_array(NODATA, TILE_SIZE * TILE_SIZE)
                base_px = tc * TILE_SIZE
                base_py = tr * TILE_SIZE
                for r in rows:
                    i = (r["py"] - base_py) * TILE_SIZE + (r["px"] - base_px)
                    band1[i] = r["min_depth"]
                    band2[i] = r["n_valid"]
                idx = tr * tiles_across + tc
                blob1 = _u32_bytes(band1)
                offsets[idx] = fp.tell()
                fp.write(blob1)
                byte_counts[idx] = len(blob1)
                idx2 = tiles_per_plane + idx
                blob2 = _u32_bytes(band2)
                offsets[idx2] = fp.tell()
                fp.write(blob2)
                byte_counts[idx2] = len(blob2)
                tiles_written += 2
                valid_pixels += len(rows)
            done += 1
        if progress is not None:
            progress(done, tiles_per_plane)

    # ---- 回填瓦片索引 ----
    fp.seek(array_patch_pos[324])
    fp.write(longs(offsets))
    fp.seek(array_patch_pos[325])
    fp.write(longs(byte_counts))
    fp.flush()
    return {
        "width_px": width,
        "height_px": height,
        "tiles_total": tiles_per_plane,
        "tiles_written": tiles_written,
        "valid_pixels": valid_pixels,
    }


# ---------------------------------------------------------------- 任务管理

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ExportJob:
    id: str
    selection: ExportSelection
    status: str = "running"          # running / done / failed
    created_at: str = ""
    finished_at: str | None = None
    data_version: int | None = None
    file_name: str | None = None
    file_bytes: int | None = None
    tiles_done: int = 0
    tiles_total: int = 0
    valid_pixels: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["width_px"] = self.selection.width_px
        d["height_px"] = self.selection.height_px
        return d


class ExportManager:
    """导出任务注册表: 后台线程生成, 内存登记状态, 失败清理残件。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, ExportJob] = {}

    def start(
        self,
        sel: ExportSelection,
        export_dir: Path,
        *,
        background: bool = True,
    ) -> ExportJob:
        job = ExportJob(
            id=uuid.uuid4().hex[:12],
            selection=sel,
            created_at=_utcnow(),
            tiles_total=(-(-sel.width_px // TILE_SIZE))
            * (-(-sel.height_px // TILE_SIZE)),
        )
        with self._lock:
            self._jobs[job.id] = job
        if background:
            threading.Thread(
                target=self._run,
                args=(job, Path(export_dir)),
                name=f"export-{job.id}",
                daemon=True,
            ).start()
        else:
            self._run(job, Path(export_dir))
        return job

    def get(self, job_id: str) -> ExportJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[ExportJob]:
        with self._lock:
            return sorted(
                self._jobs.values(), key=lambda j: j.created_at, reverse=True
            )

    def _run(self, job: ExportJob, export_dir: Path) -> None:
        export_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = export_dir / f".{job.id}.tmp"
        conn = connect()
        try:
            # 同一只读快照事务: 期间补测提交不影响本次导出的两个波段
            with read_transaction(conn) as tx:
                version = get_data_version(tx)
                job.data_version = version
                with open(tmp_path, "w+b") as fp:
                    stats = write_geotiff(
                        fp,
                        tx,
                        job.selection,
                        data_version=version,
                        progress=lambda d, _t: setattr(job, "tiles_done", d),
                    )
            # 全部写完才原子改名出现; 之前磁盘上只有隐藏临时残件
            sel = job.selection
            final_name = (
                f"shallow8m_x{sel.x0}_y{sel.y0}"
                f"_x{sel.x1}_y{sel.y1}_v{version}_{job.id}.tif"
            )
            final_path = export_dir / final_name
            os.replace(tmp_path, final_path)
            job.file_name = final_name
            job.file_bytes = final_path.stat().st_size
            job.valid_pixels = stats["valid_pixels"]
            job.status = "done"
        except Exception as exc:  # 任何失败: 删除残件, 不交付文件
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        finally:
            conn.close()
            job.finished_at = _utcnow()


def export_dir() -> Path:
    """导出目录: EXPORT_DIR 环境变量, 否则放在数据库旁的 exports/。"""
    raw = os.environ.get("EXPORT_DIR")
    if raw:
        return Path(raw)
    return resolved_db_path(None).parent / "exports"


def ensure_export_dir(path: Path) -> Path:
    """创建导出目录; 默认 /data 不可写时回退临时目录 (与 database 一致)。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        return path
    except PermissionError:
        if not os.environ.get("EXPORT_DIR") and not os.environ.get("DB_PATH"):
            fallback = Path(tempfile.gettempdir()) / "dredging" / "exports"
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback
        raise


def sweep_tmp_files(path: Path) -> int:
    """清理上次进程遗留的导出残件 (崩溃时未删的临时文件)。返回清理数。"""
    removed = 0
    try:
        candidates = Path(path).glob(".*.tmp")
        for p in candidates:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    except OSError:
        pass
    return removed
