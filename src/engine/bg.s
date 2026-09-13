; bg.s — **確認用の仮背景**。
;
; ここにあるのは「横スクロールしていることが画面で分かる」ためだけの模様であり、
; ステージの中身ではない。レイアウトのデータ形式はここで決めない。
; それは P3 で stage-author が data/layouts/ を設計して決めることであり、
; ここで形式を決めると後で全部やり直しになる（CLAUDE.md 第1条・担当境界）。
;
; したがってこのファイルは:
;   * 地形を**式で生成**する（縞・床線・等間隔の柱）。表もデータファイルも持たない
;   * シナリオ固有の地形・プロップ・ギミックを一切書かない
;   * P3 で丸ごと差し替えられる前提で書く。外に出しているのは
;     bg_fill_window / bg_queue_column / bg_queue_attr の3つだけ
;
; engine が P3 以降も持ち続けるのは「列が現れたら1列ぶん転送する」という段取りであって、
; その1列の中身をどこから取るかは差し替え点である。
.include "constants.inc"
.include "zeropage.inc"

.export bg_fill_window, bg_queue_column, bg_queue_attr
.export bg_col_lo, bg_col_hi

.import vram_queue_open, vram_queue_byte, vram_queue_close
.import vq_dst_lo, vq_dst_hi

; --- 仮背景のタイル番号（chr/bg.png の並び。背景パターンテーブルは $1000 側）---
; constants.inc に上げていないのは、このファイルごと P3 で消えるものだからである。
BG_TILE_BLANK   = 0
BG_TILE_WALL    = 1
BG_TILE_PILLAR  = 2      ; 8列（64ドット）おきに立てる。スクロール量が目で数えられる
BG_TILE_EDGE    = 3      ; 床の奥の縁
BG_TILE_FLOOR_A = 4      ; 床の市松。A と B が交互に並ぶ
BG_TILE_FLOOR_B = 5

BG_PILLAR_MASK  = 7      ; (列 & これ) == 0 の列に柱を立てる

; --- 行の帯。レーンの足元Y（src/engine/lane.s の lane_ground_y = 148/160/176/196）が
;     床の帯に載るように切ってある。上の3行は、P2 で MMC3 IRQ で分割する
;     ステータスバーのための余白である。
BG_ROW_WALL_TOP = 3      ; 0..2 空白
BG_ROW_EDGE     = 18     ; 3..17 壁 / 18 床の奥の縁 / 19.. 床
.assert BG_ROW_EDGE < SCREEN_TILES_H, error, "床の縁が画面の外にある"

; 属性（パレット選択）は属性列ごとに2種類を交互にする。見た目のためではなく、
; **属性テーブルの転送が背景に追随しているかを目で確かめる**ためである。
; 縞がタイルの模様とずれて流れたら、属性の転送が落ちている。
; 縞の周期が 64 列（= ネームテーブル2枚 = 背景の巻き取り幅）を割り切ってはならない。
; 割り切ると、巻き取ったとき「新しい列の属性」と「そのスロットに残っていた古い属性」が
; 必ず一致してしまい、属性を転送していなくても画面が正しく見える。
; それでは確認用の背景として役に立たない。そこで
;   パレット = 列の bit2 ^ 列の bit6
; とする。4列（32ドット）ごとの縞が、64列ごとに白黒反転する。
; こうすると巻き取りのたびに必ず値が変わるので、属性の転送が落ちれば必ず目に見える。
BG_ATTR_EVEN   = $00         ; 4象限すべてパレット0
BG_ATTR_ODD    = $FF         ; 4象限すべてパレット3
BG_ATTR_BIT    = %01000000   ; 上の eor のあと、この位置に「bit2 ^ bit6」が立つ

.segment "BSS"

; 転送したいワールドのタイル列。呼び出し側（scroll.s）が詰める。
; 仮背景は下位バイトしか見ない（模様の周期も、ネームテーブルの巻き取り 64 列も
; 256 を割り切るため）。上位まで受け取る形にしてあるのは、P3 のステージ読み出しが
; 列番号を 16bit で必要とするからである。
bg_col_lo:   .res 1
bg_col_hi:   .res 1

bg_row:      .res 1          ; 生成ループの行カウンタ（jsr をまたぐので自前で持つ）
bg_tile_a:   .res 1          ; 一括転送の床ループが使う交互タイル
bg_tile_b:   .res 1

