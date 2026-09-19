"""L2 実行検証 — engine P1（連続の奥行き / エンティティ表 / OAM 割当 / 向き・影・効果 / 当たり判定）。

run_tests.py から呼ばれる。層の分け方は ADR-0004 に従う。

**レーンは ADR-0009 で廃止した。** 奥行きは連続であり、その実体は足元Y（ent_y）である。
量子化・補間・補間中の入力破棄・レーン一致判定を見ていた検証は、主張そのものが
消えたので削除した（何を消したかは ADR-0009 §帰結 qa-runner）。
代わりに見るのは「歩ける帯の上下限」「足元Yの距離」である。

ここで検証してよいのは「値」であって「タイミング」ではない。
1スキャンライン8スプライト制約によるちらつき、MMC3 IRQ の分割位置、
スプライト0ヒットのようなスキャンライン単位の挙動は、この Python エミュレータでは
再現していないので検証できない。それらは L3（Mesen2）と実機に回すこと（ADR-0002 / ADR-0004）。

方針:
  * 期待値は ROM の表（body_width / depth_y_min / depth_y_max）と src/*.inc の定数
    （test/srcdefs.py が読む）から導く。**テスト側に数値を焼き付けない。**
    主がパラメータ（奥行きの速さ・歩ける帯）を調整したときに、仕様が壊れていないのに
    テストが落ちる、という事態を避けるため。
  * ラベルが欠けたときは**それを使う節だけ**が落ちる（test/gate.py）。
    以前は層の先頭でまとめて検査していたため、1つの名前替えで44件が黙って消えた。
  * 失敗メッセージは「何が期待と違い、何が壊れている疑いがあるか」を1行で書く。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "harness"))
sys.path.insert(0, HERE)

from cpu6502 import CpuCrash                  # noqa: E402
from nes import Nes, VBLANK_CYCLES, boot, frame_end   # noqa: E402
from scene import disarm_enemies                    # noqa: E402
from gate import Gate, run_sections, boot_or_fail   # noqa: E402
from srcdefs import action_defs                     # noqa: E402

# --- 値の出どころ ---
# src/constants.inc の定数は srcdefs が読む（テスト側に焼き付けない）。
# OAM のエントリ数だけは PPU のハード制約なので、ここに持っていてよい。
OAM_SPRITE_MAX = 64          # PPU のハード制約（OAM は 64 エントリ）
OAM_Y_OFFSCREEN = 0xFF
ENT_ACTIVE = 0x80
ENT_INACTIVE = 0x00

D = action_defs()            # src/constants.inc + src/action/*.inc の定数
ENT_Y_TO_OAM = D["ENT_Y_TO_OAM"]      # SPRITE_H + 1。足元Y → OAM の Y バイト
SPRITE_W = D["SPRITE_W"]
SPRITE_H = D["SPRITE_H"]
SORT_KEY_DEPTH_SHIFT = D["SORT_KEY_DEPTH_SHIFT"]
SPR_CLASS_PLAYER = D["SPR_CLASS_PLAYER"]
SPR_CLASS_PROJECT = D["SPR_CLASS_PROJECT"]
SPR_CLASS_NEAR_ENE = D["SPR_CLASS_NEAR_ENE"]
SPR_CLASS_FAR_ENE = D["SPR_CLASS_FAR_ENE"]
SPR_CLASS_EFFECT = D["SPR_CLASS_EFFECT"]
ENT_ATTR_SHADOW = D["ENT_ATTR_SHADOW"]
ENT_ATTR_HFLIP = D["ENT_ATTR_HFLIP"]
ENT_ATTR_OAM_MASK = D["ENT_ATTR_OAM_MASK"]
SPR_POSE_STRIDE = D["SPR_POSE_STRIDE"]
SPR_POSE_COUNT = D["SPR_POSE_COUNT"]
SPR_TILE_SHADOW = D["SPR_TILE_SHADOW"]

# ent_active..ent_attr は「MAX_ENTITIES ごとに並ぶ」区間である（entity.s の注記）。
# ent_tile0 は**その後ろ**に置く約束なので、この並びには入れない。
ENT_FIELDS = ("ent_active", "ent_x_lo", "ent_x_hi", "ent_y", "ent_state", "ent_class",
              "ent_body", "ent_ai", "ent_tile", "ent_attr")

# 足場（シーンを組み立てて OAM を作る）に要るラベル。ここが欠けたときだけ層ごと諦める。
CORE = ENT_FIELDS + ("oam_build", "oam_used", "oam_shadow", "cam_x_lo", "cam_x_hi", "wait_nmi")

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

    def place(self, i, x=0, y=196, cls=SPR_CLASS_FAR_ENE, body=0, tile=0, attr=0, face=0):
        """エンティティを1体置く。**奥行きは y（足元Y）そのもの**である（ADR-0009）。

        向き（act_face）まで面倒を見るのは、OAM の属性バイトが act_face から
        作り直されるため（sprite.s）。置く側が向きを決めておかないと、
        前の検証が残した向きで絵が裏返り、**engine が正しいまま落ちる**検証になる。
        """
        self.poke("ent_active", i, ENT_ACTIVE)
        self.poke("ent_x_lo", i, x & 0xFF)
        self.poke("ent_x_hi", i, (x >> 8) & 0xFF)
        self.poke("ent_y", i, y)
        self.poke("ent_class", i, cls)
        self.poke("ent_body", i, body)
        self.poke("ent_tile", i, tile)
        self.poke("ent_attr", i, attr)
        if "act_face" in self.labels:
            self.poke("act_face", i, face)


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


def table_length(labels, name):
    """ラベル name から、その次に来るラベルまでの距離を表の項数とみなす。"""
    addr = labels[name]
    after = [a for a in labels.values() if a > addr]
    return min(after) - addr if after else 0


# ---------------------------------------------------------------- 入口
# 節ごとに「その節が使うラベル」を宣言する。欠けたラベルを使う節だけが落ち、
# 残りは走る（test/gate.py の冒頭に理由を書いた）。
def layer2_engine(rom_path, labels, r):
    print("L2 実行検証 / engine P1（奥行き・エンティティ・OAM・向き・影・効果・当たり判定）")

    gate = Gate(labels, r, "engine")
    if not boot_or_fail(gate, CORE, "エンティティ表の読み書きと OAM の組み立てに使う"):
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

    sections = (
        ("テーブルの前提", (), _check_table_layout),
        ("歩ける帯（奥行きの上下限）",
         ("depth_y_min", "depth_y_max", "depth_set_band", "depth_clamp"), _check_depth_band),
        ("ent_depth_move", ("ent_depth_move", "depth_y_min", "depth_y_max"),
         _check_ent_depth_move),
        ("depth_distance", ("depth_distance",), _check_depth_distance),
        ("NMI の分担", ("oam_order", "oam_next", "sort_count", "oam_dropped"),
         _check_nmi_budget),
        ("OAM 並べ替え", ("oam_sort_order", "oam_order", "sort_count", "oam_sortkey_guard",
                         "depth_y_min", "depth_y_max"), _check_sort_order),
        ("OAM 展開", ("body_width", "oam_dropped"), _check_oam_emit),
        ("向き（水平反転）", ("act_face", "body_width"), _check_facing),
        ("影", ("body_width", "oam_fx_dropped", "oam_dropped", "ent_activate"), _check_shadow),
        ("効果スプライト (fx)",
         ("fx_spawn", "fx_update", "fx_clear_all", "fx_life", "fx_x_lo", "fx_x_hi",
          "fx_y", "fx_tile", "fx_attr", "fx_arg_x_lo", "fx_arg_x_hi", "fx_arg_y",
          "fx_arg_tile", "fx_arg_attr", "fx_arg_life"), _check_fx),
        ("姿勢 (ent_set_pose)", ("ent_set_pose", "ent_tile0", "ent_activate"), _check_pose),
        ("rect_overlap", ("rect_overlap", "rect_a", "rect_b"), _check_rect_overlap),
    )
    run_sections(gate, sections, lambda fn: (nes, labels, ents, r))


# ---------------------------------------------------------------- 前提の確認
def _check_table_layout(nes, labels, ents, r):
    """以降の検証が寄りかかっている前提（SoA の配列長）を先に確かめる。"""
    r.section("テーブルの前提")

    strides = {}
    for a, b in zip(ENT_FIELDS, ENT_FIELDS[1:]):
        strides[(a, b)] = labels[b] - labels[a]
    bad = ["%s→%s が %d" % (a, b, d) for (a, b), d in strides.items() if d != ents.count]
    r.check("エンティティ配列が MAX_ENTITIES(%d) ごとに並んでいる" % ents.count, not bad,
            "%s（期待はすべて %d）。SoA の配列長が揃っていないと "
            "`lda ent_y, x` の x が別の属性を指す。entity.s の .res の並びを見よ"
            % ("、".join(bad), ents.count))

    r.check("MAX_ENTITIES がラベルの間隔と constants.inc で一致する (%d)" % ents.count,
            ents.count == D["MAX_ENTITIES"],
            "ラベルの間隔は %d だが constants.inc の MAX_ENTITIES は %d。"
            "テストがエンティティ表の長さを取り違えており、以降の「全スロットに置く」系の"
            "検証が表からはみ出すか、途中までしか見ない"
            % (ents.count, D["MAX_ENTITIES"]))

    # ent_tile0 は ent_attr より後ろに置く約束である（entity.s）。上の「MAX_ENTITIES ごと」の
    # 検査は ent_active..ent_attr の区間しか見ないので、この1本で約束を明示的に縛る。
    if "ent_tile0" in labels:
        r.check("ent_tile0 がエンティティ表の並びの後ろにある",
                labels["ent_tile0"] >= labels["ent_attr"] + ents.count,
                "ent_tile0=$%04X、ent_attr=$%04X（+%d）。ent_active..ent_attr の連なりの"
                "**内側**に割り込むと、上の「MAX_ENTITIES ごとに並ぶ」が崩れる"
                % (labels["ent_tile0"], labels["ent_attr"], ents.count))


# ---------------------------------------------------------------- NMI の分担
def _check_nmi_budget(nes, labels, ents, r):
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
def _check_sort_order(nes, labels, ents, r):
    """oam_order が「優先度クラス昇順 → 足元Y降順 → 添字昇順（安定）」であること。

    足元Yは実装上 8ライン刻みに量子化されるが、それは実装の都合なので期待値には持ち込まない。
    歩ける帯を4等分した足元Y（互いに 8 以上離れている）だけを使い、量子化に依存せずに順序を見る。
    レーンは廃止された（ADR-0009）ので、奥行きは連続の足元Yそのものである。
    """
    r.section("OAM 並べ替え（ADR-0002）")

    depths = _depth_samples(nes, labels, 4)
    q = [d >> SORT_KEY_DEPTH_SHIFT for d in depths]
    if not r.check("並べ替えに使う4つの足元Y %s が %dライン刻みでも区別できる" % (depths, 1 << SORT_KEY_DEPTH_SHIFT),
                   len(set(q)) == len(q),
                   "歩ける帯 %d..%d を4等分した足元Y %s は、%dライン刻みに落とすと %s と"
                   "重複する。並べ替えキーが同値になり、この節は「奥行き順」ではなく"
                   "「添字順」を見ていることになる。帯を広げるか、検証の取り方を変えること"
                   % (nes.ram[labels["depth_y_min"] & 0x7FF],
                      nes.ram[labels["depth_y_max"] & 0x7FF], depths,
                      1 << SORT_KEY_DEPTH_SHIFT, q)):
        return

    # (添字, クラス, 足元Y)。同じキーの組を2つ入れて安定性を見る。空きスロットも混ぜる。
    # #1 と #4 は歩ける帯の外（画面の上端寄り / 下端寄り）に置いてある。
    # クラスは奥行きより強い（ADR-0002 はクラス0 を「毎フレーム必ず描く」と定めている）ので、
    # 「最も奥のクラス0」が「最も手前のクラス1」より先に出ることを見る。
    scene = [
        (1,  SPR_CLASS_PLAYER,   32),          # 画面のいちばん奥に居るクラス0
        (4,  SPR_CLASS_PROJECT,  232),         # 画面のいちばん手前に居るクラス1
        (0,  SPR_CLASS_PLAYER,   depths[1]),
        (2,  SPR_CLASS_FAR_ENE,  depths[3]),
        (3,  SPR_CLASS_FAR_ENE,  depths[0]),
        (5,  SPR_CLASS_NEAR_ENE, depths[2]),
        (6,  SPR_CLASS_FAR_ENE,  depths[3]),   # #2 と同じキー（安定なら #2 が先）
        (7,  SPR_CLASS_EFFECT,   depths[3]),
        (9,  SPR_CLASS_PROJECT,  depths[0]),
        (11, SPR_CLASS_FAR_ENE,  depths[1]),
        (13, SPR_CLASS_NEAR_ENE, depths[2]),   # #5 と同じキー（安定なら #5 が先）
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
    # 期待値 1 は「ent_attr のパレット指定がそのまま出る」ことだけを見ている。
    # OAM の属性 bit6（水平反転）は **act_face から作り直される**（sprite.s）ので、
    # 右向き (act_face=0) であることを ents.place が明示的に置いている。
    # ここを暗黙の前提にしていると、この節より前に LEFT を押す検証を足した日に
    # **engine が正しいまま落ちる**（engine-dev の申し送り7）。
    r.check("属性バイトがエンティティの ent_attr のまま出る（右向き = act_face 0 のとき）",
            all(c[2] == 1 for c in cols),
            "属性=%s（期待は全て 1）。パレット指定が落ちると色が化ける。"
            "$41 なら水平反転ビットが立っている＝act_face が 0 でない"
            % ["$%02X" % c[2] for c in cols])

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


def _check_rect_overlap(nes, labels, ents, r):
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


# ---------------------------------------------------------------- 歩ける帯
# レーン（4段階の量子化）の代わりに入った座標系である（ADR-0009）。
# 見るのは2つ: 「帯の外に出ないこと」と「帯が定数ではなく変数であること」。
# 帯はステージの性質（ステージごとに変わる）なので、コードに埋まっていたら
# stage-author が P3 で足場ごとに帯を変えられない。
def _band(nes, labels):
    return (nes.ram[labels["depth_y_min"] & 0x7FF], nes.ram[labels["depth_y_max"] & 0x7FF])


def _depth_samples(nes, labels, n):
    """歩ける帯を n 等分した足元Yを返す（テストに Y の数値を焼き付けないため）。"""
    lo, hi = _band(nes, labels)
    if n == 1:
        return [lo]
    return [lo + (hi - lo) * i // (n - 1) for i in range(n)]


def _depth_frames(band, speed, margin=24):
    """帯の端から端まで歩くのに要るフレーム数（速さのノブから導く）。"""
    return (band[1] - band[0]) * 16 // max(1, speed) + margin


def _check_depth_band(nes, labels, ents, r):
    r.section("歩ける帯（奥行きの上下限。ADR-0009）")

    lo, hi = _band(nes, labels)
    r.check("起動時の歩ける帯が constants.inc の既定値 (%d..%d) になっている"
            % (D["DEPTH_Y_MIN_DEFAULT"], D["DEPTH_Y_MAX_DEFAULT"]),
            (lo, hi) == (D["DEPTH_Y_MIN_DEFAULT"], D["DEPTH_Y_MAX_DEFAULT"]),
            "depth_y_min=%d / depth_y_max=%d（constants.inc の既定は %d..%d）。"
            "depth_init が呼ばれていないか、帯を誰かが書き換えたまま起動が終わっている"
            % (lo, hi, D["DEPTH_Y_MIN_DEFAULT"], D["DEPTH_Y_MAX_DEFAULT"]))

    r.check("帯が上下逆でなく、OAM に変換できる範囲に収まっている (%d..%d)" % (lo, hi),
            ENT_Y_TO_OAM <= lo < hi < 0xEF,
            "depth_y_min=%d / depth_y_max=%d。%d 未満の足元Yは OAM の Y 計算で借りが出て"
            "描かれず、$EF 以上は画面外。上下が逆だとクランプが帯の外へ押し出す"
            % (lo, hi, ENT_Y_TO_OAM))

    # --- depth_clamp: 帯の外の足元Yを帯へ畳む。境界そのものは動かさない ---
    cases = [(lo - 1, lo, "帯より1ドット奥"), (lo, lo, "帯の奥端ちょうど"),
             (lo + 1, lo + 1, "帯の中"), (hi - 1, hi - 1, "帯の中（手前寄り）"),
             (hi, hi, "帯の手前端ちょうど"), (hi + 1, hi, "帯より1ドット手前"),
             (0, lo, "画面の最上端"), (0xFF, hi, "画面の最下端")]
    wrong = []
    for value, want, why in cases:
        res = nes.call(labels["depth_clamp"], a=value & 0xFF, x=0x5A, y=0xA5)
        if res.a != want:
            wrong.append("%s (A=%d) → %d（期待 %d）" % (why, value & 0xFF, res.a, want))
        if (res.x, res.y) != (0x5A, 0xA5):
            wrong.append("%s で X/Y が壊れた (X=$%02X Y=$%02X、期待 $5A/$A5)"
                         % (why, res.x, res.y))
    r.check("depth_clamp が足元Yを歩ける帯 (%d..%d) に畳む（%d ケース・X と Y を壊さない）"
            % (lo, hi, len(cases)), not wrong,
            "%s。帯の外の足元Yは、置いた側が帯を知らなくても畳まれるのが約束である"
            % "／".join(wrong))

    # --- 帯は変数である: depth_set_band で差し替えられること（ステージごとに変わる） ---
    new_lo, new_hi = lo + 10, hi - 20
    res = nes.call(labels["depth_set_band"], a=new_lo, y=new_hi, x=0x33)
    got = _band(nes, labels)
    moved = nes.call(labels["depth_clamp"], a=lo).a
    nes.call(labels["depth_set_band"], a=lo, y=hi)          # 元に戻す
    r.check("depth_set_band で歩ける帯を差し替えられる（帯は定数ではなく変数）",
            got == (new_lo, new_hi) and moved == new_lo and _band(nes, labels) == (lo, hi),
            "帯を %d..%d に差し替えたら depth_y_min/max=%s になり、古い奥端 %d を"
            "clamp した結果が %d（期待 %d）。帯がコードに埋まっていると、"
            "P3 で stage-author がステージごとの床の広さを持てない（ADR-0009 残る論点2）"
            % (new_lo, new_hi, got, lo, moved, new_lo))
    r.check("depth_set_band が X を壊さない", res.x == 0x33,
            "X が $33 → $%02X に変わった。帯を張り直す側がエンティティ番号を"
            "持ったまま呼べることが約束である（depth.s の注記）" % res.x)

    # --- 実際に操作キャラを端まで歩かせる。端で張り付いても壊れないこと ---
    # 見たいのは奥行きのクランプであって戦闘下の挙動ではないので、敵を眠らせる
    # （殴られると のけぞりで入力が通らず、engine が正しいまま落ちる）。
    restore = disarm_enemies(nes, labels)
    # 端の手前に置いてから押す。帯の端から端まで歩かせても見えるものは同じで、
    # 回すフレーム数だけが増える（CI が遅いと誰も回さなくなる）。
    run_up = 10
    frames = _depth_frames((0, run_up), D["ACT_DEPTH_SPEED"], margin=8)
    for button, edge, sign, where in (("UP", lo, +1, "奥"), ("DOWN", hi, -1, "手前")):
        ents.poke("ent_y", 0, edge + sign * run_up)
        nes.set_buttons({button})
        nes.run_frames(frames)
        frame_end(nes, labels)
        at_edge = ents.peek("ent_y", 0)
        nes.run_frames(20)                  # 端に着いてからも押し続ける
        frame_end(nes, labels)
        stuck = ents.peek("ent_y", 0)
        nes.set_buttons(set())
        r.check("%s を押し続けると足元Yが帯の%s端 %d で止まり、押し続けても抜けない"
                % (button, where, edge),
                at_edge == edge and stuck == edge,
                "端の %d ドット手前から %d フレーム押して足元Y=%d、さらに20フレーム押して %d"
                "（期待はどちらも %d）。帯を抜けると足元Yが 0/255 を回り込み、"
                "OAM の Y 計算で借りが出てキャラが画面から消える。"
                "奥行きの速さは %d/16 ドット/f（action_params.inc の ACT_DEPTH_SPEED）"
                % (run_up, frames, at_edge, stuck, edge, D["ACT_DEPTH_SPEED"]))
    restore()


# ---------------------------------------------------------------- ent_depth_move
def _check_ent_depth_move(nes, labels, ents, r):
    """X = エンティティ, A = 符号つき移動量。桁あふれ・借り・クランプ。"""
    r.section("ent_depth_move（足元Yを符号つきの量だけ動かす）")

    lo, hi = _band(nes, labels)
    slot = ents.count - 1                       # 操作キャラや仲間に触らない空きスロット
    mid = (lo + hi) // 2

    # (足元Yの初期値, 移動量, 期待, 何を見ているか)
    cases = [
        (mid, 0, mid, "移動量 0 では動かない"),
        (mid, 5, mid + 5, "手前へ 5"),
        (mid, -5, mid - 5, "奥へ 5"),
        (hi - 1, 1, hi, "手前端ちょうどに着く"),
        (hi - 1, 2, hi, "手前端を越えようとしてクランプされる"),
        (lo + 1, -1, lo, "奥端ちょうどに着く"),
        (lo + 1, -2, lo, "奥端を越えようとしてクランプされる"),
        (hi, 1, hi, "手前端で押し続けても動かない"),
        (lo, -1, lo, "奥端で押し続けても動かない"),
        (250, 10, hi, "足元Yの加算が 255 を越える（桁あふれ）"),
        (250, 0, hi, "帯より手前に居た状態で手前へ押しても帯を越えない"),
        (5, -10, lo, "足元Yの減算が 0 を下回る（借り）"),
        (mid, 127, hi, "移動量の最大（+127）"),
        (mid, -128, lo, "移動量の最小（-128）"),
    ]
    wrong = []
    for start, delta, want, why in cases:
        ents.poke("ent_y", slot, start)
        res = nes.call(labels["ent_depth_move"], x=slot, a=delta & 0xFF)
        got = ents.peek("ent_y", slot)
        if got != want:
            wrong.append("%s: 足元Y %d に %+d → %d（期待 %d）" % (why, start, delta, got, want))
        if res.x != slot:
            wrong.append("%s: X が %d → %d に壊れた（エンティティ番号を持ったまま呼べない）"
                         % (why, slot, res.x))
        if res.a != got:
            wrong.append("%s: 戻り値 A=%d が書いた足元Y %d と違う" % (why, res.a, got))
    r.check("ent_depth_move が帯 (%d..%d) の中で符号つきに動き、桁あふれ・借りでも"
            "帯を抜けない（%d ケース）" % (lo, hi, len(cases)), not wrong,
            "%s。押したフレームに押したぶんだけ動く（補間しない）のが ADR-0009 の要点であり、"
            "クランプは engine の持ち分である（action / ai は帯の値を知らない）"
            % "／".join(wrong))

    ents.poke("ent_active", slot, ENT_INACTIVE)


# ---------------------------------------------------------------- depth_distance
def _check_depth_distance(nes, labels, ents, r):
    """A, Y → A = |足元Yの差|, Z = 一致。**lane_distance の 4×4 の代わり**（ADR-0009）。"""
    r.section("depth_distance（足元Yの近さ。当たり判定の土台）")

    lo, hi = _band(nes, labels)
    values = sorted({0, 1, 2, 5, 6, 7, 127, 128, 129, lo, lo + 1, hi - 1, hi, 200, 254, 255})
    wrong, zwrong, xwrong = [], [], []
    for a in values:
        for b in values:
            res = nes.call(labels["depth_distance"], a=a, y=b, x=0x7E)
            if res.a != abs(a - b):
                wrong.append("(%d, %d) → %d（期待 %d）" % (a, b, res.a, abs(a - b)))
            if res.zero != (a == b):
                zwrong.append("(%d, %d) → Z=%d（期待 %d）" % (a, b, res.zero, a == b))
            if res.x != 0x7E:
                xwrong.append("(%d, %d) で X が $%02X に壊れた" % (a, b, res.x))
    r.check("depth_distance が %d×%d 全ての組で |足元Yの差| を返す" % (len(values), len(values)),
            not wrong,
            "%s。奥行きの距離が狂うと、Y許容幅での当たり判定（action-dev が使う）と"
            "AI の標的選択が同時に総崩れになる" % "、".join(wrong[:6]))
    r.check("depth_distance の Z フラグが「足元Yが一致」を表す", not zwrong,
            "%s。Z だけを見て一致判定をする呼び出し側が誤爆する" % "、".join(zwrong[:6]))
    r.check("depth_distance が X を壊さない（エンティティ番号を持ったまま呼べる）", not xwrong,
            "%s。combat.s / ai.s は X にエンティティ番号を置いたまま呼んでいる"
            % "、".join(xwrong[:3]))


# ---------------------------------------------------------------- 向き・影・効果
def _body_widths(nes, labels):
    n = table_length(labels, "body_width")
    if not 1 <= n <= 16:
        n = D["BODY_TYPE_COUNT"]
    return [nes.read(labels["body_width"] + b) for b in range(n)]


def _clear_fx(nes, labels):
    """前の節が残した効果スプライトを消す。OAM の余りに出るので数え違いの元になる。"""
    if "fx_clear_all" in labels:
        nes.call(labels["fx_clear_all"])


def _emit_one(nes, labels, ents, **kw):
    """エンティティ #0 を1体だけ置いて OAM を組み、(使用数, エントリの列) を返す。"""
    ents.clear()
    _clear_fx(nes, labels)
    set_camera(nes, labels, 0)
    ents.place(0, **kw)
    used, dropped = build_oam(nes, labels)
    return used, [shadow_entry(nes, labels, i) for i in range(used)], dropped


