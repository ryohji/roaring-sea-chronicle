"""シーンから「いま検証したい対象以外」を取り除くための足場。

なぜ要るか
----------
P1 の動作確認シーン（src/main.s の scene_test_init）には操作キャラ・自律仲間1体・
敵3体が同居している。ai-dev が敵に思考を入れて以降、**敵は実際に殴ってくる**。
その結果、engine の「レーン補間とクランプ」「入力が無ければ動かないこと」を見る
検証が、のけぞり（入力が通らない）とノックバック（入力が無くても動く）で落ちた。

このとき期待値を緩めて「戦闘下でも通る」ようにするのは**テストを緩めること**である。
戦闘下の挙動は別の主張（のけぞり中は入力を受け付けない／ノックバックで動く）であり、
レーン補間の主張とは無関係だからである。正しい直し方は
**検証したい対象を邪魔するものを取り除く**ことで、それをここに置く。

取り除くのは「敵の攻撃」だけにしてある（仲間は残す）。仲間を残すのは、
自律仲間が操作キャラの座標やレーンに手を出していないことが、
残った engine の検証でそのまま見張られる状態にしておきたいからである。
"""

# action / ai 側の付随テーブルのうち、「殴られた痕跡」として残る作業変数。
# ここに無いものは残す（HP は残す。回復させてしまうと、後続の検証が
# 「敵に殴られなかった世界」を見ることになる）。
_PLAYER_COMBAT_WORK = ("act_hitstop", "act_kb", "act_invuln", "act_timer",
                       "act_step", "act_flags", "act_sub")

# 入力ゲートの状態（猶予に入ると押しっぱなしのビットが無効化されたまま残る。ADR-0006）
_PLAYER_INPUT_WORK = ("act_pad_ignore", "act_buf_atk")


def entity_count(labels):
    """MAX_ENTITIES を SoA の配列間隔から導出する（テスト側に焼き付けない）。"""
    return labels["ent_x_lo"] - labels["ent_active"]


def slot_ranges(labels):
    """区画の境界を labels から導出する。

    ent_* の配列長 = MAX_ENTITIES は取れるが、区画の切れ目（ENT_ALLY_FIRST など）は
    ラベルに出ないので、constants.inc と同じ値をここで名前付きで持つ。
    ここが変わったら AI の走査範囲も変わるので、変えたときはこの1行が唯一の直し所になる。
    """
    n = entity_count(labels)
    return {"player": 0, "ally_first": 1, "enemy_first": 3, "free_first": 9, "count": n}


def _poke(nes, labels, name, i, value):
    nes.ram[(labels[name] + i) & 0x7FF] = value & 0xFF


def _peek(nes, labels, name, i=0):
    return nes.ram[(labels[name] + i) & 0x7FF]


def disarm_enemies(nes, labels):
    """敵スロットを眠らせ、操作キャラに残っている被弾の痕跡を消す。

    戻り値は元に戻す関数。呼び出し側は、戦闘のある状態で見たい検証に入る前に
    必ず呼び戻すこと（敵を消したままにすると、その後の検証が
    「敵が居ない世界」を見ていることに誰も気付けなくなる）。
    """
    slots = slot_ranges(labels)
    saved = [(i, _peek(nes, labels, "ent_active", i))
             for i in range(slots["enemy_first"], slots["count"])]
    for i, _ in saved:
        _poke(nes, labels, "ent_active", i, 0)

    # 既に飛んできている一撃（のけぞり・ヒットストップ・ノックバック）も取り除く。
    # 敵を消すだけでは、消した瞬間に乗っていたノックバック速度が残り続ける。
    _poke(nes, labels, "ent_state", slots["player"], 0)          # ACT_ST_IDLE
    for name in _PLAYER_COMBAT_WORK:
        if name in labels:
            _poke(nes, labels, name, slots["player"], 0)
    for name in _PLAYER_INPUT_WORK:
        if name in labels:
            _poke(nes, labels, name, 0, 0)

    def restore():
        for i, value in saved:
            _poke(nes, labels, "ent_active", i, value)
    return restore
