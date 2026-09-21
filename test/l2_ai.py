"""L2 実行検証 — ai P1（自律仲間「万能型」と敵の思考・行動）。

run_tests.py から呼ばれる。層の分け方は ADR-0004 に従う。

ここで見るのは ai-dev の成功条件のうち**最優先の不具合**、
「自律仲間が壁になって進行不能にしない」ことである（.claude/agents/ai-dev.md）。
P2 で型が6つに増えたとき、誰も気付けない形で壊れてはならないので、
「実測してみたら大丈夫だった」ではなく**不変条件**として置く。

方針:
  * 期待値は data/ai_params.tsv（正本）と ROM の表、build/roaring.labels から導出する。
    **AI のパラメータは主が調整するノブである。値を変えてテストが落ちてはならない。**
  * 閾値をテスト側に置くときは、実測値の写しにせず「画面で何が起きたら不具合か」で決める。
  * 失敗メッセージは「何が期待と違い、どこが壊れている疑いがあるか」を1行で書く。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "harness"))

from cpu6502 import CpuCrash                        # noqa: E402
from nes import Nes, boot, frame_end, step_frame    # noqa: E402
from scene import disarm_enemies, slot_ranges       # noqa: E402
from gate import Gate, run_sections, boot_or_fail, skip_sections   # noqa: E402
from srcdefs import action_defs, ai_defs, defs      # noqa: E402
import dbgmode                                      # noqa: E402

TSV_PATH = os.path.join(os.path.dirname(HERE), "data", "ai_params.tsv")

# data/ai_params.tsv の列名と、src/ai/ai_params.s の配列ラベルの対応。
# id と name は表に出ない（id は添字そのもの、name は roster から引くための識別子）。
# **レーンは ADR-0009 で廃止した。** lane_hold（次にレーンを移れるまでの待ちフレーム数）は
# depth_spd（奥行きの寄り足の速さ）に意味ごと変わっている（TSV の列の説明を見よ）。
# **列を足したらここにも足すこと。**この並びが「TSV と ai_params.s の写しを
# 突き合わせる列」そのものであり、ここに無い列は写し間違いを誰も見張っていない。
PARAM_COLUMNS = ("speed", "hold_x", "reach_x", "aggr_x",
                 "follow_x", "leash_x", "atk_gap", "depth_spd", "item_pri",
                 "hate", "react", "wander", "dwell")

# 足場（シーンを組み立てて走らせる）に要るラベル。ここが欠けたときだけ層ごと諦める。
CORE = ("ent_active", "ent_x_lo", "ent_x_hi", "ent_y", "ent_state", "ent_ai", "wait_nmi")

D = action_defs()
# 「奥行きが近い」と見なす足元Yの差。8x16 のスプライトは足元Yが SPRITE_H 未満しか
# 離れていなければ必ず走査線を共有する＝画面で重なって見える。
# **当たり判定のY許容幅（action の持ち分）とは別物である。**ここで見たいのは
# 「絵が重なるか」であって「攻撃が届くか」ではない。
DEPTH_OVERLAP_Y = D["SPRITE_H"]

# 「重なっている」と見なす横距離。スプライトは 8x16 が横に並ぶので、
# 原点どうしが 8 ドット未満なら絵が必ず重なる（＝1体が他方の陰に入る）。
OVERLAP_X = 8

# 重なりが「持続した」と見なすフレーム数。
# すれ違いざまに 1〜数フレーム重なるのは避けようがなく、画面でも見えない。
# 8 フレーム（約0.13秒）続くと、1スキャンライン8スプライトの制約と相まって
# 「1体消えた」と分かる長さになる。ai-dev の実測（仲間どうしは 600 フレーム中 6 フレーム、
# 操作キャラとの間は最長 4 フレーム）も、この内側に収まる主張である。
OVERLAP_FRAMES_MAX = 8

# ACT_ST_DOWN（src/action/action.inc）。この値以上が「HP が尽きた側」。
# 生存条件の判定にだけ使う。**この値そのものを主張するテストはここに無い**
# （状態の語彙は action-dev の持ち分で、l2_action 側で見るべきもの）。
ACT_ST_DOWN = 5
ACT_ST_IDLE = 0


# ---------------------------------------------------------------- 足場
class Scene:
    """P1 の動作確認シーン（src/main.s の scene_test_init）を起動した状態。"""

    def __init__(self, rom_path, labels):
        self.labels = labels
        self.slots = slot_ranges(labels)
        self.nes = Nes(rom_path)
        self.nes.reset()
        if boot(self.nes) is None:
            raise CpuCrash("起動しても NMI が来ない（OAM DMA が1回も無い）")
        frame_end(self.nes, labels)

    # --- RAM ---
    def get(self, name, i=0):
        return self.nes.ram[(self.labels[name] + i) & 0x7FF]

    def put(self, name, i, value):
        self.nes.ram[(self.labels[name] + i) & 0x7FF] = value & 0xFF

    def x(self, i):
        return self.get("ent_x_lo", i) | (self.get("ent_x_hi", i) << 8)

    def set_x(self, i, value):
        self.put("ent_x_lo", i, value & 0xFF)
        self.put("ent_x_hi", i, (value >> 8) & 0xFF)

    def alive(self, i):
        return bool(self.get("ent_active", i)) and self.get("ent_state", i) < ACT_ST_DOWN

    # --- 進行 ---
    def hold(self, buttons, frames, watch=None):
        """buttons を押したまま frames 進める。watch(self) を毎フレーム呼んで結果を集める。"""
        self.nes.set_buttons(set(buttons))
        out = []
        for _ in range(frames):
            self.nes.run_frames(1)
            if watch is not None:
                out.append(watch(self))
        return out

    def tap(self, button):
        """1フレームだけ押して離し、そのフレームの更新が終わった点で止める
        （観測の作法は test/harness/nes.py 冒頭を見よ）。"""
        self.nes.set_buttons({button})
        self.nes.run_frames(1)
        self.nes.set_buttons(set())
        frame_end(self.nes, self.labels)

    def trace(self, frames, watch, buttons=()):
        """1フレームずつ進め、**そのフレームの更新が終わった点**で観測する。

        hold() の戻り位置はフレームの更新の**最中**である（test/harness/nes.py 冒頭）。
        ai_woff / ai_gap / ai_react のような作業変数はそこで書き換わる途中なので、
        それらを読む観測にはこちらを使うこと。
        """
        self.nes.set_buttons(set(buttons))
        out = []
        for _ in range(frames):
            step_frame(self.nes, self.labels, 1)
            out.append(watch(self))
        return out

    def settle(self, frames=1):
        self.nes.set_buttons(set())
        step_frame(self.nes, self.labels, frames)

    def call(self, name, **kw):
        return self.nes.call(self.labels[name], **kw)

    def sleep(self, first, end):
        for i in range(first, end):
            self.put("ent_active", i, 0)

    def y(self, i):
        """足元Y = 奥行きそのもの（ADR-0009。レーン番号という概念は無い）。"""
        return self.get("ent_y", i)

    def near_depth(self, i, j):
        """2体の絵が奥行き方向で重なるか（走査線を共有するか）。"""
        return abs(self.y(i) - self.y(j)) < DEPTH_OVERLAP_Y

    def place_second_ally(self, x, y):
        """自律仲間をもう1体、配置側（P3 の waves / P4 の roster）と同じ手順で置く。

        ent_* を詰める → ent_activate → act_init_entity → ai_init。
        最後が ai_init（全体）なのは、シーン開始時の main.s と同じ並びにするためである
        （scene_test_init → action_init → ai_init）。
        """
        i = self.slots["ally_first"] + 1
        self.set_x(i, x)
        for name, value in (("ent_y", y), ("ent_class", 3), ("ent_body", 0),
                            ("ent_ai", 0), ("ent_tile", 0), ("ent_attr", 2),
                            ("ent_state", ACT_ST_IDLE)):
            self.put(name, i, value)
        self.call("ent_activate", x=i)
        self.call("act_init_entity", x=i)
        self.call("ai_init")
        return i


def _signed(v):
    """8bit の符号つきの値（ai_woff など）を Python の整数にする。"""
    return v - 256 if v > 127 else v


def _sign(v):
    return (v > 0) - (v < 0)


def _ceil_div(a, b):
    return -(-a // b)


def _runs_of(flags):
    """True が連続した最長の長さと、合計の個数を返す。"""
    longest = current = 0
    for f in flags:
        current = current + 1 if f else 0
        longest = max(longest, current)
    return longest, sum(1 for f in flags if f)


# ---------------------------------------------------------------- 入口
def layer2_ai(rom_path, labels, r):
    print("L2 実行検証 / ai P1（自律仲間・敵の思考と行動）")

    gate = Gate(labels, r, "ai")
    if not boot_or_fail(gate, CORE, "シーンを起動してエンティティの座標を読むのに使う"):
        return

    # パラメータ表の検証（TSV と ROM の写しの突き合わせ）は、以降の節が
    # profiles を使うので先に走らせる。ここが読めなければ profiles を要る節だけが落ちる。
    profiles = None
    if gate.need("AI パラメータ表の検証が走る",
                 tuple("ai_p_" + c for c in PARAM_COLUMNS) + ("ai_default_profile",),
                 "data/ai_params.tsv の写しと突き合わせるのに使う"):
        profiles = _check_params_table(rom_path, labels, r)

    sections = (
        ("壁にならない",
         ("ent_activate", "act_init_entity", "ai_init", "ai_goal", "ai_gap"),
         _check_not_a_wall),
        ("操作キャラのダウン",
         ("ai_sit", "act_enter_down", "action_revive"), _check_leader_down),
        ("敵が居なくなった後の立ち位置",
         ("ai_goal", "ai_gap", "ai_woff", "ai_react"), _check_regroup),
        ("ヘイト（狙われやすさ）",
         ("ai_update", "ai_cursor", "ai_target", "ai_p_hate", "ai_p_aggr_x"),
         _check_hate),
        ("プロファイル番号の範囲",
         ("ai_init_entity", "ai_default_profile", "ai_goal", "ai_gap"), _check_profile_range),
        ("のけぞり中の入力",
         ("act_damage", "act_atk_ent", "act_kb_dir", "act_kb_amt", "depth_y_min"),
         _check_hurt_blocks_input),
    )
    if profiles is None:
        # 表が読めないと期待値（follow_x など）が作れない節がある。
        # **その節だけ**を名指しで落とす（層ごと諦めない）。
        for name, _needs, _fn in sections:
            r.check("%s の検証が走る" % name, False,
                    "AI パラメータ表（data/ai_params.tsv と ai_params.s の突き合わせ）が"
                    "読めなかったので、期待値を作れないこの節を飛ばした。"
                    "上のパラメータ表の失敗を先に直すこと")
        return
    run_sections(gate, sections, lambda fn: (rom_path, labels, profiles, r))

    # 手触り確認のためのデバッグ機構（src/debug.inc の dbg_ally）を見る節。
    # **仕様ではない。**DBG_ENABLE = 0 でコードごと消えるので、そのときは
    # 落とさずに飛ばす（無いものは壊れようがない）。飛ばしたことは SKIP として出る。
    dbg_sections = (
        ("休ませた仲間は敵の標的にならない",
         ("dbg_ally", "ai_target", "ai_update"), _check_rest_not_targeted),
        ("休ませても壁にならない",
         ("dbg_ally", "ai_goal"), _check_rest_not_a_wall),
    )
    if dbgmode.enabled():
        run_sections(gate, dbg_sections, lambda fn: (rom_path, labels, profiles, r))
    else:
        skip_sections(r, dbg_sections, dbgmode.SKIP_WHY)


# ---------------------------------------------------------------- パラメータ表
def _parse_tsv(path):
    """data/ai_params.tsv を [{列名: 値}] に読む。# 始まりと空行は読み飛ばす。"""
    rows = []
    header = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cells = line.split("\t")
            if header is None:
                header = [c.strip() for c in cells]
                continue
            rows.append(dict(zip(header, [c.strip() for c in cells])))
    return header, rows


