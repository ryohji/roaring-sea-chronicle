; header.s — iNES ヘッダ。MMC3 (Mapper 4) / PRG 256KB / CHR 128KB。
; セーブ方式は ADR-0001（案A: バッテリーバックアップ）で決定済み。
; $6000-$7FFF の PRG-RAM 8KB を電池で保持し、シナリオ中途の複数ポイントで
; オートセーブする。よって flags6 のバッテリービットを立て、flags8 で 8KB を宣言する。
.segment "HEADER"

.byte $4E, $45, $53, $1A   ; "NES", EOF
.byte 16                   ; PRG-ROM 16KB 単位 = 16 -> 256KB
.byte 16                   ; CHR-ROM  8KB 単位 = 16 -> 128KB

; flags6 = $43 = %0100_0011
;   bit7-4 = %0100 = 4 : マッパー番号の下位ニブル（MMC3）
;   bit3   = 0         : 4画面 VRAM を使わない
;   bit2   = 0         : トレーナ無し（ROM は 16 バイトヘッダ直後から始まる）
;   bit1   = 1         : バッテリーバックアップ有り（$6000-$7FFF を不揮発に）… ADR-0001
;   bit0   = 1         : 初期ミラーリングは垂直。実行時は MMC3 $A000 が制御する
.byte $43                  ; flags6

; flags7 = $00 = %0000_0000
;   bit7-4 = 0 : マッパー番号の上位ニブル（MMC3 は 4 なので上位は 0）
;   bit3-2 = 0 : iNES 1.0（NES 2.0 なら %10）。よって byte 8 は PRG-RAM サイズとして読まれる
;   bit1-0 = 0 : VS/PlayChoice ではない
.byte $00                  ; flags7

; flags8 = $01
;   iNES 1.0 では PRG-RAM のサイズを 8KB 単位で表す。1 -> 8KB。
;   0 も後方互換で 8KB 扱いされるが、電池付き ROM では容量を明示しておく
;   （0 のままだと「PRG-RAM 無し」と解釈するエミュレータがあり、セーブが消える）。
.byte $01                  ; flags8

.byte $00, $00
.byte $00, $00, $00, $00, $00
