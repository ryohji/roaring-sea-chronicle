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
.export atk_proj, atk_speed, atk_life
.export act_mset_base, act_mset_len, act_mset_by_role

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
; 技構成（ACT_MSET_*）→ 技表のどの行から何段ぶん使うか。
;
; **操作キャラのコンボ表と敵の攻撃を分けるための1枚である。**
; 以前は敵が操作キャラの atk_* の1段目を借りていたので、敵の発生が 4 フレーム
; （0.067秒）だった。人間の反応の限界（12フレーム）より短く、**原理的に避けられない**。
; 避けられない攻撃の前では立ち位置を選ぶ意味が無く、空間が使われない。
;
; act_start_attack は「段数」で呼ばれる（ai-dev の呼び方を変えずに済む）。
; 行は act_mset_base[技構成] + 段数 - 1 で引く。段数が act_mset_len を超えたら丸める。
; --------------------------------------------------------------------------
act_mset_base:
        .byte ATK_ROW_PARTY      ; ACT_MSET_PARTY  操作キャラ・仲間
        .byte ATK_ROW_ENEMY      ; ACT_MSET_ENEMY  近接の敵
        .byte ATK_ROW_SHOOT      ; ACT_MSET_SHOOT  遠隔の敵
        .byte ATK_ROW_BULLET     ; ACT_MSET_BULLET 飛び道具そのもの
.assert * - act_mset_base = ACT_MSET_COUNT, error, "act_mset_base の項数が技構成の数と違う"

act_mset_len:                    ; その技構成が持つ段数（コンボの長さ）
        .byte ACT_COMBO_MAX      ; ACT_MSET_PARTY
        .byte 1                  ; ACT_MSET_ENEMY
        .byte 1                  ; ACT_MSET_SHOOT
        .byte 1                  ; ACT_MSET_BULLET
.assert * - act_mset_len = ACT_MSET_COUNT, error, "act_mset_len の項数が技構成の数と違う"

; 立場（ACT_ROLE_*）ごとの既定の技構成。act_init_entity が引く。
; **遠隔の敵は ai-dev が生成直後に act_mset を ACT_MSET_SHOOT へ書き換える。**
; ここを立場で分けているのは「詰め忘れても近接として成立する」ための既定にすぎない。
act_mset_by_role:
        .byte ACT_MSET_PARTY     ; ACT_ROLE_PLAYER
        .byte ACT_MSET_PARTY     ; ACT_ROLE_ALLY
        .byte ACT_MSET_ENEMY     ; ACT_ROLE_ENEMY
.assert * - act_mset_by_role = 3, error, "act_mset_by_role の項数が立場の数と違う"

; --------------------------------------------------------------------------
; 技表。1行 = 1つの振り。添字は ATK_ROW_*。
;
;   行 0..2            操作キャラ・仲間のコンボ 1〜3段目（Aボタン）
;   行 ATK_ROW_ENEMY   敵の近接。**予備動作が長い**（ACT_ENEMY_TELL）
;   行 ATK_ROW_SHOOT   敵の遠隔。判定は出さず、持続に入った瞬間に弾を1つ出す
;   行 ATK_ROW_BULLET  飛び道具そのもの。弾が自分の当たり判定を引くための行
;
; 攻撃矩形は「向き × 行」で決まるが、向きは**前方への鏡像**で作る。
; 表が持つのは前方への値だけであり、左向きのときは体の前端を基準に反転する:
;   右向き: 矩形の左端 = ワールドX + 体幅 + atk_reach
;   左向き: 矩形の右端 = ワールドX        - atk_reach
;   縦    : 足元から上へ atk_h（奥行きはY許容幅 act_depth_tol が見るので、縦は保険の判定）
; 飛び道具の行だけは別で、**弾の絵そのもの**（原点から幅 atk_w・足元から高さ atk_h）が
; 当たり判定になる（act_build_proj_rect）。reach は使わない。
;
; 1振りは最初に当たったフレームで終わり（同じ振りで2度当たらない）。
; そのフレームに矩形が重なっている相手は全員に当たる。
; --------------------------------------------------------------------------
atk_startup:                     ; 発生（振り始めから判定が出るまで）。**敵はここが命**
        .byte  4,  5,  8, ACT_ENEMY_TELL, ACT_SHOOT_TELL,  1
.assert * - atk_startup = ATK_ROW_COUNT, error, "atk_startup の項数が技表の行数と違う"

atk_active:                      ; 持続（判定が出ているフレーム数）。長いほど当てやすい
        .byte  3,  3,  4,  3,  2,  1
.assert * - atk_active = ATK_ROW_COUNT, error, "atk_active の項数が技表の行数と違う"

; 硬直。**次の段の受付時間でもある**（長いほど繋げやすい）。
; 最終段（3段目）だけは次の段が無いので、受付時間ではなく**純粋な隙**である。
; 20 → 14 に縮めた。理由は2つ:
;   * 20 だと 1コンボの締めで 発生8 + 持続4 + 硬直20 = 32 フレーム（0.53秒）動けない。
;     半秒を超える無反応は、姿勢を出しても「固まった」と読まれる長さである。
;   * のけぞり（ACT_HURT_FRAMES = 14）より長く、「殴ったときの方が殴られたときより
;     長く固まる」状態だった。**当てれば五分・空振れば損**が素直な形なので、
;     最終段の硬直をのけぞりと同じ 14 に揃えた（ヒットストップは双方に等しく乗るので
;     相殺される）。
;
; 敵の硬直（18 / 22）は**避けた側の取り分**である。予備動作を見て避けたのに
; 何の見返りも無ければ、避ける意味が無い。長くするほど「避けて殴り返す」が成立し、
; 短くするほど敵が固くなる。予備動作（ACT_ENEMY_TELL）と対で回すノブである。
atk_recover:
        .byte 10, 12, 14, 18, 22,  1
