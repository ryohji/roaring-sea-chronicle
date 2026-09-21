; ai.s — 自律行動の意思決定ループ。**これ1本しか無い。**
;
; 責務:
;   * 状況評価（ai_sense）      … 場の状態を1フレームに1回まとめる
;   * 目標選択（ai_think_for）  … 標的を選び、立ち位置と目標を決める（分散実行）
;   * 行動  （ai_act_one）      … 決まった立ち位置へ歩き、奥行きを合わせ、届けば振る
;
; **型ごとの分岐をこのファイルに書いてはならない。**
; 型の違いは data/ai_params.tsv の行（速さ・間合い・攻撃性・追従距離・
; 奥行きの寄り足の速さ・回収の優先度）だけで出す。
; 「この型のときだけ〜する」と書きたくなったら、それは列が足りていないという意味である。
; 敵も同じループ・同じ表で動く（敵専用の思考を別に書かない）。
;
; 何をここに書かないか:
;   * ダメージ・ヒットストップ・ノックバック・ダウン … src/action/（act_start_attack →
;     act_hit_scan → act_damage が全部持っている。再実装しない）
;   * 矩形の交差・足元Yの距離・歩ける帯のクランプ … src/engine/（プリミティブを使う側である）
;   * キャラ名・話番号・家系名 … data/ 側（CLAUDE.md 第1条）
;
; ゼロページの tmp0〜tmp3 は engine の呼び出し（depth_distance など）をまたいで
; 保たれない。AI の状態も作業変数も、このファイルの BSS に置く。
;
; **レーンは ADR-0009 で廃止した。奥行きは連続である。**
; 「同じレーンか」で見ていた所は「足元Yが近いか」に、「1つ隣のレーンへ移る」で
; 見ていた所は「目標の足元Yへ不感帯まで寄る」に置き換わっている。
.include "constants.inc"
.include "zeropage.inc"
; action の語彙（ACT_ST_*）。ca65 の探索パスは -I src だけなので相対で指す。
; ここで欲しいのは「倒れているか」の境界 ACT_ST_DOWN であり、
; AI が状態の数値を自前で持つと action-dev が語彙を変えた日に黙って壊れる。
.include "../action/action.inc"
.include "ai.inc"
.include "ai_params.inc"
; 手触り確認のための門（dbg_ally）。**仕様ではない。**DBG_ENABLE = 0 で丸ごと消える。
.include "debug.inc"

.export ai_init, ai_init_entity, ai_update
.export ai_goal, ai_target, ai_gap, ai_flags, ai_timer
.export ai_react, ai_woff, ai_woffy, ai_wtim
.export ai_sit, ai_cursor, ai_rng

.import ent_active, ent_x_lo, ent_x_hi, ent_y, ent_state, ent_ai
.import ent_depth_move, depth_distance
.import act_role, act_hitstop, act_face, act_px
.import act_move_left, act_move_right, act_start_attack
.import ai_p_speed, ai_p_hold_x, ai_p_reach_x, ai_p_aggr_x
.import ai_p_follow_x, ai_p_leash_x, ai_p_atk_gap, ai_p_depth_spd
.import ai_p_hate, ai_p_react, ai_p_wander, ai_p_dwell
.import ai_default_profile

.if ::DBG_ENABLE
; engine（src/main.s）は SELECT で値を回すだけで、意味は解釈しない。
; **意味を決めるのはこのファイルである**（src/debug.inc の注記）。
.import dbg_ally
.endif

; 自律行動の対象になる区画（仲間 + 敵）。操作キャラ(0)と共用枠は含まない。
AI_FIRST = ENT_ALLY_FIRST
AI_END   = ENT_FREE_FIRST

; ai_dir（立ち位置を逃がす向き）に入れる値。0 = 左 / 1 = 右 /
; AI_DIR_NEAREST = 向きにこだわらない（いま立ち位置がある側＝近い方の外へ出す）。
; 調整値ではなく符号なので ai_params.inc ではなくここに置く。
AI_DIR_NEAREST = $FF

.if ::DBG_ENABLE
; --------------------------------------------------------------------------
; 仲間の確認モード（dbg_ally）の**意味**
; --------------------------------------------------------------------------
; 主が P1 の手触りを見るための仕掛けであり、遊びの仕様ではない。
; 主の言葉:「敵の攻撃を待つなどしている間に AI キャラクターが敵を殲滅してしまいます」。
; 既定（0）の挙動は**1ビットも変えない**。門は ai_act_one に1つ、
; 考え直しに1つ、敵の標的選びの入口に1つで、どれも走査の内側には無い。
;
;   0 … 通常（今までどおり）
;   1 … 攻撃しない。考え・歩き・追従し・揺らぐが、振るのだけをやめる
;        （敵の標的にはなる。「仲間が居るときの位置取り」を見たまま決着だけ止める）
;   2 … 休ませる。思考も移動も攻撃もしない。**敵の標的からも外れる**（実質1対3）
;        ただし操作キャラに押されて退くのだけは残す（止めると壁になる＝最優先の不具合）
; 門が名指しで見分けるのは「休ませる」だけである（それ以外の 0 でない値は
; 「攻撃しない」として扱う）。engine が段数を増やしても、増えた段は安全側＝
; 「攻撃しない」に倒れる。
DBG_ALLY_MUTE = 1
DBG_ALLY_REST = 2
.assert DBG_ALLY_REST < DBG_MODE_COUNT, error, "確認モードの段数が engine の巡回より多い"
.assert DBG_ALLY_MUTE < DBG_ALLY_REST, error, "門は「2 でなければ攻撃しない」で分けている"

