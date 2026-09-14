; ai.s — 自律行動の意思決定ループ。**これ1本しか無い。**
;
; 責務:
;   * 状況評価（ai_sense）      … 場の状態を1フレームに1回まとめる
;   * 目標選択（ai_think_for）  … 標的を選び、立ち位置と目標を決める（分散実行）
;   * 行動  （ai_act_one）      … 決まった立ち位置へ歩き、レーンを合わせ、届けば振る
;
; **型ごとの分岐をこのファイルに書いてはならない。**
; 型の違いは data/ai_params.tsv の行（速さ・間合い・攻撃性・追従距離・
; レーン移動の積極性・回収の優先度）だけで出す。
; 「この型のときだけ〜する」と書きたくなったら、それは列が足りていないという意味である。
; 敵も同じループ・同じ表で動く（敵専用の思考を別に書かない）。
;
; 何をここに書かないか:
;   * ダメージ・ヒットストップ・ノックバック・ダウン … src/action/（act_start_attack →
;     act_hit_scan → act_damage が全部持っている。再実装しない）
;   * 矩形の交差・レーン距離・レーン補間 … src/engine/（プリミティブを使う側である）
;   * キャラ名・話番号・家系名 … data/ 側（CLAUDE.md 第1条）
;
; ゼロページの tmp0〜tmp3 は engine の呼び出し（lane_distance など）をまたいで
; 保たれない。AI の状態も作業変数も、このファイルの BSS に置く。
.include "constants.inc"
.include "zeropage.inc"
; action の語彙（ACT_ST_*）。ca65 の探索パスは -I src だけなので相対で指す。
; ここで欲しいのは「倒れているか」の境界 ACT_ST_DOWN であり、
; AI が状態の数値を自前で持つと action-dev が語彙を変えた日に黙って壊れる。
.include "../action/action.inc"
.include "ai.inc"
.include "ai_params.inc"

.export ai_init, ai_init_entity, ai_update
.export ai_goal, ai_target, ai_gap, ai_flags, ai_timer, ai_lturn
.export ai_sit, ai_cursor

.import ent_active, ent_x_lo, ent_x_hi, ent_lane, ent_lane_step, ent_state, ent_ai
.import ent_lane_move, lane_distance
.import act_role, act_hitstop, act_face, act_px
.import act_move_left, act_move_right, act_start_attack
.import ai_p_speed, ai_p_hold_x, ai_p_reach_x, ai_p_aggr_x
.import ai_p_follow_x, ai_p_leash_x, ai_p_atk_gap, ai_p_lane_hold
.import ai_default_profile

; 自律行動の対象になる区画（仲間 + 敵）。操作キャラ(0)と共用枠は含まない。
AI_FIRST = ENT_ALLY_FIRST
AI_END   = ENT_FREE_FIRST

; ai_dir（立ち位置を逃がす向き）に入れる値。0 = 左 / 1 = 右 /
; AI_DIR_NEAREST = 向きにこだわらない（いま立ち位置がある側＝近い方の外へ出す）。
; 調整値ではなく符号なので ai_params.inc ではなくここに置く。
AI_DIR_NEAREST = $FF

.segment "BSS"

; --- エンティティごとの状態（engine の SoA と同じ添字で引く）---
ai_goal:    .res MAX_ENTITIES   ; AI_GOAL_*
ai_target:  .res MAX_ENTITIES   ; 立ち位置の基準にする相手。AI_NONE なら無し
ai_gap:     .res MAX_ENTITIES   ; 標的から取りたい横距離（hold_x に分散ぶんを足した値）
ai_flags:   .res MAX_ENTITIES   ; AI_F_*
ai_timer:   .res MAX_ENTITIES   ; 次に振れるまでのフレーム数
ai_lturn:   .res MAX_ENTITIES   ; 次にレーンを移れるまでのフレーム数
ai_sub:     .res MAX_ENTITIES   ; 横移動の小数部（1/16 ドット）

; --- 場の状態 ---
ai_sit:     .res 1              ; AI_SIT_*（状況評価の結果）
ai_cursor:  .res 1              ; 次に考え直すエンティティ番号（思考の分散実行）
ai_rethink: .res 1              ; 今フレームに残っている「臨時の考え直し」の回数

