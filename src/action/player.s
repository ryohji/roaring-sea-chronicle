; player.s — 操作キャラの入力処理。
;
; 責務:
;   * 入力ゲート（猶予中は実効入力をゼロにする。ADR-0006）
;   * 先行入力バッファ（ヒットストップ中も**捨てない**。規約）
;   * 移動・奥行き移動・攻撃の開始とコンボの継続
;
; **レーンは ADR-0009 で廃止した。** 奥行きは連続で、上下入力は足元Y（ent_y）を
; そのまま動かす。補間という状態を持たないので「移動中は入力を捨てる」区間も無い。
; 縦も横も、押したフレームに押したぶんだけ動く**同じ性質の操作**である
;（P1 で主が指摘した「縦方向にだけ制限がかかる理不尽」の除去）。
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

.import ent_state, ent_depth_move
.import act_step, act_face, act_hitstop, act_sub, act_dsub, act_px
.import act_t0, act_t1, act_t2, act_t3
.import act_flags, act_vel
.import act_update_actor, act_move_left, act_move_right, act_speed_mag
.import act_start_attack

.assert ACT_MOVE_SPEED <= 240, error, "ACT_MOVE_SPEED が大きすぎる（小数部の累算が8bitで巻き取る）"
.assert ACT_DEPTH_SPEED <= 240, error, "ACT_DEPTH_SPEED が大きすぎる（小数部の累算が8bitで巻き取る）"

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
        ; 猶予中（ダウン中）と猶予切れ。移動・奥行き移動・攻撃・ガードのいずれも受け付けず、
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
        ; **小走りの代償2**: 速すぎると振れない。
        ; 先行入力は**捨てない**（規約）。act_player_vel が急制動をかけ、
        ; 振れる速さまで落ちたフレームに出る。押したのに何も起きない時間は
        ; 「止まりにかかっている」姿として画面に出ている。
        jsr act_speed_mag
        cmp #ACT_ATK_VEL + 1
        bcs @move
        lda #0
        sta act_buf_atk
        lda #1
        jmp act_start_attack             ; 攻撃を始めたフレームは動かない
@move:
        jsr act_player_move
        jmp act_player_depth

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

; ==========================================================================
; 横移動 —— 歩きと小走り（移動の強弱）
; ==========================================================================
; 速さは act_vel（符号つき・1/16 ドット/フレーム）で持つ。
; **歩く速さ以下では今までとまったく同じ**である（押した瞬間に歩く速さ、
; 離した瞬間に停止）。主が実機で確認して「大きく改善した」と評価した手触りを
; 変えないために、慣性が働くのは**歩く速さを超えている間だけ**にしてある。
;
; Bボタンを押しながら方向を入れると、歩く速さから ACT_RUN_SPEED へ加速する。
; 代償は3つ（action_params.inc §2-a）:
;   1 向きを変えにくい … 速いうちは逆を押しても滑るだけ（向きは変わらない）
;   2 攻撃に移れない   … 振れる速さまで落ちるのを待つ（先行入力は保持する）
;   3 奥行きが鈍る     … 走っている間は上下が遅い（act_player_depth）
;
; X = ENT_PLAYER。
.proc act_player_move
        jsr act_player_vel               ; 入力 → act_vel
        lda act_vel, x
        bne @moving
        sta act_sub, x                   ; 止まったら小数部を捨てる（歩き出しを揃える）
        rts
@moving:
        bmi @left
        sta act_t0                       ; 速さの大きさ
        lda #0                           ; 進む向き 0 = 右
        beq @accum                       ; 必ず分岐（A = 0）
@left:
        eor #$FF
        clc
        adc #1                           ; 絶対値
        sta act_t0
        lda #1                           ; 進む向き 1 = 左
@accum:
        sta act_t1
        lda act_sub, x
        clc
        adc act_t0
        pha
        and #$0F
        sta act_sub, x
        pla
        lsr a
        lsr a
        lsr a
        lsr a
        sta act_px                       ; 今フレームの整数ドット数
        beq @done                        ; 1ドットに満たない
        lda act_t1                       ; ここで読む。act_move_* は act_t0..t3 を壊す
        bne @go_left
        jmp act_move_right
