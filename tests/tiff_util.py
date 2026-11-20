"""测试/脚本共用: 最小 GeoTIFF 读取器, 用于校验导出文件。

只支持本系统写出的格式: little-endian classic TIFF, 无压缩, 瓦片,
PlanarConfiguration=separate, UInt32 像元。稀疏瓦片 (offset=0) 读为 None。
"""

from __future__ import annotations

import struct
from pathlib import Path

_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 12: 8}


class TiffError(ValueError):
    """文件结构不符合预期。"""


class TiffFile:
    def __init__(self, source):
        if isinstance(source, (str, Path)):
            data = Path(source).read_bytes()
        else:
            data = bytes(source)
        self.data = data
        if data[:2] != b"II":
            raise TiffError("不是 little-endian TIFF")
        magic, ifd_off = struct.unpack_from("<HI", data, 2)
        if magic != 42:
            raise TiffError(f"TIFF magic 错误: {magic}")
        (n,) = struct.unpack_from("<H", data, ifd_off)
        self.tags: dict[int, tuple[int, int, bytes]] = {}
        pos = ifd_off + 2
        for _ in range(n):
            tag, typ, count, valoff = struct.unpack_from("<HHII", data, pos)
            size = _TYPE_SIZE[typ] * count
            if size <= 4:
                raw = data[pos + 8: pos + 12][:size]
            else:
                raw = data[valoff: valoff + size]
            self.tags[tag] = (typ, count, raw)
            pos += 12

    def _values(self, tag: int, fmt: str) -> list:
        typ, count, raw = self.tags[tag]
        return list(struct.unpack(f"<{count}{fmt}", raw))

    def shorts(self, tag: int) -> list[int]:
        return self._values(tag, "H")

    def longs(self, tag: int) -> list[int]:
        return self._values(tag, "I")

    def doubles(self, tag: int) -> list[float]:
        return self._values(tag, "d")

    def ascii(self, tag: int) -> str:
        _, _, raw = self.tags[tag]
        return raw.split(b"\x00")[0].decode("ascii")

    @property
    def width(self) -> int:
        return self.longs(256)[0]

    @property
    def height(self) -> int:
        return self.longs(257)[0]

    @property
    def tile_size(self) -> int:
        return self.longs(322)[0]

    def tiles_per_plane(self) -> int:
        ta = -(-self.width // self.tile_size)
        td = -(-self.height // self.tile_size)
        return ta * td

    def tile_bytes(self, index: int) -> bytes | None:
        """取第 index 块瓦片的原始字节; 稀疏瓦片 (未写像元数据) 返回 None。"""
        off = self.longs(324)[index]
        cnt = self.longs(325)[index]
        if off == 0 or cnt == 0:
            return None
        return self.data[off: off + cnt]

    def pixel(self, plane: int, row: int, col: int) -> int | None:
        """读某波段 (0/1) 某像元; 行 0 = 图像顶部 (北)。稀疏瓦片返回 None。"""
        ts = self.tile_size
        ta = -(-self.width // ts)
        idx = self.tiles_per_plane() * plane + (row // ts) * ta + (col // ts)
        blob = self.tile_bytes(idx)
        if blob is None:
            return None
        (v,) = struct.unpack_from("<I", blob, ((row % ts) * ts + (col % ts)) * 4)
        return v

    def band_matrix(self, plane: int) -> list[list[int | None]]:
        """重组某波段全部像元为 [row][col] 矩阵 (仅适合小图)。"""
        return [
            [self.pixel(plane, row, col) for col in range(self.width)]
            for row in range(self.height)
        ]