; --- 作業変数（engine の呼び出しをまたぐので tmp には置けない）---
ai_prof:    .res 1              ; いま処理している者のプロファイル行
ai_t0:      .res 1              ; 16bit 計算の作業（ai_dist16 が壊す）
ai_t1:      .res 1
ai_t2:      .res 1              ; ai_dist16 をまたいで保つ値
ai_t3:      .res 1
ai_t4:      .res 1              ; ai_post_gap の作業
ai_dir:     .res 1              ; 立ち位置を逃がす向き（0:左 / 1:右）
ai_dist:    .res 1              ; ai_dist16 の結果（横距離・255 で頭打ち）
ai_dsign:   .res 1              ; 同（0 = 相手は自分の右 / 1 = 左）。act_face と同じ意味
ai_cand:    .res 1              ; 標的選びの走査位置
ai_scan_end: .res 1             ; 同、終端（この番号は含まない）
ai_best:    .res 1              ; 同、いちばん良かった相手
ai_bestsc:  .res 1              ; 同、その重みつき距離
ai_px:      .res 1              ; 今フレームに進めるドット数

.segment "CODE"

; ==========================================================================
; 初期化
; ==========================================================================

; シーン開始時に1回呼ぶ。**エンティティを配置し終えた後**であること
; （ent_active と ent_ai を見る）。action_init の後でよい。
.proc ai_init
        lda #0
        sta ai_sit
        lda #AI_FIRST
        sta ai_cursor
        ldx #MAX_ENTITIES - 1
@clear:
        jsr ai_clear_one
        dex
        bpl @clear

        ldx #AI_FIRST
@prof:
        jsr ai_init_entity
        inx
        cpx #AI_END
        bcc @prof
        rts
.endproc

; X = エンティティ番号。途中で湧いた1体ぶんの初期化（P3 の waves 用）。
; 配置直後に呼べばよい。X は保存して返る。
.proc ai_init_entity
        jsr ai_clear_one
        lda ent_active, x
        beq @done
        lda ent_ai, x
        cmp #AI_PROFILE_COUNT
        bcc @done                ; データ側（roster / waves）が決めている。触らない
        ; 詰め忘れ（ent_clear_all は ent_ai を消さない）。立場ごとの既定値で埋める。
        jsr act_role
        tay
        lda ai_default_profile, y
        sta ent_ai, x
@done:
        rts
.endproc

; X = エンティティ番号。AI 側の状態だけを初期状態に戻す。
.proc ai_clear_one
        lda #AI_GOAL_IDLE
        sta ai_goal, x
        lda #AI_NONE
        sta ai_target, x
        lda #0
        sta ai_gap, x
        sta ai_flags, x
        sta ai_timer, x
        sta ai_lturn, x
        sta ai_sub, x
        rts
.endproc

; ==========================================================================
; 1フレームの入口
; ==========================================================================
; main.s から毎フレーム1回、action_update の後・lane_update_all の前に呼ぶ。
;   action_update の後  … 被弾・ダウンの結果を見てから考えるため
;   lane_update_all の前 … この場で頼んだレーン移動が同じフレームから動き出すため
;
; 費用の内訳（1フレームあたり）:
;   状況評価  1回（定数時間）
;   思考      AI_THINK_PER_FRAME 体ぶん（重い。標的の走査を含む）
;             ＋ 標的を失った者の臨時の考え直し AI_RETHINK_PER_FRAME 体ぶんまで
;   行動      自律枠の生存者ぶん（軽い。歩く・レーンを合わせる・振る）
.proc ai_update
        lda #AI_RETHINK_PER_FRAME
        sta ai_rethink
        jsr ai_sense
        jsr ai_think_slice

        ldx #AI_FIRST
@loop:
        lda ent_active, x
        beq @next
        jsr ai_act_one
@next:
        inx
        cpx #AI_END
        bcc @loop
        rts
.endproc

; ==========================================================================
; 状況評価
; ==========================================================================
; 場の状態を1フレームに1回だけまとめる（各自が毎回調べ直さない）。
;
; **ADR-0006 の口**: 操作キャラが猶予中（ダウン中）のあいだ、プレイヤーの
; キャラクター操作は一切効かない。事態を変えられるのは自律仲間だけである。
; P1 ではこのビットで行動を変えない（蘇生アイテムは P2 / ADR-0007、
; 「助けに行く」行動も蘇生手段が決まるまで実装しない）。認識だけしておく。
;
; 判定は ACT_ST_DOWN **ちょうど**であって「DOWN 以上」ではない。
; ACT_ST_OUT（猶予切れ）は ADR-0005 では シナリオ失敗が確定した状態で、
; もう蘇生は効かない。ここを「DOWN 以上」にしておくと、P2 で
; 「このビットが立っていたら蘇生アイテムを使う」と書いた瞬間に、
; 失敗確定後に共有アイテムを1個捨てる経路ができる（次のシナリオに響く）。
; 猶予切れ用のビットは足していない。読む者がまだ居ないビットを先に作ると、
; 「立っているが誰も見ない」状態が P2 まで検証されないまま残るからである。
.proc ai_sense
        lda #0
        sta ai_sit
        ldx #ENT_PLAYER
        lda ent_active, x
        beq @done
        lda ent_state, x
        cmp #ACT_ST_DOWN         ; **猶予中ちょうど**。ACT_ST_OUT（猶予切れ）は含めない
        bne @done
        lda #AI_SIT_LEADER_DOWN
        sta ai_sit
