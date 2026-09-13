"""L2 実行検証 — engine P1 後半（横スクロールのカメラ / VRAM 転送キュー / NMI の書き込み順）。

run_tests.py から呼ばれる。層の分け方は ADR-0004 に従う。

ここで検証してよいのは「値」であって「タイミング」ではない。画面のちらつき、
スクロール中の見た目の滑らかさ、スプライト0ヒットやスキャンライン単位の分割は
この Python エミュレータでは再現していないので、L3（Mesen2）と実機に回す（ADR-0002 / ADR-0004）。

方針:
  * **期待値はテストに書き写さず、ROM の生成器に作らせる。**
    背景の中身は bg.s の bg_tile_at / bg_attr_byte が決める。ここではその出力と
    VRAM の中身を突き合わせる。P3 で stage-author が bg.s を差し替えても、
    「可視列の VRAM が生成器の出力と一致しているか」という問いは変わらない。
  * **調整のノブ（CAM_DEADZONE_HALF / VQ_COST_BUDGET / ステージ長）は実測で導く。**
    主がこれらを変えたときに、仕様が壊れていないのにテストが落ちてはならない。
  * フレームを回して観測するのではなく、cam_update / vram_queue_flush を
    直接呼んで1フレームぶんを進める。run_frames の戻り位置はメインループの処理中であり、
    そこで cam_col やキューを読むと「更新の途中」を見てしまう（run_tests.py 冒頭の注記）。
  * **キューに触る前にメインループを待ちループへ寄せる**（_park）。run_frames の戻り位置は
    bg_queue_column の内側（open 済み・close 前）でありうる。その状態でテストが同じ積み手を
    呼ぶのは実機に存在しない再入であり、そこで engine を評価すると
    「テストが作り出した壊れ方」を追うことになる。寄せ忘れは QueueWatch.idle が捕まえる。
  * **症状ではなく不変条件を主張する。** 転送キューについては次の3つを、
    観測の位相に依存しない形で常時見る（QueueWatch / report_queue_invariants）:
      (a) 記録の連なり — vq_head から辿った記録が長さを保ったままちょうど vq_tail に着地する
      (b) 診断カウンタ — vq_badclose / vq_badlen / vq_badstep がセクションの前後で増えない
      (e) 転送量       — 全テストの全 NMI で $2007 の書き込みが予算以下（check_nmi_write_budget）
    いずれも「混んでいるフレームを探して覗く」必要が無い。位相をずらす掃引でしか出ない
    壊れ方（記録の境界がずれて長さ0のヘッダを読む）を、固定の観測点で捕まえるためである。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "harness"))

from cpu6502 import CpuCrash                       # noqa: E402
from nes import (Nes, VBLANK_CYCLES, boot,         # noqa: E402
                 frame_end, NMI_WRITE_WATCH)

# --- PPU のハード仕様。設計で変えられる値ではないので、ここに置いてよい ---
NT_BASE = 0x2000
NT_STRIDE = 0x0400            # ネームテーブル1枚
NT_ATTR_OFFSET = 0x03C0       # ネームテーブル先頭 → 属性テーブル先頭
SCREEN_TILES_W = 32           # ネームテーブルの横タイル数
SCREEN_TILES_H = 30           # 同 縦
SCREEN_W = 256                # 表示幅（ドット）
TILE_W_SHIFT = 3
ATTR_TILES = 4                # 属性1バイトが受け持つタイル数
ATTR_COLS = 8
ATTR_ROWS = 8
PPUCTRL, PPUSCROLL, PPUADDR, PPUDATA = 0x2000, 0x2005, 0x2006, 0x2007
CTRL_NT_X = 0x01              # PPUCTRL のベースネームテーブル選択（横）

# 横スクロールは垂直ミラーリング（ネームテーブルが左右に2枚）なので、
# 背景はワールドの 64 列 = 512 ドットで巻き取る。
NT_WRAP_COLS = SCREEN_TILES_W * 2

# **可視列は 33 列**である。画面に収まるのはちょうど 32 列だが、カメラが列の境界に
# ぴったり乗っていない限り、右端に1列ぶんがはみ出して見える。ここを 32 と数えると、
# 右端の1列が「1ドットだけ見えているのに中身が古いまま」になる（engine-dev が
# 作業中に直した off-by-one がこれ）。この +1 を縛るのがこのファイルの主目的のひとつ。
VISIBLE_COLS = SCREEN_TILES_W + 1

# スクラッチの転送先 VRAM アドレス。キューの仕組み（予算・巻き取り・未確定の記録）を
# 見るためだけに使う。ネームテーブル0/1（$2000-$27FF）の中身を壊さない場所を選ぶ。
# ※ 実機では垂直ミラーリングにより $2800 は $2000 の写しだが、
#   ここでのねらいはキューの挙動であって画面ではない。
VQ_SCRATCH = 0x2800

# リングバッファの大きさ。8bit の添字に巻き取りを任せてある以上 256 以外は取り得ない
# （vram.s は `vq_buf, x` と inx だけでリングを回している）ので、ここに置いてよい。
VQ_SIZE = 256

NEEDED = (
    "cam_update", "cam_follow_player", "cam_set_stage_width",
    "cam_x_lo", "cam_x_hi", "cam_col_lo", "cam_col_hi",
    "cam_limit_lo", "cam_limit_hi", "stage_w_lo", "stage_w_hi",
    "bg_tile_at", "bg_attr_byte", "bg_col_lo", "bg_col_hi",
    "bg_queue_column", "bg_queue_attr",
    "vram_queue_reset", "vram_queue_open", "vram_queue_byte", "vram_queue_close",
    "vram_queue_flush", "vram_queue_pending", "vq_dst_lo", "vq_dst_hi",
    "vq_head", "vq_tail", "vq_wr", "vq_buf",
    "vq_overflow", "vq_badstep", "vq_badlen", "vq_badclose",
    "ent_x_lo", "ent_x_hi", "ppu_ctrl_shadow",
)

# 「積む側が一度も間違えていない」の位相非依存な言い換え。
#   vq_badclose … close が「申告した長さちょうど書いたか」で突き返した回数
#   vq_badlen   … 長さが範囲外で open が突き返した回数
#   vq_badstep  … STEP 記録がページ境界をまたぐので open が突き返した回数
# どれも「何度積み直しても通らない」不具合の数である（空き不足の vq_overflow とは違う）。
# 混んでいるかどうか＝観測の位相に関係なく 0 でなければならない。
DIAG_COUNTERS = ("vq_badclose", "vq_badlen", "vq_badstep")


# ---------------------------------------------------------------- 足場
def _get(nes, labels, name):
    return nes.ram[labels[name] & 0x7FF]


def _set(nes, labels, name, value):
    nes.ram[labels[name] & 0x7FF] = value & 0xFF


def _get16(nes, labels, lo, hi):
    return _get(nes, labels, lo) | (_get(nes, labels, hi) << 8)


def _set16(nes, labels, lo, hi, value):
    _set(nes, labels, lo, value)
    _set(nes, labels, hi, value >> 8)


def _cam_x(nes, labels):
    return _get16(nes, labels, "cam_x_lo", "cam_x_hi")


def _cam_col(nes, labels):
    return _get16(nes, labels, "cam_col_lo", "cam_col_hi")


def _cam_limit(nes, labels):
    return _get16(nes, labels, "cam_limit_lo", "cam_limit_hi")


def _stage_w(nes, labels):
    return _get16(nes, labels, "stage_w_lo", "stage_w_hi")


def _place_player(nes, labels, x):
    """操作キャラ（添字0）のワールドX を置く。カメラを動かす唯一の入力である。"""
    _set16(nes, labels, "ent_x_lo", "ent_x_hi", x)


def _pending(nes, labels):
    return nes.call(labels["vram_queue_pending"]).a


def _diag(nes, labels):
    """診断カウンタの現在値。"""
    return {name: _get(nes, labels, name) for name in DIAG_COUNTERS}


def _diag_delta(before, after):
    """増えたカウンタだけを '名前 +n' の並びにする。"""
    return ["%s +%d" % (n, (after[n] - before[n]) & 0xFF)
            for n in DIAG_COUNTERS if after[n] != before[n]]


def _record_header(nes, labels, at):
    """キューの記録のヘッダ [長さ, アドレス上位, アドレス下位, フラグ] を読む。

    at は**リングの添字**である（vq_head / vq_tail から取ること）。
    「reset 直後の記録は添字0にある」と決め打ってはならない。vram_queue_reset が
    head/tail を 0 に落とすのは今の実装の都合でしかなく、`head := tail` にすれば
    1命令で空にできて NMI との競合も無くなる（engine-dev の申し送り）。
    テストが添字0を決め打つと、engine 側だけではその変更ができない。
    """
    base = labels["vq_buf"]
    return [nes.ram[(base + ((at + i) & 0xFF)) & 0x7FF] for i in range(4)]


def _park(nes, labels):
    """メインループを「そのフレームの更新を終えて次の NMI を待っている」点へ寄せる。

    **キューに触る nes.call の前に通すこと。** run_frames が戻るのはメインループの
    更新の最中であり、そこは bg_queue_column が記録を組み立てている途中（open 済み・
    close 前）でありうる。その状態でテストが同じ積み手を呼ぶと、
    「open の中に open が入る」という**実機には存在しない**並びを作ってしまう。
    実際、それで組み上がった「30 と申告して 56 バイト書いた記録」が engine の
    不具合（close が長さを検査していない）を炙り出したが、検証としては
    「engine がテストの作り出した状態を封じ込めた」ことしか示せていない。
    """
    frame_end(nes, labels)


class QueueWatch:
    """転送キューの不変条件を、決まった観測点で毎回確かめる番人。

    (a) 記録の連なり: vq_head から辿った記録が、長さを保ったまま**ちょうど vq_tail に
        着地する**こと。ずれると NMI は以降データの途中をヘッダとして読み続ける
        （長さ0と読めば $2007 に 256 バイト流れる）。固定の観測点で成立する主張なので、
        位相をずらす掃引に頼らずに捕まえられる。
    (d) 再入の排除: メインループ側の入口を呼ぶ前に、組み立ての途中でないこと。
    """

    def __init__(self, nes, labels, hdr, max_len, max_len_source):
        self.nes = nes
        self.labels = labels
        self.hdr = hdr
        self.max_len = max_len
        self.max_len_source = max_len_source
        self.chain_checks = 0
        self.chain_bad = []
        self.entry_checks = 0
        self.reentry = []

    def idle(self, where):
        """メインループ側の入口を呼ぶ直前。組み立て中（vq_wr != vq_tail）なら記録する。"""
        self.entry_checks += 1
        wr = _get(self.nes, self.labels, "vq_wr")
        tail = _get(self.nes, self.labels, "vq_tail")
        if wr != tail and len(self.reentry) < 8:
            self.reentry.append("%s を呼ぶ直前に vq_wr=%d / vq_tail=%d（記録の組み立て中）"
                                % (where, wr, tail))

    def chain(self, where):
        """記録の連なりを辿る。cam_update + flush のあと毎回呼ぶ。"""
        self.chain_checks += 1
        problem = _chain_problem(self.nes, self.labels, self.hdr, self.max_len)
        if problem and len(self.chain_bad) < 4:
            self.chain_bad.append("%s: %s" % (where, problem))


def _watch(nes):
    return getattr(nes, "vq_watch", None)


def _chain_problem(nes, labels, hdr, max_len):
    """vq_head から vq_tail まで記録を辿り、壊れていたら1行で読める説明を返す。"""
    base = labels["vq_buf"]
    head = _get(nes, labels, "vq_head")
    tail = _get(nes, labels, "vq_tail")
    pending = (tail - head) & 0xFF
    at = head
    lens = []
    while at != tail:
        remaining = (tail - at) & 0xFF
        length = nes.ram[(base + at) & 0x7FF]
        if not 1 <= length <= max_len:
            return ("vq_head=%d から %d 件目の記録（添字 %d）の長さが %d で、1..%d の外にある"
                    "（未処理 %d バイト / ここまでの長さ %s）。"
                    "長さ0の記録は誰も積まない——NMI が記録の境界からずれて、"
                    "ネームテーブルの中身をヘッダとして読んでいる"
                    % (head, len(lens) + 1, at, length, max_len, pending, lens))
        step = hdr + length
        if step > remaining:
            return ("vq_head=%d から %d 件目の記録（添字 %d、長さ %d）が vq_tail=%d を "
                    "%d バイト飛び越す（未処理 %d バイト / ここまでの長さ %s）。"
                    "記録の連なりに穴が開いている＝積む側が申告した長さと書いたバイト数が"
                    "食い違ったまま公開された。以降 vq_head は永久にデータの途中を指す"
                    % (head, len(lens) + 1, at, length, tail, step - remaining,
                       pending, lens))
        lens.append(length)
        at = (at + step) & 0xFF
    return None


def _flush(nes, labels):
    """NMI が1フレームに行う転送を1回ぶん実行し、その間の書き込みを返す。

    NMI と同じく VBlank 中に呼ぶ（描画中の $2007 書き込みとして数えられないように）。
    """
    was_vblank = nes.ppu.in_vblank
    outer_log = nes.write_log
    nes.ppu.in_vblank = True
    nes.write_log = []
    try:
        nes.call(labels["vram_queue_flush"])
        return nes.write_log
    finally:
        nes.write_log = outer_log
        nes.ppu.in_vblank = was_vblank


def _step(nes, labels):
    """1フレームぶんの「カメラ更新 → 転送」を進める（実フレームは回さない）。

    呼ぶ前に「組み立ての途中でない」ことを確かめ、進めたあとに記録の連なりを辿る。
    どちらも固定の観測点で成り立つ不変条件なので、位相に依存しない。
    """
    watch = _watch(nes)
    if watch is not None:
        watch.idle("cam_update")
    nes.call(labels["cam_update"])
    writes = _flush(nes, labels)
    if watch is not None:
        watch.chain("cam_update + flush のあと")
    return writes


def _settle_camera(nes, labels, limit=300):
    """カメラの列追跡が追いつき、キューが空になるまで回す。

    戻り値は要したフレーム数。limit フレームで終わらなければ None
    （＝列が積まれ続けて追いつかない。呼び出し側が失敗として報告すること）。
    """
    for i in range(1, limit + 1):
        _step(nes, labels)
        if (_cam_col(nes, labels) == _cam_x(nes, labels) >> TILE_W_SHIFT
                and _pending(nes, labels) == 0):
            return i
    return None


def _aim(nes, labels, target_cam_x, dz_left, dz_right, limit=300):
    """カメラが target_cam_x に来るよう操作キャラを置き、列の転送が終わるまで回す。

    右へ動かすときは不感帯の右端に、左へ動かすときは左端に操作キャラを貼り付ける。
    """
    going_right = target_cam_x > _cam_x(nes, labels)
    _place_player(nes, labels, target_cam_x + (dz_right if going_right else dz_left))
    return _settle_camera(nes, labels, limit)


class BgOracle:
    """期待値の出どころ。仮背景の生成器（bg.s）そのものに列の中身を作らせる。

    生成規則をテスト側に書き写すと、規則を変えたときに2か所を直すことになり、
    しかも「両方同じ間違いを書いた」という失敗の仕方をする。呼んで作らせる方が安全である。
    bg_tile_at / bg_attr_byte は bg_col_lo/hi しか見ないので、列番号で覚えてよい。
    """

    def __init__(self, nes, labels):
        self.nes = nes
        self.labels = labels
        self._cols = {}
        self._attrs = {}

    def _select(self, col):
        _set16(self.nes, self.labels, "bg_col_lo", "bg_col_hi", col)

    def column(self, col):
        """列 col のネームテーブル1列（30タイル）。"""
        if col not in self._cols:
            self._select(col)
            self._cols[col] = [self.nes.call(self.labels["bg_tile_at"], x=row).a
                               for row in range(SCREEN_TILES_H)]
        return self._cols[col]

    def attr(self, col):
        """列 col を含む属性列の属性バイト。"""
        if col not in self._attrs:
            self._select(col)
            self._attrs[col] = self.nes.call(self.labels["bg_attr_byte"]).a
        return self._attrs[col]


def _nt_addr(col):
    """ワールドのタイル列 → ネームテーブル上のアドレス（垂直ミラーリング、64列で巻き取り）。"""
    return (NT_BASE + ((col // SCREEN_TILES_W) & 1) * NT_STRIDE
            + (col % SCREEN_TILES_W))


def _attr_addr(col):
    """ワールドのタイル列 → その列を含む属性列の先頭アドレス。"""
    return (NT_BASE + ((col // SCREEN_TILES_W) & 1) * NT_STRIDE + NT_ATTR_OFFSET
            + ((col // ATTR_TILES) % ATTR_COLS))


def _column_problem(nes, oracle, col, where):
    """列 col の VRAM が生成器の出力と違っていたら、1行で読める説明を返す。"""
    want = oracle.column(col)
    addr = _nt_addr(col)
    got = [nes.ppu.vram[addr + row * SCREEN_TILES_W] for row in range(SCREEN_TILES_H)]
    if got == want:
        return None
    row = next(i for i in range(SCREEN_TILES_H) if got[i] != want[i])
    bad = sum(1 for i in range(SCREEN_TILES_H) if got[i] != want[i])
    blank = all(v == 0 for v in got)
    return ("ワールド列 %d（%s / $%04X）: 行%d のタイルが $%02X、生成規則は $%02X"
            "（%d/%d 行が食い違い%s）"
            % (col, where, addr + row * SCREEN_TILES_W, row, got[row], want[row],
               bad, SCREEN_TILES_H, "、まるごと未転送（全部 $00）" if blank else ""))


def _attr_problem(nes, oracle, col, where):
    want = oracle.attr(col)
    addr = _attr_addr(col)
    got = [nes.ppu.vram[addr + row * ATTR_COLS] for row in range(ATTR_ROWS)]
    if all(v == want for v in got):
        return None
    row = next(i for i in range(ATTR_ROWS) if got[i] != want)
    return ("ワールド列 %d の属性列（%s / $%04X）: 第%d行が $%02X、生成規則は $%02X"
            % (col, where, addr + row * ATTR_COLS, row, got[row], want))


# ---------------------------------------------------------------- 入口
def layer2_scroll(rom_path, labels, r):
    print("L2 実行検証 / engine P1 後半（スクロール・VRAM 転送キュー）")

    missing = [n for n in NEEDED if n not in labels]
    if missing:
        r.check("スクロールのラベルが build/roaring.labels に揃っている", False,
                "ラベルが無い: %s。モジュールがリンクから外れたか、名前が変わっている。"
                "このセクションの検証はラベル無しでは書けないので全部飛ばした" % ", ".join(missing))
        return

    try:
        nes = Nes(rom_path)
        nes.reset()
        if boot(nes) is None:
            r.check("スクロールの検証用に起動する", False,
                    "起動しても NMI が来ない（OAM DMA が1回も無い）。"
                    "起動処理の検証（起動処理が N フレーム以内に終わる）を先に見よ")
            return
    except CpuCrash as e:
        r.check("スクロールの検証用に起動する", False, str(e))
        return

    # キューに触る前にメインループを待ちループへ寄せる（(d) 再入の排除）。
    # ここから下は nes.call でしか CPU を進めない（call は PC を保存して戻す）ので、
    # メインループは待ちループに置かれたままになる。実フレームを回す検証だけが
    # 例外で、そこは回したあとに自分で寄せ直す（_check_nmi_writes）。
    _park(nes, labels)

    oracle = BgOracle(nes, labels)
    state = {}
    if attach_queue_watch(nes, labels, state) is None:
        r.check("記録のヘッダ長を実測できる", False,
                "空のキューに長さ4の記録すら open できない。キューの不変条件を"
                "辿る足場が作れないので、このセクションは飛ばした")
        return state
    before = _diag(nes, labels)
    base_len = _engine_record_len(nes, labels)
    if base_len is None:
        why = _diag_delta(before, _diag(nes, labels))
        r.check("engine が積む記録（ネームテーブル1列）の長さを実測できる", False,
                "空のキューに bg_queue_column が1本も積めなかった（%s）。"
                "空きは十分あるので、突き返した理由はカウンタが名指ししている: "
                "vq_badclose なら**積む側が申告した長さと違うバイト数を書いている**、"
                "vq_badlen なら長さの申告そのものが範囲外、vq_badstep なら STEP 記録が"
                "ページ境界をまたいでいる。背景が1列も転送できない＝スクロールが止まる"
                % ("／".join(why) if why else "カウンタは1つも増えていない"))
        return state
    state["base_len"] = base_len
    state["budget"], state["budget_how"] = _measure_cost_budget(nes, labels, base_len)

    # (b) 診断カウンタはセクションの前後で増えていないこと。
    # わざと突き返させるセクション（close の番人 / STEP の番人）だけは自分で数えるので除く。
    counted_elsewhere = {"close の番人"}
    dirty = []
    for name, fn in (("カメラ追従", _check_deadzone),
                     ("ステージ端のクランプ", _check_clamp),
                     ("可視列の転送", _check_visible_columns),
                     ("転送キュー", _check_queue),
                     ("close の番人", _check_close_guard),
                     ("NMI の書き込み順", _check_nmi_writes)):
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

    report_queue_invariants(nes, labels, r, dirty, "スクロール")
    return state


def report_queue_invariants(nes, labels, r, dirty, where):
    """(a) 記録の連なり / (b) 診断カウンタ / (d) 再入 の3件をまとめて報告する。

    l2_camera からも使う。どれも「位相に依存しない不変条件」なので、
    観測した回数を名前に出して、空振りしていないことが読めるようにする。
    """
    watch = _watch(nes)
    r.section("転送キューの不変条件（%s）" % where)

    r.check("記録の連なりが vq_head から vq_tail まで途切れない"
            "（%d 回の観測 / 長さの上限 %d は %s）"
            % (watch.chain_checks, watch.max_len, watch.max_len_source),
            watch.chain_checks > 0 and not watch.chain_bad,
            "%s（ほか %d 箇所）。記録は「ヘッダ %d バイト + 申告した長さ」で連なっており、"
            "最後は必ず vq_tail に着地する。ずれると NMI は記録の境界を見失い、"
            "以降データの途中をヘッダとして読み続ける（長さ0と読めば $2007 に 256 バイト流れ、"
            "VBlank を倍以上はみ出す）。症状は位相しだいで出たり出なかったりするが、"
            "この不変条件は固定の観測点で常に成り立つ"
            % (watch.chain_bad[0] if watch.chain_bad
               else "cam_update + flush のあとを一度も観測していない（検証が空振りしている）",
               max(0, len(watch.chain_bad) - 1), watch.hdr))

    r.check("積む側が一度もバイト数を数え違えていない（診断カウンタが増えない）", not dirty,
            "%s（ほか %d セクション）。vq_badclose は「close の時点で、open に申告した長さと"
            "実際に書いたバイト数が違った」回数である。空き不足（vq_overflow、次フレームに"
            "回せば通る）とは違い、**何度積み直しても通らない**。"
            "vq_badclose == 0 は「積む側が一度も数え違えていない」の位相非依存な言い換えで、"
            "混んでいるフレームを探して覗く必要が無い"
            % (dirty[0] if dirty else "", max(0, len(dirty) - 1)))

    r.check("キューに触る前、メインループが記録の組み立て中でない（%d 回の観測）"
            % watch.entry_checks, not watch.reentry,
            "%s（ほか %d 箇所）。実機にこの状態は存在しない: 記録を組み立てるのは"
            "メインループだけで、その途中に別の積み手が割り込むことはない"
            "（NMI は取り出す側であって積まない）。テストが nes.call で作り出した"
            "この状態で engine を評価すると、**テストが作った壊れ方**を engine の不具合として"
            "追うことになる。キューに触る前に frame_end() で待ちループへ寄せること"
            % (watch.reentry[0] if watch.reentry else "", max(0, len(watch.reentry) - 1)))


# ---------------------------------------------------------------- 不感帯
def _check_deadzone(nes, labels, oracle, state, r):
    """不感帯の内ではカメラが動かず、外では境界に貼り付くこと。

    CAM_DEADZONE_HALF は主が手触りで調整するノブなので、値をテストに焼き付けない。
    「操作キャラの画面X をずらしてカメラが動き出す位置」を実測して境界を求め、
    そのうえで不感帯そのものの性質（連続している／中央対称／外では貼り付く）を見る。
    """
    r.section("カメラ追従（不感帯）")

    base = 0x0100              # 左右のクランプから十分離れた位置で測る
    limit = _cam_limit(nes, labels)
    moved = {}
    for s in range(SCREEN_W):
        _set16(nes, labels, "cam_x_lo", "cam_x_hi", base)
        _place_player(nes, labels, base + s)
        nes.call(labels["cam_follow_player"])
        moved[s] = _cam_x(nes, labels)

    still = [s for s in range(SCREEN_W) if moved[s] == base]
    ok = r.check("操作キャラの画面X に、カメラが動かない帯（不感帯）がある",
                 bool(still) and still == list(range(still[0], still[-1] + 1)),
                 "カメラが動かない画面X は %s（期待は連続した1本の帯）。"
                 "帯が空なら不感帯が消えている＝1ドット動くたびに背景全体が動いて酔う。"
                 "帯が飛び飛びなら追従の比較が 16bit で行われていない疑い"
                 % (still[:8] + ["..."] if len(still) > 8 else still))
    if not ok:
        return

    left, right = still[0], still[-1]
    state["dz_left"], state["dz_right"] = left, right

    r.check("不感帯が画面の中央に対称である（左端%d / 右端%d）" % (left, right),
            left + right == SCREEN_W and 0 < left < right < SCREEN_W,
            "左端=%d, 右端=%d（左端+右端 が %d、期待 %d = 画面幅）。"
            "constants.inc の不感帯は CAM_CENTER_X から左右に同じだけ取る決まりなので、"
            "CAM_DEADZONE_HALF をいくつにしても左右の和は画面幅になる。"
            "ずれていると、左に歩くときと右に歩くときで操作キャラの画面上の位置が食い違う"
            % (left, right, left + right, SCREEN_W))

    stick = []
    for s in range(SCREEN_W):
        if s < left:
            want = base + s - left       # 左の境界に貼り付く
        elif s > right:
            want = base + s - right      # 右の境界に貼り付く
        else:
            continue
        if moved[s] != want:
            stick.append("画面X=%d のとき カメラ %d（期待 %d）" % (s, moved[s], want))
    r.check("不感帯の外では、はみ出したぶんだけカメラが動いて境界に貼り付く", not stick,
            "%s。不感帯の外に出たぶんだけ動かすのが追従の約束で、"
            "多すぎれば行き過ぎて揺り戻し、少なければ操作キャラが画面外へ置き去りになる"
            % "／".join(stick[:4]))

    over = [s for s in range(SCREEN_W) if not 0 <= moved[s] <= limit]
    r.check("追従の結果がステージの範囲 (0..%d) を出ない" % limit, not over,
            "画面X=%s でカメラが %s になった（範囲 0..%d）。追従の途中で 16bit が巻き取ると "
            "カメラがステージの反対側へ飛ぶ"
            % (over[:4], [moved[s] for s in over[:4]], limit))


# ---------------------------------------------------------------- クランプ
def _check_clamp(nes, labels, oracle, state, r):
    """ステージの左端 0 と右端 cam_limit でカメラが止まること。"""
    r.section("ステージ端のクランプ")

    stage_w = _stage_w(nes, labels)
    limit = _cam_limit(nes, labels)
    r.check("カメラの上限がステージ幅 - 画面幅である (%d - %d = %d)"
            % (stage_w, SCREEN_W, stage_w - SCREEN_W),
            limit == max(0, stage_w - SCREEN_W),
            "cam_limit=%d（ステージ幅 %d、画面幅 %d なので期待 %d）。"
            "上限が大きすぎるとステージの終端より先の、何も書いていない背景が見える"
            % (limit, stage_w, SCREEN_W, max(0, stage_w - SCREEN_W)))

    _place_player(nes, labels, stage_w + 0x200)      # ステージの外まで押し込む
    nes.call(labels["cam_follow_player"])
    r.check("ステージの右端でカメラが止まる", _cam_x(nes, labels) == limit,
            "操作キャラをステージ幅 %d より右に置いたらカメラが %d になった（上限 %d）。"
            "止め損なうと、転送していない列（何も書いていないネームテーブル）が画面に出る"
            % (stage_w, _cam_x(nes, labels), limit))

    _place_player(nes, labels, 0)
    nes.call(labels["cam_follow_player"])
    r.check("ステージの左端 (0) でカメラが止まる", _cam_x(nes, labels) == 0,
            "操作キャラをワールドX 0 に置いたらカメラが %d になった（期待 0）。"
            "負に振れると 16bit が巻き取ってカメラがステージの遥か右へ飛ぶ"
            % _cam_x(nes, labels))

    # 1画面に満たないステージではスクロールしない（上限 0）。
    # P3 で stage-author が短いステージを書いたときに、ここが 0 に丸まらないと
    # カメラがステージの外へ出る。
    narrow = SCREEN_W - 8
    nes.call(labels["cam_set_stage_width"], a=narrow & 0xFF, x=narrow >> 8)
    narrow_limit = _cam_limit(nes, labels)
    nes.call(labels["cam_set_stage_width"], a=stage_w & 0xFF, x=stage_w >> 8)
    r.check("1画面より狭いステージではカメラの上限が 0 になる", narrow_limit == 0,
            "幅 %d（画面幅 %d 未満）のステージで cam_limit=%d（期待 0）。"
            "引き算の借りをそのまま上限にすると $FF** のような巨大な値になり、"
            "カメラがステージの外まで動く" % (narrow, SCREEN_W, narrow_limit))
    r.check("ステージ幅を戻すと上限も戻る", _cam_limit(nes, labels) == limit,
            "cam_limit=%d（期待 %d）。cam_set_stage_width が呼ぶたびに違う結果を出している"
            % (_cam_limit(nes, labels), limit))


# ---------------------------------------------------------------- 可視33列
def _check_visible_columns(nes, labels, oracle, state, r):
    """可視 33 列のネームテーブルと属性テーブルが、生成器の出力と一致すること。

    左右どちらに動かしても、ネームテーブルの巻き取り（64列）をまたいでも成り立つこと。
    """
    r.section("可視 %d 列のネームテーブルと属性テーブル" % VISIBLE_COLS)

    left = state.get("dz_left")
    right = state.get("dz_right")
    if left is None:
        r.check("可視列の検証に不感帯の実測値が要る", False,
                "不感帯の実測に失敗しているので、カメラを狙った位置へ動かせない")
        return

    limit = _cam_limit(nes, labels)
    # カメラは列の境界に乗せない（+3 ドット）。乗せてしまうと 33 列目が見えず、
    # 「32 列しか転送していない」実装でも素通りしてしまう。
    # 目標のうち「右へ少しだけ」は必ず入れること。起動時の一括転送が書くのは
    # ワールド列 0..32 だけで、その先（ネームテーブル1 の残り）は空白のまま残っている。
    # そこを最初に通るときだけ、可視列の数え落としが「空白の列」として素直に見える。
    # 巻き取りを越えたあとは、仮背景のタイル模様が 8 列周期で巻き取り幅 64 を割り切るため、
    # 数え落とした列にも「たまたま同じ絵」が載っていて、タイルだけでは気付けない
    # （属性は 128 列周期なので、そちらで気付ける。bg.s の縞の周期の注記を見よ）。
    positions = [
        ("起動直後（カメラ0）", None),
        ("右へ: 起動時に書いていない列に入る", 20 * 8 + 3),
        ("右へ: ネームテーブル1 に入る", 40 * 8 + 3),
        ("右へ: 巻き取り（%d列）を越える" % NT_WRAP_COLS, 70 * 8 + 3),
        ("左へ戻る: 巻き取りを逆向きにまたぐ", 37 * 8 + 5),
    ]

    for title, target in positions:
        if target is not None:
            if target > limit:
                r.check("%s のためにカメラを動かせる" % title, False,
                        "目標 %d がカメラの上限 %d を超えている。"
                        "P1 の暫定ステージ長が縮んだなら、この検証の目標位置を見直すこと"
                        % (target, limit))
                continue
            frames = _aim(nes, labels, target, left, right)
            if frames is None:
                r.check("%s: カメラの列追跡が追いつく" % title, False,
                        "300 フレーム回してもキューが空にならない／cam_col が cam_x>>3 に "
                        "追いつかない（cam_x=%d, cam_col=%d, 未転送 %d バイト）。"
                        "転送予算より列の出現が速いか、積めなかった列を取りこぼしている"
                        % (_cam_x(nes, labels), _cam_col(nes, labels), _pending(nes, labels)))
                continue

        cam = _cam_x(nes, labels)
        first = cam >> TILE_W_SHIFT
        r.check("%s: cam_col がカメラの列 (cam_x>>3) と一致する" % title,
                _cam_col(nes, labels) == first,
                "cam_x=%d（列 %d）だが cam_col=%d。列追跡が追いついていない＝"
                "見えている列と転送済みの列がずれる"
                % (cam, first, _cam_col(nes, labels)))

        # 画面に収まる 32 列
        inside = [p for p in (_column_problem(nes, oracle, first + k, "画面内 %d 列目" % (k + 1))
                              for k in range(SCREEN_TILES_W)) if p]
        r.check("%s: 画面に収まる %d 列が生成規則と一致する" % (title, SCREEN_TILES_W), not inside,
                "%s（ほか %d 列）。cam_x=%d。転送が落ちた列は、%d 列前（ネームテーブルの"
                "巻き取り幅）の古い絵のまま残る"
                % (inside[0] if inside else "", max(0, len(inside) - 1), cam, NT_WRAP_COLS))

        # 右端にはみ出す 33 列目。ここだけを別の検証にしてあるのは、
        # 可視列を 32 と数える off-by-one が**この1列にしか出ない**ためである。
        edge = _column_problem(nes, oracle, first + SCREEN_TILES_W, "右端にはみ出す %d 列目"
                               % VISIBLE_COLS)
        r.check("%s: 右端にはみ出す %d 列目も転送されている" % (title, VISIBLE_COLS), edge is None,
                "%s。カメラが列の境界に乗っていない限り、画面の右端には %d 列目が "
                "1〜7ドットだけ顔を出す。可視列を %d と数えるとこの1列が更新されず、"
                "スクロールのたびに右端に古い絵（または未転送の空白）が1列ぶん流れる"
                % (edge, VISIBLE_COLS, SCREEN_TILES_W))

        # 属性テーブル。属性1バイトは4列ぶんを受け持つので、属性列ごとに1回だけ見る。
        seen = set()
        attr_bad = []
        for k in range(VISIBLE_COLS):
            col = first + k
            addr = _attr_addr(col)
            if addr in seen:
                continue
            seen.add(addr)
            p = _attr_problem(nes, oracle, col, "画面内 %d 列目" % (k + 1))
            if p:
                attr_bad.append(p)
        r.check("%s: 可視列を覆う属性列 (%d 本) が生成規則と一致する" % (title, len(seen)),
                not attr_bad,
                "%s（ほか %d 本）。属性列はタイル列と別の記録で積まれるので、"
                "キューが尽きたときに属性だけ積み残されやすい。落ちるとタイルの模様と "
                "パレットの縞がずれて流れる" % (attr_bad[0] if attr_bad else "", max(0, len(attr_bad) - 1)))

    # --- カメラが大きく飛んでも、積みきれなかった列が黙って落ちないこと ---
    # 1フレームに転送できるのは1列ぶんなので、飛んだぶんの列は数十フレームかけて追いつく。
    # scroll.s は「積めなかったら cam_col を進めない」ことで取りこぼしを防いでいる。
    target = min(limit, 120 * 8 + 1)
    _place_player(nes, labels, target + right)
    nes.call(labels["cam_update"])              # 追従だけ先に起こす（カメラが一気に飛ぶ）
    jumped = _cam_x(nes, labels)
    frames = _settle_camera(nes, labels)
    first = jumped >> TILE_W_SHIFT
    problems = [p for p in (_column_problem(nes, oracle, first + k, "画面内 %d 列目" % (k + 1))
                            for k in range(VISIBLE_COLS)) if p]
    r.check("カメラが %d 列ぶん飛んでも、追いついたとき可視 %d 列が全て正しい"
            % (jumped >> TILE_W_SHIFT, VISIBLE_COLS),
            frames is not None and not problems,
            "%s（ほか %d 列、%s フレームで追いついた）。キューが一杯で積めなかった列は "
            "cam_col を進めずに次フレームへ持ち越す約束。進めてしまうとその列は "
            "二度と転送されず、画面に古い絵が残り続ける"
            % (problems[0] if problems else "追いつかなかった", max(0, len(problems) - 1), frames))


# ---------------------------------------------------------------- 転送キュー
def _transfer_cost(writes):
    """flush の書き込み列から転送の費用を数える（constants.inc の重み: RUN=1 / STEP=3）。

    記録の形は書き込みの並びから見分けられる。
      RUN  … PPUADDR 2回 → PPUDATA を n 回（n > 1）
      STEP … PPUADDR 2回 → PPUDATA 1回、の繰り返し
    データ1バイトだけの RUN 記録は STEP と区別がつかないので費用 3 と数える（安全側）。
    """
    cost = 0
    i = 0
    while i < len(writes):
        if writes[i][0] != PPUADDR:
            i += 1
            continue
        while i < len(writes) and writes[i][0] == PPUADDR:
            i += 1
        n = 0
        while i < len(writes) and writes[i][0] == PPUDATA:
            n += 1
            i += 1
        cost += n if n > 1 else 3 * n
    return cost


def _queue_record(nes, labels, dst, data, flags=0):
    """記録を1つ積む（open → byte × n → close）。空き不足なら False。"""
    watch = _watch(nes)
    if watch is not None:
        watch.idle("vram_queue_open")
    _set(nes, labels, "vq_dst_lo", dst)
    _set(nes, labels, "vq_dst_hi", dst >> 8)
    if nes.call(labels["vram_queue_open"], a=flags, x=len(data)).carry:
        return False
    for b in data:
        nes.call(labels["vram_queue_byte"], a=b)
    return not nes.call(labels["vram_queue_close"]).carry


def _engine_record_len(nes, labels):
    """engine が実際に積む記録（ネームテーブル1列）の長さを実測する。

    「この長さなら engine 自身が毎フレーム積んでいる」＝**必ず受理される長さ**であり、
    費用の予算を測る物差しに使ってよい唯一の長さである（VQ_MAX_LEN を焼き付けずに済む）。
    """
    nes.call(labels["vram_queue_reset"])
    _set16(nes, labels, "bg_col_lo", "bg_col_hi", 8)
    if nes.call(labels["bg_queue_column"]).carry:
        return None
    length = _record_header(nes, labels, _get(nes, labels, "vq_head"))[0]
    nes.call(labels["vram_queue_reset"])
    return length or None


def _measure_header_len(nes, labels):
    """記録のヘッダ長（VQ_HDR）を実測する。open の直後の vq_wr - vq_tail がそれである。"""
    nes.call(labels["vram_queue_reset"])
    _set(nes, labels, "vq_dst_lo", VQ_SCRATCH & 0xFF)
    _set(nes, labels, "vq_dst_hi", VQ_SCRATCH >> 8)
    if nes.call(labels["vram_queue_open"], a=0, x=4).carry:
        return None
    hdr = (_get(nes, labels, "vq_wr") - _get(nes, labels, "vq_tail")) & 0xFF
    nes.call(labels["vram_queue_reset"])
    return hdr or None


def attach_queue_watch(nes, labels, state):
    """記録の形を実測して QueueWatch を取り付ける。l2_camera からも使う。

    測るのは「ヘッダ長」と「open が受理する記録長の上限」だけで、どちらも ROM に聞く。
    上限の検査がまだ入っていない engine では上限が測れないので、そのときは
    リングの容量（これ以上は空き不足で積めない）を上限として使う。
    engine-dev が `len <= VQ_MAX_LEN` を入れれば、この値は自動的に厳しくなる。
    """
    hdr = _measure_header_len(nes, labels)
    if hdr is None:
        return None
    max_len, source = _measure_max_len(nes, labels, hdr)
    if not max_len:
        max_len = VQ_SIZE - 1 - hdr
        source = "%s → リングの容量 %d を上限として使う" % (source, max_len)
    watch = QueueWatch(nes, labels, hdr, max_len, source)
    nes.vq_watch = watch
    state["hdr"] = hdr
    state["max_len"] = max_len
    state["max_len_source"] = source
    return watch


def _measure_max_len(nes, labels, hdr):
    """open が受理する記録長の上限（VQ_MAX_LEN）を実測する。

    戻り: (上限, 出どころ)。上限を検査していない（リングの容量まで何でも受理する）なら
    (None, 理由)。上限の検査は engine-dev がこれから入れるところなので、
    **入っても入っていなくても意味を持つ形**で測る。
    """
    cap = VQ_SIZE - 1 - hdr          # 空のリングに積める最大長（これ以上は空き不足）

    def accepts(n):
        nes.call(labels["vram_queue_reset"])
        _set(nes, labels, "vq_dst_lo", VQ_SCRATCH & 0xFF)
        _set(nes, labels, "vq_dst_hi", VQ_SCRATCH >> 8)
        carry = nes.call(labels["vram_queue_open"], a=0, x=n).carry
        nes.call(labels["vram_queue_reset"])       # 開きっぱなしにしない
        return not carry

    if accepts(cap):
        return None, ("open が長さ %d の記録まで受理する（上限の検査が無い）" % cap)
    lo, hi = 1, cap                  # lo は受理される、hi は突き返される
    if not accepts(lo):
        return 0, "open が長さ1の記録も受理しない"
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if accepts(mid):
            lo = mid
        else:
            hi = mid
    return lo, "open が突き返す境界の実測値"


def _measure_cost_budget(nes, labels, base_len):
    """1フレームの転送予算（VQ_COST_BUDGET）を実測する。

    予算は主が調整するノブなので、値をテストに焼き付けず ROM に聞く。
    RUN 形式は1バイトあたり費用1なので、「1回の flush で流しきれる RUN 記録の
    バイト数の合計」がそのまま予算である。

    **長さは base_len（engine が実際に積む記録の長さ）を超えない範囲でしか振らない。**
    以前はここで 200 バイトの記録をわざと積んでいたが、その作りだと
    engine が `len <= VQ_MAX_LEN` の上限検査を入れた瞬間に
    「突き返された（何も積まれていない）」と「予算で刻まれた（全部流れた）」が
    どちらも `_pending() == 0` になって区別できなくなる。
    そのせいで engine-dev は上限検査を入れられなかった。刻みは**複数の記録**で測る。

    戻り: (予算, 測り方の説明)。測れなければ (None, 理由)。
    """
    def flows(lengths):
        """その長さの RUN 記録を順に積み、flush 1回で全部流れるか。積めなければ None。"""
        nes.call(labels["vram_queue_reset"])
        for i, n in enumerate(lengths):
            if not _queue_record(nes, labels, VQ_SCRATCH + i * 0x40, [0] * n):
                return None
        _flush(nes, labels)
        return _pending(nes, labels) == 0

    def largest(probe, lo, hi):
        """probe(n) が True になる最大の n（lo..hi）。lo でも False なら None。"""
        if not probe(lo):
            return None
        if probe(hi):
            return hi
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if probe(mid):
                lo = mid
            else:
                hi = mid
        return lo

    # まず1件で測る。予算が base_len より小さければこれで決まる。
    single = largest(lambda n: flows([n]) is True, 1, base_len)
    if single is None:
        return None, "長さ1の RUN 記録すら flush 1回で流れない"
    if single < base_len:
        return single, "記録1件（%d バイト）で刻まれた" % (single + 1)

    # 1件では刻まれない＝予算 >= base_len。2件目を足して境界を探す。
    # 1フレームの記録数の上限（VQ_RECORDS_PER_FRAME）が1だと、この測り方は
    # 費用ではなく記録数を測ってしまう。先にそこを切り分ける。
    if flows([1, 1]) is not True:
        return None, ("長さ1の記録2件が1回の flush で流れない＝1フレームに記録を1件しか"
                      "処理しない（VQ_RECORDS_PER_FRAME=1）。費用の予算を記録数と分けて"
                      "測れないので、測り方を見直すこと")
    second = largest(lambda n: flows([base_len, n]) is True, 1, base_len)
    if second is None:
        return base_len, "記録2件（%d + 1 バイト）で刻まれた" % base_len
    if second >= base_len:
        return None, ("%d バイトの記録を2件（費用 %d）積んでも刻まれない。"
                      "記録数の上限に隠れて費用の境界が見えない"
                      % (base_len, base_len * 2))
    return base_len + second, ("記録2件（%d + %d バイト = 費用 %d、もう1バイト足すと"
                               "刻まれる）" % (base_len, second, base_len + second))


def _check_queue(nes, labels, oracle, state, r):
    r.section("VRAM 転送キュー（予算・巻き取り・公開の境界）")

    budget = state.get("budget")
    how = state.get("budget_how", "測っていない")
    ok = r.check("1フレームの転送が費用の予算で刻まれている", budget is not None,
                 "%s。予算で刻んでいない＝積まれただけ VBlank 中に流すということで、"
                 "溢れたぶんが描画期間に食い込んで画面が壊れる（vram.s の vq_cost）。"
                 "※ 刻みは engine が実際に積む記録の長さ (%s バイト) 以下の記録を"
                 "複数積んで測っている。VQ_MAX_LEN を越える記録を積んで測ると、"
                 "engine が上限検査を入れた日に「突き返された」と「全部流れた」が"
                 "区別できなくなる" % (how, state.get("base_len")))
    if not ok:
        return
    r.check("実測した1フレームの転送予算 = %d（RUN 形式のバイト数の合計。%s）"
            % (budget, how),
            budget > 0, "予算が %d。1バイトも転送できない" % budget)

    # --- 背景が積む記録が、1フレームの予算に「丸ごと」収まること ---
    # vram.s は記録を途中で切らない。したがって1記録の費用が1フレームの予算を超えると、
    # その記録は**永久に処理されない**（キューが先頭で詰まり、以降の転送が全部止まる）。
    # 予算の値そのものを期待しない形で、engine が実際に積む記録で確かめる。
    jam = []
    for what, queue_label in (("ネームテーブル1列", "bg_queue_column"),
                              ("属性1列", "bg_queue_attr")):
        if queue_label not in labels:
            continue
        nes.call(labels["vram_queue_reset"])
        _set16(nes, labels, "bg_col_lo", "bg_col_hi", 8)
        if nes.call(labels[queue_label]).carry:
            jam.append("%s を空のキューに積めなかった" % what)
            continue
        _flush(nes, labels)
        left = _pending(nes, labels)
        if left:
            jam.append("%s が flush 1回で流れきらなかった（%d バイト残った）" % (what, left))
    r.check("背景が積む記録が1フレームの予算に丸ごと収まる", not jam,
            "%s。vram.s は記録を途中で切らないので、1記録の費用が1フレームの予算 (%d) を "
            "超えると**その記録は永久に転送されず**、キューが先頭で詰まって以降の "
            "スクロールが全部止まる（constants.inc の .assert と同じ主張を実物で見ている）"
            % ("／".join(jam), budget))

    # --- 未確定の記録（vq_tail を進める前）は転送されないこと ---
    # メインループが記録を組み立てている最中に NMI が割り込むのが現実の並びである。
    # ここで半端な記録が流れると、VRAM の知らない番地に途中までのデータが書かれる。
    nes.call(labels["vram_queue_reset"])
    data = [0xA0 + i for i in range(8)]
    dst = VQ_SCRATCH + 0x40
    for a in range(len(data)):
        nes.ppu.vram[dst + a] = 0x00
    _set(nes, labels, "vq_dst_lo", dst)
    _set(nes, labels, "vq_dst_hi", dst >> 8)
    nes.call(labels["vram_queue_open"], a=0, x=len(data))
    for b in data:
        nes.call(labels["vram_queue_byte"], a=b)
    # ここでは close していない（vq_tail はまだ動いていない）
    head_before = _get(nes, labels, "vq_head")
    pending_before = _pending(nes, labels)
    _flush(nes, labels)
    leaked = [i for i in range(len(data)) if nes.ppu.vram[dst + i] != 0]
    r.check("確定していない記録（vq_tail を進める前）は転送されない",
            pending_before == 0 and not leaked and _get(nes, labels, "vq_head") == head_before,
            "open + データ書き込みだけで close していない記録が転送された "
            "（未処理 %d バイト、$%04X から %d バイトが書き換わり、vq_head %d → %d）。"
            "記録が見えるようになる境界は vq_tail の1バイト書き込みだけである。"
            "組み立て途中の記録が NMI から見えると、VBlank に半端なデータが流れて画面が壊れる"
            % (pending_before, dst, len(leaked), head_before, _get(nes, labels, "vq_head")))

    nes.call(labels["vram_queue_close"])
    _flush(nes, labels)
    got = [nes.ppu.vram[dst + i] for i in range(len(data))]
    r.check("close した記録は次の転送で VRAM に届く", got == data,
            "$%04X から %s（期待 %s）。close が vq_tail を進めていないか、"
            "転送先アドレスの上位/下位が入れ替わっている" % (dst, got, data))

    # --- リングの巻き取り ---
    # リングは 256 バイト。添字の 8bit 巻き取りに乗せてあるので、
    # 巻き取り位置をまたぐ記録でも中身が化けないことを確かめる。
    nes.call(labels["vram_queue_reset"])
    wrapped = False
    diag0 = _diag(nes, labels)
    prev_tail = _get(nes, labels, "vq_tail")
    bad = []
    total = 0
    for i in range(20):
        payload = [(i * 19 + k) & 0xFF for k in range(20)]
        dst = VQ_SCRATCH + 0x80 + i * 24
        if not _queue_record(nes, labels, dst, payload):
            bad.append("記録%d を積めなかった（空き不足か、close が突き返した。"
                       "%s）" % (i, "／".join(_diag_delta(diag0, _diag(nes, labels))) or
                                "診断カウンタは増えていない＝空き不足"))
            break
        tail = _get(nes, labels, "vq_tail")
        if tail < prev_tail:
            wrapped = True
        prev_tail = tail
        total += len(payload) + 4
        _flush(nes, labels)
        got = [nes.ppu.vram[dst + k] for k in range(len(payload))]
        if got != payload:
            bad.append("記録%d（$%04X）が %s、期待 %s" % (i, dst, got[:4], payload[:4]))
    r.check("リングバッファが巻き取っても記録が化けない（%d バイト積んだ）" % total,
            wrapped and not bad,
            "%s。リングは 256 バイトで、添字の 8bit 巻き取りに任せてある。"
            "巻き取りをまたぐ記録が壊れると、VRAM の関係ない番地にデータが書かれる"
            % ("／".join(bad) if bad else "256 バイトを越えても vq_tail が一度も巻き取らなかった"))

    r.check("巻き取りの間に取りこぼし（vq_overflow）が起きていない",
            _get(nes, labels, "vq_overflow") == 0,
            "vq_overflow=%d。1記録ずつ積んで毎回転送しているのに空き不足が起きた＝"
            "使用量の計算（vq_tail - vq_head）が巻き取りで狂っている"
            % _get(nes, labels, "vq_overflow"))


# ---------------------------------------------------------------- close の番人
def _queue_state(nes, labels):
    """キューの見える状態をひとまとめに読む（記録の公開に関わる量だけ）。"""
    s = dict(tail=_get(nes, labels, "vq_tail"), wr=_get(nes, labels, "vq_wr"),
             head=_get(nes, labels, "vq_head"), pending=_pending(nes, labels))
    s.update(_diag(nes, labels))
    return s


def _close_case(nes, labels, declared, written):
    """open(len=declared) → written バイト書く → close。前後の状態を返す。

    先に確定済みの記録を1つ積んでおく。突き返されたときに「既に確定している記録まで
    巻き込んで壊していないか」を見たいためである。
    """
    watch = _watch(nes)
    nes.call(labels["vram_queue_reset"])
    _queue_record(nes, labels, VQ_SCRATCH, [0x11, 0x22, 0x33, 0x44])
    before = _queue_state(nes, labels)
    dst = VQ_SCRATCH + 0x40
    if watch is not None:
        watch.idle("vram_queue_open")
    _set(nes, labels, "vq_dst_lo", dst)
    _set(nes, labels, "vq_dst_hi", dst >> 8)
    opened = not nes.call(labels["vram_queue_open"], a=0, x=declared).carry
    for i in range(written):
        nes.call(labels["vram_queue_byte"], a=(0xC0 + i) & 0xFF)
    carry = nes.call(labels["vram_queue_close"]).carry if opened else None
    return dict(declared=declared, written=written, opened=opened, carry=carry,
                before=before, after=_queue_state(nes, labels))


def _check_close_guard(nes, labels, oracle, state, r):
    """close が「open に申告した長さちょうど書いたか」を確かめていること。

    ここは (a) の記録の連なりを**単体で**縛る節である。連なりが壊れる唯一の入口が
    close であり（tail を進める命令はここにしかない）、申告と実際のずれをここで
    止めなければ、以降 vq_head は永久に記録の途中を指す。
    """
    r.section("vram_queue_close の番人（申告した長さと書いたバイト数の一致）")

    hdr = state["hdr"]
    good = _close_case(nes, labels, 8, 8)
    r.check("申告どおりに書いた記録は close で確定する（対照）",
            good["opened"] and good["carry"] is False
            and good["after"]["tail"] == (good["before"]["tail"] + hdr + 8) & 0xFF
            and good["after"]["wr"] == good["after"]["tail"]
            and good["after"]["vq_badclose"] == good["before"]["vq_badclose"],
            "長さ8を申告して8バイト書いたのに close が C=%s、vq_tail %d→%d"
            "（期待 %d）、vq_badclose %d→%d。番人が厳しすぎると**正しい記録まで**"
            "通らなくなり、背景が永久に更新されない"
            % (good["carry"], good["before"]["tail"], good["after"]["tail"],
               (good["before"]["tail"] + hdr + 8) & 0xFF,
               good["before"]["vq_badclose"], good["after"]["vq_badclose"]))

    # --- 申告と実際が食い違う記録は公開しない ---
    # これが今回の実バグの正体である。30 と申告した列に 56 バイト書かれ、close が
    # そのまま vq_tail を進めたため、記録の連なりに 26 バイトの穴が開いた。
    bad = []
    for declared, written in ((8, 7), (8, 9)):
        res = _close_case(nes, labels, declared, written)
        b, a = res["before"], res["after"]
        why = []
        if not res["opened"]:
            why.append("open が突き返した（この試験は open ではなく close を見るもの）")
        if res["carry"] is not True:
            why.append("close が C=%s（期待 C=1 = 突き返し）" % res["carry"])
        if a["tail"] != b["tail"]:
            why.append("vq_tail が %d→%d と動いた（公開してしまった）" % (b["tail"], a["tail"]))
        if a["wr"] != a["tail"]:
            why.append("vq_wr=%d が vq_tail=%d に戻っていない（次の open が続きから書く）"
                       % (a["wr"], a["tail"]))
        if a["pending"] != b["pending"]:
            why.append("未処理バイト数が %d→%d と増えた" % (b["pending"], a["pending"]))
        if a["vq_badclose"] != (b["vq_badclose"] + 1) & 0xFF:
            why.append("vq_badclose が %d→%d（期待 +1）" % (b["vq_badclose"], a["vq_badclose"]))
        if a["vq_badlen"] != b["vq_badlen"] or a["vq_badstep"] != b["vq_badstep"]:
            why.append("別のカウンタが動いた（vq_badlen %d→%d / vq_badstep %d→%d）"
                       % (b["vq_badlen"], a["vq_badlen"], b["vq_badstep"], a["vq_badstep"]))
        if why:
            bad.append("長さ%d を申告して %d バイト書いた場合: %s"
                       % (declared, written, " / ".join(why)))

    r.check("申告した長さと書いたバイト数が違う記録は close が突き返す（過不足の両方）",
            not bad,
            "%s（ほか %d 通り）。close は vq_tail を進める**唯一の命令**であり、"
            "ここを通ると記録は NMI から見える。申告と実際がずれたまま公開すると、"
            "その差ぶんだけ記録の連なりに穴が開き、以降 vq_head は永久にデータの途中を指す。"
            "NMI はそこを長さ・アドレス・フラグとして読む（ネームテーブルの空白タイルなら"
            "長さ0＝$2007 に 256 バイト）。**症状（VBlank をはみ出す）と原因（バイト数の"
            "数え違い）が遠いので、ここで止めて数える**"
            % (bad[0] if bad else "", max(0, len(bad) - 1)))

    # --- 二重 close ---
    # 確定後は vq_wr == vq_tail なので、同じ検査で距離0として落ちる。
    nes.call(labels["vram_queue_reset"])
    _queue_record(nes, labels, VQ_SCRATCH, [0x55] * 6)
    before = _queue_state(nes, labels)
    carry = nes.call(labels["vram_queue_close"]).carry
    after = _queue_state(nes, labels)
    r.check("確定済みの記録にもう一度 close を呼んでも、中身のない記録が生えない",
            carry is True and after["tail"] == before["tail"]
            and after["vq_badclose"] == (before["vq_badclose"] + 1) & 0xFF,
            "2回目の close が C=%s、vq_tail %d→%d、vq_badclose %d→%d。"
            "2回目が通ると長さ0の記録が確定し、NMI が $2007 に 256 バイト流す"
            % (carry, before["tail"], after["tail"],
               before["vq_badclose"], after["vq_badclose"]))

    # --- 長さ0の open ---
    nes.call(labels["vram_queue_reset"])
    _queue_record(nes, labels, VQ_SCRATCH, [0x77] * 5)
    before = _queue_state(nes, labels)
    tail_bytes = _record_header(nes, labels, before["tail"])
    watch = _watch(nes)
    if watch is not None:
        watch.idle("vram_queue_open(len=0)")
    _set(nes, labels, "vq_dst_lo", (VQ_SCRATCH + 0x40) & 0xFF)
    _set(nes, labels, "vq_dst_hi", (VQ_SCRATCH + 0x40) >> 8)
    zero = nes.call(labels["vram_queue_open"], a=0, x=0)
    after = _queue_state(nes, labels)
    why = []
    if not zero.carry:
        why.append("open が C=0（受理した）")
    if after["vq_badlen"] != (before["vq_badlen"] + 1) & 0xFF:
        why.append("vq_badlen が %d→%d（期待 +1）" % (before["vq_badlen"], after["vq_badlen"]))
    if after["vq_badclose"] != before["vq_badclose"]:
        why.append("vq_badclose まで動いた %d→%d（理由の違うカウンタを混ぜている）"
                   % (before["vq_badclose"], after["vq_badclose"]))
    for name in ("tail", "wr", "head", "pending"):
        if after[name] != before[name]:
            why.append("vq_%s が %d→%d" % (name, before[name], after[name]))
    if _record_header(nes, labels, before["tail"]) != tail_bytes:
        why.append("キューの末尾 %s が %s に書き換わった"
                   % (tail_bytes, _record_header(nes, labels, before["tail"])))
    r.check("長さ0の open は突き返され、バッファが1バイトも変わらない", not why,
            "%s。長さ0の記録が積まれると、flush の `ldy vq_n` が 0 のまま 256 回まわり、"
            "$2007 に 256 バイト流れる（費用の計算上は 0 なので予算では止まらない）。"
            "転送先はその記録のヘッダが指す先なので、どこが壊れるかは運任せになる"
            % "／".join(why))

    nes.call(labels["vram_queue_reset"])            # 次のセクションに汚れを持ち越さない


# ---------------------------------------------------------------- NMI
def _check_nmi_writes(nes, labels, oracle, state, r):
    """NMI の書き込み順と、転送が1フレームの予算を超えないこと。

    ここだけは実フレームを回す。順序も予算も「NMI ハンドラが1回で何をするか」の話なので、
    ハンドラを単体で走らせて（trace_nmi）その書き込みを見る。
    """
    r.section("NMI の書き込み（順序と1フレームの転送量）")

    budget = state.get("budget")
    left = state.get("dz_left", 96)
    right = state.get("dz_right", 160)
    limit = _cam_limit(nes, labels)

    over_budget = []
    over_vblank = []
    busy_frames = 0
    order_bad = []
    scroll_bad = []
    moved_during = []

    # 転送すべきものが溜まった状態を作る。カメラを左に寄せてから操作キャラを一気に
    # 右へ飛ばすと、カメラは1フレームで追いつき、列の転送だけが数十フレーム続く。
    # ネームテーブル選択ビットは cam_x の bit8 なので、その値が 0 になる位置と
    # 1 になる位置の両方で見る（載せ忘れは片方でしか出ない）。
    runs = [("カメラ $%04X（NT ビット 0）" % 0x0200, 0x0000, 0x0200),
            ("カメラ $%04X（NT ビット 1）" % 0x0300, 0x0200, 0x0300)]

    nes.set_buttons(set())
    for title, start, target in runs:
        if target > limit:
            r.check("%s まで動かせる" % title, False,
                    "目標 %d がカメラの上限 %d を超えている。P1 の暫定ステージ長が縮んだなら "
                    "この検証の観測位置を見直すこと" % (target, limit))
            continue
        # 前の周回で実フレームを回しているので、キューに触る前に待ちループへ寄せる。
        # 寄せないと、メインループが bg_queue_column の内側（open 済み・close 前）で
        # 止まっている状態のまま _aim が cam_update を呼び、実機には無い再入になる。
        _park(nes, labels)
        _aim(nes, labels, start, left, right)          # 出発点へ（ここはフレームを回さない）
        _place_player(nes, labels, target + right)
        nes.run_frames(2)                              # カメラが飛び、列の転送が始まる
        cam = _cam_x(nes, labels)

        watch = _watch(nes)
        nes.trace_nmi = True
        try:
            for _ in range(20):
                nes.run_frames(1)
                if watch is not None:
                    watch.chain("実フレーム（メインが積み、NMI が流したあと）")
                writes = list(nes.nmi_writes)
                data_writes = [i for i, (a, _) in enumerate(writes) if a in (PPUADDR, PPUDATA)]
                if not data_writes:
                    continue
                busy_frames += 1

                cost = _transfer_cost(writes)
                if budget is not None and cost > budget:
                    over_budget.append("%d（予算 %d）" % (cost, budget))
                if nes.nmi_cycles > VBLANK_CYCLES:
                    over_vblank.append(nes.nmi_cycles)

                # --- 順序: $2007 の転送が終わってからスクロールを置き直すこと ---
                last_data = data_writes[-1]
                scroll_writes = [i for i, (a, _) in enumerate(writes) if a == PPUSCROLL]
                ctrl_writes = [i for i, (a, _) in enumerate(writes) if a == PPUCTRL]
                if len(scroll_writes) < 2 or scroll_writes[0] < last_data:
                    order_bad.append("$2005 の書き込みが %s 番目、最後の VRAM 転送が %d 番目"
                                     % (scroll_writes or "無し", last_data))
                    continue
                if not ctrl_writes or ctrl_writes[-1] < last_data:
                    order_bad.append("最後の $2000 が %s 番目、最後の VRAM 転送が %d 番目"
                                     % (ctrl_writes[-1] if ctrl_writes else "無し", last_data))
                    continue

                # --- 値: 横スクロールは cam_x の下位、ネームテーブル選択は cam_x の bit8 ---
                sx = writes[scroll_writes[0]][1]
                sy = writes[scroll_writes[1]][1]
                nt = writes[ctrl_writes[-1]][1] & CTRL_NT_X
                want_nt = (cam >> 8) & CTRL_NT_X
                if sx != (cam & 0xFF) or sy != 0 or nt != want_nt:
                    scroll_bad.append("%s: cam_x=%d のとき $2005=(%d, %d)・$2000 の NT ビット=%d"
                                      "（期待 (%d, 0)・NT ビット %d）"
                                      % (title, cam, sx, sy, nt, cam & 0xFF, want_nt))
        finally:
            nes.trace_nmi = False
            _park(nes, labels)                         # 次に触る前に待ちループへ寄せ直す

        # 観測中にカメラが動いていたら、上の値の比較は前提から崩れている。
        if _cam_x(nes, labels) != cam:
            moved_during.append("%s: 観測中に cam_x が %d → %d と動いた"
                                % (title, cam, _cam_x(nes, labels)))

    r.check("スクロール設定を観測する間、カメラが静止していた", not moved_during,
            "%s。この検証は「止まっているカメラの値が NMI に正しく渡るか」を見るものなので、"
            "動いていると比較の前提が崩れる（検証の足場の問題であって engine の不具合ではない）"
            % "／".join(moved_during))

    ok = r.check("転送すべきものがある状態の NMI を観測できた", busy_frames > 0,
                 "40 フレーム回しても NMI が VRAM を1回も触らなかった。"
                 "スクロールで列が積まれていない（カメラが動いていないか、キューが詰まっている）")
    if not ok:
        return

    r.check("NMI が VRAM 転送を終えてからスクロールを置き直す（%d フレーム観測）" % busy_frames,
            not order_bad,
            "%s（ほか %d フレーム）。$2006 への書き込みは PPU の内部アドレスを壊すので、"
            "$2005/$2000 でスクロールを置き直すのは必ず転送の**後**でなければならない。"
            "逆にすると、転送した列の位置ぶんだけ画面が横にずれる"
            % (order_bad[0] if order_bad else "", max(0, len(order_bad) - 1)))

    r.check("NMI のスクロール設定が cam_x と一致する（下位＝横スクロール / bit8＝ネームテーブル）",
            not scroll_bad,
            "%s（ほか %d フレーム）。ネームテーブル選択ビットを載せ忘れると、"
            "カメラが 256 ドット進むたびに画面が1画面ぶん飛ぶ"
            % (scroll_bad[0] if scroll_bad else "", max(0, len(scroll_bad) - 1)))

    r.check("1フレームの VRAM 転送が予算 (%s) を超えない" % budget, not over_budget,
            "費用 %s を使ったフレームがある（%d フレーム）。予算を超えて流すと "
            "VBlank をはみ出し、描画期間に食い込んで画面が壊れる。"
            "※ 費用は $2006/$2007 の並びから数えている（RUN=1 / STEP=3、"
            "1バイトの RUN は STEP と区別できないので 3 と数える安全側の見積り）"
            % ("、".join(over_budget[:3]), len(over_budget)))

    r.check("転送が混んでいるフレームでも NMI が VBlank 予算（約%d サイクル）に収まる"
            % VBLANK_CYCLES, not over_vblank,
            "NMI が %s サイクル（%d フレーム）。OAM DMA の 513 サイクルを含めて "
            "VBlank に収まらなければ、はみ出したぶんが描画期間に出る。"
            "※ 命令単位の近似。正確なタイミングは Mesen2 で見ること（ADR-0004）"
            % (over_vblank[:3], len(over_vblank)))


# ---------------------------------------------------------------- 全 NMI の転送量
def check_nmi_write_budget(budget, r):
    """**全テストの全 NMI**で、1回の NMI が $2007 に書いたバイト数が予算以下であること。

    run_tests.py が最後に1回だけ呼ぶ。数えているのは harness（nes.py の NMI_WRITE_WATCH）で、
    L1 から L3 の手前まで、どの検証が回した NMI も等しく数に入っている。

    ここを抜き取り（「混んでいるフレーム」を 20 フレームだけ見る）にしていたのが
    今回の見落としである。記録の連なりが1バイトずれて「長さ0のヘッダ = 256 バイト書き込み」に
    なる壊れ方は、観測する位相を変えないと出ない。全 NMI を数えれば位相は関係なくなる。

    書き込み**回数**と費用は別物だが（STEP 形式は1バイトあたり費用3）、回数 <= 費用 なので
    「回数 <= 予算」は常に成り立っていなければならない。破れていたら費用の計算以前に、
    NMI が記録の境界を見失っている。
    """
    watch = NMI_WRITE_WATCH
    r.section("全 NMI の転送量（$2007 の書き込み回数）")
    if budget is None:
        r.check("1フレームの転送予算を実測できている（全 NMI の検査に要る）", False,
                "予算が測れていないので、全 NMI の転送量を比べる相手が無い")
        return
    where = watch["max_where"]
    r.check("全テストの全 NMI で $2007 の書き込みが予算 %d 以下（%d 回の NMI を観測、最大 %d）"
            % (budget, watch["nmis"], watch["max"]),
            watch["nmis"] > 0 and watch["max"] <= budget,
            "%s。1回の NMI が $2007 に %d バイト書いた（そのとき %d サイクル / %s）。"
            "予算 %d を超える転送は VBlank をはみ出して描画期間に食い込む。"
            "回数が予算を大きく超えているなら、費用の計算の誤りではなく"
            "**記録の連なりがずれて長さ0のヘッダを読んでいる**ことを先に疑うこと"
            % ("NMI を1回も観測していない（検証が空振りしている）" if not watch["nmis"]
               else "予算超過", watch["max"], watch["max_cycles"],
               "%s のフレーム %d" % where if where else "位置不明", budget))
