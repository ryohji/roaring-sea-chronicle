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
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "harness"))

from cpu6502 import CpuCrash                       # noqa: E402
from nes import Nes, VBLANK_CYCLES, boot           # noqa: E402

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

NEEDED = (
    "cam_update", "cam_follow_player", "cam_set_stage_width",
    "cam_x_lo", "cam_x_hi", "cam_col_lo", "cam_col_hi",
    "cam_limit_lo", "cam_limit_hi", "stage_w_lo", "stage_w_hi",
    "bg_tile_at", "bg_attr_byte", "bg_col_lo", "bg_col_hi",
    "bg_queue_column", "bg_queue_attr",
    "vram_queue_reset", "vram_queue_open", "vram_queue_byte", "vram_queue_close",
    "vram_queue_flush", "vram_queue_pending", "vq_dst_lo", "vq_dst_hi",
    "vq_head", "vq_tail", "vq_overflow",
    "ent_x_lo", "ent_x_hi", "ppu_ctrl_shadow",
)


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
    """1フレームぶんの「カメラ更新 → 転送」を進める（実フレームは回さない）。"""
    nes.call(labels["cam_update"])
    return _flush(nes, labels)


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

    oracle = BgOracle(nes, labels)
    state = {}
    for name, fn in (("カメラ追従", _check_deadzone),
                     ("ステージ端のクランプ", _check_clamp),
                     ("可視列の転送", _check_visible_columns),
                     ("転送キュー", _check_queue),
                     ("NMI の書き込み順", _check_nmi_writes)):
        try:
            fn(nes, labels, oracle, state, r)
        except CpuCrash as e:
            r.check("%s の検証中にクラッシュしない" % name, False, str(e))
        except Exception as e:      # noqa: BLE001 — 検証が例外で死ぬと原因が読めなくなる
            r.check("%s の検証が最後まで走る" % name, False,
                    "検証コードが %s で止まった: %s。engine の値が想定の範囲を外れている"
                    % (type(e).__name__, e))


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
    _set(nes, labels, "vq_dst_lo", dst)
    _set(nes, labels, "vq_dst_hi", dst >> 8)
    if nes.call(labels["vram_queue_open"], a=flags, x=len(data)).carry:
        return False
    for b in data:
        nes.call(labels["vram_queue_byte"], a=b)
    nes.call(labels["vram_queue_close"])
    return True


def _measure_cost_budget(nes, labels, probe_max=200):
    """1フレームの転送予算（VQ_COST_BUDGET）を実測する。

    RUN 形式は1バイトあたり費用1なので、「flush 1回で丸ごと転送しきれる最長の RUN 記録」が
    そのまま予算である。予算は主が調整するノブなので、値をテストに焼き付けず ROM に聞く。
    戻り値: 予算（バイト）。probe_max バイトでも刻まれないなら None（＝予算で刻んでいない）。
    """
    def fits(n):
        nes.call(labels["vram_queue_reset"])
        _queue_record(nes, labels, VQ_SCRATCH, [0] * n)
        _flush(nes, labels)
        return _pending(nes, labels) == 0

    if fits(probe_max):
        return None
    lo, hi = 1, probe_max            # lo は転送できる、hi は転送できない
    if not fits(lo):
        return 0
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid
    return lo


def _check_queue(nes, labels, oracle, state, r):
    r.section("VRAM 転送キュー（予算・巻き取り・公開の境界）")

    budget = _measure_cost_budget(nes, labels)
    ok = r.check("1フレームの転送が費用の予算で刻まれている", budget is not None,
                 "200 バイトの記録を積んで flush を1回呼んだら全部転送された。"
                 "予算で刻んでいない＝積まれただけ VBlank 中に流すということで、"
                 "溢れたぶんが描画期間に食い込んで画面が壊れる（vram.s の vq_cost）")
    if not ok:
        return
    state["budget"] = budget
    r.check("実測した1フレームの転送予算 = %d（RUN 形式のバイト数）" % budget,
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
    prev_tail = _get(nes, labels, "vq_tail")
    bad = []
    total = 0
    for i in range(20):
        payload = [(i * 19 + k) & 0xFF for k in range(20)]
        dst = VQ_SCRATCH + 0x80 + i * 24
        if not _queue_record(nes, labels, dst, payload):
            bad.append("記録%d を積めなかった（空き不足）" % i)
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
        _aim(nes, labels, start, left, right)          # 出発点へ（ここはフレームを回さない）
        _place_player(nes, labels, target + right)
        nes.run_frames(2)                              # カメラが飛び、列の転送が始まる
        cam = _cam_x(nes, labels)

        nes.trace_nmi = True
        try:
            for _ in range(20):
                nes.run_frames(1)
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
