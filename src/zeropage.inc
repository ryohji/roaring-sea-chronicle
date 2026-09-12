; zeropage.inc — ゼロページ変数の一元宣言。
; 各モジュールで勝手にゼロページを確保してはならない（CLAUDE.md 第4節）。
.ifndef ZEROPAGE_INC
ZEROPAGE_INC = 1

.globalzp frame_counter      ; フレームカウンタ（NMI で加算）
.globalzp nmi_done           ; NMI が完了したことを示すフラグ（メインループが消費）
.globalzp pad_state          ; 今フレームのパッド状態
.globalzp pad_pressed        ; 今フレームで押された瞬間のビット
.globalzp ptr_lo, ptr_hi     ; 汎用ポインタ（呼び出し先で保存されない）
.globalzp tmp0, tmp1, tmp2, tmp3

.endif
