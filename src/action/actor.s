; actor.s — アクション側のエンティティ付随テーブルと、1フレームの進行。
;
; 責務:
;   * HP・状態タイマー・向き・無敵・ヒットストップ・ノックバックの器（engine の SoA と並ぶ配列）
;   * 毎フレームの時間経過（ヒットストップ → 無敵 → ノックバック → 状態機械）
;   * ダウン（猶予中）と猶予タイマー、猶予切れの確定（ADR-0005）
;   * 「今は何をしても動かない」の提示（ADR-0006 の UI 要件の最小実装）
;   * フレームの入口 action_update
;
; ここに数値を書かない。すべて action_params.inc / action_params.s から来る。
;
; engine のゼロページ tmp0〜tmp3 は engine の呼び出しをまたいで保たれないので、
; 状態も作業変数も tmp に置かない（このファイルの BSS を使う）。
.include "constants.inc"
.include "zeropage.inc"
.include "action.inc"
.include "action_params.inc"

; --- 付随テーブル（エンティティ番号で引く。engine の SoA と同じ添字）---
.export act_hp, act_timer, act_step, act_face, act_invuln, act_hitstop
.export act_kb, act_flags, act_attr0, act_sub
; --- 作業変数（engine 呼び出しをまたいでも保つ必要がある）---
.export act_px, act_t0, act_t1, act_t2, act_t3
.export act_atk_ent, act_scan_end, act_kb_dir, act_kb_amt, act_dmg_amt
.export act_ld0, act_ld1, act_la0, act_la1
; --- 外に見せる状態 ---
.export act_ready, act_player_failed
; --- 手続き ---
.export action_init, act_init_entity, action_update, act_update_actor
.export act_role, act_move_left, act_move_right, act_set_pal
.export act_enter_down, act_grace_expired, action_revive

.import ent_active, ent_state, ent_attr, ent_body
.import ent_x_lo, ent_x_hi, ent_kill
.import stage_w_lo, stage_w_hi
.import act_grace_frames, act_hp_by_body
.import act_attack_step, act_damage
.import act_input_gate, act_input_buffer, act_input_drop, action_update_player
.import act_pad_new

.segment "BSS"

act_hp:       .res MAX_ENTITIES   ; 現在HP。0 でダウン（猶予中）へ
act_timer:    .res MAX_ENTITIES   ; いまの状態の残りフレーム（攻撃の相／のけぞり／猶予を共用）
act_step:     .res MAX_ENTITIES   ; コンボ段数 1..ACT_COMBO_MAX（0 = 攻撃していない）
act_face:     .res MAX_ENTITIES   ; 向き 0 = 右 / 1 = 左
act_invuln:   .res MAX_ENTITIES   ; 無敵の残りフレーム
act_hitstop:  .res MAX_ENTITIES   ; ヒットストップの残りフレーム（当人の更新を止める）
act_kb:       .res MAX_ENTITIES   ; ノックバック速度（符号つき・ドット/フレーム）
act_flags:    .res MAX_ENTITIES   ; ACT_F_*
act_attr0:    .res MAX_ENTITIES   ; 本来の ent_attr（点滅から戻すために覚えておく）
act_sub:      .res MAX_ENTITIES   ; 横移動の小数部（1/16 ドット）

act_px:       .res 1              ; 今回動かすドット数
act_t0:       .res 1              ; 16bit 計算の作業
act_t1:       .res 1
act_t2:       .res 1
act_t3:       .res 1
act_atk_ent:  .res 1              ; 判定中の攻撃側エンティティ番号
act_scan_end: .res 1              ; 走査する標的区画の終端（この番号は含まない）
act_kb_dir:   .res 1              ; 与えるノックバックの向き（0 = 右 / 1 = 左）
act_kb_amt:   .res 1              ; 与えるノックバックの初速
act_dmg_amt:  .res 1              ; 与えるダメージ
act_ld0:      .res 1              ; レーン判定の作業（防御側の実効レーン2つ）
act_ld1:      .res 1
act_la0:      .res 1              ; 同（攻撃側）
act_la1:      .res 1
act_ready:    .res 1              ; action_init 済みか（0 = まだ）
; 操作キャラの猶予が切れた = シナリオ失敗（ADR-0005）。
; **ここで立てるのはフラグまで**である。失敗の演出とオートセーブからの再開は
; P4 の campaign-dev の担当（docs/plan.md P4）。
act_player_failed: .res 1

.segment "CODE"

; ==========================================================================
; 初期化
; ==========================================================================

