; sprite.s — エンティティテーブルから OAM シャドウを組み立てる。
;
; 呼ぶのは**メインループ（描画期間中）**である。NMI 中に呼んではならない。
; NMI がやってよいのは完成済みバッファの OAM DMA だけ（CLAUDE.md 第4節 / ADR-0002「実行コスト」）。
;
; 構成は2段:
;   oam_sort_order  … 出す順序を決める（並べ替えの入口。ここ1箇所だけを差し替えれば方式が変わる）
;   oam_emit        … 決まった順序で 8x16 スプライトに展開し、余りを画面外へ退避する
;
; P1 の並べ替えは ADR-0002 の段取りどおり「単純な優先度順」である。
; P2 で重み付き巡回（クラス1・2を複数回まわす）を実験するとき、書き換えるのは
; oam_sort_order だけで済むようにしてある。oam_emit は順序表を受け取るだけで中身を知らない。
;
; OAM の並びは3段になっている。前に置いたものほど「前面に描かれ」、
; かつ「1スキャンライン8スプライト制約で生き残る」:
;
;   1. エンティティ本体   … oam_sort_order が決めた順（クラス → 奥行き）
;   2. 短命の効果         … src/engine/fx.s の枠（斬り・衝撃など）
;   3. 影                 … 有効なエンティティの足元
;
; 影を最後に置いたのは ADR-0002 の「どれを捨てるか」の帰結である。影は奥行き（レーン）を
; 画面に出す手がかりであって、消えても遊びは壊れない。逆に**影がキャラを押し出しては
; ならない**ので、キャラより後ろに置く以外の選択肢が無い。効果（攻撃の届く範囲）は
; 影より重い——当たり判定があるのに見えないのが最も理不尽——ので影より前に置く。
; この並びは P2 の重み付き巡回を入れても変わらない。巡回が並べ替えるのは 1. の中身であり、
; 2. と 3. は「余ったぶんだけ出す」という位置づけのままでよい。
;
; 向き（水平反転）の出どころは act_face ただ1つである。ent_attr の bit6 は emit で必ず
; 落としてから act_face で作り直す（constants.inc の ENT_ATTR_OAM_MASK）。
; 「どちらを向いているか」が2箇所から決まると、どちらが勝つかは書き順しだいになる。
;
; 並べ替えキー = 上位3bit:優先度クラス / 下位5bit:奥行き（足元Yの粗い階調、手前ほど小さい）。
; 値が小さいほど OAM の先頭に置かれる。OAM の先頭ほど「前面に描かれ」かつ
; 「1スキャンライン8スプライト制約で生き残る」ので、この1本のキーが前後関係と
; 欠けにくさの両方を決める。クラスを上位に置いたのは ADR-0002 がクラス0（操作キャラ）を
; 「毎フレーム必ず表示」と定めているためで、奥行きはクラス内での順序として効く。
.include "constants.inc"
.include "zeropage.inc"

.export oam_build, oam_dropped, oam_fx_dropped, oam_used, oam_order

.import oam_shadow
.import ent_active, ent_x_lo, ent_x_hi, ent_y
.import ent_class, ent_body, ent_tile, ent_attr
.import fx_life, fx_x_lo, fx_x_hi, fx_y, fx_tile, fx_attr
; 向きは action-dev が持つ（0 = 右 / 1 = 左）。engine は**読むだけ**である。
; ここを engine 側に持ち直すと、向きの決定が2箇所に分かれる。
.import act_face

.assert SPR_CLASS_COUNT <= 8, error, "優先度クラスが8種を超えると並べ替えキーの上位3bitに入らない"

.segment "BSS"

; 挿入ソートの番兵。下の @shift が `oam_sortkey - 1, y` を読むので、y=0 のときに
; ここが読まれる。番兵には必ず「どのキーよりも小さいか等しい値」($00) を入れるので、
; y=0 では必ず「ここに入る」と判定されてループが止まる。
; 以前は直前の cpy #0 / beq が y=0 への到達を防いでいたが、その1本に頼る形だと、
; 番人が外れた瞬間に**隣の配列を並べ替えキーとして読む**という気付きにくい壊れ方をする。
; 番兵なら、配列の1バイト手前を読むこと自体が正しい動作になる。
oam_sortkey_guard: .res 1
oam_sortkey: .res MAX_ENTITIES    ; キー（挿入位置を探すのに使う）
.assert oam_sortkey - 1 = oam_sortkey_guard, error, "番兵が oam_sortkey の直前に無い"

