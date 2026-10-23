"""从旧版 .doc 中提取 PNG，不依赖第三方库。

.doc 是 OLE 复合文档，需先按 FAT 扇区链拼出连续流，再在其中查找图片。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

PNG_SIG = b"\x89PNG\r\n\x1a\n"
PNG_END = b"IEND\xaeB`\x82"
FREESECT = 0xFFFFFFFF   # 扇区未使用
ENDOFCHAIN = 0xFFFFFFFE  # 链到此为止——两个值不是一回事，混了就会越界


class Ole:
    def __init__(self, data: bytes) -> None:
        self.d = data
        self.ssz = 1 << struct.unpack_from("<H", data, 0x1E)[0]
        self.mssz = 1 << struct.unpack_from("<H", data, 0x20)[0]
        self.n_fat = struct.unpack_from("<I", data, 0x2C)[0]
        self.dir_start = struct.unpack_from("<I", data, 0x30)[0]
        self.mini_cutoff = struct.unpack_from("<I", data, 0x38)[0]
        self.mini_start = struct.unpack_from("<I", data, 0x3C)[0]
        self.fat = self._load_fat()
        self.entries = self._load_dir()

    def _sector(self, sid: int) -> bytes:
        off = 512 + sid * self.ssz
        if off + self.ssz > len(self.d):
            return b"\x00" * self.ssz
        return self.d[off : off + self.ssz]

    def _load_fat(self) -> list[int]:
        # DIFAT：header 里前 109 项，其余在 DIFAT 扇区链里
        difat = list(struct.unpack_from("<109I", self.d, 0x4C))
        sid = struct.unpack_from("<I", self.d, 0x44)[0]
        seen = 0
        while sid not in (FREESECT, ENDOFCHAIN) and seen < 1000:
            sec = self._sector(sid)
            vals = struct.unpack_from(f"<{self.ssz // 4}I", sec, 0)
            difat.extend(vals[:-1])
            sid = vals[-1]
            seen += 1

        fat: list[int] = []
        for fsid in difat[: self.n_fat]:
            if fsid in (FREESECT, ENDOFCHAIN):
                continue
            sec = self._sector(fsid)
            fat.extend(struct.unpack_from(f"<{self.ssz // 4}I", sec, 0))
        return fat

    def _chain(self, start: int) -> list[int]:
        out: list[int] = []
        sid = start
        while (
            sid not in (FREESECT, ENDOFCHAIN)
            and sid < len(self.fat)
            and len(out) < 100000
        ):
            out.append(sid)
            sid = self.fat[sid]
        return out

    def _load_dir(self) -> list[tuple[str, int, int, int]]:
        raw = b"".join(self._sector(s) for s in self._chain(self.dir_start))
        out = []
        for i in range(0, len(raw), 128):
            ent = raw[i : i + 128]
            if len(ent) < 128:
                break
            nlen = struct.unpack_from("<H", ent, 0x40)[0]
            if nlen < 2:
                continue
            name = ent[: nlen - 2].decode("utf-16-le", "ignore")
            typ = ent[0x42]
            start = struct.unpack_from("<I", ent, 0x74)[0]
            size = struct.unpack_from("<I", ent, 0x78)[0]
            out.append((name, typ, start, size))
        return out

    def stream(self, name: str) -> bytes | None:
        ent = next((e for e in self.entries if e[0] == name and e[1] == 2), None)
        if ent is None:
            return None
        _, _, start, size = ent
        if size < self.mini_cutoff:
            return self._mini(start, size)
        raw = b"".join(self._sector(s) for s in self._chain(start))
        return raw[:size]

    def _mini(self, start: int, size: int) -> bytes:
        root = next((e for e in self.entries if e[1] == 5), None)
        if root is None:
            return b""
        mini = b"".join(self._sector(s) for s in self._chain(root[2]))
        # mini FAT 与 FAT 结构相同，但条目是小扇区
        mfat_raw = b"".join(self._sector(s) for s in self._chain(self.mini_start))
        mfat = list(struct.unpack_from(f"<{len(mfat_raw) // 4}I", mfat_raw, 0))

        out = b""
        sid = start
        guard = 0
        while sid not in (FREESECT, 0xFFFFFFFE) and guard < 100000:
            off = sid * self.mssz
            out += mini[off : off + self.mssz]
            sid = mfat[sid] if sid < len(mfat) else FREESECT
            guard += 1
        return out[:size]


def find_png_bytes(data: bytes) -> list[bytes]:
    out: list[bytes] = []
    pos = 0
    while True:
        i = data.find(PNG_SIG, pos)
        if i < 0:
            break
        j = data.find(PNG_END, i)
        if j < 0:
            break
        end = j + len(PNG_END)
        blob = data[i:end]
        if len(blob) > 1024:
            out.append(blob)
        pos = end
    return out


def png_size(blob: bytes) -> tuple[int, int]:
    if len(blob) < 24:
        return (0, 0)
    return struct.unpack(">II", blob[16:24])


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    dst.mkdir(parents=True, exist_ok=True)

    ole = Ole(src.read_bytes())
    print("流:", ", ".join(f"{n}({s}KB)" for n, t, _, s in ole.entries if t == 2))

    blobs: list[bytes] = []
    for name, typ, _, size in ole.entries:
        if typ != 2 or size < 1024:
            continue
        data = ole.stream(name)
        if data:
            blobs.extend(find_png_bytes(data))

    # 去掉重复（WordDocument 与 Data 可能都含同一份）
    uniq: list[bytes] = []
    for b in blobs:
        if all(b != u for u in uniq):
            uniq.append(b)

    print(f"找到 {len(uniq)} 张 PNG")
    for n, blob in enumerate(uniq, 1):
        w, h = png_size(blob)
        name = f"fig_{n:02d}.png"
        (dst / name).write_bytes(blob)
        print(f"  {name}  {w}x{h}  {len(blob) // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