def _check_facing(nes, labels, ents, r):
    """向き（水平反転）の出どころが act_face ただ1つであること（sprite.s 冒頭の約束）。

    P1 の受入で主が指摘した「向きが画面に出ていない」への対応であり、
    ここが崩れると**左を向いて右へ歩く**絵になる。
    """
    r.section("向き（水平反転の出どころは act_face ただ1つ）")

    widths = _body_widths(nes, labels)
    y = _depth_samples(nes, labels, 2)[1]

    # --- 1. act_face が OAM 属性の bit6 を決める。ent_attr の bit6 は勝てない ---
    pal = 1
    cases = [(0, 0, pal, "右向き"),
             (1, 0, pal | ENT_ATTR_HFLIP, "左向き"),
             (0, ENT_ATTR_HFLIP, pal, "右向きなのに ent_attr に反転ビットが立っている"),
             (1, ENT_ATTR_HFLIP, pal | ENT_ATTR_HFLIP, "左向きで ent_attr にも反転ビット")]
    wrong = []
    for face, extra, want, why in cases:
        _, cols, _ = _emit_one(nes, labels, ents, x=64, y=y, cls=SPR_CLASS_PLAYER,
                               body=0, tile=4, attr=pal | extra, face=face)
        got = [c[2] for c in cols]
        if got != [want] * len(cols):
            wrong.append("%s (act_face=%d, ent_attr=$%02X): OAM 属性=%s（期待 $%02X）"
                         % (why, face, pal | extra, ["$%02X" % g for g in got], want))
    r.check("OAM の属性は act_face で作り直され、ent_attr の反転ビットは勝てない", not wrong,
            "%s。向きの出どころが2箇所あると、どちらが勝つかは書き順しだいになる。"
            "emit は ENT_ATTR_OAM_MASK($%02X) で落としてから act_face で作り直すこと"
            % ("／".join(wrong), ENT_ATTR_OAM_MASK))

    # --- 2. 体が2タイル幅以上のとき、左向きは列の並びが逆順になる ---
    # 水平反転は1枚ごとの絵を裏返すだけで、**列の並び順は裏返してくれない**。
    # ここが抜けると、体の左半分に右半分の絵が出る（顔と背中が入れ替わる）。
    tile0 = 4
    problems = []
    seen_widths = []
    for body, w in enumerate(widths):
        if w < 2:
            continue
        seen_widths.append(w)
        _, right, _ = _emit_one(nes, labels, ents, x=64, y=y, cls=SPR_CLASS_PLAYER,
                                body=body, tile=tile0, attr=0, face=0)
        _, left, _ = _emit_one(nes, labels, ents, x=64, y=y, cls=SPR_CLASS_PLAYER,
                               body=body, tile=tile0, attr=0, face=1)
        want_tiles = [tile0 + SPR_POSE_STRIDE * i for i in range(w)]
        if [c[1] for c in right] != want_tiles:
            problems.append("体格%d(幅%d) 右向きのタイル=%s（期待 %s）"
                            % (body, w, [c[1] for c in right], want_tiles))
        if [c[1] for c in left] != want_tiles[::-1]:
            problems.append("体格%d(幅%d) 左向きのタイル=%s（期待 %s = 右向きの逆順）"
                            % (body, w, [c[1] for c in left], want_tiles[::-1]))
        if [c[3] for c in left] != [c[3] for c in right]:
            problems.append("体格%d(幅%d) 左向きの X=%s（右向きは %s）。X の並びは向きで変わらない"
                            % (body, w, [c[3] for c in left], [c[3] for c in right]))
    r.check("体格の幅 %s で、左向きのタイル列が右向きの逆順になり X の並びは変わらない"
            % sorted(set(seen_widths)), not problems,
            "%s。反転ビットは1枚ごとの絵を裏返すだけで列の並びは裏返さないので、"
            "左向きでは末尾のタイルから -%d ずつ並べる必要がある（sprite.s の spr_tstep）"
            % ("／".join(problems), SPR_POSE_STRIDE))