; oam_order も同じ添字で1バイト手前を読むので、同じ形にしておく
;（キーの比較で必ず先に抜けるため実際には読まれないが、並びを揃えておく）。
oam_order_guard: .res 1
oam_order:   .res MAX_ENTITIES    ; 出す順に並べたエンティティ番号
.assert oam_order - 1 = oam_order_guard, error, "番兵が oam_order の直前に無い"

oam_dropped: .res 1               ; 64 に入り切らず捨てた**エンティティの**列数（P2 の実験とデバッグ用）
; 影と効果は「余りで出す」ものなので、捨てた数をエンティティと混ぜない。
; ここが増えているのは仕様どおりの動作であって、不具合ではない。
oam_fx_dropped: .res 1            ; 64 に入り切らず出せなかった影・効果の個数
oam_used:    .res 1               ; 実際に使った OAM エントリ数（本体 + 効果 + 影）

.segment "CODE"

; 毎フレーム1回、メインループから呼ぶ。
; 構築中は oam_ready を下ろす。NMI はこれを見て、組み立て途中の OAM シャドウを
; DMA しないようにする（1フレームの処理が溢れたときに、上半身だけ新しい絵が出るのを防ぐ）。
.proc oam_build
        lda #0
        sta oam_ready
        jsr oam_sort_order
        jsr oam_emit
        lda #1
        sta oam_ready
        rts
.endproc

; ---------------------------------------------------------------- 並べ替え
; 有効なエンティティを走査し、キーの昇順に oam_order へ挿入していく。
; 挿入ソートにしたのは、要素が MAX_ENTITIES（16）と少なく、しかも前フレームと
; ほぼ同じ並びになる（＝ほぼ整列済み）ためである。
; 同じキーならエンティティ番号の小さい方が先（安定）。順序が毎フレーム暴れると
; 重なりがちらついて見えるので、安定であることには意味がある。
.proc oam_sort_order
        lda #0
        sta sort_count
        sta oam_sortkey_guard            ; 番兵を張り直す（不変条件に頼らない）
        ldx #0
@scan:
        lda ent_active, x
        beq @next

        ; --- キーを作る ---
        lda ent_y, x
        lsr a
        lsr a
        lsr a                            ; 足元Y を 8ライン刻みに（SORT_KEY_DEPTH_SHIFT）
        sta tmp0
        lda #SORT_KEY_DEPTH_MAX
        sec
        sbc tmp0                         ; 手前（Yが大きい）ほど小さい値にする
        sta tmp0
        lda ent_class, x
        asl a
        asl a
        asl a
        asl a
        asl a                            ; クラスを上位3bitへ（SORT_KEY_CLASS_SHIFT）
        ora tmp0
        sta tmp1                         ; tmp1 = キー

        ; --- 挿入位置まで後ろへずらす ---
        ; y=0 では番兵 ($00) を読む。新キーは 0 以上なので必ず bcc/beq のどちらかが
        ; 成立して @insert へ抜ける。よってループの停止は「番兵の値」だけで保証され、
        ; 手前に置いた比較や添字の下限チェックに依存しない。
        ldy sort_count
@shift:
        lda oam_sortkey - 1, y
        cmp tmp1
        bcc @insert                      ; 既存キー < 新キー → ここに入る
        beq @insert                      ; 同キーは後ろに置く（＝安定）
        sta oam_sortkey, y
        lda oam_order - 1, y
        sta oam_order, y
        dey
        jmp @shift
@insert:
        lda tmp1
        sta oam_sortkey, y
        txa
        sta oam_order, y
        inc sort_count
@next:
        inx
        cpx #MAX_ENTITIES
        bne @scan
        rts
.endproc

; ---------------------------------------------------------------- 展開
; oam_order の順に OAM シャドウへ詰める。
.proc oam_emit
        lda #0
        sta oam_next
        sta oam_dropped
        sta oam_fx_dropped

        ldy #0
@loop:
        cpy sort_count
        bcs @fill
        tya
        pha
        lda oam_order, y
        tax
        jsr emit_entity
        pla
        tay
        iny
        jmp @loop

