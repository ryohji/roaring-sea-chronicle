"""ラベルの門番。

なぜ要るか
----------
以前の作りは、層（l2_engine / l2_ai）の先頭で **その層が使うラベルを全部**まとめて
検査し、1つでも欠けたら層ごと `return` していた。ADR-0009 でレーンが廃止され
`ent_lane*` / `lane_ground_y` / `lane_distance` / `ai_p_lane_hold` が消えた結果、

    FAIL  engine のラベルが build/roaring.labels に揃っている   （44件が未実行）
    FAIL  ai のラベルが build/roaring.labels に揃っている       （20件が未実行）

となり、**1つの名前替えで60件以上が黙って消えた**。成功数は 136 → 116 に減ったが、
「何が検証されなくなったのか」は失敗メッセージのどこにも出ない。
落ちた2件を直しても、次に誰かが名前を変えた日に同じことが起きる。

作り直した形
------------
検証を**節**に分け、節ごとに「この節が使うラベル」を宣言する。

  * 欠けたラベルを使う**節だけ**が、その節の名前で落ちる。残りの節は走る。
  * 飛ばした節は**その節の名前で1件落ちる**ので、最後の「失敗したテスト」の一覧が
    そのまま「いま誰も見張っていない主張」の一覧になる。
  * 足場（起動・エンティティ表の読み書き）に要るラベルが欠けたときだけ層全体を諦める。
    足場に挙げるラベルは必要最小限にすること。ここに書いたラベルが1つ欠けると
    層ごと飛ぶので、「その節だけが落ちる」形が効かなくなる。
  * 節の中の1件だけがラベルを要るときは `Gate.need()` で1件だけ落とせる。

この形なら、ラベルが1つ消えたときに失われるのは**それを使う検証だけ**であり、
失敗メッセージがそのまま「何が見張られなくなったか」の一覧になる。
"""


class Gate:
    """labels に対する問い合わせと、欠けたときの落とし方をまとめたもの。"""

    def __init__(self, labels, r, layer):
        self.labels = labels or {}
        self.r = r
        self.layer = layer          # 報告に出す層の名前（"engine" など）

    # --- 問い合わせ ---
    def missing(self, names):
        return [n for n in names if n not in self.labels]

    # --- 落とし方 ---
    def need(self, check_name, names, why=""):
        """1件の検証が要るラベルを宣言する。揃っていれば True。

        欠けていれば**その検証の名前で**1件落とし、False を返す。
        呼び出し側は素通りさせること（層ごと return しない）。
        """
        gone = self.missing(names)
        if not gone:
            return True
        self.r.check(check_name, False,
                     "この検証が使うラベルが build/roaring.labels に無い: %s。"
                     "%s.export が消えたか、名前が変わったか、モジュールがリンクから"
                     "外れている。**落としたのはこの検証だけで、他の節は走っている**"
                     % (", ".join(gone), why + "。" if why else ""))
        return False


def run_sections(gate, sections, invoke):
    """節を順に走らせる。ラベルが欠けた節だけを落とす。

    sections: [(節の名前, 要るラベルの並び, 関数)]
    invoke:   関数を実際に呼ぶ手続き（層ごとに引数が違うので外から渡す）

    戻り値: 飛ばした節の数。
    """
    from cpu6502 import CpuCrash          # noqa: PLC0415 — harness を先に sys.path へ入れる都合

    skipped = 0
    for name, needs, fn in sections:
        gone = gate.missing(needs)
        if gone:
            skipped += 1
            gate.r.check("%s の検証が走る" % name, False,
                         "ラベルが無いので**この節だけ**飛ばした: %s。"
                         "この節が見ていた主張は、いま誰も見張っていない。"
                         "ラベルを直すか、その主張が不要になったのなら検証ごと消すこと"
                         % ", ".join(gone))
            continue
        try:
            fn(*invoke(fn))
        except CpuCrash as e:
            gate.r.check("%s の検証中にクラッシュしない" % name, False, str(e))
        except Exception as e:      # noqa: BLE001 — 検証が例外で死ぬと原因が読めなくなる
            gate.r.check("%s の検証が最後まで走る" % name, False,
                         "検証コードが %s で止まった: %s（%s:%d）。"
                         "値が想定の範囲を外れている疑いがある"
                         % (type(e).__name__, e, *_where(e)))
    return skipped


def _where(exc):
    """例外が起きたテスト側の位置（ファイル名, 行）。原因の当たりを付けるため。"""
    tb = getattr(exc, "__traceback__", None)
    last = ("?", 0)
    while tb is not None:
        last = (tb.tb_frame.f_code.co_filename.split("/")[-1], tb.tb_lineno)
        tb = tb.tb_next
    return last


def boot_or_fail(gate, needs, what):
    """足場（起動とエンティティ表）に要るラベルを検査する。

    ここが欠けたときだけ層全体を諦める。**諦めたことを1件の失敗として出す**
    （黙って消えない）。足場に挙げるラベルは必要最小限にすること。
    """
    gone = gate.missing(needs)
    if not gone:
        return True
    gate.r.check("%s の足場になるラベルが揃っている" % gate.layer, False,
                 "ラベルが無い: %s。%s。"
                 "これは個別の検証ではなく**シーンを組み立てる足場**が使うラベルなので、"
                 "この層は1件も走らせられない。上の節ごとの落とし方（gate.run_sections）と違い、"
                 "ここだけは層ごと飛ばす" % (", ".join(gone), what))
    return False