@done:
        rts
.endproc

; ==========================================================================
; 思考の分散実行
; ==========================================================================
; 毎フレーム全員ぶんの思考を回さない（ai-dev の規約）。
; 1フレームに AI_THINK_PER_FRAME 体だけ考え直す。順番は番号の巡回なので、
; 体数が増えても1フレームの費用は変わらない（考え直す間隔だけが延びる）。
.proc ai_think_slice
        ldy #AI_THINK_PER_FRAME
@one:
        ldx ai_cursor
        inx
        cpx #AI_END
        bcc :+
        ldx #AI_FIRST
:       stx ai_cursor
        lda ent_active, x
        beq @skip
        tya
        pha
        jsr ai_think_for
        pla
        tay
@skip:
        dey
        bne @one
        rts
.endproc

; X = エンティティ番号。この1体ぶんの「状況評価 → 目標選択」。X は保存して返る。
; 動けない状態（のけぞり・ダウン・攻撃中）でも考えてよい。考えた結果を実行するかは
; ai_act_one が決める。
.proc ai_think_for
        jsr ai_load_profile
        jsr ai_select_target
        jmp ai_plan
.endproc

; X = エンティティ番号 → ai_prof にプロファイル行を入れる。X, Y は壊さない。
; 範囲外（配置側の詰め忘れ・未初期化）は 0 行目に倒す。表の外を引かせない。
.proc ai_load_profile
        lda ent_ai, x
        cmp #AI_PROFILE_COUNT
        bcc :+
        lda #0
:       sta ai_prof
        rts
.endproc

; ==========================================================================
; 目標選択 1 — 標的を選ぶ
; ==========================================================================
; X = 自分。結果は ai_best（AI_NONE なら見つからなかった）。X は保存して返る。
;
; 殴る相手の区画は立場で決まる（操作キャラ・仲間 → 敵区画 / 敵 → 操作キャラ・仲間の区画）。
; combat.s の act_target_range と同じ割り方である。味方討ちは区画の切り方で起きない。
;
; 「遠さ」は 横距離 + レーン差 × AI_LANE_COST。レーン差に値段を付けないと、
; 真横の敵より 1ドット近い別レーンの敵を選んでレーンを往復する。
.proc ai_select_target
        lda #AI_NONE
        sta ai_best
        lda #$FF
        sta ai_bestsc

        cpx #ENT_ENEMY_FIRST
        bcs @enemy_side
        lda #ENT_ENEMY_FIRST     ; 味方側 → 敵区画を見る
        sta ai_cand
        lda #ENT_ENEMY_FIRST + ENT_ENEMY_COUNT
        bne @scan                ; 常に成立（区画の終端は 0 ではない）
@enemy_side:
        lda #ENT_PLAYER          ; 敵 → 操作キャラと仲間の区画を見る
        sta ai_cand
        lda #ENT_ENEMY_FIRST
@scan:
        sta ai_scan_end

@loop:
        ldy ai_cand
        cpy ai_scan_end
        bcs @done
        lda ent_active, y
        beq @next
        lda ent_state, y
        cmp #ACT_ST_DOWN
        bcs @next                ; 倒れている相手は狙わない（殴っても当たらない）

        jsr ai_dist16            ; X = 自分, Y = 相手 → ai_dist / ai_dsign
        ldy ai_cand
        lda ent_lane, y
        tay                      ; Y = 相手のレーン
        lda ent_lane, x
        jsr lane_distance        ; A = |レーン差|（engine のプリミティブ。X は壊さない）
        asl a
        asl a
        asl a                    ; × AI_LANE_COST(8)。最大 3×8 = 24 なので桁あふれしない
        clc
        adc ai_dist
        bcc :+
        lda #$FF                 ; 頭打ち
:       cmp ai_bestsc
        bcs @next
        sta ai_bestsc
        lda ai_cand
        sta ai_best