def _check_shadow(nes, labels, ents, r):
    """影（足元に1枚）。**奥行きを画面に出す唯一の手がかり**（P1 受入の指摘5）。"""
    r.section("影（足元に敷く1枚。ADR-0002 の「余りで出す」側）")

    widths = _body_widths(nes, labels)
    widest = max(widths)
    widest_body = widths.index(widest)
    y = _depth_samples(nes, labels, 2)[1]

    # --- 有効化すると既定で影が付く（置いた側が忘れても成立する。entity.s）---
    ents.clear()
    _clear_fx(nes, labels)
    set_camera(nes, labels, 0)
    ents.place(0, x=64, y=y, cls=SPR_CLASS_PLAYER, body=widest_body, tile=4, attr=0)
    nes.call(labels["ent_activate"], x=0)
    r.check("ent_activate が影を敷く印 (ENT_ATTR_SHADOW=$%02X) を既定で立てる" % ENT_ATTR_SHADOW,
            bool(ents.peek("ent_attr", 0) & ENT_ATTR_SHADOW),
            "ent_attr=$%02X。影は奥行きを画面に出す唯一の手がかりなので、既定は「敷く」である。"
            "接地していないもの（効果・飛び道具）だけが有効化のあとで下ろす"
            % ents.peek("ent_attr", 0))

    used, _ = build_oam(nes, labels)
    cols = [shadow_entry(nes, labels, i) for i in range(used)]
    body_rows = [c for c in cols if c[1] != SPR_TILE_SHADOW]
    shadows = [(i, c) for i, c in enumerate(cols) if c[1] == SPR_TILE_SHADOW]

    r.check("影が本体のぶんに1枚だけ足される（本体 %d 列 + 影 1 枚）" % widest,
            used == widest + 1 and len(shadows) == 1,
            "使用 %d エントリ、うち影のタイル($%02X)が %d 枚（期待は本体 %d + 影 1）。"
            "影は体格の幅によらず1枚である（8ドット幅のタイル1つを足元の中央に敷く）"
            % (used, SPR_TILE_SHADOW, len(shadows), widest))

    # 影が1枚も出ていないときも、下の3件は**素通りさせずに落とす**。
    # `if shadows:` で囲むと、影が消えた日にこの3件が黙って居なくなる
    # （ラベルの門番で起きたのと同じ「静かに消える」不具合である）。
    idx, sh = shadows[0] if shadows else (-1, ("影なし", "-", "-", "影なし"))
    body_last = max([i for i, c in enumerate(cols) if c[1] != SPR_TILE_SHADOW], default=-1)
    if True:
        r.check("影が本体より必ず後ろの OAM エントリに置かれる（影がキャラを隠さない）",
                bool(shadows) and idx > body_last,
                "影が #%d（-1 = 1枚も出ていない）、本体が #%s。OAM は先頭ほど前面に描かれ、"
                "1スキャンライン8スプライト制約でも生き残る。影が本体より前に出ると、"
                "混んだ場面で**本体が消えて影だけ残る**"
                % (idx, [i for i, c in enumerate(cols) if c[1] != SPR_TILE_SHADOW]))
        r.check("影の OAM の Y が本体と同じ（接地線にそろう）",
                bool(shadows) and sh[0] == body_rows[0][0] == y - ENT_Y_TO_OAM,
                "影の Y=%s / 本体の Y=%d（足元Y %d - %d = %d）。"
                "CHR の影はタイルの下端に描いてあるので、本体と同じ Y に置けば足元に来る"
                % (sh[0], body_rows[0][0], y, ENT_Y_TO_OAM, y - ENT_Y_TO_OAM))
        want_x = 64 + (widest - 1) * SPRITE_W // 2
        r.check("影が体の横幅の中央に寄る（体格の幅 %d → X=%d）" % (widest, want_x),
                bool(shadows) and sh[3] == want_x,
                "影の X=%s（期待 %d = 本体X 64 + (幅%d - 1) * %d / 2）。"
                "体格ごとの表を増やさずに body_width から計算する約束である"
                % (sh[3], want_x, widest, SPRITE_W))

    # --- 影がキャラを押し出さないこと ---
    # 本体だけで OAM を使い切る場面を作る。影は1枚も出ないが、**本体は1列も欠けない**。
    ents.clear()
    _clear_fx(nes, labels)
    for i in range(ents.count):
        ents.place(i, x=8 * i, y=y, cls=SPR_CLASS_FAR_ENE, body=widest_body, tile=0,
                   attr=ENT_ATTR_SHADOW)
    used, dropped = build_oam(nes, labels)
    fx_dropped = nes.ram[labels["oam_fx_dropped"] & 0x7FF]
    r.check("本体で OAM を使い切る場面でも、影はキャラを1列も押し出さない"
            "（本体 %d 体 × 幅 %d = %d）" % (ents.count, widest, ents.count * widest),
            dropped == 0 and used == min(ents.count * widest, OAM_SPRITE_MAX)
            and fx_dropped == ents.count,
            "oam_dropped=%d（本体の捨て。期待 0）/ oam_used=%d / oam_fx_dropped=%d"
            "（出せなかった影。期待 %d）。影は「余りで出す」ものなので、"
            "足りなければ**影が消える**のが正しい。ここで oam_dropped が増えるなら、"
            "影が本体より先に OAM を取っている（emit の順序が壊れた）"
            % (dropped, used, fx_dropped, ents.count))
    r.check("出せなかった影の数が本体の捨てと混ざっていない (oam_fx_dropped)",
            fx_dropped > 0 and dropped == 0,
            "oam_fx_dropped=%d / oam_dropped=%d。影と効果が出せないのは仕様どおりの動作で、"
            "本体が捨てられるのは不具合である。この2つを同じカウンタで数えると、"
            "P2 の重み付き巡回（ADR-0002）を調整するときに区別が付かない" % (fx_dropped, dropped))

    ents.clear()


