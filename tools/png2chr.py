#!/usr/bin/env python3
"""PNG を NES の CHR（2bpp プレーナ形式）に変換する。

画像を正とし、リポジトリで diff が見えるようにするための変換器。
入力の検証に失敗したら**ビルドを止める**（壊れたデータを黙って通さない）。

使い方:
    python3 tools/png2chr.py chr/sprites.png build/chr/sprites.chr [--pages 1]

制約:
    - 幅・高さは 8 の倍数
    - 画素値は 0..3（NES の1パレット4色に対応するインデックス）
    - タイルは左上から行優先で並ぶ。128x128 の画像 = 256 タイル = 4KB = CHR 1ページ
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pnglib import read_indexed_png, PngError   # noqa: E402

TILE_BYTES = 16
PAGE_BYTES = 4096


def convert(src, dst, pages=None):
    width, height, rows = read_indexed_png(src)

    if width % 8 or height % 8:
        raise PngError("幅・高さは8の倍数でなければならない: %s (%dx%d)" % (src, width, height))

    for y in range(height):
        for x in range(width):
            if rows[y][x] > 3:
                raise PngError("画素値は 0..3 でなければならない: %s (x=%d, y=%d, 値=%d)"
                               % (src, x, y, rows[y][x]))

    out = bytearray()
    for ty in range(height // 8):
        for tx in range(width // 8):
            plane0 = bytearray()
            plane1 = bytearray()
            for row in range(8):
                b0 = b1 = 0
                line = rows[ty * 8 + row]
                for col in range(8):
                    v = line[tx * 8 + col]
                    b0 = (b0 << 1) | (v & 1)
                    b1 = (b1 << 1) | ((v >> 1) & 1)
                plane0.append(b0)
                plane1.append(b1)
            out += plane0 + plane1

    if pages is not None:
        want = pages * PAGE_BYTES
        if len(out) > want:
            raise PngError("CHR が %d ページに収まらない: %s (%d バイト > %d バイト)"
                           % (pages, src, len(out), want))
        out += bytes(want - len(out))

    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    with open(dst, "wb") as f:
        f.write(out)
    return len(out)


def main(argv):
    if len(argv) < 3:
        sys.stderr.write(__doc__)
        return 2
    pages = None
    if "--pages" in argv:
        pages = int(argv[argv.index("--pages") + 1])
    try:
        n = convert(argv[1], argv[2], pages)
    except PngError as e:
        sys.stderr.write("png2chr: %s\n" % e)
        return 1
    print("png2chr: %s -> %s (%d バイト, %d タイル)" % (argv[1], argv[2], n, n // TILE_BYTES))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