; 「攻撃しない」を、**振るまでの間隔（atk_gap）を毎フレーム積み直す**ことで作る。
; ai_try_attack の内側に条件を撒かずに済み、構え・間合い・向きは通常のまま残る
; （型の個性のうち「どこに立つか」は見えたままで、「振る」だけが消える）。
; 1 では足りない: 門の直後に ai_tick_timers が1つ減らすので、同じフレームで 0 になる。
DBG_ALLY_MUTE_HOLD = 2
.endif

; 揺らぎを引く 8bit LFSR の種。**0 以外の固定値**であること
; （0 から出られない／種が動くと再現しなくなる）。値そのものに意味は無い。
AI_RNG_SEED = $A7

.segment "BSS"

; --- エンティティごとの状態（engine の SoA と同じ添字で引く）---
ai_goal:    .res MAX_ENTITIES   ; AI_GOAL_*
ai_target:  .res MAX_ENTITIES   ; 立ち位置の基準にする相手。AI_NONE なら無し
ai_gap:     .res MAX_ENTITIES   ; 標的から取りたい横距離（hold_x に分散ぶんを足した値）
ai_flags:   .res MAX_ENTITIES   ; AI_F_*
ai_timer:   .res MAX_ENTITIES   ; 次に振れるまでのフレーム数
ai_sub:     .res MAX_ENTITIES   ; 横移動の小数部（1/16 ドット）
ai_ysub:    .res MAX_ENTITIES   ; 奥行き移動の小数部（1/16 ドット）

; --- 棒立ちにしないための3つ組（主の指摘。ai_params.inc §4-b）---
; **どれも型ごとの分岐ではない。**値は TSV の react / wander / dwell 列から来る。
ai_react:   .res MAX_ENTITIES   ; 歩き出すまでに残っている遅れ（フレーム）
ai_woff:    .res MAX_ENTITIES   ; いま狙っている立ち位置のずれ（**符号つき**ドット・横）
ai_woffy:   .res MAX_ENTITIES   ; 同（奥行き）。横の半分。**交戦中は 0**（ai_plan）
ai_wtim:    .res MAX_ENTITIES   ; 次に ai_woff を引き直すまでのフレーム数

; --- 場の状態 ---
ai_sit:     .res 1              ; AI_SIT_*（状況評価の結果）
ai_cursor:  .res 1              ; 次に考え直すエンティティ番号（思考の分散実行）
ai_rethink: .res 1              ; 今フレームに残っている「臨時の考え直し」の回数
ai_rng:     .res 1              ; 揺らぎを引く手（8bit LFSR。種は固定＝再現する）

; --- 作業変数（engine の呼び出しをまたぐので tmp には置けない）---
ai_prof:    .res 1              ; いま処理している者のプロファイル行
ai_t0:      .res 1              ; 16bit 計算の作業（ai_dist16 が壊す）
ai_t1:      .res 1
ai_t2:      .res 1              ; ai_dist16 をまたいで保つ値
ai_t3:      .res 1
ai_t4:      .res 1              ; ai_post_gap の作業／走査位置の一時退避
ai_dir:     .res 1              ; 立ち位置を逃がす向き（0:左 / 1:右）
ai_dist:    .res 1              ; ai_dist16 の結果（横距離・255 で頭打ち）
ai_dsign:   .res 1              ; 同（0 = 相手は自分の右 / 1 = 左）。act_face と同じ意味
ai_cand:    .res 1              ; 標的選びの走査位置
ai_scan_end: .res 1             ; 同、終端（この番号は含まない）
ai_best:    .res 1              ; 同、いちばん良かった相手
ai_bestsc:  .res 1              ; 同、その重みつき距離
ai_px:      .res 1              ; 今フレームに進めるドット数（横・奥行きで共用）
ai_ydif:    .res 1              ; 標的との奥行きの隔たり
                                ;   （ai_depth_follow が測り、ai_try_attack が使い回す）
ai_urgent:  .res 1              ; 今この瞬間、**自分の体が**操作キャラの専有距離の中に居るか
                                ;   （1 なら歩き出しの遅れを飛ばして退く。ai_move_to_post）

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
        ; 揺らぎを引く手の種。**固定値である。**同じ操作をすれば必ず同じ挙動になる
        ; （0 を入れてはならない。LFSR は 0 から出られない）。
        lda #AI_RNG_SEED
        sta ai_rng
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
        sta ai_sub, x
        sta ai_ysub, x
        sta ai_react, x
        sta ai_woff, x
        sta ai_woffy, x
        sta ai_wtim, x           ; 0 = 最初に動くときその場で引き直す
        rts
.endproc

