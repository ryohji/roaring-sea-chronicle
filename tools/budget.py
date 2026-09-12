#!/usr/bin/env python3
"""PRG/CHR の ROM 予算を計測し docs/budget.md を更新する。

MMC3 はバンク単位で詰むので、**バンク単位**で報告する。
「全体では余っている」という報告には意味がない。

予算を超過したら非0で終了し、CI を落とす（CLAUDE.md 第4条）。

    python3 tools/budget.py build/roaring.map --rom build/roaring.nes --out docs/budget.md
"""
import argparse
import datetime
import re
import sys

# 予算の定義。ここを緩めるのは「設計の後退」であり、理由を PR に書くこと。
# 値は各メモリ領域の使用率上限（%）。
BUDGET = {
    "PRG31": 90.0,     # 固定バンク。ここが詰むとリセット/NMI/コアが入らなくなる
    "PRG30": 90.0,     # 固定バンク
    "_DEFAULT_PRG": 95.0,
    "CHRROM": 95.0,
    "ZP": 90.0,        # ゼロページは枯渇が最も痛い
    "RAM": 90.0,
}

# 設計上つねに満杯になる領域。使用率での監視に意味がない。
FIXED_AREAS = ("HDR", "VEC", "OAMBUF", "STACK", "PRGRAM")

# テキスト系セグメントは別枠で監視する（PRG の最大リスクだから）。
TEXT_SEGMENTS = ("TEXTDATA", "DIALOG", "DICT")


def parse_map(path):
    """ld65 の map ファイルの Segment list からセグメント名とサイズを読む。"""
    segments = {}
    in_seg = False
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("Segment list:"):
                in_seg = True
                continue
            if in_seg:
                if line.startswith("Exports list") or line.startswith("Imports list"):
                    break
                m = re.match(r"^(\S+)\s+([0-9A-Fa-f]{6})\s+([0-9A-Fa-f]{6})\s+([0-9A-Fa-f]{6})", line)
                if m:
                    segments[m.group(1)] = {
                        "name": m.group(1),
                        "start": int(m.group(2), 16),
                        "size": int(m.group(4), 16),
                    }
    return segments


def parse_cfg(path):
    """リンカスクリプトからメモリ領域の容量と、セグメントの配置先を読む。

    ld65 2.18 の map には Memory area list が無いので、バンク単位の集計は
    リンカスクリプトを正として自前で行う。バンク構成の真実源は cfg である。
    """
    text = open(path, encoding="utf-8").read()
    text = re.sub(r"#.*", "", text)

    areas = []
    seg_to_area = {}

    m = re.search(r"MEMORY\s*\{(.*?)\n\}", text, re.S)
    if m:
        for entry in re.finditer(r"(\w+)\s*:(.*?);", m.group(1), re.S):
            name, body = entry.group(1), entry.group(2)
            size = re.search(r"size\s*=\s*\$?([0-9A-Fa-f]+)", body)
            start = re.search(r"start\s*=\s*\$?([0-9A-Fa-f]+)", body)
            has_file = "file" in body
            if size:
                areas.append({
                    "name": name,
                    "start": int(start.group(1), 16) if start else 0,
                    "size": int(size.group(1), 16),
                    "in_rom": has_file,
                })

    m = re.search(r"SEGMENTS\s*\{(.*?)\n\}", text, re.S)
    if m:
        for entry in re.finditer(r"(\w+)\s*:(.*?);", m.group(1), re.S):
            load = re.search(r"load\s*=\s*(\w+)", entry.group(2))
            if load:
                seg_to_area[entry.group(1)] = load.group(1)

    return areas, seg_to_area


