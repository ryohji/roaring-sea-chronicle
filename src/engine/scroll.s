; scroll.s — 横スクロールのカメラ。
;
; カメラは「ワールドのどこを画面の左端にするか」を持つだけの器である（cam_x_lo/hi）。
; 画面X = ワールドX - カメラX という約束は OAM 構築（sprite.s）が既に守っている。
;
; 追従は硬くしない。画面中央に不感帯（デッドゾーン）を置き、操作キャラがその外に
; 出たぶんだけカメラを動かす。張り付かせると、立ち止まりや小刻みな踏み込みのたびに
; 背景全体が揺れて酔う。不感帯の幅は constants.inc の CAM_DEADZONE_HALF で調整する。
;
; ステージの右端は cam_limit（カメラX の上限）で止める。**ステージ長をコードに埋めない。**
; いまは scroll_init が暫定値を入れているが、P3 で stage-author が
; cam_set_stage_width をレイアウトのデータから呼べばそれで済む形にしてある。
.include "constants.inc"
.include "zeropage.inc"

.export scroll_init, cam_update, cam_set_stage_width, cam_publish, scroll_warp_to
.export cam_limit_lo, cam_limit_hi, stage_w_lo, stage_w_hi
.export scroll_warp_live

.import ent_x_lo, ent_x_hi
.import bg_col_lo, bg_col_hi, bg_queue_column, bg_queue_attr
.import bg_fill_window
.import vram_queue_reset

; P1 の暫定ステージ長。**この値に意味は無い**。
; 横スクロールが端で止まることを画面で確かめられるだけの長さ（4画面ぶん）を置いてある。
; 実際の長さは P3 で stage-author が data/layouts/ から cam_set_stage_width に渡す。
SCROLL_TEST_STAGE_W = SCREEN_W * 4

.segment "BSS"

cam_limit_lo: .res 1         ; カメラX の上限（= ステージ幅 - 画面幅）
cam_limit_hi: .res 1
stage_w_lo:   .res 1         ; ステージの横幅（ドット）。移動の左右限界としても使う
stage_w_hi:   .res 1

; scroll_warp_to を**描画有効のまま**呼ばれた回数（下記）。0 でなければ呼び出し側の手順違反。
scroll_warp_live: .res 1

.segment "CODE"

; 起動時に1回呼ぶ。カメラを原点に置き、そこから見える背景を書く。
; **描画無効中に呼ぶこと**（scroll_warp_to が $2007 を予算無視で叩く）。
; 中身は「カメラ位置 0 への飛び込み」でしかない。列0固定の初期化を別に持たない。
.proc scroll_init
        lda #<SCROLL_TEST_STAGE_W
        ldx #>SCROLL_TEST_STAGE_W
        jsr cam_set_stage_width          ; 上限を先に決める（warp がここでクランプする）

        lda #0
        ldx #0
        jmp scroll_warp_to
.endproc

