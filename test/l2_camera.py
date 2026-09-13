"""L2 実行検証 — engine が塞いだ3件（カメラ公開の不可分性 / 背景の張り直し / STEP の番人）。

run_tests.py から呼ばれる。層の分け方は ADR-0004 に従う。

このファイルが見ているのは、l2_scroll.py が見ている「値が正しいか」とは別の主張である。

  (a) cam_x の torn read
      NMI がどのサイクルに来るかは、この Python エミュレータでは再現しない（できない）。
      だが 6502 の命令は割り込みに中断されないので、**NMI が入りうるのは命令の切れ目だけ**
      である。つまり cam_publish を1命令ずつ走らせて切れ目を全部踏めば、
      「NMI がどこに来ても中途半端な組は読めない」は**全数検査できる**。
      タイミングの再現ではなく、切れ目の全数検査に置き換えるのがこの節の要点である。
      あわせて「NMI が cam_x を見ていない」ことを、公開コピーと cam_x を**わざと
      食い違わせて**確かめる。nmi.s が cam_x を直接読む形に戻ったら、これだけが落ちる。

  (b) scroll_warp_to（任意の cam_x から背景を張り直す入口）
      VRAM をわざと汚してから飛び込み、可視 33 列のタイルと属性を生成器（bg.s）の
      出力と突き合わせる。**列追跡の初期化漏れ**は VRAM の中身では捕まらないので、
      「飛び込んだ直後に cam_update を1回呼んで、列が1本も積まれないこと」で見る。

  (c) VQ_STEP のページ境界の番人
      受理/拒否の表を焼き付けるのではなく、**境界そのもの**を性質として書く。
      受理されるのは「下位アドレスが最後まで桁上がりしないとき、かつそのときに限る」。
      増分も長さも、engine が実際に積む記録（属性列）から実測して導く。

方針は l2_scroll.py と同じ:
  * 期待値はテストに書き写さず ROM の生成器に作らせる（BgOracle を共有する）。
  * 調整のノブ（ステージ長・属性列の増分・フラグのビット割当）は実測で導く。

ここでは**見られない**もの（L3 = Mesen2 と実機に回す。ADR-0004）:
  * NMI が実際にどのサイクルに来るか。上の (a) は「切れ目のどこに来ても安全」を
    示しているので競合そのものは塞げているが、VBlank をはみ出したかどうかは別の話である。
  * scroll_warp_to のあいだ画面が黒いこと、および戻したあと最初の1フレームが
    流れて見えないこと（$2006 を叩いたあとの PPUCTRL/PPUSCROLL の置き直し）。
    この Python PPU は描画しないので、絵としては何も確かめていない。
  * 張り直しに 0.6〜2.6 フレームかかること（暗転の裏に隠せる長さかどうか）。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "harness"))
sys.path.insert(0, HERE)

from cpu6502 import CpuCrash                       # noqa: E402
from nes import Nes, boot                          # noqa: E402

# 足場と期待値の出どころは l2_scroll と共有する。写すと規則が2か所に分かれ、
# 「両方に同じ間違いを書いた」という失敗の仕方をする（l2_scroll.BgOracle の注記）。
from l2_scroll import (                            # noqa: E402
    CTRL_NT_X, NT_BASE, NT_WRAP_COLS, PPUADDR, PPUCTRL, PPUDATA, PPUSCROLL,
    TILE_W_SHIFT, VISIBLE_COLS, VQ_SCRATCH,
    BgOracle, attach_queue_watch, report_queue_invariants,
    _attr_addr, _attr_problem, _cam_col, _cam_limit, _cam_x,
    _column_problem, _diag, _diag_delta, _flush, _get, _park, _pending,
    _place_player, _queue_record, _record_header, _set, _set16, _watch,
)

NEEDED = (
    "cam_publish", "cam_update", "scroll_warp_to",
    "cam_x_lo", "cam_x_hi", "cam_col_lo", "cam_col_hi",
    "cam_pub_lo", "cam_pub_hi", "cam_pub_sel",
    "cam_limit_lo", "cam_limit_hi",
    "bg_tile_at", "bg_attr_byte", "bg_col_lo", "bg_col_hi",
    "bg_queue_column", "bg_queue_attr",
    "vram_queue_reset", "vram_queue_open", "vram_queue_byte", "vram_queue_close",
    "vram_queue_pending", "vq_dst_lo", "vq_dst_hi", "vq_buf",
    "vq_head", "vq_tail", "vq_wr", "vq_overflow",
    "vq_badstep", "vq_badlen", "vq_badclose",
    "ent_x_lo", "ent_x_hi",
)

# 公開コピーは「面0/面1 の 2 バイト配列」である。添字が 0/1 以外になると
# 隣のゼロページ変数を読み書きすることになるので、そこも見る。
CAM_PUB_FACES = 2

# 不感帯の中央（操作キャラの画面X）。追従はここでは検証しないので、
# 「カメラが動かない点」を1つ取れれば足りる。l2_scroll の不感帯の実測とは独立に、
# 画面中央そのものを使う（CAM_CENTER_X = 画面幅/2）。
SCREEN_W = 256
DEADZONE_CENTER = SCREEN_W // 2


# ---------------------------------------------------------------- 足場
def _published(nes, labels):
    """NMI が読むのと同じ手順で公開コピーを読む（添字を1回だけ引いて lo/hi を揃える）。

    戻り: (値 or None, 添字)。添字が 0/1 の外なら値は None。
    """
    sel = _get(nes, labels, "cam_pub_sel")
    if sel >= CAM_PUB_FACES:
        return None, sel
    lo = nes.ram[(labels["cam_pub_lo"] + sel) & 0x7FF]
    hi = nes.ram[(labels["cam_pub_hi"] + sel) & 0x7FF]
    return lo | (hi << 8), sel


def _set_cam_x(nes, labels, value):
    _set16(nes, labels, "cam_x_lo", "cam_x_hi", value)


def _publish(nes, labels, cam_x):
    """cam_x を置いて cam_publish を最後まで呼ぶ（公開コピーを既知の値にする）。"""
    _set_cam_x(nes, labels, cam_x)
    nes.call(labels["cam_publish"])


def _visible_camera(cam_x):
    """カメラX のうち、PPU のスクロール設定として**外から見える**ぶんだけを残す。

    NMI が PPU に渡すのは「横スクロール = 下位8bit」と「NT 選択 = bit8」の 9bit だけである。
    背景は 64 列 = 512 ドットで巻き取るので、それ以上の桁は画面に出ない（出す先が無い）。
    したがって NMI の書き込みから復元できるのもこの 9bit までで、
    比較はこの射影どうしで行う（$0200 と $0000 は PPU から見て同じ設定である）。
    """
    return (cam_x & 0xFF) | ((cam_x >> 8) & CTRL_NT_X) << 8


def _nmi_camera(nes, labels):
    """NMI を単体で1回走らせ、NMI がスクロール設定に使ったカメラ位置を復元する。

    NMI が書くのは「横スクロール = カメラX の下位」「PPUCTRL の NT ビット = カメラX の bit8」
    なので、この2つから 9bit ぶんのカメラ位置が読み取れる（_visible_camera の射影）。
    公開コピーと cam_x を 256 ドット境界をまたいで食い違わせておけば、
    どちらを読んだかがこの値で分かる。
    戻り: (復元したカメラ位置 or None, 説明)
    """
    writes = nes.run_nmi_now()
    scroll = [v for a, v in writes if a == PPUSCROLL]
    ctrl = [v for a, v in writes if a == PPUCTRL]
    if len(scroll) < 2 or not ctrl:
        return None, ("NMI が $2005 を %d 回 / $2000 を %d 回しか書かなかった"
                      % (len(scroll), len(ctrl)))
    if scroll[1] != 0:
        return None, "縦スクロール ($2005 の2回目) が %d（横スクロール専用なので 0）" % scroll[1]
    return scroll[0] | ((ctrl[-1] & CTRL_NT_X) << 8), ""


# ---------------------------------------------------------------- 入口
def layer2_camera(rom_path, labels, r):
    print("L2 実行検証 / engine P1 修正分（カメラ公開の不可分性・背景の張り直し・STEP の番人）")

    missing = [n for n in NEEDED if n not in labels]
    if missing:
        r.check("カメラ公開・張り直し・転送キューのラベルが揃っている", False,
                "ラベルが無い: %s。モジュールがリンクから外れたか、.export が消えている。"
                "このセクションの検証はラベル無しでは書けないので全部飛ばした" % ", ".join(missing))
        return

    try:
        nes = Nes(rom_path)
        nes.reset()
        if boot(nes) is None:
            r.check("カメラ公開の検証用に起動する", False,
                    "起動しても NMI が来ない（OAM DMA が1回も無い）。"
                    "起動処理の検証（起動処理が N フレーム以内に終わる）を先に見よ")
            return
    except CpuCrash as e:
        r.check("カメラ公開の検証用に起動する", False, str(e))
        return

    # キューに触る前にメインループを待ちループへ寄せる（実機に無い再入を作らないため）。
    # このファイルは以降フレームを回さない（nes.call / call_stepwise / run_nmi_now だけ）ので、
    # メインループは待ちループに置かれたままになる。
    _park(nes, labels)

    oracle = BgOracle(nes, labels)
    state = {}
    if attach_queue_watch(nes, labels, state) is None:
        r.check("記録のヘッダ長を実測できる", False,
                "空のキューに長さ4の記録すら open できない。キューの不変条件を"
                "辿る足場が作れないので、このセクションは飛ばした")
        return

    # 診断カウンタはセクションの前後で増えないこと。STEP の番人だけは
    # わざと突き返させる（その節が自分で vq_badstep の増え方を見ている）ので除く。
    counted_elsewhere = {"STEP のページ境界"}
    dirty = []
    for name, fn in (("カメラ公開の不可分性", _check_publish_atomic),
                     ("NMI が読む出どころ", _check_nmi_reads_published),
                     ("背景の張り直し", _check_warp),
                     ("STEP のページ境界", _check_step_guard)):
        before = _diag(nes, labels)
        try:
            fn(nes, labels, oracle, state, r)
        except CpuCrash as e:
            r.check("%s の検証中にクラッシュしない" % name, False, str(e))
        except Exception as e:      # noqa: BLE001 — 検証が例外で死ぬと原因が読めなくなる
            r.check("%s の検証が最後まで走る" % name, False,
                    "検証コードが %s で止まった: %s。engine の値が想定の範囲を外れている"
                    % (type(e).__name__, e))
        delta = _diag_delta(before, _diag(nes, labels))
        if delta and name not in counted_elsewhere:
            dirty.append("%s: %s" % (name, "／".join(delta)))

    report_queue_invariants(nes, labels, r, dirty, "カメラ・張り直し")


# ---------------------------------------------------------------- (a) 不可分性
# 公開コピーの検証に使うカメラ位置。256 ドット境界をまたぐ組（NT ビットが変わる組）を
# 必ず含めること。torn read が画面に出るのはその瞬間だけである。
PUB_VALUES = (0x0000, 0x0001, 0x00FF, 0x0100, 0x0101, 0x01FF, 0x0200, 0x02FF, 0x0300)

# NMI を実際に割り込ませて見る組。lo と hi が両方変わる（＝混ざれば必ず別の値になる）ものを選ぶ。
NMI_PAIRS = ((0x00FF, 0x0100), (0x01FF, 0x0200), (0x0100, 0x00FF))


def _check_publish_atomic(nes, labels, oracle, state, r):
    """cam_publish の**どの命令の切れ目**で覗いても、公開コピーが旧値か新値しかないこと。

    6502 の命令は割り込みに中断されない。NMI が入りうるのは命令の切れ目だけなので、
    切れ目を全部踏んで観測すれば「NMI がどこに来ても混ざった組は読めない」を全数検査できる。
    ここが成り立てば、cam_x の torn read は原理的に起きない。
    """
    r.section("カメラ公開コピーの不可分性（命令の切れ目の全数検査）")

    # --- 添字が 0/1 と交互になること（＝毎回 NMI が読んでいない面に書いている）---
    seen = []
    for i in range(6):
        _publish(nes, labels, 0x0040 + i)
        seen.append(_get(nes, labels, "cam_pub_sel"))
    alternating = all(seen[i] != seen[i + 1] for i in range(len(seen) - 1))
    r.check("cam_publish のたびに公開面の添字が 0/1 と交互になる",
            alternating and set(seen) == {0, 1},
            "cam_publish を6回呼んだときの cam_pub_sel = %s（期待は 0/1 の交互）。"
            "交互でなければ、書いている面と NMI が読んでいる面が同じになる瞬間があり、"
            "二面バッファの意味が消える（書きかけの組を NMI に見せる）" % seen)

    # --- 各命令の切れ目での観測 ---
    torn = []
    not_switched = []
    regressed = []
    boundaries = 0
    for old in PUB_VALUES:
        for new in PUB_VALUES:
            _publish(nes, labels, old)
            before, sel = _published(nes, labels)
            if before != old:
                torn.append("cam_publish を最後まで呼んでも公開コピーが $%04X（cam_x=$%04X、"
                            "面=%s）" % (before if before is not None else -1, old, sel))
                continue
            _set_cam_x(nes, labels, new)        # ここから公開までが「窓」になる
            observed = []
            for n in nes.call_stepwise(labels["cam_publish"]):
                value, sel = _published(nes, labels)
                observed.append(value)
                boundaries += 1
                if value not in (old, new):
                    torn.append("旧 $%04X → 新 $%04X: %d 命令目の切れ目で公開コピーが "
                                "$%s（面=%d）。lo と hi が別の組から1バイトずつ拾えている"
                                % (old, new, n,
                                   "%04X" % value if value is not None else "??",
                                   sel))
            if observed and observed[-1] != new:
                not_switched.append("旧 $%04X → 新 $%04X: 呼び終わっても公開コピーが $%04X"
                                    % (old, new, observed[-1] if observed[-1] is not None else -1))
            # 切り替えは1命令で起きるので、いちど新値が見えたら旧値に戻ることはない。
            if old != new and new in observed:
                after = observed[observed.index(new):]
                if any(v != new for v in after):
                    regressed.append("旧 $%04X → 新 $%04X: 観測列 %s"
                                     % (old, new, ["$%04X" % v if v is not None else "??"
                                                   for v in observed]))

    r.check("cam_publish の途中のどの命令の切れ目でも、公開コピーは旧値か新値しか見えない"
            "（%d 組 / %d 箇所の切れ目を全数検査）" % (len(PUB_VALUES) ** 2, boundaries),
            not torn,
            "%s（ほか %d 箇所）。6502 の命令は NMI に中断されないので、NMI が入りうるのは"
            "命令の切れ目だけである。そこで混ざった組が見えるということは、"
            "**実機では NMI がその隙に入れる**ということであり、256 ドット境界をまたぐ瞬間に"
            "画面が1画面ぶん飛ぶ。lo/hi を同じ面に上書きする publish に戻っていないか、"
            "面の切り替え (stx cam_pub_sel) が lo/hi の書き込みより前に出ていないかを見よ"
            % (torn[0] if torn else "", max(0, len(torn) - 1)))

    r.check("cam_publish を呼び終えたら新しいカメラ位置が公開されている", not not_switched,
            "%s（ほか %d 組）。面に書いただけで添字を切り替えていない＝"
            "NMI は永久に古い面を読み続ける（カメラが動いても画面が動かない）"
            % (not_switched[0] if not_switched else "", max(0, len(not_switched) - 1)))

    r.check("公開の切り替えが1回だけ起きる（新値が見えたあと旧値に戻らない）", not regressed,
            "%s（ほか %d 組）。切り替えが複数の命令に分かれている＝"
            "その隙に NMI が入ると1フレームぶん古い値に戻って見える"
            % (regressed[0] if regressed else "", max(0, len(regressed) - 1)))

    # --- 切れ目ごとに NMI を実際に割り込ませる ---
    # 上の観測を「NMI が実際に PPU へ書いた値」に置き換えたものである。ゼロページを
    # 覗くのではなく、NMI の読み出し経路（添字を引く → lo/hi を引く → $2005/$2000 に書く）
    # を丸ごと通すので、公開する側と読む側のどちらが崩れても落ちる。
    #
    # ここで縛れ**ない**もの: nmi.s が cam_pub_sel を lo と hi で2回読み直す形にしても、
    # この検証は落ちない（変異試験で確認済み）。NMI ハンドラの実行中にメインループは
    # 1命令も進まないので、ハンドラの中で添字が変わることが**原理的に起こらない**ためである。
    # 「添字は1回だけ読む」は nmi.s の作法として正しいが、テストで縛れる主張ではない。
    mixed = []
    runs = 0
    for old, new in NMI_PAIRS:
        if _visible_camera(old) == _visible_camera(new):
            mixed.append("検証の足場の誤り: 旧 $%04X と 新 $%04X は PPU から見て同じ設定 $%04X "
                         "になるので、混ざっても気付けない組である" % (old, new, _visible_camera(old)))
            continue
        for stop_at in range(64):
            _publish(nes, labels, old)
            _set_cam_x(nes, labels, new)
            gen = nes.call_stepwise(labels["cam_publish"])
            reached = None
            try:
                for n in gen:
                    reached = n
                    if n == stop_at:
                        break
            finally:
                if reached == stop_at:
                    cam, why = _nmi_camera(nes, labels)
                    runs += 1
                    if cam is None:
                        mixed.append("旧 $%04X → 新 $%04X の %d 命令目: %s" % (old, new, stop_at, why))
                    elif cam not in (_visible_camera(old), _visible_camera(new)):
                        mixed.append("旧 $%04X → 新 $%04X: %d 命令目の切れ目で NMI を割り込ませたら "
                                     "スクロール設定が $%04X になった（旧 $%04X でも 新 $%04X でもない。"
                                     "PPU に出るのは下位9bit だけなので、この値で比べている）"
                                     % (old, new, stop_at, cam,
                                        _visible_camera(old), _visible_camera(new)))
                gen.close()
            if reached != stop_at:
                break                       # cam_publish の命令数を超えた
    r.check("cam_publish の各命令の切れ目で NMI を実際に割り込ませても、"
            "スクロール設定が旧値か新値になる（%d 回）" % runs, not mixed,
            "%s（ほか %d 箇所）。公開（cam_publish）が NMI から見て不可分になっていない。"
            "面の切り替え (stx cam_pub_sel) が lo/hi の書き込みより前に出ていないか、"
            "二面バッファが同じ面への上書きに戻っていないかを見よ。"
            "実機ではこれが 256 ドット境界をまたぐ瞬間の「画面が1画面ぶん飛ぶ」になる"
            % (mixed[0] if mixed else "", max(0, len(mixed) - 1)))


def _check_nmi_reads_published(nes, labels, oracle, state, r):
    """NMI が cam_x ではなく公開コピーを読んでいること。

    公開コピーと cam_x を**わざと食い違わせて** NMI を単体で走らせる。
    誰かが nmi.s で cam_x を直接読む形に戻したら、これだけが落ちる。
    """
    r.section("NMI が読むのは公開コピーであって cam_x ではない")

    # 公開値 / cam_x の順。NT ビット（bit8）も横スクロール（下位）も食い違う組にする。
    # 片方しか違わない組だと、読み間違えても値が一致してしまい素通りする。
    cases = ((0x0100, 0x02FF), (0x02FF, 0x0100), (0x0000, 0x01FF), (0x01FF, 0x0000))

    wrong = []
    faces = set()
    for published, decoy in cases:
        want = _visible_camera(published)
        if want == _visible_camera(decoy):
            wrong.append("検証の足場の誤り: 公開 $%04X と cam_x $%04X は PPU から見て同じ設定 "
                         "$%04X になるので、どちらを読んでも区別できない" % (published, decoy, want))
            continue
        _publish(nes, labels, published)
        faces.add(_get(nes, labels, "cam_pub_sel"))
        _set_cam_x(nes, labels, decoy)          # 公開せずに cam_x だけ動かす
        cam, why = _nmi_camera(nes, labels)
        if cam is None:
            wrong.append("公開 $%04X / cam_x $%04X: %s" % (published, decoy, why))
        elif cam != want:
            wrong.append("公開 $%04X / cam_x $%04X のとき、NMI のスクロール設定は $%04X "
                         "（期待 $%04X。%s を読んでいる）"
                         % (published, decoy, cam, want,
                            "cam_x" if cam == _visible_camera(decoy) else "どちらでもない値"))

    r.check("公開コピーと cam_x が食い違っていても、NMI は公開コピーだけを見る"
            "（面 %s の両方で確認）" % sorted(faces), not wrong,
            "%s（ほか %d 組）。NMI が cam_x を直接読むと、メインが lo/hi を書く2命令の隙に"
            "NMI が入ったとき「新しい lo と古い hi」の組を読む。cam_x の bit8 は"
            "ネームテーブル選択ビットなので、**256 ドット境界をまたぐ瞬間だけ画面が"
            "1画面ぶん飛ぶ**。sei では NMI は止まらないので割り込み禁止では防げない"
            % (wrong[0] if wrong else "", max(0, len(wrong) - 1)))

    r.check("公開コピーの面の添字が 0/1 の外に出ない", faces <= set(range(CAM_PUB_FACES)),
            "cam_pub_sel が %s になった。公開コピーは2バイトの配列なので、"
            "添字がこの範囲を出ると隣のゼロページ変数を読み書きする" % sorted(faces))

    # cam_update（毎フレームの入口）も最後に公開すること。呼び忘れると
    # 「NMI が1フレーム古いカメラを出し続ける」という気付きにくい壊れ方をする。
    _publish(nes, labels, 0x0100)
    _place_player(nes, labels, 0x0200 + DEADZONE_CENTER)
    nes.call(labels["cam_update"])
    cam = _cam_x(nes, labels)
    value, _sel = _published(nes, labels)
    r.check("cam_update がカメラを動かしたあと公開までしている", value == cam,
            "cam_update のあと cam_x=$%04X だが公開コピーは $%04X。"
            "カメラを動かす入口（cam_update / scroll_warp_to）は必ず最後に cam_publish を"
            "通ること。抜けると NMI が古いカメラを出し続け、絵とスクロールが1フレームずれる"
            % (cam, value if value is not None else -1))


# ---------------------------------------------------------------- (b) 張り直し
DIRT = 0x5A          # VRAM を汚す値。仮背景のタイル (0..5) とも属性 ($00/$FF) とも違う


def _dirty_nametables(nes):
    """ネームテーブル2枚（属性テーブル込み）を「前の場面の絵」で埋める。

    起動時の一括転送が書いた絵がそのまま残っていると、張り直しが1列も走らなくても
    検証が通ってしまう。汚してから飛び込むこと。
    """
    for addr in range(NT_BASE, NT_BASE + 0x800):
        nes.ppu.vram[addr] = DIRT


def _warp(nes, labels, target):
    """scroll_warp_to を「描画を止めて呼び、あとで PPUCTRL/PPUMASK を戻す」手順で呼ぶ。

    手順は scroll.s のコメントが呼び出し側に要求しているものである（$2007 を予算無視で
    叩くので描画無効中に呼ぶ／$2006 を叩くので戻したあとに PPUCTRL を置き直す）。
    ここを守らずに呼ぶと、それは engine の不具合ではなく呼び出し側の誤用になる。
    """
    watch = _watch(nes)
    if watch is not None:
        watch.idle("scroll_warp_to")         # 中で vram_queue_reset を呼ぶ
    saved_ctrl = nes.ppu.ctrl
    saved_mask = nes.ppu.mask
    nes.ppu.write(1, 0)                      # 手順1: 描画を止める
    try:
        nes.call(labels["scroll_warp_to"], a=target & 0xFF, x=(target >> 8) & 0xFF)
    finally:
        nes.ppu.write(0, saved_ctrl)         # 手順3: PPUCTRL を置き直す（NMI 許可ビット込み）
        nes.ppu.write(1, saved_mask)


def _check_warp(nes, labels, oracle, state, r):
    r.section("scroll_warp_to（任意のカメラ位置から背景を張り直す）")

    limit = _cam_limit(nes, labels)

    # 目標は「何を試しているか」で選ぶ。カメラ位置そのものは上限から導く。
    targets = [
        ("列の境界に乗る / カメラ 0", 0),
        ("列の境界に乗らない (cam_x&7 = 3) / NT ビット 0", 31 * 8 + 3),
        ("ブロックの右端 ((cam_x>>3)&31 = 31) / NT ビット 1", 63 * 8 + 7),
        ("NT ビット 1 / 巻き取りの手前", 0x0105),
        ("巻き取り (%d 列) を越えた先" % NT_WRAP_COLS, (NT_WRAP_COLS + 6) * 8 + 1),
        ("カメラの上限ちょうど", limit),
        ("上限を越える指定（クランプされる）", limit + 0x0100),
        ("16bit の上限を越える指定（クランプされる）", 0xFFFF),
    ]

    bad_state = []
    bad_tiles = []
    bad_attrs = []
    bad_quiet = []

    for title, target in targets:
        want = min(target, limit)       # 上限を越える指定はクランプされる
        _dirty_nametables(nes)
        # 積み残しがある状態から飛ぶ。古い列の記録は張り直した絵を上書きするので、
        # warp は捨てるのが正しい（scroll.s の注記）。
        _queue_record(nes, labels, VQ_SCRATCH, [0] * 8)
        _warp(nes, labels, target)

        cam = _cam_x(nes, labels)
        col = _cam_col(nes, labels)
        pending = _pending(nes, labels)
        published, _sel = _published(nes, labels)
        why = []
        if cam != want:
            why.append("cam_x=$%04X（期待 $%04X。上限 $%04X でクランプする）" % (cam, want, limit))
        if col != cam >> TILE_W_SHIFT:
            why.append("cam_col=%d（期待 %d = cam_x>>%d）" % (col, cam >> TILE_W_SHIFT, TILE_W_SHIFT))
        if pending != 0:
            why.append("転送キューに %d バイト残っている（飛び込み前の記録を捨てていない）" % pending)
        if published != cam:
            why.append("公開コピー=$%04X（期待 $%04X）" % (published if published is not None else -1, cam))
        if why:
            bad_state.append("%s（目標 $%04X）: %s" % (title, target, " / ".join(why)))

        first = cam >> TILE_W_SHIFT
        tiles = [p for p in (_column_problem(nes, oracle, first + k, "可視 %d 列目" % (k + 1))
                             for k in range(VISIBLE_COLS)) if p]
        if tiles:
            bad_tiles.append("%s（cam_x=$%04X）: %s（ほか %d 列）"
                             % (title, cam, tiles[0], len(tiles) - 1))

        seen = set()
        attrs = []
        for k in range(VISIBLE_COLS):
            addr = _attr_addr(first + k)
            if addr in seen:
                continue
            seen.add(addr)
            p = _attr_problem(nes, oracle, first + k, "可視 %d 列目" % (k + 1))
            if p:
                attrs.append(p)
        if attrs:
            bad_attrs.append("%s（cam_x=$%04X）: %s（ほか %d 本）"
                             % (title, cam, attrs[0], len(attrs) - 1))

        # --- 列追跡の初期化漏れは VRAM の中身には出ない ---
        # 操作キャラを不感帯の中央に置けばカメラは動かない。そこで cam_update を
        # 1回呼んで列が1本も積まれなければ、cam_col が新しいカメラに揃っている。
        # 揃っていなければ、張り直した絵の上に同じ絵が数十フレームかけて積み直される。
        _place_player(nes, labels, cam + DEADZONE_CENTER)
        tail_before = _get(nes, labels, "vq_tail")
        watch = _watch(nes)
        if watch is not None:
            watch.idle("cam_update")
        nes.call(labels["cam_update"])
        if watch is not None:
            watch.chain("scroll_warp_to のあとの cam_update")
        moved = _cam_x(nes, labels) != cam
        queued = _pending(nes, labels)
        if moved or queued or _get(nes, labels, "vq_tail") != tail_before:
            bad_quiet.append("%s（cam_x=$%04X）: cam_update を1回呼んだら %s"
                             % (title, cam,
                                "カメラが $%04X へ動いた（不感帯の中央に置いたのに）"
                                % _cam_x(nes, labels) if moved
                                else "列が %d バイトぶん積まれた" % queued))

    r.check("飛び込み先で cam_x / cam_col / 転送キュー / 公開コピーが揃う（%d 箇所）" % len(targets),
            not bad_state,
            "%s（ほか %d 箇所）。scroll_warp_to は「クランプ → キューを空に → cam_col を"
            "置き直す → 背景を張り直す → 公開」の順で、どれが欠けても症状が別々に出る"
            % (bad_state[0] if bad_state else "", max(0, len(bad_state) - 1)))

    r.check("飛び込み先の可視 %d 列のタイルが生成規則と一致する（%d 箇所）"
            % (VISIBLE_COLS, len(targets)), not bad_tiles,
            "%s（ほか %d 箇所）。飛び込みは1列ずつの追跡では追いつかない（96 列飛べば "
            "96 フレーム＝1.6 秒のあいだ誤った背景が出る）ので、bg_fill_window が"
            "可視列を丸ごと書き直す。$%02X が残っている列は**一度も書かれていない**"
            % (bad_tiles[0] if bad_tiles else "", max(0, len(bad_tiles) - 1), DIRT))

    r.check("飛び込み先の可視列を覆う属性列が生成規則と一致する（%d 箇所）" % len(targets),
            not bad_attrs,
            "%s（ほか %d 箇所）。属性はタイルと別の経路で書かれるので、"
            "タイルだけ張り直して属性を忘れると、模様とパレットの縞がずれたまま残る"
            % (bad_attrs[0] if bad_attrs else "", max(0, len(bad_attrs) - 1)))

    r.check("飛び込んだ直後は列追跡が新しいカメラに揃っている（cam_update で1列も積まない）",
            not bad_quiet,
            "%s（ほか %d 箇所）。cam_col を置き直し忘れると、張り直した直後に"
            "「古い cam_col から新しい cam_col まで」の列が延々と積み直され、"
            "せっかく書いた絵の上に同じ絵を数十フレームかけて書き戻す。"
            "VRAM の中身は最終的に正しくなるので、**この検証でしか捕まらない**"
            % (bad_quiet[0] if bad_quiet else "", max(0, len(bad_quiet) - 1)))


# ---------------------------------------------------------------- (c) STEP の番人
def _open(nes, labels, dst, length, flags):
    """vram_queue_open を叩き、受理/拒否とカウンタの動きを返す。

    拒否されたときに「半端な記録（half-open）が残らない」ことまで見たいので、
    先に確定済みの記録を1つ積んでから開く。
    """
    nes.call(labels["vram_queue_reset"])
    _queue_record(nes, labels, VQ_SCRATCH, [0x11, 0x22, 0x33, 0x44])
    before = dict(tail=_get(nes, labels, "vq_tail"), wr=_get(nes, labels, "vq_wr"),
                  head=_get(nes, labels, "vq_head"),
                  overflow=_get(nes, labels, "vq_overflow"),
                  badstep=_get(nes, labels, "vq_badstep"),
                  pending=_pending(nes, labels))
    tail_bytes = _record_header(nes, labels, before["tail"])
    _set(nes, labels, "vq_dst_lo", dst)
    _set(nes, labels, "vq_dst_hi", dst >> 8)
    carry = nes.call(labels["vram_queue_open"], a=flags, x=length).carry
    after = dict(tail=_get(nes, labels, "vq_tail"), wr=_get(nes, labels, "vq_wr"),
                 head=_get(nes, labels, "vq_head"),
                 overflow=_get(nes, labels, "vq_overflow"),
                 badstep=_get(nes, labels, "vq_badstep"),
                 pending=_pending(nes, labels))
    return dict(accepted=not carry, before=before, after=after,
                tail_bytes_before=tail_bytes,
                tail_bytes_after=_record_header(nes, labels, before["tail"]))


def _check_step_guard(nes, labels, oracle, state, r):
    """STEP 記録の「アドレス下位が桁上がりしない」前提を、積む時点で検査していること。

    STEP 形式は転送先アドレスの下位だけを増分で進める（上位に繰り上げない）。
    下位が桁上がりすると巻き取って**同じページの先頭**へ書き込む
    ——属性を書いたつもりでネームテーブルの上段を潰す、という壊れ方をする。
    """
    r.section("VQ_STEP のページ境界の番人（vq_badstep）")

    # --- 増分・長さ・フラグのビット割当を、engine が実際に積む記録から実測する ---
    # ここを定数で書くと、engine が形式を変えたときにテストだけが古い前提で通ってしまう。
    nes.call(labels["vram_queue_reset"])
    _set16(nes, labels, "bg_col_lo", "bg_col_hi", 8)
    if nes.call(labels["bg_queue_attr"]).carry:
        r.check("属性列を空のキューに積める", False,
                "空のキューに bg_queue_attr が積めなかった。STEP 形式の実測ができないので"
                "このセクションは飛ばした")
        return
    # 記録の位置は vq_head から取る。「reset 直後の記録は添字0」と決め打たない
    # （決め打つと、engine が vram_queue_reset を `head := tail` の1命令に変えられない。
    #  いまの reset は head と tail を別々の命令で 0 にするので、その隙に NMI が入ると
    #  head=0 / tail=旧 の組が見え、バッファ先頭の残骸をヘッダとして読む）。
    attr_len, attr_hi, attr_lo, attr_flags = _record_header(
        nes, labels, _get(nes, labels, "vq_head"))

    writes = _flush(nes, labels)
    addrs = [v for a, v in writes if a == PPUADDR]
    data = [v for a, v in writes if a == PPUDATA]
    # STEP 形式は「1バイトごとに $2006 を置き直す」。RUN 形式なら $2006 は先頭の2回だけ。
    per_byte = len(addrs) == 2 * len(data) and len(data) == attr_len
    targets = [(addrs[i * 2] << 8) | addrs[i * 2 + 1] for i in range(len(addrs) // 2)]
    deltas = {targets[i + 1] - targets[i] for i in range(len(targets) - 1)}
    ok = r.check("属性列が STEP 形式（1バイトごとにアドレスを置き直す）で積まれている",
                 per_byte and len(deltas) == 1,
                 "$2006 を %d 回 / $2007 を %d 回書いた（記録の長さは %d）、アドレスの刻みは %s。"
                 "STEP 形式でなくなったなら、このセクションが縛っている前提（下位アドレスの"
                 "桁上がり）自体が無くなっているので、検証ごと見直すこと"
                 % (len(addrs), len(data), attr_len, sorted(deltas)))
    if not ok:
        return

    step = deltas.pop()
    r.check("STEP 形式のフラグが bit7 で、下位7bit が増分 (%d) である" % step,
            bool(attr_flags & 0x80) and (attr_flags & 0x7F) == step,
            "属性列の記録のフラグが $%02X。実測した増分は %d なので、"
            "bit7=1 かつ下位7bit=%d のはず。フラグの割当が変わったなら、"
            "この検証が作る記録も的外れになっている" % (attr_flags, step, step))

    nes.call(labels["vram_queue_reset"])
    _set16(nes, labels, "bg_col_lo", "bg_col_hi", 8)
    nes.call(labels["bg_queue_column"])
    run_flags = _record_header(nes, labels, _get(nes, labels, "vq_head"))[3]

    def step_flags(increment):
        return 0x80 | (increment & 0x7F)

    # --- 境界そのものを性質として書く ---
    # 受理されるのは「最後のバイトまで下位アドレスが桁上がりしないとき、かつそのときだけ」。
    # 表を焼き付けず、増分・長さ・下位アドレスを振って規則と突き合わせる。
    page = attr_hi << 8
    cases = []
    for inc in (1, step, step * 2, step * 4, 0x7F):
        for length in (1, 2, attr_len, attr_len + 1):
            span = inc * (length - 1)
            if span > 0xFF:
                los = (0x00, 0xFF)
            else:
                los = (0x00, (0xFF - span) & 0xFF, (0x100 - span) & 0xFF, 0xFF)
            for lo in los:
                cases.append((inc, length, lo))

    wrong = []
    counted = []
    clobbered = []
    tail_moved = []
    for inc, length, lo in cases:
        want = lo + inc * (length - 1) <= 0xFF
        res = _open(nes, labels, page | lo, length, step_flags(inc))
        where = ("増分 %d / 長さ %d / 転送先 $%04X（最後のバイトは $%04X）"
                 % (inc, length, page | lo, page + lo + inc * (length - 1)))
        if res["accepted"] != want:
            wrong.append("%s: %s（期待 %s）"
                         % (where, "積めた" if res["accepted"] else "突き返された",
                            "受理" if want else "拒否"))
            continue
        d_bad = res["after"]["badstep"] - res["before"]["badstep"]
        d_over = res["after"]["overflow"] - res["before"]["overflow"]
        if d_bad != (0 if want else 1) or d_over != 0:
            counted.append("%s: vq_badstep %+d / vq_overflow %+d（期待 %+d / +0）"
                           % (where, d_bad, d_over, 0 if want else 1))
        if not want:
            if (res["after"]["tail"] != res["before"]["tail"]
                    or res["after"]["wr"] != res["before"]["wr"]
                    or res["after"]["pending"] != res["before"]["pending"]):
                tail_moved.append("%s: vq_tail %d→%d / vq_wr %d→%d / 未処理 %d→%d"
                                  % (where, res["before"]["tail"], res["after"]["tail"],
                                     res["before"]["wr"], res["after"]["wr"],
                                     res["before"]["pending"], res["after"]["pending"]))
            if res["tail_bytes_after"] != res["tail_bytes_before"]:
                clobbered.append("%s: キューの末尾 %s が %s に書き換わった"
                                 % (where, res["tail_bytes_before"], res["tail_bytes_after"]))
        elif res["after"]["wr"] != (res["before"]["tail"] + 4) & 0xFF:
            tail_moved.append("%s: 受理されたのに vq_wr=%d（期待 %d = vq_tail + ヘッダ長）"
                              % (where, res["after"]["wr"], (res["before"]["tail"] + 4) & 0xFF))

    r.check("STEP 記録は、下位アドレスが桁上がりするときに限って突き返される（%d 通り）"
            % len(cases), not wrong,
            "%s（ほか %d 通り）。桁上がりする記録を積むと、転送は巻き取って**同じページの"
            "先頭**を書きつぶす（属性を書いたつもりでネームテーブルの上段が潰れる）。"
            "逆に、またがない記録まで突き返すと属性列が永久に転送されない"
            % (wrong[0] if wrong else "", max(0, len(wrong) - 1)))

    r.check("突き返した回数が vq_badstep にだけ計上される（vq_overflow は増えない）", not counted,
            "%s（ほか %d 通り）。2つのカウンタは意味が違う: vq_overflow は空き不足で"
            "**次フレームに回せば通る**、vq_badstep は記録そのものが不正で"
            "**何度積み直しても通らない**。混ぜると「キューが混んでいるだけ」と誤診する"
            % (counted[0] if counted else "", max(0, len(counted) - 1)))

    r.check("突き返した記録が半端に残らない（vq_tail / vq_wr が動かない）", not tail_moved,
            "%s（ほか %d 通り）。open が失敗したのに vq_wr が進むと、"
            "次に誰かが close を呼んだ瞬間に**中身のない記録**が確定して NMI に流れる"
            % (tail_moved[0] if tail_moved else "", max(0, len(tail_moved) - 1)))

    r.check("突き返しても、既に確定している記録を壊さない", not clobbered,
            "%s（ほか %d 通り）。積めないと分かる前にヘッダを書き始めている"
            % (clobbered[0] if clobbered else "", max(0, len(clobbered) - 1)))

    # --- RUN 形式はこの検査の対象外である ---
    # PPU の内部アドレスは 16bit で加算されるので、RUN 形式はページをまたいでも正しく進む。
    # ここまで突き返すと、ネームテーブルの末尾にかかる転送が通らなくなる。
    run_bad = []
    for lo in (0x00, 0xC8, 0xF8, 0xFF):
        res = _open(nes, labels, page | lo, attr_len, run_flags)
        if not res["accepted"] or res["after"]["badstep"] != res["before"]["badstep"]:
            run_bad.append("転送先 $%04X / フラグ $%02X: %s（vq_badstep %+d）"
                           % (page | lo, run_flags, "突き返された" if not res["accepted"] else "積めた",
                              res["after"]["badstep"] - res["before"]["badstep"]))
    r.check("RUN 形式（PPU の自動加算）はページ境界の検査の対象外", not run_bad,
            "%s（ほか %d 通り）。RUN 形式のアドレスは PPU が 16bit で加算するので、"
            "ページをまたいでも正しい番地に届く。ここを突き返すと、ネームテーブルの"
            "末尾にかかる列が転送されなくなる"
            % (run_bad[0] if run_bad else "", max(0, len(run_bad) - 1)))

    # 後片付け: 次のセクション（があれば）にキューの汚れを持ち越さない。
    nes.call(labels["vram_queue_reset"])
