; input.s — コントローラ読み取り。
; DPCM による読み取り不良を避けるため、2回読んで一致するまで繰り返す。
.include "constants.inc"
.include "zeropage.inc"

.export read_pad

.segment "CODE"

.proc read_pad
        lda pad_state
        sta tmp0                 ; 前フレームの状態を退避

@reread:
        jsr read_once
        lda tmp1
        sta tmp2                 ; 1回目の結果
        jsr read_once
        lda tmp1
        cmp tmp2
        bne @reread              ; 一致するまで読み直す

        sta pad_state

        ; 押された瞬間 = 今フレーム ON かつ 前フレーム OFF
        lda tmp0
        eor #$FF
        and pad_state
        sta pad_pressed
        rts
.endproc

; 1回ぶんの読み取り。結果は tmp1。
.proc read_once
        lda #1
        sta JOYPAD1
        lda #0
        sta JOYPAD1
        ldx #8
@loop:
        lda JOYPAD1
        lsr a                    ; bit0 -> キャリー
        rol tmp1                 ; A, B, Select, Start, Up, Down, Left, Right の順で入る
        dex
        bne @loop
        rts
.endproc
