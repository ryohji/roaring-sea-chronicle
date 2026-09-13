; action_params.s — アクションの調整値（表）。
;
; スカラーの調整値は action_params.inc にある。ここにあるのは
; 「段数ごと」「体格ごと」「立場ごと」に値が並ぶ**表**である。
; 主はこの数値を触るだけで手触りを変えられる。コード側に同じ値を書いてはならない。
;
; 将来 data/action_params.tsv からビルド時に生成する前提の形（1行1項目の並び）に
; してある。差し替わるのは .byte の中身だけで、コードは触らない。
.include "constants.inc"
.include "action_params.inc"

.export act_grace_frames
.export act_hp_by_body, act_hurt_w, act_hurt_h
.export atk_startup, atk_active, atk_recover
.export atk_reach, atk_w, atk_h, atk_dmg, atk_kb

.segment "RODATA"

; --------------------------------------------------------------------------
; 猶予（ダウンしてから確定するまで）の長さ。立場ごと。ADR-0005。
; 添字は ACT_ROLE_*（操作キャラ / 自律仲間 / 敵）。
; --------------------------------------------------------------------------
act_grace_frames:
        .byte ACT_GRACE_PLAYER   ; ACT_ROLE_PLAYER 切れると シナリオ失敗
        .byte ACT_GRACE_ALLY     ; ACT_ROLE_ALLY   切れると 戦線離脱
        .byte ACT_GRACE_ENEMY    ; ACT_ROLE_ENEMY  切れると 画面から消える
.assert * - act_grace_frames = 3, error, "act_grace_frames の項数が立場の数と違う"

; --------------------------------------------------------------------------
; 体格型（BODY_*）ごとの値。添字は ent_body。
;   act_hp_by_body … 敵の初期HP（味方の初期HP は ACT_HP_PARTY。P4 で roster に移る）
;   act_hurt_w/h   … やられ矩形の幅・高さ。幅は見た目（engine の body_width×8）に合わせる。
;                    高さは足元から上へ。攻撃の基点（体の前端）にもこの幅を使う。
; --------------------------------------------------------------------------
act_hp_by_body:
        .byte  8      ; BODY_PLACEHOLDER 仮CHR
        .byte  6      ; BODY_LIGHT       軽装・小柄
        .byte 14      ; BODY_HEAVY       重装
        .byte 10      ; BODY_BEAST       獣
        .byte 18      ; BODY_BEAST_LARGE 獣（大）
        .byte 24      ; BODY_MACHINE     機械型
.assert * - act_hp_by_body = BODY_TYPE_COUNT, error, "act_hp_by_body の項数が体格型の数と違う"

act_hurt_w:
        .byte  8      ; BODY_PLACEHOLDER
        .byte 16      ; BODY_LIGHT
        .byte 24      ; BODY_HEAVY
        .byte 24      ; BODY_BEAST
        .byte 32      ; BODY_BEAST_LARGE
        .byte 32      ; BODY_MACHINE
.assert * - act_hurt_w = BODY_TYPE_COUNT, error, "act_hurt_w の項数が体格型の数と違う"

act_hurt_h:
        .byte 24      ; BODY_PLACEHOLDER
        .byte 24      ; BODY_LIGHT
        .byte 28      ; BODY_HEAVY
        .byte 24      ; BODY_BEAST
        .byte 28      ; BODY_BEAST_LARGE
        .byte 32      ; BODY_MACHINE
.assert * - act_hurt_h = BODY_TYPE_COUNT, error, "act_hurt_h の項数が体格型の数と違う"

; --------------------------------------------------------------------------
; 通常攻撃（Aボタン）の段ごとの表。添字は 0..ACT_COMBO_MAX-1（段数-1）。
;
; 攻撃矩形は「向き × 段数」で決まるが、向きは**前方への鏡像**で作る。
; 表が持つのは前方への値だけであり、左向きのときは体の前端を基準に反転する:
;   右向き: 矩形の左端 = ワールドX + 体幅 + atk_reach
;   左向き: 矩形の右端 = ワールドX        - atk_reach
;   縦    : 足元から上へ atk_h（レーンが奥行きを見るので、縦は保険の判定）
;
; 1振りは最初に当たったフレームで終わり（同じ振りで2度当たらない）。
; そのフレームに矩形が重なっている相手は全員に当たる。
; --------------------------------------------------------------------------
atk_startup:                     ; 発生（ボタンから判定が出るまで）。短いほど手が速い
        .byte  4,  5,  8
.assert * - atk_startup = ACT_COMBO_MAX, error, "atk_startup の項数が段数と違う"

atk_active:                      ; 持続（判定が出ているフレーム数）。長いほど当てやすい
        .byte  3,  3,  4
.assert * - atk_active = ACT_COMBO_MAX, error, "atk_active の項数が段数と違う"

atk_recover:                     ; 硬直。**次の段の受付時間でもある**（長いほど繋げやすい）
        .byte 10, 12, 20
.assert * - atk_recover = ACT_COMBO_MAX, error, "atk_recover の項数が段数と違う"

atk_reach:                       ; 体の前端から前方へのオフセット（ドット）。リーチ
        .byte  2,  3,  4
.assert * - atk_reach = ACT_COMBO_MAX, error, "atk_reach の項数が段数と違う"

atk_w:                           ; 攻撃矩形の幅（ドット）
        .byte 14, 14, 18
.assert * - atk_w = ACT_COMBO_MAX, error, "atk_w の項数が段数と違う"

atk_h:                           ; 攻撃矩形の高さ（足元から上へ・ドット）
        .byte 24, 24, 26
.assert * - atk_h = ACT_COMBO_MAX, error, "atk_h の項数が段数と違う"

atk_dmg:                         ; 威力
        .byte  3,  3,  5
.assert * - atk_dmg = ACT_COMBO_MAX, error, "atk_dmg の項数が段数と違う"

atk_kb:                          ; ノックバックの初速（ドット/フレーム）。減衰は ACT_KB_DECAY
        .byte  3,  3,  8
.assert * - atk_kb = ACT_COMBO_MAX, error, "atk_kb の項数が段数と違う"

; 相のフレーム数が 0 だと状態機械の「残りフレームを1減らして 0 なら次へ」が
; 255 フレームに化ける。表を触るときの安全柵として、リンク時に縛っておく。
.assert ACT_COMBO_MAX >= 2 && ACT_COMBO_MAX <= 3, error, "コンボは2〜3段まで"