@next:
        inc ai_cand
        jmp @loop

@done:
        ; **攻撃性**: この距離より遠い相手には食いつかない
        ldy ai_prof
        lda ai_bestsc
        cmp ai_p_aggr_x, y
        bcc @keep
        lda #AI_NONE
        sta ai_best
@keep:
        rts
.endproc

; ==========================================================================
; 目標選択 2 — 目標と立ち位置を決める
; ==========================================================================
; X = 自分。ai_best を受けて ai_goal / ai_target / ai_gap / ai_flags を決める。
;
; 順序に意味がある:
;   1. 追従の限界（leash_x）を超えていたら、標的が居ても捨てて戻る
;      … 仲間が敵を追って画面外へ消えるのを防ぐ。**追従は戦闘より優先**
;   2. 標的が居れば間合いを取る
;   3. 標的が居なければ、追従する者は戻り、しない者（敵）はその場に留まる
.proc ai_plan
        ldy ai_prof
        lda ai_p_leash_x, y
        beq @has_no_leash        ; 0 = 追従しない（敵）
        sta ai_t2
        ldy #ENT_PLAYER
        lda ent_active, y
        beq @has_no_leash        ; 戻る先が居ない
        jsr ai_dist16
        lda ai_dist
        cmp ai_t2
        bcs @regroup             ; 離れすぎた。標的を捨てて戻る
@has_no_leash:
        lda ai_best
        cmp #AI_NONE
        beq @no_target

        sta ai_target, x
        lda #AI_GOAL_ENGAGE
        sta ai_goal, x
        ldy ai_best
        jsr ai_dist16            ; 標的から見て自分がどちら側に居るか
        jsr ai_set_side          ; いま居る側に立つ（標的を突き抜けない）
        ldy ai_prof
        lda ai_p_hold_x, y       ; **好む間合い**
        sta ai_gap, x
        jmp ai_spread

@no_target:
        ldy ai_prof
        lda ai_p_leash_x, y
        beq @idle
        ldy #ENT_PLAYER
        lda ent_active, y
        bne @regroup             ; 追従する者は操作キャラの側へ戻る
@idle:
        lda #AI_GOAL_IDLE        ; 追従先を持たない者はその場に留まる
        sta ai_goal, x
        lda #AI_NONE
        sta ai_target, x
        rts

@regroup:
        lda #ENT_PLAYER
        sta ai_target, x
        lda #AI_GOAL_REGROUP
        sta ai_goal, x
        ldy #ENT_PLAYER
        jsr ai_dist16
        jsr ai_set_side
        ldy ai_prof
        lda ai_p_follow_x, y     ; **プレイヤーへの追従距離**
        sta ai_gap, x
        jmp ai_spread
.endproc

; X = 自分。直前の ai_dist16 の向きから、標的のどちら側に立つかを決める。
; ai_dsign = 0（相手は自分の右）なら、自分は相手の左側に立つ。
.proc ai_set_side
        lda ai_flags, x
        and #<~AI_F_SIDE_LEFT
        sta ai_t3
        lda ai_dsign
        eor #1                   ; 右に居る相手 → 自分は左側
        and #AI_F_SIDE_LEFT
        ora ai_t3
        sta ai_flags, x
        rts
.endproc

; ==========================================================================
; 団子にならないための分散
; ==========================================================================
; X = 自分。同じ標的に同じ側から寄る者のうち、自分より番号の小さい者の数だけ
; 立ち位置を後ろへずらす。乱数を使わないので、同じ状況からは必ず同じ陣形になる
; （再現しない不具合を作らない）。
;
; これが無いと、仲間2人が同じ敵の同じ側の同じ座標へ寄って完全に重なり、
; 1スキャンライン8スプライトの制約でどちらかが消える。
.proc ai_spread
        stx ai_t2
        ldy #AI_FIRST
@loop:
        cpy ai_t2
        bcs @done                ; 自分以上の番号は見ない（順位は番号順）
        lda ent_active, y
        beq @next
        lda ai_goal, y
        cmp #AI_GOAL_IDLE
        beq @next                ; 立ち位置を持っていない者は数えない
        lda ai_target, y
        cmp ai_target, x
        bne @next                ; 別の相手に寄っている
        lda ai_flags, y
        eor ai_flags, x
        and #AI_F_SIDE_LEFT
        bne @next                ; 反対側に寄っている
        lda ai_gap, x
        clc
        adc #AI_SPREAD
        bcs @next                ; 255 で頭打ち（それ以上は離さない）
        sta ai_gap, x