; ---------------------------------------------------------------- 飛び込み
; カメラを**任意の位置へ飛ばし、そこから見える背景を丸ごと張り直す**。
;   入力: A = 新しい cam_x 下位, X = 同 上位（ステージの上限でクランプする）
;
; 1フレームに転送できるのは1列ぶんなので、カメラが大きく飛んだときに
; 通常の列追跡（scroll_track_columns）で追いつかせると、96列飛べば 96 フレーム＝
; 1.6 秒のあいだ誤った背景が出続ける。飛ばすなら背景は一括で書き直すしかない。
;
; 想定する使い手:
;   * ADR-0001 のオートセーブ復帰（ステージ途中の cam_x から再開） … P4 campaign-dev
;   * シーン転換・大きなノックバック                              … P3 以降
; **セーブ側の処理は engine の担当ではない。ここは入口を用意するだけである。**
;
; 呼び出し側の手順（**描画無効中に呼ぶこと**。$2007 を予算無視で叩く）:
;   1. 描画を止める。ppu_mask_shadow に 0 を入れてから PPUMASK に書くこと。
;      NMI は sei では止まらないので、シャドウを直さずに PPUMASK だけ 0 にしても
;      次の NMI がシャドウを読んで描画を戻してしまう。
;   2. scroll_warp_to を呼ぶ（転送キューは中で空にする。積んであった古い列の記録は
;      張り直した絵を上書きしてしまうので捨てるのが正しい）。
;   3. 呼んだあとに復元するもの:
;        - oam_build を呼び直す（画面X = ワールドX - カメラX が全部変わっている）
;        - ppu_mask_shadow を元に戻す
;        - bit PPUSTATUS のあと PPUCTRL / PPUSCROLL / PPUMASK をシャドウから置き直す
;          （$2006 を叩いたので PPU の内部アドレスが壊れている。置き直さないと
;           最初の1フレームが流れて見える）
;   なお 1 と 3 は VBlank の中で行うこと。描画期間中に PPUMASK を切ると画面が乱れる。
;
; 手順 1 を飛ばして呼ばれた回数は scroll_warp_live に出る（ppu_mask_shadow で見ている）。
; 0 でなければ、どこかが描画したまま背景を張り直している。
;
; 費用（実測）: カメラが列の境界に乗っているとき約 17900 サイクル（0.6 フレーム）、
; 最悪（左端が 32 列ブロックの右端にあるとき）約 76500 サイクル（2.6 フレーム）。
; そのあいだ画面は黒いままである。転換の演出（暗転）の裏に隠せる長さだが、
; 「一瞬で切り替わる」ようには見えないことを織り込んで呼ぶこと。
.proc scroll_warp_to
        sta cam_x_lo
        stx cam_x_hi

        ; --- 「描画無効中に呼ぶこと」が守られているかを数える ---
        ; これまでこの前提は呼び出し側の約束でしかなく、破っても何も起きなかった。
        ; 破ったときに起きるのは「bg_fill_window が $2007 を予算無視で数万サイクルぶん
        ; 叩く」ことであり、描画中なら画面が丸ごと壊れる。壊れ方が派手なわりに
        ; 原因（呼ぶ場所を間違えた）までが遠いので、目盛りを置く。
        ; 止めはしない。ここで引き返すと背景が張り直されないまま先へ進み、
        ; 「絵が古いまま」という別の壊れ方に化けるだけである。
        ; 費用は1回の warp あたり 8 サイクル（warp 自体が数万サイクルかかる）。
        lda ppu_mask_shadow
        and #(MASK_SHOW_BG | MASK_SHOW_SPR)
        beq @blanked
        inc scroll_warp_live
@blanked:

        ; ステージの右端でクランプする（左端は 0 で、16bit の下限なので自明）。
        lda cam_limit_hi
        cmp cam_x_hi
        bcc @clamp
        bne @in_range
        lda cam_limit_lo
        cmp cam_x_lo
        bcs @in_range
@clamp:
        lda cam_limit_lo
        sta cam_x_lo
        lda cam_limit_hi
        sta cam_x_hi
@in_range:
        jsr vram_queue_reset

        ; 列追跡の状態も一緒に初期化する。ここを置き直さないと、張り直した直後に
        ; 「古い cam_col から新しい cam_col まで」の列が延々と積み直され、
        ; せっかく書いた絵の上に同じ絵を数十フレームかけて書き戻すことになる。
        jsr cam_x_to_col
        lda tmp0
        sta cam_col_lo
        sta bg_col_lo
        lda tmp1
        sta cam_col_hi
        sta bg_col_hi
        jsr bg_fill_window
        jmp cam_publish                  ; NMI に見せる値も新しいカメラに合わせる
.endproc

; ---------------------------------------------------------------- 公開
; cam_x を NMI に見せる。**1フレームに1回、カメラを動かし終えてから呼ぶこと。**
;
; なぜこの形なら競合しないか:
;   * NMI が読むのは cam_pub_lo/hi の「cam_pub_sel が指す面」だけである。
;   * ここで書くのは **NMI が読んでいない方の面** なので、書いている最中に NMI が
;     入っても、NMI は触られていない面（＝前フレームの完全な組）を読む。
;   * 面を切り替えるのは最後の stx cam_pub_sel **1命令**である。6502 の命令は
;     NMI に途中で割られないので、NMI から見た cam_pub_sel は切り替えの前か後の
;     どちらかしかない。中途半端な lo/hi の組を読む窓が存在しない。
;   * したがって「メインが更新中にフレームが来たら前フレームの値を使う」が
;     自動的に成り立つ。1フレーム古い値で描くことはあっても、画面は飛ばない。
; 順序の要件: lo と hi を書き終える**前に** cam_pub_sel を書いてはならない。
; 逆にすると、書きかけの面を NMI に見せることになり、対策の意味が消える。
.proc cam_publish
        lda cam_pub_sel
        eor #1
        tax                              ; X = いま NMI が読んでいない面
        lda cam_x_lo
        sta cam_pub_lo, x
        lda cam_x_hi
        sta cam_pub_hi, x
        stx cam_pub_sel                  ; ★ ここで初めて新しい組が見える
        rts
