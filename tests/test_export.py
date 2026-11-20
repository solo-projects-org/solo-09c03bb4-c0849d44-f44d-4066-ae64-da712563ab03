"""浅点筛查图导出: GeoTIFF 结构、像元汇总、稀疏瓦片、快照一致性与失败清理。"""

import asyncio
import io
import os
import tempfile
import unittest
from pathlib import Path

import httpx

from app import export as ex
from app.database import connect, get_data_version, init_db, read_transaction
from app.ingest import Reading, ingest_batch
from app.main import app

from tests._base import DBTestCase, csv_text
from tests.tiff_util import TiffFile

ND = ex.NODATA


def make_content_readings():
    """构造含零水深、空水深、缺测的后测读数 (选区 [0,0]-[24,24) -> 3x3 像元)。"""
    readings = []
    # 像元 (列0, 行2): x0..7,y0..7 全部有效, 含零水深 (1,1)=0
    for x in range(8):
        for y in range(8):
            d = 100 + (x * 7 + y * 13) % 100
            if (x, y) == (1, 1):
                d = 0
            readings.append(Reading(x, y, 1, d))
    # 像元 (列1, 行2): 只有 5 个有效细格, 其余缺测
    readings += [
        Reading(8, 0, 1, 50), Reading(9, 1, 1, 60), Reading(10, 2, 1, 70),
        Reading(11, 3, 1, 80), Reading(12, 4, 1, 90),
    ]
    # 像元 (列2, 行2): 有读数但全部空水深 -> NoData
    for x in range(16, 24):
        for y in range(8):
            readings.append(Reading(x, y, 1, None))
    # 像元 (列1, 行1): 3 个有效 (含零水深) + 1 个空水深 + 其余缺测
    readings += [
        Reading(8, 8, 1, 0), Reading(9, 9, 1, 10), Reading(10, 10, 1, 20),
        Reading(11, 11, 1, None),
    ]
    # 像元 (列2, 行1): 单个有效细格
    readings.append(Reading(20, 12, 1, 777))
    # 其余像元 (y8..15 列0, y16..23 整行): 无任何读数 -> NoData
    return readings


class SelectionValidationTest(unittest.TestCase):
    def test_ok(self):
        sel = ex.validate_selection(0, 0, 64, 64)
        self.assertEqual((sel.width_px, sel.height_px), (8, 8))

    def test_must_align_8m_grid(self):
        with self.assertRaises(ex.ExportError):
            ex.validate_selection(4, 0, 64, 64)
        with self.assertRaises(ex.ExportError):
            ex.validate_selection(0, 0, 64, 63)

    def test_empty_rejected(self):
        with self.assertRaises(ex.ExportError):
            ex.validate_selection(64, 0, 64, 64)
        with self.assertRaises(ex.ExportError):
            ex.validate_selection(64, 0, 32, 64)

    def test_side_limit_65536m(self):
        with self.assertRaises(ex.ExportError):
            ex.validate_selection(0, 0, 65536 + 8, 64)
        sel = ex.validate_selection(0, 0, 65536, 65536)  # 上限本身允许
        self.assertEqual((sel.width_px, sel.height_px), (8192, 8192))

    def test_negative_coords_ok(self):
        sel = ex.validate_selection(-16, -16, 16, 16)
        self.assertEqual((sel.width_px, sel.height_px), (4, 4))