; ==========================================================================
; 1フレームの入口
; ==========================================================================
; main.s から毎フレーム1回、action_update の後に呼ぶ
; （被弾・ダウンの結果を見てから考えるため）。
; レーンの補間が無くなった（ADR-0009）ので、呼び出し順に関する制約は
; もうこれだけである。奥行きの移動は頼んだその場で足元Yに反映される。
;
; 費用の内訳（1フレームあたり）:
;   状況評価  1回（定数時間）
;   思考      AI_THINK_PER_FRAME 体ぶん（重い。標的の走査を含む）
;             ＋ 標的を失った者の臨時の考え直し AI_RETHINK_PER_FRAME 体ぶんまで
;   行動      自律枠の生存者ぶん（軽い。歩く・奥行きを合わせる・振る）
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
;
; **揺らぎを引く手をここで毎フレーム1歩進める。**引く瞬間にだけ進めると、
; 8bit LFSR は1歩でビットが1つずれるだけなので、**続けて引いた目が強く相関する**。
; 実測: 引く瞬間だけ進めた版では「真ん中」が4回続き、仲間が 430 フレーム（7.2 秒）
; 固まったまま立っていた（＝主の言う棒立ちが直っていなかった）。
; 毎フレーム進めれば、引き直しの間隔（80 フレーム前後）ぶん離れた目を引くので相関が切れる。
; 種は固定のままなので**再現性は失われない**（同じ操作なら必ず同じ並びになる）。
; 費用は1フレーム 10 サイクル強である。
.proc ai_sense
        jsr ai_rand              ; 揺らぎを引く手を1歩進める（下の注記）
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
.if ::DBG_ENABLE
        lda dbg_ally             ; 休ませている仲間は考え直さない（既定 0 は素通り）
        cmp #DBG_ALLY_REST
        bne :+
        cpx #ENT_ENEMY_FIRST
        bcc @rest                ; **敵には一切効かせない**（敵が黙ったら見る物が消える）
:
.endif
        jsr ai_load_profile
        jsr ai_select_target
        jmp ai_plan
.if ::DBG_ENABLE
@rest:
        rts                      ; 目標・立ち位置・揺らぎを**そのまま凍らせて**返る
.endif
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
; 「遠さ」は 横距離 + **足元Yの差**（どちらもドット）。奥行きが連続になった以上、
; 奥行きの差はドットでそのまま測れるので、重み（かつての AI_LANE_COST = 8 倍）は要らない。
; **ドット単位で 8 倍すると横距離を食い潰す**（レーン差は 0..3 だったが、
; 足元Yの差は歩ける帯の高さぶん＝数十ドットまで出る）。1ドットは1ドットとして足す。
;
; **ヘイト**（誰を狙うかの重み）は、その「遠さ」から (hate - AI_HATE_MID) ドットを引く形で
; 効かせる（ai_apply_hate）。ヘイトの高い者は**実際より近くに居る**ものとして測られるので、
; 距離と同じ物差しの上で一貫して比較できる。別の物差し（倍率・優先順位のテーブル）を
; 足さないのは、攻撃性 aggr_x が距離の物差しで書かれているからである。
; 128 が中立なので、**全行 128 のあいだは何も変わらない**（P1 がそれである）。
;
; 相手の側のプロファイルを引くので、走査のたびに1回だけ ent_ai を読む。
; 範囲外の ent_ai は 0 行目に倒す（ai_load_profile と同じ安全柵。表の外を引かせない）。
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
.if ::DBG_ENABLE
        ; 休ませている仲間（dbg_ally = 2）は**狙われない**。棒立ちの仲間に敵が
        ; 張りつくと、結局1対1の間合いが見られないからである（主の求め＝実質1対3）。
        ; 走査の**外**で終端を縮めるので、候補ごとの条件は1つも増えていない。
        ; 区画が [操作キャラ][仲間][敵] の順に並んでいるので、終端を仲間の手前に
        ; 置き直すだけで「操作キャラだけを見る」になる。
        ldy dbg_ally
        cpy #DBG_ALLY_REST
        bne @scan
        lda #ENT_ALLY_FIRST
.endif
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
        lda ent_y, y
        tay                      ; Y = 相手の足元Y
        lda ent_y, x
        jsr depth_distance       ; A = |足元Yの差|（ドット。重みを掛けない）
        clc
        adc ai_dist
        bcc :+
        lda #$FF                 ; 頭打ち
        ; --- ヘイト（狙われやすさ）。ここは**走査の内側**なので畳み込んである ---
        ; 遠さ' = 遠さ - (hate - AI_HATE_MID)。詳しくは下の注記を見よ。
:       sta ai_t3
        ldy ai_cand
        lda ent_ai, y
        cmp #AI_PROFILE_COUNT
        bcc :+
        lda #0                   ; 詰め忘れ・未初期化は 0 行目に倒す（表の外を引かせない）
:       tay
        lda #AI_HATE_MID
        sec
        sbc ai_p_hate, y
        bcs @repel               ; hate <= 128 … 遠く見せる（足す）
        clc                      ; hate >  128 … 近く見せる（引く。8bit では足し算になる）
        adc ai_t3
        bcs @scored              ; 256 を跨いだ＝引き算の答がそのまま A に居る
        lda #0                   ; 跨がない＝負。0 まで（いちばん近い）
        beq @scored              ; 常に成立
@repel:
        clc
        adc ai_t3
        bcc @scored
        lda #$FF                 ; 255 で頭打ち（これ以上遠くはひとまとめでよい）
@scored:
        cmp ai_bestsc
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

