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
sys.path.insert(0, HERE)

from cpu6502 import CpuCrash            # noqa: E402
from nes import Nes, Rom, load_labels   # noqa: E402
from routes import parse_route, play, RouteError   # noqa: E402
from l2_engine import layer2_engine, OAM_SPRITE_MAX  # noqa: E402


ADR1 = "ADR-0001（案A: バッテリーバックアップ + シナリオ中途のオートセーブ）"


class Results:
    def __init__(self):
        self.passed = 0
        self.failed = []

    def section(self, title):
        """テストが増えても読めるように、層の中を小見出しで区切る。"""
        print("  -- %s" % title)

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

    r.section("ROM 構成とベクタ")
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

    layer1_save_header(rom, r)
    return rom


def layer1_save_header(rom, r):
    """セーブ方式（ADR-0001 案A）が ROM ヘッダから消えていないことの検証。

    ここで検証する3点が崩れても ROM は「NES の ROM として valid」なままなので、
    実行検証では捕まらない。構造検証でしか気付けない。
    """
    r.section("セーブ領域 / %s" % ADR1)

    flags6, flags7, flags8 = rom.header[6], rom.header[7], rom.header[8]

    r.check("flags6 bit1 = バッテリーバックアップ有り", rom.has_battery,
            "flags6=$%02X（bit1=0）。電池なしの ROM と宣言している。"
            "この宣言が無いとエミュレータは .sav を作らず、実機カートでも $6000-$7FFF は "
            "電源断で揮発する。つまり**オートセーブしたはずのデータが電源を切るたびに全部消える**。"
            "%s により flags6 は $43（bit1=1）。src/header.s を見よ" % (flags6, ADR1))

    ines2 = (flags7 & 0x0C) == 0x08
    r.check("flags7 が iNES 1.0 を示す (bit3-2 = 00)", not ines2,
            "flags7=$%02X（bit3-2=%%10 = NES 2.0）。NES 2.0 ではバイト8は PRG-RAM サイズではなく "
            "マッパー番号の上位/サブマッパーとして読まれるため、flags8=$01 が "
            "「PRG-RAM 8KB」の意味を失い（マッパー番号も 4 から化ける）、"
            "**セーブ領域が確保されずセーブが消える**。"
            "NES 2.0 に移行するなら flags10 の PRG-RAM シフト値を別途宣言すること" % flags7)

    r.check("flags8 が PRG-RAM 8KB を宣言している (=1)", flags8 == 1,
            "flags8=$%02X（期待 $01 = 8KB）。0 でも 8KB 扱いする実装が多いが、"
            "「PRG-RAM 無し」と解釈するエミュレータ／フラッシュカートでは $6000-$7FFF が "
            "存在しないことになり、**セーブしたはずのデータが次回起動時に丸ごと無い**。"
            "電池付き ROM では容量を明示する。src/header.s のバイト8 を $01 に" % flags8)

    r.check("トレーナ無し (flags6 bit2 = 0)", not rom.has_trainer,
            "flags6=$%02X（bit2=1）。トレーナ 512 バイトが挟まると PRG の読み出し位置が "
            "ずれ、バッテリー領域を含むヘッダ解釈が全部狂う" % flags6)


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
    r.section("起動・描画・入力")
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

    # 「使っていない OAM エントリは画面外に隠れている」ことの検証。
    # P0 では画面上のスプライトが1個だったので OAM[4..] 全部が $FF だったが、
    # P1 からは複数のエンティティが並ぶので「1個だけ」は定義上成立しない。
    # 境界は oam_used（sprite.s が毎フレーム書く「使った OAM エントリ数」）が持つ。
    # ここが崩れると、前フレームに出ていたスプライトが消えずに残る（幽霊スプライト）。
    # 起動処理は OAM シャドウを1回 $FF で埋める。それだけでも「未使用は $FF」は
    # 見かけ上成立してしまうので、わざと汚してから走らせ、
    # **毎フレームの OAM 構築が** 未使用エントリを隠し直していることを見る。
    shadow_base = labels.get("oam_shadow", 0x0200)
    for i in range(OAM_SPRITE_MAX):
        nes.ram[(shadow_base + i * 4) & 0x7FF] = 0x50
    nes.run_frames(2)

    oam_used_addr = labels.get("oam_used")
    if oam_used_addr is None:
        r.check("未使用の OAM エントリが画面外に隠れている", False,
                "ラベル oam_used が build/roaring.labels に無い。"
                "sprite.s が使用済みエントリ数を公開しなくなったか、.export が消えている")
    else:
        used = nes.ram[oam_used_addr & 0x7FF]
        stale = [i for i in range(used, OAM_SPRITE_MAX) if nes.ppu.oam[i * 4] != 0xFF]
        r.check("未使用の OAM エントリ (#%d 以降) が画面外に隠れている" % used, not stale,
                "OAM #%s の Y が $FF でない（先頭は #%d: Y=%d）。使用済み %d 個の後ろは "
                "$FF で隠すこと。隠し忘れると前フレームのスプライトが残って画面に出る"
                % (stale[:8], stale[0] if stale else -1,
                   nes.ppu.oam[stale[0] * 4] if stale else -1, used))
        # 上の検証が「使用 0 個」「使用 64 個」で素通りしないことを確かめる。
        # oam_used が壊れて 64 になると、上の検証は何も見ずに成功してしまう。
        r.check("oam_used が妥当な範囲にある (0 < %d < %d)" % (used, OAM_SPRITE_MAX),
                0 < used < OAM_SPRITE_MAX,
                "oam_used=%d。この画面には操作キャラ1体と敵3体が居るので 0 でも 64 でもないはず。"
                "0 なら何も描いていない、64 なら上の「未使用エントリ」の検証が空振りになる"
                % used)

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
    crash = None
    try:
        nes.run_frames(120)
    except CpuCrash as e:
        crash = e
    r.check("2秒ぶん（120フレーム）走り続ける", crash is None, str(crash))
    if crash is not None:
        return

    # セーブ領域は 120 フレーム走り切った後の状態で見る。
    # 「起動時に有効化したが、走っているうちに誰かが $A001 を潰した」も捕まえたいため。
    layer2_save_ram(nes, r)


