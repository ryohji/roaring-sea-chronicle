; combat.s — 攻撃の相（発生・持続・硬直）、当たり判定、ダメージ。
;
; 責務:
;   * 攻撃矩形の組み立て（向き × **技表の行**から。数値は action_params.s）
;   * **技構成**（act_mset）— 操作キャラのコンボと敵の攻撃を別の行として引く
;   * **commit** — 振り始めた瞬間の足元Y・向きを固定し、溜めている間は追尾しない
;   * **奥行き方向の攻撃の可否**（Y許容幅 act_depth_tol。ADR-0009。P1 の受入条件）
;   * 命中時のヒットストップ・ノックバック・無敵の付与
;   * **飛び道具**（出る・飛ぶ・当たる・消える）。当たり判定は action の持ち分
;   * **攻撃の届く範囲と当たった位置を画面に出す**（斬り／衝撃の効果スプライト）
;
; **敵の攻撃を操作キャラのコンボ表から分離したのが、このファイルの一番大きな変更である。**
; 敵が操作キャラの1段目を借りていた頃、敵の発生は 4 フレーム（0.067秒）だった。
; 人間の反応の限界（12フレーム）より短い攻撃は**原理的に避けられない**。
; 避けられない攻撃の前では立ち位置を選ぶ意味が無く、空間が使われない。
; 予備動作を長くしただけでは足りず、**溜めている間に狙いが追尾しないこと**（commit）が
; 同じだけ重要である。追尾すると、踏み込んで避けても当たってしまう。
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
.export act_row_for, act_commit_hold
.export act_proj_fire, act_proj_update

.import ent_active, ent_state, ent_body, ent_y
.import ent_x_lo, ent_x_hi, ent_class, ent_tile, ent_attr, ent_ai
.import ent_activate, ent_find_free, ent_kill
.import rect_overlap, depth_distance, fx_spawn
.import act_hp, act_timer, act_step, act_face, act_invuln, act_hitstop
.import act_kb, act_flags, act_sub, act_dsub, act_vel
.import act_mset, act_cy, act_proj_owner, act_attr0
.import act_px, act_t0, act_t1, act_t2, act_atk_ent, act_scan_end
.import act_kb_dir, act_kb_amt, act_dmg_amt
.import act_dy, act_tol
.import act_enter_down, act_shift_left, act_shift_right
.import act_hurt_w, act_hurt_h, act_depth_tol
.import act_mset_base, act_mset_len
.import atk_startup, atk_active, atk_recover
.import atk_reach, atk_w, atk_h, atk_dmg, atk_kb
.import atk_proj, atk_speed, atk_life

.segment "CODE"

; ==========================================================================
; 攻撃の状態機械
; ==========================================================================

; X = エンティティ番号 → Y = 技表の行（ATK_ROW_*）。A を壊す。X は保存して返る。
;
; 行 = その者の技構成の先頭 + 段数 - 1。**段数と行を分けたことが、
; 「敵の攻撃を操作キャラのコンボ表から分離する」ことの実体である。**
; 呼び出し側（ai-dev を含む）は今までどおり「段数」で振れる。
.proc act_row_for
        lda act_mset, x
        tay
        lda act_mset_base, y
        clc
        adc act_step, x
        sec
        sbc #1                           ; 段数は 1 から数える
        tay
        rts
.endproc

; X = エンティティ番号, A = 段数（1..）。攻撃を開始する。
; 段数がその技構成の持つ段数を超えていたら丸める（敵の技構成は1段しかない）。
;
; **ここが commit の起点である。** 振り始めた瞬間の足元Y（＝狙う奥行き）と向きを
; 写し取り、攻撃が終わるまでそれを保つ。溜めている間に追尾すると、
; 踏み込んで避けても当たってしまい、予備動作を長くした意味が消える。
.proc act_start_attack
        pha
        lda act_mset, x
        tay
        pla
        cmp act_mset_len, y
        bcc @step
        beq @step
        lda act_mset_len, y              ; 段数を丸める
@step:
        sta act_step, x
        lda #ACT_ST_ATK_START
        sta ent_state, x

        ; --- commit: 狙う奥行き（足元Y）と向きを固定する ---
        lda ent_y, x
        sta act_cy, x
        lda #0
        sta act_vel, x                   ; 振ったら走りは止まる（根が生える）
        ldy act_face, x
        beq @right
        lda #ACT_F_CFACE
@right:
        sta act_flags, x                 ; ACT_F_HIT / ACT_F_MOVED は下ろす

        jsr act_row_for
        lda atk_startup, y
        jmp act_set_phase
.endproc

