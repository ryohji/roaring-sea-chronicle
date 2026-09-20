; action.inc — アクションの語彙（状態IDと立場ID）。**調整値はここに書かない**
;              （数値は action_params.inc / action_params.s）。
;
; ent_state（src/engine/entity.s）は「状態 ID の箱」であり、語彙を決めるのは
; action-dev である（engine は中身を見ない）。ここがその語彙の一次情報。
.ifndef ACTION_INC
ACTION_INC = 1

.include "constants.inc"

; --- ent_state に入る値 ---
; 並び順に意味がある: ACT_ST_DOWN 以上が「HP が尽きた側」である。
; 判定は cmp #ACT_ST_DOWN / bcs の1回で済ませているので、間に値を挿してはならない。
ACT_ST_IDLE        = 0   ; 立ち・歩き。入力を受け付ける
ACT_ST_ATK_START   = 1   ; 攻撃の発生中（判定はまだ出ていない）
ACT_ST_ATK_ACTIVE  = 2   ; 攻撃の持続中（判定が出ている）
ACT_ST_ATK_RECOVER = 3   ; 攻撃の硬直中（次の段の受付時間でもある）
ACT_ST_HURT        = 4   ; のけぞり
ACT_ST_DOWN        = 5   ; ダウン（猶予中）。ADR-0005。操作は一切効かない（ADR-0006）
ACT_ST_OUT         = 6   ; 猶予切れ。操作キャラなら シナリオ失敗、それ以外は 戦線離脱／撃破
ACT_ST_COUNT       = 7

.assert ACT_ST_IDLE = ENT_ST_IDLE, error, "action の待機状態が engine の ENT_ST_IDLE と違う"
.assert ACT_ST_OUT = ACT_ST_DOWN + 1, error, "ACT_ST_DOWN / ACT_ST_OUT が隣り合っていない（cmp 1回の判定が壊れる）"

; --- 立場（猶予の長さや初期HPの引き方が変わる）---
; エンティティ番号の区画（ENT_PLAYER / ENT_ALLY_FIRST / ENT_ENEMY_FIRST）から求める。
ACT_ROLE_PLAYER = 0
ACT_ROLE_ALLY   = 1
ACT_ROLE_ENEMY  = 2

; --- act_flags のビット ---
ACT_F_HIT   = %10000000  ; この振りで既に当てた（同じ振りで2度当たらない）。bmi で見る
; 今フレーム動いた（歩きの絵にするため）。act_update_actor が毎フレーム下ろし、
; 移動した側（act_move_left / act_move_right / act_player_depth）が立てる。
; **状態ではない。**「1フレームの事実」であり、次のフレームには持ち越さない。
ACT_F_MOVED = %01000000
; 振り始めに固定した向き（0 = 右 / 1 = 左）。**commit の一部**である。
; 予備動作の間に向きが変わると、避けたはずの位置に判定が出てしまい、
; 予備動作を長くした意味が消える。act_start_attack が写し、攻撃中は毎フレーム
; これを act_face へ書き戻す（誰が向きを触っても振りは元の方向に出る）。
ACT_F_CFACE = %00100000
; この枠は飛び道具である（エンティティの空き枠 ENT_FREE_FIRST.. に入る）。
; **立てるのは action だけ**。engine の描画と OAM 並べ替えはこのビットを見ない
; （ent_active だけで描かれる）。見るのは action の飛び道具の進行 act_proj_update と、
; 「このエンティティは人ではない」を知りたい所だけである。
ACT_F_PROJ  = %00010000

.endif