def layer2_save_ram(nes, r):
    """セーブ領域 ($6000-$7FFF) が使える状態になっていることの実行検証。

    ROM ヘッダ（L1）が電池を宣言していても、MMC3 の $A001 で有効化しなければ
    $6000-$7FFF は開放バスのままで、書いたセーブは1バイトも残らない。
    """
    r.section("セーブ領域 / %s" % ADR1)
    m = nes.mapper

    if not r.check("mmc3_init が $A001 (PRG-RAM 制御) に書き込んでいる", m.prg_ram_reg is not None,
                   "リセットから130フレーム走って $A001 に一度も書いていない。"
                   "MMC3 の PRG-RAM は電源投入時は無効なので、$6000-$7FFF は開放バスのまま＝"
                   "**オートセーブが1バイトも保存されない**。"
                   "src/engine/bank.s の mmc3_init にある "
                   "`lda #MMC3_PRGRAM_RW / sta MMC3_PRG_RAM` が消えている"):
        return

    r.check("$A001 の最終値が $80（PRG-RAM 有効・書込可）", m.prg_ram_reg == 0x80,
            "$A001=$%02X（bit7 有効=%d / bit6 書込禁止=%d、$A001 への書き込みは %d 回）。"
            "bit7=0 ならセーブ領域が見えず、bit6=1 なら読めても書けない。"
            "どちらも**オートセーブが保存されない**。期待は MMC3_PRGRAM_RW = $80"
            % (m.prg_ram_reg, 1 if m.prg_ram_enabled else 0,
               1 if m.prg_ram_write_protected else 0, m.prg_ram_reg_writes))

    # CPU バス経由の往復。ハーネスの配列を直接叩かないこと（バス配線の誤りを捕まえる）。
    probes = [(0x6000, 0x5A), (0x6123, 0xC3), (0x7000, 0x01), (0x7FFF, 0xA5)]
    saved_ram = bytes(nes.ram)
    originals = [(a, nes.read(a)) for a, _ in probes]
    for addr, value in probes:
        nes.write(addr, value)
    bad = ["$%04X: 書いた $%02X / 読めた $%02X" % (a, v, nes.read(a))
           for a, v in probes if nes.read(a) != v]
    r.check("$6000-$7FFF が CPU バス経由で読み書きできる", not bad,
            "%s。セーブ領域がバスに繋がっていない（$A001 の有効化漏れ、"
            "cfg/mmc3.cfg の PRGRAM 領域の消失、ハーネスのバス配線の誤りのいずれか）。"
            "**セーブを書いても読み戻せない**。捨てられた書き込み %d 回 / "
            "無効のまま読んだ回数 %d 回"
            % ("、".join(bad), m.prg_ram_writes_denied, m.prg_ram_reads_disabled))

    r.check("$6000-$7FFF が CPU RAM ($0000-$07FF) にエイリアスしていない",
            bytes(nes.ram) == saved_ram,
            "セーブ領域に書いたら内部 RAM も変わった。アドレスデコードが誤っており、"
            "セーブのたびにゲーム状態が壊れる（逆に RAM の更新でセーブが壊れる）")

    for addr, value in originals:                 # 後続の検証のため元に戻す
        nes.write(addr, value)

    # 電池バックアップの意味づけ: 電源を入れ直しても $6000-$7FFF の内容は残る。
    # 起動処理が RAM クリアのついでにここまで消すと、セーブが起動のたびに失われる。
    residual = bytes((i * 7 + 0x5A) & 0xFF for i in range(0x2000))
    nes.mapper.prg_ram[:] = residual              # 前回の電源断時点の内容を模す
    nes.power_cycle()                             # 電池で保持されるのは PRG-RAM だけ
    try:
        nes.run_frames(10)
    except CpuCrash as e:
        r.check("電池に残ったデータがあっても起動する", False,
                "$6000-$7FFF に前回のセーブが残った状態で起動したらクラッシュした: %s。"
                "起動処理が PRG-RAM の内容を「未初期化（ゼロ）」と決めつけている疑い。"
                "電池付きなので電源投入時の内容は前回のまま、電池切れなら不定値である" % e)
        return
    r.check("電池に残ったデータがあっても起動する", True)

    after = nes.snapshot_prg_ram()
    if after == residual:
        diff = ""
    else:
        first = next(i for i in range(len(residual)) if after[i] != residual[i])
        count = sum(1 for i in range(len(residual)) if after[i] != residual[i])
        diff = ("$6000-$7FFF の %d バイトが起動処理で書き換わった（最初の相違 $%04X: "
                "$%02X -> $%02X）。起動時の RAM クリアがセーブ領域まで巻き込んでいる＝"
                "**電源を入れるたびにプレイヤーのセーブが消える**。"
                "セーブ領域の読み書きは P4 の campaign-dev の担当で、"
                "engine の起動処理は $A001 の有効化までしか触らない（%s）"
                % (count, 0x6000 + first, residual[first], after[first], ADR1))
    r.check("起動処理がセーブ領域を消去していない", after == residual, diff)


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
        layer2_engine(args.rom, labels, r)
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