; 一括転送（bg_fill_window）の作業変数。bg_col_lo/hi は生成器への引数として
; 書き換えながら使うので、張り直しの基準はこちらに控える。
bg_base_lo:  .res 1          ; 一括で埋めるネームテーブルの先頭ワールド列（32の倍数）
bg_base_hi:  .res 1
bg_extra:    .res 1          ; ブロックの右に追加で書く列数

.segment "CODE"

; ---------------------------------------------------------------- 1タイルの生成
; X = 行 (0..29) → A = タイル番号。X は壊さない。列は bg_col_lo を見る。
.proc bg_tile_at
        cpx #BG_ROW_WALL_TOP
        bcc @blank
        cpx #BG_ROW_EDGE
        bcc @wall
        beq @edge
        ; 床: 列と行の偶奇で市松にする。スクロールで模様が流れるのが見える
        txa
        eor bg_col_lo
        and #1
        clc
        adc #BG_TILE_FLOOR_A
        rts
@wall:
        lda bg_col_lo
        and #BG_PILLAR_MASK
        beq @pillar
        lda #BG_TILE_WALL
        rts
@pillar:
        lda #BG_TILE_PILLAR
        rts
@edge:
        lda #BG_TILE_EDGE
        rts
@blank:
        lda #BG_TILE_BLANK
        rts
.endproc

; bg_col_lo の列 → 属性バイト（A）。
.proc bg_attr_byte
        lda bg_col_lo
        asl a
        asl a
        asl a
        asl a                            ; 列の bit2 を bit6 まで持ち上げる
        eor bg_col_lo
        and #BG_ATTR_BIT                 ; = 列の bit2 ^ bit6
        beq @even
        lda #BG_ATTR_ODD
        rts
@even:
        lda #BG_ATTR_EVEN
        rts
.endproc

; ---------------------------------------------------------------- VRAM アドレス
; bg_col_lo の列 → ネームテーブル上のアドレスを vq_dst_lo/hi に置く。
;   垂直ミラーリングなので、ワールド列 c は
;   ネームテーブル (c>>5)&1 の第 (c&31) 列に載る（背景は 64 列 = 512 ドットで巻き取る）。
.proc bg_column_addr
        lda bg_col_lo
        and #(SCREEN_TILES_W - 1)
        sta vq_dst_lo
        lda bg_col_lo
        and #SCREEN_TILES_W              ; 列の bit5 = どちらのネームテーブルか
        beq @nt0
        lda #>(NT_BASE + NT_STRIDE)
        sta vq_dst_hi
        rts
@nt0:
        lda #>NT_BASE
        sta vq_dst_hi
        rts
.endproc

; bg_col_lo の列 → その列を含む属性列の先頭アドレスを vq_dst_lo/hi に置く。
.proc bg_attr_addr
        lda bg_col_lo
        lsr a
        lsr a                            ; 属性列 = 列 / ATTR_TILES
        and #(ATTR_COLS - 1)
        clc
        adc #<NT_ATTR_OFFSET
        sta vq_dst_lo
        lda bg_col_lo
        and #SCREEN_TILES_W
        beq @nt0
        lda #>(NT_BASE + NT_STRIDE + NT_ATTR_OFFSET)
        sta vq_dst_hi
        rts
@nt0:
        lda #>(NT_BASE + NT_ATTR_OFFSET)
        sta vq_dst_hi
        rts
.endproc

; ---------------------------------------------------------------- キューへ積む
; bg_col_lo/hi の列のネームテーブル1列（30タイル）を転送キューに積む。
;   出力: C=0 積めた / C=1 キューに空きが無い（呼び出し側が次フレームに回す）
.proc bg_queue_column
        jsr bg_column_addr
        ldx #SCREEN_TILES_H
        lda #VQ_INC32                    ; 縦1列なのでアドレスは +32 ずつ
        jsr vram_queue_open
        bcs @full
        lda #0
        sta bg_row
@loop:
        ldx bg_row
        jsr bg_tile_at
        jsr vram_queue_byte
        inc bg_row
        lda bg_row
        cmp #SCREEN_TILES_H
        bne @loop
        jsr vram_queue_close
        clc
        rts
@full:
        sec
        rts
.endproc

; bg_col_lo/hi の列を含む属性列（8バイト）を転送キューに積む。
;   出力: C=0 積めた / C=1 キューに空きが無い
.proc bg_queue_attr
        jsr bg_attr_addr
        ldx #ATTR_ROWS
        lda #VQ_STEP_ATTR                ; 属性列は 8 バイトおき（PPU の自動加算では届かない）
        jsr vram_queue_open
        bcs @full
        lda #ATTR_ROWS
        sta bg_row
