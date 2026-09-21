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
.export act_kb, act_flags, act_attr0, act_sub, act_dsub
.export act_vel, act_mset, act_cy, act_proj_owner
; --- 作業変数（engine 呼び出しをまたいでも保つ必要がある）---
.export act_px, act_t0, act_t1, act_t2, act_t3
.export act_atk_ent, act_scan_end, act_kb_dir, act_kb_amt, act_dmg_amt
.export act_dy, act_tol
; --- 外に見せる状態 ---
.export act_ready, act_player_failed, act_lframe
; --- 手続き ---
.export action_init, act_init_entity, action_update, act_update_actor
.export act_role, act_move_left, act_move_right, act_set_pal
.export act_shift_left, act_shift_right, act_speed_mag, act_can_move
.export act_enter_down, act_grace_expired, action_revive

.import ent_active, ent_state, ent_attr, ent_body, ent_y
.import ent_x_lo, ent_x_hi, ent_kill, ent_set_pose
.import stage_w_lo, stage_w_hi
.import act_grace_frames, act_hp_by_body, act_pose_by_state
.import act_mset_by_role
.import act_attack_step, act_damage, act_row_for, act_proj_update
.import atk_startup
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
act_dsub:     .res MAX_ENTITIES   ; 奥行き移動の小数部（1/16 ドット）。ADR-0009
; 横方向の速度（符号つき・1/16 ドット/フレーム）。**歩きと小走りの強弱の実体**である。
; 歩く速さ以下では押した瞬間に入り離した瞬間に 0 になる（＝今までと同じ手触り）。
; 慣性が出るのは歩く速さを超えている間だけ（action_params.inc §2-a）。
; 飛び道具も AI が動かす者も 0 のままで、歩きの絵の周期に影響しない。
act_vel:      .res MAX_ENTITIES
; 技構成（ACT_MSET_*）。**敵の攻撃を操作キャラのコンボ表から分離する鍵**。
; 既定は立場から決まる（act_mset_by_role）。遠隔の敵は ai-dev がここを書き換える。
act_mset:     .res MAX_ENTITIES
; 振り始めに固定した足元Y（commit）。攻撃中はここへ毎フレーム引き戻す。
; 溜めている間に奥行きを追尾させないための1バイトであり、
; **予備動作を長くしたことが意味を持つかどうかは、この値を守れるかで決まる。**
act_cy:       .res MAX_ENTITIES
; 空き枠に居る飛び道具を撃った主のエンティティ番号（枠1つにつき1バイト）。
; 弾が誰を狙う側なのかは、番号ではなく**撃った主の立場**で決まる（combat.s）。
act_proj_owner: .res ENT_FREE_COUNT

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
act_dy:       .res 1              ; 奥行き判定の作業（攻撃側と被弾側の足元Yの差）
act_tol:      .res 1              ; 同（その組み合わせのY許容幅）
act_ready:    .res 1              ; action_init 済みか（0 = まだ）
; 論理フレームカウンタ。**action_update が1回走るごとに1増える。**
; ゼロページの frame_counter（NMI が毎フレーム加算する実フレーム）と違い、
; デバッグのスロー（src/debug.inc の dbg_slow）で論理更新が間引かれると
; こちらも同じだけ遅くなる。
;   運動（歩きのコマ送り）は論理フレームで数える。移動量も論理フレームで積むので、
;   これを実フレームで数えると、半速のとき「進む距離：脚の運び」が 1:2 にずれる。
;   通知（点滅）は実フレーム（frame_counter）のまま。点滅は運動ではなく人間への
;   合図なので、スロー中も同じ速さで明滅した方が読める。
; **この2つが揃っていないのは書き忘れではない**（act_present / act_pose_for_state）。
act_lframe:   .res 1
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
        lda #0
        ldx #ENT_FREE_COUNT - 1
@owner:
        sta act_proj_owner, x
        dex
        bpl @owner
        ldx #MAX_ENTITIES - 1
@loop:
        jsr act_init_entity
        dex
        bpl @loop
.if ::ACT_DBG_SHOOTER
        jsr act_dbg_make_shooter
.endif
        lda #1
        sta act_ready
        rts
.endproc

.if ::ACT_DBG_SHOOTER
; P2 の確認用。**遠隔の敵は ai-dev が作る**（AI 側の型がまだ無い）。
; それまで主が飛び道具を実機で見られるように、いちばん後ろの敵1体だけを
; 遠隔の技構成にする。**仕様ではない。** ai-dev が act_mset を詰めるようになったら
; ACT_DBG_SHOOTER を 0 にする（ai_init は action_init の後なので、両方あれば ai が勝つ）。
.proc act_dbg_make_shooter
        ldx #ENT_ENEMY_FIRST + ENT_ENEMY_COUNT - 1
@find:
        lda ent_active, x
        bne @found
        dex
        cpx #ENT_ENEMY_FIRST
        bcs @find
        rts
@found:
        lda #ACT_MSET_SHOOT
        sta act_mset, x
        rts