def _check_fx(nes, labels, ents, r):
    """短命の効果スプライト（斬り・衝撃）の枠と寿命。**後始末は engine が持つ。**"""
    r.section("効果スプライト fx（枠・寿命・消し忘れの防止）")

    fx_max = labels["fx_x_lo"] - labels["fx_life"]
    r.check("効果の枠数が constants.inc の FX_MAX (%d) と一致する" % D["FX_MAX"],
            fx_max == D["FX_MAX"],
            "ラベルの間隔から読んだ枠数は %d だが FX_MAX は %d。"
            "fx_* の配列長が揃っていないと `lda fx_y, x` が隣の列を読む"
            % (fx_max, D["FX_MAX"]))

    def life(i):
        return nes.ram[(labels["fx_life"] + i) & 0x7FF]

    def spawn(x=0x0140, y=180, tile=50, attr=3, frames=4, regx=0x11, regy=0x22):
        for name, value in (("fx_arg_x_lo", x & 0xFF), ("fx_arg_x_hi", (x >> 8) & 0xFF),
                            ("fx_arg_y", y), ("fx_arg_tile", tile), ("fx_arg_attr", attr),
                            ("fx_arg_life", frames)):
            nes.ram[labels[name] & 0x7FF] = value
        return nes.call(labels["fx_spawn"], x=regx, y=regy)

    nes.call(labels["fx_clear_all"])
    r.check("fx_clear_all で全ての枠が空く（寿命 0 = 空き）",
            all(life(i) == 0 for i in range(fx_max)),
            "fx_life=%s。シーン開始で消し残すと、前のシーンの斬りが画面に残る"
            % [life(i) for i in range(fx_max)])

    res = spawn(x=0x0140, y=180, tile=50, attr=3, frames=4)
    slots = [i for i in range(fx_max) if life(i)]
    r.check("fx_spawn が空き枠に1つ出し、キャリークリアで返る（X と Y を壊さない）",
            not res.carry and len(slots) == 1 and (res.x, res.y) == (0x11, 0x22),
            "carry=%d / 生きている枠=%s / X=$%02X Y=$%02X（期待 $11/$22）。"
            "エンティティ番号を X に持ったまま呼べることが約束である（fx.s 冒頭）"
            % (res.carry, slots, res.x, res.y))
    # 枠が1つも生きていなくても、下の1件は素通りさせずに落とす（黙って消えないため）。
    i = slots[0] if slots else 0
    if True:
        got = (nes.ram[(labels["fx_x_lo"] + i) & 0x7FF],
               nes.ram[(labels["fx_x_hi"] + i) & 0x7FF],
               nes.ram[(labels["fx_y"] + i) & 0x7FF],
               nes.ram[(labels["fx_tile"] + i) & 0x7FF],
               nes.ram[(labels["fx_attr"] + i) & 0x7FF],
               life(i))
        r.check("fx_arg_* がそのまま枠へ写る",
                bool(slots) and got == (0x40, 0x01, 180, 50, 3, 4),
                "生きている枠=%s / 枠 #%d = (x_lo,x_hi,y,tile,attr,life)=%s"
                "（期待 (64,1,180,50,3,4)）" % (slots, i, got))

    # --- 寿命どおり出て、誰も後始末しなくても自動で消える ---
    nes.call(labels["fx_clear_all"])
    spawn(frames=3)
    alive = []
    for _ in range(6):
        alive.append(sum(1 for i in range(fx_max) if life(i)))
        nes.call(labels["fx_update"])
    r.check("寿命 3 の効果が 3 フレーム出て、4 フレーム目に自動で消える",
            alive == [1, 1, 1, 0, 0, 0],
            "各フレームの生存数=%s（期待 [1,1,1,0,0,0]）。fx_update は寿命を1つ減らし、"
            "尽きた枠を空きに戻す。**呼び出し側に後始末をさせない**のが約束であり、"
            "消し忘れは「画面に判定の残骸が残る」という最も気付きにくい不具合になる" % alive)

    nes.call(labels["fx_clear_all"])
    spawn(frames=0)
    r.check("寿命 0 で呼ばれても 1 フレームは出る（入れ忘れが黙って消えない）",
            sum(1 for i in range(fx_max) if life(i)) == 1,
            "生きている枠=%d（期待 1）。黙って何も出ないと、呼び出し側からは"
            "「枠が無かった」のか「寿命を入れ忘れた」のか区別がつかない"
            % sum(1 for i in range(fx_max) if life(i)))

    # --- 枠が尽きたら C=1 で断る。既に出ているものを壊さない ---
    nes.call(labels["fx_clear_all"])
    results = [spawn(frames=9, tile=50 + 2 * (k % 3)) for k in range(fx_max)]
    before = [life(i) for i in range(fx_max)]
    over = spawn(frames=9)
    r.check("FX_MAX(%d) を超える fx_spawn がキャリーセットで断り、出ているものを壊さない" % fx_max,
            all(not x.carry for x in results) and over.carry
            and [life(i) for i in range(fx_max)] == before,
            "最初の %d 回の carry=%s / %d 回目の carry=%d / 枠の寿命 %s → %s。"
            "枠が空いていなくても呼び出し側は何もしなくてよい（効果は消えてよいもの。"
            "ADR-0002 の SPR_CLASS_EFFECT）が、**既に出ている効果を上書きしてはならない**"
            % (fx_max, [x.carry for x in results], fx_max + 1, over.carry,
               before, [life(i) for i in range(fx_max)]))

    # --- 生きている効果が OAM に出て、死んだら出ない ---
    nes.call(labels["fx_clear_all"])
    y = _depth_samples(nes, labels, 2)[1]
    ents.clear()
    set_camera(nes, labels, 0)
    ents.place(0, x=32, y=y, cls=SPR_CLASS_PLAYER, body=0, tile=4, attr=0)
    base, _ = build_oam(nes, labels)
    spawn(x=96, y=y, tile=50, attr=3, frames=1)
    with_fx, _ = build_oam(nes, labels)
    tiles = [shadow_entry(nes, labels, i)[1] for i in range(with_fx)]
    nes.call(labels["fx_update"])                    # 寿命が尽きる
    after, _ = build_oam(nes, labels)
    r.check("生きている効果が OAM に1枚増え、寿命が尽きると消える",
            with_fx == base + 1 and 50 in tiles and after == base,
            "効果なし %d エントリ → 効果あり %d エントリ（タイル=%s）→ 寿命切れ後 %d。"
            "効果が消えないと、攻撃が終わっても斬りの絵が画面に残り続ける"
            % (base, with_fx, tiles, after))
    nes.call(labels["fx_clear_all"])
    ents.clear()


