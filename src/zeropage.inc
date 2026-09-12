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

; --- カメラ（横スクロールの原点）---
; 画面X = ワールドX - カメラX。横スクロールは次の作業だが、OAM 構築が
; 画面座標を経由する形を最初から取っておかないと、後で全経路を書き換えることになる。
; P1 では 0 に固定（RAM クリアで 0 になる）。
.globalzp cam_x_lo, cam_x_hi

; --- OAM 構築の作業変数（src/engine/sprite.s）---
; 毎フレーム 64 エントリぶん回る内側ループで使うのでゼロページに置く。
.globalzp oam_next           ; 次に書き込む OAM エントリ番号 (0..OAM_SPRITE_MAX)
.globalzp sort_count         ; 並べ替え結果の要素数
.globalzp spr_x_lo, spr_x_hi ; 展開中スプライトの画面X（16bit。範囲外の切り捨て判定に使う）
.globalzp spr_y              ; 展開中スプライトの OAM Y バイト
.globalzp spr_tile           ; 展開中スプライトのタイル番号
.globalzp spr_attr           ; 展開中スプライトの属性バイト
.globalzp spr_cols           ; 残り列数

; --- 矩形交差の引数（src/engine/collide.s）---
; オフセットは constants.inc の RECT_*。呼び出し側がここに詰めて rect_overlap を呼ぶ。
.globalzp rect_a, rect_b

.endif
