; main.s — リセットハンドラとメインループ。
.include "constants.inc"
.include "zeropage.inc"

.export reset_handler
.import init_system, read_pad
.import ent_clear_all, ent_activate, ent_lane_move
.import ent_x_lo, ent_x_hi, ent_lane, ent_lane_step
.import ent_state, ent_class, ent_body, ent_tile, ent_attr
.import lane_update_all, oam_build, cam_update
.import action_init, action_update
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
        jsr action_init         ; HP・状態機械の初期化（scene_test_init がエンティティを置いた後）
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
        jsr action_update       ; 操作・攻撃・ダウンは src/action/ が持つ
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

.segment "RODATA"

; 確認用の敵の配置。レーンごとの前後関係と体格型ごとのタイル数が
; 画面で見えるように、別レーン・別体格で置いてある。
test_enemy_x:
        .byte 64, 176, 112
test_enemy_lane:
        .byte 0, 1, LANE_LAST
test_enemy_body:
        .byte BODY_HEAVY, BODY_LIGHT, BODY_PLACEHOLDER