; シーン開始時に1回呼ぶ。エンティティを配置し終えた後であること
; （ent_active / ent_body / ent_attr を見て初期HPと本来の色を決める）。
.proc action_init
        lda #0
        sta act_player_failed
        sta act_atk_ent
        sta act_kb_dir
        sta act_kb_amt
        jsr act_input_drop               ; 先行入力と実効パッドを空にする
        ldx #MAX_ENTITIES - 1
@loop:
        jsr act_init_entity
        dex
        bpl @loop
        lda #1
        sta act_ready
        rts
.endproc

; X = エンティティ番号。1体ぶんの初期化。
; 途中で湧いた敵（P3 の waves）にも、配置直後にこれを呼べばよい。
.proc act_init_entity
        lda #0
        sta act_timer, x
        sta act_step, x
        sta act_face, x
        sta act_invuln, x
        sta act_hitstop, x
        sta act_kb, x
        sta act_flags, x
        sta act_sub, x
        sta act_hp, x
        lda ent_active, x
        beq @done
        lda ent_attr, x
        sta act_attr0, x
        lda #ACT_ST_IDLE
        sta ent_state, x
        jsr act_role
        cmp #ACT_ROLE_ENEMY
        beq @enemy
        lda #ACT_HP_PARTY                ; 操作キャラ・仲間（P4 で roster データに置き換わる）
        sta act_hp, x
        rts
@enemy:
        ldy ent_body, x                  ; 敵は体格型ごとの耐久
        lda act_hp_by_body, y
        sta act_hp, x
@done:
        rts
.endproc

; X = エンティティ番号 → A = ACT_ROLE_*。X と Y は壊さない。
.proc act_role
        cpx #ENT_ALLY_FIRST
        bcc @player
        cpx #ENT_ENEMY_FIRST
        bcc @ally
        lda #ACT_ROLE_ENEMY
        rts
@player:
        lda #ACT_ROLE_PLAYER
        rts
@ally:
        lda #ACT_ROLE_ALLY
        rts
.endproc

; ==========================================================================
; 1フレームの進行（main.s から毎フレーム1回）
; ==========================================================================
; 並び:
;   1. 入力ゲート（猶予中なら実効入力をゼロにし、先行入力も捨てる。ADR-0006）
;   2. 先行入力バッファの寿命（ヒットストップ中でも捨てない。規約）
;   3. 操作キャラの更新（時間経過 → 入力）
;   4. 仲間・敵の更新（**行動は決めない**。被弾反応の時間経過だけ。行動は ai-dev）
;   5. 提示（点滅など）
.proc action_update
        lda act_ready
        bne @ready
        jsr action_init                  ; 呼び忘れの保険。シーン開始で明示的に呼ぶのが正しい
@ready:
        jsr act_input_gate
        jsr act_input_buffer

.if ::ACT_DBG_SELF_DAMAGE
        jsr act_dbg_self_damage
.endif

        jsr action_update_player

        ldx #ENT_ALLY_FIRST
@loop:
        lda ent_active, x
        beq @next
        jsr act_update_actor
@next:
        inx
        cpx #ENT_FREE_FIRST
        bcc @loop

        jmp act_present_all
.endproc

.if ::ACT_DBG_SELF_DAMAGE
; P1 の確認用。敵に AI が無いので、被弾・ダウン・猶予を主が見る手段がこれしかない。
; 猶予中は act_input_gate が実効入力をゼロにするので、この SELECT も効かない。
.proc act_dbg_self_damage
        lda act_pad_new                  ; 実効入力（猶予中は 0 になっている）
        and #PAD_SELECT
        beq @done
        ldx #ENT_PLAYER
        stx act_atk_ent                  ; 加害者は自分（ヒットストップも自分に乗る）
        lda #0
        sta act_kb_dir                   ; 右へ吹き飛ぶ
        lda #ACT_DBG_SELF_KB
        sta act_kb_amt
        lda #ACT_DBG_SELF_DMG
        jmp act_damage
@done:
        rts
.endproc
.endif

; X = エンティティ番号。1体ぶんの時間経過。
; **行動の決定はここに書かない**（プレイヤーの入力は player.s、AI は ai-dev）。
; X は保存して返る。
.proc act_update_actor
        lda act_hitstop, x
        beq @no_stop
        dec act_hitstop, x               ; ヒットストップ中は当人の時間を止める
        rts                              ; （入力は別経路で溜まり続ける。規約）
