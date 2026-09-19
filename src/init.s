; init.s — 電源投入時の初期化。
; ここに書いてよいのは「ハードウェアを既知の状態に置く」処理だけである。
.include "constants.inc"
.include "zeropage.inc"

.export init_system
.import mmc3_init, mmc3_set_mirroring
.import oam_shadow
.import scroll_init
.import depth_init

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

        ; 横スクロールには**垂直**ミラーリングを使う。
        ; NES の用語は紛らわしいが、垂直ミラーリング = ネームテーブルが**左右**に2枚並ぶ
        ; （$2000 と $2400 が別の内容を持ち、$2800/$2C00 がその写し）配置である。
        ; 水平ミラーリングだと $2000 と $2400 が同じ内容になり、横スクロールで
        ; 右から出てくる画面が左と同じものになってしまう。
        lda #MIRROR_VERTICAL
        jsr mmc3_set_mirroring

        ; PPUCTRL / PPUMASK のシャドウを決める。以降ここ以外で固定値を直書きしない
        ; （NMI も、ステータスバー分割の IRQ も、このシャドウを読んで書く）。
        ;   背景パターンテーブルは $1000 側。スプライトは 8x16 モードなので
        ;   タイル番号の bit0 がパターンテーブルを選ぶ（PPUCTRL bit3 は効かない）。
        ;   ネームテーブル選択ビットは 0 のままにしておくこと。
;   NMI がカメラの公開コピー (cam_pub_hi) から毎フレーム載せる。
        lda #(CTRL_NMI_ON | CTRL_SPR_8X16 | CTRL_BG_1000 | CTRL_INC_1)
        sta ppu_ctrl_shadow

        jsr load_palette
        jsr depth_init           ; 歩ける帯（足元Yの上下限）を既定値に置く
        jsr scroll_init          ; カメラを原点に置き、そこから見える仮背景を書く

        ; PPUMASK のシャドウは**初期転送が終わってから**置く。
        ; ここまでは実機の描画も無効（$2001 に 0 を書いたまま）なので、
        ; シャドウが 0 のままである方が実物と合う。合わせておくと
        ; 「描画中に scroll_warp_to を呼んでいないか」をシャドウで見張れる
        ; （src/engine/scroll.s の scroll_warp_live）。先に置くと、起動の
        ; 初期転送そのものが毎回1件計上されて目盛りが使いものにならない。
        ; 実際に $2001 へ書くのは reset_handler（描画開始）と NMI である。
        lda #(MASK_SHOW_BG | MASK_SHOW_SPR | MASK_BG_LEFT | MASK_SPR_LEFT)
        sta ppu_mask_shadow
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
        .byte $0F, $16, $27, $30   ; 0: 操作キャラ（赤）
        .byte $0F, $11, $21, $30   ; 1: 敵（青）
        .byte $0F, $19, $29, $30   ; 2: 仲間（緑）
        ; 3: 影と効果（SPR_PAL_EFFECT）。**色2 だけを $26 から $0F（黒）に変えてある。**
        ; 影の絵（chr/sprites.png のタイル48）は色2 で塗られており、仮背景の床は
        ; どちらの属性でも色2（$11 の青 / $10 の明灰）で塗られている。黒はそのどちらより
        ; 暗いので、属性の縞をまたいでも影が消えない。
        ; このパレットは action-dev の ACT_PAL_HURT / ACT_PAL_DOWN_ALT（点滅色）と
        ; 共用である（スプライトパレットは4本しかない）。色1（輪郭）と色3（明部）を
        ; 元のままにしてあるのは、点滅色としての見え方を変えないためである。
        .byte $0F, $06, $0F, $30
