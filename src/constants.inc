; constants.inc — プロジェクト全体の定数。マジックナンバーはここに集約する。
.ifndef CONSTANTS_INC
CONSTANTS_INC = 1

; ---- PPU ----
PPUCTRL   = $2000
PPUMASK   = $2001
PPUSTATUS = $2002
OAMADDR   = $2003
OAMDATA   = $2004
PPUSCROLL = $2005
PPUADDR   = $2006
PPUDATA   = $2007
OAMDMA    = $4014

; PPUCTRL ビット
CTRL_NMI_ON    = %10000000
CTRL_SPR_8X16  = %00100000
CTRL_BG_1000   = %00010000
CTRL_SPR_0000  = %00000000
CTRL_INC_1     = %00000000

; PPUMASK ビット
MASK_SHOW_BG   = %00001000
MASK_SHOW_SPR  = %00010000
MASK_BG_LEFT   = %00000010
MASK_SPR_LEFT  = %00000100

; ---- APU / 入力 ----
APU_DMC_FREQ   = $4010
APU_STATUS     = $4015
APU_FRAME      = $4017
JOYPAD1        = $4016
JOYPAD2        = $4017

; パッドビット（$4016 の読み出し順）
PAD_A      = %10000000
PAD_B      = %01000000
PAD_SELECT = %00100000
PAD_START  = %00010000
PAD_UP     = %00001000
PAD_DOWN   = %00000100
PAD_LEFT   = %00000010
PAD_RIGHT  = %00000001

; ---- MMC3 (Mapper 4) ----
MMC3_BANK_SELECT = $8000   ; 偶数アドレス
MMC3_BANK_DATA   = $8001   ; 奇数アドレス
MMC3_MIRRORING   = $A000
MMC3_PRG_RAM     = $A001
MMC3_IRQ_LATCH   = $C000
MMC3_IRQ_RELOAD  = $C001
MMC3_IRQ_DISABLE = $E000
MMC3_IRQ_ENABLE  = $E001

MMC3_PRG_MODE_0  = %00000000   ; $8000 可変 / $C000 固定
MMC3_CHR_MODE_0  = %00000000   ; $0000 に 2KB×2 / $1000 に 1KB×4

; $A001 (MMC3_PRG_RAM) のビット。ADR-0001（案A: バッテリーバックアップ）により
; $6000-$7FFF の PRG-RAM をセーブ領域として常用する。
;   bit7 = 1 で PRG-RAM 有効（0 なら $6000-$7FFF は開放バス）
;   bit6 = 1 で書き込み禁止（読み出しのみ）。書き込むときは 0 でなければならない
MMC3_PRGRAM_ENABLE    = %10000000   ; PRG-RAM を $6000-$7FFF に見せる
MMC3_PRGRAM_DENY_WR   = %01000000   ; 書き込み禁止（セーブしない区間の保護に使う）
MMC3_PRGRAM_RW        = MMC3_PRGRAM_ENABLE                          ; 有効かつ書き込み可
MMC3_PRGRAM_RO        = MMC3_PRGRAM_ENABLE | MMC3_PRGRAM_DENY_WR    ; 有効だが書き込み禁止

MIRROR_VERTICAL   = 0
MIRROR_HORIZONTAL = 1

; ---- OAM ----
; OAM シャドウの実体は src/oam.s（セグメント OAM）。アドレスはリンカが決める。
OAM_SPRITE_MAX  = 64
OAM_Y_OFFSCREEN = $FF

; PPU のハード制約: 1スキャンラインに描けるスプライトは8個まで。
; 9個目以降は描画されない（＝キャラが消える）。この値は PPU の仕様であり、
; 設計上の都合で変えられるものではない。
SPRITES_PER_SCANLINE = 8

; OAM 1エントリのオフセット
OAM_Y    = 0
OAM_TILE = 1
OAM_ATTR = 2
OAM_X    = 3

; ---- ゲーム基本定数 ----
; レーン数は4に固定。ADR なしに変更してはならない（CLAUDE.md 第5条）。
LANE_COUNT = 4

; 1画面の敵同時数。ADR-0002（案B: 敵同時6体・フリッカー許容）で決定済み。
MAX_ENEMIES = 6

; パーティ（操作1＋自律2）
PARTY_SIZE = 3

; ---- スプライト優先度クラス ----
; ADR-0002（案B）は「敵6体＋パーティ3体」を 1スキャンライン8スプライト制約の下で
; 出すために、フリッカー（OAM 巡回）を許容する。ただし単純な総当たり巡回はしない。
; 巡回順を決める重みとして、全エンティティに以下のクラスを1つ割り当てる。
; 値が小さいほど優先。低いクラスほど巡回1周あたりの出現回数を多く配る
;   （＝クラス1,2 は複数回まわしてから、クラス3,4 を回す。実験は P2）。
; engine / action / ai がこの語彙を共有すること。
SPR_CLASS_PLAYER   = 0   ; 操作キャラ。巡回対象外で毎フレーム必ず描く
SPR_CLASS_PROJECT  = 1   ; 弾・飛び道具。当たり判定があるのに見えないのが最も理不尽
SPR_CLASS_NEAR_ENE = 2   ; 操作キャラ至近の敵。読み合いの対象なので消してはいけない
SPR_CLASS_FAR_ENE  = 3   ; それ以外の敵、および自律仲間
SPR_CLASS_EFFECT   = 4   ; エフェクト・UI。消えても遊びが壊れない
SPR_CLASS_COUNT    = 5

.endif
