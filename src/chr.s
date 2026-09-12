; chr.s — CHR-ROM。PNG を正とし、ビルド時に tools/png2chr.py が変換したものを取り込む。
; 画像を直接いじらず、必ず chr/*.png を編集すること（リポジトリで diff が見えるように）。
.segment "CHR"

.incbin "sprites.chr"
