; header.s — iNES ヘッダ。MMC3 (Mapper 4) / PRG 256KB / CHR 128KB。
; バッテリーバックアップの有無は ADR-0001 の決定待ちのため、現状は 0（電池なし）。
.segment "HEADER"

.byte $4E, $45, $53, $1A   ; "NES", EOF
.byte 16                   ; PRG-ROM 16KB 単位 = 16 -> 256KB
.byte 16                   ; CHR-ROM  8KB 単位 = 16 -> 128KB
.byte $41                  ; flags6: mapper 下位ニブル=4 + bit0=1(初期は垂直)。実行時のミラーリングは MMC3 $A000 が制御する
.byte $00                  ; flags7: mapper 上位ニブル=0
.byte $00                  ; flags8: PRG-RAM サイズ（8KB 単位、0 は 8KB 扱い）
.byte $00, $00
.byte $00, $00, $00, $00, $00
