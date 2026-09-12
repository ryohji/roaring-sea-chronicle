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
CTRL_INC_32    = %00000100   ; $2007 の書き込みごとにアドレスが +32（列単位の転送に使う）
CTRL_NT_X      = %00000001   ; ベースネームテーブル選択の横ビット（カメラX の bit8 が入る）
; シャドウから NMI 許可ビットだけを落とすマスク。初期化中に PPUCTRL を触るときに使う
; （NMI は割り込み禁止 sei では止まらないので、ビットを立てた瞬間に飛んでくる）。
CTRL_NMI_OFF   = %01111111

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

; ---- 画面とネームテーブル ----
SCREEN_W       = 256         ; 表示幅（ドット）
SCREEN_TILES_W = 32          ; 1画面の横タイル数
SCREEN_TILES_H = 30          ; 1画面の縦タイル数（ネームテーブルの有効行数）
TILE_W         = 8
TILE_W_SHIFT   = 3           ; ドット → タイル列（>> 3）

; 横スクロールは垂直ミラーリング（ネームテーブルが左右に2枚並ぶ）で行う。
; ワールドのタイル列 c は、ネームテーブル (c>>5)&1 の第 (c&31) 列に載る。
; つまり背景は 64 列 = 512 ドットで巻き取られる。
NT_BASE        = $2000
NT_STRIDE      = $0400       ; ネームテーブル1枚のサイズ
NT_ATTR_OFFSET = $03C0       ; ネームテーブル先頭 → 属性テーブル先頭
ATTR_TILES     = 4           ; 属性1バイトが受け持つタイル数（4x4 タイル = 32x32 ドット）
ATTR_COLS      = SCREEN_TILES_W / ATTR_TILES   ; = 8（1画面ぶんの属性列数）
ATTR_ROWS      = 8                             ; 属性テーブルの行数（30行ぶんを8行で覆う）

; ---- カメラ（横スクロール）----
; 画面X = ワールドX - カメラX。カメラは操作キャラを追うが、**硬く張り付かせない**。
; 画面中央に不感帯（デッドゾーン）を置き、操作キャラがその外に出たときだけ動かす。
; 張り付かせると、立ち止まりや小刻みな踏み込みのたびに画面全体が揺れて酔う。
;
; 不感帯は「操作キャラの基準点（体の左端）の画面X」で測る。主が調整するノブはここ。
;   広げる → 画面が落ち着くが、キャラが端に寄った状態が長く続き、進行方向が見えにくい
;   狭める → 常に中央に居るが、細かい移動で画面が揺れる
CAM_CENTER_X       = SCREEN_W / 2      ; = 128
CAM_DEADZONE_HALF  = 32                ; 中央から左右にこのぶんは追わない（幅 64 ドット = 8タイル）
CAM_DEADZONE_LEFT  = CAM_CENTER_X - CAM_DEADZONE_HALF    ; = 96
CAM_DEADZONE_RIGHT = CAM_CENTER_X + CAM_DEADZONE_HALF    ; = 160

.assert CAM_DEADZONE_LEFT > 0, error, "不感帯が画面より広い"
.assert CAM_DEADZONE_RIGHT < SCREEN_W, error, "不感帯が画面より広い"

; ---- VRAM 転送キュー（src/engine/vram.s）----
; NMI が VBlank に使えるのは OAM DMA（約513サイクル）を引いた残り、およそ1700サイクル。
; 1フレームぶんを刻むため、キューは「費用」の予算で区切る。
;   RUN 形式（PPU の自動加算に任せる連続転送）… 1バイトあたり費用 1
;   STEP 形式（1バイトごとにアドレスを置き直す）… 1バイトあたり費用 3（実測比に合わせた重み）
; 予算は「ネームテーブル1列(30) + 属性1列(8×3=24) = 54」がちょうど1フレームに乗る値。
VQ_SIZE              = 256   ; リングバッファのバイト数（256 固定。添字の 8bit 巻き取りに乗せる）
VQ_HDR               = 4     ; 記録のヘッダ長
VQ_MAX_LEN           = 32    ; 1記録のデータ長の上限
VQ_COST_BUDGET       = 54    ; 1フレームに処理してよい費用の合計
VQ_RECORDS_PER_FRAME = 2     ; 1フレームに処理してよい記録数（ヘッダ解釈のぶんも予算に効く）

; 記録のヘッダ（+0 から順に）
VQ_LEN     = 0               ; データ長（1..VQ_MAX_LEN）
VQ_ADDR_HI = 1               ; 転送先 VRAM アドレス 上位
VQ_ADDR_LO = 2               ; 同 下位
VQ_FLAGS   = 3               ; 下記のフラグ

; フラグ。RUN 形式では bit2 がそのまま PPUCTRL の加算ビットになる（ora で載せられる）。
VQ_INC1    = %00000000       ; RUN: アドレス +1（パレット・横一列・テキスト行）
VQ_INC32   = CTRL_INC_32     ; RUN: アドレス +32（ネームテーブルの縦1列）
VQ_STEP    = %10000000       ; STEP: 1バイトごとにアドレスを置き直す。下位7bit が増分
VQ_STEP_MASK = %01111111
; 属性テーブルの縦1列は 8 バイトおきなので PPU の自動加算（1 か 32）では届かない。
VQ_STEP_ATTR = VQ_STEP | ATTR_COLS       ; = STEP 増分 8

