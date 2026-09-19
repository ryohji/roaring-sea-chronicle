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
from gate import Gate, run_sections, boot_or_fail   # noqa: E402
from srcdefs import action_defs                     # noqa: E402

TSV_PATH = os.path.join(os.path.dirname(HERE), "data", "ai_params.tsv")

# data/ai_params.tsv の列名と、src/ai/ai_params.s の配列ラベルの対応。
# id と name は表に出ない（id は添字そのもの、name は roster から引くための識別子）。
# **レーンは ADR-0009 で廃止した。** lane_hold（次にレーンを移れるまでの待ちフレーム数）は
# depth_spd（奥行きの寄り足の速さ）に意味ごと変わっている（TSV の列の説明を見よ）。
PARAM_COLUMNS = ("speed", "hold_x", "reach_x", "aggr_x",
                 "follow_x", "leash_x", "atk_gap", "depth_spd", "item_pri")

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
        ("敵が居なくなった後の追従", ("ai_goal", "ai_gap"), _check_regroup),
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
    PAIR_FRAMES = 220
    pair = Scene(rom_path, labels)
    # 2体目は操作キャラを挟んで1体目の反対側に置く（1体目と同じ距離だけ右）。
    # 位置をテストに焼き付けず、シーンの配置から作る。
    mirror = pair.x(player) + (pair.x(player) - pair.x(ally))
    ally2 = pair.place_second_ally(mirror, pair.y(player))

    def watch3(s):
        both = s.alive(ally) and s.alive(ally2)
        return (both, s.near_depth(ally, ally2), abs(s.x(ally) - s.x(ally2)))

    pair_samples = pair.hold(set(), PAIR_FRAMES, watch3)
    pair_overlap = [(both and same and d < OVERLAP_X) for both, same, d in pair_samples]
    longest2, total2 = _runs_of(pair_overlap)
    near = [d for both, close, d in pair_samples if both and close]
    r.check("仲間2体がともに生存して奥行きが重なる位置に居る状態を観測できた（%d フレーム）"
            % PAIR_FRAMES, len(near) >= PAIR_FRAMES // 4,
            "両者生存かつ奥行きが重なっていたのは %d フレーム（%d 中）。"
            "2体目の配置（%d ドット）が遠すぎるか、片方が早々に戦線離脱している"
            % (len(near), PAIR_FRAMES, mirror))
    r.check("仲間どうしの重なり (|x差| < %d) が %d フレーム以上続かない"
            % (OVERLAP_X, OVERLAP_FRAMES_MAX + 1), longest2 <= OVERLAP_FRAMES_MAX,
            "重なりが最長 %d フレーム続いた（合計 %d フレーム / %d 中、最小の横距離 %d ドット）。"
            "同じ標的に寄った2体が同じ座標に潰れており、1スキャンライン8スプライトの制約で"
            "片方が消える。ai.s の ai_spread（番号順に AI_SPREAD ずつ後ろへずらす）が"
            "効いていないか、ai_clear_player_zone の押し出しが分散を**上書き**している"
            "（押された仲間が、押されていない仲間の立ち位置に重なる）"
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
    """敵を全滅させた後、仲間が追従距離（follow_x）に落ち着くこと。"""
    r.section("敵が居なくなった後の追従（follow_x）")
    slots = slot_ranges(labels)
    player, ally = slots["player"], slots["ally_first"]
    follow_x = profiles["follow_x"][0]          # 仲間（万能型）の追従距離。TSV が正本

    # 立ち位置の許容誤差（AI_DEADBAND, 既定4）と1フレームの歩き幅を包む余裕。
    # ここは「落ち着いた距離が follow_x のあたりである」ことだけを見たいので、
    # ドット単位で一致することは求めない。
    SETTLE_TOL = 8
    SETTLE_FRAMES = 100
    TAIL = 20

    s = Scene(rom_path, labels)
    s.sleep(slots["enemy_first"], slots["count"])        # 敵を全滅させた状態にする
    s.hold(set(), SETTLE_FRAMES)
    tail = s.hold(set(), TAIL, lambda sc: abs(sc.x(player) - sc.x(ally)))

    r.check("敵が居なくなったら仲間が %d フレーム以内に静止する（立ち位置で震えない）"
            % SETTLE_FRAMES, len(set(tail)) == 1,
            "落ち着いたはずの %d フレームで操作キャラとの距離が %s と動き続けている。"
            "目標の立ち位置と実際の位置が行き過ぎ／戻りを繰り返している"
            "（AI_DEADBAND が 0 だと 1/16 ドットの端数で毎フレーム震える）"
            % (TAIL, sorted(set(tail))))
    r.check("仲間が追従距離 follow_x=%d ドットのあたりに落ち着く（実測 %d）"
            % (follow_x, tail[-1]), abs(tail[-1] - follow_x) <= SETTLE_TOL,
            "操作キャラとの距離が %d ドット（TSV の follow_x=%d、許容 ±%d）。"
            "ai_goal=%d（1 = REGROUP）/ ai_gap=%d。"
            "狙う相手が居なくなったら操作キャラの側へ戻るのが万能型の約束である。"
            "戻らないと、次の戦闘が始まったとき仲間が画面外に取り残される"
            % (tail[-1], follow_x, SETTLE_TOL, s.get("ai_goal", ally), s.get("ai_gap", ally)))


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
