; ai_params.s — 自律行動のパラメータ表。
;
; **正本は data/ai_params.tsv である。**このファイルはそれを書き写したものにすぎない。
; 値を変えるときは必ず TSV を先に直し、ここを合わせること。
;
; なぜ写しが要るか: TSV を ca65 の表へ変換する tools/ai_params.py は
; **stage-author の範囲**（CLAUDE.md 第3節: tools/ は stage-author）であり、
; ai-dev は tools/ に書けない。変換器が入ったら、このファイルは
; 生成物の .include 1行に置き換わり、下の .byte は消える（列の名前と並びは変えない）。
;
; 表は列ごとに1本の配列である（Structure of Arrays）。
; 行（ent_ai の値）を添字にして `lda ai_p_hold_x, y` の1命令で引ける。
; 行を足す = 型を足す。**コードには一切触らない。**
.include "constants.inc"
.include "ai.inc"
.include "ai_params.inc"

.export ai_p_speed, ai_p_hold_x, ai_p_reach_x, ai_p_aggr_x
.export ai_p_follow_x, ai_p_leash_x, ai_p_atk_gap, ai_p_depth_spd, ai_p_item_pri
.export ai_p_hate, ai_p_react, ai_p_wander, ai_p_dwell
.export ai_default_profile

.segment "RODATA"

; --------------------------------------------------------------------------
; data/ai_params.tsv の写し。行の並びは id 列の順（= ent_ai の値）。
;
;   id 0  ally_versatile  万能（自律仲間）
;         6型の中では**すべての値が中庸**である。P2 で他の5型が入ったとき、
;         突撃は hold_x と atk_gap をこれより小さく・aggr_x と depth_spd を大きく、
;         遠隔は hold_x を大きく、庇護は follow_x と leash_x を小さく、
;         牽制は reach_x に対して hold_x を大きめに取る。
;         万能はそのどれでもない位置に置く（＝この行が基準線になる）。
;   id 1  enemy_melee     近づいて殴るだけの敵
;         追従先を持たない（leash_x = 0）ので、食いついた相手をどこまでも追う。
; --------------------------------------------------------------------------

ai_p_speed:                      ; 移動の速さ（1/16 ドット/f）。操作キャラは 24
        .byte 28                 ; ally_versatile  操作キャラ(24) より速い。遅れて出ても追いつける
        .byte 14                 ; enemy_melee     遅い（引き撃ちで捌ける速さ）
.assert * - ai_p_speed = AI_PROFILE_COUNT, error, "ai_p_speed の行数がプロファイル数と違う"

ai_p_hold_x:                     ; 好む間合い（標的との横距離・ドット）
        .byte 14                 ; ally_versatile  近接。密着はしない
        .byte 12                 ; enemy_melee     近接
.assert * - ai_p_hold_x = AI_PROFILE_COUNT, error, "ai_p_hold_x の行数がプロファイル数と違う"

ai_p_reach_x:                    ; 攻撃に移る閾値（奥行きを合わせ切った上で、この横距離以内なら振る）
        .byte 20                 ; ally_versatile
        .byte 20                 ; enemy_melee
.assert * - ai_p_reach_x = AI_PROFILE_COUNT, error, "ai_p_reach_x の行数がプロファイル数と違う"

ai_p_aggr_x:                     ; 攻撃性（この重みつき距離以内の相手にしか食いつかない）
        .byte 80                 ; ally_versatile  画面幅の 1/3 ほど。中庸
        .byte 140                ; enemy_melee     画面の半分。見つけたら寄ってくる
.assert * - ai_p_aggr_x = AI_PROFILE_COUNT, error, "ai_p_aggr_x の行数がプロファイル数と違う"

ai_p_follow_x:                   ; プレイヤーへの追従距離（戻ってきたときに立つ位置）
        .byte 26                 ; ally_versatile  専有距離(14)の外側。揺らぎの平均が
                                 ;                 +0.5 段(=4) 外に寄るぶん内側に取ってある
        .byte 0                  ; enemy_melee     追従先を持たない（未使用）
.assert * - ai_p_follow_x = AI_PROFILE_COUNT, error, "ai_p_follow_x の行数がプロファイル数と違う"

ai_p_leash_x:                    ; 追従の限界（0 = 限界なし＝操作キャラに追従しない）
        .byte 112                ; ally_versatile  画面幅の半分弱。離れすぎたら戻る
        .byte 0                  ; enemy_melee     限界なし