def _check_params_table(rom_path, labels, r):
    """data/ai_params.tsv（正本）と src/ai/ai_params.s の写しが一致すること。

    変換器 tools/ai_params.py は stage-author の範囲で、まだ無い（ai_params.s 冒頭）。
    それまでの間、表は**人の手で書き写されている**。写し間違いと片側だけの変更は
    ここでしか気付けない。**TSV が正本である。**食い違ったら TSV 側を正として落とす。
    """
    r.section("パラメータ表（data/ai_params.tsv が正本 / 変換器が入るまでの drift 検出）")

    try:
        nes = Nes(rom_path)
        nes.reset()
    except CpuCrash as e:
        r.check("パラメータ表を読むために ROM を起動する", False, str(e))
        return None

    # 行数（AI_PROFILE_COUNT）は列ラベルの間隔から導出する。テストに焼き付けない。
    addrs = sorted(labels["ai_p_" + c] for c in PARAM_COLUMNS)
    strides = {b - a for a, b in zip(addrs, addrs[1:])}
    if len(strides) != 1:
        r.check("ai_params.s の列が同じ行数で並んでいる", False,
                "列ラベルの間隔が %s とばらついている（ai_p_* の並び: %s）。"
                "列ごとに1本の .byte 配列という形（Structure of Arrays）が崩れており、"
                "`lda ai_p_hold_x, y` が隣の列を読む" % (sorted(strides), [hex(a) for a in addrs]))
        return None
    rows_rom = strides.pop()

    if not os.path.exists(TSV_PATH):
        r.check("data/ai_params.tsv がある", False,
                "%s が無い。AI パラメータの正本が消えている" % TSV_PATH)
        return None
    header, tsv = _parse_tsv(TSV_PATH)

    missing_cols = [c for c in ("id", "name") + PARAM_COLUMNS if c not in (header or [])]
    if not r.check("TSV の列が %d 列（id / name / %s）揃っている"
                   % (2 + len(PARAM_COLUMNS), " / ".join(PARAM_COLUMNS)), not missing_cols,
                   "TSV に列が無い: %s（読めた見出し: %s）。列を増やすときは "
                   "ai_params.s 側にも対応する配列を足すこと" % (missing_cols, header)):
        return None

    ok_rows = r.check("TSV の行数と ai_params.s の行数が一致する (%d 行)" % len(tsv),
                      len(tsv) == rows_rom,
                      "TSV は %d 行、ai_params.s の表は %d 行。**TSV が正本**である。"
                      "型を足すとは TSV に行を1本足すことであり、ai_params.s の .byte を "
                      "同じ数だけ増やさないと、増えた型が隣の列の値で動く"
                      % (len(tsv), rows_rom))
    if not ok_rows:
        return None

    ids = [row["id"] for row in tsv]
    r.check("TSV の id が 0 からの連番である %s" % ids,
            ids == [str(i) for i in range(len(tsv))],
            "id=%s。id はそのまま ent_ai に入り、表の添字になる。連番でないと "
            "roster / waves が指した型と別の行が引かれる" % ids)

    bad = []
    table = {}
    for col in PARAM_COLUMNS:
        rom_values = [nes.read(labels["ai_p_" + col] + i) for i in range(rows_rom)]
        tsv_values = [int(row[col]) for row in tsv]
        table[col] = tsv_values
        for i, (want, got) in enumerate(zip(tsv_values, rom_values)):
            if want != got:
                bad.append("%s[%s(id=%d)]: TSV=%d / ai_params.s=%d"
                           % (col, tsv[i]["name"], i, want, got))
    r.check("TSV の %d 列 × %d 行が ai_params.s の表と一致する"
            % (len(PARAM_COLUMNS), rows_rom), not bad,
            "%d 件が食い違う: %s。**TSV が正本**なので、直すのは src/ai/ai_params.s 側である"
            "（値を変えるときは TSV を先に直し、写しを合わせる）。"
            "変換器 tools/ai_params.py が入るまで、この写しのずれはここでしか気付けない"
            % (len(bad), "／".join(bad[:6])))

    defaults = [nes.read(labels["ai_default_profile"] + i) for i in range(3)]
    r.check("立場ごとの既定プロファイルが表の中を指している %s" % defaults,
            all(0 <= d < rows_rom for d in defaults),
            "ai_default_profile=%s、表は %d 行。範囲外だと、配置側が ent_ai を詰め忘れた"
            "エンティティが**表の外**を読んで動く" % (defaults, rows_rom))
    return table