@go_left:
        jmp act_move_left
@done:
        rts
.endproc

; X = ENT_PLAYER。入力から act_vel を作る。act_t2 / act_t3 を作業に使う。
;
;   act_t2 = 望む速さ（0 = 方向入力なし）
;   act_t3 = 望む向き（0 = 右 / 1 = 左）
;
; 向き（act_face）を変えるのは「歩く速さ以下まで落ちてから」である。
; これが代償1 の実体であり、**速いほど小回りが利かない**。
.proc act_player_vel
        ; --- 望む向きと望む速さ ---
        lda #0
        sta act_t2
        sta act_t3
        lda act_pad
        and #PAD_LEFT
        beq @try_right
        lda #1
        sta act_t3                       ; 左右同時押しは左を採る（今までどおり）
        jmp @want
@try_right:
        lda act_pad
        and #PAD_RIGHT
        beq @have_want                   ; 方向入力なし。望む速さは 0 のまま
@want:
        lda #ACT_MOVE_SPEED
.if ::ACT_RUN_ENABLE
        ldy act_buf_atk
        bne @store_want                  ; 振ろうとしている間は走らない（代償2）
        lda act_pad
        and #PAD_B
        beq @walk
        lda #ACT_RUN_SPEED
        bne @store_want                  ; 必ず分岐
@walk:
        lda #ACT_MOVE_SPEED
.endif
@store_want:
        sta act_t2
@have_want:

        ; --- いまの速さと向き ---
        jsr act_speed_mag
        sta act_t0                       ; いまの速さ
        bne @rolling
        ; 止まっている。押した向きへ即座に歩き出す（今までと同じ手触り）。
        ; **走り出しは歩く速さから**。いきなり最高速にすると「小走りになる」過程が消える。
        lda act_t2
        beq @stopped
        ldy act_t3
        sty act_t1                       ; 止まっていたので向きは自由に決まる
        cmp #ACT_MOVE_SPEED
        bcc @to_apply
        lda #ACT_MOVE_SPEED
        jmp @apply
@stopped:
        lda #0
        sta act_vel, x
        rts
@to_apply:
        jmp @apply                       ; 分岐が届かないので踏み台を1つ置く

@rolling:
        lda act_vel, x
        bpl @cur_right
        lda #1
        bne @cur_have                    ; 必ず分岐
@cur_right:
        lda #0
@cur_have:
        sta act_t1                       ; いま進んでいる向き
        lda act_t2
        beq @brake_free                  ; 方向入力なし → 自然減速
        lda act_t3
        cmp act_t1
        beq @same_dir
        ; --- 逆を押した。速いうちは向きを変えず、滑りながら減速する（代償1）---
        lda act_t0
        cmp #ACT_TURN_VEL + 1
        bcc @turn_now
        lda #ACT_RUN_TURN_BRAKE
        jmp @brake
@turn_now:
        lda act_t3
        sta act_t1                       ; 落ち切った。ここで向きが変わる
@same_dir:
        lda act_t3
        sta act_t1
        lda act_t0
        cmp #ACT_MOVE_SPEED
        bcs @above_walk
        lda act_t2                       ; 歩く速さ未満は即時（今までと同じ）
        cmp #ACT_MOVE_SPEED
        bcc @to_apply
        lda #ACT_MOVE_SPEED
        jmp @apply
@above_walk:
        lda act_t2
        cmp act_t0
        beq @keep
        bcc @slow_down
        lda act_t0                       ; 加速（小走りへ）
        clc
        adc #ACT_RUN_ACCEL
        cmp act_t2
        bcc @to_apply
        lda act_t2                       ; 最高速に届いた
        jmp @apply
@slow_down:
        ; Bを離した／振ろうとしている。歩く速さまで慣性で落ちる。
        ; 振ろうとしているときだけ急制動にするのは、**先行入力の寿命に間に合わせる**
        ; ためである（ACT_BUF_FRAMES より短いフレーム数で振れる速さまで落ちること）。
        lda act_buf_atk
        beq @coast_down
        lda #ACT_ATK_BRAKE
        jmp @brake
