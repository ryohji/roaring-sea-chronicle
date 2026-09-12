; chr_bg.s — 背景パターンテーブル（$1000 側）の CHR。
;
; PNG が正である（chr/bg.png）。ビルド時に tools/png2chr.py が変換したものを取り込む。
; スプライト側（src/chr.s の sprites.chr）の直後に置かれることで $1000 に載る。
;
; 「直後に置かれる」はリンク順に依存する暗黙の前提なので、下の .assert で縛ってある。
; ここが崩れると、背景が全部スプライトの絵で描かれるという分かりにくい壊れ方をする。
;
; 中身は確認用の仮タイル（空白・壁・柱・床の縁・床A/B）である。
; 本番の背景タイルは P3 で stage-author が差し替える。
.include "constants.inc"

.segment "CHR"

bg_chr_base:
.assert bg_chr_base = $1000, error, "背景 CHR が $1000 に載っていない（CHR セグメントのリンク順を見よ）"

.incbin "bg.chr"
