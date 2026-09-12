; lane.s — 擬似奥行き4レーンの座標系。
;
; レーン数は4に固定。ADR なしに変更してはならない（CLAUDE.md 第5条）。
;
; レーンは「番号 0..LANE_LAST」であり、画面Yへの対応は下の lane_ground_y 表が決める。
; 奥（レーン0）ほど画面上方、手前（レーン3）ほど下方。等間隔にしていないのは、
; 手前ほどレーン間隔が広く見える方が奥行きに見えるからである（表なので主が調整できる）。
;
; レーン移動は瞬間ではなく LANE_MOVE_FRAMES フレームかけて補間する。
; 補間中の中間Yは ent_y に入り、描画も奥行きの並べ替えもこの中間Yを見る。
; つまり「移動の途中で前後関係が入れ替わる」ことが自然に起きる。
.include "constants.inc"
.include "zeropage.inc"

.export lane_y_of, lane_update_all, ent_lane_move, lane_distance

.import ent_active, ent_y, ent_lane, ent_lane_from
.import ent_lane_step, ent_lane_acc, ent_lane_dy

.segment "CODE"

; A = レーン番号 → A = そのレーンの足元（接地線）の画面Y。
; X は保存する（エンティティ番号を持ったまま呼べるようにするため）。
.proc lane_y_of
        tay
        lda lane_ground_y, y
        rts
.endproc

; 2つのレーン番号の距離を返す。「同一か・隣接か・それ以上離れているか」の判定はこれで足りる。
;   入力: A = レーン1, Y = レーン2
;   出力: A = |レーン1 - レーン2|（0 = 同一, 1 = 隣接, 2以上 = それ以上）, Z = 同一なら1
; ここが engine の持ち分の境界である。「隣のレーンに攻撃が当たるか」は action-dev が決める。
.proc lane_distance
        sty tmp0
        sec
        sbc tmp0
        bcs @positive
        eor #$FF                 ; 負なら符号反転して絶対値にする
        clc
        adc #1
@positive:
        cmp #0                   ; 0 のとき Z を立て直す
        rts
.endproc

; X = エンティティ番号, A = 目標レーン番号。
; 目標レーンへ LANE_MOVE_FRAMES フレームかけて移動を開始する。
; すでに移動中のものに対して呼んでも、そのときの中間Yを出発点にして引き直すので破綻しない。
.proc ent_lane_move
        cmp #LANE_COUNT
        bcs @reject              ; 範囲外のレーン番号は無視する
        cmp ent_lane, x
        beq @reject              ; 同じレーンなら何もしない

        pha
        lda ent_lane, x
        sta ent_lane_from, x     ; 出発レーンを覚えておく（移動中の問い合わせ用）
        pla
        sta ent_lane, x          ; ent_lane は常に「目標」を指す

        jsr lane_y_of            ; A = 目標の足元Y
        sec
        sbc ent_y, x             ; 総移動量 = 目標Y - 現在Y（符号つき）
        sta ent_lane_dy, x
        lda #LANE_MOVE_FRAMES
        sta ent_lane_step, x
        lda #0
        sta ent_lane_acc, x
@reject:
        rts
.endproc

; 移動中のエンティティすべてのレーン補間を1フレーム進める。
; メインループから毎フレーム1回呼ぶこと（NMI 中に呼んではならない）。
.proc lane_update_all
        ldx #MAX_ENTITIES - 1
@loop:
        lda ent_active, x
        beq @next
        lda ent_lane_step, x
        beq @next
        jsr lane_step_one
@next:
        dex
        bpl @loop
        rts
.endproc

; X = エンティティ番号。レーン補間を1フレームぶん進める。
;
; 割り算を使わずに等速で運ぶため、ブレゼンハムと同じ累算で刻む:
;   毎フレーム acc += |dy| し、acc が LANE_MOVE_FRAMES に達するたびに Y を1動かす。
; LANE_MOVE_FRAMES フレームで合計 |dy| ちょうど動く。フレーム数を変えても割り算は要らない。
.proc lane_step_one
        lda ent_lane_dy, x
        bmi @negative
        sta tmp0                 ; tmp0 = |dy|
        lda #1
        sta tmp1                 ; tmp1 = 1フレームあたりの向き
        jmp @accumulate
@negative:
        eor #$FF
        clc
        adc #1
        sta tmp0
        lda #$FF                 ; -1 を加算で表す
        sta tmp1

@accumulate:
        lda ent_lane_acc, x
        clc
        adc tmp0
        sta ent_lane_acc, x
@reduce:
        cmp #LANE_MOVE_FRAMES
        bcc @advance_frame
        sec
        sbc #LANE_MOVE_FRAMES
        sta ent_lane_acc, x
        lda ent_y, x
        clc
        adc tmp1
        sta ent_y, x
        lda ent_lane_acc, x
        jmp @reduce

@advance_frame:
        dec ent_lane_step, x
        bne @done
        ; 到着。丸め誤差が残らないようレーンの値そのものに吸着させる。
        lda ent_lane, x
        jsr lane_y_of
        sta ent_y, x
        lda #0
        sta ent_lane_acc, x
        sta ent_lane_dy, x
@done:
        rts
.endproc

.segment "RODATA"

; レーン番号 → 足元（接地線）の画面Y。
; 間隔は 12 / 16 / 20 と手前ほど広げてある（遠近感）。値は主が調整してよい表である。
; 上端に余白を残してあるのは、上部のステータスバー（MMC3 スキャンライン IRQ で分割）と
; 背景の遠景を置くため。下端の余白は手前側の前景用。
lane_ground_y:
        .byte 148        ; レーン0（最も奥）
        .byte 160        ; レーン1
        .byte 176        ; レーン2
        .byte 196        ; レーン3（最も手前）

; レーン数が4であることを、表の長さでも縛っておく。
.assert * - lane_ground_y = LANE_COUNT, error, "lane_ground_y の項数がレーン数と違う（レーン数は4固定。CLAUDE.md 第5条）"