; X = エンティティ番号。commit した狙いを保つ。攻撃中の毎フレーム、
; 判定を見る**前に**呼ぶ。A と Y を壊す。X は保存して返る。
;
; 誰が足元Yや向きを触っても、振りは**振り始めた位置・向き**のまま出る。
; ai 側は攻撃中のエンティティに手を出さない作りだが、commit を
; 「相手の作法」に依存させない。ここが崩れると避ける遊びが丸ごと崩れる。
.proc act_commit_hold
        lda act_cy, x
        sta ent_y, x
        lda act_flags, x
        and #ACT_F_CFACE
        beq @right
        lda #1
@right:
        sta act_face, x
        rts
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
        jsr act_commit_hold              ; 振り始めに固定した狙いを保つ（追尾しない）
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
        lda act_flags, x
        and #<~ACT_F_HIT                 ; 当たり直しを許す（commit した向きは残す）
        sta act_flags, x
        jsr act_row_for
        lda atk_active, y
        jsr act_set_phase
        jsr act_row_for
        lda atk_proj, y
        bmi @melee                       ; ATK_NO_PROJ = $FF。素手の行
        ; 飛び道具を出す行。**矩形の判定は出さない**（至近で二重に当たるため）。
        ; 出た印として ACT_F_HIT を立て、この振りの走査を丸ごと飛ばす。
        jsr act_proj_fire
        lda act_flags, x
        ora #ACT_F_HIT
        sta act_flags, x
        rts
@melee:
.if ::ACT_SHOW_SWIPE
        jmp act_spawn_swipe              ; 判定が出た瞬間に、その位置へ斬りを出す
.else
        rts
.endif
@to_recover:
        lda #ACT_ST_ATK_RECOVER
        sta ent_state, x
        jsr act_row_for
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
        ; act_scan_targets へ落ちる
.endproc

; rect_a と act_atk_ent を詰めてから呼ぶ。攻撃側の立場から標的の区画を決めて走査する。
; 飛び道具もここを通る（矩形の組み立て方だけが違う）。
; 出口で X = act_atk_ent に戻る。
.proc act_scan_targets
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
;
; **飛び道具は「撃った主の立場」で決める。** 弾はエンティティの空き枠に入るので
; 番号だけで見ると必ず敵区画より後ろになり、味方が撃った弾が味方を狙ってしまう。
; 撃った主は act_proj_owner（空き枠1つにつき1バイト）が覚えている。
.proc act_target_range
        lda act_atk_ent
        cmp #ENT_FREE_FIRST
        bcc @by_slot
        tay
        lda act_proj_owner - ENT_FREE_FIRST, y
@by_slot:
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
        jsr act_row_for
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
        txa
        pha                              ; 被弾側の番号を退避（行を引く間 X を貸す）
        ldx act_atk_ent
        jsr act_row_for
        pla
        tax
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
        ; 殴られたら走りは止まる。ノックバックと自走が同時に乗ると、
        ; 吹き飛びの距離が「そのとき走っていたか」で変わって読めなくなる。
        lda #0
        sta act_vel, x

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
        jsr act_row_for
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

; ==========================================================================
; 飛び道具 —— 出る・飛ぶ・当たる・消える
; ==========================================================================
; **当たり判定は action の持ち分**なので、弾の進行と判定もここにある
; （ai-dev が書くのは「いつ撃つか」だけである）。
;
; 弾はエンティティの**空き枠**（ENT_FREE_FIRST..MAX_ENTITIES = 7枠）に入る。
; 短命の効果（src/engine/fx.s）ではなく本物のエンティティにしてあるのは、
;   * 当たり判定を持つ（fx は判定を持たない道具である）
;   * OAM の並べ替えに乗る＝奥行きの前後関係が正しく描かれる
;   * 優先度クラス SPR_CLASS_PROJECT を名乗れる。ADR-0002 が
;     「当たり判定があるのに見えないのが最も理不尽」として操作キャラの次に置いたクラス
; の3つによる。
;
; 枠が無いときは**黙って出ない**。撃った側の振りは成立し、弾だけが出ない。
; ここで詰まって撃てなくなるより、たまに弾が出ない方が害が小さい。
;
; **絵は衝撃（SPR_TILE_IMPACT）の流用である**（ACT_PROJ_TILE）。
; 専用の絵が入ったら action_params.inc のその1行を差し替えればよい。

; X = 撃つ主体, Y = その者が振っている技表の行（atk_proj が弾の行を指していること）。
; X は保存して返る。枠が無ければ何もしない。
.proc act_proj_fire
        lda atk_proj, y
        sta act_t1                       ; 弾の行（ATK_ROW_*）
        jsr act_build_attack_rect        ; 銃口の位置。その行の reach / w が決める
        stx act_t2                       ; 撃つ主体
        ldx #ENT_FREE_FIRST
        ldy #ENT_FREE_COUNT
        jsr ent_find_free
        bcc @found
        ldx act_t2                       ; 空き枠なし。黙って出ない（弾は消えてよい）
        rts