.endproc
.endif

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
        sta act_dsub, x
        sta act_vel, x
        sta act_mset, x                  ; = ACT_MSET_PARTY。有効なら立場で上書きする
        sta act_hp, x
        lda ent_y, x
        sta act_cy, x                    ; commit の初期値は「いま立っている奥行き」
        lda ent_active, x
        beq @done
        lda ent_attr, x
        sta act_attr0, x
        lda #ACT_ST_IDLE
        sta ent_state, x
        jsr act_role
        tay
        lda act_mset_by_role, y          ; 立場ごとの既定の技構成（敵は敵の表を引く）
        sta act_mset, x
        tya
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
;   5. 飛び道具の進行（空き枠。出る・飛ぶ・当たる・消える）
;   6. 提示（点滅など）
.proc action_update
        lda act_ready
        bne @ready
        jsr action_init                  ; 呼び忘れの保険。シーン開始で明示的に呼ぶのが正しい
@ready:
        inc act_lframe                   ; 論理フレーム（スロー中は実フレームより遅く進む）
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

        ; 飛び道具は**人の更新の後**に進める。撃たれたその瞬間のフレームにも
        ; 1歩ぶん飛ぶので、銃口に1フレーム貼り付いて見えることがない。
        jsr act_proj_update

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
        sta act_vel, x                   ; 倒れたら走りも止まる
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
        sta act_dsub, x
        sta act_vel, x                   ; 復帰は「何も押していない・止まっている」から始める
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
        jsr act_shift_right              ; **門を通さない**（攻撃中でも吹き飛ぶ）
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
        jsr act_shift_left               ; **門を通さない**（攻撃中でも吹き飛ぶ）
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

; X = エンティティ番号。「今フレーム動いた」印を立てる。A を壊す。
.proc act_mark_moved
        lda act_flags, x
        ora #ACT_F_MOVED
        sta act_flags, x
        rts
.endproc

; X = エンティティ番号 → キャリークリア = 自分の足で動いてよい /
;                        キャリーセット = 攻撃中なので**その場に根が生える**。
; A を壊す。X と Y は保存して返る。
;
; 攻撃の3相（発生・持続・硬直）の間は動けない。これは commit の一部である:
; 溜めている間に踏み込まれたら、当たる位置も一緒に動いてしまう。
; **ノックバックはここを通らない**（act_shift_* を直に呼ぶ）。殴られて吹き飛ぶのは
; 「自分の足で動く」ことではないからである。
.proc act_can_move
.if ::ACT_ATTACK_ROOT
        lda ent_state, x
        cmp #ACT_ST_ATK_START
        bcc @ok                          ; ACT_ST_IDLE
        cmp #ACT_ST_ATK_RECOVER + 1
        bcs @ok                          ; のけぞり以上（動かす主体はもう自分ではない）
        sec
        rts
.endif
@ok:
        clc
        rts
.endproc

; X = エンティティ番号, act_px = ドット数。**自分の足で**左右へ動く入口。
; プレイヤーの移動と AI の移動はどちらもここを通る（攻撃中の扱いを1箇所にするため）。
; 攻撃中は動かず、「今フレーム動いた」印も立たない（歩きの絵にならない）。
.proc act_move_left
        jsr act_can_move
        bcs @rooted
        jsr act_mark_moved
        jmp act_shift_left
@rooted:
        rts
.endproc

.proc act_move_right
        jsr act_can_move
        bcs @rooted
        jsr act_mark_moved
        jmp act_shift_right
@rooted:
        rts
.endproc

; X = エンティティ番号 → A = |act_vel|（速さの大きさ）。X と Y は保存して返る。
; 歩きか小走りかの判定（絵のコマ送り・向きの変え方・攻撃に移れるか）はすべてこれで見る。
.proc act_speed_mag
        lda act_vel, x
        bpl @done
        eor #$FF
        clc
        adc #1
@done:
        rts
.endproc

; X = エンティティ番号, act_px = ドット数。ワールドの左端 (0) を越えさせない。
; 越えると 16bit が巻き取って「ステージの遥か右」になり、カメラが飛ぶ。
; **門も印も通らない生の移動**である（ノックバックと飛び道具が使う）。
.proc act_shift_left
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
; **門も印も通らない生の移動**である（ノックバックと飛び道具が使う）。
.proc act_shift_right
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
; 提示 — いま何が起きているかを画面に出す（ADR-0006 の UI 要件 ＋ P1 の受入指摘）
; ==========================================================================
; **姿勢（絵）が主で、色は補助である。**
;
; P1 の受入で主が指摘した「攻撃とダメージ硬直の違いがわからない」は、
; 姿勢が1つしか無く、攻撃中も被弾中も同じ絵だったことによる。
; 6姿勢（立ち・歩き・構え・斬り・のけぞり・ダウン）が入ったので、状態ごとに
; ent_set_pose で絵を切り替える。割り当ては action_params.s の act_pose_by_state。
; **斬りは前へ、のけぞりは後ろへ折れる**ので、同じ「止まっている」フレームでも
; 攻撃の硬直か殴られた硬直かが見分けられる。
;
; 色は「本来の色 ⇄ ACT_PAL_BLINK」の点滅だけに使う。無敵中とダウン中（猶予中）で
; 点滅の速さが違い、猶予の残りが ACT_DOWN_WARN を切るとさらに速くなる。
; **無反応は不具合と区別がつかない**（ADR-0006）ので、猶予の残り時間は速さで見せる。
; 数値表示などの本格的な UI は P8。
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

