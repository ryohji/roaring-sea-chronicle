; main.s — リセットハンドラとメインループ。
.include "constants.inc"
.include "zeropage.inc"

.export reset_handler
.import init_system, read_pad
.import ent_clear_all, ent_activate, ent_lane_move
.import ent_x_lo, ent_x_hi, ent_lane, ent_lane_step
.import ent_state, ent_class, ent_body, ent_tile, ent_attr
.import lane_update_all, oam_build, cam_update
.import stage_w_lo, stage_w_hi

; --- P1 の動作確認用シーンのパラメータ ---
; ここにあるのは engine の経路（入力 → エンティティ → レーン補間 → OAM → 画面）が
; 繋がっていることを確かめるための仮置きである。ステージの中身は P3 で
; stage-author が data/ から読む形に置き換える。ここに固有名詞を増やしてはならない。
PLAYER_HOME_X    = 120
PLAYER_HOME_LANE = 2
PLAYER_SPEED     = 1          ; 手触りの値ではない。動きが見えればよい
PLAYER_EDGE_MARGIN = 16       ; ステージ右端に残す余白（体1つぶん）
TEST_TILE_FIGURE = 0          ; 仮CHR: タイル0/1 が 8x16 の人型
TEST_ATTR_PLAYER = 0          ; スプライトパレット0
TEST_ATTR_ENEMY  = 1          ; スプライトパレット1
TEST_ENEMY_COUNT = 3

.segment "CODE"

.proc reset_handler
        ; CPU 自身の初期化はここで行う。サブルーチンに出してはならない
        ; （txs より後に jsr の戻り先が積まれるため）。
        sei
        cld
        ldx #$FF
        txs

        jsr init_system

        jsr scene_test_init
        jsr oam_build           ; 最初の DMA に間に合うよう1回組んでおく

        ; 描画を開始する。値はシャドウが持っている（init_system が決めた）。
        ; ここで固定値を直書きしないこと。NMI と、のちのステータスバー分割 IRQ が
        ; 同じシャドウを読んで書き戻すので、出どころが2つあると競合する。
        ; スクロールを先に置いてから描画を有効にする。初期転送で PPUADDR を
        ; 動かしたままにしてあるので、置き直さないと最初の1フレームが流れて見える。
        bit PPUSTATUS
        lda ppu_ctrl_shadow              ; ここで初めて NMI 許可ビットが立つ
        sta PPUCTRL
        lda cam_x_lo
        sta PPUSCROLL
        lda #0
        sta PPUSCROLL
        lda ppu_mask_shadow
        sta PPUMASK

        cli
        jmp main_loop
.endproc

; 1フレームの並び。OAM の構築（並べ替えと展開）と VRAM 転送の積み込みは
; **描画期間中**に済ませ、NMI は完成済みのものを流すだけにする
; （CLAUDE.md 第4節 / ADR-0002）。
; cam_update を oam_build より前に置いてあるのは、同じフレームのカメラ位置で
; スプライトを組むためである。ここが前後すると、スクロール中に背景とキャラが1フレームずれる。
.proc main_loop
@frame:
        jsr wait_nmi
        jsr read_pad
        jsr update_player_test
        jsr lane_update_all
        jsr cam_update          ; カメラ追従 + 現れた列を転送キューへ
        jsr oam_build
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

; --- P1 の動作確認用シーン ---
; 操作キャラ1体と、静止した敵3体を別々のレーンに置く。
; 敵の思考（AI）は ai-dev の範囲なので書かない。ここでは「置くだけ」である。
.proc scene_test_init
        jsr ent_clear_all

        ldx #ENT_PLAYER
        lda #<PLAYER_HOME_X
        sta ent_x_lo, x
        lda #>PLAYER_HOME_X
        sta ent_x_hi, x
        lda #PLAYER_HOME_LANE
        sta ent_lane, x
        lda #SPR_CLASS_PLAYER
        sta ent_class, x
        lda #BODY_PLACEHOLDER
        sta ent_body, x
        lda #TEST_TILE_FIGURE
        sta ent_tile, x
        lda #TEST_ATTR_PLAYER
        sta ent_attr, x
        lda #ENT_ST_IDLE
        sta ent_state, x
        jsr ent_activate

        ldx #ENT_ENEMY_FIRST
        ldy #0
