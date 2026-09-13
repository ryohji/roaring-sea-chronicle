; combat.s — 攻撃の相（発生・持続・硬直）、当たり判定、ダメージ。
;
; 責務:
;   * 攻撃矩形の組み立て（向き × 段数の表から。数値は action_params.s）
;   * **レーン間攻撃の可否**（ACT_LANE_REACH。P1 の受入条件）
;   * 命中時のヒットストップ・ノックバック・無敵の付与
;
; engine の当たり判定プリミティブ（rect_overlap / lane_distance）を**使う側**である。
; 判定ルール（どのレーンまで届くか・何段目が何ドットか）は action の持ち分であり、
; engine には持ち込まない（src/engine/collide.s の冒頭コメント）。
;
; rect_overlap は A・tmp0・tmp1 を壊す。lane_distance は tmp0 を壊す。
; したがって走査の途中状態は tmp に置かず、actor.s の BSS に置く。
.include "constants.inc"
.include "zeropage.inc"
.include "action.inc"
.include "action_params.inc"

.export act_attack_step, act_start_attack, act_damage

.import ent_active, ent_state, ent_body, ent_y
.import ent_x_lo, ent_x_hi, ent_lane, ent_lane_from, ent_lane_step
.import rect_overlap, lane_distance
.import act_hp, act_timer, act_step, act_face, act_invuln, act_hitstop
.import act_kb, act_flags
.import act_px, act_t0, act_atk_ent, act_scan_end
.import act_kb_dir, act_kb_amt, act_dmg_amt
.import act_ld0, act_ld1, act_la0, act_la1
.import act_enter_down
.import act_hurt_w, act_hurt_h
.import atk_startup, atk_active, atk_recover
.import atk_reach, atk_w, atk_h, atk_dmg, atk_kb

.segment "CODE"

; ==========================================================================
; 攻撃の状態機械
; ==========================================================================

; X = エンティティ番号, A = 段数（1..ACT_COMBO_MAX）。攻撃を開始する。
.proc act_start_attack
        sta act_step, x
        tay
        dey                              ; Y = 表の添字（段数 - 1）
        lda #ACT_ST_ATK_START
        sta ent_state, x
        lda #0
        sta act_flags, x
        lda atk_startup, y
        jmp act_set_phase
.endproc

; X = エンティティ番号, A = 相のフレーム数。0 を 1 に読み替えてから入れる。
; 表を 0 にされると「1減らして 0 なら次へ」が 255 フレームに化けるための安全柵。
.proc act_set_phase
        bne :+
        lda #1
:       sta act_timer, x
        rts
.endproc

; X = エンティティ番号。攻撃中の1フレーム。X は保存して返る。
.proc act_attack_step
        lda ent_state, x
        cmp #ACT_ST_ATK_ACTIVE
        bne @tick
        lda act_flags, x
        and #ACT_F_HIT
        bne @tick                        ; この振りでは既に当てた
        jsr act_hit_scan
        lda act_hitstop, x
        bne @done                        ; 今フレーム当たった。相の進行も止める（手応え）
@tick:
        dec act_timer, x
        beq @advance
@done:
        rts
@advance:
        lda ent_state, x
        cmp #ACT_ST_ATK_START
        beq @to_active
        cmp #ACT_ST_ATK_ACTIVE
        beq @to_recover
        lda #ACT_ST_IDLE                 ; 硬直が明けた
        sta ent_state, x
        lda #0
        sta act_step, x
        rts
@to_active:
        lda #ACT_ST_ATK_ACTIVE
        sta ent_state, x
        ldy act_step, x
        dey
        lda #0
        sta act_flags, x
        lda atk_active, y
        jmp act_set_phase
@to_recover:
        lda #ACT_ST_ATK_RECOVER
        sta ent_state, x
        ldy act_step, x
        dey
        lda atk_recover, y
        jmp act_set_phase
.endproc

; ==========================================================================
; 当たり判定
; ==========================================================================

