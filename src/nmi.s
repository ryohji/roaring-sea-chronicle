; nmi.s — NMI ハンドラ。
; ここで行ってよいのは OAM DMA・VRAM 転送・スクロール設定・IRQ設定だけである。
; ゲームロジックを書いてはならない（CLAUDE.md 第4節）。
;
; 1フレームの並び:
;   OAM DMA（約513サイクル）→ VRAM 転送キュー（予算のぶんだけ）→ スクロール設定
; VRAM 転送を**スクロール設定より先**に置いてあるのは順序の要件である。
; $2006 への書き込みは PPU の内部アドレスを壊すので、$2005/$2000 でスクロールを
; 置き直すのは必ずそのあとでなければならない。
;
; PPUCTRL / PPUMASK は固定値を直書きせず、シャドウ（zeropage）を書く。
; ステータスバー分割の MMC3 スキャンライン IRQ を入れると IRQ ハンドラも
; これらを触るので、値の出どころを一箇所にしておかないと競合する。
.include "constants.inc"
.include "zeropage.inc"

.export nmi_handler, irq_handler
.import oam_shadow
.import vram_queue_flush

.segment "CODE"

.proc nmi_handler
        pha
        txa
        pha
        tya
        pha

        ; --- OAM DMA（VBlank 中に必ず行う。約 513 サイクル）---
        ; oam_ready が下りているときは、メインループが OAM シャドウを組み立てている
        ; 最中である。そのまま DMA すると「上半身だけ新しい」中途半端な OAM を
        ; 転送してしまう。1フレーム前の完成品を出したままにする方がまだ見られる。
        lda oam_ready
        beq @no_dma
        lda #0
        sta OAMADDR
        lda #>oam_shadow
        sta OAMDMA
@no_dma:

        ; --- VRAM 転送（スクロールで現れた列・属性・パレットなど）---
        ; 予算で刻むのはキューの側の仕事。ここは呼ぶだけである。
        jsr vram_queue_flush

        ; --- スクロール設定 ---
        ; ネームテーブルは左右に2枚（垂直ミラーリング）。カメラX の bit8 が
        ; そのままベースネームテーブルの選択ビットになる。
        ;
        ; **cam_x_lo/hi を直接読んではならない。** メインは 16bit のカメラを
        ; 2命令に分けて書くので、その隙に NMI が入ると lo が新・hi が旧の組を読み、
        ; 256ドット境界をまたぐ瞬間だけ画面が1画面ぶん飛ぶ。読むのは公開コピーである。
        ;
        ; 順序の要件: 添字 cam_pub_sel は**1回だけ**読んで X に取り、lo と hi を
        ; 同じ X で引くこと。lo と hi で読み直すと、その間にメインが面を切り替えたときに
        ; 別々の面から1バイトずつ拾う——つまり直そうとしている不整合そのものが戻る。
        bit PPUSTATUS                    ; $2005/$2006 の書き込みラッチを倒しておく
        ldx cam_pub_sel                  ; ここ1箇所でだけ面を決める
        lda cam_pub_hi, x
        and #CTRL_NT_X
        ora ppu_ctrl_shadow
        sta PPUCTRL
        lda cam_pub_lo, x
        sta PPUSCROLL                    ; 横
        lda #0
        sta PPUSCROLL                    ; 縦（横スクロール専用なので常に 0）
        lda ppu_mask_shadow
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

; IRQ は MMC3 のスキャンライン IRQ（ステータスバー分割）用。P1 では何もしない。
; ここを実装するときは、PPUCTRL/PPUMASK をシャドウ経由で触ること。
.proc irq_handler
        rti
.endproc
