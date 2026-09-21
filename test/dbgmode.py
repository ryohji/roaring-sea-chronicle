"""手触り確認のためのデバッグ機構（src/debug.inc）をテストから回すための足場。

**モードは変数に直接書かない。実際に START / SELECT を押して回す。**
dbg_slow / dbg_ally に値を書き込んでしまうと、ボタンの結線（src/main.s の
dbg_buttons）が切れた日にテストが素通りする。ここで押すのは、モードの中身だけでなく
**「ボタンでそのモードに入れること」ごと**見張るためである。

押下は数フレーム保つこと。dbg_buttons は read_pad の直後、つまり**論理フレームでしか**
呼ばれない（src/main.s）。1フレームだけ押して離すと、dbg_slow = 2（1/3速）のときは
飛ばされたフレームに当たって取りこぼされる（engine-dev の申し送り）。
押した瞬間（pad_pressed）でしか回らないので、押しっぱなしでも二重には回らない。

DBG_ENABLE = 0（src/debug.inc）のとき
--------------------------------------
デバッグ機構は**コードも変数も丸ごと消える**。dbg_slow / dbg_ally のラベルも
build/roaring.labels から消える。そのとき確認モードを使う節は
**落とすのではなく飛ばす**のが正しい（機構が無いのは不具合ではないから）。

ただし**黙って消えてはならない**。飛ばしたことは SKIP として出力に出し、
最後の要約にも件数と一覧が出る（gate.skip_sections / run_tests.Results.skip）。

逆に DBG_ENABLE が 1 のときにラベルが欠けていたら、それは本物の不具合である
（.export が消えた／名前が変わった）。そちらは節ごと**落とす**
（gate.run_sections の既定の振る舞い）。この2つを取り違えないこと。
"""
from srcdefs import defs

# src/debug.inc の DBG_ENABLE / DBG_MODE_COUNT。値はテストに焼き付けない。
DBG = defs("src/debug.inc")

# 押下を保つフレーム数。dbg_slow の最大（DBG_MODE_COUNT-1 = 2 → 3フレームに1回しか
# 論理更新が来ない）でも必ず1回は論理フレームに当たる長さにする。
PRESS_HOLD = 4
# 離しておくフレーム数。次の「押した瞬間」を作るために、間に離したフレームの
# 論理更新が1回は要る。
PRESS_REST = 4

SKIP_WHY = ("src/debug.inc の DBG_ENABLE = 0 なので、確認モード（START / SELECT）は "
            "**コードごと消えている**。dbg_slow / dbg_ally のラベルも無い。"
            "機構が無いのは不具合ではないのでこの節は飛ばした。"
            "手触りを見るときは DBG_ENABLE = 1 に戻すこと（そのときこの節が走る）")


def enabled():
    """デバッグ機構がビルドに載っているか（src/debug.inc の DBG_ENABLE）。"""
    return bool(DBG.get("DBG_ENABLE", 0))


def mode_count():
    """モードの巡回の段数（0 から mode_count()-1 まで回って 0 に戻る）。"""
    return DBG["DBG_MODE_COUNT"]


def press(scene, button, hold=PRESS_HOLD, rest=PRESS_REST):
    """button を hold フレーム押し、rest フレーム離す。

    scene は .nes / .labels / .get を持つもの（l2_action.Actors, l2_ai.Scene）。
    観測は各フレームの更新が終わった点で揃える（test/harness/nes.py 冒頭の作法）。
    """
    from nes import step_frame          # noqa: PLC0415 — harness を先に sys.path へ入れる都合
    scene.nes.set_buttons({button})
    for _ in range(hold):
        step_frame(scene.nes, scene.labels, 1)
    scene.nes.set_buttons(set())
    for _ in range(rest):
        step_frame(scene.nes, scene.labels, 1)


def cycle(scene, button, var, times):
    """button を times 回押し、**押すたびの var の値**を並べて返す。

    戻り値には押す前の値は含まない。結線が切れていれば同じ値が並ぶ。
    """
    seen = []
    for _ in range(times):
        press(scene, button)
        seen.append(scene.get(var))
    return seen


def to_mode(scene, button, var, want):
    """var が want になるまで button を押す。戻り値は**実際に読めた値**。

    want にならないまま巡回しきったら、そのまま（want と違う値を）返す。
    呼び出し側は必ず戻り値を検査すること。ここで例外を投げないのは、
    「ボタンを押してもモードが変わらない」を**その節の失敗として**
    名前つきで報告させたいからである。
    """
    for _ in range(mode_count()):
        if scene.get(var) == want:
            return want
        press(scene, button)
    return scene.get(var)