; X = 攻撃側エンティティ番号。判定が出ているフレームに呼ぶ。X は保存して返る。
; 同じフレームに矩形が重なっている相手は全員に当たる。その後この振りは終わり。
.proc act_hit_scan
        stx act_atk_ent
        jsr act_build_attack_rect
        jsr act_target_range             ; X = 走査の先頭, act_scan_end = 終端
@loop:
        cpx act_scan_end
        bcs @done
        lda ent_active, x
        beq @next
        lda ent_state, x
        cmp #ACT_ST_DOWN
        bcs @next                        ; ダウン中・猶予切れには当たらない
        lda act_invuln, x
        bne @next                        ; 無敵時間中
        jsr act_lane_ok                  ; レーンを先に見る（矩形より安い）
        bcc @next
        jsr act_build_hurt_rect
        jsr rect_overlap                 ; engine のプリミティブ（辺が接するだけは非重なり）
        bcc @next
        jsr act_hit
@next:
        inx
        jmp @loop
@done:
        ldx act_atk_ent
        rts
.endproc

; act_atk_ent の立場から、殴る相手の区画を決める。
;   出力: X = 先頭の添字, act_scan_end = 終端（この番号は含まない）
.proc act_target_range
        lda act_atk_ent
        cmp #ENT_ENEMY_FIRST
        bcs @enemy_attacks
        lda #ENT_ENEMY_FIRST + ENT_ENEMY_COUNT
        sta act_scan_end
        ldx #ENT_ENEMY_FIRST             ; 操作キャラ・仲間 → 敵区画
        rts
@enemy_attacks:
        lda #ENT_ENEMY_FIRST
        sta act_scan_end
        ldx #ENT_PLAYER                  ; 敵 → 操作キャラ・仲間の区画
        rts
.endproc

; **レーン間攻撃の可否**。X = 防御側, act_atk_ent = 攻撃側。
;   出力: キャリーセット = 届く / キャリークリア = 届かない
;
; ACT_LANE_REACH（action_params.inc）が 0 なら同一レーンのみ、1 なら隣接レーンまで。
; レーン補間中（ent_lane_step != 0）は「出発レーンにも目標レーンにも居る」とみなす。
; そうしないと、レーンを移りながらの攻撃が LANE_MOVE_FRAMES のあいだ必ず空振りになる。
.proc act_lane_ok
        lda ent_lane, x
        sta act_ld0
        sta act_ld1
        lda ent_lane_step, x
        beq @attacker
        lda ent_lane_from, x
        sta act_ld1
@attacker:
        ldy act_atk_ent
        lda ent_lane, y
        sta act_la0
        sta act_la1
        lda ent_lane_step, y
        beq @test
        lda ent_lane_from, y
        sta act_la1
@test:
        lda act_ld0
        ldy act_la0
        jsr @dist
        bcc @ok
        lda act_ld0
        ldy act_la1
        jsr @dist
        bcc @ok
        lda act_ld1
        ldy act_la0
        jsr @dist
        bcc @ok
        lda act_ld1
        ldy act_la1
        jsr @dist
        bcc @ok
        clc
        rts
@ok:
        sec
        rts
; A = レーン, Y = レーン → キャリークリアなら届く距離
@dist:
        jsr lane_distance                ; engine のプリミティブ（X は壊さない）
        cmp #ACT_LANE_REACH + 1
        rts
.endproc

; X = 攻撃側。攻撃矩形を rect_a に組む。
;   右向き: 左端 = ワールドX + 体幅 + atk_reach
;   左向き: 右端 = ワールドX        - atk_reach
;   縦    : 足元から上へ atk_h
.proc act_build_attack_rect
        ldy act_step, x
        dey
        lda atk_w, y
        sta rect_a + RECT_W
        lda atk_h, y
        sta rect_a + RECT_H
        lda atk_reach, y
        sta act_px
        lda ent_y, x
        sec
        sbc rect_a + RECT_H
        sta rect_a + RECT_Y
        lda act_face, x
        bne @left
        ldy ent_body, x                  ; 体の前端 = ワールドX + 体幅
        lda act_hurt_w, y
        clc
        adc act_px
        sta act_px
        lda ent_x_lo, x
        clc
        adc act_px
        sta rect_a + RECT_X_LO
        lda ent_x_hi, x
        adc #0
        sta rect_a + RECT_X_HI
        rts