class GeoTiffContentTest(DBTestCase):
    def setUp(self):
        super().setUp()
        ingest_batch(self.conn, "after", make_content_readings())

    def _render(self, x0, y0, x1, y1):
        buf = io.BytesIO()
        with read_transaction(self.conn) as tx:
            version = get_data_version(tx)
            stats = ex.write_geotiff(
                buf, tx, ex.validate_selection(x0, y0, x1, y1),
                data_version=version, generated_at="2026-11-20T00:00:00+00:00",
            )
        return TiffFile(buf.getvalue()), stats, version

    def test_pixel_aggregation_zero_depth_and_nodata(self):
        tiff, stats, _ = self._render(0, 0, 24, 24)
        self.assertEqual((tiff.width, tiff.height), (3, 3))
        # 波段1: 最浅有效水深 (行0 = 最北 = y16..23)
        self.assertEqual(tiff.band_matrix(0), [
            [ND, ND, ND],
            [ND, 0, 777],
            [0, 50, ND],
        ])
        # 波段2: 有效细格数
        self.assertEqual(tiff.band_matrix(1), [
            [ND, ND, ND],
            [ND, 3, 1],
            [64, 5, ND],
        ])
        self.assertEqual(stats["valid_pixels"], 4)
        self.assertEqual(stats["tiles_written"], 2)  # 1 瓦片 x 2 波段

    def test_geotiff_tags_and_version_metadata(self):
        tiff, _, version = self._render(0, 0, 24, 24)
        self.assertEqual(version, 1)
        self.assertEqual(tiff.shorts(258), [32, 32])      # BitsPerSample
        self.assertEqual(tiff.shorts(259), [1])           # 无压缩
        self.assertEqual(tiff.shorts(277), [2])           # 双波段
        self.assertEqual(tiff.shorts(284), [2])           # planar separate
        self.assertEqual(tiff.shorts(339), [1, 1])        # UInt
        self.assertEqual(tiff.longs(322), [256])          # 瓦片 256
        self.assertEqual(tiff.doubles(33550), [8.0, 8.0, 0.0])
        # 北向上: 左上角 (列0,行0) 对应 (x0, y1), y 向北递减
        self.assertEqual(tiff.doubles(33922), [0.0, 0.0, 0.0, 0.0, 24.0, 0.0])
        keys = tiff.shorts(34735)
        self.assertIn(32767, keys)                        # 用户自定义投影
        self.assertIn(9001, keys)                         # 单位米
        self.assertIn("Harbor metric grid", tiff.ascii(34737))
        self.assertEqual(tiff.ascii(42113), str(ND))      # GDAL NoData
        self.assertIn(f"data_version={version}", tiff.ascii(270))
        self.assertIn(
            f'<Item name="data_version">{version}</Item>', tiff.ascii(42112)
        )

    def test_negative_coords(self):
        ingest_batch(self.conn, "after",
                     [Reading(-8, -8, 1, 42), Reading(-1, -1, 1, 7)])
        tiff, _, _ = self._render(-16, -16, 16, 16)
        # 格 (-8,-8) 与 (-1,-1) 同属像元 (列1, 行2): min(42,7)=7, 2 个有效
        self.assertEqual(tiff.pixel(0, 2, 1), 7)
        self.assertEqual(tiff.pixel(1, 2, 1), 2)
        self.assertEqual(tiff.doubles(33922), [0.0, 0.0, 0.0, -16.0, 16.0, 0.0])


class SparseAndEmptyTest(DBTestCase):
    def test_empty_db_all_tiles_sparse(self):
        buf = io.BytesIO()
        with read_transaction(self.conn) as tx:
            stats = ex.write_geotiff(
                buf, tx, ex.validate_selection(0, 0, 64, 64), data_version=0)
        tiff = TiffFile(buf.getvalue())
        self.assertEqual(stats["tiles_written"], 0)
        self.assertTrue(all(o == 0 for o in tiff.longs(324)))
        self.assertTrue(all(c == 0 for c in tiff.longs(325)))

    def test_sparse_65536m_selection(self):
        # 只在两个对角附近有数据: 原点 (含零水深) 与东北角
        ingest_batch(self.conn, "after", [
            Reading(0, 0, 1, 0), Reading(1, 0, 1, 10), Reading(0, 1, 1, 20),
            Reading(65535, 65535, 1, 40),
        ])
        buf = io.BytesIO()
        with read_transaction(self.conn) as tx:
            stats = ex.write_geotiff(
                buf, tx, ex.validate_selection(0, 0, 65536, 65536),
                data_version=get_data_version(tx))
        self.assertEqual(stats["tiles_total"], 32 * 32)
        self.assertEqual(stats["tiles_written"], 4)      # 2 瓦片 x 2 波段
        raw = buf.getvalue()
        # 稀疏: 8192x8192 像元全量需 512MB, 实际只写了 4 块瓦片
        self.assertLess(len(raw), 2_000_000)
        tiff = TiffFile(raw)
        offsets = tiff.longs(324)
        self.assertEqual(sum(1 for o in offsets if o != 0), 4)
        # 原点像元 (行 8191, 列 0): 零水深为最浅, 3 个有效细格
        self.assertEqual(tiff.pixel(0, 8191, 0), 0)
        self.assertEqual(tiff.pixel(1, 8191, 0), 3)
        # 东北角像元 (行 0, 列 8191)
        self.assertEqual(tiff.pixel(0, 0, 8191), 40)
        self.assertEqual(tiff.pixel(1, 0, 8191), 1)
        # 中间全空: 稀疏瓦片未写像元数据
        self.assertIsNone(tiff.pixel(0, 4000, 4000))
        self.assertIsNone(tiff.pixel(1, 4000, 4000))


