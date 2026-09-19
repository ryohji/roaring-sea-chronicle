; collide.s — 当たり判定の**プリミティブ**。
;
; ここに置いてよいのは「2つの矩形が重なっているか」までである。
; 奥行き方向の距離は src/engine/depth.s の depth_distance が持つ（ADR-0009）。
; 攻撃の当たり方・ヒットストップ・**足元Yの許容幅**といった
; 「判定ルール」は action-dev の領分であり、engine は触らない
; （.claude/agents/engine-dev.md の禁止節）。
;
; 矩形は「ワールドX 16bit / 画面Y 8bit / 幅 8bit / 高さ 8bit」。
; X だけ 16bit なのは、ステージが横に長くカメラを跨ぐためである。
; Y は1画面に収まるので 8bit で足りる。
.include "constants.inc"
.include "zeropage.inc"

.export rect_overlap

.segment "CODE"

; rect_a と rect_b が重なっているかを返す。
;   入力: ゼロページの rect_a / rect_b（オフセットは RECT_X_LO / RECT_X_HI / RECT_Y / RECT_W / RECT_H）
;   出力: キャリーセット = 重なっている / キャリークリア = 重なっていない
;   破壊: A, tmp0, tmp1
; 辺が接しているだけ（右端 == 左端）は「重なっていない」とする。
; こうしないと幅0の隙間に判定が出て、隣接する当たり判定が二重に当たる。
.proc rect_overlap
        ; b の右端 = b.x + b.w （16bit）
        lda rect_b + RECT_X_LO
        clc
        adc rect_b + RECT_W
        sta tmp0
        lda rect_b + RECT_X_HI
        adc #0
        sta tmp1

        ; a.x < b の右端 か
        lda rect_a + RECT_X_LO
        cmp tmp0
        lda rect_a + RECT_X_HI
        sbc tmp1
        bcs @no                          ; a.x >= b の右端 → 重ならない

        ; a の右端 = a.x + a.w
        lda rect_a + RECT_X_LO
        clc
        adc rect_a + RECT_W
        sta tmp0
        lda rect_a + RECT_X_HI
        adc #0
        sta tmp1

        ; b.x < a の右端 か
        lda rect_b + RECT_X_LO
        cmp tmp0
        lda rect_b + RECT_X_HI
        sbc tmp1
        bcs @no

        ; --- Y は 8bit。下端が 255 を超える場合は「必ず a.y より下」なので通す ---
        lda rect_b + RECT_Y
        clc
        adc rect_b + RECT_H
        bcs @b_bottom_ok
        cmp rect_a + RECT_Y
        bcc @no                          ; b の下端 < a.y
        beq @no                          ; 接しているだけ
@b_bottom_ok:
        lda rect_a + RECT_Y
        clc
        adc rect_a + RECT_H
        bcs @yes
        cmp rect_b + RECT_Y
        bcc @no
        beq @no
@yes:
        sec
        rts
@no:
        clc
        rts
.endproc