@loop:
        jsr bg_attr_byte
        jsr vram_queue_byte
        dec bg_row
        bne @loop
        jsr vram_queue_close
        clc
        rts
@full:
        sec
        rts
.endproc

; ---------------------------------------------------------------- 一括転送
; **任意のカメラ位置から、見えている背景を丸ごと書き直す入口。**
;   入力: bg_col_lo/hi = 画面左端のタイル列（= cam_x >> 3。32 の倍数でなくてよい）
;   **描画無効中に呼ぶこと**（$2007 を予算無視で叩くため）。
;   呼び出し側が守るべき手順は src/engine/scroll.s の scroll_warp_to に書いてある。
;
; 書くのは可視 33 列（画面の 32 列＋右端にはみ出す1列）である。2枚のネームテーブルを
; 丸ごと埋めないのは起動時間の都合で、起動もこの入口を通るためである
; （ここが長引くと最初の NMI が遅れる。run_tests.py の BOOT_FRAMES_MAX を見よ）。
; 見えていない列は、カメラが動くにつれて転送キューが1列ずつ埋める。
;
; 段取りは2つに分かれる:
;   1. 左端の列を含む「32 列ブロック」（= ネームテーブル1枚ぶん）を行単位で一括に書く
;   2. その右隣から (列 & 31) + 1 列を、1列ずつ書く
; ブロックの先頭は必ず 32 の倍数なので、柱の周期 (8) も床の市松 (2) も位相が
; 列0 と揃う。だから 1 の一括ループは列番号を見ずに書ける。
.proc bg_fill_window
        ; --- 追加で書く列数 = (列 & 31) + 1 ---
        ; ブロックが覆うのは 列 .. ブロック先頭+31。可視列は 列 .. 列+32 なので、
        ; はみ出すのは (列 - ブロック先頭) + 1 列である。左端が列の境界に乗っていても
        ; 1 列は必ずはみ出す（可視列が 33 列であることの言い換え）。
        lda bg_col_lo
        and #(SCREEN_TILES_W - 1)
        clc
        adc #1
        sta bg_extra

        lda bg_col_lo                    ; ブロック先頭 = 列 & ~31
        and #<(256 - SCREEN_TILES_W)
        sta bg_base_lo
        lda bg_col_hi
        sta bg_base_hi
        jsr bg_fill_block

        ; --- ブロックの右隣から bg_extra 列 ---
        lda bg_base_lo
        clc
        adc #SCREEN_TILES_W
        sta bg_col_lo
        lda bg_base_hi
        adc #0
        sta bg_col_hi
@extra:
        jsr bg_write_column_now
        lda bg_col_lo                    ; 属性列の左端に来たら、その属性列も書く。
        and #(ATTR_TILES - 1)            ; 追加列は 32 の倍数から始まるので、
        bne @next                        ; 触れる属性列の左端は必ずこの範囲に入る
        jsr bg_write_attr_now
@next:
        inc bg_col_lo
        bne :+
        inc bg_col_hi
:       dec bg_extra
        bne @extra
        rts
.endproc

; bg_base_lo/hi（32 の倍数）から 32 列ぶん＝ネームテーブル1枚を一括で書く。
; 1セルずつ生成すると起動時間に響くので、ここだけは行ごとに帯を判定して、
; その帯専用のループで 32 バイトを流す。生成規則は bg_tile_at と同じものである。
.assert (SCREEN_TILES_W .MOD (BG_PILLAR_MASK + 1)) = 0, error, "柱の周期がブロック幅を割り切らない"
.proc bg_fill_block
        ; ここで PPUCTRL に NMI 許可ビットを書いてはならない。
        ; NMI は sei では止まらないので、立てた瞬間にハンドラが走り、
        ; この転送の途中で PPUADDR を奪われて画面が壊れる。
        ; NMI を立てるのは reset_handler の最後、描画開始のときの1回だけである。
        lda ppu_ctrl_shadow
        and #CTRL_NMI_OFF
        sta PPUCTRL                      ; アドレス加算は +1（行方向に流す）

        lda bg_base_lo                   ; 先頭列のアドレス = そのネームテーブルの原点
        sta bg_col_lo
        lda bg_base_hi
        sta bg_col_hi
        jsr bg_column_addr
        bit PPUSTATUS
        lda vq_dst_hi
        sta PPUADDR
        lda vq_dst_lo
        sta PPUADDR

        ldx #0                           ; X = 行