; --------------------------------------------------------------------------
; ヘイト（狙われやすさ）の算数 —— 上の走査に畳み込んである実体の説明
; --------------------------------------------------------------------------
;   遠さ' = 遠さ - (hate - AI_HATE_MID)
;
; hate = AI_HATE_MID(128) なら素通りである（**P1 は全行これなので挙動は何も変わらない**）。
; 大きいほど近くに見える＝狙われる（タンク）。小さいほど遠くに見える＝狙われにくい（遠隔）。
;
; 8bit のまま扱うために、まず d = (AI_HATE_MID - hate) を 8bit で作る:
;   hate <= 128 … d は 0..128 の**正**。遠さ + d（あふれたら 255 で頭打ち）
;   hate >  128 … d は 256-(hate-128)。遠さ + d は 256 を跨いだときだけ正しい答になる
;                  （跨がなければ答は負なので 0 に落とす）
; どちらも「遠さ + d」1回で済む。場合分けは sbc の桁借りがそのまま教えてくれる。
;
; **独立した手続きにして jsr で呼んではならない。**ここは候補の数だけ回る走査の
; 内側であり、jsr/rts の 12 サイクルが満員（候補6体 × 思考2回）で 144 サイクルになる。
; 実測でそれが1フレームの費用にそのまま乗った。

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
        jsr ai_clear_wander      ; **構えている間は揺らがない**（下の注記）
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
        jsr ai_set_woffy         ; 狙う相手が居ない。ぴったり並ぶ理由も無い
        jsr ai_apply_wander      ; **揺らぎ → 分散**の順（下の注記）
        jmp ai_spread
.endproc

; ==========================================================================
; 揺らぐのは「構えていないとき」だけである —— 型で分けているのではない
; ==========================================================================
; 揺らぎ（ai_woff / ai_woffy）を足すのは AI_GOAL_REGROUP の側だけで、
; AI_GOAL_ENGAGE では 0 に落とす。**目標で分けているのであって型ではない。**
;
; 理由は2つあり、どちらも「揺らぎが型の個性を壊す」という同じ話である:
;
;   1. 横（ai_woff）… 間合い hold_x と攻撃に移る閾値 reach_x の隙間は狭い
;      （万能型で 14 と 20 の 6 ドット）。ここに AI_DEADBAND より広い揺らぎは入らない。
;      入れると立ち位置が reach_x の外に出て、**構えたまま振らない**時間ができる。
;      実測: 交戦中の横距離の中央値が 15 → 19 ドット（reach_x = 20）に上がり、
;      1体倒すのに 717 → 760 フレームかかった。
;   2. 奥行き（ai_woffy）… 奥行きを合わせ切ったかどうかが**攻撃に移る入口**である
;      （ai_try_attack）。揺らすと永久に振らない仲間ができる。
;
; 揺らぎは「棒立ちに見えないため」のものであり、棒立ちが問題になるのは
; **狙う相手が居ないとき**である。構えている間は型の間合いどおりに立てばよい。
;
; **分散（ai_spread）より前に足すこと。**分散は「同じ所に寄る者どうしを番号順に
; 引き離す」保証であり、その後に揺らぎを足すと保証を壊す。
.proc ai_clear_wander
        lda #0
        sta ai_woffy, x
        rts
.endproc

; ==========================================================================
; 棒立ちにしない —— 止まる位置を揺らす
; ==========================================================================
; X = 自分。決まった間合い（ai_gap）に、いま引いてある揺らぎ（ai_woff）を足す。
;
; 主の指摘: 「プレイヤーキャラクターと Y を揃えて等距離を保つという動きが、
; あまりに機械的 ── プレイヤーから等距離を移動するモノという印象を与えています」。
; そう見えたのは、**追従距離ちょうどに吸着していた**からである。
; ここで間合いそのものを数ドット外す。外し方は何秒かに一度しか変わらないので、
; 画面では「そこまで歩いて、しばらくそこに居た」という一つの意図として読める。
;
; 揺らぎは**間合いの側**に足す。実際の歩きの側（ai_move_to_post）に足すと、
; 毎フレーム目標がぶれて足踏みになる。ここなら目標が静かに数ドット動くだけである。
;
; 下限 AI_GAP_MIN で止める。**操作キャラとの間の下限はここではない**
; （ai_clear_player_zone が立ち位置そのものに対して持っている。壁にならない規則は
;  揺らぎより後に効くので、内向きの揺らぎが専有距離を食い破ることはない）。
.proc ai_apply_wander
        lda ai_woff, x
        beq @done                ; 揺らぎ無し（wander = 0 の型もここを通る）
        bmi @inward
        clc                      ; 外向き
        adc ai_gap, x
        bcc @store
        lda #$FF                 ; 255 で頭打ち
        bne @store               ; 常に成立
@inward:
        clc                      ; 内向き（ai_woff は符号つき。足せばよい）
        adc ai_gap, x
        bcc @floor               ; 桁借り = 0 を下回った
        cmp #AI_GAP_MIN
        bcs @store
@floor:
        lda #AI_GAP_MIN
@store:
        sta ai_gap, x
@done:
        rts
.endproc