.assert VQ_MAX_LEN <= VQ_COST_BUDGET, error, "1記録が1フレームの予算に収まらない（永久に処理されない）"
.assert SCREEN_TILES_H <= VQ_MAX_LEN, error, "ネームテーブル1列が1記録に収まらない"

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

; ---- 擬似奥行きレーンの座標系 ----
; レーン番号 0 が最も奥（画面上方）、LANE_COUNT-1 が最も手前（画面下方）。
; レーン番号 → 画面Y の対応表は src/engine/lane.s の lane_ground_y にある（等間隔ではない）。
LANE_FIRST = 0
LANE_LAST  = LANE_COUNT - 1

; レーン移動にかける補間フレーム数。手触りのパラメータなのでここで調整する。
;
; lane.s の補間は 8bit の累算で刻む。実際の制約は2つある:
;   * レーン間のY差が ±127 に収まること。総移動量 ent_lane_dy が符号つき8bit なので、
;     lane_ground_y のどの2要素の差もこれを超えてはならない。
;   * (LANE_MOVE_FRAMES - 1) + レーン間の最大Y差 <= 255 であること。
;     累算器 ent_lane_acc は「1フレームぶん足してから LANE_MOVE_FRAMES ずつ引く」
;     順で動くので、引く前の一瞬だけこの値まで大きくなる。
; いまの表（148/160/176/196、最大Y差 48）では、どちらの制約にも大きく余裕がある。
LANE_MOVE_FRAMES = 8

; ---- エンティティテーブル ----
; Structure of Arrays（属性ごとに1配列）。実体と各配列のラベルは src/engine/entity.s。
; 構造体オフセットを持たないのは `lda ent_x_lo, x` の1命令で引けるようにするため。
;
; 添字の区画割り。固定区画にしてあるので「プレイヤーは常に添字0」が保証される。
; 探索なしで引けること、および ai / action がスロット範囲で走査できることを優先した。
ENT_PLAYER      = 0                                   ; 操作キャラ（1体）
ENT_ALLY_FIRST  = 1                                   ; 自律仲間
ENT_ALLY_COUNT  = PARTY_SIZE - 1                      ; = 2
ENT_ENEMY_FIRST = ENT_ALLY_FIRST + ENT_ALLY_COUNT     ; = 3
ENT_ENEMY_COUNT = MAX_ENEMIES                         ; = 6（ADR-0002）
ENT_FREE_FIRST  = ENT_ENEMY_FIRST + ENT_ENEMY_COUNT   ; = 9
ENT_FREE_COUNT  = 7                                   ; 弾・エフェクトなどの共用枠
MAX_ENTITIES    = ENT_FREE_FIRST + ENT_FREE_COUNT     ; = 16

; ent_active の値。$80 にしてあるので bmi / bpl でも判定できる。
ENT_INACTIVE = $00
ENT_ACTIVE   = $80

; ent_state の最小値。状態の語彙を増やすのは action-dev の領分（engine は箱だけ持つ）。
ENT_ST_IDLE = 0

; ---- 体格型 ----
; ここにあるのは ID（＝表の添字）だけである。横タイル数の実値は
; src/engine/sprite.s の body_width 表が持つ。ADR-0002 の見積表がその出典。
; 体格型は「データ側の ID を入れる箱」であり、キャラ名や話番号を持たせてはならない。
BODY_PLACEHOLDER = 0   ; 仮CHR用。8x16 の図形が1つしかないので1タイル幅
BODY_LIGHT       = 1   ; 軽装・小柄
BODY_HEAVY       = 2   ; 重装
BODY_BEAST       = 3   ; 獣
BODY_BEAST_LARGE = 4   ; 獣（大）
BODY_MACHINE     = 5   ; 機械型（大型敵）
BODY_TYPE_COUNT  = 6

; ---- スプライト描画 ----
; 8x16 スプライト前提。1体は「8x16 を横に body_width 個」で構成する。
SPRITE_W = 8
SPRITE_H = 16

; エンティティの Y は「足元（接地線）の画面Y」である。奥行きの前後関係は
; この値の大小で決まるので、描画の基準点を足元に置いておくと並べ替えが素直になる。
; OAM の Y バイトは実際の表示より1ライン上にずれる仕様なので、その1も含めて引く。
ENT_Y_TO_OAM = SPRITE_H + 1

; OAM 並べ替えキーの詰め方: 上位3bit = 優先度クラス / 下位5bit = 奥行き（足元Yの粗い階調）。
; 値が小さいほど OAM の先頭に置かれる（＝前面に描かれ、8スプライト制約で生き残る）。
SORT_KEY_DEPTH_SHIFT = 3    ; 足元Y を 8ライン刻みに落として 0..31 にする
SORT_KEY_DEPTH_MAX   = 31
SORT_KEY_CLASS_SHIFT = 5

; ---- 矩形（当たり判定のプリミティブ）----
; X はワールド座標で 16bit、Y と幅・高さは 8bit。実体は zeropage の rect_a / rect_b。
RECT_X_LO = 0
RECT_X_HI = 1
RECT_Y    = 2
RECT_W    = 3
RECT_H    = 4
RECT_SIZE = 5

.endif
