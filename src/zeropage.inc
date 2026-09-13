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
;   NMI がカメラの公開コピー (cam_pub_hi) から毎フレーム計算して ora で載せる。
.globalzp ppu_ctrl_shadow, ppu_mask_shadow

; --- カメラ（横スクロールの原点）---
; 画面X = ワールドX - カメラX。操作キャラを不感帯つきで追う（src/engine/scroll.s）。
; **これを書いてよいのはメインループだけ。NMI はここを読んではならない**（下記）。
.globalzp cam_x_lo, cam_x_hi
; NMI に見せるカメラの公開コピー（二面）。
; cam_x は 16bit なので、メインは lo と hi を別々の命令で書く。その2命令の間に
; NMI が入ると「新しい lo と古い hi」の組が画面に出る。cam_x_hi はネームテーブル
; 選択ビットなので、**256ドット境界をまたぐ瞬間だけ画面が丸1画面ぶん飛ぶ**。
; sei では NMI は止まらないので、割り込み禁止では防げない。
; そこで二面を持ち、NMI が読んでいない側に組み立ててから、添字 cam_pub_sel を
; **1命令で**差し替える（src/engine/scroll.s の cam_publish）。
; 6502 の命令は NMI に中断されないので、NMI が見る添字は必ず切り替えの前か後であり、
; どちらの面も「完全に書き終えた組」である。
;   書くのは cam_publish だけ / 読むのは NMI だけ。
.globalzp cam_pub_lo, cam_pub_hi   ; 面0 / 面1 の 2 バイト配列
.globalzp cam_pub_sel              ; NMI が読む面の添字 (0 or 1)
; カメラの左端が今どのタイル列にあるか（= cam_x >> 3）。
; 新しい列が見えるようになったことは、この値の変化で検出する。
.globalzp cam_col_lo, cam_col_hi

; --- VRAM 転送キュー（src/engine/vram.s）---
; メインループが積み、NMI が予算のぶんだけ刻んで転送する。
; 書き手が分かれているので、どちらか片方しか書かない変数に分けてある。
.globalzp vq_head            ; NMI が次に読む位置（進めるのは NMI。メインは
                             ; vram_queue_reset で空にするときだけ書く。理由は vram.s）
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
