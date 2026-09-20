"""L2 実行検証 — action P1（当たり判定のY許容幅 / 姿勢 / 効果スプライト）。

run_tests.py から呼ばれる。層の分け方は ADR-0004 に従う。

ここで見るのは、P1 の受入で主が指摘した3件に対する action-dev の対応である。

  * 「縦方向にだけ制限がかかる理不尽」→ レーン廃止（ADR-0009）。
    当たり判定は**足元Yの近さ**で行う。その**許容幅の境界**をここで見る。
  * 「攻撃とダメージ硬直の違いがわからない」→ 状態ごとの姿勢（絵）。
    **攻撃中とのけぞり中が別の絵**であることをここで見る。
  * 「攻撃の距離感が掴めない」→ 斬り／衝撃の効果スプライト。
    持続中に出て**寿命で自動的に消える**ことをここで見る。

方針:
  * **値ではなく関係を縛る。** 主の決定（ADR-0009 残る論点 1-a）は
    「Y許容幅 = キャラクターの縦サイズの約 1/3」であって、5 という数値ではない。
    したがって期待値は ROM の act_depth_tol の表から読み、
    さらに「**絵の段数を変えたら許容幅が追随する**」ことを
    ca65 で組み直して確かめる（_check_tol_follows_body）。
  * 調整値（速さ・フレーム数・許容幅・効果の有無）は主が回すノブである。
    **値を変えてテストが落ちてはならない。**期待値は ROM の表と
    src/action/action_params.inc（test/srcdefs.py が読む）から導く。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "harness"))
sys.path.insert(0, HERE)

from cpu6502 import CpuCrash                        # noqa: E402
from nes import Nes, boot, frame_end, load_labels, step_frame   # noqa: E402
from scene import slot_ranges                       # noqa: E402
from gate import Gate, run_sections, boot_or_fail   # noqa: E402
from srcdefs import action_defs, body_rows          # noqa: E402

D = action_defs()

# 足場（シーンを起動してエンティティを置く）に要るラベル。
# **ここは必要最小限にする。**足場に挙げたラベルが1つ欠けると層ごと飛ぶので、
# 「その節だけが落ちる」形が効かなくなる（節ごとの宣言は SECTIONS 側に書く）。
CORE = ("ent_active", "ent_x_lo", "ent_x_hi", "ent_y", "ent_state", "ent_body",
        "ent_tile", "act_hp", "wait_nmi")


class Actors:
    """P1 の動作確認シーンを起動し、当たり判定を単体で叩ける状態にする。"""

    def __init__(self, rom_path, labels):
        self.labels = labels
        self.slots = slot_ranges(labels)
        self.nes = Nes(rom_path)
        self.nes.reset()
        if boot(self.nes) is None:
            raise CpuCrash("起動しても NMI が来ない（OAM DMA が1回も無い）")
        frame_end(self.nes, labels)
        self.player = self.slots["player"]
        self.enemy = self.slots["enemy_first"]

    def get(self, name, i=0):
        return self.nes.ram[(self.labels[name] + i) & 0x7FF]

    def put(self, name, i, value):
        self.nes.ram[(self.labels[name] + i) & 0x7FF] = value & 0xFF

    def rom(self, name, i=0):
        return self.nes.read(self.labels[name] + i)

    def call(self, name, **kw):
        return self.nes.call(self.labels[name], **kw)

    def set_x(self, i, value):
        self.put("ent_x_lo", i, value & 0xFF)
        self.put("ent_x_hi", i, (value >> 8) & 0xFF)

    def x(self, i):
        return self.get("ent_x_lo", i) | (self.get("ent_x_hi", i) << 8)

    def hold(self, buttons, frames, watch=None):
        """buttons を押したまま frames 進め、**各フレームの更新が終わった点**で観測する。

        入力から来る挙動（歩き・小走り・先行入力）は、作業変数（act_vel / act_sub /
        act_buf_atk）を毎フレーム読まないと見えない。run_frames の戻り位置は更新の
        最中なので、必ずフレームの切れ目まで進めてから読む（test/harness/nes.py 冒頭）。
        """
        self.nes.set_buttons(set(buttons))
        out = []
        for _ in range(frames):
            step_frame(self.nes, self.labels, 1)
            if watch is not None:
                out.append(watch(self))
        return out

    def sleep_all(self):
        """全エンティティを眠らせる。見たい2体だけを自分で起こすため。"""
        for i in range(self.slots["count"]):
            self.put("ent_active", i, 0)

    def stand(self, i, x, y, body=0, state=0):
        for name, value in (("ent_active", 0x80), ("ent_y", y), ("ent_body", body),
                            ("ent_state", state), ("act_invuln", 0), ("act_hitstop", 0),
                            ("act_flags", 0), ("act_step", 0), ("act_face", 0),
                            ("act_timer", 0), ("act_kb", 0), ("act_hp", 99)):
            if name in self.labels:
                self.put(name, i, value)
        self.set_x(i, x)


# ---------------------------------------------------------------- 入口
def layer2_action(rom_path, labels, r):
    print("L2 実行検証 / action P1（Y許容幅・姿勢・効果スプライト）")

    gate = Gate(labels, r, "action")
    if not boot_or_fail(gate, CORE, "攻撃側と被弾側を置いて当たり判定を叩くのに使う"):
        return

    sections = (
        ("Y許容幅の境界（奥行きの当たり判定）",
         ("act_depth_ok", "act_depth_tol", "act_atk_ent", "act_dy", "act_tol"),
         _check_depth_tolerance),
        ("Y許容幅が実際の命中に効いている",
         ("act_hit_scan", "act_depth_tol", "act_hurt_w", "act_hurt_h",
          "atk_reach", "atk_w", "atk_h", "act_atk_ent", "act_face", "act_step",
          "act_flags", "act_invuln"), _check_hit_uses_tolerance),
        ("Y許容幅が絵の段数に追随する（ADR-0009 残る論点 1-a）",
         ("act_depth_tol",), _check_tol_follows_body),
        ("姿勢（攻撃とのけぞりが別の絵）",
         ("act_present", "act_pose_by_state", "ent_set_pose", "ent_tile0",
          "act_flags", "frame_counter"), _check_poses),
        ("敵の攻撃が避けられる",
         ("act_start_attack", "act_update_actor", "action_update_player", "act_pad",
          "act_mset", "act_mset_base", "atk_startup", "atk_active", "atk_reach",
          "act_hurt_w", "act_depth_tol", "depth_y_min", "depth_y_max", "act_hp",
          "act_init_entity", "act_flags"), _check_dodge),
        ("commit とノックバック",
         ("act_start_attack", "act_update_actor", "act_commit_hold", "act_cy",
          "act_move_right", "act_px", "act_kb", "act_damage", "act_kb_dir",
          "act_kb_amt", "act_atk_ent", "act_mset", "act_mset_base", "atk_startup",
          "atk_active", "atk_reach", "act_hurt_w", "act_depth_tol", "depth_y_min",
          "act_init_entity", "act_hp", "act_flags"), _check_commit),
        ("飛び道具",
         ("act_proj_update", "act_proj_owner", "act_start_attack", "act_update_actor",
          "act_mset", "act_mset_base", "atk_startup", "atk_proj", "atk_speed",
          "atk_life", "act_cy", "act_flags", "act_hitstop", "act_invuln",
          "act_init_entity", "act_hp", "depth_y_min"), _check_projectile),
        ("移動の強弱（歩きと小走り）",
         ("act_vel", "act_buf_atk", "act_pad_ignore", "act_init_entity",
          "depth_y_min", "depth_y_max", "act_face"), _check_run),
        ("効果スプライト（斬りと衝撃）",
         ("act_start_attack", "act_attack_step", "fx_life", "fx_tile", "fx_update",
          "fx_clear_all", "atk_active", "act_hit_scan", "act_face", "act_step",
          "act_flags", "act_hurt_w", "atk_reach"), _check_effects),
    )
    run_sections(gate, sections, lambda fn: (rom_path, labels, r))


# ---------------------------------------------------------------- Y許容幅
def _tol_table(nes, labels):
    return [nes.read(labels["act_depth_tol"] + b) for b in range(D["BODY_TYPE_COUNT"])]


def _tol_pair(tol, att_body, def_body):
    """攻撃側と被弾側で体格が違うときの許容幅（ACT_DEPTH_TOL_PAIR の約束どおり）。"""
    if D["ACT_DEPTH_TOL_PAIR"] == 0:
        return (tol[att_body] + tol[def_body]) >> 1      # 両者の平均
    return tol[def_body]                                  # 被弾側だけで決める


def _check_depth_tolerance(rom_path, labels, r):
    """`act_depth_ok` の境界値。**レーン一致判定の代わり**である（ADR-0009）。

    許容幅ちょうどで当たり、+1 で当たらず、-1 で当たる。
    期待値は ROM の act_depth_tol から読む（数値を焼き付けない）。
    """
    r.section("Y許容幅の境界（act_depth_ok。許容幅は ROM の act_depth_tol から読む）")

    a = Actors(rom_path, labels)
    tol = _tol_table(a.nes, labels)
    bodies = D["BODY_TYPE_COUNT"]

    r.check("act_depth_tol の表が体格型の数（%d）ぶんあり、すべて 1 ドット以上ある %s"
            % (bodies, tol), all(t >= 1 for t in tol),
            "act_depth_tol=%s。0 ドットの許容幅は「足元Yが完全一致でなければ当たらない」＝"
            "**レーン時代の理不尽がそのまま戻る**（ADR-0009 の発端）" % tol)

    a.sleep_all()
    y = a.get("depth_y_min") + 20 if "depth_y_min" in labels else 180
    problems = []
    pairs = [(0, 0), (0, bodies - 1), (bodies - 1, 0), (bodies - 1, bodies - 1)]
    for att_body, def_body in pairs:
        want = _tol_pair(tol, att_body, def_body)
        a.stand(a.player, 64, y, body=att_body)
        for sign in (+1, -1):
            for delta, expect in ((want - 1, True), (want, True), (want + 1, False)):
                if delta < 0:
                    continue
                a.stand(a.enemy, 96, y + sign * delta, body=def_body)
                a.put("act_atk_ent", 0, a.player)
                got = a.call("act_depth_ok", x=a.enemy).carry
                if got != expect:
                    problems.append(
                        "体格 %d が体格 %d を足元Yの差 %+d で %s（許容幅 %d なので %s はず）"
                        % (att_body, def_body, sign * delta,
                           "届いた" if got else "届かなかった", want,
                           "届く" if expect else "届かない"))
                seen = a.get("act_dy")
                if seen != delta:
                    problems.append("足元Yの差 %+d を act_dy=%d と測った（期待 %d）"
                                    % (sign * delta, seen, delta))
    r.check("奥行きの当たり判定が許容幅ちょうどで当たり、+1 で当たらない"
            "（体格の組 %s / 上下両方向）" % (pairs,), not problems,
            "%s。判定は「足元Yの差 <= 許容幅」であり、許容幅は体格型ごとの act_depth_tol "
            "%s から引く（違う体格どうしは %s）"
            % ("／".join(problems), tol,
               "両者の平均" if D["ACT_DEPTH_TOL_PAIR"] == 0 else "被弾側の値"))


def _check_hit_uses_tolerance(rom_path, labels, r):
    """境界が**実際の命中**に効いていること（横の矩形判定との組み合わせ）。

    act_depth_ok が正しくても、当たり判定の走査がそれを見ていなければ意味が無い。
    HP が減ったかどうかで見る。
    """
    r.section("Y許容幅が実際の命中に効いている（act_hit_scan）")

    a = Actors(rom_path, labels)
    tol = _tol_table(a.nes, labels)
    body = 0
    want = _tol_pair(tol, body, body)
    hurt_w = a.rom("act_hurt_w", body)
    reach = a.rom("atk_reach", 0)
    atk_w = a.rom("atk_w", 0)
    y = a.get("depth_y_min") + 20 if "depth_y_min" in labels else 180
    px = 64
    inside_x = px + hurt_w + reach + 1            # 攻撃矩形の中に居るX
    far_x = px + hurt_w + reach + atk_w + 64      # 矩形の外（奥行きは合っている）

    def swing(target_x, dy):
        a.sleep_all()
        a.stand(a.player, px, y, body=body)
        a.stand(a.enemy, target_x, y + dy, body=body)
        a.put("act_step", a.player, 1)
        a.put("ent_state", a.player, D["ACT_ST_ATK_ACTIVE"])
        a.put("act_flags", a.player, 0)
        before = a.get("act_hp", a.enemy)
        a.call("act_hit_scan", x=a.player)
        return before - a.get("act_hp", a.enemy)

    cases = [(inside_x, 0, True, "奥行きも横も合っている"),
             (inside_x, want, True, "奥行きが許容幅ちょうど"),
             (inside_x, -want, True, "奥行きが許容幅ちょうど（手前から奥へ）"),
             (inside_x, want + 1, False, "奥行きが許容幅 +1"),
             (inside_x, -(want + 1), False, "奥行きが許容幅 +1（手前から奥へ）"),
             (far_x, 0, False, "奥行きは合っているが横に離れている")]
    problems = []
    for tx, dy, expect, why in cases:
        dealt = swing(tx, dy)
        if bool(dealt) != expect:
            problems.append("%s → %s（期待 %s。与えたダメージ %d）"
                            % (why, "当たった" if dealt else "当たらなかった",
                               "当たる" if expect else "当たらない", dealt))
    r.check("命中の可否が「足元Yの差 <= 許容幅 %d」と横の矩形の両方で決まる（%d ケース）"
            % (want, len(cases)), not problems,
            "%s。奥行きと横のどちらか一方でも外れたら当たらない。"
            "レーン番号の一致ではなく**足元Yの近さ**で判定するのが ADR-0009 の決定である"
            % "／".join(problems))


# ---------------------------------------------------------------- 許容幅と絵の大きさ
_ROWS_RE = re.compile(r"^(ACT_BODY_ROWS_[A-Z_]+)(\s*)=\s*(\d+)", re.M)

_PROBE_CFG = """MEMORY { MAIN: start=$0000, size=$10000, file=%O, fill=no; }
SEGMENTS { RODATA: load=MAIN, type=ro; }
"""


def _probe_depth_tol(scale):
    """action_params.s を**組み直して** act_depth_tol の表を取り出す。

    ACT_BODY_ROWS_*（絵の段数）を scale 倍した写しでアセンブルする。
    これが「縦サイズを変えたら許容幅が追随する」を確かめる唯一の方法である。
    ROM を読むだけでは、表が計算式で埋まっているのか数値が手で書かれているのかを
    区別できない（いまは段数が全て 1 なので、5 と書いてあっても式と同じ値になる）。

    戻り値: 表（リスト）/ 道具が無ければ None。
    """
    if not (shutil.which("ca65") and shutil.which("ld65")):
        return None
    src = os.path.join(ROOT, "src")
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("action_params.s", "action_params.inc"):
            shutil.copy(os.path.join(src, "action", name), os.path.join(tmp, name))
        inc = os.path.join(tmp, "action_params.inc")
        with open(inc, encoding="utf-8") as f:
            text = f.read()
        text, n = _ROWS_RE.subn(
            lambda m: "%s%s= %d" % (m.group(1), m.group(2), int(m.group(3)) * scale), text)
        if not n:
            return None                     # ACT_BODY_ROWS_* が見当たらない
        with open(inc, "w", encoding="utf-8") as f:
            f.write(text)
        with open(os.path.join(tmp, "probe.cfg"), "w", encoding="utf-8") as f:
            f.write(_PROBE_CFG)
        obj, out, lab = (os.path.join(tmp, n) for n in ("p.o", "p.bin", "p.labels"))
        for cmd in ([shutil.which("ca65"), "-g", "-I", tmp, "-I", src,
                     "-I", os.path.join(src, "action"), "-o", obj,
                     os.path.join(tmp, "action_params.s")],
                    [shutil.which("ld65"), "-C", os.path.join(tmp, "probe.cfg"),
                     "-Ln", lab, "-o", out, obj]):
            p = subprocess.run(cmd, capture_output=True, text=True)
            if p.returncode != 0:
                raise RuntimeError("組み直しに失敗した（段数 ×%d）: %s"
                                   % (scale, (p.stderr or p.stdout).strip()[:300]))
        labels = load_labels(lab)
        if "act_depth_tol" not in labels:
            return None
        with open(out, "rb") as f:
            data = f.read()
        at = labels["act_depth_tol"]
        return list(data[at:at + D["BODY_TYPE_COUNT"]])


def _check_tol_follows_body(rom_path, labels, r):
    """**値ではなく関係を縛る。**

    主の決定は「許容幅はキャラクターの縦サイズの約 1/3」であり（ADR-0009 残る論点 1-a）、
    5 という数値ではない。絵を大きくしたときに許容幅が置き去りになると、
    「大きいキャラほど当たらない」という P1 で指摘されたのと同種の理不尽が戻る。
    """
    r.section("Y許容幅が絵の段数に追随する（値ではなく比が本体。ADR-0009）")

    nes = Nes(rom_path)
    nes.reset()
    rom_tol = _tol_table(nes, labels)
    rows = [n for _, n in body_rows(D)]
    num, den = D["ACT_DEPTH_TOL_NUM"], D["ACT_DEPTH_TOL_DEN"]
    want = [n * D["SPRITE_H"] * num // den for n in rows]

    r.check("ROM の act_depth_tol が「段数 × %d × %d/%d」と一致する %s"
            % (D["SPRITE_H"], num, den, rom_tol), rom_tol == want,
            "ROM の表=%s / 段数 %s から計算した値=%s。許容幅は "
            "action_params.inc の ACT_BODY_ROWS_*（絵の段数）と比から**計算した式**で"
            "持つこと。数値を手で書くと、絵を大きくしたときに必ず置き去りになる"
            % (rom_tol, rows, want))

    r.check("主の決定「縦 %d ドットのキャラなら 5〜6 ドット」の範囲に比が収まっている（%d）"
            % (D["SPRITE_H"], D["SPRITE_H"] * num // den),
            5 <= D["SPRITE_H"] * num // den <= 6,
            "8x16 一段のキャラに当てはめた許容幅が %d ドット（主の決定は 5〜6）。"
            "比（ACT_DEPTH_TOL_NUM/DEN = %d/%d）を動かすなら、それは手触りの仕様変更なので"
            "主の決定を取り直すこと。**絵を大きくして値が 6 を超えるのは正しい**"
            "（比は保たれている）" % (D["SPRITE_H"] * num // den, num, den))

    # --- ここが本題: 段数を変えて組み直し、許容幅が追随することを確かめる ---
    try:
        base = _probe_depth_tol(1)
        tripled = _probe_depth_tol(3)
    except RuntimeError as e:
        r.check("絵の段数を変えて action_params を組み直せる", False, str(e))
        return
    if base is None or tripled is None:
        print("      ** 段数 → 許容幅の追随は**未実行** — ca65 / ld65 が無い **")
        print("         （ROM との突き合わせだけでは、表が式なのか手書きの数値なのか区別できない）")
        return

    r.check("組み直した表が ROM の表と一致する（このプローブが本物を測っている）",
            base == rom_tol,
            "組み直し=%s / ROM=%s。プローブが実際の action_params.s とは別のものを"
            "測っている。以下の追随の検証も信用できない" % (base, rom_tol))
    want3 = [n * 3 * D["SPRITE_H"] * num // den for n in rows]
    r.check("絵の段数を3倍にすると許容幅も3倍になる（%s → %s）" % (base, tripled),
            tripled == want3 and tripled != base,
            "段数を3倍にして組み直したら許容幅は %s（期待 %s）。"
            "追随しないということは、act_depth_tol の表に**数値が焼き付いている**か、"
            "式が ACT_BODY_ROWS_* を見ていないということである。"
            "主の決定は「許容幅 = 縦サイズの %d/%d」であって値そのものではない"
            "（ADR-0009 残る論点 1-a）。絵を 2x3 タイルに拡大したときに"
            "「大きいキャラほど当たらない」が起きる" % (tripled, want3, num, den))


# ---------------------------------------------------------------- 姿勢
def _check_poses(rom_path, labels, r):
    """状態ごとに絵が変わること。**攻撃中とのけぞり中が別の絵**であること。"""
    r.section("姿勢（P1 受入「攻撃とダメージ硬直の違いがわからない」への対応）")

    a = Actors(rom_path, labels)
    states = D["ACT_ST_COUNT"]
    pose_of = [a.rom("act_pose_by_state", st) for st in range(states)]
    stride = D["SPR_POSE_STRIDE"]

    r.check("状態 → 姿勢の表が %d 状態ぶんあり、すべて姿勢の語彙 (0..%d) の中を指す %s"
            % (states, D["SPR_POSE_COUNT"] - 1, pose_of),
            all(0 <= p < D["SPR_POSE_COUNT"] for p in pose_of),
            "act_pose_by_state=%s、姿勢は %d 種（SPR_POSE_*）。表の外を指すと"
            "役割16タイルの枠を出て、別の役割の絵が出る" % (pose_of, D["SPR_POSE_COUNT"]))

    r.check("攻撃の持続中とのけぞり中が**別の姿勢**である（姿勢 %d と %d）"
            % (pose_of[D["ACT_ST_ATK_ACTIVE"]], pose_of[D["ACT_ST_HURT"]]),
            pose_of[D["ACT_ST_ATK_ACTIVE"]] != pose_of[D["ACT_ST_HURT"]],
            "攻撃中ものけぞり中も姿勢 %d。P1 の受入で主が指摘した"
            "「攻撃とダメージ硬直の違いがわからない」がそのまま残っている。"
            "斬りは前へ、のけぞりは後ろへ折れる絵にすること（action_params.s の表）"
            % pose_of[D["ACT_ST_ATK_ACTIVE"]])

    # --- 実際に ent_tile が切り替わること ---
    a.sleep_all()
    y = a.get("depth_y_min") + 20 if "depth_y_min" in labels else 180
    a.stand(a.player, 64, y, body=0)
    base = a.get("ent_tile0", a.player)
    a.put("frame_counter", 0, 0)
    wrong = []
    tiles = {}
    for st in range(states):
        a.put("ent_state", a.player, st)
        a.put("act_flags", a.player, 0)          # 「今フレーム動いた」は立てない
        a.call("act_present", x=a.player)
        got = a.get("ent_tile", a.player)
        tiles[st] = got
        want = base + pose_of[st] * stride
        if got != want:
            wrong.append("状態 %d → ent_tile=%d（期待 %d = 先頭タイル %d + 姿勢 %d × %d）"
                         % (st, got, want, base, pose_of[st], stride))
    r.check("%d 状態すべてで ent_tile が表どおりの姿勢になる" % states, not wrong,
            "%s。状態ごとに絵が変わらないと、画面から「いま何が起きているか」が読めない"
            % "／".join(wrong))
    r.check("攻撃中とのけぞり中で実際に出るタイルが違う (%d と %d)"
            % (tiles.get(D["ACT_ST_ATK_ACTIVE"], -1), tiles.get(D["ACT_ST_HURT"], -1)),
            tiles.get(D["ACT_ST_ATK_ACTIVE"]) != tiles.get(D["ACT_ST_HURT"]),
            "どちらも ent_tile=%s。表が別の姿勢を指していても、"
            "ent_set_pose へ渡るまでに潰れている" % tiles.get(D["ACT_ST_HURT"]))

    # --- 立ちと歩きの見分け（待機状態だけは表の1項では足りない）---
    a.put("ent_state", a.player, D["ACT_ST_IDLE"])
    a.put("act_flags", a.player, 0)
    a.call("act_present", x=a.player)
    standing = a.get("ent_tile", a.player)
    a.put("act_flags", a.player, D["ACT_F_MOVED"])
    a.put("frame_counter", 0, D["ACT_WALK_ANIM"] or 0)
    a.call("act_present", x=a.player)
    walking = a.get("ent_tile", a.player)
    want_walk = base + D["ACT_POSE_MOVE"] * stride
    r.check("動いたフレームは歩きの絵になる（立ち %d → 歩き %d）" % (standing, walking),
            walking == want_walk,
            "動いた印 (ACT_F_MOVED) を立てて act_present を呼んだら ent_tile=%d"
            "（期待 %d = 姿勢 ACT_POSE_MOVE(%d)）。歩きの絵が出ないと、"
            "動いているのか固まっているのかが画面で分からない"
            % (walking, want_walk, D["ACT_POSE_MOVE"]))
    r.check("「今フレーム動いた」印は読まれた後に下ろされる（次のフレームに持ち越さない）",
            not (a.get("act_flags", a.player) & D["ACT_F_MOVED"]),
            "act_flags=$%02X。ACT_F_MOVED は状態ではなく「1フレームの事実」であり、"
            "持ち越すと止まっても歩き続ける" % a.get("act_flags", a.player))


# ---------------------------------------------------------------- 効果スプライト
def _check_effects(rom_path, labels, r):
    """斬りと衝撃。**攻撃の距離感**を画面に出すための効果（P1 受入の指摘）。"""
    r.section("効果スプライト（攻撃の届く範囲と当たった位置を画面に出す）")

    a = Actors(rom_path, labels)
    fx_max = labels["fx_x_lo"] - labels["fx_life"]

    def live():
        return [(a.get("fx_tile", i), a.get("fx_life", i))
                for i in range(fx_max) if a.get("fx_life", i)]

    # --- 斬り: 判定が出ている間に出て、寿命で自動的に消える ---
    a.sleep_all()
    y = a.get("depth_y_min") + 20 if "depth_y_min" in labels else 180
    a.stand(a.player, 64, y, body=0)
    a.call("fx_clear_all")
    a.call("act_start_attack", x=a.player, a=1)

    # メインループと同じ並びで手回しする（fx_update → 攻撃の相）。
    # 実フレームを回さないのは、敵と AI を混ぜずに「攻撃1回ぶん」だけを見たいため。
    active_state = D["ACT_ST_ATK_ACTIVE"]
    trace = []
    for _ in range(80):
        a.call("fx_update")
        a.call("act_attack_step", x=a.player)
        trace.append((a.get("ent_state", a.player), len(live())))
        if a.get("ent_state", a.player) == D["ACT_ST_IDLE"] and len(trace) > 4:
            break
    during = [n for st, n in trace if st == active_state]
    tail = []
    for _ in range(max(2, a.rom("atk_active", 0) + D["ACT_FX_IMPACT_LIFE"] + 2)):
        a.call("fx_update")
        tail.append(len(live()))

    if D["ACT_SHOW_SWIPE"]:
        r.check("攻撃の持続中は斬りの効果が出ている（%d フレーム）" % len(during),
                bool(during) and all(n >= 1 for n in during),
                "持続フレームごとの効果の数=%s（推移=%s）。攻撃判定が出ているのに"
                "画面に何も出ないと、どこまで届くのかが分からない"
                "（P1 受入「攻撃の距離感が掴めない」）。"
                "ACT_SHOW_SWIPE=%d のときは出る約束である" % (during, trace, D["ACT_SHOW_SWIPE"]))
    else:
        r.check("ACT_SHOW_SWIPE=0 なので斬りの効果は出ない", not any(during),
                "斬りを切ってあるのに効果が出ている（推移=%s）" % trace)

    r.check("攻撃が終われば、誰も消さなくても効果が自動で消える",
            tail[-1] == 0,
            "攻撃が終わってからさらに %d フレーム進めても効果が %d 個残っている（推移=%s）。"
            "寿命が尽きたら engine が消す（fx_update）約束であり、"
            "**呼び出し側に後始末をさせない**。消し忘れは画面に判定の残骸が残る"
            % (len(tail), tail[-1], tail))

    # --- 衝撃: 命中した瞬間に、当たった位置へ出る ---
    tol = _tol_table(a.nes, labels)
    body = 0
    hurt_w = a.rom("act_hurt_w", body)
    reach = a.rom("atk_reach", 0)
    a.sleep_all()
    a.stand(a.player, 64, y, body=body)
    a.stand(a.enemy, 64 + hurt_w + reach + 1, y, body=body)
    a.put("act_step", a.player, 1)
    a.put("ent_state", a.player, active_state)
    a.put("act_flags", a.player, 0)
    a.call("fx_clear_all")
    a.call("act_hit_scan", x=a.player)
    hit_fx = live()
    dealt = 99 - a.get("act_hp", a.enemy)

    r.check("当たった相手にダメージが入る（この節の前提が空振りでない）", dealt > 0,
            "HP が %d しか減っていない。許容幅 %d / 攻撃矩形の組み立てを先に見よ"
            % (dealt, _tol_pair(tol, body, body)))
    if D["ACT_SHOW_IMPACT"]:
        tiles = [t for t, _ in hit_fx]
        r.check("命中した瞬間に衝撃の効果が出る（タイル $%02X）" % D["SPR_TILE_IMPACT"],
                D["SPR_TILE_IMPACT"] in tiles,
                "命中後に出ている効果=%s（期待はタイル %d の衝撃を含むこと）。"
                "当たった位置が画面に出ないと、当たったのか避けられたのかが分からない"
                % (hit_fx, D["SPR_TILE_IMPACT"]))
    else:
        r.check("ACT_SHOW_IMPACT=0 なので衝撃の効果は出ない",
                D["SPR_TILE_IMPACT"] not in [t for t, _ in hit_fx],
                "衝撃を切ってあるのに出ている（%s）" % hit_fx)


# ==========================================================================
# 避けられる攻撃 —— **仮説の本体**
# ==========================================================================
def _attack_row(a, ent):
    """その者がいま振っている技表の行（combat.s の act_row_for と同じ引き方）。

    行 = 技構成の先頭（act_mset_base）+ 段数 - 1。**行番号を焼き付けない。**
    敵の近接が何行目かは action_params.inc の ATK_ROW_* が決めることであり、
    技構成が増えれば動く。
    """
    mset = a.get("act_mset", ent)
    return a.rom("act_mset_base", mset) + a.get("act_step", ent) - 1


def _check_dodge(rom_path, labels, r):
    """敵の攻撃が**避けられる**こと。P1 の受入で主が出した仮説そのものである。

    「予備動作を見てから奥／手前へ歩けば当たらない」——これが成り立たなければ、
    奥行きを連続にした意味も（ADR-0009）、敵の技表を分離して予備動作を延ばした意味も
    消える。**当たり判定が出たフレームの足元Yの差だけで当否が決まる**ことを見る。

    **ACT_ENEMY_TELL を変えても落ちない形にしてある**（主が真っ先に回すノブである）。
    予備動作の長さは技表（ROM の atk_startup）から読み、歩ける距離はそこから計算する。
    予備動作を短くすると「避けられなくなる」のは仕様どおりの帰結なので、
    そのときは避けられることを要求する検証の方が**前提ごと**降りる（名前にそう出る）。

    進め方は「操作キャラの1フレーム（action_update_player）」と
    「敵の1フレーム（act_update_actor）」を直接呼ぶ形にしてある。
    敵の AI に振らせると、いつ振るかが ai 側のノブ（atk_gap）で変わってしまい、
    action の主張を見ているつもりで ai を見ることになる。
    入力は act_pad（入力ゲートを通った後の**実効のパッド状態**）に直接置く。
    """
    r.section("敵の攻撃が避けられる（予備動作の間に奥へ歩く / 仮説の本体）")

    a = Actors(rom_path, labels)
    tol = _tol_pair(_tol_table(a.nes, labels), 0, 0)
    depth_lo = a.get("depth_y_min")
    depth_hi = a.get("depth_y_max")
    y = depth_hi - 2                      # 手前端。ここから奥いっぱいに逃げられる
    px = 80
    hurt_w = a.rom("act_hurt_w", 0)

    def one(press):
        """敵に1発振らせる。press なら被弾側は予備動作の間ずっと奥へ歩く。"""
        a.sleep_all()
        a.stand(a.enemy, px, y)
        a.call("act_init_entity", x=a.enemy)
        a.put("act_face", a.enemy, 0)                  # 右を向く＝被弾側の側
        a.call("act_start_attack", a=1, x=a.enemy)
        row = _attack_row(a, a.enemy)
        reach = a.rom("atk_reach", row)
        a.stand(a.player, px + hurt_w + reach + 1, y)  # 攻撃矩形の中に立たせる
        a.call("act_init_entity", x=a.player)
        a.put("act_hp", a.player, 99)

        tell = a.rom("atk_startup", row)
        limit = tell + a.rom("atk_active", row) + 4
        hp0 = a.get("act_hp", a.player)
        ys, scan_dy = [a.get("ent_y", a.player)], []
        for _ in range(limit):
            a.put("act_pad", 0, D["PAD_UP"] if press else 0)
            a.call("action_update_player")
            ys.append(a.get("ent_y", a.player))
            judging = (a.get("ent_state", a.enemy) == D["ACT_ST_ATK_ACTIVE"]
                       and not a.get("act_flags", a.enemy) & D["ACT_F_HIT"])
            dy = abs(a.get("ent_y", a.player) - a.get("ent_y", a.enemy))
            a.call("act_update_actor", x=a.enemy)
            if judging:
                scan_dy.append(dy)
        a.put("act_pad", 0, 0)
        return {"hit": a.get("act_hp", a.player) < hp0, "dy": scan_dy,
                "ys": ys, "tell": tell, "row": row}

    stay = one(False)
    dodge = one(True)

    # --- 前提: 振りかぶっている相手の前でも、被弾側は奥へ歩ける ---
    tell = stay["tell"]
    walked = dodge["ys"][0] - dodge["ys"][tell]
    want_walk = min(tell * D["ACT_DEPTH_SPEED"] // 16, dodge["ys"][0] - depth_lo)
    r.check("敵が振りかぶっている %d フレームの間に、被弾側は奥へ %d ドット歩ける"
            % (tell, want_walk), walked == want_walk,
            "足元Y %d → %d（%d ドット）。予備動作 %d フレーム × 奥行きの速さ "
            "ACT_DEPTH_SPEED=%d/16 ドットなら %d ドット動けるはずである（帯の奥端は %d）。"
            "動けないなら、振りかぶられた側が動けない＝**避ける遊びが成立しない**"
            % (dodge["ys"][0], dodge["ys"][tell], walked, tell,
               D["ACT_DEPTH_SPEED"], want_walk, depth_lo))

    # --- 空振り防止: 動かなければ当たる場である ---
    r.check("その場に立ち続ければ当たる（この検証が空振りでない）", stay["hit"],
            "奥行きも横も合わせて立たせたのに当たらなかった（判定の出たフレームの"
            "足元Yの差 %s / 許容幅 %d、技表の行 %d）。当たらない場で「避けられた」と"
            "言っても何も確かめていない" % (stay["dy"], tol, stay["row"]))

    # --- 本題: 当否は「判定が出たフレームの足元Yの差」だけで決まる ---
    bad = []
    for name, run in (("その場に立った", stay), ("奥へ歩いた", dodge)):
        reach = min(run["dy"]) if run["dy"] else None
        want = reach is not None and reach <= tol
        if run["hit"] != want:
            bad.append("%s側が %s（判定の出たフレームの足元Yの差は %s、許容幅 %d）"
                       % (name, "当たった" if run["hit"] else "当たらなかった",
                          run["dy"], tol))
    r.check("当たるかどうかが、判定の出たフレームの足元Yの差（許容幅 %d）だけで決まる" % tol,
            not bad,
            "%s。予備動作の間にどれだけ動いたかではなく、**判定が出た瞬間の奥行き**で"
            "決まるのが ADR-0009 の当たり判定である。ここがずれるなら、"
            "act_hit_scan が判定フレーム以外で当てているか、"
            "commit した足元Y（act_cy）ではなく現在位置で判定している"
            % "／".join(bad))

    # --- 予備動作が「避けられる長さ」であること（ノブの帰結。前提ごと名前に出す）---
    escapable = want_walk > tol
    r.check("予備動作 %d フレームで %d ドット逃げられ、許容幅 %d を超える＝この一撃は避けられる"
            % (tell, want_walk, tol) if escapable else
            "予備動作 %d フレームでは許容幅 %d の外へ出られない設定である"
            "（ACT_ENEMY_TELL / ACT_DEPTH_SPEED のノブの帰結。避けられないのが仕様）"
            % (tell, tol),
            (not dodge["hit"]) if escapable else True,
            "予備動作の間に奥へ %d ドット歩いて許容幅 %d の外に出たのに当たった"
            "（判定フレームの足元Yの差 %s）。**これが主の仮説の本体である**: "
            "「予備動作を見てから避けられる」が成り立たないなら、奥行きを連続にした意味も"
            "（ADR-0009）、敵の技表を分けて予備動作を延ばした意味も無い"
            % (walked, tol, dodge["dy"]))


# ==========================================================================
# commit（振り始めに狙いを固定する）とノックバック
# ==========================================================================
def _check_commit(rom_path, labels, r):
    """溜めている間に追尾しないこと。**これが壊れると仮説そのものが死ぬ。**

    踏み込んで避けたのに当たり判定が付いてきたら、予備動作を長くした意味が消える。
    commit の実体は act_start_attack が写す act_cy（狙う足元Y）と ACT_F_CFACE（向き）で、
    攻撃中の毎フレーム act_commit_hold がそれを書き戻す。

    **ノックバックだけは根の門（act_can_move）を通らない。**
    殴られたら攻撃中でも吹き飛ぶ。ここを塞ぐと、振りかぶった敵が殴られても微動だにしない。
    """
    r.section("commit（溜め中に追尾しない）とノックバック（門を通らない）")

    a = Actors(rom_path, labels)
    tol = _tol_pair(_tol_table(a.nes, labels), 0, 0)
    y = a.get("depth_y_min") + 20
    px = 80
    hurt_w = a.rom("act_hurt_w", 0)

    def wind_up(drag_y=None, drag_face=False, target_y=None):
        """敵に1発振らせ、予備動作の途中で足元Yか向きを**外から**動かしてみる。

        drag_y    … 溜め中に攻撃側の足元Yを動かす先（None なら動かさない）
        drag_face … 溜め中に攻撃側の向きを裏返す
        target_y  … 標的を置く足元Y
        戻り: (当たったか, 判定フレームの攻撃側の足元Y, 判定フレームの向き)

        **奥行きと向きは別々に動かす。**両方いっぺんに動かすと、commit が壊れても
        「向きが変わって空振りした」ために当たらず、検証が素通りする。
        """
        a.sleep_all()
        a.stand(a.enemy, px, y)
        a.call("act_init_entity", x=a.enemy)
        a.put("act_face", a.enemy, 0)
        a.call("act_start_attack", a=1, x=a.enemy)
        row = _attack_row(a, a.enemy)
        a.stand(a.player, px + hurt_w + a.rom("atk_reach", row) + 1,
                y if target_y is None else target_y)
        a.call("act_init_entity", x=a.player)
        a.put("act_hp", a.player, 99)
        hp0 = a.get("act_hp", a.player)

        tell = a.rom("atk_startup", row)
        for i in range(tell + a.rom("atk_active", row) + 2):
            if i == tell // 2:
                if drag_y is not None:
                    a.put("ent_y", a.enemy, drag_y)     # 誰かが奥行きに手を出した
                if drag_face:
                    a.put("act_face", a.enemy, 1)       # 誰かが向きを変えた
            a.call("act_update_actor", x=a.enemy)
        return (a.get("act_hp", a.player) < hp0,
                a.get("ent_y", a.enemy), a.get("act_face", a.enemy))

    # --- 1. 振り始めに狙いが写される ---
    a.sleep_all()
    a.stand(a.enemy, px, y)
    a.call("act_init_entity", x=a.enemy)
    a.call("act_start_attack", a=1, x=a.enemy)
    r.check("振り始めに、狙う足元Y（act_cy）と向き（ACT_F_CFACE）が写される",
            a.get("act_cy", a.enemy) == y
            and bool(a.get("act_flags", a.enemy) & D["ACT_F_CFACE"]) == bool(a.get("act_face", a.enemy)),
            "act_cy=%d（立っている足元Yは %d）/ act_flags=$%02X・act_face=%d。"
            "振り始めの位置と向きを写していないと、溜めている間の追尾を止めようがない"
            % (a.get("act_cy", a.enemy), y, a.get("act_flags", a.enemy),
               a.get("act_face", a.enemy)))

    # --- 2. 溜め中に動かされても、振りは振り始めの位置・向きに出る ---
    far = y + tol + 1
    hit_ctrl, y_ctrl, face_ctrl = wind_up()
    hit_dragy, y_drag, _ = wind_up(drag_y=far, target_y=far)
    hit_dragf, _, face_drag = wind_up(drag_face=True)
    r.check("溜め中に足元Yと向きを動かしても、判定のフレームには振り始めの値に戻っている",
            y_drag == y and face_drag == 0,
            "足元Yを %d へ動かしたら判定のフレームで %d（振り始めは %d）、"
            "向きを 1 へ動かしたら %d（振り始めは 0）だった。act_commit_hold が act_cy と "
            "ACT_F_CFACE を書き戻していない。**commit が崩れると避ける遊びが丸ごと崩れる**"
            % (far, y_drag, y, face_drag))
    r.check("振り始めの奥行きに立っている標的には当たる（commit の対照）", hit_ctrl,
            "振り始めの足元Y %d に標的を置いたのに当たらなかった。"
            "この対照が当たらないと、下の「追尾しない」は何も確かめていない" % y)
    r.check("溜め中に攻撃側を標的ごと %d ドット動かしても、振りは振り始めの奥行きに出る"
            "（追尾しない）" % (tol + 1), not hit_dragy,
            "攻撃側と標的をそろって足元Y %d へ動かしたのに当たった（振り始めは %d）。"
            "**溜めている間に狙いが付いてきている**＝踏み込んで避けても当たるということで、"
            "予備動作を長くした意味が消える（仮説そのものが死ぬ）" % (far, y))
    r.check("溜め中に向きを裏返されても、振りは振り始めの向きに出る（標的に当たる）",
            hit_dragf,
            "溜めている途中で向きだけを裏返したら、振り始めの側に居た標的に当たらなくなった。"
            "向きも commit の一部である（ACT_F_CFACE）。ここが崩れると、"
            "予備動作を見て回り込んだのに背中から殴られる／その逆が起きる")

    # --- 3. 攻撃中は自分の足で動けない（根が生える）---
    a.sleep_all()
    a.stand(a.enemy, px, y)
    a.call("act_init_entity", x=a.enemy)
    a.call("act_start_attack", a=1, x=a.enemy)
    a.put("act_px", 0, 4)
    a.call("act_move_right", x=a.enemy)
    rooted = a.x(a.enemy) == px
    if D["ACT_ATTACK_ROOT"]:
        r.check("攻撃の間は自分の足で動けない（ACT_ATTACK_ROOT=1。その場に根が生える）",
                rooted,
                "攻撃中に act_move_right で %d → %d と動いた。予備動作の間に踏み込めると、"
                "当たる位置も一緒に動く（commit の一部である）" % (px, a.x(a.enemy)))
    else:
        r.check("ACT_ATTACK_ROOT=0 の設定では攻撃中でも自分の足で動ける", not rooted,
                "ノブが 0（根が生えない）なのに攻撃中に動けなかった（%d のまま）" % px)

    # --- 4. ノックバックは根の門を通らない ---
    kb = 5
    a.put("act_kb", a.enemy, kb)
    a.put("act_hitstop", a.enemy, 0)
    before = a.x(a.enemy)
    a.call("act_update_actor", x=a.enemy)
    moved = a.x(a.enemy) - before
    r.check("殴られたら攻撃中でも吹き飛ぶ（ノックバックは根の門 act_can_move を通らない）",
            moved == kb,
            "攻撃中（ent_state=%d）にノックバック速度 %d を乗せて1フレーム進めたら "
            "%d ドットしか動かなかった。ノックバックは「自分の足で動く」ことではないので"
            "act_shift_* を直に呼ぶのが約束である（actor.s の act_knockback_step）。"
            "ここを門に通すと、振りかぶった敵を殴っても微動だにしない"
            % (a.get("ent_state", a.enemy), kb, moved))

    # --- 5. ノックバックの向きは殴った側の向きから来る（公の経路）---
    signs = []
    for kb_dir, want in ((0, +1), (1, -1)):
        a.sleep_all()
        a.stand(a.player, px, y)
        a.call("act_init_entity", x=a.player)
        a.put("act_hp", a.player, 99)
        a.put("act_atk_ent", 0, a.player)
        a.put("act_kb_dir", 0, kb_dir)
        a.put("act_kb_amt", 0, kb)
        a.call("act_damage", a=1, x=a.player)
        got = a.get("act_kb", a.player)
        got = got - 256 if got > 127 else got
        if got != want * kb:
            signs.append("act_kb_dir=%d → act_kb=%d（期待 %d）" % (kb_dir, got, want * kb))
    r.check("ノックバックの向きは殴った側の向き（act_kb_dir）で決まる", not signs,
            "%s。向きの符号が逆だと、殴られた相手が**殴った側へ吸い込まれる**"
            % "／".join(signs))


# ==========================================================================
# 飛び道具 —— 出る・飛ぶ・当たる・味方には当たらない・寿命で消える
# ==========================================================================
def _check_projectile(rom_path, labels, r):
    """弾がエンティティとして成立していること。

    弾は短命の効果（fx）ではなく**本物のエンティティ**である（当たり判定を持ち、
    OAM の並べ替えに乗る）。その代わり、出しっぱなしで枠を食い潰す・味方討ちをする・
    見えないのに当たる、といった壊れ方をしうる。ここで全部塞ぐ。

    速さ・寿命・威力・大きさは**技表（ROM の atk_* の ATK_ROW_BULLET 行）から読む**。
    主がそこを触ってもこの検証は落ちない。
    """
    r.section("飛び道具（出る・飛ぶ・当たる・味方には当たらない・寿命で消える）")

    a = Actors(rom_path, labels)
    free, ents = D["ENT_FREE_FIRST"], D["MAX_ENTITIES"]
    y = a.get("depth_y_min") + 20
    px = 48

    def fire(shooter):
        """shooter に遠隔の技構成で1発撃たせ、出た弾の番号を返す（出なければ None）。"""
        a.stand(shooter, px, y)
        a.call("act_init_entity", x=shooter)
        a.put("act_mset", shooter, D["ACT_MSET_SHOOT"])
        a.put("act_face", shooter, 0)                 # 右へ撃つ
        a.call("act_start_attack", a=1, x=shooter)
        row = _attack_row(a, shooter)
        for _ in range(a.rom("atk_startup", row) + 2):
            a.call("act_update_actor", x=shooter)
            live = [i for i in range(free, ents) if a.get("ent_active", i)]
            if live:
                return live[0]
        return None

    def clear_free():
        for i in range(free, ents):
            a.put("ent_active", i, 0)
            a.put("act_flags", i, 0)

    # --- 1. 出る ---
    a.sleep_all()
    clear_free()
    shooter = a.enemy
    proj = fire(shooter)
    if not r.check("遠隔の技構成（ACT_MSET_SHOOT）で振ると、空き枠に弾が1つ出る",
                   proj is not None,
                   "振りかぶりの発生 %d フレームを過ぎても空き枠（%d..%d）に弾が出ない。"
                   "act_attack_step が atk_proj の行を見て act_proj_fire を呼んでいない、"
                   "または空き枠を見つけられていない（ent_find_free）"
                   % (a.rom("atk_startup", D["ATK_ROW_SHOOT"]), free, ents - 1)):
        return

    bullet_row = a.rom("atk_proj", _attack_row(a, shooter))
    speed = a.rom("atk_speed", bullet_row)
    life = a.rom("atk_life", bullet_row)
    born_x = a.x(proj)
    facts = []
    if not a.get("act_flags", proj) & D["ACT_F_PROJ"]:
        facts.append("飛び道具の印 ACT_F_PROJ が立っていない（act_flags=$%02X）"
                     % a.get("act_flags", proj))
    if a.get("ent_y", proj) != a.get("act_cy", shooter):
        facts.append("弾の足元Yが %d（撃った主が commit した足元Y は %d）"
                     % (a.get("ent_y", proj), a.get("act_cy", shooter)))
    if a.get("act_proj_owner", proj - free) != shooter:
        facts.append("撃った主が %d と記録されている（本当は %d）"
                     % (a.get("act_proj_owner", proj - free), shooter))
    if a.get("act_hitstop", proj) != D["ACT_PROJ_ARM"]:
        facts.append("出たフレームの据え置きが %d（ACT_PROJ_ARM=%d）"
                     % (a.get("act_hitstop", proj), D["ACT_PROJ_ARM"]))
    r.check("出た弾が、撃った主の commit した奥行き・向き・持ち主を引き継ぐ", not facts,
            "%s。弾の奥行きが撃った主の**現在位置**から来ると、溜め中に動かされた分だけ"
            "狙いがずれる。持ち主が違うと、狙う区画（act_target_range）が逆になって"
            "**味方討ち**になる" % "／".join(facts))

    # --- 2. 生まれたフレームには当たらない（**見えない判定を作らない**）---
    # 銃口に重なって立っている相手を置く。ここで即座にダメージが入るなら、
    # 弾は**画面に一度も出ないまま**当てたことになる（ADR-0002 が最も理不尽としたもの）。
    a.stand(a.player, a.x(proj), a.get("ent_y", proj))
    a.call("act_init_entity", x=a.player)
    a.put("act_hp", a.player, 99)
    a.put("act_invuln", a.player, 0)
    hp0 = a.get("act_hp", a.player)
    a.call("act_proj_update")
    r.check("銃口に重なって立っていても、弾が生まれた最初のフレームには当たらない"
            "（必ず1フレームは画面に出る / ACT_PROJ_ARM=%d）" % D["ACT_PROJ_ARM"],
            a.get("act_hp", a.player) == hp0 and a.get("ent_active", proj),
            "1回目の進行でいきなり %d ダメージ入った（弾はまだ %s）。"
            "ACT_PROJ_ARM が 0 だと、至近で撃たれた弾は出たその瞬間に当たって消え、"
            "**当たり判定があるのに絵が一度も出ない**。銃口に1フレーム留めること"
            % (hp0 - a.get("act_hp", a.player),
               "居る" if a.get("ent_active", proj) else "消えている"))
    landed = None
    for i in range(4):
        a.call("act_proj_update")
        if a.get("act_hp", a.player) < hp0:
            landed = i + 2
            break
    r.check("留まっていた弾は、その後ちゃんと当たる（この検証が空振りでない）",
            landed is not None,
            "銃口に重ねた相手に %d フレーム進めても当たらなかった。"
            "当たらない場で「最初のフレームには当たらない」と言っても何も確かめていない" % 5)

    # --- 3. 飛ぶ（速さは技表どおり）---
    a.sleep_all()
    clear_free()
    proj = fire(a.enemy)
    if proj is None:
        return
    born_x = a.x(proj)
    for _ in range(D["ACT_PROJ_ARM"]):
        a.call("act_proj_update")
    FLY = 8
    for _ in range(FLY):
        a.call("act_proj_update")
    want = FLY * speed // 16
    r.check("弾が技表の速さ（atk_speed=%d/16 ドット/f）で飛ぶ" % speed,
            a.x(proj) - born_x == want,
            "%d フレームで %d ドット進んだ（期待 %d）。速さは技表の "
            "ATK_ROW_BULLET 行（atk_speed）が決める。端数（act_sub）の持ち越しが"
            "壊れていると、ここがずれる" % (FLY, a.x(proj) - born_x, want))

    # --- 4. 寿命で消える（外れた弾が枠に残り続けない）---
    died = None
    for i in range(life + 4):
        a.call("act_proj_update")
        if not a.get("ent_active", proj):
            died = FLY + i + 1
            break
    r.check("誰にも当たらなかった弾が寿命（atk_life=%d フレーム）で消える" % life,
            died == life,
            "弾が %s（期待は飛び始めてから %d フレーム目）。消えないと空き枠が %d 個しか"
            "無いのに埋まり続け、次の弾が**黙って出なくなる**。act_proj_step の "
            "`dec act_timer / beq @expire` を見よ"
            % ("%d フレーム目に消えた" % died if died else "消えなかった",
               life, D["ENT_FREE_COUNT"]))
    r.check("消えた弾の枠は飛び道具の印（ACT_F_PROJ）ごと返る（次の弾が使える）",
            not a.get("act_flags", proj) & D["ACT_F_PROJ"],
            "act_flags=$%02X のまま。印が残った枠を別の用途（P2 のアイテム）が使うと、"
            "act_proj_update がそれを弾として飛ばす" % a.get("act_flags", proj))

    # --- 5. 当たる / 撃った主の味方には当たらない ---
    def shoot_at(victim_slot, distance):
        a.sleep_all()
        clear_free()
        p = fire(a.enemy)
        if p is None:
            return None
        a.stand(victim_slot, a.x(p) + distance, y)
        a.call("act_init_entity", x=victim_slot)
        a.put("act_hp", victim_slot, 99)
        a.put("act_invuln", victim_slot, 0)
        hp0 = a.get("act_hp", victim_slot)
        for _ in range(D["ACT_PROJ_ARM"] + life):
            a.call("act_proj_update")
            if a.get("act_hp", victim_slot) < hp0:
                return ("hit", a.get("ent_active", p))
            if not a.get("ent_active", p):
                return ("gone", 0)
        return ("alive", 1)

    span = max(1, speed // 16)
    got = shoot_at(a.player, span * 6)
    r.check("撃った主の**相手**（敵 → 操作キャラ）には当たり、当たった弾は消える",
            got is not None and got[0] == "hit" and not got[1],
            "弾を %d ドット先の操作キャラへ飛ばしたら %s。"
            "当たらないなら act_build_proj_rect か act_scan_targets が狙う区画を"
            "間違えている。当たって消えないなら弾が貫通している（枠も返らない）"
            % (span * 6, {"hit": "当たったが消えなかった", "gone": "誰にも当たらず消えた",
                          "alive": "飛び続けた", None: "そもそも出なかった"}
               .get(got[0] if got else None, got)))

    friend = a.enemy + 1                                 # 撃った主と同じ区画＝味方
    got = shoot_at(friend, span * 6)
    r.check("**撃った主の味方には当たらない**（敵の弾が敵を撃たない）",
            got is not None and got[0] != "hit",
            "同じ敵区画の #%d を %d ドット先に置いたら弾が当たった。"
            "狙う区画は act_target_range が act_proj_owner（撃った主）から決める。"
            "ここが弾自身の番号（空き枠 = 敵区画より後ろ）で決まると、"
            "**敵の弾が敵に当たる**（味方討ち）" % (friend, span * 6))


# ==========================================================================
# 移動の強弱 —— 歩きと小走り（速さには代償がある）
# ==========================================================================
def _check_run(rom_path, labels, r):
    """小走りの**3つの代償**と、「歩く速さ以下では今までと同じ」こと。

    主の提案は「小走りになる、ダッシュする、といった動きのバリエーション」だが、
    速いだけの選択肢は選択にならない。action-dev は速さと引き換えに3つを失う形にした
    （action_params.inc §2-a）:

        代償1 向きを変えにくい … 歩く速さ以下に落ちるまで向きが変わらない
        代償2 攻撃に移れない   … 同上。ただし**先行入力は捨てない**
        代償3 奥行きが鈍る     … 走っている間だけ上下が遅い

    もう一つ、**主が実機で承認した手触りを壊していないこと**も同じ重さで縛る。
    歩く速さ以下では、押した瞬間に歩き出し・離した瞬間に止まり・逆を押せばその場で
    振り向く。ここに慣性が漏れ出したら、それは承認された手触りの変更である。

    期待値はすべて action_params.inc のノブから計算する（ACT_RUN_SPEED を変えても
    落ちない）。走りを丸ごと切った設定（ACT_RUN_ENABLE = 0）では、
    **主張の形の方が変わる**（B を押しても歩きのままであること）。
    """
    r.section("移動の強弱（歩き / 小走りの3つの代償。ノブは action_params.inc）")

    walk, run = D["ACT_MOVE_SPEED"], D["ACT_RUN_SPEED"]
    a = Actors(rom_path, labels)
    y = (a.get("depth_y_min") + a.get("depth_y_max")) // 2
    a.sleep_all()

    def rest(x0=48):
        a.nes.set_buttons(set())
        step_frame(a.nes, labels, 1)
        a.stand(a.player, x0, y)
        a.call("act_init_entity", x=a.player)
        for name in ("act_buf_atk", "act_pad_ignore"):
            if name in labels:
                a.put(name, 0, 0)
        return x0

    def watch(s):
        return (s.x(s.player), _sgn8(s.get("act_vel", s.player)),
                s.get("act_face", s.player), s.get("ent_state", s.player),
                s.get("ent_y", s.player), s.get("act_buf_atk", 0))

    # ---------------- 歩き（主が承認した手触り。ここは変わっていないこと）----------------
    N = 16
    x0 = rest()
    rows = a.hold({"RIGHT"}, N, watch)
    want = N * walk // 16
    r.check("歩きは %d フレームで %d ドット進む（ACT_MOVE_SPEED=%d/16 ドット/f）"
            % (N, want, walk), rows[-1][0] - x0 == want and rows[0][1] == walk,
            "%d ドット進み、1フレーム目の速さは %d だった（期待 %d ドット / 速さ %d）。"
            "歩きは押した**そのフレーム**から歩く速さで動くのが約束である"
            "（慣性が働くのは歩く速さを超えている間だけ）"
            % (rows[-1][0] - x0, rows[0][1], want, walk))

    stop = a.hold(set(), 2, watch)
    r.check("方向を離した次のフレームに止まる（歩く速さからの慣性は無い）",
            stop[0][0] == stop[1][0] == rows[-1][0] and stop[1][1] == 0,
            "離した後も %d → %d → %d と滑った（速さ %d）。歩く速さ以下は即停止が"
            "**主の承認した手触り**である。ここに慣性が漏れると、小走りの代償ではなく"
            "歩きそのものが変わる" % (rows[-1][0], stop[0][0], stop[1][0], stop[1][1]))

    rest()
    turn = a.hold({"LEFT"}, 1, watch)
    r.check("歩く速さでは、逆を押したその場で振り向く（代償が出るのは小走り中だけ）",
            turn[0][2] == 1 and turn[0][1] == -walk,
            "1フレーム押して 向き=%d / 速さ=%d（期待 向き=1 / 速さ=%d）。"
            "止まっている所から逆を押したときまで向きが変わらないなら、"
            "それは代償ではなく操作不能である" % (turn[0][2], turn[0][1], -walk))

    # ---------------- 走りを切ってある設定では、ここで主張の形が変わる ----------------
    if not D["ACT_RUN_ENABLE"]:
        rest()
        rows = a.hold({"B", "RIGHT"}, N, watch)
        r.check("ACT_RUN_ENABLE=0 では B を押しても歩きのままである",
                all(v == walk for _, v, _, _, _, _ in rows),
                "B を押しながら歩いたら速さが %s になった。走りを切った設定なので"
                "歩く速さ（%d）から変わってはならない"
                % (sorted({v for _, v, _, _, _, _ in rows}), walk))
        return

    # ---------------- 小走り: 加速 ----------------
    accel = D["ACT_RUN_ACCEL"]
    ramp = _ceil_div(run - walk, accel) + 2
    x0 = rest()
    rows = a.hold({"B", "RIGHT"}, ramp, watch)
    vels = [v for _, v, _, _, _, _ in rows]
    want_vels = [min(run, walk + i * accel) for i in range(ramp)]
    r.check("B を押しながら走ると、歩く速さ %d から %d/16 ドット/f ずつ最高速 %d まで上がる"
            % (walk, accel, run), vels == want_vels,
            "速さの推移が %s（期待 %s）。走り出しは**歩く速さから**であり"
            "（いきなり最高速だと「小走りになる」過程が消える）、"
            "上限は ACT_RUN_SPEED=%d で頭打ちになること" % (vels, want_vels, run))
    r.check("小走りは歩きより速く進む（%d フレームで %d ドット > 歩きの %d ドット）"
            % (ramp, rows[-1][0] - x0, ramp * walk // 16),
            rows[-1][0] - x0 > ramp * walk // 16,
            "%d フレームで %d ドット。歩けば %d ドットなので、速くなっていない"
            % (ramp, rows[-1][0] - x0, ramp * walk // 16))

    # ---------------- 代償1: 向きを変えにくい ----------------
    turn_vel, brake = D["ACT_TURN_VEL"], D["ACT_RUN_TURN_BRAKE"]
    want_slide = _ceil_div(run - turn_vel, brake)
    rows = a.hold({"B", "LEFT"}, want_slide + 4, watch)
    slide = next((i for i, row in enumerate(rows) if row[2] == 1), None)
    slid_back = [i for i in range(1, len(rows))
                 if rows[i][2] == 0 and rows[i][0] < rows[i - 1][0]]
    r.check("代償1: 最高速から逆を押しても、歩く速さ %d まで落ちるまで向きが変わらない"
            "（%d フレーム滑る）" % (turn_vel, want_slide),
            slide == want_slide and not slid_back,
            "向きが変わったのは %s フレーム目（期待 %d = (最高速 %d - %d) / 減速 %d）、"
            "滑っている間に後ろへ動いたフレーム %s。逆を押した瞬間に向きが変われば"
            "**速いだけで代償が無い**ことになる（ACT_TURN_VEL を ACT_RUN_SPEED まで"
            "上げるとそうなる。それは仕様変更である）"
            % (slide, want_slide, run, turn_vel, brake, slid_back))

    # ---------------- 代償2: 攻撃に移れない。ただし先行入力は捨てない ----------------
    atk_vel = D["ACT_ATK_VEL"]
    rest()
    a.hold({"B", "RIGHT"}, ramp)
    rows = a.hold({"B", "RIGHT", "A"}, 1, watch)
    rows += a.hold({"B", "RIGHT"}, D["ACT_BUF_FRAMES"] + 2, watch)
    swung = next((i for i, row in enumerate(rows) if row[3] == D["ACT_ST_ATK_START"]), None)
    too_fast = [i for i, row in enumerate(rows)
                if row[3] == D["ACT_ST_ATK_START"] and abs(rows[i - 1][1]) > atk_vel]
    dropped = swung is not None and any(row[5] == 0 and row[3] != D["ACT_ST_ATK_START"]
                                        for row in rows[:swung])
    r.check("代償2: 走っている間は振れないが、先行入力は捨てずに %d フレーム以内に出る"
            % D["ACT_BUF_FRAMES"],
            swung is not None and swung > 0 and not too_fast and not dropped,
            "Aを押してから %s フレーム目に振った（速さが %d 以下に落ちる前に振った"
            "フレーム %s / 先行入力を落としたか %s）。**先行入力は捨てない**のが規約で、"
            "捨てると「押したのに出ない」になる。すぐ振れると代償2 が消える"
            % (swung, atk_vel, too_fast, dropped))

    # ---------------- 代償3: 奥行きが鈍る ----------------
    DN = 16
    rest()
    y_start = a.get("ent_y", a.player)
    rows = a.hold({"UP"}, DN, watch)
    walked_depth = y_start - rows[-1][4]
    want_walk_depth = DN * D["ACT_DEPTH_SPEED"] // 16
    rest()
    a.hold({"B", "RIGHT"}, ramp)
    y_start = a.get("ent_y", a.player)
    rows = a.hold({"B", "RIGHT", "UP"}, DN, watch)
    ran_depth = y_start - rows[-1][4]
    want_run_depth = DN * D["ACT_RUN_DEPTH_SPEED"] // 16
    r.check("代償3: 歩きながらなら %d フレームで奥へ %d ドット、走りながらだと %d ドット"
            % (DN, want_walk_depth, want_run_depth),
            walked_depth == want_walk_depth and ran_depth == want_run_depth,
            "歩き %d ドット（期待 %d / ACT_DEPTH_SPEED=%d）、走り %d ドット"
            "（期待 %d / ACT_RUN_DEPTH_SPEED=%d）。横に速い代わりに縦の回避は歩いた方が"
            "速い、というのが代償3 である。**予備動作を見てから奥へ避けるなら"
            "走るのをやめる判断が要る**。同じ値にすると代償が消える"
            % (walked_depth, want_walk_depth, D["ACT_DEPTH_SPEED"],
               ran_depth, want_run_depth, D["ACT_RUN_DEPTH_SPEED"]))
    a.nes.set_buttons(set())


def _sgn8(v):
    return v - 256 if v > 127 else v


def _ceil_div(a, b):
    return -(-a // max(1, b))
