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
from nes import Nes, boot, frame_end, load_labels   # noqa: E402
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