class SnapshotDuringExportTest(DBTestCase):
    def test_export_reads_single_committed_snapshot(self):
        """导出进行中补测: 两个波段仍取生成开始时的同一份已提交数据。"""
        ingest_batch(self.conn, "after",
                     [Reading(x, y, 1, 500) for x in range(8) for y in range(16)])
        version_before = get_data_version(self.conn)
        conn2 = connect(self.db_path)  # 另一连接模拟导出期间的补测
        state = {"ingested": False}

        def hook(done, _total):
            if not state["ingested"]:
                # 第一行瓦片写完后补测: 全部改成 100cm (更浅), 版本 +1
                ingest_batch(conn2, "after",
                             [Reading(x, y, 2, 100)
                              for x in range(8) for y in range(16)])
                state["ingested"] = True

        old_tile = ex.TILE_SIZE
        ex.TILE_SIZE = 1  # 1 像元 1 瓦片, 让补测落在两行瓦片之间
        try:
            buf = io.BytesIO()
            with read_transaction(self.conn) as tx:
                version = get_data_version(tx)
                ex.write_geotiff(
                    buf, tx, ex.validate_selection(0, 0, 8, 16),
                    data_version=version, progress=hook)
        finally:
            ex.TILE_SIZE = old_tile
            conn2.close()

        self.assertTrue(state["ingested"])
        self.assertEqual(version, version_before)
        tiff = TiffFile(buf.getvalue())
        # 两个波段全部仍是旧快照的 500cm, 而不是补测后的 100cm
        for row in range(2):
            self.assertEqual(tiff.pixel(0, row, 0), 500)
            self.assertEqual(tiff.pixel(1, row, 0), 64)
        self.assertIn(f"data_version={version_before}", tiff.ascii(270))
        # 补测本身已正常提交
        self.assertEqual(get_data_version(self.conn), version_before + 1)


class ExportManagerTest(DBTestCase):
    def setUp(self):
        super().setUp()
        self.export_dir = Path(self.tmp.name) / "exports"

    def test_successful_export_renames_tmp(self):
        ingest_batch(self.conn, "after",
                     [Reading(0, 0, 1, 0), Reading(1, 0, 1, 30)])
        manager = ex.ExportManager()
        job = manager.start(
            ex.validate_selection(0, 0, 8, 8), self.export_dir, background=False)
        self.assertEqual(job.status, "done")
        self.assertEqual(job.data_version, 1)
        self.assertIn(f"_v{job.data_version}_", job.file_name)
        path = self.export_dir / job.file_name
        self.assertTrue(path.is_file())
        self.assertEqual(job.file_bytes, path.stat().st_size)
        # 临时残件不存在
        self.assertEqual([p.name for p in self.export_dir.iterdir()],
                         [job.file_name])
        tiff = TiffFile(path)
        self.assertEqual(tiff.pixel(0, 0, 0), 0)   # 零水深有效
        self.assertEqual(tiff.pixel(1, 0, 0), 2)

    def test_failed_export_leaves_no_files(self):
        ingest_batch(self.conn, "after", [Reading(0, 0, 1, 100)])
        manager = ex.ExportManager()
        original = ex.write_geotiff

        def boom(fp, conn, sel, **kw):
            fp.write(b"partial junk")
            raise RuntimeError("模拟写盘失败")

        ex.write_geotiff = boom
        try:
            job = manager.start(
                ex.validate_selection(0, 0, 16, 16), self.export_dir,
                background=False)
        finally:
            ex.write_geotiff = original
        self.assertEqual(job.status, "failed")
        self.assertIn("模拟写盘失败", job.error)
        self.assertEqual(list(self.export_dir.iterdir()), [])  # 残件已清理

    def test_sweep_tmp_files(self):
        self.export_dir.mkdir()
        (self.export_dir / ".abc123.tmp").write_bytes(b"junk")
        (self.export_dir / "keep.tif").write_bytes(b"ok")
        self.assertEqual(ex.sweep_tmp_files(self.export_dir), 1)
        self.assertEqual([p.name for p in self.export_dir.iterdir()],
                         ["keep.tif"])


class ExportApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "api.db"
        os.environ["DB_PATH"] = str(self.db_path)
        conn = connect(self.db_path)
        init_db(conn)
        conn.close()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        self.tmp.cleanup()
        os.environ.pop("DB_PATH", None)

    async def _upload(self, phase, rows):
        return await self.client.post(
            f"/api/surveys/{phase}",
            files={"file": ("s.csv", csv_text(rows), "text/csv")},
        )

    async def _wait_job(self, job_id):
        for _ in range(400):
            await asyncio.sleep(0.05)
            r = await self.client.get(f"/api/exports/{job_id}")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            if body["status"] != "running":
                return body
        self.fail("导出任务超时未完成")

    async def test_export_workflow_zero_depth_and_missing(self):
        # 后测: x0..7,y0..7 全有效且 (0,0)=0cm; (8,0) 空水深; (9,0)=55; 其余缺测
        rows = [
            (x, y, 1, 0 if (x, y) == (0, 0) else 100 + x + y)
            for x in range(8) for y in range(8)
        ]
        rows += [(8, 0, 1, None), (9, 0, 1, 55)]
        r = await self._upload("after", rows)
        self.assertEqual(r.status_code, 200, r.text)

        r = await self.client.post(
            "/api/exports", json={"x0": 0, "y0": 0, "x1": 16, "y1": 16})
        self.assertEqual(r.status_code, 202, r.text)
        job = r.json()
        self.assertEqual(job["status"], "running")

        done = await self._wait_job(job["id"])
        self.assertEqual(done["status"], "done", done)
        self.assertEqual(done["data_version"], 1)
        self.assertEqual((done["width_px"], done["height_px"]), (2, 2))

        r = await self.client.get(f"/api/exports/{job['id']}/file")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/tiff")
        tiff = TiffFile(r.content)
        # 像元 (列0, 行1) = x0..7,y0..7: 零水深为最浅, 64 个有效
        self.assertEqual(tiff.pixel(0, 1, 0), 0)
        self.assertEqual(tiff.pixel(1, 1, 0), 64)
        # 像元 (列1, 行1) = x8..15,y0..7: 空水深不算, 只有 (9,0)=55
        self.assertEqual(tiff.pixel(0, 1, 1), 55)
        self.assertEqual(tiff.pixel(1, 1, 1), 1)
        # 像元 (列0, 行0) = x0..7,y8..15: 缺测 -> 两波段 NoData
        self.assertEqual(tiff.pixel(0, 0, 0), ND)
        self.assertEqual(tiff.pixel(1, 0, 0), ND)
        # 文件内注明数据版本
        self.assertIn("data_version=1", tiff.ascii(270))
        self.assertIn('<Item name="data_version">1</Item>', tiff.ascii(42112))

        # 任务列表包含该任务
        r = await self.client.get("/api/exports")
        self.assertEqual(r.status_code, 200)
        self.assertIn(job["id"], [j["id"] for j in r.json()["exports"]])

    async def test_export_validation_and_unknown_job(self):
        r = await self.client.post(
            "/api/exports", json={"x0": 4, "y0": 0, "x1": 16, "y1": 16})
        self.assertEqual(r.status_code, 400)
        r = await self.client.post(
            "/api/exports", json={"x0": 0, "y0": 0, "x1": 65544, "y1": 16})
        self.assertEqual(r.status_code, 400)
        r = await self.client.post(
            "/api/exports", json={"x0": 16, "y0": 0, "x1": 16, "y1": 16})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(
            (await self.client.get("/api/exports/nope")).status_code, 404)
        self.assertEqual(
            (await self.client.get("/api/exports/nope/file")).status_code, 404)

    async def test_failed_job_not_downloadable(self):
        original = ex.write_geotiff
        ex.write_geotiff = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("磁盘满"))
        try:
            r = await self.client.post(
                "/api/exports", json={"x0": 0, "y0": 0, "x1": 8, "y1": 8})
            self.assertEqual(r.status_code, 202, r.text)
            done = await self._wait_job(r.json()["id"])
        finally:
            ex.write_geotiff = original
        self.assertEqual(done["status"], "failed")
        r = await self.client.get(f"/api/exports/{done['id']}/file")
        self.assertEqual(r.status_code, 409)


if __name__ == "__main__":
    unittest.main()
