; entity.s — エンティティテーブル（Structure of Arrays）。
;
; 属性ごとに1本の配列を持つ。構造体の配列にすると `lda (ptr),y` か
; 「添字×構造体長」の掛け算が要るが、SoA なら `lda ent_x_lo, x` の1命令で引ける。
; 6502 ではこの差がそのまま毎フレームのサイクル数になる。
;
; 添字の区画割り（ENT_PLAYER / ENT_ALLY_FIRST / ENT_ENEMY_FIRST / ENT_FREE_FIRST）と
; 上限 MAX_ENTITIES は src/constants.inc にある。ここにマジックナンバーを書かない。
;
; ここに持ってよいのは「座標・状態・表示のための箱」だけである。
; キャラ名・話番号・シナリオ固有の情報を持たせてはならない（CLAUDE.md 第1条）。
; 体格型 (ent_body) と AI 型 (ent_ai) は data/ 側の ID を入れる箱にとどめる。
.include "constants.inc"
.include "zeropage.inc"

.export ent_active, ent_x_lo, ent_x_hi, ent_y
.export ent_state, ent_class, ent_body, ent_ai, ent_tile, ent_attr
.export ent_tile0
.export ent_clear_all, ent_activate, ent_kill, ent_find_free, ent_set_pose

.import depth_clamp

.segment "BSS"

ent_active:     .res MAX_ENTITIES   ; ENT_INACTIVE / ENT_ACTIVE
ent_x_lo:       .res MAX_ENTITIES   ; ワールドX 下位（16bit。ステージは横に長い）
ent_x_hi:       .res MAX_ENTITIES   ; ワールドX 上位
ent_y:          .res MAX_ENTITIES   ; 足元の画面Y。**奥行きそのもの**（連続。ADR-0009）。
                                    ; 歩ける帯の中に居ることは src/engine/depth.s が保つ
ent_state:      .res MAX_ENTITIES   ; 状態 ID。語彙を決めるのは action-dev
ent_class:      .res MAX_ENTITIES   ; SPR_CLASS_*（OAM 並べ替えの重み。ADR-0002）
ent_body:       .res MAX_ENTITIES   ; BODY_*（描画タイル数が変わる）
ent_ai:         .res MAX_ENTITIES   ; AI 型 ID の箱。中身を決めるのは ai-dev / data
ent_tile:       .res MAX_ENTITIES   ; いま描くタイル番号（= ent_tile0 + 姿勢 * SPR_POSE_STRIDE）
ent_attr:       .res MAX_ENTITIES   ; OAM 属性（パレット・背面・影の有無）。**水平反転は載せない**
; ここから下は ent_attr より後ろに置くこと。ent_active..ent_attr の並びは
; テストが「配列が MAX_ENTITIES ごとに並ぶ」ことを見張っている区間である。
ent_tile0:      .res MAX_ENTITIES   ; 役割の先頭タイル（SPR_ROLE_*）。姿勢の基準点

.segment "CODE"

; テーブル全体を空にする。シーン開始時に呼ぶ。
.proc ent_clear_all
        lda #ENT_INACTIVE
        ldx #MAX_ENTITIES - 1
@loop:
        sta ent_active, x
        dex
        bpl @loop
        rts
.endproc

; X = エンティティ番号。呼ぶ前に ent_x_lo/hi, ent_y, ent_class, ent_body,
; ent_tile, ent_attr を詰めておくこと（SoA なので呼び出し側が直接書くのが速い）。
; ここでは「有効化」と、足元Yを歩ける帯に収めることだけを行う。
;
; ついでに2つ、**置いた側が忘れても成立する**ようにしてある:
;   * ent_tile を役割の先頭タイル ent_tile0 として覚える（以降は ent_set_pose で姿勢を切る）
;   * 足元に影を敷く印 (ENT_ATTR_SHADOW) を立てる。影は奥行きを画面に出す唯一の
;     手がかりなので、既定を「敷く」にしてある。接地していないもの（効果・飛び道具）だけが
;     有効化のあとで下ろすこと。
.proc ent_activate
        lda #ENT_ACTIVE
        sta ent_active, x
        lda ent_tile, x
        sta ent_tile0, x                ; 役割の先頭タイルを覚える
        lda ent_attr, x
        ora #ENT_ATTR_SHADOW
        sta ent_attr, x
        lda ent_y, x
        jsr depth_clamp                 ; 歩ける帯に収める（置いた側が帯を知らなくてよい）
        sta ent_y, x
        rts
.endproc

; X = エンティティ番号, A = 姿勢（SPR_POSE_*）。描くタイルを姿勢のぶんだけずらす。
;
; 「いま何をしているか」を絵にするのは action-dev / ai-dev の領分である。engine が持つのは
; 役割の先頭タイルを覚えておく箱（ent_tile0）と、この1本の差し替えまで。
; 役割ごとの先頭タイルが変わっても呼び出し側は何も直さなくてよい。
; A が SPR_POSE_COUNT 以上でも表の外を引くことはない（タイルがずれるだけ）が、
; 役割16タイルの枠は出るので、姿勢の語彙は SPR_POSE_* に収めること。
.proc ent_set_pose
        asl a                           ; 姿勢 * SPR_POSE_STRIDE
        clc
        adc ent_tile0, x
        sta ent_tile, x
        rts
.endproc

.assert SPR_POSE_STRIDE = 2, error, "ent_set_pose の asl a が SPR_POSE_STRIDE と合っていない"

; X = エンティティ番号。
.proc ent_kill
        lda #ENT_INACTIVE
        sta ent_active, x
        rts
.endproc

; 空きスロットを区画の中から探す。
;   入力: X = 先頭の添字, Y = 個数（例: ldx #ENT_FREE_FIRST / ldy #ENT_FREE_COUNT）
;   出力: キャリークリア = 見つかった（X が添字）/ キャリーセット = 空きなし
.proc ent_find_free
@loop:
        lda ent_active, x
        beq @found
        inx
        dey
        bne @loop
        sec
        rts
@found:
        clc
        rts
.endproc