# ---------------------------------------------------------------- 壁にならない
def _check_not_a_wall(rom_path, labels, profiles, r):
    """ai-dev の成功条件2「壁になって進行不能にしない」の不変条件3つ。"""
    r.section("壁にならない（ai-dev 成功条件2・最優先）")
    slots = slot_ranges(labels)
    ally = slots["ally_first"]
    player = slots["player"]

    # --- 1. 前進量が変わらない ---
    # 仲間が居ても居なくても、右へ押しっぱなしにしたときの到達Xが**完全に一致**すること。
    # 「ほぼ一致」では駄目である。1ドットでも変われば、仲間が操作キャラの移動に
    # 干渉する経路が存在するという意味になる（いまのエンジンに実体の押し合いは無いので、
    # 干渉したならそれは AI が操作キャラの座標か足元Yに手を出している）。
    FORWARD_FRAMES = 180

    def watch(s):
        return (s.x(player), s.x(ally), s.near_depth(player, ally),
                s.alive(ally), s.alive(player))

    with_ally = Scene(rom_path, labels)
    # 最悪の場から走り出す: 仲間を操作キャラに**重ねて**置く。
    # シーンの初期配置のまま走ると仲間は 25 ドット後方に居り、専有距離
    # （AI_CLEAR_X = 14）の内側に一度も入らないので、「押し出し」の経路を
    # 一度も通らないまま「一致した」と言うことになる。重ねて始めれば、
    # 走り出しの数フレームで必ずその経路を通る。
    with_ally.set_x(ally, with_ally.x(player))
    with_ally.put("ent_y", ally, with_ally.y(player))
    samples = with_ally.hold({"RIGHT"}, FORWARD_FRAMES, watch)
    x_with = with_ally.x(player)

    without = Scene(rom_path, labels)
    without.sleep(slots["ally_first"], slots["enemy_first"])
    without.hold({"RIGHT"}, FORWARD_FRAMES)
    x_without = without.x(player)

    # 空振り防止: 仲間が生きていて、奥行きが重なる位置に居て、近くまで寄っていたこと。
    # 仲間が画面の彼方に居たのなら「一致して当然」であって、何も確かめていない。
    near = [abs(px - ax) for px, ax, close, aa, pa in samples if aa and close]
    r.check("仲間が生存して奥行きの重なる位置に居る状態で %d フレーム前進した"
            "（比較が空振りでない）" % FORWARD_FRAMES,
            len(near) >= FORWARD_FRAMES // 2 and min(near or [999]) < OVERLAP_X,
            "奥行きが重なった状態で生存していたのは %d フレーム（%d 中）、最接近 %s ドット。"
            "仲間が近くに居ないまま比べても「前進量が一致」は何も意味しない"
            "（専有距離の押し出しを一度も通らない）。重ねて置いたはずの仲間が"
            "初手で離れている＝シーンの配置か追従（follow_x / leash_x）が変わっている"
            % (len(near), FORWARD_FRAMES, min(near) if near else "—"))

    r.check("仲間が居ても居なくても %d フレーム前進した到達X が完全に一致する"
            % FORWARD_FRAMES, x_with == x_without,
            "仲間あり %d / 仲間なし %d（差 %d ドット）。自律仲間が操作キャラの前進を"
            "1ドットでも変えている＝**壁になっている**。いまのエンジンに実体の押し合いは"
            "無いので、干渉するとすれば AI が操作キャラ(#%d)の座標・足元Y・状態に"
            "手を出している経路である（ai.s の走査範囲 AI_FIRST が操作キャラを含んでいないか）"
            % (x_with, x_without, abs(x_with - x_without), player))

    # --- 2. 重なりが持続しない（操作キャラ vs 仲間）---
    # 最も厳しいのは**ワールド左端に押し込んだ**ときである。仲間の立ち位置は
    # 「操作キャラの follow_x だけ左」だが、左端より左には立てないので操作キャラの
    # 真上に潰れる。そこから外へ逃がすのが ai_clear_player_zone の仕事である。
    EDGE_FRAMES = 150
    edge = Scene(rom_path, labels)
    edge_restore = disarm_enemies(edge.nes, labels)   # 見たいのは追従であって戦闘ではない

    def watch2(s):
        both = s.alive(player) and s.alive(ally)
        return (both, s.near_depth(player, ally), abs(s.x(player) - s.x(ally)))

    edge_samples = edge.hold({"LEFT"}, EDGE_FRAMES, watch2)
    edge_restore()
    overlap = [(both and same and d < OVERLAP_X) for both, same, d in edge_samples]
    longest, total = _runs_of(overlap)
    live = [(same, d) for both, same, d in edge_samples if both]
    r.check("操作キャラと仲間がともに生存して奥行きが重なる位置に居る状態を観測できた"
            "（ワールド左端に %d フレーム押し込む）" % EDGE_FRAMES,
            sum(1 for same, _ in live if same) >= EDGE_FRAMES // 2,
            "両者生存 %d フレーム / うち足元Yの差が %d ドット未満 %d フレーム（%d 中）。"
            "両者ダウン後は誰も動かないので、生存条件を外すとこの検証は空振りする"
            % (len(live), DEPTH_OVERLAP_Y, sum(1 for same, _ in live if same), EDGE_FRAMES))
    r.check("左端に押し込んでも仲間と操作キャラの重なり (|x差| < %d) が %d フレーム以上続かない"
            % (OVERLAP_X, OVERLAP_FRAMES_MAX + 1), longest <= OVERLAP_FRAMES_MAX,
            "重なりが最長 %d フレーム続いた（合計 %d フレーム / %d 中、最接近 %d ドット）。"
            "操作キャラが仲間の陰に入り、1スキャンライン8スプライトの制約で"
            "どちらかが消える＝**壁になっている**。ai.s の ai_clear_player_zone が"
            "立ち位置を専有距離の外へ押し出していない（左へ押し出せないときに右へ回す "
            "@push_right の経路を疑え）"
            % (longest, total, EDGE_FRAMES,
               min([d for both, same, d in edge_samples if both and same] or [-1])))

    # --- 3. 仲間どうしが重ならない ---
    # P1 のシーンには仲間が1体しか居ないので、2体目を配置側と同じ手順で置いて見る
    # （パーティは操作1＋自律2。PARTY_SIZE = 3）。
    #
    # **観測を2段に分けてある。**
    #   前半 … 敵が居る。2体は同じ標的に寄るので ai_spread（番号順の分散）が効く
    #   後半 … 敵を消す。2体とも操作キャラの follow_x へ戻るので、
    #          **同じ立ち位置を奪い合う**最悪の場になり、ai_avoid_allies が効く
    #
    # 後半を足したのは、この節の「空振り防止」（2体がともに生存し、奥行きが重なる位置に
    # 居ることを十分な時間観測した）が**戦闘の終わる時刻に依存していた**からである。
    # 敵に寄っている間、2体は別々の敵を追って別々の奥行きに居る。奥行きが重なるのは
    # 戦闘が終わって2人とも操作キャラの所へ戻ってからで、その時刻は立ち位置の揺らぎ
    # （wander / dwell）や速さのノブで前後する。ai-dev の報告では、揺らぎが入っただけで
    # 重なりの観測できる時間が 71 → 35 フレームに減っている。
    # **こちらから戦闘を終わらせれば、観測窓は位相に依らない。**
    PAIR_ENGAGE = 220           # 敵に寄っている間（ai_spread）
    PAIR_REGROUP = 150          # 敵が居なくなってから（ai_avoid_allies）。ここで奥行きが揃う
    PAIR_FRAMES = PAIR_ENGAGE + PAIR_REGROUP
    pair = Scene(rom_path, labels)
    # 2体目は操作キャラを挟んで1体目の反対側に置く（1体目と同じ距離だけ右）。
    # 位置をテストに焼き付けず、シーンの配置から作る。
    mirror = pair.x(player) + (pair.x(player) - pair.x(ally))
    ally2 = pair.place_second_ally(mirror, pair.y(player))

    def watch3(s):
        both = s.alive(ally) and s.alive(ally2)
        return (both, s.near_depth(ally, ally2), abs(s.x(ally) - s.x(ally2)))

    pair_samples = pair.hold(set(), PAIR_ENGAGE, watch3)
    pair.sleep(slots["enemy_first"], slots["count"])     # 戦闘を終わらせる（敵が全滅した状態）
    pair_samples += pair.hold(set(), PAIR_REGROUP, watch3)
    pair_overlap = [(both and same and d < OVERLAP_X) for both, same, d in pair_samples]
    longest2, total2 = _runs_of(pair_overlap)
    near = [d for both, close, d in pair_samples if both and close]
    # 期待値は後半（敵を消してから）の長さで作る。前半で奥行きが重なるかどうかは
    # 敵の配置しだいであって、この節の主張とは関係が無い。
    want_near = PAIR_REGROUP // 2
    r.check("仲間2体がともに生存して奥行きが重なる位置に居る状態を観測できた"
            "（敵を消してから %d フレーム / 全 %d フレーム）" % (PAIR_REGROUP, PAIR_FRAMES),
            len(near) >= want_near,
            "両者生存かつ奥行きが重なっていたのは %d フレーム（%d 中、期待 %d 以上）。"
            "2体目の配置（%d ドット）が遠すぎるか、片方が早々に戦線離脱しているか、"
            "敵を消しても2体が操作キャラの奥行きへ戻ってきていない（追従が壊れている）"
            % (len(near), PAIR_FRAMES, want_near, mirror))
    r.check("仲間どうしの重なり (|x差| < %d) が %d フレーム以上続かない"
            % (OVERLAP_X, OVERLAP_FRAMES_MAX + 1), longest2 <= OVERLAP_FRAMES_MAX,
            "重なりが最長 %d フレーム続いた（合計 %d フレーム / %d 中、最小の横距離 %d ドット）。"
            "同じ立ち位置に寄った2体が同じ座標に潰れており、1スキャンライン8スプライトの制約で"
            "片方が消える。ai.s の ai_spread（番号順に AI_SPREAD ずつ後ろへずらす）が"
            "効いていないか、ai_clear_player_zone の押し出しが分散を**上書き**している"
            "（押された仲間が、押されていない仲間の立ち位置に重なる）。"
            "揺らぎ（ai_woff）を分散より**後**に足しても同じことが起きる"
            % (longest2, total2, PAIR_FRAMES, min(near) if near else -1))