; X = エンティティ番号。姿勢を決めてから色を決める。X は保存して返る。
.proc act_present
        jsr act_pose_for_state
        ; 「今フレーム動いた」は**読んだ直後に下ろす**。ここで下ろすのが要点で、
        ; フレームの頭で下ろすと AI が動かす仲間・敵が歩いて見えない
        ; （ai_update は action_update の**後**に走るので、印が立つのは提示の後になる）。
        ; 読んでから下ろせば、操作キャラは同じフレーム・AI 側は次のフレームに歩きが出る。
        lda act_flags, x
        and #<~ACT_F_MOVED
        sta act_flags, x
        lda ent_state, x
        cmp #ACT_ST_OUT
        beq @out
        cmp #ACT_ST_DOWN
        beq @down
        lda act_invuln, x
        bne @invuln
.if ::ACT_SHOW_TELL
        lda ent_state, x
        cmp #ACT_ST_ATK_START
        beq @tell
.endif
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
        lda #ACT_PAL_ACTIVE              ; 攻撃判定が出ているフレーム（既定では使わない）
        jmp act_set_pal
.endif
.if ::ACT_SHOW_TELL
; 予備動作中（溜めている最中）。**長い振りだけ**点滅させる。
; 「溜めているのが見える」ことは、避けられる攻撃の要件そのものである。
; 短い振り（操作キャラの初段は4フレーム）まで点滅させると画面が騒がしくなるだけなので、
; その行の発生フレーム数が ACT_TELL_MIN 以上のときだけ光る。
@tell:
        jsr act_row_for
        lda atk_startup, y
        cmp #ACT_TELL_MIN
        bcc @base
        lda #ACT_BLINK_TELL
        and frame_counter
        beq @base
        lda #ACT_PAL_TELL
        jmp act_set_pal
.endif
@invuln:
        lda #ACT_BLINK_HURT
        and frame_counter
        beq @base
        lda #ACT_PAL_BLINK
        jmp act_set_pal
@out:
        lda #ACT_PAL_OUT                 ; 猶予切れ。点滅を止めて「もう終わった」を示す
        jmp act_set_pal
@down:
        lda act_timer, x                 ; 残り猶予が少ないほど速く点滅する
        cmp #ACT_DOWN_WARN
        lda #ACT_BLINK_SLOW
        bcs @blink
        lda #ACT_BLINK_FAST
@blink:
        and frame_counter
        beq @base                        ; 点滅の片側は**本来の色**（他の役割の色を借りない）
        lda #ACT_PAL_BLINK
        jmp act_set_pal
.endproc

; X = エンティティ番号。いまの状態に対応する姿勢を engine に渡す。X は保存して返る。
;
; 待機状態（ACT_ST_IDLE）だけは、表の1項では足りない。立っているのか歩いているのかは
; 状態ではなく「今フレーム動いたか」（ACT_F_MOVED）で決まるためである。
; 印を下ろすのは呼び出し元（act_present）。**読んだ後に下ろす**（理由はそちらに書いた）。
; 歩きの絵は1枚しか無いので、立ちと交互に出して2コマの歩行にしている（ACT_WALK_ANIM）。
.proc act_pose_for_state
        ldy ent_state, x
        cpy #ACT_ST_COUNT
        bcs @done                        ; 語彙の外。表の外を引かないための保険
        lda act_pose_by_state, y
        cpy #ACT_ST_IDLE
        bne @set
        lda act_flags, x
        and #ACT_F_MOVED
        beq @stand
        ; 小走りの絵は無い（chr/ は範囲外）。**コマ送りを速くして強弱を見せる。**
        ; 歩く速さを超えている間だけ周期が半分になる。
        jsr act_speed_mag
        cmp #ACT_MOVE_SPEED + 1
        lda #ACT_WALK_ANIM
        bcc :+
        lda #ACT_RUN_ANIM
        ; 歩き・小走りのコマ送りだけは**論理フレーム**で数える。移動量も論理フレームで
        ; 積むので、実フレームで数えるとスロー中に「進む距離：脚の運び」が 1:2 にずれる。
        ; 点滅（act_present の3箇所）は逆に frame_counter のままである。理由は act_lframe の宣言に。
:       and act_lframe
        beq @stand
        lda #ACT_POSE_MOVE
        jmp @set
@stand:
        lda act_pose_by_state + ACT_ST_IDLE
@set:
        jmp ent_set_pose
@done:
        rts
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