def limit_for(name):
    if name in BUDGET:
        return BUDGET[name]
    if name.startswith("PRG"):
        return BUDGET["_DEFAULT_PRG"]
    return 100.0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("mapfile")
    ap.add_argument("--rom")
    ap.add_argument("--out", default="docs/budget.md")
    ap.add_argument("--cfg", default="cfg/mmc3.cfg")
    args = ap.parse_args(argv)

    segments = parse_map(args.mapfile)
    areas, seg_to_area = parse_cfg(args.cfg)
    if not areas:
        sys.stderr.write("budget: リンカスクリプトを解釈できない: %s\n" % args.cfg)
        return 2
    if not segments:
        sys.stderr.write("budget: map ファイルを解釈できない: %s\n" % args.mapfile)
        return 2

    used_by_area = {}
    for seg in segments.values():
        area = seg_to_area.get(seg["name"])
        if area is None:
            sys.stderr.write("budget: セグメント %s の配置先が cfg に無い\n" % seg["name"])
            return 2
        used_by_area.setdefault(area, 0)
        used_by_area[area] += seg["size"]

    over = []
    rows = []
    for a in areas:
        used = used_by_area.get(a["name"], 0)
        if a["size"] == 0:
            continue
        if used == 0 and a["name"].startswith("PRG") and a["name"] not in BUDGET:
            continue                      # 未使用の可変バンクは表を汚すだけなので省く
        pct = used * 100.0 / a["size"]
        lim = limit_for(a["name"])
        if a["name"] in FIXED_AREAS:
            rows.append((a["name"], a["size"], used, pct, 100.0, "固定"))
            continue
        state = "OK"
        if pct > lim:
            state = "**超過**"
            over.append((a["name"], pct, lim))
        elif pct > lim - 10.0:
            state = "注意"
        rows.append((a["name"], a["size"], used, pct, lim, state))

    bank_areas = [a for a in areas if re.match(r"^PRG\d\d$", a["name"])]
    unused_banks = sum(1 for a in bank_areas if used_by_area.get(a["name"], 0) == 0)

    text_used = sum(v["size"] for k, v in segments.items() if k in TEXT_SEGMENTS)

    lines = []
    lines.append("# ROM 予算の現況")
    lines.append("")
    lines.append("`make budget` が自動生成する。手で編集しない。")
    lines.append("")
    lines.append("- 生成日時: %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("- map: `%s`" % args.mapfile)
    if args.rom:
        try:
            with open(args.rom, "rb") as f:
                rom = f.read()
            lines.append("- ROM: `%s` (%d バイト / PRG %dKB / CHR %dKB)"
                         % (args.rom, len(rom), rom[4] * 16, rom[5] * 8))
        except OSError:
            pass
    lines.append("")
    lines.append("## メモリ領域（バンク単位）")
    lines.append("")
    lines.append("| 領域 | 容量 | 使用 | 使用率 | 上限 | 判定 |")
    lines.append("|---|---:|---:|---:|---:|---|")
    for name, size, used, pct, lim, state in rows:
        lines.append("| %s | %d | %d | %.1f%% | %.0f%% | %s |" % (name, size, used, pct, lim, state))
    lines.append("")
    lines.append("")
    lines.append("未使用の PRG バンク: **%d / %d**" % (unused_banks, len(bank_areas)))
    lines.append("")
    lines.append("## テキスト（PRG の最大リスク）")
    lines.append("")
    lines.append("- テキスト系セグメント合計: **%d バイト**" % text_used)
    lines.append("")
    lines.append("> 企画書 §7: テキスト圧縮を後回しにすると、最後に削る対象は必ずキャラクターの個性になる。")
    lines.append("")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print("budget: %d 領域を計測 -> %s" % (len(rows), args.out))
    for name, size, used, pct, lim, state in rows:
        if state not in ("OK", "固定"):
            print("  %-8s %6d/%6d バイト (%.1f%%, 上限 %.0f%%) %s" % (name, used, size, pct, lim, state))

    if over:
        sys.stderr.write("\nbudget: 予算超過 %d 件\n" % len(over))
        for name, pct, lim in over:
            sys.stderr.write("  %s: %.1f%% > 上限 %.0f%%\n" % (name, pct, lim))
        sys.stderr.write("機能追加より削減を優先すること（CLAUDE.md 第4条）。\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