@left:
        lda act_px                       ; 左端 = ワールドX - atk_reach - 幅
        clc
        adc rect_a + RECT_W
        sta act_px
        lda ent_x_lo, x
        sec
        sbc act_px
        sta rect_a + RECT_X_LO
        lda ent_x_hi, x
        sbc #0
        sta rect_a + RECT_X_HI
        bcs @done
        lda #0                           ; ワールドの左端より左へは出さない（16bit の巻き取り防止）
        sta rect_a + RECT_X_LO
        sta rect_a + RECT_X_HI
@done:
        rts
.endproc

; X = 防御側。やられ矩形を rect_b に組む。体格型（ent_body）ごとの表から。
.proc act_build_hurt_rect
        lda ent_x_lo, x
        sta rect_b + RECT_X_LO
        lda ent_x_hi, x
        sta rect_b + RECT_X_HI
        ldy ent_body, x
        lda act_hurt_w, y
        sta rect_b + RECT_W
        lda act_hurt_h, y
        sta rect_b + RECT_H
        lda ent_y, x
        sec
        sbc act_hurt_h, y                ; 足元から上へ
        sta rect_b + RECT_Y
        rts
.endproc

; X = 被弾側。攻撃側（act_atk_ent）の段の値で殴る。
.proc act_hit
        ldy act_atk_ent
        lda act_flags, y
        ora #ACT_F_HIT                   ; この振りはもう当たった
        sta act_flags, y
        lda act_face, y
        sta act_kb_dir                   ; 吹き飛ぶ向きは攻撃側の向き
        lda act_step, y
        tay
        dey
        lda atk_kb, y
        sta act_kb_amt
        lda atk_dmg, y
        jmp act_damage
.endproc

; ==========================================================================
; ダメージ
; ==========================================================================
; X = 被弾側, A = ダメージ量。
; 事前に act_kb_dir（0=右へ / 1=左へ）, act_kb_amt, act_atk_ent を入れておくこと。
; ai-dev（敵の攻撃）からもここを呼ぶ。
.proc act_damage
        sta act_dmg_amt
        lda ent_active, x
        beq @done
        lda ent_state, x
        cmp #ACT_ST_DOWN
        bcs @done                        ; ダウン中・猶予切れには当たらない
        lda act_invuln, x
        beq @accept
@done:
        rts
@accept:
        ; --- ノックバック（速度を入れるだけ。運ぶのは actor.s）---
        lda act_kb_dir
        beq @kb_right
        lda act_kb_amt
        eor #$FF
        clc
        adc #1                           ; 左向きは負の速度
        jmp @kb_store
@kb_right:
        lda act_kb_amt
@kb_store:
        sta act_kb, x

        ; --- ヒットストップは攻撃側と被弾側の両方に乗せる ---
        lda #ACT_HITSTOP_FRAMES
        sta act_hitstop, x
        ldy act_atk_ent
        sta act_hitstop, y

        ; --- HP ---
        lda act_hp, x
        sec
        sbc act_dmg_amt
        bcc @down                        ; 借りが出た = 0 未満
        sta act_hp, x
        bne @hurt
@down:
        lda #0
        sta act_hp, x
        jmp act_enter_down               ; HP が尽きても即死亡ではない（ADR-0005）
@hurt:
        lda #ACT_ST_HURT
        sta ent_state, x
        lda #ACT_HURT_FRAMES
        sta act_timer, x
        lda #ACT_INVULN_FRAMES
        sta act_invuln, x
        lda #0
        sta act_step, x
        rts
.endproc