# ---------------------------------------------------------------- 操作キャラのダウン
def _check_leader_down(rom_path, labels, profiles, r):
    """ADR-0006 の「口だけ開けておく」: 操作キャラが倒れたことを AI が認識していること。"""
    r.section("操作キャラのダウンを AI が認識する（ADR-0006 / ai_sit）")
    slots = slot_ranges(labels)
    player = slots["player"]
    s = Scene(rom_path, labels)

    s.settle()
    before = s.get("ai_sit")
    r.check("立っている間は ai_sit の bit0 が下りている", not (before & 1),
            "ai_sit=$%02X。操作キャラが倒れていないのに AI_SIT_LEADER_DOWN が立っている。"
            "この口が常時立っていると、P2 で蘇生アイテムを使う判断が常に「使う」側に倒れる"
            % before)

    # ダウンは action 側の入口（act_enter_down）から入れる。状態の数値を
    # テストが直接書くと、action-dev が語彙を変えた日に**テストだけが正しく動く**。
    s.call("act_enter_down", x=player)
    s.settle()
    after = s.get("ai_sit")
    r.check("操作キャラがダウンすると ai_sit の bit0 (AI_SIT_LEADER_DOWN) が立つ",
            bool(after & 1),
            "ai_sit=$%02X（ent_state=%d）。猶予中は**プレイヤーの操作が一切効かない**ので"
            "（ADR-0006）、事態を変えられるのは自律仲間だけである。AI が「操作キャラが"
            "倒れている」を認識していなければ、P2 の蘇生アイテム（ADR-0007）を使う判断が"
            "書けず、猶予がそのまま シナリオ失敗 になる。ai.s の ai_sense を見よ"
            % (after, s.get("ent_state", player)))

    res = s.call("action_revive", x=player)
    s.settle()
    revived = s.get("ai_sit")
    r.check("復帰すると ai_sit の bit0 が下りる", not res.carry and not (revived & 1),
            "復帰の戻り値 carry=%d（0 なら復帰した）、ai_sit=$%02X、ent_state=%d。"
            "認識が下りないと、立ち上がった後も AI が「操作キャラは倒れたまま」と"
            "思い続ける（P2 で蘇生アイテムを二重に使う）"
            % (res.carry, revived, s.get("ent_state", player)))


