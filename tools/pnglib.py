"""最小限の PNG 読み書き。Python 標準ライブラリのみで動く。

外部依存（Pillow 等）を足さない方針のため自前で持つ。
対応するのはこのプロジェクトで扱う範囲だけ:
  - 読み: 8bit インデックスカラー (color type 3) / 8bit グレースケール (color type 0)
  - 書き: 8bit インデックスカラー (color type 3)
  - インタレース非対応（インタレース PNG はエラーにする）
"""
import struct
import zlib

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class PngError(Exception):
    pass


def _chunks(data):
    if data[:8] != PNG_MAGIC:
        raise PngError("PNG シグネチャが不正")
    pos = 8
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        yield ctype, body


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter(raw, width, height, bpp):
    out = []
    prev = bytearray(width * bpp)
    pos = 0
    for y in range(height):
        ftype = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + width * bpp])
        pos += width * bpp
        if ftype == 0:
            pass
        elif ftype == 1:
            for i in range(bpp, len(line)):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ftype == 2:
            for i in range(len(line)):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(len(line)):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(len(line)):
                left = line[i - bpp] if i >= bpp else 0
                upleft = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(left, prev[i], upleft)) & 0xFF
        else:
            raise PngError("未知のフィルタ種別 %d (行 %d)" % (ftype, y))
        out.append(line)
        prev = line
    return out


def read_indexed_png(path):
    """PNG を読み、(width, height, rows) を返す。rows は各行のインデックス値の bytearray。"""
    with open(path, "rb") as f:
        data = f.read()

    width = height = depth = ctype = None
    idat = b""
    for name, body in _chunks(data):
        if name == b"IHDR":
            width, height, depth, ctype, comp, filt, interlace = struct.unpack(">IIBBBBB", body)
            if interlace != 0:
                raise PngError("インタレース PNG には対応しない: %s" % path)
            if depth != 8:
                raise PngError("8bit 深度の PNG のみ対応する (深度 %d): %s" % (depth, path))
            if ctype not in (0, 3):
                raise PngError("インデックスカラーまたはグレースケールのみ対応する "
                               "(color type %d): %s" % (ctype, path))
        elif name == b"IDAT":
            idat += body
        elif name == b"IEND":
            break

    if width is None:
        raise PngError("IHDR が見つからない: %s" % path)

    return width, height, _unfilter(zlib.decompress(idat), width, height, 1)


def write_indexed_png(path, width, height, rows, palette):
    """8bit インデックスカラー PNG を書く。palette は (r,g,b) のリスト。"""
    raw = bytearray()
    for y in range(height):
        raw.append(0)               # フィルタなし。diff を安定させるため常に 0 で書く
        raw.extend(rows[y])

    def chunk(name, body):
        return (struct.pack(">I", len(body)) + name + body
                + struct.pack(">I", zlib.crc32(name + body) & 0xFFFFFFFF))

    plte = b"".join(bytes(c) for c in palette)
    out = (PNG_MAGIC
           + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 3, 0, 0, 0))
           + chunk(b"PLTE", plte)
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(out)