@fill:
        ; --- 余ったぶんで効果と影を出す（順序の理由はファイル冒頭）---
        jsr emit_fx
        jsr emit_shadows

        ; 使わなかったエントリは Y = $FF で画面外へ退避する。
        ; oam_used はここで**1回だけ**書く。組み立ての途中経過は oam_next が持つ。
        lda oam_next
        sta oam_used
        cmp #OAM_SPRITE_MAX
        bcs @done
        asl a
        asl a
        tay                              ; Y = 先頭の空きエントリのバイト位置
        lda #OAM_Y_OFFSCREEN
@hide:
        sta oam_shadow + OAM_Y, y
        iny
        iny
        iny
        iny
        bne @hide                        ; 256 バイトで一周して終わる
@done:
        rts
.endproc

; X = エンティティ番号。体格型のぶんだけ 8x16 スプライトを横に並べる。
.proc emit_entity
        ; 画面X = ワールドX - カメラX（16bit）
        lda ent_x_lo, x
        sec
        sbc cam_x_lo
        sta spr_x_lo
        lda ent_x_hi, x
        sbc cam_x_hi
        sta spr_x_hi

        ; OAM の Y バイト。足元Y から体の高さと1ラインぶんを引く。
        lda ent_y, x
        sec
        sbc #ENT_Y_TO_OAM
        bcc @done                        ; 画面上端より上 → 出さない
        sta spr_y

        ; --- 属性と向き ---
        ; ent_attr の水平反転ビットは落とし、向きは act_face から作り直す
        ; （向きの出どころを1つにする。ファイル冒頭の注記）。
        lda ent_attr, x
        and #ENT_ATTR_OAM_MASK
        ldy act_face, x
        beq @face_right
        ora #ENT_ATTR_HFLIP
@face_right:
        sta spr_attr

        ldy ent_body, x
        lda body_width, y
        sta spr_cols

        ; --- タイルの並び ---
        ; 体が2タイル幅以上のとき、水平反転は1枚ごとの絵を裏返すだけで、
        ; **列の並び順は裏返してくれない**。左向きでは左端の列に体の右端の絵が要る。
        ; そこで左向きのときだけ末尾のタイルから始め、列を進めるごとに -2 する。
        ; 反転ビットは spr_attr の bit6 にあるので bit / bvc で見る。
        lda #SPR_POSE_STRIDE
        sta spr_tstep
        lda ent_tile, x
        bit spr_attr
        bvc @tile_ready                  ; 右向き: 先頭タイルから +2 ずつ
        lda spr_cols
        asl a
        sec
        sbc #SPR_POSE_STRIDE             ; (列数 - 1) * 2
        clc
        adc ent_tile, x                  ; 末尾の列のタイル
        ldy #<-SPR_POSE_STRIDE
        sty spr_tstep
@tile_ready:
        sta spr_tile

@col:
        lda spr_x_hi
        bne @advance                     ; 画面の左右にはみ出した列は出さない

        lda oam_next
        cmp #OAM_SPRITE_MAX
        bcc @have_slot
        inc oam_dropped                  ; 64 を超えた。捨てた数を数えておく
        jmp @advance
@have_slot:
        asl a
        asl a
        tay                              ; Y = OAM のバイト位置
        lda spr_y
        sta oam_shadow + OAM_Y, y
        lda spr_tile
        sta oam_shadow + OAM_TILE, y
        lda spr_attr
        sta oam_shadow + OAM_ATTR, y
        lda spr_x_lo
        sta oam_shadow + OAM_X, y
        inc oam_next

@advance:
        lda spr_x_lo
        clc
        adc #SPRITE_W
        sta spr_x_lo
        bcc @next_tile
        inc spr_x_hi
@next_tile:
        lda spr_tile
        clc
        adc spr_tstep                    ; 8x16 モードでは1スプライトが上下2タイルを使う
        sta spr_tile                     ; ent_tile は偶数で置くこと（bit0 はパターンテーブル選択）
        dec spr_cols
        bne @col
@done:
        rts
.endproc

; ---------------------------------------------------------------- 影
; 有効なエンティティのうち ENT_ATTR_SHADOW が立っているものの足元に、
; 影のタイルを1枚ずつ敷く。
;
; **これが4レーンを「規則」ではなく「空間」に見せる唯一の手がかりである。**
; 影が無いと、16ドット高のキャラが 12〜20 ドット間隔で並ぶだけの絵になり、
; 縦移動の制限が理不尽に見える（P1 受入での主の指摘）。
;
; 走査は oam_order の順に行う。並べ替え済みの順で回すので、OAM が詰まったときに
; 消えるのは「奥にいる・優先度の低いキャラの影」からになる。
; 影は接地線（ent_y）に合わせる。CHR 側の影は 8x16 タイルの下端3ラインに描いてあるので、
; キャラ本体と同じ OAM の Y に置けばちょうど足元に来る。
.proc emit_shadows
        ldy #0
