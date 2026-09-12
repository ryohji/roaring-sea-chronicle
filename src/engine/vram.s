; vram.s — VRAM 転送キュー。
;
; VRAM に書けるのは VBlank の間だけである。ところが1フレームに書きたい量は
; 一定ではない（スクロールで列が現れた／パレットが変わった／テキストが出た）。
; そこで「書きたいもの」をメインループがここへ積み、NMI が**予算のぶんだけ**取り出す。
;
; 予算で刻むことがこのモジュールの存在理由である。VBlank は約2270サイクル、
; OAM DMA が約513サイクルを持っていくので、転送に使えるのはおよそ1700サイクル。
; 積まれた全部をその場で流すと、溢れたぶんが描画期間に食い込んで画面が壊れる。
;
; 記録の形式は転送先 VRAM アドレス＋長さ＋データの汎用形である。
; スクロールのネームテーブル列に限らず、パレット変更も、P7 のテキストウィンドウも
; 同じ形でここに載る。載せる側は「どう書くか」を知らなくてよい。
;
;   +0 VQ_LEN     データ長 (1..VQ_MAX_LEN)
;   +1 VQ_ADDR_HI 転送先 VRAM アドレス 上位
;   +2 VQ_ADDR_LO 同 下位
;   +3 VQ_FLAGS   VQ_INC1 / VQ_INC32 / VQ_STEP|増分
;   +4.. データ
;
; 書き手が2人（メインループと NMI）いるので、変数は「片方しか書かない」形に分けてある。
;   メイン: vq_wr に組み立て、最後に vq_tail を1回書いて公開する
;   NMI   : vq_head だけを進める
; 記録は vq_tail を動かすまで NMI から見えない。したがって組み立ての途中で
; NMI が割り込んでも、半端な記録が転送されることはない。
.include "constants.inc"
.include "zeropage.inc"

.export vram_queue_reset, vram_queue_open, vram_queue_byte, vram_queue_close
.export vram_queue_flush, vram_queue_pending
.export vq_dst_lo, vq_dst_hi, vq_overflow

.segment "BSS"

; リングバッファ。ちょうど 256 バイトなので、添字は 8bit レジスタの巻き取りに任せられる
; （inx が 255→0 に回る）。ページ境界をまたぐ配置でも正しく動く（1サイクル遅くなるだけ）。
vq_buf:      .res VQ_SIZE

vq_dst_lo:   .res 1          ; vram_queue_open の引数: 転送先 VRAM アドレス
vq_dst_hi:   .res 1
vq_arg_len:  .res 1          ; 同: データ長（open が控える）
vq_arg_flags: .res 1         ; 同: フラグ
vq_overflow: .res 1          ; 空きが無くて積めなかった回数（デバッグと予算の当たりを見る用）

.segment "CODE"

; キューを空にする。シーン切り替えなど、積んだものを捨ててよい場面でだけ呼ぶこと。
.proc vram_queue_reset
        lda #0
        sta vq_head
        sta vq_tail
        sta vq_wr
        sta vq_overflow
        rts
.endproc

; 未処理のバイト数を返す（A）。0 なら NMI に渡す仕事は無い。
.proc vram_queue_pending
        lda vq_tail
        sec
        sbc vq_head
        rts
.endproc

; 記録を1つ開く。
;   入力: vq_dst_lo/hi = 転送先 VRAM アドレス、X = データ長、A = フラグ
;   出力: C=0 成功（続けて vram_queue_byte を X 回、最後に vram_queue_close）
;         C=1 空き不足（何も積んでいない。呼び出し側は次フレームに回すこと）
;   壊す: A, X
.proc vram_queue_open
        stx vq_arg_len
        sta vq_arg_flags

        ; 積んだ後の使用量が 255 を超えないこと。
        ; 256 ちょうどにすると tail == head になり「空」と区別がつかなくなる。
        lda vq_tail
        sec
        sbc vq_head                     ; A = 現在の使用量（8bit の巻き取りで正しい）
        clc
        adc vq_arg_len
        bcs @full
        adc #VQ_HDR                     ; ここに来る時点でキャリーは 0
        bcs @full

        ldx vq_tail                     ; 組み立ては tail からの続きに書く
        lda vq_arg_len
        sta vq_buf, x
        inx
        lda vq_dst_hi
        sta vq_buf, x
        inx
        lda vq_dst_lo
        sta vq_buf, x
        inx
        lda vq_arg_flags
        sta vq_buf, x
        inx
        stx vq_wr
        clc
        rts