@next:
        iny
        jmp @loop
@done:
        ldx ai_t2
        rts
.endproc

; ==========================================================================
; 行動
; ==========================================================================
; X = エンティティ番号。決まった目標を1フレームぶん実行する。X は保存して返る。
;
; 動けるのは ACT_ST_IDLE のときだけである（攻撃の相・のけぞり・ダウン・ヒットストップ中は
; action 側が時間を進めている最中なので、AI は手を出さない）。
; 「操作キャラの前に立たない」規則は立ち位置の計算に織り込んである
; （ai_move_to_post → ai_clear_player_zone）。目標より先に効く。
.proc ai_act_one
        jsr ai_tick_timers
        jsr ai_load_profile

        lda act_hitstop, x
        bne @done                ; ヒットストップ中は当人の時間が止まっている
        lda ent_state, x
        bne @done                ; ACT_ST_IDLE 以外（攻撃中・のけぞり・ダウン・離脱）

        lda ai_target, x
        cmp #AI_NONE
        beq @done                ; AI_GOAL_IDLE。その場に居る
        tay
        lda ent_active, y
        beq @retarget
        lda ai_goal, x
        cmp #AI_GOAL_ENGAGE
        bne @go                  ; 戻る先としてなら、倒れていても基準にしてよい
        lda ent_state, y
        cmp #ACT_ST_DOWN
        bcc @go
@retarget:
        ; 標的が倒れた／消えた。次の思考の順番まで棒立ちになるのは不自然なので、
        ; **その瞬間だけ**その場で考え直す。標的が居なくなったときにしか起きないので、
        ; 「毎フレーム全員ぶん考える」には戻らない。
        ; （倒れた操作キャラを基準に戻る場合はここを通らない。通すと、操作キャラが
        ;   猶予中のあいだ毎フレーム全員が考え直すことになる）
        ;
        ; ただし1フレームに何体でも、とすると「まとめて倒した次のフレーム」だけ
        ; 全員ぶんの思考が走る。1フレームの費用の山はそこにできるので、
        ; ここにも予算を置く。あぶれた者は今フレーム何もしない（次のフレームで考える）。
        lda ai_rethink
        beq @done
        dec ai_rethink
        jsr ai_think_for
        lda ai_target, x
        cmp #AI_NONE
        beq @done
@go:
        ; 標的との距離と向きは、この1回だけ測って act_face・立ち位置・攻撃判断で
        ; 使い回す（1体につき3回測っていたものを1回にした）。
        ldy ai_target, x
        jsr ai_dist16
        lda ai_dsign
        sta act_face, x          ; 向きは常に標的へ（歩く向きではない）
        jsr ai_move_to_post
        jsr ai_lane_follow
        jmp ai_try_attack
@done:
        rts
.endproc

; X = エンティティ番号。時間の経過。動けない状態でも進める。
.proc ai_tick_timers
        lda ai_timer, x
        beq :+
        dec ai_timer, x
:       lda ai_lturn, x
        beq :+
        dec ai_lturn, x
:       rts
.endproc

; ==========================================================================
; 壁にならないための専有距離 —— **最優先の不具合を潰す規則**
; ==========================================================================
; X = 自分。立ち位置（ai_t0/ai_t1）が操作キャラの専有距離の内側にあったら、
; 外側へ押し出す。**立ち位置そのものを直す**のが肝である。
;
; 「近づきすぎたら退く」という別行動にすると、
;   退く → 目標の立ち位置へ戻る → また近づきすぎる
; を毎フレーム繰り返して、仲間がその場で細かく震える（実測で 8ドット幅の振動）。
; 立ち位置の側を直せば、目標が最初から専有距離の外にあるので震えない。
;
; 押し出す向きは「いま立ち位置がある側」＝近い方の外である。
; 押し出した先で仲間どうしが重なる件は、この後の ai_avoid_allies が引き受ける
; （かつて専有距離を番号ごとに広げて凌いでいたが、それは2人とも押されたときしか
;   効かず、押されなかった者の居場所には届かなかった）。
;
; 敵には効かない。敵が操作キャラに近づくのは仕事だからである。
.proc ai_clear_player_zone
        cpx #ENT_ENEMY_FIRST
        bcc :+
        rts
:       lda #AI_DIR_NEAREST      ; 向きはこだわらない。近い方の外へ出せばよい
        sta ai_dir
        jmp ai_push_out_of_player
.endproc

