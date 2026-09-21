; debug.inc — 手触り確認のためのデバッグ機構。**仕様ではない。**
;
; 主が実機／エミュで P1 の手触りを見るための仕掛けだけを置く。
; ここにあるものはいずれ 0 にして消す前提であり、遊びの仕様を表さない。
;
; DBG_ENABLE = 0 にすると、**コードも変数も丸ごと消える**こと。
; 呼び出し側（src/main.s）も参照側（src/ai/）も `.if DBG_ENABLE` で囲むこと。
; 前例は src/action/action_params.inc の ACT_DBG_SHOOTER。
;
; 割り当て（押した瞬間 = pad_pressed で判定する。押しっぱなしで回さない）:
;   START  … dbg_slow を 0 → 1 → 2 → 0
;   SELECT … dbg_ally を 0 → 1 → 2 → 0
;
; **NMI は常に毎フレーム走る。** 遅くするのは論理更新だけであって、
; OAM DMA・VRAM 転送・スクロール設定は等速のままである（止めると画面がガタつく）。
.ifndef DEBUG_INC
DEBUG_INC = 1

; 1 = デバッグ機構を載せる / 0 = 丸ごと消す（既定の挙動に1命令も足さない）
DBG_ENABLE = 1

; モードの巡回の段数。0 から DBG_MODE_COUNT-1 まで回って 0 に戻る。
DBG_MODE_COUNT = 3

.if DBG_ENABLE

; --- dbg_slow — 論理更新をどれだけ遅くするか ---
; 論理更新1回につき**飛ばす**フレーム数。
;   0 = 等速（既定）／ 1 = 半速 ／ 2 = 1/3 速
; 飛ばすフレームでは read_pad / fx_update / action_update / ai_update /
; cam_update / oam_build を**すべて**飛ばす。一部だけ回すと、ヒットストップや
; 予備動作の見かけの長さが型ごとにずれて、遅くした意味が無くなる。
; 特に read_pad を毎フレーム呼ぶと、飛ばすフレームで pad_pressed（押した瞬間）が
; 消費され、攻撃の入力が取りこぼされる。
;
; **実体は BSS（ゼロページではない）。** 毎フレーム1回読むだけの値に
; ゼロページを1バイト割く価値は無い（src/zeropage.inc は一元宣言の場であり、
; そこに置けば「ゼロページが足りない」日を早めるだけである）。
.global dbg_slow

; --- dbg_ally — 仲間の確認モード ---
;   0 = 通常 ／ 1 = 攻撃しない ／ 2 = 休ませる（完全静止）
; **engine は値を回すだけで、意味を解釈しない。** 読んで振る舞いを変えるのは
; src/ai/ の担当である（CLAUDE.md 第3節の担当境界）。
.global dbg_ally

.endif   ; DBG_ENABLE

.endif