@no_stop:
        lda ent_state, x
        cmp #ACT_ST_DOWN
        bcc @alive
        cmp #ACT_ST_OUT
        beq @done                        ; 猶予切れ。もう何も進まない
        jsr act_knockback_step           ; ダウン中も吹き飛びは続ける
        jmp act_down_step
@alive:
        lda act_invuln, x
        beq @no_invuln
        dec act_invuln, x
@no_invuln:
        jsr act_knockback_step
        lda ent_state, x
        cmp #ACT_ST_HURT
        beq @hurt
        cmp #ACT_ST_IDLE
        beq @done
        jmp act_attack_step              ; 攻撃の3相（combat.s）
@hurt:
        dec act_timer, x
        bne @done
        lda #ACT_ST_IDLE
        sta ent_state, x
@done:
        rts
.endproc

; ==========================================================================
; ダウン（猶予中）と猶予タイマー — ADR-0005 / ADR-0006
; ==========================================================================

; X = エンティティ番号。HP が尽きたときに入る。**即死亡ではない**。
.proc act_enter_down
        lda #ACT_ST_DOWN
        sta ent_state, x
        lda #0
        sta act_step, x
        sta act_invuln, x                ; ダウン中は act_damage 側で弾くので無敵は要らない
        sta act_flags, x
        jsr act_role
        tay
        lda act_grace_frames, y          ; 猶予の長さは立場ごとの表から（データ側）
        sta act_timer, x
        cpx #ENT_PLAYER
        bne @done
        ; ADR-0006: 猶予中は先行入力を保持しない。ここで捨て、いま押されているボタンは
        ; 「離して押し直す」まで無効にする。ヒットストップ（捨てない）との唯一の違い。
        jsr act_input_drop
@done:
        rts
.endproc

; X = エンティティ番号。猶予タイマーを1フレーム進める。
.proc act_down_step
        lda act_timer, x
        beq act_grace_expired            ; 猶予 0 の設定（即確定）にも耐える
        dec act_timer, x
        beq act_grace_expired
.if ::ACT_DBG_AUTO_REVIVE
        lda act_timer, x
        cmp #ACT_DBG_AUTO_REVIVE
        bne @done
        jmp action_revive                ; P1 の確認用。本来これを起こすのは P2 の ai-dev
.endif
@done:
        rts
.endproc

; X = エンティティ番号。猶予が切れた（確定）。
;   操作キャラ → シナリオ失敗のフラグを立てる（演出と再開は P4 campaign-dev）
;   それ以外   → 戦線離脱／撃破。画面から消す
.proc act_grace_expired
        lda #ACT_ST_OUT
        sta ent_state, x
        lda #0
        sta act_kb, x
        cpx #ENT_PLAYER
        bne @other
        lda #1
        sta act_player_failed
        rts                              ; 操作キャラは倒れたまま画面に残す
@other:
        jmp ent_kill
.endproc

; X = エンティティ番号。ダウンからの復帰。**蘇生手段そのものは実装しない**
; （ADR-0007 の蘇生アイテムを使うのは P2 の ai-dev）。ここは「復帰の口」だけ。
;   出力: キャリークリア = 復帰した / キャリーセット = 猶予中でないので何もしなかった
.proc action_revive
        lda ent_state, x
        cmp #ACT_ST_DOWN
        bne @fail
        lda #ACT_ST_IDLE
        sta ent_state, x
        lda #ACT_REVIVE_HP
        sta act_hp, x
        lda #0
        sta act_timer, x
        sta act_step, x
        sta act_kb, x
        sta act_hitstop, x
        sta act_flags, x
        sta act_sub, x
        lda #ACT_REVIVE_INVULN
        sta act_invuln, x
        cpx #ENT_PLAYER
        bne @ok
        ; ADR-0006: 復帰時の入力状態は「何も押していない」から始める。
        ; ボタンを押しっぱなしで復帰しても、押し直すまで入力として扱わない。
        jsr act_input_drop
@ok:
        clc
        rts
@fail:
        sec
        rts
.endproc

; ==========================================================================
; ノックバックと移動（ワールドX・16bit）
; ==========================================================================

; X = エンティティ番号。ノックバックを1フレームぶん適用し、減衰させる。
.proc act_knockback_step
        lda act_kb, x
        beq @done
        bmi @left
        sta act_px
        jsr act_move_right
        lda act_kb, x
        sec
        sbc #ACT_KB_DECAY
        bcs @store
        lda #0
@store:
        sta act_kb, x
        rts
@left:
        eor #$FF
        clc
        adc #1                           ; 絶対値
        sta act_px
        jsr act_move_left
        lda act_kb, x
        clc
        adc #ACT_KB_DECAY
        bmi @store2
        lda #0