@coast_down:
        lda #ACT_RUN_BRAKE
        jmp @brake
@keep:
        lda act_t0
        jmp @apply

@brake_free:
        ; 方向入力なし。歩く速さ以下なら即停止（今までと同じ）、
        ; 超えていれば滑ってから止まる（代償3の裏返し。**止まるのに時間がかかる**）。
        lda act_t0
        cmp #ACT_MOVE_SPEED + 1
        bcc @halt
        lda act_buf_atk
        beq @coast
        lda #ACT_ATK_BRAKE               ; 振ろうとしている。急いで止まる（代償2）
        jmp @brake
@coast:
        lda #ACT_RUN_BRAKE
        jmp @brake
@halt:
        lda #0
        sta act_vel, x
        sta act_sub, x
        rts

        ; A = 減速量。act_t0 から引いて act_t1 の向きのまま入れ直す。
@brake:
        sta act_t3
        lda act_t0
        sec
        sbc act_t3
        bcs @apply
        lda #0
        ; act_apply へ

        ; A = 新しい速さ（大きさ）, act_t1 = 進む向き。符号つきにして入れる。
        ;
        ; **絵の向き（act_face）は「進んでいる向き」である**（望む向きではない）。
        ; act_t1 が望む向きに変わるのは ACT_TURN_VEL 以下まで落ちたときだけなので、
        ; 速いうちに逆を押しても背を向けない ＝ 代償1 がそのまま絵に出る。
@apply:
        sta act_t0
        beq @zero
        lda act_t1
        sta act_face, x
        lda act_t0
        ldy act_t1
        beq @store
        eor #$FF
        clc
        adc #1                           ; 左向きは負
@store:
        sta act_vel, x
        rts
@zero:
        sta act_vel, x
        sta act_sub, x
        rts
.endproc

; X = ENT_PLAYER。上下で奥行き（足元Y）を動かす。ADR-0009。
;
; 横移動（act_player_move）と**まったく同じ形**である。違うのは速さ
;（ACT_DEPTH_SPEED）と、クランプを engine の ent_depth_move が持つことだけ。
; レーン時代にあった3つの制限（4段階の量子化・8フレームの補間・補間中の入力破棄）は
; いずれも無い。押しっぱなしで動き続け、離せばその場で止まる。
;
; 上下同時押しは上（奥）を採る。左右同時押しで左を採るのと同じ扱いである。
.proc act_player_depth
        lda act_pad
        and #(PAD_UP | PAD_DOWN)
        bne @moving
        lda #0
        sta act_dsub, x                  ; 止まったら小数部を捨てる（歩き出しを揃える）
        rts
@moving:
        ; **小走りの代償3**: 走っている間は奥行きが鈍る。
        ; 横に速い代わりに、縦の回避は歩いた方が速い。
        ; 予備動作を見てから奥へ避けるなら、走るのをやめる判断が要る。
        jsr act_speed_mag
        cmp #ACT_MOVE_SPEED + 1
        lda #ACT_DEPTH_SPEED
        bcc :+
        lda #ACT_RUN_DEPTH_SPEED
:       clc
        adc act_dsub, x
        pha
        and #$0F
        sta act_dsub, x
        pla
        lsr a
        lsr a
        lsr a
        lsr a
        beq @done                        ; 今フレームは1ドットに満たない
        sta act_px                       ; 今フレームの整数ドット数（横移動はもう済んでいる）
        lda act_flags, x
        ora #ACT_F_MOVED                 ; 歩きの絵にする（姿勢を選ぶのは actor.s）
        sta act_flags, x
        lda act_pad
        and #PAD_UP
        beq @near
        lda act_px                       ; 奥へ = 足元Yを小さく = 負の移動量
        eor #$FF
        clc
        adc #1
        jmp ent_depth_move               ; 歩ける帯のクランプは engine が持つ
@near:
        lda act_px                       ; 手前へ = 足元Yを大きく
        jmp ent_depth_move
@done:
        rts
.endproc