; X = 自分（ai_prof 済み）。揺らぎの段と、次に引き直すまでの周期を引き直す。
;
; **引くのはこの瞬間だけ**である（毎フレームではない）。毎フレーム振ると
; 落ち着きの無い震えになり、「意図があって歩いた」に見えない（ai_params.inc §4-b）。
;
; 段は **-1 / 0 / +1 / +2 の4通り**を等確率で引く。
; 3通り（-1 / 0 / +1 に 0 を2回割り当てる）にすると、**引き直しの半分が同じ所を指して
; 一歩も動かない**。引き直しの間隔が 80 フレーム前後なので、それでは
; 「立ち止まったまま固まっている」時間が平均 2.7 秒になり、棒立ちが直らない。
; 4通りなら動かないのは 1/4 で、1.8 秒に一度は足が出る。
; 「ときどき少し向こうまで歩いてから戻る」は +2 段 → 0 段 の並びとして出る。
;
; 平均が +0.5 段ぶん外へ寄るので、**その半段ぶんは follow_x / hold_x 側で引いてある**
; （TSV の follow_x を 30 → 26 にしたのはこのためである。平均の立ち位置は変えていない）。
;
; 1段の幅（wander 列）は **AI_DEADBAND より確実に広く**取ること。
; 同じくらいだと、引き直しても立ち位置の許容誤差に飲まれて一歩も動かない
; （実測: wander = AI_DEADBAND = 4 のとき、900 フレームで動いたのは 2 フレームだけだった）。
;
; 周期は dwell に AI_WANDER_JITTER ぶんの散らしを足す。散らさないと全員が
; 同じ拍で引き直し、隊列が一斉に動いて**かえって機械的に見える**。
.proc ai_reroll_wander
        lda ai_rng               ; 引く手は ai_sense が毎フレーム進めている
        sta ai_t0                ; 引いた目
        ldy ai_prof
        lda ai_p_dwell, y
        beq @never               ; dwell = 0 … この型は揺らがない
        sta ai_t1
        lda ai_p_wander, y
        sta ai_t2                ; 1段の幅
        lda ai_t0
        and #AI_WANDER_JITTER
        clc
        adc ai_t1
        bcc :+
        lda #$FF                 ; 頭打ち（周期が 255 フレームを超えることはまず無い）
:       sta ai_wtim, x

        lda ai_t0                ; 段は上の方のビットから取る
        lsr a                    ;   （下位は周期の散らしに使ったので、同じビットを
        lsr a                    ;    二度使うと段と周期が連動してしまう）
        lsr a
        lsr a
        lsr a
        and #3                   ; 0..3
        tay
        lda #0
        sec
        sbc ai_t2                ; -1 段（= 0 - wander）から始めて、引いた目だけ足す
@step:
        cpy #0
        beq @drawn
        clc
        adc ai_t2
        dey
        jmp @step

@drawn:
        ; **前と同じ段を引いたら1段ずらす。**そのまま採ると、引き直したのに
        ; 一歩も動かない回ができる（4通りなので 1/4）。それが続くと
        ; 立ち止まったまま何秒も固まり、主の言う棒立ちがそこに戻ってくる。
        ; ずらせば、引き直しのたびに必ず足が出る＝固まる時間は周期（dwell）で頭打ちになる。
        ; **間隔と距離は引いた目のままなので、規則正しくは見えない**
        ; （周期は AI_WANDER_JITTER で散り、動く距離は 1〜3 段ぶんとまちまちになる）。
        cmp ai_woff, x
        bne @set
        sta ai_t1                ; dwell はもう使い終わっているので箱を借りる
        lda ai_t2
        asl a                    ; +2 段（段の上端）
        cmp ai_t1
        bne @bump
        lda #0                   ; 上端だった → -1 段（下端）へ回す
        sec
        sbc ai_t2
        jmp @set
@bump:
        lda ai_t1
        clc
        adc ai_t2                ; 1段外へ
@set:
        sta ai_woff, x           ; -1 / 0 / +1 / +2 段
        rts
@never:
        lda #0
        sta ai_woff, x
        sta ai_woffy, x
        lda #$FF                 ; 引き直しに来ない（毎フレーム引き直さない）
        sta ai_wtim, x
        rts
.endproc

; X = 自分。奥行きの揺らぎを横の揺らぎから作る（**列を増やさない**）。
;
; 主の指摘のもう半分:「プレイヤーキャラクターと **Y を揃えて** 等距離を保つ」。
; 横だけ揺らしても、足元Yが操作キャラと1ドット違わず並んでいれば
; 「等距離を移動するモノ」のままである。奥行きにも同じだけ意図を持たせる。
;
; 幅は横の半分にする。奥行きは横より狭く・遅く動くのがベルトスクロールの慣習で、
; 寄り足の速さ（depth_spd = speed の半分）と同じ関係をここでも保つ。
; 列を足さないのは、**奥行きの揺らぎを横と独立に回したくなる理由が無い**からである
; （独立にすると、横が動かない引き直しで奥行きだけ動くという読めない動きが出る）。
.proc ai_set_woffy
        lda ai_woff, x
        cmp #$80                 ; 符号を保ったまま半分にする（算術右シフト）
        ror a
        sta ai_woffy, x
        rts
.endproc

; 揺らぎを引く手。8bit の最大長 LFSR（x^8+x^4+x^3+x^2+1）。A = 引いた目。
; **種が固定なので、同じ操作をすれば必ず同じ挙動になる。**
; 乱数を使ってよいのは「いつ・どれだけ揺れるか」だけである。
; 陣形の順位（ai_spread / ai_avoid_allies）は番号順のままであり、ここを乱数にすると
; 再現しない重なりの不具合を作る。
.proc ai_rand
        lda ai_rng
        asl a
        bcc :+
        eor #$1D
:       sta ai_rng
        rts
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
.if ::DBG_ENABLE
        ; --- 仲間の確認モードの門（dbg_ally）。**行動側の門はここ1箇所だけ** ---
        ; 既定（0）は比較1回で素通りし、以降は今までと**完全に同じ経路**を通る。
        lda dbg_ally
        beq dbg_resume
        jmp dbg_ally_gate
