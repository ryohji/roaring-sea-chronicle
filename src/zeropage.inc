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

; --- PPU レジスタのシャドウ ---
; PPUCTRL / PPUMASK は書き込み専用なので、現在値を RAM で覚えておく。
; NMI はここを読んで書き戻す（固定値を直書きしない）。ステータスバー分割の
; MMC3 スキャンライン IRQ を入れると IRQ 側もこれらを触るため、
; 「最後に誰が何を書いたか」が一箇所で分かる形にしておかないと競合する。
;   ppu_ctrl_shadow のネームテーブル選択ビット (bit0) は常に 0 にしておくこと。
;   NMI が cam_x から毎フレーム計算して ora で載せる。
.globalzp ppu_ctrl_shadow, ppu_mask_shadow

; --- カメラ（横スクロールの原点）---
; 画面X = ワールドX - カメラX。操作キャラを不感帯つきで追う（src/engine/scroll.s）。
.globalzp cam_x_lo, cam_x_hi
; カメラの左端が今どのタイル列にあるか（= cam_x >> 3）。
; 新しい列が見えるようになったことは、この値の変化で検出する。
.globalzp cam_col_lo, cam_col_hi

; --- VRAM 転送キュー（src/engine/vram.s）---
; メインループが積み、NMI が予算のぶんだけ刻んで転送する。
; 書き手が分かれているので、どちらか片方しか書かない変数に分けてある。
.globalzp vq_head            ; NMI が次に読む位置（NMI だけが書く）
.globalzp vq_tail            ; 確定した記録の末尾（メインだけが書く。ここが公開の境界）
.globalzp vq_wr              ; 組み立て中の書き込み位置（メインだけが使う）
; 以下は **NMI 専用**の作業変数。メインループから触ってはならない。
; （メイン側で使うと、転送の途中で NMI に割り込まれて値が壊れる）
.globalzp vq_n               ; 転送中の記録の残りバイト数
.globalzp vq_cost            ; このフレームの残り予算
.globalzp vq_recs            ; このフレームの残り記録数
.globalzp vq_addr_lo, vq_addr_hi   ; 転送中の VRAM アドレス（STEP 形式で進める）
.globalzp vq_flags           ; 転送中の記録のフラグ
.globalzp vq_step            ; STEP 形式の増分

; --- OAM 構築の作業変数（src/engine/sprite.s）---
; 毎フレーム 64 エントリぶん回る内側ループで使うのでゼロページに置く。
.globalzp oam_ready          ; OAM シャドウが完成している (1) / 構築中 (0)。NMI の DMA の可否
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
