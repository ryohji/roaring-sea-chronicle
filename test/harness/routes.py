"""入力列（経路）ファイルの読み書き。

L2（Python エミュレータ）と L3（Mesen2 Lua）の両方から同じ形式を読む（ADR-0004）。

書式: 1行 = 「フレーム数 ボタン...」。`#` 以降はコメント。
    60  RIGHT          ; 右を60フレーム押す
    1   A              ; A を1フレーム
    30                 ; 30フレーム何もしない

ボタン名: A B SELECT START UP DOWN LEFT RIGHT
"""
VALID = {"A", "B", "SELECT", "START", "UP", "DOWN", "LEFT", "RIGHT"}


class RouteError(Exception):
    pass


def parse_route(path):
    """[(フレーム数, ボタン名の集合), ...] を返す。"""
    steps = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            try:
                frames = int(parts[0])
            except ValueError:
                raise RouteError("%s:%d: 先頭はフレーム数でなければならない: %r"
                                 % (path, lineno, line))
            buttons = set(p.upper() for p in parts[1:])
            unknown = buttons - VALID
            if unknown:
                raise RouteError("%s:%d: 未知のボタン名 %s（有効: %s）"
                                 % (path, lineno, sorted(unknown), " ".join(sorted(VALID))))
            steps.append((frames, buttons))
    if not steps:
        raise RouteError("%s: 経路が空" % path)
    return steps


def play(nes, steps):
    """経路を再生する。総フレーム数を返す。"""
    total = 0
    for frames, buttons in steps:
        nes.set_buttons(buttons)
        nes.run_frames(frames)
        total += frames
    nes.set_buttons(set())
    return total