@row:
        cpx #BG_ROW_WALL_TOP
        bcc @blank_row
        cpx #BG_ROW_EDGE
        bcc @wall_row
        beq @edge_row

        ; --- 床の行: 2種類を交互に。行の偶奇で開始タイルを入れ替える ---
        txa
        and #1
        tay
        lda floor_first, y
        sta bg_tile_a
        lda floor_second, y
        sta bg_tile_b
        ldy #(SCREEN_TILES_W / 2)
@floor_loop:
        lda bg_tile_a
        sta PPUDATA
        lda bg_tile_b
        sta PPUDATA
        dey
        bne @floor_loop
        jmp @row_done

@blank_row:
        lda #BG_TILE_BLANK
        jmp @solid_row
@edge_row:
        lda #BG_TILE_EDGE
@solid_row:
        ldy #SCREEN_TILES_W
@solid_loop:
        sta PPUDATA
        dey
        bne @solid_loop
        jmp @row_done

        ; --- 壁の行: 「柱1本 + 壁 BG_PILLAR_MASK 本」の組を繰り返す ---
        ; 1列ずつ (列 & マスク) を判定するより速い。起動時間は最初の NMI までの
        ; 締め切りに直結するので、ここだけは組単位で流す。
@wall_row:
        txa
        pha                              ; 行番号を退避（X を組の数え上げに使う）
        ldy #(SCREEN_TILES_W / (BG_PILLAR_MASK + 1))
@wall_group:
        lda #BG_TILE_PILLAR
        sta PPUDATA
        lda #BG_TILE_WALL
        ldx #BG_PILLAR_MASK
@wall_run:
        sta PPUDATA
        dex
        bne @wall_run
        dey
        bne @wall_group
        pla
        tax

@row_done:
        inx
        cpx #SCREEN_TILES_H
        beq @attr
        jmp @row

        ; --- このネームテーブルの属性テーブル 64 バイト ---
@attr:
        lda bg_base_lo
        sta bg_col_lo
        jsr bg_attr_addr                 ; 先頭列の属性 = 属性テーブルの原点
        bit PPUSTATUS
        lda vq_dst_hi
        sta PPUADDR
        lda vq_dst_lo
        sta PPUADDR
        ldx #0                           ; X = 属性テーブルの通し番号
@attr_loop:
        txa
        and #(ATTR_COLS - 1)             ; 属性列
        asl a
        asl a                            ; 属性列 → タイル列（模様の周期に合わせる）
        clc
        adc bg_base_lo                   ; ブロック先頭を足してワールド列に直す
        sta bg_col_lo                    ; （模様は列で決まる。仮背景は下位しか見ない）
        stx bg_row
        jsr bg_attr_byte
        sta PPUDATA
        ldx bg_row
        inx
        cpx #(ATTR_COLS * ATTR_ROWS)
        bne @attr_loop
        rts
.endproc

; bg_col_lo の列を、キューを通さずその場で書く。**描画無効中専用**。
.proc bg_write_column_now
        lda ppu_ctrl_shadow
        and #CTRL_NMI_OFF                ; 初期化中は NMI を立てない（上の注意を見よ）
        ora #CTRL_INC_32
        sta PPUCTRL
        jsr bg_column_addr
        bit PPUSTATUS
        lda vq_dst_hi
        sta PPUADDR
        lda vq_dst_lo
        sta PPUADDR
        lda #0
        sta bg_row
@loop:
        ldx bg_row
        jsr bg_tile_at
        sta PPUDATA
        inc bg_row
        lda bg_row
        cmp #SCREEN_TILES_H
        bne @loop
        lda ppu_ctrl_shadow              ; 加算を +1 に戻す
        and #CTRL_NMI_OFF
        sta PPUCTRL
        rts
.endproc

; bg_col_lo の列の属性列を、キューを通さずその場で書く。**描画無効中専用**。
.proc bg_write_attr_now
        jsr bg_attr_addr
        lda #ATTR_ROWS
        sta bg_row
@loop:
        bit PPUSTATUS
        lda vq_dst_hi
        sta PPUADDR
        lda vq_dst_lo
        sta PPUADDR
        jsr bg_attr_byte
        sta PPUDATA
        lda vq_dst_lo
        clc
        adc #ATTR_COLS                   ; 次の属性行は 8 バイト先
        sta vq_dst_lo
        dec bg_row
        bne @loop
        rts
.endproc

.segment "RODATA"
; 起動時の床ループが使う交互タイル。bg_tile_at の eor と同じ並びになる。
floor_first:
        .byte BG_TILE_FLOOR_A, BG_TILE_FLOOR_B
floor_second:
        .byte BG_TILE_FLOOR_B, BG_TILE_FLOOR_A
