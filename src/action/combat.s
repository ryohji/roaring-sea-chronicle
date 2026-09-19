; combat.s — 攻撃の相（発生・持続・硬直）、当たり判定、ダメージ。
;
; 責務:
;   * 攻撃矩形の組み立て（向き × 段数の表から。数値は action_params.s）
;   * **奥行き方向の攻撃の可否**（Y許容幅 act_depth_tol。ADR-0009。P1 の受入条件）
;   * 命中時のヒットストップ・ノックバック・無敵の付与
;   * **攻撃の届く範囲と当たった位置を画面に出す**（斬り／衝撃の効果スプライト）
;
; engine の当たり判定プリミティブ（rect_overlap / depth_distance）と、効果スプライトの
; 枠（fx_spawn）を**使う側**である。判定ルール（Y何ドットまで届くか・何段目が何ドットか）と
; 「いつ何を出すか」は action の持ち分であり、engine には持ち込まない
;（src/engine/collide.s / src/engine/fx.s の冒頭コメント）。
;
; **レーンは ADR-0009 で廃止した。** 奥行きは連続なので、
; 「同じレーンか」ではなく「足元Yがどれだけ近いか」で判定する。
; レーン補間中の救済（出発レーンにも目標レーンにも居るとみなす分岐）は、
; 補間という状態そのものが無くなったので**丸ごと消えた**。
;
; rect_overlap は A・tmp0・tmp1 を壊す。depth_distance は tmp0 を壊す。
; したがって走査の途中状態は tmp に置かず、actor.s の BSS に置く。
.include "constants.inc"
.include "zeropage.inc"
.include "action.inc"
.include "action_params.inc"

.export act_attack_step, act_start_attack, act_damage

.import ent_active, ent_state, ent_body, ent_y
.import ent_x_lo, ent_x_hi
.import rect_overlap, depth_distance, fx_spawn
.import act_hp, act_timer, act_step, act_face, act_invuln, act_hitstop
.import act_kb, act_flags
.import act_px, act_t0, act_atk_ent, act_scan_end
.import act_kb_dir, act_kb_amt, act_dmg_amt
.import act_dy, act_tol
.import act_enter_down
.import act_hurt_w, act_hurt_h, act_depth_tol
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
        jsr act_set_phase
.if ::ACT_SHOW_SWIPE
        jmp act_spawn_swipe              ; 判定が出た瞬間に、その位置へ斬りを出す
.else
        rts
.endif
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
        jsr act_depth_ok                 ; 奥行きを先に見る（矩形より安い）
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

; **奥行き方向の攻撃の可否**（ADR-0009）。X = 防御側, act_atk_ent = 攻撃側。
;   出力: キャリーセット = 届く / キャリークリア = 届かない
;
; 判定は「足元Yの差が許容幅以内か」だけである。レーン番号の一致ではない。
; 許容幅は体格型ごとの act_depth_tol（= 絵の縦サイズの約1/3。action_params.s）から引く。
;
; 攻撃側と被弾側で体格が違うときは**両者の平均**を採る（ACT_DEPTH_TOL_PAIR = 0）。
; 被弾側だけで決めると「大きい相手には広く、小さい相手には狭い」が極端になり、
; 攻撃側だけで決めると相手の大きさが判定に出ない。平均はその中間である。
;
; depth_distance は tmp0 を壊し、X は壊さない（エンティティ番号を持ったまま呼べる）。
.proc act_depth_ok
        ldy act_atk_ent
        lda ent_y, y
        tay                              ; Y = 攻撃側の足元Y（**値**であって番号ではない）
        lda ent_y, x                     ; A = 被弾側の足元Y
        jsr depth_distance               ; A = |足元Yの差|
        sta act_dy
        ldy ent_body, x                  ; 被弾側の許容幅
        lda act_depth_tol, y
.if ::ACT_DEPTH_TOL_PAIR = 0
        sta act_tol
        ldy act_atk_ent                  ; 攻撃側の許容幅
        lda ent_body, y
        tay
        lda act_depth_tol, y
        clc
        adc act_tol
        lsr a                            ; 両者の平均