; X = 自分、ai_dir = 出す向き（0:左 / 1:右 / AI_DIR_NEAREST:近い方）。
; 立ち位置が操作キャラの専有距離の内側なら、**ai_dir の向きのまま**外へ出す。
; 向きを呼び出し元に決めさせられるのは、仲間を避けて動いた立ち位置を
; 「近い方」へ戻すと、避けたはずの相手の所へ押し返されるからである。
.proc ai_push_out_of_player
        ldy #ENT_PLAYER
        lda ent_active, y
        beq @done
        lda ent_lane, x
        cmp ent_lane, y
        bne @done                ; 別レーンなら画面上で重ならない
        jsr ai_post_gap
        bcs @done                ; 256 ドット以上離れている
        cmp #AI_CLEAR_X
        bcs @done                ; もう専有距離の外に居る

        lda ai_dir
        bpl :+
        lda ai_t3                ; 近い方＝いま立ち位置がある側（ai_post_gap が測っている）
        sta ai_dir
:       lsr a                    ; 0 → carry 0（左） / 1 → carry 1（右）
        bcc @push_left
@push_right:
        lda ent_x_lo, y
        clc
        adc #AI_CLEAR_X
        sta ai_t0
        lda ent_x_hi, y
        adc #0
        sta ai_t1
        rts
@push_left:
        lda ent_x_lo, y
        sec
        sbc #AI_CLEAR_X
        sta ai_t0
        lda ent_x_hi, y
        sbc #0
        sta ai_t1
        bcs @done
        lda #1                   ; 左はワールドの外（左端は 0 で止まり逃げ場が無い）。右へ回す
        sta ai_dir
        jmp @push_right
@done:
        rts
.endproc

; ==========================================================================
; 立ち位置どうしの最小間隔 —— 押し出しで潰れた分散を取り戻す
; ==========================================================================
; X = 自分。立ち位置（ai_t0/ai_t1）が、同じレーンに居て**自分より番号の小さい仲間**の
; 居場所に AI_SPREAD より近ければ、その仲間から AI_SPREAD だけ離れた所へ置き直す。
;
; なぜ ai_spread だけでは足りないか（実測で見つかった穴）:
;   ai_spread は「標的から取る間合い」を番号順にずらす。ところが
;   ai_clear_player_zone は、その立ち位置を**操作キャラからの距離**に潰す。
;   潰された者の行き先は、潰されなかった者がたまたまそこに立っているかどうかを知らない。
;   実測: 仲間1 は標的+14 に素通り、仲間2 は標的+38 が専有距離の内側なので
;   「操作キャラ-専有距離」へ潰され、両者が 1〜3 ドットまで寄って 70 フレーム重なった。
;
; 避ける向きは**いま自分の体が居る側**である（相手を横切らない）。
; 「操作キャラから遠ざかる側」に固定すると、相手を通り抜けて向こう側へ回ることになり、
; すれ違いのあいだ（相対速度 1.6 ドット/f で 16 ドットぶん＝約10フレーム）重なる。
; すれ違いは短くできないので、**そもそもすれ違わせない**のが正しい。
;
; 避けた先が操作キャラの専有距離に入るときは、同じ向きのまま専有距離の外まで出す。
; 近い方へ戻すと、避けたはずの相手の所へ押し返されて振動する。
;
; 動くのは番号の大きい方だけなので、2人が押し合って震えることはない。
; 乱数を使わないので同じ状況からは必ず同じ陣形になる（再現しない不具合を作らない）。
;
; 型に属さない場の規則である（型ごとの分岐をここに書かない）。
; 敵は見ない。敵は押し出しを受けないので分散が潰れず、6体ぶん走査する費用に見合わない。
.proc ai_avoid_allies
        cpx #ENT_ENEMY_FIRST
        bcc :+
        rts
:       stx ai_t2                ; 走査の終端（自分の番号）
        ldy #AI_FIRST
@loop:
        cpy ai_t2
        bcs @done                ; 自分以上の番号は見ない（順位は番号順）
        lda ent_active, y
        beq @next
        lda ent_lane, y
        cmp ent_lane, x
        bne @next                ; 別レーンなら画面上で重ならない
        jsr ai_post_gap
        bcs @next                ; 256 ドット以上離れている
        cmp #AI_SPREAD
        bcs @next                ; 十分離れている

        lda ent_x_lo, x          ; 向き = いま自分の体が居る側（相手を横切らない）
        sec
        sbc ent_x_lo, y
        lda ent_x_hi, x
        sbc ent_x_hi, y
        lda #1
        bcs :+
        lda #0
