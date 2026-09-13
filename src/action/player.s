; player.s — 操作キャラの入力処理。
;
; 責務:
;   * 入力ゲート（猶予中は実効入力をゼロにする。ADR-0006）
;   * 先行入力バッファ（ヒットストップ中も**捨てない**。規約）
;   * 移動・レーン移動・攻撃の開始とコンボの継続
;
; 入力に関する規約が2つあり、**扱いが逆である**ことに注意:
;   ヒットストップ中 … 入力を捨てない（先行入力を保持する）
;   猶予中（ダウン中）… 先行入力を保持しない。復帰時は「何も押していない」から始める
; 後者は ADR-0006 の決定3であり、前者の**唯一の例外**である。
; 猶予中の入力を保持すると、蘇生で復帰した瞬間に攻撃や移動が暴発する。
;
; 押しっぱなしで復帰した場合も押し直すまで効かせないため、
; 「無視ビット（act_pad_ignore）」を持つ。復帰時にそのとき押されていたビットを立て、
; 毎フレーム「離されたビット」を落としていく。実効入力は pad & ~ignore。
.include "constants.inc"
.include "zeropage.inc"
.include "action.inc"
.include "action_params.inc"

.export act_pad, act_pad_new, act_pad_ignore, act_buf_atk
.export act_input_gate, act_input_buffer, act_input_drop
.export action_update_player

.import ent_state, ent_lane, ent_lane_step, ent_lane_move
.import act_step, act_face, act_hitstop, act_sub, act_px, act_t0
.import act_update_actor, act_move_left, act_move_right
.import act_start_attack

.assert ACT_MOVE_SPEED <= 240, error, "ACT_MOVE_SPEED が大きすぎる（小数部の累算が8bitで巻き取る）"

.segment "BSS"

act_pad:        .res 1       ; 実効のパッド状態（猶予・復帰の無視ビットを適用済み）
act_pad_new:    .res 1       ; 実効の「押した瞬間」
act_pad_ignore: .res 1       ; 離して押し直すまで無効にするビット
act_buf_atk:    .res 1       ; 攻撃の先行入力の残りフレーム

.segment "CODE"

; ==========================================================================
; 入力ゲート
; ==========================================================================
; 毎フレーム read_pad の後、他のどの処理より先に呼ぶ。
.proc act_input_gate
        ldx #ENT_PLAYER
        lda ent_state, x
        cmp #ACT_ST_DOWN
        bcc @alive
        ; 猶予中（ダウン中）と猶予切れ。移動・レーン移動・攻撃・ガードのいずれも受け付けず、
        ; 先行入力も残さない（ADR-0006）。ゲームの中断のようなメタ操作は
        ; ここを通らない別経路で拾うこと（ポーズ仕様が決まるまでは未実装）。
        jmp act_input_drop
@alive:
        lda act_pad_ignore
        and pad_state                    ; 離されたビットは無視をやめる
        sta act_pad_ignore
        eor #$FF
        sta act_t0
        and pad_state
        sta act_pad
        lda act_t0
        and pad_pressed
        sta act_pad_new
        rts
.endproc

; 先行入力の受付と寿命。**状態を見ない**のが肝で、ヒットストップ中も硬直中も溜まる。
.proc act_input_buffer
        lda act_pad_new
        and #PAD_A
        beq @age
        lda #ACT_BUF_FRAMES
        sta act_buf_atk
        rts
@age:
        lda act_buf_atk
        beq @done
        dec act_buf_atk
@done:
        rts
.endproc

; 実効入力と先行入力を空にし、いま押されているボタンを「押し直すまで無効」にする。
; ダウンに入った時点と復帰の時点の両方で呼ぶ（ADR-0006 の帰結節）。
; X は壊さない。
.proc act_input_drop
        lda #0
        sta act_pad
        sta act_pad_new
        sta act_buf_atk
        lda pad_state                    ; 押しっぱなしのビットは押し直すまで効かせない
        sta act_pad_ignore
        rts
.endproc

; ==========================================================================
; 操作キャラの1フレーム
; ==========================================================================
.proc action_update_player
        ldx #ENT_PLAYER
        jsr act_update_actor             ; 時間経過（ヒットストップ・無敵・ノックバック・状態）
        ldx #ENT_PLAYER
        lda act_hitstop, x
        bne @done                        ; ヒットストップ中は操作を受け付けない（入力は溜まる）
        lda ent_state, x
        beq @idle                        ; ACT_ST_IDLE
        cmp #ACT_ST_ATK_RECOVER
        beq @combo
@done:
        rts

        ; 立ち・歩き。攻撃の先行入力があれば初段を出す
@idle:
        lda act_buf_atk
        beq @move
        lda #0
        sta act_buf_atk
        lda #1
        jmp act_start_attack             ; 攻撃を始めたフレームは動かない
@move:
        jsr act_player_move
        jmp act_player_lane

        ; 硬直中。この相が次の段の受付時間である（長さは atk_recover）
@combo:
        lda act_step, x
        cmp #ACT_COMBO_MAX
        bcs @done                        ; 最終段。これ以上は繋がない
        lda act_buf_atk
        beq @done
        lda #0
        sta act_buf_atk
        lda act_step, x
        clc
        adc #1
        jmp act_start_attack
.endproc

; X = ENT_PLAYER。左右移動。速さは 1/16 ドット単位で刻む（小数部は act_sub）。
.proc act_player_move
        lda act_pad
        and #(PAD_LEFT | PAD_RIGHT)
        bne @moving
        lda #0
        sta act_sub, x                   ; 止まったら小数部を捨てる（歩き出しを揃える）
        rts
@moving:
        lda act_sub, x
        clc
        adc #ACT_MOVE_SPEED
        pha
        and #$0F
        sta act_sub, x
        pla
        lsr a
        lsr a
        lsr a
        lsr a
        sta act_px                       ; 今フレームの整数ドット数
        lda act_pad
        and #PAD_LEFT
        beq @right
        lda #1
        sta act_face, x                  ; 左右同時押しは左を採る
        lda act_px
        beq @done
        jmp act_move_left
@right:
        lda #0
        sta act_face, x
        lda act_px
        beq @done
        jmp act_move_right
@done:
        rts
.endproc

; X = ENT_PLAYER。上下でレーンを1つ移す。補間は engine（lane.s）が持つ。
.proc act_player_lane
        lda ent_lane_step, x
        bne @done                        ; 補間中は次のレーン移動を受け付けない
        lda act_pad_new
        and #PAD_UP
        beq @down_btn
        lda ent_lane, x
        beq @done                        ; レーン0 より奥は無い
        sec
        sbc #1
        jmp ent_lane_move
@down_btn:
        lda act_pad_new
        and #PAD_DOWN
        beq @done
        lda ent_lane, x
        cmp #LANE_LAST
        bcs @done                        ; 最も手前のレーンより先は無い
        clc
        adc #1
        jmp ent_lane_move
@done:
        rts
.endproc