@found:

        ; --- 位置。奥行きは**振り始めに固定した足元Y**（commit）である ---
        lda rect_a + RECT_X_LO
        sta ent_x_lo, x
        lda rect_a + RECT_X_HI
        sta ent_x_hi, x
        ldy act_t2
        lda act_cy, y
        sta ent_y, x
        lda act_face, y
        sta act_face, x                  ; 飛ぶ向きも commit した向きを継ぐ
        txa
        tay
        lda act_t2
        sta act_proj_owner - ENT_FREE_FIRST, y   ; 撃った主（狙う区画を決めるのに要る）

        ; --- 見た目 ---
        lda #SPR_CLASS_PROJECT
        sta ent_class, x
        lda #BODY_PLACEHOLDER            ; 奥行きのY許容幅を引くための体格
        sta ent_body, x
        lda #ACT_PROJ_TILE
        sta ent_tile, x
        lda #SPR_PAL_EFFECT
        sta ent_attr, x
        lda #ACT_ST_IDLE
        sta ent_state, x

        ; --- 付随テーブル。弾は「1段しかない技構成」を持つ者として判定を引く ---
        lda #0
        sta ent_ai, x
        sta act_sub, x
        sta act_dsub, x
        sta act_vel, x
        sta act_kb, x
        sta act_invuln, x
        sta act_hp, x
        sta act_cy, x
        lda #1
        sta act_step, x
        lda #ACT_MSET_BULLET
        sta act_mset, x
        lda #ACT_PROJ_ARM                ; 出たフレームは止まって見える（見えない判定を作らない）
        sta act_hitstop, x
        lda #ACT_F_PROJ
        sta act_flags, x
        ldy act_t1
        lda atk_life, y
        bne :+
        lda #1                           ; 0 だと寿命の減算が 255 に化けて消えなくなる
:       sta act_timer, x

        jsr ent_activate                 ; 帯へのクランプと先頭タイルの記憶は engine の持ち分
.if ::ACT_PROJ_SHADOW = 0
        lda ent_attr, x
        and #<~ENT_ATTR_SHADOW
        sta ent_attr, x
.endif
        lda ent_attr, x
        sta act_attr0, x                 ; 本来の色（提示が点滅から戻すときに読む）
        ldx act_t2
        rts
.endproc

; 毎フレーム1回。空き枠に居る弾だけを進める。
; **人の枠（ENT_PLAYER..ENT_ENEMY_*）はここを通らない。**
.proc act_proj_update
        ldx #ENT_FREE_FIRST
@loop:
        lda ent_active, x
        beq @next
        lda act_flags, x
        and #ACT_F_PROJ
        beq @next                        ; 空き枠に居る別のもの（将来のアイテムなど）
        jsr act_proj_step
@next:
        inx
        cpx #MAX_ENTITIES
        bcc @loop
        rts
.endproc

; X = 弾。寿命 → 移動 → 判定の順に1フレーム進める。X は保存して返る。
; 当たったフレームに消える（貫通しない）。壁は無いので、外れた弾は寿命で消える。
.proc act_proj_step
        lda act_hitstop, x
        beq @live
        dec act_hitstop, x               ; 出たばかり。まだ飛ばないし当たらない
        rts
@live:
        dec act_timer, x
        beq @expire

        jsr act_row_for
        lda act_sub, x
        clc
        adc atk_speed, y                 ; 速さは 1/16 ドット/フレーム
        pha
        and #$0F
        sta act_sub, x
        pla
        lsr a
        lsr a
        lsr a
        lsr a
        sta act_px
        beq @scan                        ; 今フレームは1ドットに満たない（判定は出す）
        lda act_face, x
        bne @left
        jsr act_shift_right              ; ステージ端のクランプ込み。止まっても寿命で消える
        jmp @scan
@left:
        jsr act_shift_left
@scan:
        stx act_atk_ent
        jsr act_build_proj_rect
        jsr act_scan_targets
        lda act_flags, x
        and #ACT_F_HIT
        beq @done
@expire:
        lda #0
        sta act_flags, x                 ; 飛び道具の印を下ろす（枠を次の弾に返す）
        jmp ent_kill
@done:
        rts
.endproc

; X = 弾。**弾の絵そのもの**を当たり判定にする（原点から幅 atk_w・足元から高さ atk_h）。
; 人の攻撃と違って体の前端から前へ伸ばさないのは、弾には「体」と「間合い」の区別が
; 無いからである。見えている絵と判定が一致している方が、避ける側から読みやすい。
.proc act_build_proj_rect
        jsr act_row_for
        lda atk_w, y
        sta rect_a + RECT_W
        lda atk_h, y
        sta rect_a + RECT_H
        lda ent_x_lo, x
        sta rect_a + RECT_X_LO
        lda ent_x_hi, x
        sta rect_a + RECT_X_HI
        lda ent_y, x
        sec
        sbc rect_a + RECT_H
        sta rect_a + RECT_Y
        rts
.endproc
