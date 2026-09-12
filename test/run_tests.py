#!/usr/bin/env python3
"""回帰テストの入口。

3層構成（ADR-0004）:
  L1 構造検証   — iNES ヘッダ、バンク構成、ベクタ。ROM が「NES の ROM として正しい」か
  L2 実行検証   — Python 6502 エミュレータでリセットから走らせ、RAM/OAM/PPU を検証
  L3 自動プレイ — Mesen2 + Lua（環境変数 MESEN があるときだけ実行）

失敗メッセージは「何が期待と違ったか」が1行で分かるように書くこと。
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "harness"))

from cpu6502 import CpuCrash            # noqa: E402
from nes import Nes, Rom, load_labels   # noqa: E402
from routes import parse_route, play, RouteError   # noqa: E402


class Results:
    def __init__(self):
        self.passed = 0
        self.failed = []

    def check(self, name, ok, detail=""):
        if ok:
            self.passed += 1
            print("  PASS  %s" % name)
        else:
            self.failed.append((name, detail))
            print("  FAIL  %s\n        %s" % (name, detail))
        return ok


# ---------------------------------------------------------------- L1
def layer1_structure(rom_path, labels, r):
    print("L1 構造検証")
    try:
        rom = Rom(rom_path)
    except ValueError as e:
        r.check("ROM を読める", False, str(e))
        return None

    r.check("iNES ヘッダのマッパー番号が 4 (MMC3)", rom.mapper == 4,
            "mapper=%d。cfg/mmc3.cfg と src/header.s の不整合を疑え" % rom.mapper)
    r.check("PRG-ROM が 256KB (8KB × 32 バンク)", len(rom.prg) == 256 * 1024,
            "PRG=%d バイト。企画書 §7 の推奨構成は 256KB" % len(rom.prg))
    r.check("CHR-ROM が 128KB", len(rom.chr) == 128 * 1024,
            "CHR=%d バイト。企画書 §7 の推奨構成は 128KB" % len(rom.chr))
    r.check("ROM 全体のサイズがヘッダと一致", rom.size == 16 + len(rom.prg) + len(rom.chr),
            "size=%d, 期待=%d" % (rom.size, 16 + len(rom.prg) + len(rom.chr)))

    # ベクタは最終バンク（$E000-$FFFF）の末尾にある
    vec = rom.prg[-6:]
    nmi = vec[0] | (vec[1] << 8)
    reset = vec[2] | (vec[3] << 8)
    irq = vec[4] | (vec[5] << 8)
    for name, addr in (("NMI", nmi), ("RESET", reset), ("IRQ", irq)):
        r.check("%s ベクタが固定バンク内を指す ($%04X)" % (name, addr), 0xE000 <= addr <= 0xFFF9,
                "$%04X は $E000-$FFF9 の外。固定バンクに置かれていないハンドラは "
                "バンク切替後に飛べなくなる" % addr)

    if labels:
        for vec_name, addr, label in (("NMI", nmi, "nmi_handler"),
                                      ("RESET", reset, "reset_handler"),
                                      ("IRQ", irq, "irq_handler")):
            want = labels.get(label)
            if want is not None:
                r.check("%s ベクタが %s を指す" % (vec_name, label), addr == want,
                        "ベクタ=$%04X, %s=$%04X" % (addr, label, want))

    r.check("CHR の先頭ページにタイルが入っている", any(rom.chr[:0x1000]),
            "CHR 先頭 4KB が全て 0。chr/*.png の変換に失敗している可能性がある")
    return rom


# ---------------------------------------------------------------- L2
def layer2_execution(rom_path, labels, r):
    print("L2 実行検証（Python 6502 エミュレータ）")

    try:
        nes = Nes(rom_path)
        nes.reset()
        nes.run_frames(10)
    except CpuCrash as e:
        r.check("リセットから10フレーム走る", False, str(e))
        return
    r.check("リセットから10フレーム、クラッシュせずに走る", True)

    r.check("NMI が有効になっている", nes.ppu.nmi_enabled,
            "PPUCTRL=$%02X。bit7 が立っていない＝NMI が来ないので、"
            "メインループが wait_nmi で止まる" % nes.ppu.ctrl)
    r.check("スプライト表示が有効", bool(nes.ppu.mask & 0x10),
            "PPUMASK=$%02X。bit4 が立っていないとスプライトが出ない" % nes.ppu.mask)
    r.check("8x16 スプライトモード", bool(nes.ppu.ctrl & 0x20),
            "PPUCTRL=$%02X。本作は 8x16 前提（体格型のタイル割当が変わる）" % nes.ppu.ctrl)

    # 起動直後の2フレームは PPU のウォームアップ（VBlank 2回待ち）に使うため DMA は走らない
    r.check("OAM DMA が毎フレーム行われている", nes.dma_count >= 8,
            "10フレームで DMA %d 回（起動の2フレームを除いて8回以上を期待）。"
            "NMI 中の $4014 書き込みが抜けている" % nes.dma_count)
    r.check("OAM DMA の転送元が OAM シャドウのページ", nes.oam_dma_page == 0x02,
            "$4014 に $%02X を書いている。OAM シャドウは $0200 に置く" % (nes.oam_dma_page or 0))

    r.check("描画中の PPUDATA 書き込みが無い", nes.ppu.writes_outside_vblank == 0,
            "%d 回。描画中の VRAM 書き込みは画面を壊す。転送は VBlank 中に限る"
            % nes.ppu.writes_outside_vblank)

    # パレット
    r.check("パレットが $3F00 に転送されている", nes.ppu.vram[0x3F00] == 0x0F,
            "$3F00=$%02X（期待 $0F）。load_palette が呼ばれていないか、"
            "アドレスラッチがずれている" % nes.ppu.vram[0x3F00])

    # P0 の目標: 黒画面に1スプライトが出る
    y, tile, attr, x = nes.ppu.oam[0:4]
    r.check("スプライト0 が画面内にいる (y=%d, x=%d)" % (y, x), 0 < y < 0xEF and 0 < x < 0xF8,
            "OAM[0..3] = %s。画面外に置かれている" % list(nes.ppu.oam[0:4]))
    r.check("スプライト0 以外は画面外に退避している", all(v == 0xFF for v in nes.ppu.oam[4:8]),
            "OAM[4..7] = %s。未使用スプライトは y=$FF で隠す" % list(nes.ppu.oam[4:8]))

    if labels and "frame_counter" in labels:
        fc = nes.ram[labels["frame_counter"] & 0x7FF]
        r.check("frame_counter が加算されている", fc >= 8,
                "frame_counter=%d（10フレーム走った後。起動の2フレームは PPU ウォームアップ）。"
                "NMI が来ていない可能性" % fc)

    # 入力 → OAM シャドウ → DMA → OAM の経路
    # OAM は DMA でシャドウを1フレーム遅れて写す。移動の検証はシャドウ側で行い、
    # DMA が効いていることは「シャドウと OAM が一致する」ことで確かめる。
    oam_shadow = labels.get("oam_shadow", 0x0200)

    def shadow(i):
        return nes.ram[(oam_shadow + i) & 0x7FF]

    base_x = shadow(3)
    nes.set_buttons({"RIGHT"})
    nes.run_frames(20)
    right_x = shadow(3)
    r.check("右キーでスプライトが右に動く", right_x > base_x,
            "OAM シャドウの x: %d -> %d。入力が読めていないか反映されていない" % (base_x, right_x))

    nes.set_buttons({"LEFT"})
    nes.run_frames(20)
    left_x = shadow(3)
    r.check("左キーでスプライトが左に動く", left_x < right_x,
            "OAM シャドウの x: %d -> %d" % (right_x, left_x))

    nes.set_buttons(set())
    nes.run_frames(20)
    r.check("入力なしでスプライトが止まる", shadow(3) == left_x,
            "OAM シャドウの x: %d -> %d。入力を離しても動いている" % (left_x, shadow(3)))

    r.check("OAM シャドウが DMA で OAM に反映されている",
            list(nes.ppu.oam[0:4]) == [shadow(0), shadow(1), shadow(2), shadow(3)],
            "OAM=%s, シャドウ=%s。$4014 の転送元ページが違う可能性"
            % (list(nes.ppu.oam[0:4]), [shadow(i) for i in range(4)]))

    # 経路ファイルの再生（P6 の代表20経路と同じ仕組みを P0 から通しておく）
    route_path = os.path.join(HERE, "routes", "p0_walk.txt")
    try:
        steps = parse_route(route_path)
        played = play(nes, steps)
        r.check("経路ファイルを再生できる (%s, %dフレーム)" % (os.path.basename(route_path), played),
                True)
    except (RouteError, CpuCrash) as e:
        r.check("経路ファイルを再生できる", False, str(e))

    # 長時間の安定性（スタック破壊や暴走の検出）
    try:
        nes.run_frames(120)
    except CpuCrash as e:
        r.check("2秒ぶん（120フレーム）走り続ける", False, str(e))
        return
    r.check("2秒ぶん（120フレーム）走り続ける", True)


# ---------------------------------------------------------------- L3
def layer3_mesen(rom_path, r):
    mesen = os.environ.get("MESEN")
    script = os.path.join(HERE, "lua", "p0_boot.lua")
    if not mesen:
        print("L3 自動プレイ（Mesen2）: **未実行** — 環境変数 MESEN が未設定")
        print("      P6（代表20経路の自動プレイ）に入る前に Mesen2 を用意すること（ADR-0004）")
        return False
    if not os.path.exists(mesen):
        r.check("MESEN が指す実行ファイルが存在する", False, "MESEN=%s が見つからない" % mesen)
        return False
    print("L3 自動プレイ（Mesen2）")
    cmd = [mesen, "--testrunner", rom_path, script]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    r.check("Mesen2 の自動プレイが成功する", proc.returncode == 0,
            "終了コード %d\n%s" % (proc.returncode, proc.stderr.strip()))
    return True


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("rom", nargs="?", default="build/roaring.nes")
    ap.add_argument("--labels", default="build/roaring.labels")
    args = ap.parse_args(argv)

    labels = {}
    if os.path.exists(args.labels):
        labels = load_labels(args.labels)

    r = Results()
    print("== 潮鳴り三代記 回帰テスト ==")
    print("ROM: %s" % args.rom)
    print()
    if layer1_structure(args.rom, labels, r) is not None:
        print()
        layer2_execution(args.rom, labels, r)
    print()
    ran_l3 = layer3_mesen(args.rom, r)

    print()
    print("結果: %d 件成功 / %d 件失敗   (L3 自動プレイ: %s)"
          % (r.passed, len(r.failed), "実行" if ran_l3 else "未実行"))
    if r.failed:
        print()
        print("失敗したテスト:")
        for name, detail in r.failed:
            print("  - %s: %s" % (name, detail))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
