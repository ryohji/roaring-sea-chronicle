; init.s — 電源投入時の初期化。
; ここに書いてよいのは「ハードウェアを既知の状態に置く」処理だけである。
.include "constants.inc"
.include "zeropage.inc"

.export init_system
.import mmc3_init, mmc3_set_mirroring
.import oam_shadow

.segment "CODE"

; 注意: スタックポインタの初期化は reset_handler で済ませてある。
; ここで txs をやると jsr の戻り先を失う。
.proc init_system
        ldx #0
        stx PPUCTRL             ; NMI 無効
        stx PPUMASK             ; 描画無効
        stx APU_DMC_FREQ        ; DMC IRQ 無効
        stx APU_STATUS          ; APU 全チャンネル停止
        lda #$40
        sta APU_FRAME           ; フレームIRQ 無効

        ; --- 1回目の VBlank 待ち（PPU のウォームアップ）---
        bit PPUSTATUS
@vblank1:
        bit PPUSTATUS
        bpl @vblank1

        ; --- RAM クリア。OAM シャドウだけは画面外に追い出す ---
        ; $0100-$01FF はスタック。init_system は jsr で呼ばれており、
        ; ここに戻り先アドレスが載っているのでクリアしてはならない。
        lda #0
        ldx #0
@clear:
        sta $0000, x
        sta $0300, x
        sta $0400, x
        sta $0500, x
        sta $0600, x
        sta $0700, x
        inx
        bne @clear

        lda #OAM_Y_OFFSCREEN
        ldx #0
@clear_oam:
        sta oam_shadow, x
        inx
        bne @clear_oam

        ; --- 2回目の VBlank 待ち（ここで PPU が安定する）---
@vblank2:
        bit PPUSTATUS
        bpl @vblank2

        jsr mmc3_init
        lda #MIRROR_HORIZONTAL   ; ベルトスクロールは横スクロールなので水平ミラーリング
        jsr mmc3_set_mirroring
        jsr load_palette
        rts
.endproc

; パレットを $3F00 に転送する。呼び出しは描画無効中に限る。
.proc load_palette
        bit PPUSTATUS            ; アドレスラッチをリセット
        lda #$3F
        sta PPUADDR
        lda #$00
        sta PPUADDR
        ldx #0
@loop:
        lda palette_data, x
        sta PPUDATA
        inx
        cpx #32
        bne @loop
        rts
.endproc

.segment "RODATA"

; P0 の暫定パレット。CHR とパレットの本実装は P1 以降。
palette_data:
        ; 背景
        .byte $0F, $01, $11, $21
        .byte $0F, $06, $16, $26
        .byte $0F, $09, $19, $29
        .byte $0F, $00, $10, $30
        ; スプライト
        .byte $0F, $16, $27, $30
        .byte $0F, $11, $21, $30
        .byte $0F, $19, $29, $30
        .byte $0F, $06, $26, $30
