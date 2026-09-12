; bank.s — MMC3 バンク制御。
; MMC3 レジスタを engine 外から直接叩いてはならない（CLAUDE.md 第4節）。
; MMC3 のレジスタは書き込み専用なので、現在値をこのモジュールが RAM で覚えておく。
.include "constants.inc"
.include "zeropage.inc"

.export mmc3_init, mmc3_set_mirroring, mmc3_set_prg_bank, mmc3_set_chr_bank
.export mmc3_irq_set, mmc3_irq_disable

.segment "BSS"
mmc3_select_shadow: .res 1      ; $8000 に最後に書いた値

.segment "CODE"

; MMC3 を既知の状態に初期化する。
;   PRG モード0 / CHR モード0
;   R6 = バンク0 ($8000), R7 = バンク1 ($A000)
;   CHR は R0..R5 に先頭から順に割り当てる
.proc mmc3_init
        lda #0
        sta mmc3_select_shadow

        ; CHR: R0 = 2KB@$0000, R1 = 2KB@$0800, R2..R5 = 1KB@$1000..$1C00
        ldx #0
@chr_loop:
        txa
        jsr select_reg
        lda chr_init_table, x
        sta MMC3_BANK_DATA
        inx
        cpx #6
        bne @chr_loop

        ; PRG: R6 = 0, R7 = 1
        lda #6
        jsr select_reg
        lda #0
        sta MMC3_BANK_DATA
        lda #7
        jsr select_reg
        lda #1
        sta MMC3_BANK_DATA

        jsr mmc3_irq_disable

        lda #0
        sta MMC3_PRG_RAM         ; PRG-RAM 無効（ADR-0001 の決定までは使わない）
        rts
.endproc

; A = バンクレジスタ番号 (0..7)。モードビットを保ったまま選択する。
.proc select_reg
        ora #(MMC3_PRG_MODE_0 | MMC3_CHR_MODE_0)
        sta mmc3_select_shadow
        sta MMC3_BANK_SELECT
        rts
.endproc

; A = MIRROR_HORIZONTAL / MIRROR_VERTICAL
.proc mmc3_set_mirroring
        sta MMC3_MIRRORING
        rts
.endproc

; X = レジスタ番号 (6 または 7), A = PRG バンク番号
.proc mmc3_set_prg_bank
        pha
        txa
        jsr select_reg
        pla
        sta MMC3_BANK_DATA
        rts
.endproc

; X = レジスタ番号 (0..5), A = CHR バンク番号
.proc mmc3_set_chr_bank
        pha
        txa
        jsr select_reg
        pla
        sta MMC3_BANK_DATA
        rts
.endproc

; A = IRQ を発生させるスキャンライン数。ステータスバー分割に使う。
.proc mmc3_irq_set
        sta MMC3_IRQ_LATCH
        sta MMC3_IRQ_RELOAD      ; 次のラインでラッチを読み込ませる
        sta MMC3_IRQ_ENABLE
        rts
.endproc

.proc mmc3_irq_disable
        sta MMC3_IRQ_DISABLE
        rts
.endproc

.segment "RODATA"
chr_init_table:
        .byte $00, $02           ; R0, R1: 2KB バンク
        .byte $04, $05, $06, $07 ; R2..R5: 1KB バンク
