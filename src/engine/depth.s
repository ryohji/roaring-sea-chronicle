; depth.s — 擬似奥行きの座標系。
;
; **レーン（4段階の量子化）は ADR-0009 で廃止した。奥行きは連続である。**
; 廃止したのはレーンであって奥行きではない。横から見た画面で上下に動き、
; 足元Y（ent_y）の大小で前後が決まる、という擬似奥行きそのものは維持する。
;
; ここが持つのは座標系のプリミティブだけである:
;   * 歩ける帯（足元Yの上下限）と、そこへのクランプ
;   * 足元Yを符号つきの量だけ動かす入口（ent_depth_move）
;   * 2つの足元Yの距離（depth_distance）
;
; **「何ドットまで当たるか」（Y許容幅）は action-dev の持ち分である**（ADR-0009）。
; engine は距離を測る手段までしか持たない。閾値をここに置いてはならない。
; 奥行きの移動速度も action / ai の持ち分である（ここは量を受け取って動かすだけ）。
;
; 歩ける帯はステージの性質なので、**コードに埋めずに変数で持つ**。
; ステージ長（scroll.s の stage_w）と同じ扱いである。既定値は constants.inc の
; DEPTH_Y_MIN_DEFAULT / DEPTH_Y_MAX_DEFAULT にあり、P3 で stage-author が
; data/layouts/ から depth_set_band を呼べばそれで済む形にしてある。
.include "constants.inc"
.include "zeropage.inc"

.export depth_init, depth_set_band, depth_clamp, depth_distance, ent_depth_move
.export depth_y_min, depth_y_max

.import ent_y

.segment "BSS"

depth_y_min: .res 1          ; 帯の奥端（最も小さい足元Y）
depth_y_max: .res 1          ; 帯の手前端（最も大きい足元Y）

.segment "CODE"

; 起動時に1回呼ぶ。帯を既定値に置く。
.proc depth_init
        lda #DEPTH_Y_MIN_DEFAULT
        ldy #DEPTH_Y_MAX_DEFAULT
        ; depth_set_band へ落ちる
.endproc

; A = 帯の奥端（最小の足元Y）, Y = 帯の手前端（最大の足元Y）。
; X は壊さない。**帯の上下を逆に渡さないこと**（クランプが帯の外へ押し出す）。
.proc depth_set_band
        sta depth_y_min
        sty depth_y_max
        rts
.endproc

; A = 足元Yの候補 → A = 歩ける帯に収めた足元Y。X と Y は壊さない。
; 置いた側が帯の値を知らなくてよいようにするための入口である。
.proc depth_clamp
        cmp depth_y_min
        bcc @too_far
        cmp depth_y_max
        beq @done
        bcs @too_near
@done:
        rts
@too_far:
        lda depth_y_min
        rts
@too_near:
        lda depth_y_max
        rts
.endproc

; X = エンティティ番号, A = 奥行き方向の移動量（符号つき。負 = 奥へ、正 = 手前へ）。
; 足元Yをそのぶん動かし、歩ける帯でクランプする。**補間という状態は持たない。**
; 押したフレームに押したぶんだけ動く（ADR-0009 の要点）。
; X は壊さない。A は動かした後の足元Y。ゼロページは壊さない。
.proc ent_depth_move
        cmp #$80
        bcs @backward            ; 負 = 奥へ（画面上方）

        clc                      ; 手前へ
        adc ent_y, x
        bcs @clamp_near          ; 255 を越えた
        cmp depth_y_max
        bcc @store
        beq @store
@clamp_near:
        lda depth_y_max
        jmp @store

@backward:
        clc
        adc ent_y, x
        bcc @clamp_far           ; 0 を下回った
        cmp depth_y_min
        bcs @store
@clamp_far:
        lda depth_y_min
@store:
        sta ent_y, x
        rts
.endproc

; 2つの足元Yの距離を返す。**レーン番号の一致ではなく、Y の近さで当たりを見るための道具**である
; （ADR-0009）。許容幅と比べるのは呼び出し側（action-dev）の仕事である。
;   入力: A = 足元Y その1, Y = 足元Y その2
;   出力: A = |その1 - その2|（Z = 一致なら1）
;   tmp0 を壊す。X は壊さない（エンティティ番号を持ったまま呼べる）。
.proc depth_distance
        sty tmp0
        sec
        sbc tmp0
        bcs @done
        eor #$FF                 ; 負なら符号反転して絶対値にする
        clc
        adc #1
@done:
        rts
.endproc
