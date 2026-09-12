; nmi.s — NMI ハンドラ。
; ここで行ってよいのは OAM DMA・VRAM 転送・スクロール設定・IRQ設定だけである。
; ゲームロジックを書いてはならない（CLAUDE.md 第4節）。
.include "constants.inc"
.include "zeropage.inc"

.export nmi_handler, irq_handler
.import oam_shadow

.segment "CODE"

.proc nmi_handler
        pha
        txa
        pha
        tya
        pha

        ; --- OAM DMA（VBlank 中に必ず行う。約 513 サイクル）---
        lda #0
        sta OAMADDR
        lda #>oam_shadow
        sta OAMDMA

        ; --- スクロール設定 ---
        ; P0 では固定。横スクロールは P1 で engine-dev が実装する。
        bit PPUSTATUS
        lda #0
        sta PPUSCROLL
        sta PPUSCROLL

        lda #(CTRL_NMI_ON | CTRL_SPR_8X16)
        sta PPUCTRL
        lda #(MASK_SHOW_SPR | MASK_SPR_LEFT)   ; P0 は黒画面＋スプライトのみ
        sta PPUMASK

        inc frame_counter
        lda #1
        sta nmi_done

        pla
        tay
        pla
        tax
        pla
        rti
.endproc

; IRQ は MMC3 のスキャンライン IRQ（ステータスバー分割）用。P0 では何もしない。
.proc irq_handler
        rti
.endproc
