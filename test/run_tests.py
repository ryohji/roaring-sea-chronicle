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

from cpu6502 import CpuCrash                  # noqa: E402
from nes import (Nes, Rom, load_labels, boot,  # noqa: E402
                 frame_instructions, sample_phases)
from routes import parse_route, play, RouteError   # noqa: E402
from l2_engine import layer2_engine, OAM_SPRITE_MAX  # noqa: E402
from l2_scroll import (layer2_scroll, check_nmi_write_budget,  # noqa: E402
                       DIAG_COUNTERS)
from l2_camera import layer2_camera            # noqa: E402
from l2_ai import layer2_ai                    # noqa: E402
from scene import disarm_enemies               # noqa: E402


ADR1 = "ADR-0001（案A: バッテリーバックアップ + シナリオ中途のオートセーブ）"

# ---------------------------------------------------------------- 観測の作法
# ハーネスの run_frames() が戻るのは「NMI から VBlank ぶんだけ進めた点」であって、
# フレームの切れ目ではない。その点はメインループが1フレームぶんの処理を走らせている
# **最中**である。実測（P1 後半時点）:
#     NMI+0     NMI ハンドラ（OAM DMA → VRAM 転送 → スクロール設定）
#     NMI+997   read_pad
#     NMI+1466  lane_update_all
#     NMI+2247  oam_build が操作キャラの OAM シャドウ X を書く   ← ここ
#     NMI+2280  run_frames が戻る
# つまり「入力を離しても動かない」の類を run_frames の直後に生で読むと、
# 余裕はわずか 33 サイクルしかなく、フレームの処理が数十サイクル増減しただけで
# 「1フレーム古い OAM」を読む側に倒れる（実際、engine-dev の作業中に倒れた）。
#
# 対策は「観測点がフレームのどこにあっても同じ値が読める状態にしてから読む」ことである。
# 入力を離してキャラが静止していれば、組み立て途中の OAM シャドウを読んでも値は変わらない。
# 位置の比較を行うテストは必ず settle() を通してから読むこと。
#
# **ただし settle() で救えるのは「落ち着けば動かなくなる量」だけである。**
# 作業変数（ent_lane_step / ent_lane_acc / ent_lane_dy、これから増える act_timer /
# act_hitstop / act_invuln のようなタイマー類）は、静止していても**更新の最中には
# 半端な値を通過する**。それらを run_frames の直後に生で読むと、メインループの
# 命令数が変わった日に別の隙間を覗いて落ちる。作業変数は harness の
# frame_end() / step_frame() で「そのフレームの更新が終わった点」まで進めてから読むこと。
# 作法の全体は test/harness/nes.py 冒頭の「観測の作法」にまとめてある。
SETTLE_FRAMES = 2

# sample_phases / PHASE_STEPS は harness/nes.py に移してある（l2_* からも使うため）。


def settle(nes, frames=SETTLE_FRAMES):
    """入力を離して数フレーム走らせ、観測点に依存しない（静止した）状態にする。"""
    nes.set_buttons(set())
    nes.run_frames(frames)


