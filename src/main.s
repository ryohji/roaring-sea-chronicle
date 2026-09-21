; main.s — リセットハンドラとメインループ。
.include "constants.inc"
.include "zeropage.inc"
.include "debug.inc"

.export reset_handler
.if ::DBG_ENABLE
.export dbg_slow, dbg_ally      ; 読むのは ai-dev（dbg_ally）。engine は回すだけ
.endif
.import init_system, read_pad
.import ent_clear_all, ent_activate
.import ent_x_lo, ent_x_hi, ent_y
.import ent_state, ent_class, ent_body, ent_tile, ent_attr
.import oam_build, cam_update
.import fx_clear_all, fx_update
.import action_init, action_update
.import ai_init, ai_update
.import ent_ai
.import stage_w_lo, stage_w_hi

; --- P1 の動作確認用シーンのパラメータ ---
; ここにあるのは engine の経路（入力 → エンティティ → OAM → 画面）が
; 繋がっていることを確かめるための仮置きである。ステージの中身は P3 で
; stage-author が data/ から読む形に置き換える。ここに固有名詞を増やしてはならない。
PLAYER_HOME_X    = 120
; 足元Y の初期値。歩ける帯（既定 DEPTH_Y_MIN_DEFAULT..DEPTH_Y_MAX_DEFAULT）の
; まんなかに置く。レーン番号ではない（ADR-0009）。
PLAYER_HOME_Y    = (DEPTH_Y_MIN_DEFAULT + DEPTH_Y_MAX_DEFAULT) / 2
PLAYER_SPEED     = 1          ; 手触りの値ではない。動きが見えればよい
PLAYER_EDGE_MARGIN = 16       ; ステージ右端に残す余白（体1つぶん）
; 仮CHR のタイル割当は src/constants.inc の SPR_ROLE_* が持つ
; （tools/mk_placeholder_chr.py と対応）。役割ごとに輪郭の違う絵を割り当てる。
; 色だけで見分けさせない。ent_activate が先頭タイルを ent_tile0 に覚えるので、
; 以降の姿勢の切り替えは ent_set_pose で済む（姿勢を選ぶのは action-dev / ai-dev）。
TILE_PLAYER = SPR_ROLE_PLAYER
TILE_ALLY   = SPR_ROLE_ALLY
TILE_ENEMY  = SPR_ROLE_ENEMY
TEST_ATTR_PLAYER = 0          ; スプライトパレット0
TEST_ATTR_ENEMY  = 1          ; スプライトパレット1
TEST_ATTR_ALLY   = 2          ; スプライトパレット2（仲間）
ALLY_HOME_X      = PLAYER_HOME_X - 24

; data/ai_params.tsv の id。仮シーンの配置なのでここに置く（本番は data から入る）
AI_PROFILE_ALLY  = 0          ; ally_versatile
AI_PROFILE_ENEMY = 1          ; enemy_melee
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
        jsr ai_init             ; 自律行動の初期化（action_init の後）
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
.if ::DBG_ENABLE
        ; --- スローの門（デバッグ。既定 dbg_slow = 0 では素通りする）---
        ; ここが NMI の直後・read_pad の**手前**にあることが肝である。
        ; 飛ばすフレームで read_pad を呼ぶと、そのフレームで pad_pressed
        ;（押した瞬間）が立って誰にも読まれずに消え、攻撃の入力が落ちる。
        ; 飛ばすのは論理更新まるごとであって、NMI ではない（DMA・転送・
        ; スクロールは等速のまま走り続けるので、画面は止まらない）。
        lda dbg_skip
        beq @logic
        dec dbg_skip
        jmp @frame
@logic:
        lda dbg_slow            ; この論理フレームのぶんを積み直す
        sta dbg_skip
.endif
        jsr read_pad
.if ::DBG_ENABLE
        jsr dbg_buttons         ; START / SELECT でモードを回す（入力を読んだ直後）
.endif
        jsr fx_update           ; 短命の効果の寿命。**action / ai が出す前に**減らす
        jsr action_update       ; 操作・攻撃・ダウンは src/action/ が持つ
        jsr ai_update           ; 自律仲間と敵の思考・行動は src/ai/ が持つ
        jsr cam_update          ; カメラ追従 + 現れた列を転送キューへ
        jsr oam_build
        jmp @frame
.endproc