.assert * - ai_p_leash_x = AI_PROFILE_COUNT, error, "ai_p_leash_x の行数がプロファイル数と違う"

ai_p_atk_gap:                    ; 攻撃の間隔（フレーム）
        .byte 26                 ; ally_versatile
        .byte 54                 ; enemy_melee     1秒弱に1回。捌ける手数
.assert * - ai_p_atk_gap = AI_PROFILE_COUNT, error, "ai_p_atk_gap の行数がプロファイル数と違う"

ai_p_depth_spd:                  ; 奥行きの寄り足の速さ（1/16 ドット/f。**大きいほど積極的**）
        .byte 13                 ; ally_versatile  横(28)のほぼ半分。中庸
        .byte 8                  ; enemy_melee     鈍い（奥行きをずらせば振り切れる）
.assert * - ai_p_depth_spd = AI_PROFILE_COUNT, error, "ai_p_depth_spd の行数がプロファイル数と違う"

ai_p_item_pri:                   ; アイテム回収の優先度。**P1 では読まれない**（アイテムが無い）
        .byte 128                ; ally_versatile  中庸（回収型は P2 でこれより大きくする）
        .byte 0                  ; enemy_melee     拾わない
.assert * - ai_p_item_pri = AI_PROFILE_COUNT, error, "ai_p_item_pri の行数がプロファイル数と違う"

ai_p_hate:                       ; 狙われやすさ。**AI_HATE_MID (128) が中立**
        .byte AI_HATE_MID        ; ally_versatile  中立（万能はヘイトを取りに行かない）
        .byte AI_HATE_MID        ; enemy_melee     中立
.assert * - ai_p_hate = AI_PROFILE_COUNT, error, "ai_p_hate の行数がプロファイル数と違う"

ai_p_react:                      ; 歩き出しの遅れ（フレーム）。0 だと操作キャラと同時に歩く
        .byte 10                 ; ally_versatile  0.17 秒。人が連れと同時に歩き出さない程度
        .byte 6                  ; enemy_melee     寄ってくるのが仕事なので短め
.assert * - ai_p_react = AI_PROFILE_COUNT, error, "ai_p_react の行数がプロファイル数と違う"

ai_p_wander:                     ; 立ち止まる位置の揺らぎ（ドット・1段ぶん）。0 で揺らがない
        .byte 8                  ; ally_versatile  AI_DEADBAND(4) の倍。引き直せば一歩出る幅
        .byte 0                  ; enemy_melee     揺らがない。leash_x = 0 なので追従に入らず、
                                 ;                 揺らぎが効く場面（狙う相手が居ないとき）が無い
.assert * - ai_p_wander = AI_PROFILE_COUNT, error, "ai_p_wander の行数がプロファイル数と違う"

ai_p_dwell:                      ; 揺らぎを引き直す周期（フレーム）。0 = 引き直さない
        .byte 80                 ; ally_versatile  1.3〜1.9 秒（AI_WANDER_JITTER で散る）
                                 ;                 固まったまま立っている時間の上限でもある
        .byte 0                  ; enemy_melee     同上（引き直しに来ない）
.assert * - ai_p_dwell = AI_PROFILE_COUNT, error, "ai_p_dwell の行数がプロファイル数と違う"

; --------------------------------------------------------------------------
; 区画からの既定プロファイル。**TSV には無い。**
; ent_ai は ent_clear_all が消さない箱なので、配置側（P3 の waves / P4 の roster）が
; 詰め忘れると未初期化の値で表を引くことになる。その安全柵として、
; ai_init が「範囲外の ent_ai」を立場（ACT_ROLE_*）ごとの既定値で埋める。
; 添字は ACT_ROLE_PLAYER / ACT_ROLE_ALLY / ACT_ROLE_ENEMY。
; --------------------------------------------------------------------------
ai_default_profile:
        .byte AI_PROF_ALLY_VERSATILE     ; ACT_ROLE_PLAYER（操作キャラ。AI は動かさない）
        .byte AI_PROF_ALLY_VERSATILE     ; ACT_ROLE_ALLY
        .byte AI_PROF_ENEMY_MELEE        ; ACT_ROLE_ENEMY
.assert * - ai_default_profile = 3, error, "ai_default_profile の項数が立場の数と違う"