dbg_resume:
.endif
        jsr ai_load_profile      ; 時間の経過（揺らぎの引き直し）が表を引くので先に
        jsr ai_tick_timers

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
        jsr ai_depth_follow
        jmp ai_try_attack
@done:
        rts

.if ::DBG_ENABLE
; 門の中身（素通りしなかったときだけ通る）。A = dbg_ally, X = 自分。
dbg_ally_gate:
        cpx #ENT_ENEMY_FIRST
        bcs dbg_resume           ; **敵には一切効かせない。**効くのは仲間だけである
        cmp #DBG_ALLY_REST
        bne :+
        jmp ai_dbg_rest_one      ; 2 = 休ませる（あちらが rts する）
:       lda #DBG_ALLY_MUTE_HOLD  ; 1 = 攻撃しない。振るまでの間隔を毎フレーム積み直す
        sta ai_timer, x          ;     （考え・歩き・追従・揺らぎはこの後そのまま走る）
        jmp dbg_resume
.endif
.endproc

; X = エンティティ番号（ai_prof 済み）。時間の経過。動けない状態でも進める。
;
; 揺らぎの周期もここで進める。のけぞっていても倒れていても「次はあそこに立とう」
; という気は進む（動けるようになった瞬間から新しい立ち位置へ歩き出す）。
.proc ai_tick_timers
        lda ai_timer, x
        beq :+
        dec ai_timer, x
:       lda ai_wtim, x
        beq @reroll
        dec ai_wtim, x
        rts
@reroll:
        jmp ai_reroll_wander
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
; 奥行きが離れていれば絵は重ならないので、**足元Yが近いときだけ**効かせる
; （かつての「同じレーンのときだけ」に当たる。AI_OVERLAP_Y）。
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

; X = 自分。**立ち位置ではなく自分の体**が操作キャラの専有距離の中に居るなら
; ai_urgent を立てる（そうでなければ落とす）。X は壊さない。Y と ai_t4 を壊す。
;
; 呼ぶのは「歩き出しの遅れがまだ残っている」ときだけである（ai_move_to_post）。
; 遅れが 0 なら、急ぐも急がないも無い＝この検査に意味が無い。
; 毎フレーム全員ぶん呼ぶと depth_distance の呼び出しが仲間の数だけ増える
; （実測で 1フレーム 150 サイクル）。**意味が変わる場面でだけ払う。**
;
; なぜ立ち位置（ai_push_out_of_player が見ている方）では足りないか:
; 操作キャラが歩いてきて仲間に**寄った**とき、仲間の立ち位置はもう専有距離の外に
; 逃げているが、仲間の体はまだ操作キャラの上に載っている。このとき
; 「歩き出しの遅れ」で仲間が数フレーム待つと、その間ずっと操作キャラが陰に入る。
; **退くのを待たせてはならない**（ai-dev の最優先の不具合）。
; 揺らぎは邪魔をしない範囲でしか入れない、というのがこの分岐の意味である。
.proc ai_crowds_player
        lda #0
        sta ai_urgent
        cpx #ENT_ENEMY_FIRST
        bcs @done                ; 敵が操作キャラに近づくのは仕事である
        ldy #ENT_PLAYER
        lda ent_active, y
        beq @done
        lda ent_y, y
        tay
        lda ent_y, x
        jsr depth_distance
        cmp #AI_OVERLAP_Y
        bcs @done                ; 奥行きが離れていれば画面上で重ならない
        ldy #ENT_PLAYER
        lda ent_x_lo, x
        sec
        sbc ent_x_lo, y
        sta ai_t4
        lda ent_x_hi, x
        sbc ent_x_hi, y
        beq @right               ; 上位 0 → 自分は操作キャラの右 0..255 ドット
        cmp #$FF
        bne @done                ; 256 ドット以上離れている
        lda ai_t4                ; 上位 $FF → 自分は左。|差| < AI_CLEAR_X は
        cmp #<(256 - AI_CLEAR_X + 1)     ; 下位が 256-(AI_CLEAR_X-1) 以上と同じこと
        bcs @crowd
        rts
@right:
        lda ai_t4
        cmp #AI_CLEAR_X
        bcs @done
@crowd:
        lda #1
        sta ai_urgent
@done:
        rts
.endproc

; X = 自分、ai_dir = 出す向き（0:左 / 1:右 / AI_DIR_NEAREST:近い方）。
; 立ち位置が操作キャラの専有距離の内側なら、**ai_dir の向きのまま**外へ出す。
; 向きを呼び出し元に決めさせられるのは、仲間を避けて動いた立ち位置を
; 「近い方」へ戻すと、避けたはずの相手の所へ押し返されるからである。
.proc ai_push_out_of_player
        ldy #ENT_PLAYER
        lda ent_active, y
        beq @done
        lda ent_y, y
        tay
        lda ent_y, x
        jsr depth_distance
        cmp #AI_OVERLAP_Y
        bcs @done                ; 奥行きが離れていれば画面上で重ならない
        ldy #ENT_PLAYER          ; 足元Yを渡すのに Y を使ったので、番号に戻す
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
; X = 自分。立ち位置（ai_t0/ai_t1）が、**足元Yが近い**（＝画面で縦に重なる）
; **自分より番号の小さい仲間**の居場所に AI_SPREAD より近ければ、
; その仲間から AI_SPREAD だけ離れた所へ置き直す。
;
; レーンが無くなった（ADR-0009）ので「同じレーンか」は「足元Yの差が AI_OVERLAP_Y 未満か」
; になった。**判定が緩くなった向きの変更ではない**。以前は Y が4段階に量子化されていたので
; 「同じレーン」＝「足元Yが完全一致」であり、隣の段（12〜20 ドット差）は素通りしていた。
; 8x16 のスプライトは十数ドット差でも縦に重なるので、素通りしていた側が誤りである。
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
        sty ai_t4                ; 走査位置を退避（ai_post_gap より前なので使ってよい）
        lda ent_y, y
        tay
        lda ent_y, x
        jsr depth_distance
        ldy ai_t4                ; 走査位置に戻す（A は壊れない）
        cmp #AI_OVERLAP_Y
        bcs @next                ; 奥行きが離れていれば画面上で重ならない
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