.endproc

; ステージの横幅（ドット）からカメラの上限を決める。
;   入力: A = 幅 下位, X = 幅 上位
; 上限 = 幅 - 画面幅。1画面に満たないステージならスクロールしない（上限 0）。
.proc cam_set_stage_width
        sta stage_w_lo
        stx stage_w_hi
        sec
        sbc #<SCREEN_W
        sta cam_limit_lo
        txa
        sbc #>SCREEN_W
        sta cam_limit_hi
        bcs @done
        lda #0                           ; 幅が1画面未満 → 借りが出た
        sta cam_limit_lo
        sta cam_limit_hi
@done:
        rts
.endproc

; 毎フレーム1回、メインループから呼ぶ（NMI からではない）。
; 操作キャラを不感帯つきで追い、新しく見えるようになった列を転送キューに積み、
; 最後に **cam_publish で NMI に見せる**。
; 公開をここに畳んであるのは、呼び忘れると「NMI が1フレーム古いカメラを出し続ける」
; という気付きにくい壊れ方をするためである。カメラを動かす入口はここと
; scroll_warp_to の2つだけにし、どちらも最後に公開する。
.proc cam_update
        jsr cam_follow_player
        jsr scroll_track_columns
        jmp cam_publish
.endproc

; ---------------------------------------------------------------- 追従
; 不感帯の外に出たぶんだけカメラを動かし、ステージの端でクランプする。
;
; 画面X = ワールドX - カメラX なので、
;   「操作キャラが不感帯より右」は カメラX < ワールドX - 右端 と同値である。
; そこで両端の「これ以上は動かせない」カメラ位置を作って挟むだけでよい。
; tmp0/tmp1 = その目標カメラX（16bit）。
.proc cam_follow_player
        ldx #ENT_PLAYER

        ; --- 右の不感帯: 目標 = ワールドX - CAM_DEADZONE_RIGHT ---
        lda ent_x_lo, x
        sec
        sbc #CAM_DEADZONE_RIGHT
        sta tmp0
        lda ent_x_hi, x
        sbc #0
        sta tmp1
        bcc @left                        ; 目標が負 → カメラ(>=0) は既に右にある

        lda cam_x_hi                     ; カメラ < 目標 なら引き寄せる
        cmp tmp1
        bcc @take
        bne @left
        lda cam_x_lo
        cmp tmp0
        bcs @left
@take:
        lda tmp0
        sta cam_x_lo
        lda tmp1
        sta cam_x_hi
        jmp @clamp

        ; --- 左の不感帯: 目標 = ワールドX - CAM_DEADZONE_LEFT ---
@left:
        lda ent_x_lo, x
        sec
        sbc #CAM_DEADZONE_LEFT
        sta tmp0
        lda ent_x_hi, x
        sbc #0
        sta tmp1
        bcs @left_cmp
        lda #0                           ; 目標が負 → 0 に丸める（ステージの左端）
        sta tmp0
        sta tmp1
@left_cmp:
        lda tmp1                         ; カメラ > 目標 なら引き戻す
        cmp cam_x_hi
        bcc @take_left
        bne @clamp
        lda tmp0
        cmp cam_x_lo
        bcs @clamp
@take_left:
        lda tmp0
        sta cam_x_lo
        lda tmp1
        sta cam_x_hi

        ; --- ステージの右端でクランプ（左端 0 は上の丸めで既に守られている）---
@clamp:
        lda cam_limit_hi
        cmp cam_x_hi
        bcc @do_clamp
        bne @done
        lda cam_limit_lo
        cmp cam_x_lo
        bcs @done
@do_clamp:
        lda cam_limit_lo
        sta cam_x_lo
        lda cam_limit_hi
        sta cam_x_hi
@done:
        rts
.endproc