.endif
        cmp act_dy                       ; 許容幅 >= 差 なら キャリーセット = 届く
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
.if ::ACT_SHOW_IMPACT
        jsr act_spawn_impact             ; 当たった位置を画面に出す（距離感の手がかり）
.endif
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

; ==========================================================================
; 効果スプライト — **攻撃の届く範囲を画面に出す**
; ==========================================================================
; P1 の受入で主が指摘した「攻撃の距離感が掴めません」への直接の対策である。
; 当たり判定を一切描いていなかったので、どこまで届くのかが画面から分からなかった。
;
; 出すのは2つだけ。
;   斬り（SPR_TILE_SWIPE）  … 判定が出ている間、**攻撃矩形の中心**に出す。届く範囲の提示
;   衝撃（SPR_TILE_IMPACT） … 命中した瞬間、**相手の体の中心**に出す。当たった位置の提示
;
; 枠と寿命は engine（src/engine/fx.s）が持つ。**寿命が尽きたら engine が消すので
; 後始末は要らない。**枠が空いていなければ黙って出ない（効果は消えてよい）。
; 出す／出さないは action_params.inc の ACT_SHOW_SWIPE / ACT_SHOW_IMPACT で切れる。

.if ::ACT_SHOW_SWIPE
; X = 攻撃側。攻撃矩形の中心に斬りを出す。X は保存して返る。
;
; 寿命を「持続フレーム数 + 1」にしてあるのは、判定が出た**フレームの終わり**にここへ
; 来るのに対し、当たり判定の走査は次のフレームから始まるためである。
; +1 しないと、最後の走査フレームだけ絵が消えている状態になる。
.proc act_spawn_swipe
        jsr act_build_attack_rect        ; 判定と同じ手続きで組む（絵と判定がずれない）
        ; --- X = 矩形の中心。8 ドット幅の絵の中心を矩形の中心に合わせる ---
        lda rect_a + RECT_W
        lsr a
        sec
        sbc #SPRITE_W / 2
        bcs :+
        lda #0                           ; 矩形が絵より狭いとき
:       clc
        adc rect_a + RECT_X_LO
        sta fx_arg_x_lo
        lda rect_a + RECT_X_HI
        adc #0
        sta fx_arg_x_hi
        ; --- Y = 矩形の縦の中心。fx は足元Yで置くので絵の高さの半分を足す ---
        lda rect_a + RECT_H
        lsr a
        sta act_t0
        lda ent_y, x
        sec
        sbc act_t0
        clc
        adc #SPRITE_H / 2
        sta fx_arg_y
        lda #SPR_TILE_SWIPE
        sta fx_arg_tile
        lda #SPR_PAL_EFFECT
        ldy act_face, x
        beq :+
        ora #ENT_ATTR_HFLIP              ; 左向きの攻撃は絵も裏返す
:       sta fx_arg_attr
        ldy act_step, x
        dey
        lda atk_active, y
        clc
        adc #1
        sta fx_arg_life
        jmp fx_spawn
.endproc
.endif

.if ::ACT_SHOW_IMPACT
; X = 被弾側。相手の体の中心に衝撃を出す。X は保存して返る。
.proc act_spawn_impact
        ldy ent_body, x
        lda act_hurt_w, y
        lsr a
        sec
        sbc #SPRITE_W / 2
        bcs :+
        lda #0
:       clc
        adc ent_x_lo, x
        sta fx_arg_x_lo
        lda ent_x_hi, x
        adc #0
        sta fx_arg_x_hi
        lda act_hurt_h, y
        lsr a
        sta act_t0
        lda ent_y, x
        sec
        sbc act_t0
        clc
        adc #SPRITE_H / 2
        sta fx_arg_y
        lda #SPR_TILE_IMPACT
        sta fx_arg_tile
        lda #SPR_PAL_EFFECT
        sta fx_arg_attr
        lda #ACT_FX_IMPACT_LIFE
        sta fx_arg_life
        jmp fx_spawn
.endproc
.endif
