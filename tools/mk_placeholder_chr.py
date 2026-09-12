#!/usr/bin/env python3
"""P0 の暫定 CHR（chr/sprites.png）を生成する。

CHR の正は PNG であり、通常はドット絵ツールで直接編集する。
このスクリプトは「最初の1枚」を作るためだけのもので、
本番のグラフィックが入ったら役目を終える。

    python3 tools/mk_placeholder_chr.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pnglib import write_indexed_png   # noqa: E402

WIDTH = HEIGHT = 128          # 16x16 タイル = 256 タイル = 4KB = CHR 1ページ

# 8x16 スプライト1体。NES の 8x16 モードはタイル N が上半分、N+1 が下半分なので、
# 画像上では上半分をタイル0（左上）、下半分をタイル1（その右隣）に置く。
# 0=透明, 1=輪郭, 2=影, 3=明
FIGURE = [
    "..1111..",
    ".133331.",
    ".132231.",
    ".133331.",
    "..1331..",
    ".111111.",
    "1132231 ",
    "1133331 ",
    ".133331.",
    ".132231.",
    ".133331.",
    "..1221..",
    "..1..1..",
    "..1..1..",
    ".11..11.",
    ".22..22.",
]

# NES パレット相当の見た目（PNG 上の確認用。実際の色は ROM 側のパレットが決める）
PALETTE = [(0, 0, 0), (32, 24, 56), (168, 96, 64), (248, 216, 168)] + [(255, 0, 255)] * 252


def main():
    rows = [bytearray(WIDTH) for _ in range(HEIGHT)]

    for y, line in enumerate(FIGURE):
        # 上半分 -> タイル0 (x=0..7) / 下半分 -> タイル1 (x=8..15)
        tile_x = 0 if y < 8 else 8
        dst_y = y if y < 8 else y - 8
        for x, ch in enumerate(line):
            rows[dst_y][tile_x + x] = 0 if ch in ". " else int(ch)

    # タイル 2（右隣）に当たり判定確認用の 8x8 の枠を置く
    for i in range(8):
        for (yy, xx) in ((0, i), (7, i), (i, 0), (i, 7)):
            rows[yy][16 + xx] = 1

    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "chr", "sprites.png")
    write_indexed_png(out, WIDTH, HEIGHT, rows, PALETTE)
    print("wrote %s (%dx%d)" % (out, WIDTH, HEIGHT))


if __name__ == "__main__":
    main()