; 「決めた立ち位置（ai_t0/ai_t1）へ歩く」だけの入口。既定の経路はここを素通りする
; （ラベルが1つ増えるだけで、命令は1つも増えていない）。
; 借り手は休ませるモード（ai_dbg_rest_one）である。あちらは標的も目標も持たないので
; 立ち位置の計算（上）には入れない。ここから下は ai_target を見ていないので借りられる。
walk_to_post:
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
        bcc @arrived             ; もう着いている
@walk:
        ; **歩き出しの遅れ**（react）。主の指摘「プレイヤーに少し遅れて歩き出す」。
        ; 立ち止まっている者は、行くべき所が動いてから react フレーム置いて足を出す。
        ; 0 にすると操作キャラと同じフレームに歩き出し、連動する影に見える。
        ; いったん歩き出したら着くまで遅れは効かない（歩きながら足踏みしない）。
        ;
        ; **退くときだけは待たない。**操作キャラの陰に入っている間の数フレームは、
        ; そのまま「壁になっている」時間だからである（ai_crowds_player）。
        lda ai_react, x
        beq @go                  ; 遅れは残っていない。急ぐかどうかを調べる意味も無い
        jsr ai_crowds_player
        lda ai_urgent
        bne @go
        dec ai_react, x
        rts
@go:
        jsr ai_step_px
        lda ai_px
        beq @done
        sta act_px
        lda ai_t3
        bne @go_left
        jmp act_move_right
@go_left:
        jmp act_move_left
@arrived:
        ldy ai_prof              ; 着いた。次に歩き出すときの遅れを積み直す
        lda ai_p_react, y
        sta ai_react, x
@done:
        rts
.endproc

.if ::DBG_ENABLE
; ==========================================================================
; 休ませる（dbg_ally = 2）—— **仕様ではない。手触りを見るための仕掛けである**
; ==========================================================================
; X = 仲間。思考も移動も攻撃もしない。目標・タイマ・揺らぎの状態には**一切触らない**
; （凍らせたまま持ち越すので、0 に戻した瞬間から続きとして動き出す）。
;
; 唯一残すのが**操作キャラに押されて退く**ことである。止めると、操作キャラが
; 休んでいる仲間に乗り上げたまま抜けられなくなる＝ai-dev の最優先の不具合そのものになる。
; 立ち位置を「いま自分が立っている所」に置いてから ai_clear_player_zone に通すので、
; 専有距離の外に居るあいだは立ち位置＝現在地であり、1ドットも動かない。
.proc ai_dbg_rest_one
        lda act_hitstop, x
        bne @done                ; ヒットストップ中は当人の時間が止まっている
        lda ent_state, x
        bne @done                ; ACT_ST_IDLE 以外は action 側が時間を進めている最中
        jsr ai_load_profile      ; 退く足の速さ（ai_step_px）に要る。表を引くだけで考えてはいない
        lda ent_x_lo, x          ; 立ち位置 = いま立っている所（＝動かない）
        sta ai_t0
        lda ent_x_hi, x
        sta ai_t1
        jsr ai_clear_player_zone ; 専有距離の中に居るときだけ、その外へ立ち位置がずれる
        jmp ai_move_to_post::walk_to_post   ; 「立ち位置へ歩く」所だけを借りる（あちらの注記）
@done:
        rts