@full:
        inc vq_overflow
        sec
        rts
.endproc

; 開いている記録にデータを1バイト足す。A = データ。壊す: X
.proc vram_queue_byte
        ldx vq_wr
        sta vq_buf, x
        inx
        stx vq_wr
        rts
.endproc

; 記録を確定する。この1バイトの書き込みで初めて NMI から見えるようになる。
.proc vram_queue_close
        lda vq_wr
        sta vq_tail
        rts
.endproc

; ---------------------------------------------------------------- NMI 側
; 予算のぶんだけ記録を転送する。**NMI からのみ呼ぶこと。**
; 予算を使い切ったら、残りは次フレームに持ち越す（vq_head を進めない）。
;
; 費用の重みは実測に合わせてある:
;   RUN  形式は1バイトあたり約16サイクル → 費用 1
;   STEP 形式は1バイトあたり約43サイクル → 費用 3
; 記録の頭の解釈にも100サイクル前後かかるので、記録数にも別途上限を置く。
.proc vram_queue_flush
        lda #VQ_COST_BUDGET
        sta vq_cost
        lda #VQ_RECORDS_PER_FRAME
        sta vq_recs

; 抜け口が遠いので、終了は分岐先ではなく rts で直に返す。
@record:
        lda vq_recs
        bne @have_budget
        rts                             ; 今フレームの記録数を使い切った
@have_budget:
        lda vq_head
        cmp vq_tail
        bne @take
        rts                             ; 空
@take:

        ldx vq_head
        lda vq_buf, x                   ; VQ_LEN
        sta vq_n
        inx
        lda vq_buf, x                   ; VQ_ADDR_HI
        sta vq_addr_hi
        inx
        lda vq_buf, x                   ; VQ_ADDR_LO
        sta vq_addr_lo
        inx
        lda vq_buf, x                   ; VQ_FLAGS
        sta vq_flags
        inx                             ; X = データ先頭

        ; --- 費用を予算から引く。足りなければ丸ごと次フレームへ回す ---
        ; 記録を途中で切らないのは、切ると転送先アドレスの続きを覚える必要があるからで、
        ; それは「1記録 <= 1フレームの予算」を守れば要らない（constants.inc の .assert）。
        lda vq_n
        bit vq_flags
        bpl @cost_ready
        asl a                           ; STEP は 3 倍
        clc
        adc vq_n
@cost_ready:
        eor #$FF
        sec
        adc vq_cost                     ; A = vq_cost - 費用
        bcs @afford
        rts                             ; 借りが出た = 予算不足。丸ごと次フレームへ
@afford:
        sta vq_cost
        dec vq_recs

        lda vq_flags
        bmi @step_mode

        ; --- RUN 形式: PPU の自動加算に任せて連続で書く ---
        ; VQ_INC32 のビット位置は PPUCTRL の加算ビットと同じなので、そのまま載せられる。
        lda ppu_ctrl_shadow
        ora vq_flags
        sta PPUCTRL
        lda vq_addr_hi
        sta PPUADDR
        lda vq_addr_lo
        sta PPUADDR
        ldy vq_n
@run:
        lda vq_buf, x
        sta PPUDATA
        inx
        dey
        bne @run
        jmp @next

        ; --- STEP 形式: 1バイトごとにアドレスを置き直す ---
        ; 属性テーブルの縦1列は 8 バイトおきで、PPU の自動加算（1 か 32）では届かない。
        ; 高くつくので、費用の重みを 3 にして予算で抑えてある。
        ; 前提: 増分を足してもアドレス下位が桁上がりしないこと（属性1列は $23C0..$23F8 に収まる）。
@step_mode:
        and #VQ_STEP_MASK
        sta vq_step
        lda ppu_ctrl_shadow             ; 加算は 1（アドレスは自分で置き直す）
        sta PPUCTRL
        ldy vq_n
@step:
        lda vq_addr_hi
        sta PPUADDR
        lda vq_addr_lo
        sta PPUADDR
        lda vq_buf, x
        sta PPUDATA
        inx
        lda vq_addr_lo
        clc
        adc vq_step
        sta vq_addr_lo
        dey
        bne @step

@next:
        stx vq_head                     ; ここで初めて「処理済み」にする
        jmp @record
.endproc
