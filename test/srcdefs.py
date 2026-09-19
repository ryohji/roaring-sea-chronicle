"""src/*.inc の定数を**読み出す**ための足場（テスト側に値を焼き付けないため）。

CLAUDE.md 第4節は「定数・構造体オフセットは src/constants.inc に集約し、
マジックナンバーを書かない」と定めている。テスト側も同じである。
SPRITE_H や ACT_DEPTH_SPEED をテストに数値で書くと、**主がノブを回した日に
仕様が壊れていないのにテストが落ちる**。

値の出どころの優先順位は次のとおり。
  1. build/roaring.labels と ROM の表（実際にリンクされた結果。いちばん強い）
  2. data/*.tsv（正本があるもの）
  3. src/*.inc の定義をここで読む（1 と 2 では取れないアセンブル時定数だけ）

ここが担うのは 3 である。`.res` も `.byte` も伴わない純粋な定数（SPRITE_H、
ACT_DEPTH_SPEED、ACT_BODY_ROWS_* など）は ROM にもラベルにも現れない。
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# `NAME = 値` の行だけを拾う。値は 10進 / $16進 / %2進 と、それらの単純な四則。
_DEF = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^;]+?)\s*(?:;.*)?$")
_TOKEN = re.compile(r"\$[0-9A-Fa-f]+|%[01]+|\d+|[A-Za-z_][A-Za-z0-9_]*|<<|>>|[-+*/()|&^~]")


class SourceDefs:
    """1つ以上の .inc から `NAME = 値` を読み、名前で引けるようにする。"""

    def __init__(self, paths):
        self.raw = {}
        self.files = []
        for path in paths:
            full = path if os.path.isabs(path) else os.path.join(ROOT, path)
            if not os.path.exists(full):
                continue
            self.files.append(full)
            with open(full, encoding="utf-8") as f:
                for line in f:
                    if line.lstrip().startswith((".", ";")):
                        continue
                    m = _DEF.match(line)
                    if m and m.group(1) not in self.raw:
                        self.raw[m.group(1)] = m.group(2)
        self.cache = {}

    def __contains__(self, name):
        return name in self.raw

    def get(self, name, default=None):
        try:
            return self[name]
        except KeyError:
            return default

    def __getitem__(self, name):
        if name in self.cache:
            return self.cache[name]
        if name not in self.raw:
            raise KeyError("定数 %s が %s のどこにも無い（名前が変わった？）"
                           % (name, [os.path.basename(p) for p in self.files]))
        self.cache[name] = None                  # 循環参照の番人
        value = self._eval(self.raw[name], name)
        self.cache[name] = value
        return value

    def _eval(self, expr, name):
        """ca65 の定数式を Python の式に直して評価する。整数除算であることに注意。

        ca65 の `/` は整数除算である（ACT_DEPTH_TOL_* がこれに依存している）。
        Python の `/` は浮動小数なので `//` に置き換える。
        """
        out = []
        for tok in _TOKEN.findall(expr):
            if tok.startswith("$"):
                out.append(str(int(tok[1:], 16)))
            elif tok.startswith("%"):
                out.append(str(int(tok[1:], 2)))
            elif tok == "/":
                out.append("//")
            elif tok[0].isdigit():
                out.append(tok)
            elif tok in ("+", "-", "*", "(", ")", "|", "&", "^", "~", "<<", ">>"):
                out.append(tok)
            else:
                out.append(str(self[tok]))       # 他の定数への参照
        try:
            return int(eval(" ".join(out), {"__builtins__": {}}, {}))   # noqa: S307
        except Exception as e:      # noqa: BLE001
            raise KeyError("定数 %s = %r を評価できない: %s" % (name, expr, e))


_CACHE = {}


def defs(*paths):
    """よく使う組み合わせを1回だけ読む。"""
    key = tuple(paths)
    if key not in _CACHE:
        _CACHE[key] = SourceDefs(paths)
    return _CACHE[key]


def action_defs():
    """action の調整値。constants.inc を後ろに置いて SPRITE_H などを解決させる。"""
    return defs("src/action/action_params.inc", "src/action/action.inc", "src/constants.inc")


def body_rows(d=None):
    """体格型（BODY_*）ごとの「絵の段数」を constants.inc の並び順で返す。

    ACT_BODY_ROWS_* の名前は BODY_* の名前と1対1である（action_params.inc）。
    ここで名前の対応を作っておくと、体格型が増えたときに直すのは
    constants.inc と action_params.inc だけで済む。
    """
    d = d or action_defs()
    names = []
    for key in d.raw:
        if key.startswith("BODY_") and key not in ("BODY_TYPE_COUNT",):
            names.append(key)
    names.sort(key=lambda n: d[n])
    return [(n, d["ACT_BODY_ROWS_" + n[len("BODY_"):]]) for n in names]
