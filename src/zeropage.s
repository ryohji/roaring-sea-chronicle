; zeropage.s — ゼロページ変数の実体。宣言は zeropage.inc を見よ。
.include "zeropage.inc"

.segment "ZEROPAGE"

frame_counter: .res 1
nmi_done:      .res 1
pad_state:     .res 1
pad_pressed:   .res 1
ptr_lo:        .res 1
ptr_hi:        .res 1
tmp0:          .res 1
tmp1:          .res 1
tmp2:          .res 1
tmp3:          .res 1