@loop:
        cpy sort_count
        bcs @done
        tya
        pha
        lda oam_order, y
        tax
        lda ent_attr, x
        and #ENT_ATTR_SHADOW
        beq @skip
        jsr emit_shadow_one
@skip:
        pla
        tay
        iny
        jmp @loop
@done:
        rts
.endproc

; X = エンティティ番号。足元に影を1枚置く。
.proc emit_shadow_one
        lda ent_x_lo, x
        sec
        sbc cam_x_lo
        sta spr_x_lo
        lda ent_x_hi, x
        sbc cam_x_hi
        sta spr_x_hi

        ; 体の横幅の中央に寄せる（影は1タイル幅。体は body_width タイル幅）。
        ; ずらす量は (幅 - 1) * SPRITE_W / 2。body_width から毎回計算するので、
        ; 体格を増やしても直す表は body_width の1つだけで済む。
        ldy ent_body, x
        lda body_width, y
        sec
        sbc #1
        asl a
        asl a
        clc
        adc spr_x_lo
        sta spr_x_lo
        bcc @no_carry
        inc spr_x_hi
@no_carry:
        lda spr_x_hi
        bne @done                        ; 画面の左右にはみ出した → 出さない

        lda ent_y, x
        sec
        sbc #ENT_Y_TO_OAM
        bcc @done
        sta spr_y
        lda #SPR_TILE_SHADOW
        sta spr_tile
        lda #SPR_PAL_EFFECT
        sta spr_attr
        jmp put_extra_sprite
@done:
        rts
.endproc

; ---------------------------------------------------------------- 短命の効果
; 枠と寿命は src/engine/fx.s が持つ。ここは出すだけである。
.proc emit_fx
        ldx #FX_MAX - 1
@loop:
        lda fx_life, x
        beq @next
        jsr emit_fx_one
@next:
        dex
        bpl @loop
        rts
.endproc

; X = 効果の枠番号。
.proc emit_fx_one
        lda fx_x_lo, x
        sec
        sbc cam_x_lo
        sta spr_x_lo
        lda fx_x_hi, x
        sbc cam_x_hi
        bne @done                        ; 画面の左右にはみ出した → 出さない

        lda fx_y, x
        sec
        sbc #ENT_Y_TO_OAM
        bcc @done
        sta spr_y
        lda fx_tile, x
        sta spr_tile
        lda fx_attr, x
        sta spr_attr
        jmp put_extra_sprite
@done:
        rts
.endproc

; spr_y / spr_tile / spr_attr / spr_x_lo を次の OAM エントリへ書く。
; 空きが無ければ何もせず oam_fx_dropped を増やす（影と効果は消えてよい）。
; X は壊さない。
.proc put_extra_sprite
        lda oam_next
        cmp #OAM_SPRITE_MAX
        bcc @have_slot
        inc oam_fx_dropped
        rts
@have_slot:
        asl a
        asl a
        tay
        lda spr_y
        sta oam_shadow + OAM_Y, y
        lda spr_tile
        sta oam_shadow + OAM_TILE, y
        lda spr_attr
        sta oam_shadow + OAM_ATTR, y
        lda spr_x_lo
        sta oam_shadow + OAM_X, y
        inc oam_next
        rts
.endproc

.segment "RODATA"

; 体格型 → 横タイル数（＝ 8x16 スプライトの消費数）。
; 出典は ADR-0002 の見積表。コードに埋めず表で持つので、体格を増やすのはこの表の追加で済む。
body_width:
        .byte 1          ; BODY_PLACEHOLDER 仮CHR（8x16 の図形が1つしかない）
        .byte 2          ; BODY_LIGHT       軽装・小柄
        .byte 3          ; BODY_HEAVY       重装
        .byte 3          ; BODY_BEAST       獣
        .byte 4          ; BODY_BEAST_LARGE 獣（大）
        .byte 4          ; BODY_MACHINE     機械型（大型敵）

.assert * - body_width = BODY_TYPE_COUNT, error, "body_width の項数が体格型の数と違う"

