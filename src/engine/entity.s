; entity.s — エンティティテーブル（Structure of Arrays）。
;
; 属性ごとに1本の配列を持つ。構造体の配列にすると `lda (ptr),y` か
; 「添字×構造体長」の掛け算が要るが、SoA なら `lda ent_x_lo, x` の1命令で引ける。
; 6502 ではこの差がそのまま毎フレームのサイクル数になる。
;
; 添字の区画割り（ENT_PLAYER / ENT_ALLY_FIRST / ENT_ENEMY_FIRST / ENT_FREE_FIRST）と
; 上限 MAX_ENTITIES は src/constants.inc にある。ここにマジックナンバーを書かない。
;
; ここに持ってよいのは「座標・状態・表示のための箱」だけである。
; キャラ名・話番号・シナリオ固有の情報を持たせてはならない（CLAUDE.md 第1条）。
; 体格型 (ent_body) と AI 型 (ent_ai) は data/ 側の ID を入れる箱にとどめる。
.include "constants.inc"
.include "zeropage.inc"

.export ent_active, ent_x_lo, ent_x_hi, ent_y
.export ent_lane, ent_lane_from, ent_lane_step, ent_lane_acc, ent_lane_dy
.export ent_state, ent_class, ent_body, ent_ai, ent_tile, ent_attr
.export ent_clear_all, ent_activate, ent_kill, ent_find_free

.import lane_y_of

.segment "BSS"

ent_active:     .res MAX_ENTITIES   ; ENT_INACTIVE / ENT_ACTIVE
ent_x_lo:       .res MAX_ENTITIES   ; ワールドX 下位（16bit。ステージは横に長い）
ent_x_hi:       .res MAX_ENTITIES   ; ワールドX 上位
ent_y:          .res MAX_ENTITIES   ; 足元の画面Y。レーン移動中は補間された中間値が入る
ent_lane:       .res MAX_ENTITIES   ; レーン番号 0..LANE_LAST（移動中は「目標」レーン）
ent_lane_from:  .res MAX_ENTITIES   ; 移動中の出発レーン（ent_lane_step が 0 でない間だけ有効）
ent_lane_step:  .res MAX_ENTITIES   ; レーン補間の残りフレーム数。0 なら静止
ent_lane_acc:   .res MAX_ENTITIES   ; レーン補間の累算器（lane.s の内部用）
ent_lane_dy:    .res MAX_ENTITIES   ; レーン補間の総Y移動量（符号つき。lane.s の内部用）
ent_state:      .res MAX_ENTITIES   ; 状態 ID。語彙を決めるのは action-dev
ent_class:      .res MAX_ENTITIES   ; SPR_CLASS_*（OAM 並べ替えの重み。ADR-0002）
ent_body:       .res MAX_ENTITIES   ; BODY_*（描画タイル数が変わる）
ent_ai:         .res MAX_ENTITIES   ; AI 型 ID の箱。中身を決めるのは ai-dev / data
ent_tile:       .res MAX_ENTITIES   ; 描画タイルの基準番号
ent_attr:       .res MAX_ENTITIES   ; OAM 属性（パレット・反転・背面）

.segment "CODE"

; テーブル全体を空にする。シーン開始時に呼ぶ。
.proc ent_clear_all
        lda #ENT_INACTIVE
        ldx #MAX_ENTITIES - 1
@loop:
        sta ent_active, x
        sta ent_lane_step, x
        sta ent_lane_acc, x
        sta ent_lane_dy, x
        dex
        bpl @loop
        rts
.endproc

; X = エンティティ番号。呼ぶ前に ent_x_lo/hi, ent_lane, ent_class, ent_body,
; ent_tile, ent_attr を詰めておくこと（SoA なので呼び出し側が直接書くのが速い）。
; ここでは「有効化」と、レーンから足元Yを決めることだけを行う。
.proc ent_activate
        lda #ENT_ACTIVE
        sta ent_active, x
        lda #0
        sta ent_lane_step, x            ; 補間中でない
        sta ent_lane_acc, x
        sta ent_lane_dy, x
        lda ent_lane, x
        sta ent_lane_from, x
        jsr lane_y_of                   ; A = そのレーンの足元Y（X は壊さない）
        sta ent_y, x
        rts
.endproc

; X = エンティティ番号。
.proc ent_kill
        lda #ENT_INACTIVE
        sta ent_active, x
        rts
.endproc

; 空きスロットを区画の中から探す。
;   入力: X = 先頭の添字, Y = 個数（例: ldx #ENT_FREE_FIRST / ldy #ENT_FREE_COUNT）
;   出力: キャリークリア = 見つかった（X が添字）/ キャリーセット = 空きなし
.proc ent_find_free
@loop:
        lda ent_active, x
        beq @found
        inx
        dey
        bne @loop
        sec
        rts
@found:
        clc
        rts
.endproc