# ---------------------------------------------------------------- 起動の予算
# 起動処理（リセット → 最初の NMI）に許すフレーム数の上限。
# init.s は PPU のウォームアップで VBlank を2回待ち、そのあと MMC3 初期化・パレット転送・
# 背景1画面ぶんの初期転送・シーン構築・最初の oam_build を済ませてから NMI を許可する。
# この「2回目の VBlank 待ちのあと」の仕事がフレーム長を超えると、最初の NMI が
# 1フレームぶん後ろにずれる。実測ではリセットから 3 フレーム目に最初の NMI が来ており、
# 2回目の VBlank 待ちから先には約 8000 サイクル（0.27 フレーム）しか余裕が無い。
#
# 上限を 4 にしてあるのは、P3/P4 で初期転送が増えたときに 1 フレームぶんの伸びは
# 許し、それ以上伸びたら**この検証が名指しで落ちる**ようにするためである。
# 「DMA が毎フレーム走る」「frame_counter が増える」といった別の主張のテストが
# 起動の重さで落ちると、原因が分からなくなる。上限を上げるときは、
# 起動時間が延びてよいのかを先に考えること（最初の絵が出るまでの待ち時間である）。
BOOT_FRAMES_MAX = 4


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

    boot_limit = BOOT_FRAMES_MAX + 8        # 超えていても「何フレームかかったか」は報告したい
    try:
        nes = Nes(rom_path)
        nes.reset()
        boot_frames = boot(nes, limit=boot_limit)
        dma_at_boot = nes.dma_count
        fc_at_boot = nes.ram[labels["frame_counter"] & 0x7FF] if "frame_counter" in labels else 0
        nes.run_frames(10)
        # フレーム数の勘定はここで締める。以降の検証もフレームを進めるので、
        # あとで読むと「10フレームぶん」ではなくなる。
        fc_after = nes.ram[labels["frame_counter"] & 0x7FF] if "frame_counter" in labels else 0
    except CpuCrash as e:
        r.check("リセットから10フレーム走る", False, str(e))
        return
    r.section("起動・描画・入力")
    r.check("リセットから10フレーム、クラッシュせずに走る", True)

    # 起動の長さは「起動が重すぎないか」という独立した主張なので、専用の検証にする。
    # これを DMA やフレームカウンタの検証に混ぜると、起動が延びただけなのに
    # 「DMA が毎フレーム走っていない」という誤った診断が出る（BOOT_FRAMES_MAX の注記を見よ）。
    r.check("起動処理が %d フレーム以内に終わる（最初の NMI が来る）" % BOOT_FRAMES_MAX,
            boot_frames is not None and boot_frames <= BOOT_FRAMES_MAX,
            "%s。init.s の2回目の VBlank 待ちより後ろ（MMC3 初期化・パレット転送・"
            "背景1画面ぶんの初期転送・シーン構築・最初の oam_build）が1フレームに収まっていない。"
            "起動が延びると最初の絵が出るまでの待ちが伸び、さらに「DMA が毎フレーム走る」"
            "「frame_counter が増える」がこの重さのせいで落ちて原因が読めなくなる。"
            "伸ばしてよいと判断したなら test/run_tests.py の BOOT_FRAMES_MAX を理由つきで上げること"
            % ("最初の OAM DMA が %d フレーム目に来た" % boot_frames if boot_frames is not None
               else "%d フレーム走っても OAM DMA が一度も無い（NMI が来ていない）" % boot_limit))

    r.check("NMI が有効になっている", nes.ppu.nmi_enabled,
            "PPUCTRL=$%02X。bit7 が立っていない＝NMI が来ないので、"
            "メインループが wait_nmi で止まる" % nes.ppu.ctrl)
    r.check("スプライト表示が有効", bool(nes.ppu.mask & 0x10),
            "PPUMASK=$%02X。bit4 が立っていないとスプライトが出ない" % nes.ppu.mask)
    r.check("8x16 スプライトモード", bool(nes.ppu.ctrl & 0x20),
            "PPUCTRL=$%02X。本作は 8x16 前提（体格型のタイル割当が変わる）" % nes.ppu.ctrl)

    # 起動が終わった**あと**の10フレームで数える。起動にかかるフレーム数は上で別に見ており、
    # ここは「走り出したら毎フレーム DMA する」という主張だけを見る。
    r.check("OAM DMA が毎フレーム行われている", nes.dma_count - dma_at_boot == 10,
            "起動後の10フレームで DMA %d 回（期待はちょうど10回 = 毎フレーム1回）。"
            "NMI 中の $4014 書き込みが抜けているか、oam_ready が下りたまま NMI を迎えて "
            "DMA を飛ばしたフレームがある（飛ばしたフレームは1フレーム前の絵が出たままになる）"
            % (nes.dma_count - dma_at_boot))
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
    # OAM（DMA された完成品）と oam_used（組み立て中の作業変数）を突き合わせるので、
    # 静止した状態で読む。動いている最中だと、この2つが別々のフレームのものになる。
    settle(nes)

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

        # oam_used は「組み立て中の数」——作業変数である（harness/nes.py 冒頭の作法 (b)）。
        # 上の2件は run_frames の直後、つまりメインループが oam_build を走らせている
        # **最中**に読んでいる。それが許されるのは、oam_build が oam_used を最後に1回だけ
        # 書く（＝静止したシーンでは毎フレーム同じ値になる）からであって、
        # 「作業変数を run_frames の直後に読んでよい」からではない。
        # その前提そのものをここで縛る。oam_used を数えながら増やす実装に変えたら、
        # 上の2件が観測点しだいで落ちるより先に、この名前で落ちてほしい。
        used_seen = set()
        boundaries = 0
        for _ in frame_instructions(nes, labels):
            boundaries += 1
            used_seen.add(nes.ram[oam_used_addr & 0x7FF])
        r.check("静止中の oam_used はフレーム更新中のどの命令の切れ目でも同じ値"
                "（%d 箇所を全数検査）" % boundaries, used_seen == {used},
                "1フレームの更新中に oam_used が %s と揺れた（更新の終わりでは %d）。"
                "組み立ての途中経過が oam_used に見えている＝上の「未使用の OAM エントリ」の"
                "検証は、フレームのどのサイクルで読んだかで結論が変わる。"
                "途中経過は oam_next に置き、oam_used は完成時に1回だけ書くこと"
                % (sorted(used_seen), used))

    if labels and "frame_counter" in labels:
        delta = (fc_after - fc_at_boot) & 0xFF
        r.check("frame_counter が毎フレーム加算されている", delta == 10,
                "起動後の10フレームで frame_counter が %d しか進んでいない（期待 10）。"
                "NMI が来ていないフレームがある。※ 起動にかかったフレーム数は別の検証で見ている"
                % delta)

    # 入力 → OAM シャドウ → DMA → OAM の経路
    # OAM は DMA でシャドウを1フレーム遅れて写す。移動の検証はシャドウ側で行い、
    # DMA が効いていることは「シャドウと OAM が一致する」ことで確かめる。
    oam_shadow = labels.get("oam_shadow", 0x0200)

    def shadow(i):
        return nes.ram[(oam_shadow + i) & 0x7FF]

    # ここから下の3件が見たいのは「入力 → OAM シャドウ → DMA → OAM」の経路であって、
    # 戦闘下の挙動ではない。敵が殴るようになって以降、ノックバックで操作キャラが
    # **入力が無くても動く**ため「入力なしで止まる」が落ちる。これは engine の不具合ではない。
    # 期待値を「数ドットまでの移動は許す」に緩めると、入力が無いのに動く不具合を
    # 二度と捕まえられなくなる。邪魔している側（敵の攻撃）を取り除いてから見る。
    # ノックバックで動くこと自体は仕様であり、l2_ai.py 側で別の主張として見ている。
    restore_enemies = disarm_enemies(nes, labels)

    # 位置の読み取りは必ず「入力を離して静止させてから」行う（冒頭の SETTLE_FRAMES の注記）。
    settle(nes)
    base_x = shadow(3)

    # 以下の比較が成り立つ前提そのものを、ここで1回だけ確かめておく。
    # 静止しているのにフレーム内の観測位置で値が変わるなら、
    # 下の「右に動く／左に動く／止まる」は**どれも**フレームの処理時間しだいで
    # 勝手に落ちるテストになる。前提が崩れたときは、その前提の名前で落としたい。
    phases = sample_phases(nes, lambda: shadow(3))
    r.check("静止中の OAM シャドウはフレーム内のどこで読んでも同じ値になる",
            len(set(phases)) == 1,
            "同じフレームの中で位相をずらして読むと x が %s と揺れた。"
            "run_frames が戻る位置はメインループが oam_build を走らせている最中なので、"
            "揺れる状態で読むと「1フレーム古い OAM」を読むことがあり、"
            "位置を比べるテストの合否がフレームの処理時間に左右される。"
            "入力を離して静止させてから読むこと" % (phases,))

    nes.set_buttons({"RIGHT"})
    nes.run_frames(20)
    settle(nes)                      # 離してから読む（押しっぱなしのまま読むと観測点に依存する）
    right_x = shadow(3)
    r.check("右キーでスプライトが右に動く", right_x > base_x,
            "OAM シャドウの x: %d -> %d。入力が読めていないか反映されていない" % (base_x, right_x))

    nes.set_buttons({"LEFT"})
    nes.run_frames(20)
    settle(nes)
    left_x = shadow(3)
    r.check("左キーでスプライトが左に動く", left_x < right_x,
            "OAM シャドウの x: %d -> %d" % (right_x, left_x))

    # ここは「離した後さらに走らせても1ドットも動かない」ことを見る。
    # left_x は既に静止状態で読んであるので、両辺が同じ土俵にある。
    nes.run_frames(20)
    r.check("入力なしでスプライトが止まる", shadow(3) == left_x,
            "OAM シャドウの x: %d -> %d。入力を離しても動いている" % (left_x, shadow(3)))

    # OAM は DMA で1フレーム遅れて写るので、この比較が意味を持つのは静止しているときだけ。
    # 直前の20フレームは入力なしなので、シャドウも OAM も同じ絵を指している。
    r.check("OAM シャドウが DMA で OAM に反映されている",
            list(nes.ppu.oam[0:4]) == [shadow(0), shadow(1), shadow(2), shadow(3)],
            "OAM=%s, シャドウ=%s。$4014 の転送元ページが違う可能性"
            % (list(nes.ppu.oam[0:4]), [shadow(i) for i in range(4)]))

    # 位置を比べる検証はここまで。以降（経路の再生・120フレームの安定性・転送キューの
    # 診断カウンタ）は**敵が殴ってくる状態**で見たいので、敵を戻す。
    restore_enemies()

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

    # 転送キューの診断カウンタは「何度積み直しても通らない」不具合の数である
    # （空き不足の vq_overflow とは別物で、場面が変わっても消えない）。
    # ここは**普通に走らせたメインループ**で見ている。位相をずらして混んだ NMI を
    # 探す必要は無く、「0 のままか」だけで積む側の数え違いが分かる。
    present = [n for n in DIAG_COUNTERS if n in labels]
    seen = [(n, nes.ram[labels[n] & 0x7FF]) for n in present]
    nonzero = ["%s=%d" % (n, v) for n, v in seen if v]
    r.check("130 フレーム走っても転送キューの診断カウンタが 0 のまま (%s)"
            % "、".join(present), bool(present) and not nonzero,
            "%s。vq_badclose は「open に申告した長さと実際に書いたバイト数が違った」、"
            "vq_badlen は「長さの申告が範囲外」、vq_badstep は「STEP 記録がページ境界を"
            "またぐ」回数で、どれも**何度積み直しても通らない**記録の数である。"
            "0 でなければ背景の列が転送されないまま残る"
            % ("、".join(nonzero) if nonzero
               else "カウンタのラベルが build/roaring.labels に無い（vram.s の .export が消えた）"))

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
        scroll_state = layer2_scroll(args.rom, labels, r) or {}
        print()
        layer2_camera(args.rom, labels, r)
        print()
        layer2_ai(args.rom, labels, r)
        print()
        # ここまでの検証が回した**全ての NMI** をまとめて見る。転送量の上限は
        # 抜き取り（混んでいるフレームを20フレーム）では位相しだいで素通りするので、
        # 数えるのは harness に常時やらせ、予算との比較をここで1回行う。
        check_nmi_write_budget(scroll_state.get("budget"), r)
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
