; main.s — リセットハンドラとメインループ。
.include "constants.inc"
.include "zeropage.inc"

.export reset_handler
.import init_system, read_pad, oam_shadow

; P0 の動作確認用スプライトの初期位置
SPRITE_HOME_X = 120
SPRITE_HOME_Y = 112
.segment "CODE"

.proc reset_handler
        ; CPU 自身の初期化はここで行う。サブルーチンに出してはならない
        ; （txs より後に jsr の戻り先が積まれるため）。
        sei
        cld
        ldx #$FF
        txs

        jsr init_system

        jsr setup_test_sprite

        ; NMI を有効化して描画を開始する
        lda #(CTRL_NMI_ON | CTRL_SPR_8X16)
        sta PPUCTRL
        lda #(MASK_SHOW_SPR | MASK_SPR_LEFT)   ; P0 は黒画面＋スプライトのみ
        sta PPUMASK

        cli
        jmp main_loop
.endproc

.proc main_loop
@frame:
        jsr wait_nmi
        jsr read_pad
        jsr update_test_sprite
        jmp @frame
.endproc

; NMI が来るまで待つ。1フレーム1回だけ呼ぶこと。
.proc wait_nmi
        lda #0
        sta nmi_done
@wait:
        lda nmi_done
        beq @wait
        rts
.endproc

; --- P0 の動作確認用スプライト（8x16 を1体）---
; P1 で engine-dev のスプライト割当に置き換える。
.proc setup_test_sprite
        lda #SPRITE_HOME_Y
        sta oam_shadow + OAM_Y
        lda #0                          ; 8x16 モードではタイル番号の bit0 がパターンテーブル選択
        sta oam_shadow + OAM_TILE
        lda #0                          ; パレット0、前面
        sta oam_shadow + OAM_ATTR
        lda #SPRITE_HOME_X
        sta oam_shadow + OAM_X
        rts
.endproc

; 十字キーで左右に動かす。手触りの実装ではなく、
; 「入力 → OAM → 画面」の経路が繋がっていることの確認である。
.proc update_test_sprite
        lda pad_state
        and #PAD_LEFT
        beq @check_right
        dec oam_shadow + OAM_X
@check_right:
        lda pad_state
        and #PAD_RIGHT
        beq @done
        inc oam_shadow + OAM_X
@done:
        rts
.endproc
