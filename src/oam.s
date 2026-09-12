; oam.s — OAM シャドウバッファ。NMI 中に $4014 で DMA 転送される。
; ページ境界に整列していなければならない（リンカスクリプトの align = $100 が保証する）。
.export oam_shadow

.segment "OAM"
oam_shadow: .res 256