# ---------------------------------------------------------------- 追従
def _check_regroup(rom_path, labels, profiles, r):
    """敵が居なくなった後の立ち位置。**揺らぎ（意図）と震え（制御の失敗）を区別する。**

    ここはもともと「落ち着いたら静止する」＝ 末尾のフレームの距離が1種類だけ、と
    書いてあった。
    だがそれは主が「やめろ」と言った挙動そのものを不変条件として固定していた:

        「敵を排除し終えた後、AI キャラクターが棒立ちになるというところでも、
          能動的に動く仲間という感じがしない」（主の指摘）

    ai-dev はこれを受けて、立ち位置を -1/0/+1/+2 段に振る**揺らぎ**（TSV の
    wander / dwell）と、歩き出しの**遅れ**（react）を入れた。仲間は何秒かに一度
    立ち位置を引き直し、そこまで歩いて、また何秒か止まる。**動くのが正しい。**

    では何を見張るのか。この節が本当に捕まえたかったのは
    **「AI_DEADBAND = 0 による毎フレームの震え」**である（節の名前が「震えない」）。
    揺らぎと震えは、画面でも数値でも次の3点で別物である:

        揺らぎ … 引き直した瞬間にだけ行き先が変わる。そこまでの歩きは**単調**で、
                 着いたら次の引き直しまで**1ドットも動かない**
        震え   … 行き先は変わっていないのに**往復**する。止まっている時間が無い

    したがって主張は「動くな」ではなく、次の4つである。
    どれも wander / dwell / react / speed を振っても成立する形にしてある。

      1. 引き直しと引き直しの間、歩きは**単調**である（往復しない）
      2. 引き直しと引き直しの間に、**必ず静止する時間がある**
      3. 落ち着いた位置が、そのとき狙っている段（ai_woff から作った ai_gap）と一致する
      4. 揺らいでも追従距離 follow_x のまわり（-1〜+2 段）から出ない

    さらに、**棒立ちに戻っていないこと**（引き直しが来て、実際に足が出ること）も
    見る。ここが死ぬと主の指摘した不具合がそのまま戻るが、
    tail が1種類であることを要求していた昔の形では、それが**合格**になっていた。
    """
    r.section("敵が居なくなった後の立ち位置（追従距離 follow_x と揺らぎ wander）")
    slots = slot_ranges(labels)
    player, ally = slots["player"], slots["ally_first"]

    # 期待値はすべて TSV（正本）と ai_params.inc（場の規則）から作る。焼き付けない。
    follow_x = profiles["follow_x"][0]
    wander = profiles["wander"][0]
    dwell = profiles["dwell"][0]
    react = profiles["react"][0]
    speed = profiles["speed"][0]
    A = ai_defs()
    deadband = A["AI_DEADBAND"]
    jitter = A["AI_WANDER_JITTER"]
    gap_min = A["AI_GAP_MIN"]
    clear_x = A["AI_CLEAR_X"]           # 操作キャラの専有距離（壁にならないための場の規則）
    goal_regroup = A["AI_GOAL_REGROUP"]

    # 1フレームに進むドット数（1/16 ドット刻み）。速さを変えても空振りしない。
    step = max(1, speed // 16 + (1 if speed % 16 else 0))
    # 思考の順番待ち: 自律枠を AI_THINK_PER_FRAME 体ずつ巡るので、行き先が変わってから
    # 本人が気付くまで最大これだけかかる。
    think_lag = _ceil_div(A["ENT_FREE_FIRST"] - A["ENT_ALLY_FIRST"],
                          max(1, A["AI_THINK_PER_FRAME"]))
    # 引き直しで行き先が動く最大幅は -1 段 → +2 段 の 3 段ぶん。
    travel = _ceil_div(3 * wander * 16, max(1, speed)) + 1
    # 「引き直してから落ち着くまで」に許す長さ。これを超えて動いていたら震えている。
    allow = think_lag + react + travel + 2
    period = dwell + jitter                     # 引き直しの周期の上限（AI_WANDER_JITTER で散る）

    # 完全な区間（引き直しから次の引き直しまで）を2つは見たい。周期は dwell に
    # AI_WANDER_JITTER ぶん散らされるので、3周期ぶん回して端の欠けた区間を捨てる。
    # dwell を大きくされても走行時間が延び続けないよう頭で止める（CI が遅いと誰も回さない）。
    FRAMES = min(3 * period + allow + 20, 460)

    s = Scene(rom_path, labels)
    s.sleep(slots["enemy_first"], slots["count"])        # 敵を全滅させた状態にする
    player_y = s.y(player)                               # 操作キャラは動かない（入力なし）

    def watch(sc):
        return (abs(sc.x(player) - sc.x(ally)), sc.y(ally),
                _signed(sc.get("ai_woff", ally)), sc.get("ai_gap", ally),
                sc.get("ai_goal", ally))

    # 作業変数（ai_woff / ai_gap）を読むので、**そのフレームの更新が終わった点**で観測する。
    rows = s.trace(FRAMES, watch)

    # 追従に入る前（思考の順番が回ってくるまで）の数フレームは捨てる。
    first = next((i for i, row in enumerate(rows) if row[4] == goal_regroup), None)
    if not r.check("敵が居なくなったら仲間が操作キャラへの追従（AI_GOAL_REGROUP）に入る",
                   first is not None,
                   "%d フレーム走らせても ai_goal が REGROUP(%d) にならない（最後の値 %d）。"
                   "狙う相手が居なくなったら操作キャラの側へ戻るのが万能型の約束である"
                   "（leash_x=%d が 0 だと戻らない）。戻らないと、次の戦闘が始まったときに"
                   "仲間が画面外に取り残される"
                   % (FRAMES, goal_regroup, rows[-1][4] if rows else -1,
                      profiles["leash_x"][0])):
        return
    rows = rows[first:]
    dists = [row[0] for row in rows]

    # ---- 揺らがない型（wander か dwell が 0）は、止まったまま動かないのが正しい ----
    if wander == 0 or dwell == 0:
        tail = dists[-(allow + 4):]              # 落ち着くのに要る長さのぶんだけ後ろを見る
        r.check("揺らがない設定（wander=%d / dwell=%d）では仲間が完全に静止する"
                % (wander, dwell), len(set(tail)) == 1,
                "落ち着いたはずの %d フレームで操作キャラとの距離が %s と動き続けている。"
                "wander か dwell が 0 の型は立ち位置を引き直さないので、"
                "追従距離に着いたら1ドットも動かないはずである。動くなら、"
                "目標の立ち位置と実際の位置が行き過ぎ／戻りを繰り返している"
                "（AI_DEADBAND が 0 だと 1/16 ドットの端数で毎フレーム震える）"
                % (len(tail), sorted(set(tail))))
        return

    # ---- 揺らぐ型: 区間（引き直しから次の引き直しまで）に切って見る ----
    rerolls = [i for i in range(1, len(rows)) if rows[i][2] != rows[i - 1][2]]
    spans = list(zip(rerolls, rerolls[1:]))          # 端の欠けた区間は使わない

    moved = sum(1 for a, b in zip(dists, dists[1:]) if a != b)
    r.check("立ち位置の引き直しが来て、仲間が実際に足を出している（棒立ちに戻っていない）",
            len(spans) >= 1 and moved > 0,
            "%d フレームで引き直しは %d 回、距離が変わったフレームは %d。"
            "**主が「やめろ」と言ったのはこの棒立ちである**"
            "（「敵を排除し終えた後、AI キャラクターが棒立ちになる」）。"
            "TSV の wander=%d / dwell=%d が読まれていないか、引き直し（ai_reroll_wander）が"
            "同じ段を引き続けて一歩も動いていない。dwell の周期は最大 %d フレームなので、"
            "%d フレーム観測して1度も引き直しが来ないのは不具合である。"
            "**1段の幅 wander=%d は立ち位置の許容誤差 AI_DEADBAND=%d より確実に広く取ること**"
            "（同じくらいだと、引き直しても許容誤差に飲まれて一歩も動かない）"
            % (len(rows), len(rerolls), moved, wander, dwell, period, len(rows),
               wander, deadband))
    if not spans:
        return

    # --- 1. 往復しない（震えの決定的な特徴は往復である）---
    wobble = []
    for a, b in spans:
        signs = [_sign(y - x) for x, y in zip(dists[a:b], dists[a + 1:b + 1])]
        signs = [g for g in signs if g]
        if len(set(signs)) > 1:
            wobble.append((a, b, dists[a:b + 1]))
    r.check("引き直しから次の引き直しまで、仲間の歩きが**単調**である（往復しない / %d 区間）"
            % len(spans), not wobble,
            "%d 区間で往復した。最初の区間 [%s]: 距離が %s と行き来している。"
            "行き先（ai_woff）が変わっていないのに向きが変わるのは**震え**であって"
            "揺らぎではない。目標の立ち位置と実際の位置が行き過ぎ／戻りを繰り返している"
            "（立ち位置の許容誤差 AI_DEADBAND はいま %d。0 だと 1/16 ドットの端数で"
            "毎フレーム震える。1フレームの歩き %d/16 ドットが許容誤差より大きいときも、"
            "そこをまたいで往復する）"
            % (len(wobble), "%d-%d" % wobble[0][:2] if wobble else "",
               wobble[0][2][:24] if wobble else [], deadband, speed))

    # --- 2. 区間ごとに必ず静止する（震えには止まっている時間が無い）---
    busy = []
    for a, b in spans:
        still = _runs_of([dists[i] == dists[i + 1] and rows[i][1] == rows[i + 1][1]
                          for i in range(a, b)])[0]
        need = max(1, (b - a) - allow)
        if still < need:
            busy.append((a, b, still, need))
    r.check("引き直しと引き直しの間に、仲間が静止している時間がある（%d 区間 / 震えない）"
            % len(spans), not busy,
            "%d 区間で静止しなかった。最初の区間 [%d-%d] は %d フレームのうち"
            "連続して止まっていたのが最長 %d フレーム（期待 %d 以上）。"
            "立ち位置を引き直してから落ち着くまでに許した猶予は %d フレーム"
            "（思考の順番待ち %d ＋ 歩き出しの遅れ react=%d ＋ %d 段ぶんの歩き %d ＋ 2）。"
            "止まる時間が無いのは、意図して歩いているのではなく**震えている**という意味である"
            % (len(busy), busy[0][0] if busy else 0, busy[0][1] if busy else 0,
               (busy[0][1] - busy[0][0]) if busy else 0,
               busy[0][2] if busy else 0, busy[0][3] if busy else 0,
               allow, think_lag, react, 3, travel))

    # --- 3. 落ち着いた位置が、そのとき狙っている段と一致する ---
    # ai_woff（符号つきの揺らぎ）と ai_gap（揺らぎを足した後の間合い）は ai-dev が
    # .export している。**テスト側から「いまどの段を狙っているか」を読んで期待値を作る。**
    #
    # ただし ai_gap は**立ち位置そのものではない**。絵が縦に重なる奥行きに居る仲間は、
    # 操作キャラの専有距離（AI_CLEAR_X）の内側に立たない——壁にならないための場の規則が
    # 揺らぎより後に効く（ai_clear_player_zone）。内向きの揺らぎで ai_gap が専有距離を
    # 下回ったときは、立つのは専有距離の外側である。
    tol = deadband + step
    off = []
    for a, b in spans:
        d, gap = dists[b], rows[b][3]
        crowded = abs(rows[b][1] - player_y) < A["AI_OVERLAP_Y"]
        want = max(gap, clear_x) if crowded else gap
        if abs(d - want) > tol:
            off.append((b, d, gap, want, _signed(rows[b][2])))
    r.check("落ち着いた立ち位置が、そのとき狙っている段（ai_gap と専有距離 %d）と "
            "%d ドット以内で一致する" % (clear_x, tol), not off,
            "%d 区間でずれた。最初は %d フレーム目で 距離 %d / ai_gap %d → 期待 %d "
            "（揺らぎ ai_woff=%d 段ぶん）。許容は不感帯 AI_DEADBAND=%d ＋ 1フレームの歩き %d。"
            "決めた立ち位置と実際に立った位置が合っていない＝"
            "歩き（ai_move_to_post）が立ち位置を目指していないか、"
            "揺らぎ（ai_apply_wander）が間合いに足されていない"
            % (len(off), off[0][0] if off else 0, off[0][1] if off else 0,
               off[0][2] if off else 0, off[0][3] if off else 0,
               off[0][4] if off else 0, deadband, step))

    # --- 4. 揺らいでも追従距離そのものは壊れない ---
    # 揺らぎは follow_x からの**ずれ**であって、間合いの作り直しではない。
    # -1 段 〜 +2 段（TSV の約束）の外に出るなら、揺らぎが間合いを食い潰している。
    lo, hi = max(gap_min, follow_x - wander), follow_x + 2 * wander
    band = [(i, row[3]) for i, row in enumerate(rows) if not lo <= row[3] <= hi]
    r.check("狙う間合いが追従距離 follow_x=%d の -1〜+2 段（%d〜%d ドット）に収まる"
            % (follow_x, lo, hi), not band,
            "%d フレームで外れた（最初は %d フレーム目の ai_gap=%d、許容 %d〜%d）。"
            "揺らぎ wander=%d は follow_x からの**ずれ**であって間合いの置き換えではない。"
            "外れるなら、引き直しが -1/0/+1/+2 段の外を引いている（ai_reroll_wander）か、"
            "分散（ai_spread）が追従中にも足されている（この場に仲間は1体しか居ない）"
            % (len(band), band[0][0] if band else 0, band[0][1] if band else 0,
               lo, hi, wander))


# ---------------------------------------------------------------- プロファイル番号
def _check_profile_range(rom_path, labels, profiles, r):
    """ent_ai に範囲外の値が入っても、表の外を引かないこと。

    ent_ai は ent_clear_all が消さない箱なので、配置側（P3 の waves / P4 の roster）が
    詰め忘れると前のシーンの値や未初期化の値が残る。$FF で引けば表の 255 バイト先、
    つまり**別の表の中身を速さや攻撃性として読む**ことになる。
    """
    r.section("ent_ai が範囲外でも表の外を引かない")
    slots = slot_ranges(labels)
    ally, enemy = slots["ally_first"], slots["enemy_first"]
    rows = len(profiles["speed"])
    FRAMES = 60

    def trace(profile_value):
        s = Scene(rom_path, labels)
        s.put("ent_ai", ally, profile_value)
        return s.hold(set(), FRAMES,
                      lambda sc: (sc.x(ally), sc.get("ent_y", ally),
                                  sc.get("ai_goal", ally), sc.get("ai_gap", ally)))

    base = trace(0)
    for value, why in ((rows, "AI_PROFILE_COUNT ちょうど（境界の外側）"),
                       (0xFF, "$FF（未初期化の値）")):
        got = trace(value)
        diff = [i for i, (a, b) in enumerate(zip(base, got)) if a != b]
        r.check("ent_ai = %d %s でも 0 行目の型として動く（%d フレーム）" % (value, why, FRAMES),
                not diff,
                "profile 0 と %d フレーム目から挙動が分かれた（0行目=%s / ent_ai=%d のとき=%s）。"
                "表は %d 行しかないので、範囲外をそのまま添字に使うと**表の外**の "
                "バイトを速さ・間合い・攻撃性として読む。ai.s の ai_load_profile が "
                "`cmp #AI_PROFILE_COUNT / bcc` で 0 行目に倒していることを確かめよ"
                % (diff[0] if diff else -1, base[diff[0]] if diff else None,
                   value, got[diff[0]] if diff else None, rows))

    # 配置側が詰め忘れたときの安全柵（ai_init_entity が立場ごとの既定値で埋める）。
    s = Scene(rom_path, labels)
    nes = s.nes
    defaults = [nes.read(labels["ai_default_profile"] + i) for i in range(3)]
    filled = []
    for slot, role, name in ((ally, 1, "仲間"), (enemy, 2, "敵")):
        s.put("ent_ai", slot, 0xFF)
        s.call("ai_init_entity", x=slot)
        filled.append((name, slot, s.get("ent_ai", slot), defaults[role]))
    bad = ["%s(#%d) が %d（期待 %d）" % f for f in filled if f[2] != f[3]]
    r.check("ai_init_entity が範囲外の ent_ai を立場ごとの既定値で埋める %s"
            % [(f[0], f[2]) for f in filled], not bad,
            "%s。ai_default_profile=%s（添字は ACT_ROLE_PLAYER/ALLY/ENEMY）。"
            "埋めないと、詰め忘れたエンティティが範囲外の行のまま動き続ける"
            % ("／".join(bad), defaults))

    # 埋めてよいのは範囲外だけである。データ側が決めた値を上書きしてはならない。
    keep = rows - 1
    s.put("ent_ai", ally, keep)
    s.call("ai_init_entity", x=ally)
    r.check("ai_init_entity は範囲内の ent_ai (=%d) を書き換えない" % keep,
            s.get("ent_ai", ally) == keep,
            "ent_ai が %d → %d に書き換えられた。型を決めるのは data 側"
            "（roster.tsv / waves）であって ai.s ではない（CLAUDE.md 第1条）"
            % (keep, s.get("ent_ai", ally)))


# ---------------------------------------------------------------- のけぞり
def _check_hurt_blocks_input(rom_path, labels, profiles, r):
    """のけぞり中は入力を受け付けないこと（仕様）。

    engine の奥行き検証から敵を取り除いた（l2_engine.py / run_tests.py）ぶん、
    「戦闘中は奥行きを動かせない」という**仕様の側**をここで名指しで押さえる。
    見ているのは「入力が落ちる」ではなく「のけぞり中は受け付けない」であり、
    のけぞりが明けたら同じ入力が通ることまでを1組にしてある。

    **レーンは ADR-0009 で廃止した。**この節はもともと ent_lane（レーン番号が
    変わらないこと）で書いてあったが、主張はレーンに依らないので足元Y（ent_y）で
    書き直した。押した量に応じて連続に動くようになったぶん、「1ドットも動かない」を
    見ればよい。奥行きの速さ（ACT_DEPTH_SPEED）は主のノブなので、押すフレーム数は
    その値から導いて焼き付けない。
    """
    r.section("のけぞり中は入力を受け付けない（ADR-0006 の猶予とは別物）")
    slots = slot_ranges(labels)
    player = slots["player"]
    s = Scene(rom_path, labels)
    disarm_enemies(s.nes, labels)          # 殴る相手と時機をこちらで決める
    s.settle()

    # 1ドット動くのに要るフレーム数（1/16 ドット/f 刻み）。速さを下げても空振りしない。
    push = max(2, 16 // max(1, D["ACT_DEPTH_SPEED"]) + 1)
    depth_lo = s.get("depth_y_min")
    y0, x0 = s.y(player), s.x(player)
    if not r.check("のけぞりの検証を始める前に操作キャラが待機状態で静止している",
                   s.get("ent_state", player) == ACT_ST_IDLE and y0 > depth_lo,
                   "ent_state=%d / ent_y=%d（歩ける帯の奥端は %d）。待機状態で、"
                   "奥へまだ動ける位置から始める"
                   % (s.get("ent_state", player), y0, depth_lo)):
        return

    # ノックバック 0 で1発入れる。位置が動かないので「入力で動いたか」だけを見られる。
    s.put("act_kb_dir", 0, 0)
    s.put("act_kb_amt", 0, 0)
    s.put("act_atk_ent", 0, player)
    s.call("act_damage", a=1, x=player)
    hurt_state = s.get("ent_state", player)
    r.check("1発もらうと待機状態から出る（のけぞり）",
            ACT_ST_IDLE < hurt_state < ACT_ST_DOWN,
            "ent_state=%d（期待は待機(%d)とダウン(%d)の間）。act_damage がのけぞりに"
            "遷移させていない" % (hurt_state, ACT_ST_IDLE, ACT_ST_DOWN))

    # のけぞり（＋ヒットストップ）が明けるまで、UP と RIGHT を押しっぱなしにする。
    # **押している間ずっと**足元YもワールドXも1ドットも動かないこと。
    # 記録するのは「そのフレームの更新が終わった時点の (状態, 足元Y, X)」である。
    # のけぞりが明けるフレームは、明けた**その同じフレーム**に入力が通る
    # （act_update_actor が待機へ戻してから action_update_player が入力を読む）。
    # そこで動くのは不具合ではないので、判定からは外す。外さずに数えると
    # **action が正しいまま落ちる**検証になる。
    samples = []
    waited = 0
    LIMIT = 90
    while s.get("ent_state", player) != ACT_ST_IDLE and waited < LIMIT:
        s.hold({"UP"}, 1)
        frame_end(s.nes, labels)
        samples.append((s.get("ent_state", player), s.y(player), s.x(player)))
        waited += 1
    s.nes.set_buttons(set())
    states = [st for st, _, _ in samples]
    ys = [y for st, y, _ in samples if st != ACT_ST_IDLE]
    xs = [x for st, _, x in samples if st != ACT_ST_IDLE]

    r.check("のけぞり中に UP を押し続けても足元Y（奥行き）が1ドットも動かない"
            "（%d フレーム観測）" % len(ys),
            bool(ys) and set(ys) == {y0},
            "足元Y %d から %s と動いた（状態の推移=%s）。のけぞり・ヒットストップ中は"
            "操作を受け付けないのが仕様であり、受け付けると被弾のたびに奥行きが飛ぶ"
            "（action_update_player が ACT_ST_IDLE 以外で戻っているか、"
            "act_player_depth が状態を見ずに呼ばれている）"
            % (y0, sorted(set(ys)), states))
    r.check("のけぞり中は横にも動かない（ノックバック 0 で殴っている）",
            bool(xs) and set(xs) == {x0},
            "ワールドX %d から %s と動いた。ノックバック初速 0 で殴ったので、"
            "動いたなら入力が通っている" % (x0, sorted(set(xs))))

    if not r.check("のけぞりが %d フレーム以内に明ける" % LIMIT,
                   s.get("ent_state", player) == ACT_ST_IDLE,
                   "%d フレーム待っても ent_state=%d のまま。のけぞりから待機へ戻らないと"
                   "操作が永久に効かない" % (waited, s.get("ent_state", player))):
        return

    # 明けたら同じ入力が通ること（塞がりっぱなしでない）。
    s.hold({"UP"}, push)
    frame_end(s.nes, labels)
    s.nes.set_buttons(set())
    y_after = s.y(player)
    r.check("のけぞりが明けたら同じ UP が通り、足元Yが奥へ動く",
            y_after < y0,
            "足元Y %d → %d（%d フレーム押した。奥へ＝Yが小さくなる向き）。"
            "のけぞり中に入力を受け付けないことと、のけぞりが明けても受け付けないことは"
            "別物である。後者なら操作不能の不具合"
            % (y0, y_after, push))


# ---------------------------------------------------------------- ヘイト
def _hate_score(dist, hate, mid):
    """ai_select_target が付ける「遠さ'」を Python で写したもの。

        遠さ' = 遠さ - (hate - AI_HATE_MID)     （0..255 で頭打ち）

    ai.s 側はこれを 8bit の桁借りで場合分けせずに計算している（走査の内側なので
    jsr も分岐も削ってある）。**テスト側は約束の式の方を持つ。**
    実装の写しを持つと、実装が間違ったときにテストも同じだけ間違う。
    """
    return max(0, min(255, dist - (hate - mid)))


def _predict_target(cands, hates, mid):
    """候補 [(番号, 遠さ)] のうち、ai_select_target が選ぶはずの相手。

    同点なら番号の小さい方（走査は番号順で、同点は先に見た方を残す）。
    """
    best, best_score = None, 256
    for slot, dist in cands:
        score = _hate_score(dist, hates[slot], mid)
        if score < best_score:
            best, best_score = slot, score
    return best


def _check_hate(rom_path, labels, profiles, r):
    """ヘイト（狙われやすさ）の目盛り。**P1 では全行が中立なので画面は何も変わらない。**

    それでも今ここで縛るのは、P2 で6型に列を埋めた瞬間に役割分担（タンクが狙いを集め、
    遠隔型が狙われにくい）が出る**足場**だからである。値を入れてから
    「効かない／効きすぎる」を調べるのでは、原因が型の設計か算数かを切り分けられない。

    見るのは2つ:
      1. 中立（AI_HATE_MID = 128）では、狙われ方が**距離だけ**で決まる（素通りする）
      2. 値を振ると、距離が同じでも狙われ方が変わる（大きいほど近くに見える）

    2 のために、**ROM の ai_p_hate の1バイトだけを書き換えた写し**を作って起動する。
    いまの TSV には中立以外の行が無いので、そうしないと「振ったら変わる」を
    一度も通らないまま P2 に入ることになる（l2_action の _probe_depth_tol と同じ考え方）。
    """
    r.section("ヘイト（狙われやすさ。AI_HATE_MID が中立 / P2 に向けた足場）")

    A = ai_defs()
    mid = A["AI_HATE_MID"]
    slots = slot_ranges(labels)
    player, ally, enemy = slots["player"], slots["ally_first"], slots["enemy_first"]
    aggr = profiles["aggr_x"][1]            # 敵の攻撃性。これより遠い相手には食いつかない

    # 距離の差と、ヘイトの振り幅。振り幅は「距離の差を確実に覆す」大きさにする。
    GAP = A["AI_SPREAD"]
    NEAR = 40
    DELTA = 2 * GAP

    def aim(scene, d_player, d_ally, ally_profile=0):
        """敵に1回だけ考えさせ、誰を狙ったかを返す。

        思考は1フレームに AI_THINK_PER_FRAME 体ぶんしか回らないので、
        ai_cursor（.export されている）を敵の1つ手前に置いて順番を作る。
        こうすると**1フレームで**狙いが決まり、観測の間に誰も歩かない。
        """
        scene.sleep(enemy + 1, slots["count"])       # 敵は1体だけにする
        ex, y = 200, scene.y(player)
        for i, x in ((enemy, ex), (player, ex - d_player), (ally, ex - d_ally)):
            scene.set_x(i, x)
            scene.put("ent_y", i, y)
            scene.put("ent_state", i, ACT_ST_IDLE)
        scene.put("ent_ai", ally, ally_profile)
        scene.put("ai_cursor", 0, enemy - 1)
        scene.call("ai_update")
        return scene.get("ai_target", enemy)

    def expect(d_player, d_ally, hates):
        return _predict_target([(player, d_player), (ally, d_ally)], hates, mid)

    # --- 1. 中立: 狙われ方は距離だけで決まる ---
    # P1 の表は全行が中立なので、ここは「近い方を狙う」になる。P2 で列が埋まったら
    # **主張の形が変わる**（近い方ではなく、目盛りを織り込んだ式どおりになる）ので、
    # 期待値は式（_hate_score）から作り、名前の方で今どちらを見ているかを出す。
    base_hates = {player: profiles["hate"][0], ally: profiles["hate"][0]}
    neutral = base_hates[player] == base_hates[ally] == mid
    s = Scene(rom_path, labels)
    cases = []
    for d_player, d_ally, why in ((NEAR, NEAR + GAP, "操作キャラの方が近い"),
                                  (NEAR + GAP, NEAR, "仲間の方が近い")):
        got = aim(s, d_player, d_ally)
        want = expect(d_player, d_ally, base_hates)
        if got != want:
            cases.append("%s（%d ドット vs %d ドット）のに #%d を狙った（期待 #%d）"
                         % (why, d_player, d_ally, got, want))
    r.check("ヘイトが中立（%d）のあいだ、敵は**近い方**を狙う（ヘイトの項が素通りする）" % mid
            if neutral else
            "敵が狙う相手が「遠さ - (hate - %d)」の式どおりに決まる"
            "（TSV の hate: 操作キャラ側=%d / 仲間側=%d）"
            % (mid, base_hates[player], base_hates[ally]),
            not cases,
            "%s。TSV の hate は 操作キャラ側=%d / 仲間側=%d（中立は %d）。"
            "中立の行で狙いが距離と食い違うなら、目盛りの中点がずれているか、"
            "(hate - %d) の符号が逆になっている（低いはずの者が狙われる）"
            % ("／".join(cases), base_hates[player], base_hates[ally], mid, mid))

    # --- 2. 値を振ると狙われ方が変わる ---
    if "ai_p_hate" not in labels:
        return
    probe = Nes(rom_path)
    probe.reset()
    offset = probe.mapper.prg_offset(labels["ai_p_hate"])
    with open(rom_path, "rb") as f:
        image = bytearray(f.read())
    head = len(image) - len(probe.rom.prg) - len(probe.rom.chr)

    # 書き換える行は**仲間と別の行**でなければ差が出ない（同じ行なら両者とも動く）。
    # P1 の表は2行しか無いので、敵の行（1）を借りて仲間に着せる。
    row = min(len(profiles["hate"]) - 1, 1)
    if not r.check("ヘイトを振る実験に使える行が2行以上ある", row >= 1,
                   "ai_params.s の表が %d 行しかない。仲間と操作キャラに別々のヘイトを"
                   "与えられないので、「振ったら変わる」を確かめられない"
                   % len(profiles["hate"])):
        return

    import tempfile                        # noqa: PLC0415 — この節だけで使う
    with tempfile.TemporaryDirectory() as tmp:
        swings = []
        for value, d_player, d_ally, why in (
                (mid + DELTA, NEAR, NEAR + GAP, "遠くに居ても狙われる（タンク）"),
                (mid - DELTA, NEAR + GAP, NEAR, "近くに居ても狙われない（遠隔型）")):
            image[head + offset + row] = value & 0xFF
            path = os.path.join(tmp, "hate_%d.nes" % value)
            with open(path, "wb") as f:
                f.write(image)
            ps = Scene(path, labels)
            # このプローブが本物を測っていることの自己検査（書き換えが効いているか）。
            seen = ps.nes.read(labels["ai_p_hate"] + row)
            if seen != value & 0xFF:
                r.check("ROM の ai_p_hate を書き換えた写しが起動する", False,
                        "書き換えた ROM で ai_p_hate[%d] が %d と読めた（書いたのは %d）。"
                        "PRG のバンク割り当てが変わって、別の場所を書き換えている"
                        % (row, seen, value))
                return
            got = aim(ps, d_player, d_ally, ally_profile=row)
            hates = {player: profiles["hate"][0], ally: value}
            want = expect(d_player, d_ally, hates)
            if got != want:
                swings.append("hate=%d（中立 %+d）で %s はずが #%d を狙った（期待 #%d / "
                              "距離は 操作キャラ %d・仲間 %d ドット）"
                              % (value, value - mid, why, got, want, d_player, d_ally))
        r.check("ヘイトを中立から %+d / %+d に振ると、距離が同じでも狙われ方が変わる"
                % (DELTA, -DELTA), not swings,
                "%s。約束は「遠さ' = 遠さ - (hate - %d)」で、ヘイトの高い者は"
                "**実際より近くに居るものとして**測られる（攻撃性 aggr_x=%d と同じ物差し）。"
                "ここが効かないと、P2 で庇護型に狙いを集める設計が列を埋めても動かない"
                % ("／".join(swings), mid, aggr))


# ================================================================ 仲間の確認モード
# src/debug.inc の dbg_ally（SELECT で 0 → 1 → 2 → 0）。**仕様ではない。**
# 主が「1対1の間合い」を見るための仕掛けで、DBG_ENABLE = 0 でコードごと消える。
#
#   1 = 攻撃しない ／ 2 = 休ませる（思考も移動もしない＋**敵の標的選択から外れる**）
#
# ここで見るのは、その 2（休ませる）が**主の求めるものになっているか**である。
#   * 敵が休んでいる仲間に張りつくと、結局1対1の間合いは見られない（実質1対3のまま）。
#   * だからといって仲間を完全に凍らせると、操作キャラが乗り上げたまま抜けられない
#     ＝ai-dev の最優先の不具合「自律仲間が壁になって進行不能にする」そのものになる。
# この2つは互いに引っぱり合うので、両方を**同時に**見張る。
#
# **モードは変数に直接書かず、実際に SELECT を押して入る**（test/dbgmode.py）。
# 押下を数フレーム保つのは、モードの切り替えが論理フレームでしか読まれないからである。

# dbg_ally の値の意味は src/ai/ai.s が正本（engine は値を回すだけで解釈しない）。
# テストに 2 と書かずにそこから読む。
AI_DBG = defs("src/ai/ai.s", "src/ai/ai_params.inc", "src/ai/ai.inc", "src/constants.inc")

# 標的の観測フレーム数。敵は数十フレームおきに考え直すので、
# 「たまたま狙われなかった」では済まない長さを取る。
REST_TARGET_FRAMES = 400
# 対照（通常モードで仲間が狙われること）の観測フレーム数。こちらは短くてよい。
# 「一度も無い」を示すには長い窓が要るが、「ある」を示すには最初の1回で足りる。
# 実測では敵は**観測の初回フレームから**仲間を標的にしている。
# ここを縮めたのは回帰テストの実行時間のためである（CI が遅いと誰も回さなくなる）。
# 主張そのもの（上の 400 フレーム）は縮めていない。
REST_CONTROL_FRAMES = 120


def _rest_mode():
    return AI_DBG["DBG_ALLY_REST"]


def _watch_enemy_targets(rom_path, labels, want_mode, frames):
    """dbg_ally = want_mode にして frames 進め、敵が誰を狙ったかを数える。

    戻り値: (実際に入れたモード, {標的の番号: 観測フレーム数}, 仲間が生存していたフレーム数)
    """
    s = Scene(rom_path, labels)
    mode = dbgmode.to_mode(s, "SELECT", "dbg_ally", want_mode)
    slots = s.slots
    allies = range(slots["ally_first"], slots["enemy_first"])
    seen = {}
    ally_alive = 0
    enemy_seen = 0
    s.nes.set_buttons(set())
    for _ in range(frames):
        step_frame(s.nes, s.labels, 1)
        if any(s.alive(i) for i in allies):
            ally_alive += 1
        for e in range(slots["enemy_first"], slots["count"]):
            if not s.get("ent_active", e):
                continue
            enemy_seen += 1
            t = s.get("ai_target", e)
            seen[t] = seen.get(t, 0) + 1
    return mode, seen, ally_alive, enemy_seen


def _check_rest_not_targeted(rom_path, labels, profiles, r):
    """休ませた仲間（dbg_ally = 2）が敵の標的に**一度も**現れないこと。"""
    r.section("休ませた仲間は敵の標的にならない（dbg_ally = 2）")

    slots = slot_ranges(labels)
    allies = list(range(slots["ally_first"], slots["enemy_first"]))
    rest = _rest_mode()

    # --- 0. ボタンでモードが巡回するか（結線の検査）---
    wiring = Scene(rom_path, labels)
    seq = dbgmode.cycle(wiring, "SELECT", "dbg_ally", dbgmode.mode_count())
    want_seq = list(range(1, dbgmode.mode_count())) + [0]
    r.check("SELECT を押すと dbg_ally が %s と巡回する（モードに変数で入らない）"
            % " → ".join(str(v) for v in [0] + want_seq), seq == want_seq,
            "SELECT を %d 回押したときの dbg_ally が %s（期待 %s）。"
            "src/main.s の dbg_buttons が PAD_SELECT を読めていないか、dbg_bump の巡回が "
            "DBG_MODE_COUNT と合っていない。**ここが落ちている間、下の主張は"
            "「モードに入れていないまま通った」可能性がある**"
            % (dbgmode.mode_count(), seq, want_seq))

    # --- 1. 対照（dbg_ally = 0 では仲間が実際に狙われる）---
    # これが無いと、下の「狙われない」は**仲間が端から狙われる立場に無い**だけでも通る。
    off_mode, off_seen, off_alive, off_enemies = _watch_enemy_targets(
        rom_path, labels, 0, REST_CONTROL_FRAMES)
    off_hits = sum(off_seen.get(i, 0) for i in allies)
    r.check("対照: 通常（dbg_ally = 0）では敵が仲間を標的にする（比較が空振りでない）",
            off_mode == 0 and off_hits > 0 and off_enemies > 0,
            "dbg_ally=%d、仲間(#%s)を狙っていたのは %d フレーム、敵の観測はのべ %d 体フレーム "
            "（標的の内訳 %s / %d フレーム中）。通常モードで一度も仲間が狙われないなら、"
            "下の「休ませると狙われない」は何も確かめていない。敵が全滅しているか、"
            "ヘイト（ai_p_hate）が仲間を候補から外している"
            % (off_mode, allies, off_hits, off_enemies, sorted(off_seen.items()),
               REST_CONTROL_FRAMES))

    # --- 2. 主張（dbg_ally = 2 では一度も狙われない）---
    mode, seen, alive, enemies = _watch_enemy_targets(
        rom_path, labels, rest, REST_TARGET_FRAMES)
    hits = sum(seen.get(i, 0) for i in allies)
    r.check("休ませた仲間(#%s)が %d フレームのあいだ敵の ai_target に一度も現れない"
            % (allies, REST_TARGET_FRAMES),
            mode == rest and hits == 0,
            "dbg_ally=%d（期待 %d）で、仲間が標的になっていたのが %d フレーム"
            "（標的の内訳 %s）。休ませた仲間に敵が張りつくと、**主の求める"
            "「1対1の間合い」が見られない**（実質1対3のまま）。ai.s の ai_pick_target が "
            "dbg_ally = %d のときに走査の終端を ENT_ALLY_FIRST まで縮めていない"
            % (mode, rest, hits, sorted(seen.items()), rest))

    # 空振り防止: 仲間が生きていて、敵も居た窓であること。
    # 仲間が早々に倒れていたのなら「狙われない」は当たり前で、何も確かめていない。
    r.check("その窓で仲間が生存し、敵も動いていた（観測が空振りでない）",
            alive >= REST_TARGET_FRAMES // 2 and enemies > 0,
            "仲間の生存 %d フレーム（%d 中、期待は半分以上）、敵の観測のべ %d 体フレーム。"
            "仲間が倒れていれば標的から外れるのは当然で、dbg_ally の門が効いているかは"
            "分からない。休ませた仲間は攻撃されないはずなので、ここが減るのは"
            "「休ませているのに殴られている」疑い"
            % (alive, REST_TARGET_FRAMES, enemies))


# 壁の検証のフレーム数。
REST_STILL_FRAMES = 60      # 休んでいる仲間が自分からは動かないことを見る
REST_PIN_FRAMES = 90        # 操作キャラをワールド左端まで押し込む
REST_WALL_FRAMES = 150      # 乗り上げてから退くまでを見る


def _check_rest_not_a_wall(rom_path, labels, profiles, r):
    """休ませていても（dbg_ally = 2）操作キャラの壁にならないこと。

    休ませる門は「思考も移動もしない」だが、**操作キャラに押されて退くことだけは残る**
    （ai.s の ai_dbg_rest_one → ai_clear_player_zone）。ここを止めると、操作キャラが
    休んでいる仲間に乗り上げたまま抜けられなくなる＝進行不能そのものである。

    いちばん厳しいのは**ワールド左端に押し込んだ**ときである。操作キャラはそれ以上
    左へ逃げられないので、退くのは仲間の側しかない。
    """
    r.section("休ませても壁にならない（dbg_ally = 2）")

    slots = slot_ranges(labels)
    player, ally = slots["player"], slots["ally_first"]
    rest = _rest_mode()

    s = Scene(rom_path, labels)
    disarm_enemies(s.nes, labels)      # 見たいのは押し合いであって戦闘ではない
    mode = dbgmode.to_mode(s, "SELECT", "dbg_ally", rest)

    # --- 0. 休ませる門が本当に効いているか（空振り防止）---
    # 効いていなければ、以下は「いつもの追従」を見ているだけで dbg_ally を検証していない。
    before = (s.x(ally), s.y(ally))
    s.trace(REST_STILL_FRAMES, lambda _s: None)
    after = (s.x(ally), s.y(ally))
    r.check("休ませた仲間は、離れて立っているあいだ1ドットも動かない（門が効いている）",
            mode == rest and after == before,
            "dbg_ally=%d（期待 %d）で、仲間が (%d,%d) から (%d,%d) へ動いた。"
            "**ここが動いている間、下の「退く」は普段の追従を見ているだけである。**"
            "ai.s の ai_act_one の門が dbg_ally を読めていないか、ai_dbg_rest_one が"
            "立ち位置を現在地に置いていない（置かないと、休ませても元の立ち位置へ歩き出す）"
            % (mode, rest, before[0], before[1], after[0], after[1]))

    # --- 1. 操作キャラをワールド左端へ押し込む ---
    xs = s.trace(REST_PIN_FRAMES, lambda t: t.x(player), buttons={"LEFT"})
    pinned = len(set(xs[-10:])) == 1
    r.check("操作キャラがワールド左端に達してそれ以上左へ行けない（最悪の場を作れた）",
            pinned,
            "直前 10 フレームの x が %s。左端に貼り付いていないなら、"
            "操作キャラは単に仲間から歩き去れる＝**退く側が居なくても重なりは解ける**ので、"
            "下の検証は最悪の場を見ていない" % xs[-10:])

    # --- 2. その真上に休んでいる仲間を置き、押し続ける ---
    # 乗り上げた瞬間を作るための配置である（休んでいる仲間は自分から寄ってこないので、
    # 「操作キャラが歩いて乗り上げた」状態をここで作る）。
    s.set_x(ally, s.x(player))
    s.put("ent_y", ally, s.y(player))

    def watch(t):
        both = t.alive(player) and t.alive(ally)
        return (both, t.near_depth(player, ally), abs(t.x(player) - t.x(ally)))

    samples = s.trace(REST_WALL_FRAMES, watch, buttons={"LEFT"})
    overlap = [(both and same and d < OVERLAP_X) for both, same, d in samples]
    longest, total = _runs_of(overlap)

    r.check("乗り上げた状態を実際に観測できた（重なりが1フレーム以上ある）", total >= 1,
            "重なり (|x差| < %d かつ足元Yの差 < %d) が %d フレーム中1度も無かった。"
            "配置した直後に離れているなら、この節は「退く」を一度も見ていない"
            % (OVERLAP_X, DEPTH_OVERLAP_Y, REST_WALL_FRAMES))
    r.check("休ませた仲間に乗り上げても、重なり (|x差| < %d) が %d フレーム以上続かない"
            % (OVERLAP_X, OVERLAP_FRAMES_MAX + 1), longest <= OVERLAP_FRAMES_MAX,
            "重なりが最長 %d フレーム続いた（合計 %d フレーム / %d 中）。"
            "休ませた仲間が**壁になっている**。操作キャラは左端で逃げ場が無いので、"
            "退けるのは仲間の側だけである。ai.s の ai_dbg_rest_one から "
            "ai_clear_player_zone（専有距離の外へ立ち位置を押し出す）への経路が"
            "切れている疑いが濃い。「休ませる = 完全に凍らせる」にすると必ずこうなる。"
            "凍らせてよいのは**思考と攻撃**までで、押されて退く足は残すこと"
            % (longest, total, REST_WALL_FRAMES))