@enemy:
        lda test_enemy_x, y
        sta ent_x_lo, x
        lda #0
        sta ent_x_hi, x
        lda test_enemy_lane, y
        sta ent_lane, x
        lda test_enemy_body, y
        sta ent_body, x
        lda #SPR_CLASS_FAR_ENE
        sta ent_class, x
        lda #TEST_TILE_FIGURE
        sta ent_tile, x
        lda #TEST_ATTR_ENEMY
        sta ent_attr, x
        lda #ENT_ST_IDLE
        sta ent_state, x
        tya
        pha
        jsr ent_activate
        pla
        tay
        inx
        iny
        cpy #TEST_ENEMY_COUNT
        bne @enemy
        rts
.endproc

; 十字キーで操作キャラを動かす。手触りの実装ではなく、
; 「入力 → エンティティ → レーン補間 → OAM → 画面」の経路の確認である。
;   左右: ワールドX を動かす（16bit）
;   上下: レーンを1つ移動する（押した瞬間だけ。移動は lane.s が補間する）
; 実際の移動と攻撃は action-dev が書く。ここは engine の確認用に留めること。
.proc update_player_test
        ldx #ENT_PLAYER

        ; 左: ワールドの左端 (0) を越えさせない。
        ; 越えると 16bit が巻き取って「ステージの遥か右」になり、カメラが飛ぶ。
        lda pad_state
        and #PAD_LEFT
        beq @check_right
        lda ent_x_lo, x
        sec
        sbc #PLAYER_SPEED
        sta tmp0
        lda ent_x_hi, x
        sbc #0
        bcc @hit_left
        sta ent_x_hi, x
        lda tmp0
        sta ent_x_lo, x
        jmp @check_right
@hit_left:
        lda #0
        sta ent_x_lo, x
        sta ent_x_hi, x

        ; 右: ステージの右端を越えさせない。
        ; ステージ幅は scroll.s が持っている（ここに長さを埋めない）。
@check_right:
        lda pad_state
        and #PAD_RIGHT
        beq @check_lane
        lda ent_x_lo, x
        clc
        adc #PLAYER_SPEED
        sta tmp0
        lda ent_x_hi, x
        adc #0
        sta tmp1

        lda stage_w_lo                   ; 限界 = ステージ幅 - 体1つぶん
        sec
        sbc #PLAYER_EDGE_MARGIN
        sta tmp2
        lda stage_w_hi
        sbc #0
        sta tmp3

        lda tmp3                         ; 限界 < 新しい位置 ならクランプ
        cmp tmp1
        bcc @hit_right
        bne @store_x
        lda tmp2
        cmp tmp0
        bcs @store_x
@hit_right:
        lda tmp2
        sta tmp0
        lda tmp3
        sta tmp1
@store_x:
        lda tmp0
        sta ent_x_lo, x
        lda tmp1
        sta ent_x_hi, x

@check_lane:
        lda ent_lane_step, x
        bne @done                ; 補間中は次のレーン移動を受け付けない
        lda pad_pressed
        and #PAD_UP
        beq @check_down
        lda ent_lane, x
        beq @done                ; レーン0 より奥は無い
        sec
        sbc #1
        jsr ent_lane_move
        rts
@check_down:
        lda pad_pressed
        and #PAD_DOWN
        beq @done
        lda ent_lane, x
        cmp #LANE_LAST
        bcs @done                ; 最も手前のレーンより先は無い
        clc
        adc #1
        jsr ent_lane_move
@done:
        rts
.endproc

.segment "RODATA"

; 確認用の敵の配置。レーンごとの前後関係と体格型ごとのタイル数が
; 画面で見えるように、別レーン・別体格で置いてある。
test_enemy_x:
        .byte 64, 176, 112
test_enemy_lane:
        .byte 0, 1, LANE_LAST
test_enemy_body:
        .byte BODY_HEAVY, BODY_LIGHT, BODY_PLACEHOLDER