@store2:
        sta act_kb, x
@done:
        rts
.endproc

; X = エンティティ番号, act_px = ドット数。ワールドの左端 (0) を越えさせない。
; 越えると 16bit が巻き取って「ステージの遥か右」になり、カメラが飛ぶ。
.proc act_move_left
        lda ent_x_lo, x
        sec
        sbc act_px
        sta act_t0
        lda ent_x_hi, x
        sbc #0
        bcc @clamp
        sta ent_x_hi, x
        lda act_t0
        sta ent_x_lo, x
        rts
@clamp:
        lda #0
        sta ent_x_lo, x
        sta ent_x_hi, x
        rts
.endproc

; X = エンティティ番号, act_px = ドット数。ステージ右端（幅 - 余白）で止める。
; ステージ幅は scroll.s が持っている。ここに長さを埋めない。
.proc act_move_right
        lda ent_x_lo, x
        clc
        adc act_px
        sta act_t0
        lda ent_x_hi, x
        adc #0
        sta act_t1

        lda stage_w_lo
        sec
        sbc #ACT_EDGE_MARGIN
        sta act_t2
        lda stage_w_hi
        sbc #0
        sta act_t3

        lda act_t3                       ; 限界 < 新しい位置 ならクランプ
        cmp act_t1
        bcc @clamp
        bne @store
        lda act_t2
        cmp act_t0
        bcs @store
@clamp:
        lda act_t2
        sta act_t0
        lda act_t3
        sta act_t1
@store:
        lda act_t0
        sta ent_x_lo, x
        lda act_t1
        sta ent_x_hi, x
        rts
.endproc

; ==========================================================================
; 提示 — 「今は何をしても動かない」が分かること（ADR-0006）
; ==========================================================================
; 仮CHR には攻撃・ダウンの絵が無いので、スプライトのパレット番号だけで示す。
; 無反応は不具合と区別がつかない、というのが ADR-0006 の要求である。
; 猶予の残り時間は**点滅の速さ**で見せる（残りが少ないほど速い）。
; 本番の絵とUI（残り時間の数値表示など）は P8。
;
; ダウン中も優先度クラス（ent_class）は変えない。操作キャラを class 3 に落とすと
; フリッカーで消えうるが、それでは ADR-0006 の「猶予中だと分かる提示」が成立しない。
; 仲間・敵はもともと SPR_CLASS_FAR_ENE（= クラス3）なので、ADR-0005 の
; 「クラス3相当で足りる」はそのまま満たしている。
.proc act_present_all
        ldx #0
@loop:
        lda ent_active, x
        beq @next
        jsr act_present
@next:
        inx
        cpx #ENT_FREE_FIRST
        bcc @loop
        rts
.endproc

; X = エンティティ番号。
.proc act_present
        lda ent_state, x
        cmp #ACT_ST_DOWN
        beq @down
        cmp #ACT_ST_OUT
        beq @out
        lda act_invuln, x
        bne @invuln
.if ::ACT_SHOW_ACTIVE
        lda ent_state, x
        cmp #ACT_ST_ATK_ACTIVE
        beq @active
.endif
@base:
        lda act_attr0, x
        sta ent_attr, x
        rts
.if ::ACT_SHOW_ACTIVE
@active:
        lda #ACT_PAL_ACTIVE              ; 攻撃判定が出ているフレーム
        jmp act_set_pal
.endif
@invuln:
        lda #ACT_BLINK_HURT
        and frame_counter
        beq @base
        lda #ACT_PAL_HURT
        jmp act_set_pal
@out:
        lda #ACT_PAL_DOWN                ; 猶予切れ。点滅を止めて「もう終わった」を示す
        jmp act_set_pal
@down:
        lda act_timer, x                 ; 残り猶予が少ないほど速く点滅する
        cmp #ACT_DOWN_WARN
        lda #ACT_BLINK_SLOW
        bcs @blink
        lda #ACT_BLINK_FAST
@blink:
        and frame_counter
        beq @down_a
        lda #ACT_PAL_DOWN_ALT
        jmp act_set_pal
@down_a:
        lda #ACT_PAL_DOWN
        jmp act_set_pal
.endproc

; X = エンティティ番号, A = パレット番号(0..3)。本来の属性のパレットだけ差し替える。
.proc act_set_pal
        sta act_t0
        lda act_attr0, x
        and #%11111100
        ora act_t0
        sta ent_attr, x
        rts
.endproc