.endproc
.endif

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
; 奥行きを合わせる
; ==========================================================================
; X = エンティティ番号。標的の足元Yへ**連続に**寄る（ADR-0009）。
; レーン番号への吸着ではないので、「1つ隣へ移る」も「移れるまでの待ち時間」も無い。
; **奥行きの寄り足の速さ**（depth_spd）が大きいほど素早く標的の奥行きに並ぶ。
; 帯（歩ける範囲）のクランプは engine（ent_depth_move）が持つ。
;
; 狙うのは標的の足元Yそのものではなく、そこに**奥行きの揺らぎ**（ai_woffy）を足した所である。
; 操作キャラと足元Yが1ドット違わず並び続けるのが「機械的」の正体だった（主の指摘）。
; ただし ai_woffy は**交戦中は 0** に落としてある（ai_plan）。奥行きを合わせ切ったか
; どうかが攻撃に移る入口だからで、そこを揺らすと永久に振らない仲間ができる。
;
; 測った隔たりは ai_ydif に残す。直後の ai_try_attack がそれを使い回す
; （横距離 ai_dist を ai_act_one が1回だけ測って使い回しているのと同じ作りである）。
;
; 不感帯（AI_DEADBAND_Y）を置いてあるのは、横移動に AI_DEADBAND を置いたのと同じ理由である。
; 標的は毎フレーム動くので、「ぴったり合わせる」と合わせ直しが毎フレーム走って
; 足元Yが1ドット刻みで震える（＝影とスプライトが上下にちらつく）。
;
; 不感帯の内側に入る前に**行き過ぎない**ようにもしてある（残りの距離で刻み幅を頭打ちにする）。
; これが無いと、寄り足が不感帯より速い型（depth_spd が大きい行）が
; 不感帯をまたいで往復し、速い型ほど震えるという逆立ちが起きる。
.proc ai_depth_follow
        ldy ai_target, x
        lda ent_y, y
        clc
        adc ai_woffy, x          ; 狙う足元Y = 標的 + 揺らぎ（**交戦中は 0**。ai_plan）
        sec
        sbc ent_y, x             ; 差 = 狙う足元Y - 自分
        bcs @toward_near         ; 借りが出ない = 標的の方が手前（画面下）
        eor #$FF                 ; 奥（画面上）。8bit の絶対値を取る
        adc #1                   ; ここは carry = 0 なのでちょうど +1
        sta ai_ydif
        lda #1                   ; 向き: 1 = 奥へ（負の移動量）
        bne @measured            ; 常に成立
@toward_near:
        sta ai_ydif
        lda #0
@measured:
        sta ai_t4
        lda ai_ydif
        cmp #AI_DEADBAND_Y
        bcc @done                ; もう合っている（端数で震えないための不感帯）

        jsr ai_step_py           ; ai_px = 今フレームに動ける奥行きのドット数
        lda ai_px
        beq @done                ; 端数が溜まっていない
        cmp ai_ydif
        bcc :+
        lda ai_ydif              ; 残りより大きく刻まない（行き過ぎない）
:       ldy ai_t4
        beq @move                ; 手前へ = 正の移動量
        eor #$FF                 ; 奥へ = 負の移動量
        clc
        adc #1
@move:
        jmp ent_depth_move       ; X は保たれる。帯のクランプは engine の持ち分
@done:
        rts
.endproc

; X = エンティティ番号。今フレームに動ける**奥行き方向**のドット数を ai_px に入れる。
; 横（ai_step_px）と同じく 1/16 ドット単位で刻み、端数は ai_ysub に持ち越す。
; 横と別の端数を持つのは、奥行きの速さが横と別の値だからである
; （ベルトスクロールの慣習では奥行きの方が遅い。ADR-0009 の帰結）。
; ai_prof が要る。
.proc ai_step_py
        ldy ai_prof
        lda ai_ysub, x
        clc
        adc ai_p_depth_spd, y    ; TSV 側で 1..240 に制限してある（ここで桁あふれしない）
        pha
        and #$0F
        sta ai_ysub, x
        pla
        lsr a
        lsr a
        lsr a
        lsr a
        sta ai_px
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
; 奥行きは「**自分の不感帯の内側まで寄れているか**」だけを見る。
; 何ドットまで当たるか（Y許容幅）は action の持ち分であり（ADR-0009）、
; AI がその値を知って振る舞いを変えると判定ルールの二重管理になる。
; ここで見ているのは action の閾値ではなく、**AI 自身の立ち位置の許容誤差**である。
; 狙う奥行きは標的の足元Yそのものなので、不感帯の内側なら「合わせ切った」と言える。
; 許容幅は体格の縦サイズから導かれる量（ADR-0009 1-a）で、
; 数ドットの不感帯より狭くなることはない。したがってこの判断は常に安全側である。
;
; 「移動中は振らない」という条件は**丸ごと消えた**。補間という状態がもう無いからである
; （ADR-0009。ent_lane_step は存在しない）。
;
; 奥行きの隔たり（ai_ydif）も横距離（ai_dist）も、**この1フレームで既に測ってある値**を
; 使い回す。どちらも今フレームに動いたぶんだけ古いが、その差は数ドットで、
; 不感帯・間合いの幅に対して無視できる。ここで測り直すと、1体につき毎フレーム
; 2回ぶん余分に距離を測ることになる（満員では効いてくる）。
.proc ai_try_attack
        lda ai_goal, x
        cmp #AI_GOAL_ENGAGE
        bne @done
        lda ai_timer, x
        bne @done                ; 攻撃の間隔（atk_gap）
        ldy ai_target, x
        lda ent_state, y
        cmp #ACT_ST_DOWN
        bcs @done                ; 倒れている相手は殴らない
        lda ai_ydif              ; 奥行きは直前の ai_depth_follow が測った値を使い回す
        cmp #AI_DEADBAND_Y
        bcs @done                ; まだ奥行きを合わせ切っていない
        ldy ai_prof              ; 横距離は ai_act_one が測った ai_dist を使い回す
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
; 奥行き（足元Y）の隔たりは engine のプリミティブ depth_distance が測る。
; 入口が「足元Yの値どうし」（A と Y）なので、番号から引き直して渡す:
;       lda ent_y, y  /  tay  /  lda ent_y, x  /  jsr depth_distance
; この4行は3箇所に現れる。**AI 側の包み直しを1本挟むと、呼び出しのたびに
; Y レジスタの退避が要って満員時に効いてくる**ので、そのまま並べてある
; （呼んだ後に相手の番号が要る所だけ、その場で退避する）。
;
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
