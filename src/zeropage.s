; zeropage.s — ゼロページ変数の実体。宣言は zeropage.inc を見よ。
.include "constants.inc"
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

ppu_ctrl_shadow: .res 1
ppu_mask_shadow: .res 1

cam_x_lo:      .res 1
cam_x_hi:      .res 1
cam_col_lo:    .res 1
cam_col_hi:    .res 1

vq_head:       .res 1
vq_tail:       .res 1
vq_wr:         .res 1
vq_n:          .res 1
vq_cost:       .res 1
vq_recs:       .res 1
vq_addr_lo:    .res 1
vq_addr_hi:    .res 1
vq_flags:      .res 1
vq_step:       .res 1

oam_ready:     .res 1
oam_next:      .res 1
sort_count:    .res 1
spr_x_lo:      .res 1
spr_x_hi:      .res 1
spr_y:         .res 1
spr_tile:      .res 1
spr_attr:      .res 1
spr_cols:      .res 1

rect_a:        .res RECT_SIZE
rect_b:        .res RECT_SIZE