:       sta ai_dir
        beq @apart_left
@apart_right:
        lda ent_x_lo, y
        clc
        adc #AI_SPREAD
        sta ai_t0
        lda ent_x_hi, y
        adc #0
        sta ai_t1
        jmp @pushed
@apart_left:
        lda ent_x_lo, y
        sec
        sbc #AI_SPREAD
        sta ai_t0
        lda ent_x_hi, y
        sbc #0
        sta ai_t1
        bcs @pushed
        lda #1                   ; 左はワールドの外。右へ回す
        sta ai_dir
        jmp @apart_right
@pushed:
        tya                      ; ai_push_out_of_player は Y を使う
        pha
        jsr ai_push_out_of_player
        pla
        tay
@next:
        iny
        jmp @loop
@done:
        rts
.endproc

; X = 自分, Y = 相手。立ち位置（ai_t0/ai_t1）と相手の居場所の距離を測る。
;   carry = 1 … 256 ドット以上離れている（A は無意味）
;   carry = 0 … A = |差|、ai_t3 = 0: 立ち位置は相手の左 / 1: 右
; X, Y は壊さない。ai_t4 を壊す。
.proc ai_post_gap
        lda ai_t0
        sec
        sbc ent_x_lo, y
        sta ai_t4
        lda ai_t1
        sbc ent_x_hi, y
        beq @right               ; 上位 0 → 差は 0..255
        cmp #$FF
        bne @far                 ; 上位が 0 でも $FF でもない → 256 ドット以上
        lda #0                   ; 上位 $FF → 差は -256..-1
        sta ai_t3
        lda ai_t4
        eor #$FF
        clc
        adc #1                   ; |差|
        beq @far                 ; ちょうど 256 ドット
        clc
        rts
@right:
        lda #1
        sta ai_t3
        lda ai_t4
        clc
        rts
@far:
        sec
        rts
.endproc

; ==========================================================================
; 立ち位置へ歩く
; ==========================================================================
; X = エンティティ番号。標的から ai_gap だけ離れた側（ai_flags の SIDE）へ歩く。
; 目標位置との差が AI_DEADBAND 以内なら動かない（端数で震えないため）。
; 向き（act_face）は呼び出し元が標的へ向けて設定済みである。
.proc ai_move_to_post
        ldy ai_target, x
        lda ai_flags, x
        and #AI_F_SIDE_LEFT
        bne @left_side
        lda ent_x_lo, y          ; 立ち位置 = 標的 + gap
        clc
        adc ai_gap, x
        sta ai_t0
        lda ent_x_hi, y
        adc #0
        sta ai_t1
        jmp @clear
@left_side:
        lda ent_x_lo, y          ; 立ち位置 = 標的 - gap
        sec
        sbc ai_gap, x
        sta ai_t0
        lda ent_x_hi, y
        sbc #0
        sta ai_t1
        bcs @clear
        lda #0                   ; ワールドの左端より左には立てない
        sta ai_t0
        sta ai_t1

@clear:
        jsr ai_clear_player_zone ; **操作キャラの前に立たない**（最優先）
        jsr ai_avoid_allies      ; 押し出しで潰れた分散を取り戻す（仲間どうしが団子にならない）

        lda ai_t0                ; 差 = 立ち位置 - 自分（符号つき16bit）
        sec
        sbc ent_x_lo, x
        sta ai_t0
        lda ai_t1
        sbc ent_x_hi, x
        sta ai_t1
        bcs @positive
        lda ai_t0                ; 負 = 左へ行くべき。絶対値にする
        eor #$FF
        clc
        adc #1
        sta ai_t0
        lda ai_t1
        eor #$FF
        adc #0
        sta ai_t1
        lda #1
        sta ai_t3
        jmp @magnitude
@positive:
        lda #0
        sta ai_t3

@magnitude:
        lda ai_t1
        bne @walk                ; 256 ドット以上離れている
        lda ai_t0
        cmp #AI_DEADBAND
        bcc @done                ; もう着いている
@walk:
        jsr ai_step_px
        lda ai_px
        beq @done
        sta act_px
        lda ai_t3
        bne @go_left
        jmp act_move_right
@go_left:
        jmp act_move_left
@done:
        rts
.endproc

; X = エンティティ番号。今フレームに進めるドット数を ai_px に入れる。
; 速さは 1/16 ドット単位で刻む（端数は ai_sub に持ち越す）。ai_prof が要る。
.proc ai_step_px
        ldy ai_prof
        lda ai_sub, x
        clc
        adc ai_p_speed, y
        pha
        and #$0F
        sta ai_sub, x
        pla
        lsr a
        lsr a
        lsr a
        lsr a
        sta ai_px
        rts
