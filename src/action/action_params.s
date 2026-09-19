; action_params.s — アクションの調整値（表）。
;
; スカラーの調整値は action_params.inc にある。ここにあるのは
; 「段数ごと」「体格ごと」「立場ごと」に値が並ぶ**表**である。
; 主はこの数値を触るだけで手触りを変えられる。コード側に同じ値を書いてはならない。
;
; 将来 data/action_params.tsv からビルド時に生成する前提の形（1行1項目の並び）に
; してある。差し替わるのは .byte の中身だけで、コードは触らない。
.include "constants.inc"
.include "action.inc"          ; 状態の語彙（act_pose_by_state の添字）
.include "action_params.inc"

.export act_grace_frames
.export act_hp_by_body, act_hurt_w, act_hurt_h, act_depth_tol
.export act_pose_by_state
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
;   act_depth_tol  … 当たり判定のY許容幅（ADR-0009）。**この列は手で書かない。**
;                    action_params.inc の ACT_BODY_ROWS_*（絵の縦サイズ）と
;                    ACT_DEPTH_TOL_NUM/DEN（比）から計算した値が入る。
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

; 当たり判定のY許容幅（ドット）。**絵の縦サイズの ACT_DEPTH_TOL_NUM/DEN 倍**である。
; ADR-0009 の要求「縦サイズを変えたときに許容幅が追随すること」を、
; 表を計算式で埋めることで満たしている。**ここに数値を直接書いてはならない。**
; 絵を大きくするときに直すのは action_params.inc の ACT_BODY_ROWS_* だけである。
act_depth_tol:
        .byte ACT_DEPTH_TOL_PLACEHOLDER   ; BODY_PLACEHOLDER 仮CHR
        .byte ACT_DEPTH_TOL_LIGHT         ; BODY_LIGHT       軽装・小柄
        .byte ACT_DEPTH_TOL_HEAVY         ; BODY_HEAVY       重装
        .byte ACT_DEPTH_TOL_BEAST         ; BODY_BEAST       獣
        .byte ACT_DEPTH_TOL_BEAST_LARGE   ; BODY_BEAST_LARGE 獣（大）
        .byte ACT_DEPTH_TOL_MACHINE       ; BODY_MACHINE     機械型
.assert * - act_depth_tol = BODY_TYPE_COUNT, error, "act_depth_tol の項数が体格型の数と違う"

; 主の決定（ADR-0009 残る論点 1-a）は「縦 16 ドットのキャラで 5〜6 ドット」である。
; 比を触って仮CHR の許容幅がその外に出たら、それは決定から外れたということなので止める。
; **絵を大きくして値が 6 を超えるのは正しい**（比は保たれている）ため、
; この番人が見るのは体格ごとの値ではなく、「8x16 一段のキャラに当てはめた比」である。
.assert SPRITE_H * ACT_DEPTH_TOL_NUM / ACT_DEPTH_TOL_DEN >= 5, error, "Y許容幅が主の決定（16ドットのキャラで5〜6）より狭い"
.assert SPRITE_H * ACT_DEPTH_TOL_NUM / ACT_DEPTH_TOL_DEN <= 6, error, "Y許容幅が主の決定（16ドットのキャラで5〜6）より広い"

; --------------------------------------------------------------------------
; 状態 → 姿勢（SPR_POSE_*）。添字は ACT_ST_*。engine の ent_set_pose に渡す。
;
; P1 の受入で主が指摘した「攻撃とダメージ硬直の違いがわからない」への対応の本体である。
; **斬り（STRIKE）は前へ、のけぞり（HURT）は後ろへ折れる絵**になっているので、
; 同じ「止まっている」フレームでも、攻撃の硬直か殴られた硬直かが見分けられる。
;
; 攻撃の3相に GUARD / STRIKE / GUARD を割り当ててあるのは、
;   発生（構えに沈む）→ 持続（振り抜く）→ 硬直（構えに戻る）
; という往復にして、**硬直がまだ続いていること**を立ちと区別して見せるためである。
; 硬直を立ち（SPR_POSE_STAND）にすると「もう動けるのに動かない」ように見える。
; --------------------------------------------------------------------------
act_pose_by_state:
        .byte SPR_POSE_STAND    ; ACT_ST_IDLE        立ち（動いている間は ACT_POSE_MOVE）
        .byte SPR_POSE_GUARD    ; ACT_ST_ATK_START   発生（構えに沈む）
        .byte SPR_POSE_STRIKE   ; ACT_ST_ATK_ACTIVE  持続（斬り。判定が出ている）
        .byte SPR_POSE_GUARD    ; ACT_ST_ATK_RECOVER 硬直（構えに戻る。次の段の受付でもある）
        .byte SPR_POSE_HURT     ; ACT_ST_HURT        のけぞり
        .byte SPR_POSE_DOWN     ; ACT_ST_DOWN        ダウン（猶予中）
        .byte SPR_POSE_DOWN     ; ACT_ST_OUT         猶予切れ
.assert * - act_pose_by_state = ACT_ST_COUNT, error, "act_pose_by_state の項数が状態の数と違う"

; --------------------------------------------------------------------------
; 通常攻撃（Aボタン）の段ごとの表。添字は 0..ACT_COMBO_MAX-1（段数-1）。
;
; 攻撃矩形は「向き × 段数」で決まるが、向きは**前方への鏡像**で作る。
; 表が持つのは前方への値だけであり、左向きのときは体の前端を基準に反転する:
;   右向き: 矩形の左端 = ワールドX + 体幅 + atk_reach
;   左向き: 矩形の右端 = ワールドX        - atk_reach
;   縦    : 足元から上へ atk_h（奥行きはY許容幅 act_depth_tol が見るので、縦は保険の判定）
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

; 硬直。**次の段の受付時間でもある**（長いほど繋げやすい）。
; 最終段（3段目）だけは次の段が無いので、受付時間ではなく**純粋な隙**である。
; 20 → 14 に縮めた。理由は2つ:
;   * 20 だと 1コンボの締めで 発生8 + 持続4 + 硬直20 = 32 フレーム（0.53秒）動けない。
;     半秒を超える無反応は、姿勢を出しても「固まった」と読まれる長さである。
;   * のけぞり（ACT_HURT_FRAMES = 14）より長く、「殴ったときの方が殴られたときより
;     長く固まる」状態だった。**当てれば五分・空振れば損**が素直な形なので、
;     最終段の硬直をのけぞりと同じ 14 に揃えた（ヒットストップは双方に等しく乗るので
;     相殺される）。
atk_recover:
        .byte 10, 12, 14
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
