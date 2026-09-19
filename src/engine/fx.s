; fx.s — 短命の効果スプライト（斬り・衝撃・判定枠など）の枠と寿命。
;
; **エンティティではない。** 当たり判定にも AI の走査にも一切混ざらない。
; 混ぜないために、エンティティテーブルの空き枠（ENT_FREE_FIRST..）ではなく独立した
; 表にしてある。空き枠を使うと、いずれそこに入る飛び道具を走査する側が
; 「当たらない・考えない effect」を毎回はじく仕事を背負うことになる。
;
; 担当の線引き（P1 受入での主の指摘3への回答）:
;   engine … 「出す仕組み」。枠・寿命・消し忘れの防止・OAM への展開
;   action / ai … 「いつ何を出すか」。攻撃の届く位置に斬りを出す、当たった位置に衝撃を出す
;
; 寿命が尽きたら engine が自動で消す。**呼び出し側に後始末をさせない**。
; 消し忘れは「画面に判定の残骸が残る」という、最も気付きにくい形の不具合になる。
;
; 出し方（action-dev / ai-dev 向け）:
;
;     lda #<world_x          ; ワールドX（16bit）。攻撃の届く位置は呼び出し側が計算する
;     sta fx_arg_x_lo
;     lda #>world_x
;     sta fx_arg_x_hi
;     lda ent_y, x           ; 足元（接地線）の画面Y。レーン番号から引くなら fx_spawn_lane
;     sta fx_arg_y
;     lda #SPR_TILE_SWIPE
;     sta fx_arg_tile
;     lda #SPR_PAL_EFFECT
;     sta fx_arg_attr        ; 左向きに出すなら ora #ENT_ATTR_HFLIP
;     lda #6
;     sta fx_arg_life        ; 寿命（フレーム）。攻撃の持続フレーム数を入れるのが素直
;     jsr fx_spawn           ; キャリークリア = 出た / キャリーセット = 枠が空いていない
;
; X と Y は壊さない（エンティティ番号を X に持ったまま呼べる）。
; 枠が空いていなくても呼び出し側は何もしなくてよい。効果は消えてよいものである
;（ADR-0002 の SPR_CLASS_EFFECT「消えても遊びが壊れない」）。
.include "constants.inc"
.include "zeropage.inc"

.export fx_life, fx_x_lo, fx_x_hi, fx_y, fx_tile, fx_attr
.export fx_clear_all, fx_spawn, fx_spawn_lane, fx_update

.import lane_y_of

.segment "BSS"

; 枠は「寿命が 0 なら空き」の1本で管理する。別に in_use を持たない
;（2つ持つと、片方だけ落ちた状態が作れてしまう）。
fx_life:  .res FX_MAX        ; 残りフレーム数。0 = 空き
fx_x_lo:  .res FX_MAX        ; ワールドX 下位
fx_x_hi:  .res FX_MAX        ; ワールドX 上位
fx_y:     .res FX_MAX        ; 足元（接地線）の画面Y
fx_tile:  .res FX_MAX        ; タイル番号（8x16。偶数）
fx_attr:  .res FX_MAX        ; OAM 属性

.segment "CODE"

; 全部消す。シーン開始時に呼ぶこと（ent_clear_all と同じ場所）。
.proc fx_clear_all
        lda #0
        ldx #FX_MAX - 1
@loop:
        sta fx_life, x
        dex
        bpl @loop
        rts
.endproc

; A = レーン番号。そのレーンの接地線に出す。ほかの引数は fx_spawn と同じ。
; レーン番号 → 足元Y の対応を呼び出し側に持たせないための入口である。
.proc fx_spawn_lane
        jsr lane_y_of                    ; A = そのレーンの足元Y（X は壊さない）
        sta fx_arg_y
        ; fx_spawn へ落ちる
.endproc

; fx_arg_* を空き枠へ写す。
;   出力: キャリークリア = 出した / キャリーセット = 空き枠が無かった
;   X と Y は壊さない。
.proc fx_spawn
        txa
        pha
        ldx #FX_MAX - 1
@find:
        lda fx_life, x
        beq @found
        dex
        bpl @find
        pla                              ; 空きなし。何もしない（効果は消えてよい）
        tax
        sec
        rts
@found:
        lda fx_arg_x_lo
        sta fx_x_lo, x
        lda fx_arg_x_hi
        sta fx_x_hi, x
        lda fx_arg_y
        sta fx_y, x
        lda fx_arg_tile
        sta fx_tile, x
        lda fx_arg_attr
        sta fx_attr, x
        ; 寿命 0 で呼ばれたら 1 フレームは出す。黙って何も出ないと、呼び出し側からは
        ; 「枠が無かった」のか「寿命を入れ忘れた」のか区別がつかない。
        lda fx_arg_life
        bne @life
        lda #1
@life:
        sta fx_life, x
        pla
        tax
        clc
        rts
.endproc

; 毎フレーム1回、メインループから呼ぶ。寿命を1つ減らし、尽きた枠を空きに戻す。
; **フレームの頭で呼ぶこと。** 出した当のフレームから数えて fx_arg_life フレームぶん
; 画面に出る形にするためである（出した直後に減らすと、寿命1が一度も出ない）。
.proc fx_update
        ldx #FX_MAX - 1
@loop:
        lda fx_life, x
        beq @next
        dec fx_life, x
@next:
        dex
        bpl @loop
        rts
.endproc