def _check_pose(nes, labels, ents, r):
    """姿勢の差し替え（ent_set_pose）。**何の絵を出すかを決めるのは action / ai** で、
    engine が持つのは役割の先頭タイル（ent_tile0）と、このずらし1本だけである。"""
    r.section("姿勢 (ent_set_pose = ent_tile0 + 姿勢 * %d)" % SPR_POSE_STRIDE)

    slot = ents.count - 1
    base = 32
    ents.place(slot, x=0, y=_depth_samples(nes, labels, 2)[1], body=0, tile=base, attr=0)
    nes.call(labels["ent_activate"], x=slot)
    r.check("ent_activate が置いたタイルを役割の先頭タイル (ent_tile0) として覚える",
            ents.peek("ent_tile0", slot) == base,
            "ent_tile0=%d（期待 %d）。覚えていないと、姿勢を1回切り替えた後に"
            "元の絵へ戻れない（姿勢のずらしが累積する）"
            % (ents.peek("ent_tile0", slot), base))

    wrong = []
    for pose in range(SPR_POSE_COUNT):
        nes.call(labels["ent_set_pose"], x=slot, a=pose)
        want = base + pose * SPR_POSE_STRIDE
        if ents.peek("ent_tile", slot) != want:
            wrong.append("姿勢 %d → ent_tile=%d（期待 %d）"
                         % (pose, ents.peek("ent_tile", slot), want))
    r.check("ent_set_pose が %d 姿勢すべてで ent_tile0 + 姿勢 * %d を書く"
            % (SPR_POSE_COUNT, SPR_POSE_STRIDE), not wrong,
            "%s。8x16 スプライトは1体が上下2タイルを使うので、姿勢の間隔は %d である。"
            "ここがずれると、攻撃の絵のつもりで別の役割のタイルを出す"
            % ("／".join(wrong), SPR_POSE_STRIDE))

    # 役割の先頭タイルが変わっても呼び出し側は何も直さなくてよい、という約束。
    ents.poke("ent_tile0", slot, 16)
    nes.call(labels["ent_set_pose"], x=slot, a=1)
    r.check("役割の先頭タイルを変えると、同じ姿勢の指定でも出る絵がその役割のものになる",
            ents.peek("ent_tile", slot) == 16 + SPR_POSE_STRIDE,
            "ent_tile0=16 で姿勢1を指定したら ent_tile=%d（期待 %d）。"
            "姿勢の語彙（SPR_POSE_*）は役割に依存しない、が約束である"
            % (ents.peek("ent_tile", slot), 16 + SPR_POSE_STRIDE))
    ents.clear()