; カメラの左端が乗っているタイル列を tmp0/tmp1 に置く（= cam_x >> TILE_W_SHIFT）。
; 列追跡と飛び込みの両方が使う。
.proc cam_x_to_col
        lda cam_x_hi
        lsr a
        sta tmp1
        lda cam_x_lo
        ror a
        lsr tmp1
        ror a
        lsr tmp1
        ror a
        sta tmp0
        rts
.endproc

; ---------------------------------------------------------------- 列の追跡
; カメラの左端タイル列 (cam_x >> 3) が変わったら、新しく見えるようになった列を積む。
;
; cam_col = c のとき、画面には c から c + SCREEN_TILES_W までの **33 列**が写る。
; 画面幅はちょうど 32 列だが、カメラが列の境界にぴったり乗っていない限り、
; 右端に1列ぶんがはみ出して見えるためである。ここを 32 列と数えると、
; 右端の1列が「1ドットだけ見えているのに中身が古いまま」になる。
;   右へ1列動いたとき: 新しく現れるのは (c+1) + SCREEN_TILES_W の列
;   左へ1列動いたとき: 新しく現れるのは (c-1) の列（左端はカメラの列そのもの）
; 積めた（キューに空きがあった）ときだけ cam_col を進める。積めなければ cam_col を
; 据え置いて次フレームに持ち越すので、列が黙って抜け落ちることはない。
;
; tmp0/tmp1 = カメラの新しい列、tmp2/tmp3 = 積む列。
.proc scroll_track_columns
        jsr cam_x_to_col                 ; tmp0/tmp1 = cam_x >> TILE_W_SHIFT

@loop:
        lda tmp0                         ; 追いついたら終わり
        cmp cam_col_lo
        bne @differ
        lda tmp1
        cmp cam_col_hi
        beq @done
@differ:
        lda tmp1                         ; 新しい列 < 今の列 なら左へ動いた
        cmp cam_col_hi
        bcc @go_left
        bne @go_right
        lda tmp0
        cmp cam_col_lo
        bcc @go_left

        ; --- 右へ1列。現れるのは (cam_col + 1) + SCREEN_TILES_W ---
@go_right:
        lda cam_col_lo
        clc
        adc #(SCREEN_TILES_W + 1)
        sta tmp2
        lda cam_col_hi
        adc #0
        sta tmp3
        jsr queue_column_and_attr
        bcs @done                        ; キューが一杯。cam_col は進めず次フレームへ
        inc cam_col_lo
        bne @loop
        inc cam_col_hi
        jmp @loop

        ; --- 左へ1列。現れるのは cam_col - 1 ---
@go_left:
        lda cam_col_lo
        sec
        sbc #1
        sta tmp2
        lda cam_col_hi
        sbc #0
        sta tmp3
        jsr queue_column_and_attr
        bcs @done
        lda cam_col_lo
        bne :+
        dec cam_col_hi
:       dec cam_col_lo
        jmp @loop
@done:
        rts
.endproc

; tmp2/tmp3 の列を積む。属性列の切れ目にあたる列なら属性列も一緒に積む。
;   出力: C=0 積めた / C=1 積めなかった（呼び出し側は cam_col を進めないこと）
;
; 属性1バイトは4列ぶんを受け持つので、その4列のうち**最初に現れる列**で属性を積む。
; 右へ進むときは 4の倍数の列が最初に、左へ進むときは 4の倍数+3 の列が最初に現れる。
; ここでは向きを問わず「両端の列」で積む。同じ属性列を2度積むことはあるが、
; 内容は同じなので害は無く、片方だけ取りこぼす事故は起きない。
;
; 列は積めたが属性が積めなかったとき（キューが尽きたとき）は C=1 を返す。
; 呼び出し側は cam_col を進めないので、次のフレームに同じ列がもう一度積まれる。
; 二重に積んでも書く内容は同じなので実害は無く、「属性だけ永久に古いまま」を防げる。
; この取りこぼしの回数は vram.s の vq_overflow で数えている。
.proc queue_column_and_attr
        lda tmp2
        sta bg_col_lo
        lda tmp3
        sta bg_col_hi
        jsr bg_queue_column
        bcs @full

        lda tmp2
        and #(ATTR_TILES - 1)
        beq @attr                        ; 属性列の左端
        cmp #(ATTR_TILES - 1)
        beq @attr                        ; 属性列の右端
        clc
        rts
@attr:
        jmp bg_queue_attr                ; C はそのまま呼び出し元へ返る
@full:
        sec
        rts
.endproc
