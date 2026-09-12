; vectors.s — 6502 割り込みベクタ。
.import nmi_handler, reset_handler, irq_handler

.segment "VECTORS"
        .addr nmi_handler
        .addr reset_handler
        .addr irq_handler