.assert * - atk_recover = ATK_ROW_COUNT, error, "atk_recover の項数が技表の行数と違う"

; 体の前端から前方へのオフセット（ドット）。リーチ。
; 敵の近接（2）を操作キャラの1段目と同じにしてあるのは、**間合いを対称にする**ためである。
; 操作キャラは3段目（4）で敵より先に届く。そこが間合いの取り合いの取っ掛かりになる。
; 遠隔（6）は矩形ではなく**弾の出る位置**（銃口）として使われる。
atk_reach:
        .byte  2,  3,  4,  2,  6,  0
.assert * - atk_reach = ATK_ROW_COUNT, error, "atk_reach の項数が技表の行数と違う"

atk_w:                           ; 攻撃矩形の幅（ドット）。弾の行は**弾自身の幅**
        .byte 14, 14, 18, 14,  8,  8
.assert * - atk_w = ATK_ROW_COUNT, error, "atk_w の項数が技表の行数と違う"

atk_h:                           ; 攻撃矩形の高さ（足元から上へ・ドット）。弾の行は弾自身の高さ
        .byte 24, 24, 26, 24, 16, 16
.assert * - atk_h = ATK_ROW_COUNT, error, "atk_h の項数が技表の行数と違う"

; 威力。遠隔の振りかぶり（ATK_ROW_SHOOT）は 0 である。当てるのは弾なので、
; 振りかぶりそのものには判定が無い（判定を出すと、至近で撃たれたとき二重に当たる）。
atk_dmg:
        .byte  3,  3,  5,  3,  0,  2
.assert * - atk_dmg = ATK_ROW_COUNT, error, "atk_dmg の項数が技表の行数と違う"

atk_kb:                          ; ノックバックの初速（ドット/フレーム）。減衰は ACT_KB_DECAY
        .byte  3,  3,  8,  4,  0,  3
.assert * - atk_kb = ATK_ROW_COUNT, error, "atk_kb の項数が技表の行数と違う"

; --------------------------------------------------------------------------
; 飛び道具の列。**素手で殴る行では atk_proj = ATK_NO_PROJ ($FF) である。**
;   atk_proj  … 持続に入った瞬間に出す弾の行（ATK_ROW_*）。$FF なら弾を出さない
;   atk_speed … 弾の速さ。1/16 ドット/フレーム（32 = 2.0 ドット/f）。弾の行でだけ意味を持つ
;   atk_life  … 弾の寿命（フレーム）。速さ×寿命が射程になる（32/16 × 96 = 192 ドット）
; --------------------------------------------------------------------------
atk_proj:
        .byte ATK_NO_PROJ, ATK_NO_PROJ, ATK_NO_PROJ
        .byte ATK_NO_PROJ                ; ATK_ROW_ENEMY  近接（素手）
        .byte ATK_ROW_BULLET             ; ATK_ROW_SHOOT  振りかぶりの終わりに弾が出る
        .byte ATK_NO_PROJ                ; ATK_ROW_BULLET 弾が弾を産まない
.assert * - atk_proj = ATK_ROW_COUNT, error, "atk_proj の項数が技表の行数と違う"

atk_speed:
        .byte  0,  0,  0,  0,  0, 32
.assert * - atk_speed = ATK_ROW_COUNT, error, "atk_speed の項数が技表の行数と違う"

atk_life:
        .byte  0,  0,  0,  0,  0, 96
.assert * - atk_life = ATK_ROW_COUNT, error, "atk_life の項数が技表の行数と違う"

; 相のフレーム数が 0 だと状態機械の「残りフレームを1減らして 0 なら次へ」が
; 255 フレームに化ける。表を触るときの安全柵として、リンク時に縛っておく。
.assert ACT_COMBO_MAX >= 2 && ACT_COMBO_MAX <= 3, error, "コンボは2〜3段まで"

; 敵の予備動作は「見てから避けられる」長さでなければならない。
; 人間の反応は速くても 12 フレームであり、**意図して避けるなら 15 は要る**
; （主の診断）。ここを下回る値を入れたら、それは分離した意味が消えたということである。
.assert ACT_ENEMY_TELL >= 15, error, "敵の予備動作が短すぎる（見てから避けられない）"
.assert ACT_SHOOT_TELL >= 15, error, "敵の遠隔の予備動作が短すぎる（見てから避けられない）"
; 点滅で予備動作を見せる行と、見せない行の切れ目が正しいこと。
; 操作キャラの初段まで点滅すると画面が騒がしくなるだけである。
.assert ACT_TELL_MIN > 8, error, "ACT_TELL_MIN が小さすぎる（操作キャラの振りまで点滅する）"
.assert ACT_TELL_MIN <= ACT_ENEMY_TELL, error, "ACT_TELL_MIN が敵の予備動作より長い（溜めが見えない）"

; 小走り（移動の強弱）。代償が消える値を入れたら、それは「速いだけ」になったということ。
.assert ACT_RUN_SPEED > ACT_MOVE_SPEED, error, "小走りが歩きより速くない"
.assert ACT_TURN_VEL >= ACT_MOVE_SPEED, error, "歩く速さでも向きが変えられない（歩きの手触りが変わる）"
.assert ACT_ATK_VEL >= ACT_MOVE_SPEED, error, "歩く速さでも攻撃できない（歩きの手触りが変わる）"