.if ::DBG_ENABLE
; --- デバッグのモード切り替え ---
; **押した瞬間**（pad_pressed）だけで回す。押しっぱなしで走らせない。
; ここは論理フレームでしか呼ばれないので、飛ばしているフレームの入力は見ない。
.proc dbg_buttons
        lda pad_pressed
        and #PAD_START
        beq @select
        lda dbg_slow
        jsr dbg_bump
        sta dbg_slow
        sta dbg_skip            ; 押した瞬間から新しい速さで刻む
@select:
        lda pad_pressed
        and #PAD_SELECT
        beq @done
        lda dbg_ally            ; 意味を解釈するのは src/ai/。ここは値を回すだけ
        jsr dbg_bump
        sta dbg_ally
@done:
        rts
.endproc

; A = いまの値 → A = 次の値（0 → 1 → 2 → 0）
.proc dbg_bump
        clc
        adc #1
        cmp #DBG_MODE_COUNT
        bcc :+
        lda #0
:       rts
.endproc

.segment "BSS"
; 実体はここ（BSS）。ゼロページは使わない（理由は src/debug.inc）。
dbg_slow:   .res 1              ; 論理更新1回につき飛ばすフレーム数（0 = 等速）
dbg_ally:   .res 1              ; 仲間の確認モード（読むのは src/ai/）
dbg_skip:   .res 1              ; 残りの飛ばすフレーム数（engine 内部。公開しない）

.segment "CODE"
.endif

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
; 操作キャラ1体と、敵3体を別々の奥行き（足元Y）に置く。
; 敵の思考（AI）は ai-dev の範囲なので書かない。ここでは「置くだけ」である。
.proc scene_test_init
        jsr ent_clear_all
        jsr fx_clear_all

        ldx #ENT_PLAYER
        lda #<PLAYER_HOME_X
        sta ent_x_lo, x
        lda #>PLAYER_HOME_X
        sta ent_x_hi, x
        lda #PLAYER_HOME_Y
        sta ent_y, x
        lda #SPR_CLASS_PLAYER
        sta ent_class, x
        lda #BODY_PLACEHOLDER
        sta ent_body, x
        lda #TILE_PLAYER
        sta ent_tile, x
        lda #TEST_ATTR_PLAYER
        sta ent_attr, x
        lda #ENT_ST_IDLE
        sta ent_state, x
        jsr ent_activate

        ; --- 自律仲間1体（万能型）。P1 の垂直スライスは操作1＋自律1で見る ---
        ldx #ENT_ALLY_FIRST
        lda #<ALLY_HOME_X
        sta ent_x_lo, x
        lda #>ALLY_HOME_X
        sta ent_x_hi, x
        lda #PLAYER_HOME_Y
        sta ent_y, x
        lda #SPR_CLASS_FAR_ENE
        sta ent_class, x
        lda #BODY_PLACEHOLDER
        sta ent_body, x
        lda #AI_PROFILE_ALLY
        sta ent_ai, x
        lda #TILE_ALLY
        sta ent_tile, x
        lda #TEST_ATTR_ALLY
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
        lda test_enemy_y, y
        sta ent_y, x
        lda test_enemy_body, y
        sta ent_body, x
        lda #SPR_CLASS_FAR_ENE
        sta ent_class, x
        lda #AI_PROFILE_ENEMY
        sta ent_ai, x
        lda #TILE_ENEMY
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
; 「入力 → エンティティ → OAM → 画面」の経路の確認である。
;   左右: ワールドX を動かす（16bit）
;   上下: 足元Y を連続に動かす（歩ける帯でクランプする。補間はしない。ADR-0009）

.segment "RODATA"

; 確認用の敵の配置。奥行きごとの前後関係と体格型ごとのタイル数が
; 画面で見えるように、別の足元Y・別体格で置いてある。
; 帯の両端（DEPTH_Y_MIN_DEFAULT / DEPTH_Y_MAX_DEFAULT）を含めてあるのは、
; 歩ける帯の広さが画面で分かるようにするためである。
test_enemy_x:
        .byte 64, 176, 112
test_enemy_y:
        .byte DEPTH_Y_MIN_DEFAULT, PLAYER_HOME_Y, DEPTH_Y_MAX_DEFAULT
test_enemy_body:
        .byte BODY_HEAVY, BODY_LIGHT, BODY_PLACEHOLDER