.endproc

; ==========================================================================
; レーンを合わせる
; ==========================================================================
; X = エンティティ番号。標的のレーンへ1つずつ寄る。
; **レーン移動の積極性**（lane_hold）が小さいほど頻繁に移る。
; 補間は engine（lane.s）が持つ。ここは「移りたい」と言うだけである。
.proc ai_lane_follow
        lda ent_lane_step, x
        bne @done                ; 補間中
        lda ai_lturn, x
        bne @done                ; まだ移らない
        ldy ai_target, x
        lda ent_lane, y
        cmp ent_lane, x
        beq @done
        bcc @backward
        lda ent_lane, x          ; 標的の方が手前 → 1つ手前へ
        clc
        adc #1
        jmp @move
@backward:
        lda ent_lane, x          ; 標的の方が奥 → 1つ奥へ
        sec
        sbc #1
@move:
        jsr ent_lane_move        ; 範囲外のレーン番号は engine 側が弾く
        ldy ai_prof
        lda ai_p_lane_hold, y
        sta ai_lturn, x
@done:
        rts
.endproc

; ==========================================================================
; 攻撃
; ==========================================================================
; X = エンティティ番号。**攻撃に移る閾値**（reach_x）の内側なら振る。
;
; 振り出した後の発生・持続・硬直・当たり判定・ダメージ・ヒットストップ・
; ノックバック・ダウンは**すべて src/action/ が持っている**。ここでは起こすだけである
; （act_start_attack → act_attack_step → act_hit_scan → act_damage）。
; 敵の攻撃もこの経路を通る。AI 側でダメージを与えてはならない。
;
; レーン差は見ず「同じレーンのときだけ」に絞ってある。どのレーンまで攻撃が届くかは
; action の持ち分（ACT_LANE_REACH）であり、AI がその値を知って振る舞いを変えると
; 判定ルールの二重管理になる。同じレーンなら必ず届くので、この判断は常に安全側である。
.proc ai_try_attack
        lda ai_goal, x
        cmp #AI_GOAL_ENGAGE
        bne @done
        lda ai_timer, x
        bne @done                ; 攻撃の間隔（atk_gap）
        lda ent_lane_step, x
        bne @done                ; レーン移動中は振らない
        ldy ai_target, x
        lda ent_lane_step, y
        bne @done                ; 相手も移動中（すれ違いざまの空振りを避ける）
        lda ent_lane, y
        cmp ent_lane, x
        bne @done
        lda ent_state, y
        cmp #ACT_ST_DOWN
        bcs @done                ; 倒れている相手は殴らない
        ldy ai_prof              ; 距離は ai_act_one が測った ai_dist を使い回す
        lda ai_dist              ; （歩いたぶん最大 2 ドットだけ古い。間合いの幅に対して無視できる）
        cmp ai_p_reach_x, y
        bcs @done
        lda ai_p_atk_gap, y
        sta ai_timer, x
        lda #1                   ; 初段のみ。コンボは操作キャラの手触り（P2 で見直す）
        jmp act_start_attack
@done:
        rts
.endproc

; ==========================================================================
; 距離
; ==========================================================================
; X = 自分, Y = 相手 →
;   ai_dist  = 横距離（ワールドX の差の絶対値。255 で頭打ち）
;   ai_dsign = 0: 相手は自分の右 / 1: 左。**act_face と同じ意味の値**
; X と Y は壊さない。ai_t0 / ai_t1 を壊す。
.proc ai_dist16
        lda ent_x_lo, y
        sec
        sbc ent_x_lo, x
        sta ai_t0
        lda ent_x_hi, y
        sbc ent_x_hi, x
        sta ai_t1
        bcs @positive
        lda ai_t0                ; 負（相手は左）。16bit の絶対値を取る
        eor #$FF
        clc
        adc #1
        sta ai_t0
        lda ai_t1
        eor #$FF
        adc #0
        sta ai_t1
        lda #1
        sta ai_dsign
        jmp @clamp
@positive:
        lda #0
        sta ai_dsign
@clamp:
        lda ai_t1
        beq @low
        lda #$FF                 ; 256 ドット以上は「遠い」でひとまとめにしてよい
        sta ai_dist
        rts
@low:
        lda ai_t0
        sta ai_dist
        rts
.endproc
