#!/usr/bin/env python3
"""ビルド直後の ROM を1行で要約する。"""
import sys


def main(argv):
    path = argv[1]
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"NES\x1a":
        sys.stderr.write("romstat: iNES ヘッダが不正: %s\n" % path)
        return 1
    prg = data[4] * 16384
    chr_ = data[5] * 8192
    mapper = (data[6] >> 4) | (data[7] & 0xF0)
    print("romstat: %s  mapper=%d  PRG=%dKB (%dバンク)  CHR=%dKB  合計=%d バイト"
          % (path, mapper, prg // 1024, prg // 8192, chr_ // 1024, len(data)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
