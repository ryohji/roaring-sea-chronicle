; lane_compat.s — **暫定。移行が済んだら、このファイルごと消す。**
;
; レーンは ADR-0009 で廃止した。engine 側の実装（補間の状態機械・lane_ground_y・
; lane_y_of・ent_lane_move・lane_update_all）はもう無い。座標系は src/engine/depth.s である。
;
; ここに残してあるのは、src/action/ と src/ai/ がまだ参照している**名前だけ**である。
; engine-dev は src/action/ と src/ai/ を書き換えられない（CLAUDE.md 第3節）。
; 名前が消えるとリンクが通らず、ほかの担当の作業まで止まる。そこで
; 「常に 0 の箱」と「何もしない入口」を置いて、**ビルドだけを通す。**
;
; **この暫定の間、奥行きは死んでいる**ことを承知すること:
;   * 上下入力で操作キャラは動かない（act_player_lane が ent_lane_move を呼ぶため）
;   * AI は奥行き方向に寄らない（ai_lane_follow が「同じレーン」と見るため）
;   * 攻撃の当たり判定から奥行きが抜ける（act_lane_ok が常に通るため）
;
; 移行のしかた（action-dev / ai-dev）:
;   ent_lane        → ent_y（足元Yそのもの。番号ではない）
;   ent_lane_from   → 無くなる（補間という状態が無い）
;   ent_lane_step   → 無くなる（常に 0。「移動中」という状態が存在しない）
;   ent_lane_move   → ent_depth_move（X = エンティティ番号, A = 符号つきの移動量）
;   lane_distance   → depth_distance（A, Y = 足元Y → A = |差|。tmp0 を壊す点は同じ）
;   LANE_LAST       → depth_y_max（定数ではなく変数。帯はステージごとに変わる）
; 移り終わったら、このファイルと constants.inc の LANE_LAST を消すこと。
.include "constants.inc"

.export ent_lane, ent_lane_from, ent_lane_step
.export ent_lane_move
.export lane_distance

.import depth_distance

; レーン番号はもう存在しない。読み手に 0 を返すためだけの箱である。
; 起動時の RAM クリア（src/init.s）で 0 になり、**誰も書かない**ので 0 のままである。
; 3つの名前を同じ実体に重ねてあるのは、消すときに迷わないようにするためである。
.segment "BSS"

lane_compat_zero: .res MAX_ENTITIES

ent_lane      = lane_compat_zero
ent_lane_from = lane_compat_zero
ent_lane_step = lane_compat_zero

; |A - Y| を返すという約束は depth_distance と同じなので、別名にしておく。
; 呼び出し側が渡す値をレーン番号から足元Yに替えれば、そのまま意味が通る。
lane_distance = depth_distance

.segment "CODE"

; 何もしない。奥行きの移動は ent_depth_move（src/engine/depth.s）に移った。
.proc ent_lane_move
        rts
.endproc
