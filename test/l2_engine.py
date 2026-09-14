"""L2 実行検証 — engine P1 前半（4レーン座標系 / エンティティテーブル / OAM 割当 / 当たり判定）。

run_tests.py から呼ばれる。層の分け方は ADR-0004 に従う。

ここで検証してよいのは「値」であって「タイミング」ではない。
1スキャンライン8スプライト制約によるちらつき、MMC3 IRQ の分割位置、
スプライト0ヒットのようなスキャンライン単位の挙動は、この Python エミュレータでは
再現していないので検証できない。それらは L3（Mesen2）と実機に回すこと（ADR-0002 / ADR-0004）。

方針:
  * 期待値は極力 ROM の表そのもの（lane_ground_y / body_width）から読む。
    主がパラメータ（LANE_MOVE_FRAMES やレーンY）を調整したときに、
    仕様が壊れていないのにテストが落ちる、という事態を避けるため。
  * 失敗メッセージは「何が期待と違い、何が壊れている疑いがあるか」を1行で書く。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "harness"))

from cpu6502 import CpuCrash                  # noqa: E402
from nes import (Nes, VBLANK_CYCLES, boot,          # noqa: E402
                 frame_end, step_frame, frame_instructions)
from scene import disarm_enemies                    # noqa: E402

# --- src/constants.inc と対応する値。ここに無いものは ROM かラベルから導出する ---
OAM_SPRITE_MAX = 64          # PPU のハード制約（OAM は 64 エントリ）
OAM_Y_OFFSCREEN = 0xFF
ENT_ACTIVE = 0x80
ENT_INACTIVE = 0x00
ENT_Y_TO_OAM = 17            # SPRITE_H + 1。足元Y → OAM の Y バイト
SPRITE_W = 8
LANE_COUNT = 4               # 4固定。ADR なしに変えてはならない（CLAUDE.md 第5条）
SPR_CLASS_PLAYER = 0
SPR_CLASS_PROJECT = 1
SPR_CLASS_NEAR_ENE = 2
SPR_CLASS_FAR_ENE = 3
SPR_CLASS_EFFECT = 4

ENT_FIELDS = ("ent_active", "ent_x_lo", "ent_x_hi", "ent_y", "ent_lane", "ent_lane_from",
              "ent_lane_step", "ent_lane_acc", "ent_lane_dy", "ent_state", "ent_class",
              "ent_body", "ent_ai", "ent_tile", "ent_attr")

NEEDED = ENT_FIELDS + (
    "oam_build", "oam_sort_order", "oam_order", "oam_used", "oam_dropped", "oam_next",
    "sort_count", "oam_shadow", "cam_x_lo", "cam_x_hi",
    "rect_overlap", "rect_a", "rect_b", "lane_distance", "lane_ground_y", "body_width",
    "wait_nmi")

RECT_X_LO, RECT_X_HI, RECT_Y, RECT_W, RECT_H = 0, 1, 2, 3, 4


# ---------------------------------------------------------------- 足場
class Entities:
    """エンティティテーブル（SoA）への読み書き。配列の長さはラベルから導出する。"""

    def __init__(self, nes, labels):
        self.nes = nes
        self.labels = labels
        self.count = labels["ent_x_lo"] - labels["ent_active"]

    def poke(self, field, i, value):
        self.nes.ram[(self.labels[field] + i) & 0x7FF] = value & 0xFF

    def peek(self, field, i):
        return self.nes.ram[(self.labels[field] + i) & 0x7FF]

    def clear(self):
        for i in range(self.count):
            self.poke("ent_active", i, ENT_INACTIVE)
            self.poke("ent_lane_step", i, 0)

    def place(self, i, x=0, y=196, cls=SPR_CLASS_FAR_ENE, body=0, tile=0, attr=0, lane=0):
        self.poke("ent_active", i, ENT_ACTIVE)
        self.poke("ent_x_lo", i, x & 0xFF)
        self.poke("ent_x_hi", i, (x >> 8) & 0xFF)
        self.poke("ent_y", i, y)
        self.poke("ent_lane", i, lane)
        self.poke("ent_lane_step", i, 0)
        self.poke("ent_class", i, cls)
        self.poke("ent_body", i, body)
        self.poke("ent_tile", i, tile)
        self.poke("ent_attr", i, attr)


def shadow_entry(nes, labels, index):
    """OAM シャドウの index 番目のエントリを (Y, タイル, 属性, X) で返す。"""
    base = (labels["oam_shadow"] + index * 4) & 0x7FF
    return tuple(nes.ram[base + k] for k in range(4))


def build_oam(nes, labels):
    """oam_build を呼び、(使用エントリ数, 捨てた数) を返す。"""
    nes.call(labels["oam_build"])
    return (nes.ram[labels["oam_used"] & 0x7FF],
            nes.ram[labels["oam_dropped"] & 0x7FF])


def set_camera(nes, labels, x):
    nes.ram[labels["cam_x_lo"] & 0x7FF] = x & 0xFF
    nes.ram[labels["cam_x_hi"] & 0x7FF] = (x >> 8) & 0xFF


def tap(nes, labels, button):
    """1フレームだけ押して離し、そのフレームの更新が終わった点で止める。

    run_frames が戻る位置はメインループが1フレームぶんの更新を走らせている**最中**なので、
    そこで止めると、読む相手によって「1フレーム古い値」と「更新の途中の値」の
    どちらにも転ぶ。step_frame() でフレームの更新が終わった点まで進めてから返すことで、
    この直後に読む値を「押した1フレームぶんの更新を終えた状態」に固定する
    （作業変数の読み方は harness/nes.py 冒頭の「観測の作法」を見よ）。

    したがって、この直後の読みは **一様に1フレーム遅れる**こともなければ、
    更新の途中を覗くこともない。ent_lane_step のような作業変数を読んでよい。
    """
    nes.set_buttons({button})
    nes.run_frames(1)
    nes.set_buttons(set())
    frame_end(nes, labels)


def table_length(labels, name):
    """ラベル name から、その次に来るラベルまでの距離を表の項数とみなす。"""
    addr = labels[name]
    after = [a for a in labels.values() if a > addr]
    return min(after) - addr if after else 0


# ---------------------------------------------------------------- 入口
def layer2_engine(rom_path, labels, r):
    print("L2 実行検証 / engine P1 前半（レーン・エンティティ・OAM・当たり判定）")

    missing = [n for n in NEEDED if n not in labels]
    if missing:
        r.check("engine のラベルが build/roaring.labels に揃っている", False,
                "ラベルが無い: %s。.export が消えたか、モジュールがリンクから外れている。"
                "このセクションの検証はラベル無しでは書けないので全部飛ばした" % ", ".join(missing))
        return

    try:
        nes = Nes(rom_path)
        nes.reset()
        # 「起動は N フレームで終わる」をここに書かない。起動が延びたことは
        # run_tests.py の BOOT_FRAMES_MAX が名指しで捕まえる（そこで落ちてほしい）。
        if boot(nes) is None:
            r.check("engine の検証用に起動する", False,
                    "起動しても NMI が来ない（OAM DMA が1回も無い）。"
                    "起動処理の検証を先に見よ")
            return
    except CpuCrash as e:
        r.check("engine の検証用に起動する", False, str(e))
        return

    ents = Entities(nes, labels)
    lane_y = [nes.read(labels["lane_ground_y"] + i) for i in range(LANE_COUNT)]

    if not _check_table_layout(nes, labels, ents, lane_y, r):
        return
    for name, fn in (("レーン移動", _check_lane_interpolation),
                     ("NMI の分担", _check_nmi_budget),
                     ("OAM 並べ替え", _check_sort_order),
                     ("OAM 展開", _check_oam_emit),
                     ("rect_overlap", _check_rect_overlap),
                     ("lane_distance", _check_lane_distance)):
        try:
            if fn in (_check_nmi_budget, _check_rect_overlap, _check_lane_distance):
                fn(nes, labels, r)
            elif fn is _check_oam_emit:
                fn(nes, labels, ents, r)
            else:
                fn(nes, labels, ents, lane_y, r)
        except CpuCrash as e:
            r.check("%s の検証中にクラッシュしない" % name, False, str(e))
        except Exception as e:      # noqa: BLE001 — 検証が例外で死ぬと原因が読めなくなる
            r.check("%s の検証が最後まで走る" % name, False,
                    "検証コードが %s で止まった: %s。engine の値が想定の範囲を外れている "
                    "（テーブルの外を指すレーン番号・体格 ID など）疑いがある"
                    % (type(e).__name__, e))


# ---------------------------------------------------------------- 前提の確認
def _check_table_layout(nes, labels, ents, lane_y, r):
    """以降の検証が寄りかかっている前提（配列長とレーン表）を先に確かめる。"""
    r.section("テーブルの前提")

    strides = {}
    for a, b in zip(ENT_FIELDS, ENT_FIELDS[1:]):
        strides[(a, b)] = labels[b] - labels[a]
    bad = ["%s→%s が %d" % (a, b, d) for (a, b), d in strides.items() if d != ents.count]
    ok = r.check("エンティティ配列が MAX_ENTITIES(%d) ごとに並んでいる" % ents.count, not bad,
                 "%s（期待はすべて %d）。SoA の配列長が揃っていないと "
                 "`lda ent_y, x` の x が別の属性を指す。entity.s の .res の並びを見よ"
                 % ("、".join(bad), ents.count))

    ok &= r.check("レーン数が %d 固定である" % LANE_COUNT, ents.count > 0 and len(lane_y) == LANE_COUNT,
                  "lane_ground_y の項数が %d でない。レーン数の変更は ADR が要る"
                  "（CLAUDE.md 第5条）" % LANE_COUNT)

    ok &= r.check("lane_ground_y が奥→手前で単調増加する %s" % (lane_y,),
                  all(lane_y[i] < lane_y[i + 1] for i in range(LANE_COUNT - 1)),
                  "lane_ground_y=%s。レーン0が最も奥（画面上方）、レーン%d が最も手前という "
                  "前提が崩れると、奥行きの並べ替え（足元Y降順）が前後を逆に描く"
                  % (lane_y, LANE_COUNT - 1))

    ok &= r.check("レーンの足元Yが画面内かつ OAM に変換できる範囲にある",
                  all(ENT_Y_TO_OAM <= y < 0xEF for y in lane_y),
                  "lane_ground_y=%s。%d 未満のレーンは OAM の Y 計算で借りが出て描画されず、"
                  "$EF 以上は画面外" % (lane_y, ENT_Y_TO_OAM))
    return ok


# ---------------------------------------------------------------- レーン補間
def _slide(nes, labels, ents, button, max_frames=90):
    """button を1フレーム押し、レーン補間が終わるまで走らせる。

    戻り値: (各フレームの ent_y の列, かかったフレーム数)。
    LANE_MOVE_FRAMES を期待値に焼き付けない（主が手触りを調整したときに落ちないように）ため、
    「ent_lane_step が 0 に戻るまで」で待つ。

    待ちの条件に使う ent_lane_step も、集める ent_y も、フレームの更新が終わった点
    （step_frame）で読む。生の run_frames の戻り位置で読むと、到着したフレームだけは
    lane_step_one の到着処理の**途中** — dec ent_lane_step で step が 0 になった直後、
    足元Yの吸着と acc/dy の消去がまだ済んでいない点 — で抜けてしまい、
    「移動後の状態」を名乗れない値を持ち帰る（harness/nes.py 冒頭「観測の作法」(b)）。
    """
    samples = [ents.peek("ent_y", 0)]
    tap(nes, labels, button)
    samples.append(ents.peek("ent_y", 0))
    frames = 1
    while ents.peek("ent_lane_step", 0) != 0 and frames < max_frames:
        step_frame(nes, labels)
        samples.append(ents.peek("ent_y", 0))
        frames += 1
    return samples, frames


def _monotone_problem(samples, target):
    """samples が目標へ単調に近づき、行き過ぎずに到達しているかを調べる。問題文字列 or None。"""
    start = samples[0]
    if target == start:
        return None
    step = 1 if target > start else -1
    for i in range(1, len(samples)):
        delta = samples[i] - samples[i - 1]
        if delta * step < 0:
            return ("%d フレーム目で %d → %d と逆戻りした" % (i, samples[i - 1], samples[i]))
        if step > 0 and samples[i] > target:
            return ("%d フレーム目に %d まで進んで目標 %d を行き過ぎた" % (i, samples[i], target))
        if step < 0 and samples[i] < target:
            return ("%d フレーム目に %d まで戻って目標 %d を行き過ぎた" % (i, samples[i], target))
    if samples[-1] != target:
        return ("最後の値が %d で、目標 %d に届いていない" % (samples[-1], target))
    return None


def _check_lane_interpolation(nes, labels, ents, lane_y, r):
    r.section("レーン移動（補間とクランプ）")

    # ここで見たいのは engine の**レーン補間とクランプ**であって、戦闘下の挙動ではない。
    # 敵が殴るようになって以降、UP/DOWN を叩いた瞬間に操作キャラがのけぞり／
    # ヒットストップに居ると入力が通らず、この節は engine が正しいまま落ちる。
    # 期待値を緩めて戦闘下でも通るようにするのは**テストを緩めること**なので、
    # 邪魔している側（敵の攻撃）を取り除いてから駆動する。
    # 「のけぞり中は入力を受け付けない」ことは別の主張として l2_ai.py で見ている。
    # 仲間は残す（仲間が操作キャラのレーンや座標に手を出していたら、ここで落ちてほしい）。
    restore_enemies = disarm_enemies(nes, labels)
    step_frame(nes, labels)          # 取り除いた結果を1フレームぶん落ち着かせる

    start_lane = ents.peek("ent_lane", 0)
    if not 0 <= start_lane < LANE_COUNT - 1:
        r.check("操作キャラ (#0) が起動時に有効なレーンに居る", False,
                "ent_lane=%d。有効なのは 0..%d で、この検証は下へ1つ動ける位置から始める"
                % (start_lane, LANE_COUNT - 1))
        return
    r.check("操作キャラ (#0) が起動時にレーン表どおりの足元Yに居る (レーン%d)" % start_lane,
            ents.peek("ent_y", 0) == lane_y[start_lane],
            "ent_y=%d だが lane_ground_y[%d]=%d。ent_activate がレーンから足元Yを決めていない"
            % (ents.peek("ent_y", 0), start_lane, lane_y[start_lane]))

    # --- DOWN 1回: 手前のレーンへ補間で移動する ---
    samples, frames = _slide(nes, labels, ents, "DOWN")
    target_lane = start_lane + 1
    target_y = lane_y[target_lane]

    r.check("DOWN で目標レーンが %d になる" % target_lane,
            ents.peek("ent_lane", 0) == target_lane,
            "ent_lane=%d（期待 %d）。ent_lane は移動中も「目標」を指す約束"
            % (ents.peek("ent_lane", 0), target_lane))
    r.check("レーン移動が補間される（1フレームで瞬間移動しない）", frames >= 2,
            "%d フレームで着いた（%s）。瞬間移動だと奥行きの移動が見えず、"
            "移動中に前後関係が入れ替わる演出も出ない" % (frames, samples))
    problem = _monotone_problem(samples, target_y)
    r.check("DOWN のレーン移動が単調で、行き過ぎず lane_ground_y[%d]=%d に吸着する"
            % (target_lane, target_y), problem is None,
            "%s。ent_y の推移=%s（%d フレーム）。補間の累算（lane_step_one）か、"
            "到着時の吸着が壊れている" % (problem, samples, frames))
    # 作業変数はフレームの更新が終わった点で読む（_slide がそこで止めてある）。
    # 「更新が終われば片付いている」がエンジンの不変条件であって、
    # 「更新の途中のどの瞬間も片付いている」ではない（到着処理は step → 足元Y → acc → dy の順に
    # 書くので、その最中には必ず半端な状態が存在する）。
    def work():
        return (ents.peek("ent_lane_step", 0), ents.peek("ent_lane_acc", 0),
                ents.peek("ent_lane_dy", 0))

    r.check("レーン移動の完了後に補間の作業変数が片付いている", work() == (0, 0, 0),
            "step=%d acc=%d dy=%d（期待は全て 0）。残っていると次の移動が前回の端数から始まる。"
            "※ この値はフレームの更新が終わった点（step_frame）で読んでいる。"
            "更新の最中で読んでいるなら、それはテスト側の観測点の誤りである" % work())

    # 上の1件は「更新が終わった点」という1箇所で見た主張である。それが位相に依存していない
    # ことまで縛る。静止しているエンティティは lane_update_all に拾われないので、
    # 作業変数はフレームの**どの命令の切れ目**でも 0 のままでなければならない。
    # ここが破れたら、静止中のエンティティの作業変数を誰かが毎フレーム書いている。
    dirty = None
    count = 0
    for _ in frame_instructions(nes, labels):
        count += 1
        if work() != (0, 0, 0):
            dirty = (count, work())
            break
    r.check("静止したエンティティの補間作業変数は、フレーム更新中のどの命令の切れ目でも 0 "
            "（%d 箇所を全数検査）" % count, dirty is None,
            "移動を終えた #0 の作業変数が %s (step, acc, dy)=%s になっている。"
            "更新に入る前から汚れているなら到着処理の片付け漏れ、更新中に汚れたなら "
            "ent_lane_step が 0 のエンティティを lane_update_all が飛ばしていない。"
            "どちらにせよ、作業変数を見る検証はすべて観測点しだいで結論が変わる状態である"
            % ("フレームの更新に入る前から" if dirty and dirty[0] == 1
               else "更新の %s 命令目の切れ目で" % (dirty[0] if dirty else -1),
               dirty[1] if dirty else None))

    # --- 補間中の再入力が無視されること ---
    samples2 = [ents.peek("ent_y", 0)]
    tap(nes, labels, "UP")                           # レーン移動を始める
    lane_mid = ents.peek("ent_lane", 0)
    tap(nes, labels, "DOWN")                         # 補間の途中で逆方向を入れる
    interrupted_lane = ents.peek("ent_lane", 0)
    interrupted_step = ents.peek("ent_lane_step", 0)
    while ents.peek("ent_lane_step", 0) != 0:
        step_frame(nes, labels)
        samples2.append(ents.peek("ent_y", 0))
    samples2.append(ents.peek("ent_y", 0))
    r.check("補間中の再入力が無視される", interrupted_lane == lane_mid,
            "補間中（残り %d フレーム）に DOWN を入れたら目標レーンが %d → %d に変わった。"
            "移動中の入力は捨てること。受け付けると移動距離が中途半端なまま次の補間が始まり、"
            "足元Yがレーンの表からずれる" % (interrupted_step, lane_mid, interrupted_lane))
    r.check("再入力されても足元Yはレーン表の値に着地する",
            0 <= lane_mid < LANE_COUNT and ents.peek("ent_y", 0) == lane_y[lane_mid],
            "ent_y=%d / ent_lane=%d（期待 lane_ground_y[%d]=%s）。推移=%s"
            % (ents.peek("ent_y", 0), lane_mid, lane_mid,
               lane_y[lane_mid] if 0 <= lane_mid < LANE_COUNT else "レーン番号が範囲外", samples2))

    # --- UP を押し続けてレーン0に張り付くこと ---
    problems = []
    for _ in range(LANE_COUNT + 1):
        before = ents.peek("ent_lane", 0)
        samples, frames = _slide(nes, labels, ents, "UP")
        now = ents.peek("ent_lane", 0)
        expect = max(0, before - 1)
        if now != expect:
            problems.append("レーン%d で UP → レーン%d（期待 %d）" % (before, now, expect))
        if not 0 <= now < LANE_COUNT:
            problems.append("レーン番号が %d になった（有効なのは 0..%d）。"
                            "範囲外のレーン番号は lane_ground_y の表の外を引くので足元Yが化ける"
                            % (now, LANE_COUNT - 1))
            break
        p = _monotone_problem(samples, lane_y[now])
        if p:
            problems.append("レーン%d→%d の補間: %s（推移=%s）" % (before, now, p, samples))
        if ents.peek("ent_y", 0) != lane_y[now]:
            problems.append("レーン%d の足元Yが %d（表は %d）"
                            % (now, ents.peek("ent_y", 0), lane_y[now]))
    r.check("UP を%d回でレーン0(y=%d)に着き、それ以上は奥へ抜けない"
            % (LANE_COUNT + 1, lane_y[0]),
            not problems and ents.peek("ent_lane", 0) == 0
            and ents.peek("ent_y", 0) == lane_y[0],
            "%s。最終 lane=%d y=%d（期待 lane=0 y=%d）。レーン0より奥へ出ると "
            "lane_ground_y の範囲外を引いて足元Yが化ける"
            % ("／".join(problems) or "問題なし", ents.peek("ent_lane", 0),
               ents.peek("ent_y", 0), lane_y[0]))

    # --- DOWN を押し続けて最手前レーンに張り付くこと ---
    problems = []
    for _ in range(LANE_COUNT + 1):
        before = ents.peek("ent_lane", 0)
        samples, frames = _slide(nes, labels, ents, "DOWN")
        now = ents.peek("ent_lane", 0)
        expect = min(LANE_COUNT - 1, before + 1)
        if now != expect:
            problems.append("レーン%d で DOWN → レーン%d（期待 %d）" % (before, now, expect))
        if not 0 <= now < LANE_COUNT:
            problems.append("レーン番号が %d になった（有効なのは 0..%d）。"
                            "範囲外のレーン番号は lane_ground_y の表の外を引くので足元Yが化ける"
                            % (now, LANE_COUNT - 1))
            break
        p = _monotone_problem(samples, lane_y[now])
        if p:
            problems.append("レーン%d→%d の補間: %s（推移=%s）" % (before, now, p, samples))
    r.check("DOWN を%d回でレーン%d(y=%d)に着き、それ以上は手前へ抜けない"
            % (LANE_COUNT + 1, LANE_COUNT - 1, lane_y[-1]),
            not problems and ents.peek("ent_lane", 0) == LANE_COUNT - 1
            and ents.peek("ent_y", 0) == lane_y[-1],
            "%s。最終 lane=%d y=%d（期待 lane=%d y=%d）"
            % ("／".join(problems) or "問題なし", ents.peek("ent_lane", 0),
               ents.peek("ent_y", 0), LANE_COUNT - 1, lane_y[-1]))

    restore_enemies()        # 以降の節は敵の居る状態に戻して見る


# ---------------------------------------------------------------- NMI の分担
def _check_nmi_budget(nes, labels, r):
    """NMI が「完成済みバッファを DMA するだけ」に留まっていることの検証。

    ここで測るサイクル数は命令単位の近似である（ハーネスは cycle 精度を持たない）。
    正確なタイミングの検証は L3（Mesen2）の仕事なので、ここでは
    「NMI に仕事を積みすぎていないか」を桁で見る（ADR-0004）。
    """
    r.section("NMI の分担（CLAUDE.md 第4節 / ADR-0002）")

    # OAM 構築が NMI に引っ越していたら、これらの番地が NMI 中に書かれる。
    ent_span = labels["ent_attr"] + (labels["ent_x_lo"] - labels["ent_active"]) - labels["ent_active"]
    watched = [(labels["oam_shadow"], 256, "OAM シャドウ"),
               (labels["ent_active"], ent_span, "エンティティテーブル"),
               (labels["oam_order"], labels["ent_x_lo"] - labels["ent_active"], "oam_order")]
    watched += [(labels[n], 1, n) for n in ("oam_used", "oam_dropped", "oam_next", "sort_count")]

    nes.trace_nmi = True
    nes.run_frames(2)
    nes.trace_nmi = False

    bad = []
    for addr, value in nes.nmi_writes:
        for base, size, what in watched:
            if base <= addr < base + size:
                bad.append("$%04X (%s) に $%02X" % (addr, what, value))
                break
    r.check("NMI 中に OAM 構築を行っていない", not bad,
            "NMI ハンドラが OAM 構築の作業番地へ %d 回書き込んだ（先頭3件: %s）。"
            "並べ替えと OAM 展開は描画期間中に済ませ、"
            "NMI は完成済みバッファの DMA だけにすること。NMI に積むと VBlank を食い潰して "
            "画面が乱れる（CLAUDE.md 第4節 / ADR-0002 実行コスト）"
            % (len(bad), "、".join(bad[:3])))

    r.check("NMI が VBlank 予算（約%d サイクル）に収まっている（%d サイクル・概算）"
            % (VBLANK_CYCLES, nes.nmi_cycles or 0),
            nes.nmi_cycles is not None and nes.nmi_cycles <= VBLANK_CYCLES,
            "NMI ハンドラが約 %s サイクル。VBlank は約 %d サイクルしかないので、"
            "はみ出したぶんは描画期間に食い込んで画面が乱れる。OAM DMA だけで 513 サイクル要る。"
            "※ この値は命令単位の近似。正確なタイミングは Mesen2 で見ること（ADR-0004）"
            % (nes.nmi_cycles, VBLANK_CYCLES))

    r.check("NMI が OAM DMA を行っている", nes.oam_dma_page == (labels["oam_shadow"] >> 8),
            "$4014 に書いたページが $%02X、OAM シャドウは $%04X にある"
            % (nes.oam_dma_page or 0, labels["oam_shadow"]))


# ---------------------------------------------------------------- 並べ替え
def _check_sort_order(nes, labels, ents, lane_y, r):
    """oam_order が「優先度クラス昇順 → 足元Y降順 → 添字昇順（安定）」であること。

    足元Yは実装上 8ライン刻みに量子化されるが、それは実装の都合なので期待値には持ち込まない。
    レーンの足元Y（互いに 8 以上離れている）だけを使って、量子化に依存せずに順序を見る。
    """
    r.section("OAM 並べ替え（ADR-0002）")

    # (添字, クラス, 足元Y)。同じキーの組を2つ入れて安定性を見る。空きスロットも混ぜる。
    # #1 と #4 はレーンの外（画面の上端寄り / 下端寄り）に置いてある。
    # クラスは奥行きより強い（ADR-0002 はクラス0 を「毎フレーム必ず描く」と定めている）ので、
    # 「最も奥のクラス0」が「最も手前のクラス1」より先に出ることを見る。
    scene = [
        (1,  SPR_CLASS_PLAYER,   32),          # 画面のいちばん奥に居るクラス0
        (4,  SPR_CLASS_PROJECT,  208),         # 画面のいちばん手前に居るクラス1
        (0,  SPR_CLASS_PLAYER,   lane_y[1]),
        (2,  SPR_CLASS_FAR_ENE,  lane_y[3]),
        (3,  SPR_CLASS_FAR_ENE,  lane_y[0]),
        (5,  SPR_CLASS_NEAR_ENE, lane_y[2]),
        (6,  SPR_CLASS_FAR_ENE,  lane_y[3]),   # #2 と同じキー（安定なら #2 が先）
        (7,  SPR_CLASS_EFFECT,   lane_y[3]),
        (9,  SPR_CLASS_PROJECT,  lane_y[0]),
        (11, SPR_CLASS_FAR_ENE,  lane_y[1]),
        (13, SPR_CLASS_NEAR_ENE, lane_y[2]),   # #5 と同じキー（安定なら #5 が先）
    ]
    scene = [e for e in scene if e[0] < ents.count]
    ents.clear()
    for i, cls, y in scene:
        ents.place(i, x=8 * i, y=y, cls=cls, body=0, tile=0)

    nes.call(labels["oam_sort_order"])
    count = nes.ram[labels["sort_count"] & 0x7FF]
    order = [nes.ram[(labels["oam_order"] + i) & 0x7FF] for i in range(count)]

    expected = [i for i, _, _ in sorted(scene, key=lambda e: (e[1], -e[2], e[0]))]
    info = {i: (c, y) for i, c, y in scene}

    def fmt(seq):
        return "、".join("#%d(class=%d,y=%d)" % (i, info[i][0], info[i][1])
                         if i in info else "#%d(未配置!)" % i for i in seq)

    r.check("有効なエンティティだけが並べ替えの対象になる (%d 体)" % len(scene),
            count == len(scene) and sorted(order) == sorted(i for i, _, _ in scene),
            "sort_count=%d, oam_order=%s。期待は %d 体 %s。"
            "非アクティブなスロットを拾うと、居ないはずのキャラが画面に出る"
            % (count, order, len(scene), sorted(i for i, _, _ in scene)))

    r.check("oam_order がクラス昇順 → 足元Y降順（手前が先）に並ぶ", order == expected,
            "実際=%s\n        期待=%s。OAM は先頭ほど前面に出るので、"
            "この順序が崩れると奥のキャラが手前に描かれる（ADR-0002）" % (fmt(order), fmt(expected)))

    ties = [(2, 6), (5, 13)]
    tie_bad = []
    for a, b in ties:
        if a in order and b in order and order.index(a) > order.index(b):
            tie_bad.append("#%d と #%d（同じクラス・同じ足元Y）が %d, %d 番目"
                           % (a, b, order.index(a), order.index(b)))
    r.check("キーが同じなら添字の小さい方が先（並べ替えが安定）", not tie_bad,
            "%s。並べ替えが安定でないと、重なった2体の前後がフレームごとに入れ替わって "
            "ちらついて見える" % "／".join(tie_bad))

    nes.call(labels["oam_sort_order"])
    again = [nes.ram[(labels["oam_order"] + i) & 0x7FF]
             for i in range(nes.ram[labels["sort_count"] & 0x7FF])]
    r.check("同じ配置なら並びが毎回同じ（フレーム間で暴れない）", again == order,
            "1回目=%s / 2回目=%s。前フレームの並びが結果に影響している" % (order, again))

    # --- 番兵 (oam_sortkey_guard) を汚してから呼んでも並びが壊れないこと ---
    # 挿入ソートの内側ループは oam_sortkey の1バイト手前を読み、そこに置いた $00 で止まる。
    # 「起動時に 0 だから大丈夫」に頼っていると、番兵が別の値になった瞬間に
    # ループが添字 0 を通り越し、**隣の配列を並べ替えキーとして読みながら RAM を
    # 塗り潰す**という気付きにくい壊れ方をする。番兵を張り直しているかを直接見る。
    guard = labels.get("oam_sortkey_guard")
    if guard is None:
        r.check("並べ替えキーの番兵 (oam_sortkey_guard) がある", False,
                "ラベル oam_sortkey_guard が build/roaring.labels に無い。"
                "挿入ソートの停止を番兵で保証する作りが消えている（sprite.s を見よ）")
    else:
        nes.ram[guard & 0x7FF] = 0xFF            # 前のフレームが残した「どのキーより大きい値」
        try:
            nes.call(labels["oam_sort_order"])
            dirty = [nes.ram[(labels["oam_order"] + i) & 0x7FF]
                     for i in range(nes.ram[labels["sort_count"] & 0x7FF])]
            crashed = None
        except CpuCrash as e:
            dirty, crashed = [], e
        r.check("並べ替えキーの番兵が汚れていても並びが壊れない",
                crashed is None and dirty == order and nes.ram[guard & 0x7FF] == 0,
                "番兵に $FF を置いてから oam_sort_order を呼んだら %s。"
                "番兵は呼び出しのたびに張り直すこと（sprite.s の `sta oam_sortkey_guard`）。"
                "張り直しが無いと、挿入ソートが添字 0 で止まらず oam_sortkey の手前へ "
                "はみ出して読み書きし、スプライトの前後関係が壊れるだけでなく "
                "隣のグローバル変数を塗り潰す"
                % ("クラッシュした: %s" % crashed if crashed
                   else "並びが %s になった（期待 %s、呼び出し後の番兵=$%02X）"
                        % (dirty, order, nes.ram[guard & 0x7FF])))


# ---------------------------------------------------------------- OAM 展開
def _check_oam_emit(nes, labels, ents, r):
    r.section("OAM 展開（体格・画面外の切り捨て・容量）")

    widths_rom = []
    body_count = table_length(labels, "body_width")
    if not 1 <= body_count <= 16:
        body_count = 6
    for b in range(body_count):
        widths_rom.append(nes.read(labels["body_width"] + b))

    # --- 体格型ごとの消費スプライト数が body_width の表と一致すること ---
    measured = []
    for b in range(body_count):
        ents.clear()
        set_camera(nes, labels, 0)
        ents.place(0, x=32, y=196, cls=SPR_CLASS_PLAYER, body=b, tile=4)
        used, dropped = build_oam(nes, labels)
        measured.append(used)
    r.check("体格型ごとの消費スプライト数が body_width の表と一致する %s" % widths_rom,
            measured == widths_rom,
            "実測=%s / 表=%s（体格 %d 種）。emit_entity が体格表を見ていない。"
            "表より多く出すと OAM が足りなくなり、少ないとキャラが欠けて描かれる"
            % (measured, widths_rom, body_count))

    widest = max(widths_rom)
    widest_body = widths_rom.index(widest)

    # --- 横に並ぶ列の位置とタイル番号（8x16 を横に並べる約束）---
    ents.clear()
    set_camera(nes, labels, 0)
    ents.place(0, x=64, y=196, cls=SPR_CLASS_PLAYER, body=widest_body, tile=4, attr=1)
    used, dropped = build_oam(nes, labels)
    cols = [shadow_entry(nes, labels, i) for i in range(used)]
    xs = [c[3] for c in cols]
    tiles = [c[1] for c in cols]
    r.check("体の各列が %dドット間隔・タイル+2 ずつで並ぶ" % SPRITE_W,
            xs == [64 + SPRITE_W * i for i in range(used)]
            and tiles == [4 + 2 * i for i in range(used)],
            "X=%s（期待 %s）、タイル=%s（期待 %s）。8x16 モードでは1スプライトが上下2タイルを使うので "
            "隣の列はタイル番号 +2。ここがずれると体が途中で切れた絵になる"
            % (xs, [64 + SPRITE_W * i for i in range(used)], tiles,
               [4 + 2 * i for i in range(used)]))
    r.check("足元Yから OAM の Y バイトへの変換が %d ライン上にずれる" % ENT_Y_TO_OAM,
            all(c[0] == 196 - ENT_Y_TO_OAM for c in cols),
            "OAM の Y=%s（期待 %d = 足元196 - %d）。OAM の Y は実際の表示より1ライン上にずれる "
            "仕様なので、体の高さ+1 を引く" % ([c[0] for c in cols], 196 - ENT_Y_TO_OAM,
                                              ENT_Y_TO_OAM))
    r.check("属性バイトがエンティティの ent_attr のまま出る", all(c[2] == 1 for c in cols),
            "属性=%s（期待は全て 1）。パレット指定が落ちると色が化ける" % [c[2] for c in cols])

    # --- カメラを動かしたときの画面X ---
    set_camera(nes, labels, 0x0100)
    ents.clear()
    ents.place(0, x=0x0100 + 128, y=196, cls=SPR_CLASS_PLAYER, body=0, tile=0)
    used, _ = build_oam(nes, labels)
    r.check("画面X = ワールドX - カメラX（16bit）", used == 1 and shadow_entry(nes, labels, 0)[3] == 128,
            "カメラ $0100 / ワールドX $0180 のとき OAM の X=%s（期待 128）。"
            "カメラの引き算が 16bit で行われていない"
            % ([shadow_entry(nes, labels, i)[3] for i in range(used)]))

    # --- 画面右端をまたぐ: 画面内の列だけ出る ---
    ents.clear()
    ents.place(0, x=0x0100 + 240, y=196, cls=SPR_CLASS_PLAYER, body=widest_body, tile=0)
    used, dropped = build_oam(nes, labels)
    xs = [shadow_entry(nes, labels, i)[3] for i in range(used)]
    in_screen = [240 + SPRITE_W * i for i in range(widest) if 240 + SPRITE_W * i < 256]
    r.check("画面右端をまたぐキャラは画面内の列だけ出る", xs == in_screen,
            "X=%s（期待 %s、体格幅 %d 列のうち画面内は %d 列）。画面外の列を出すと "
            "X が回り込んで反対側の端にゴミが出る" % (xs, in_screen, widest, len(in_screen)))

    # --- 画面左端をまたぐ ---
    ents.clear()
    ents.place(0, x=0x0100 - 16, y=196, cls=SPR_CLASS_PLAYER, body=widest_body, tile=0)
    used, _ = build_oam(nes, labels)
    xs = [shadow_entry(nes, labels, i)[3] for i in range(used)]
    tiles = [shadow_entry(nes, labels, i)[1] for i in range(used)]
    left_expect = [SPRITE_W * i - 16 for i in range(widest) if SPRITE_W * i - 16 >= 0]
    r.check("画面左端をまたぐキャラは画面内の列だけ出る", xs == left_expect,
            "X=%s（期待 %s）。画面外（画面X が負）の列を出すと右端にゴミが出る"
            % (xs, left_expect))
    r.check("切り捨てられても残った列のタイル番号が列の位置と合っている",
            tiles == [2 * (widest - len(left_expect) + i) for i in range(len(left_expect))],
            "タイル=%s（期待 %s）。切り捨てた列のぶんタイル番号を進めていないと、"
            "体の左半分が右半分の絵で描かれる"
            % (tiles, [2 * (widest - len(left_expect) + i) for i in range(len(left_expect))]))

    # --- 完全に画面外 ---
    ents.clear()
    ents.place(0, x=0x0100 + 0x0300, y=196, cls=SPR_CLASS_PLAYER, body=widest_body, tile=0)
    used, dropped = build_oam(nes, labels)
    r.check("画面外のキャラは1列も出ない", used == 0,
            "使用エントリ数=%d（期待 0）。カメラから 768 ドット離れたキャラが画面に出ている" % used)

    # --- 画面上端の切り捨て（足元Yが小さすぎるもの）---
    ents.clear()
    set_camera(nes, labels, 0)
    ents.place(0, x=32, y=ENT_Y_TO_OAM - 1, cls=SPR_CLASS_PLAYER, body=0, tile=0)
    above, _ = build_oam(nes, labels)
    ents.place(0, x=32, y=ENT_Y_TO_OAM, cls=SPR_CLASS_PLAYER, body=0, tile=0)
    edge, _ = build_oam(nes, labels)
    r.check("足元Yが画面上端より上のキャラは出ない（境界は %d）" % ENT_Y_TO_OAM,
            above == 0 and edge == 1,
            "足元Y=%d のとき %d 個、足元Y=%d のとき %d 個（期待 0 個と 1 個）。"
            "上端の判定で借りが出たまま OAM の Y に書くと、画面下端にゴミが出る"
            % (ENT_Y_TO_OAM - 1, above, ENT_Y_TO_OAM, edge))

    # --- 容量: MAX_ENTITIES × 最大体格幅 が OAM に収まること ---
    ents.clear()
    set_camera(nes, labels, 0)
    for i in range(ents.count):
        ents.place(i, x=8 * i, y=196, cls=SPR_CLASS_FAR_ENE, body=widest_body, tile=0)
    used, dropped = build_oam(nes, labels)
    need = ents.count * widest
    r.check("MAX_ENTITIES(%d) × 最大体格幅(%d) = %d が OAM 64 に収まり、捨てが出ない"
            % (ents.count, widest, need),
            dropped == 0 and used == min(need, OAM_SPRITE_MAX) and need <= OAM_SPRITE_MAX,
            "使用=%d / 捨て=%d（必要 %d, OAM は %d）。上限か体格表を増やして 64 を超えたなら、"
            "先に「どれを捨てるか」を決めること（ADR-0002 の巡回）。"
            "いまの捨て方は oam_order の後ろから無条件に落とすので、"
            "操作キャラより先に並んだ敵が生き残って操作キャラが消えることはないが、"
            "同クラス内では奥のキャラが黙って消える"
            % (used, dropped, need, OAM_SPRITE_MAX))
    r.check("OAM を使い切ったとき 64 エントリ全部が実体で埋まっている",
            all(shadow_entry(nes, labels, i)[0] == 196 - ENT_Y_TO_OAM
                for i in range(min(used, OAM_SPRITE_MAX))),
            "使用 %d 個のうち Y が %d でないエントリがある: %s"
            % (used, 196 - ENT_Y_TO_OAM,
               [i for i in range(min(used, OAM_SPRITE_MAX))
                if shadow_entry(nes, labels, i)[0] != 196 - ENT_Y_TO_OAM][:8]))

    # --- 直前に 64 個出していた状態から 1 体だけの画面へ: 残りが隠れること ---
    ents.clear()
    ents.place(0, x=32, y=196, cls=SPR_CLASS_PLAYER, body=0, tile=0)
    used, _ = build_oam(nes, labels)
    stale = [i for i in range(used, OAM_SPRITE_MAX)
             if shadow_entry(nes, labels, i)[0] != OAM_Y_OFFSCREEN]
    r.check("キャラが減ったフレームで、前の描画が画面に残らない", used == 1 and not stale,
            "使用=%d、$FF で隠れていないエントリ=%s。直前に OAM を使い切った状態から "
            "1体だけの画面に変えたのに、古いスプライトが %d 個残っている"
            % (used, stale[:8], len(stale)))


# ---------------------------------------------------------------- 矩形交差
def _set_rect(nes, labels, which, x, y, w, h):
    base = labels[which] & 0x7FF
    nes.ram[base + RECT_X_LO] = x & 0xFF
    nes.ram[base + RECT_X_HI] = (x >> 8) & 0xFF
    nes.ram[base + RECT_Y] = y & 0xFF
    nes.ram[base + RECT_W] = w & 0xFF
    nes.ram[base + RECT_H] = h & 0xFF


# (名前, a=(x,y,w,h), b=(x,y,w,h), 期待)
RECT_CASES = [
    ("半分重なる",                     (100, 100, 16, 16), (108, 100, 16, 16), True),
    ("1ドットだけ重なる",              (100, 100, 16, 16), (115, 100, 16, 16), True),
    ("辺で接するだけ（右端＝左端）",   (100, 100, 16, 16), (116, 100, 16, 16), False),
    ("1ドット離れる",                  (100, 100, 16, 16), (117, 100, 16, 16), False),
    ("完全一致",                       (100, 100, 16, 16), (100, 100, 16, 16), True),
    ("b が a に完全に含まれる",        (100, 100, 32, 32), (108, 108,  8,  8), True),
    ("X が辺で接するだけ（b が左）",   (100, 100, 16, 16), ( 84, 100, 16, 16), False),
    ("X が1ドット重なる（b が左）",    (100, 100, 16, 16), ( 85, 100, 16, 16), True),
    ("Y が1ドット重なる",              (100, 100, 16, 16), (100, 115, 16, 16), True),
    ("Y が辺で接するだけ",             (100, 100, 16, 16), (100, 116, 16, 16), False),
    ("Y が辺で接するだけ（b が上）",   (100, 100, 16, 16), (100,  84, 16, 16), False),
    ("Y が1ドット重なる（b が上）",    (100, 100, 16, 16), (100,  85, 16, 16), True),
    ("Y が離れている",                 (100, 100, 16, 16), (100, 140, 16, 16), False),
    ("幅0 はどこでも当たらない",       (100, 100,  0, 16), (100, 100, 16, 16), False),
    ("高さ0 はどこでも当たらない",     (100, 100, 16,  0), (100, 100, 16, 16), False),
    # --- X の 16bit 桁上がりを跨ぐ場合（当たり判定で最も事故が起きる場所）---
    ("桁上がりを跨いで重なる",         (248, 100, 16, 16), (260, 100,  8, 16), True),
    ("桁上がりの先で離れる",           (240, 100,  8, 16), (260, 100,  8, 16), False),
    ("桁上がりの境界で1ドット重なる",  (255, 100,  2, 16), (256, 100,  8, 16), True),
    ("桁上がりの境界で接するだけ",     (255, 100,  1, 16), (256, 100,  8, 16), False),
    ("a の右端の計算が桁上がりする",   (0x01FF, 100, 2, 16), (0x0200, 100, 8, 16), True),
    ("b の右端の計算が桁上がりする",   (0x0100, 100, 8, 16), (0x00FC, 100, 8, 16), True),
    ("b の右端の桁上がりの先で離れる", (0x0104, 100, 8, 16), (0x00F0, 100, 8, 16), False),
    ("上位バイトが違って離れる",       (16, 100, 16, 16), (272, 100, 16, 16), False),
    ("上位バイトが2つ違って離れる",    (16, 100, 16, 16), (528, 100, 16, 16), False),
    # --- Y の下端が 8bit を溢れる場合 ---
    ("Y下端が255を超えて重なる",       (100, 250, 16, 20), (100, 252, 16, 20), True),
    ("Y下端が255を超えるが X が離れる", (100, 250, 16, 20), (200, 252, 16, 20), False),
    ("Y下端が255を超える相手が遠く下", (100,  10, 16, 16), (100, 250, 16, 20), False),
]


def _call_rect(nes, labels, a, b):
    _set_rect(nes, labels, "rect_a", *a)
    _set_rect(nes, labels, "rect_b", *b)
    return nes.call(labels["rect_overlap"])


def _check_rect_overlap(nes, labels, r):
    r.section("rect_overlap（当たり判定プリミティブ）")

    wrong = []
    for name, a, b, expect in RECT_CASES:
        got = _call_rect(nes, labels, a, b).carry
        if got != expect:
            wrong.append("%s: a=(x=%d,y=%d,w=%d,h=%d) b=(x=%d,y=%d,w=%d,h=%d) → %s（期待 %s）"
                         % ((name,) + a + b + ("重なる" if got else "重ならない",
                                               "重なる" if expect else "重ならない")))
    r.check("rect_overlap の真理値表 (%d ケース)" % len(RECT_CASES), not wrong,
            "%d 件が期待と違う:\n        %s" % (len(wrong), "\n        ".join(wrong)))

    # 交換しても同じ結果になること（片側だけの比較漏れは、殴った側と殴られた側で
    # 判定が食い違うという形で出る）
    asym = []
    for name, a, b, expect in RECT_CASES:
        if _call_rect(nes, labels, b, a).carry != expect:
            asym.append(name)
    r.check("rect_overlap が引数の順序によらない (a↔b を入れ替えても同じ)", not asym,
            "入れ替えると結果が変わるケース: %s。攻撃側と被弾側で判定が食い違う"
            % "、".join(asym))

    res = _call_rect(nes, labels, RECT_CASES[0][1], RECT_CASES[0][2])
    r.check("rect_overlap がスタックを壊さない", res.sp_delta == 0,
            "呼び出しの前後でスタックポインタが %d ずれた" % res.sp_delta)


# ---------------------------------------------------------------- レーン距離
def _check_lane_distance(nes, labels, r):
    r.section("lane_distance（レーン間の距離）")

    wrong = []
    zwrong = []
    for i in range(LANE_COUNT):
        for j in range(LANE_COUNT):
            res = nes.call(labels["lane_distance"], a=i, y=j)
            if res.a != abs(i - j):
                wrong.append("(%d, %d) → %d（期待 %d）" % (i, j, res.a, abs(i - j)))
            if res.zero != (i == j):
                zwrong.append("(%d, %d) → Z=%d（期待 %d）" % (i, j, res.zero, i == j))
    r.check("lane_distance が %d×%d 全ての組で |差| を返す" % (LANE_COUNT, LANE_COUNT), not wrong,
            "%s。レーン間の距離が狂うと、隣レーンへの攻撃判定（action-dev が使う）が総崩れになる"
            % "、".join(wrong))
    r.check("lane_distance の Z フラグが「同一レーン」を表す", not zwrong,
            "%s。Z フラグだけを見て同レーン判定をする呼び出し側が誤爆する" % "、".join(zwrong))
