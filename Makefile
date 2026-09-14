# 潮鳴り三代記 — ビルド
#
#   make          ROM をビルドする
#   make test     回帰テスト（構造検証 + 6502 実行検証 + Mesen があれば自動プレイ）
#   make budget   PRG/CHR 予算を計測し docs/budget.md を更新する（超過で非0終了）
#   make clean
#
# 必要ツール: cc65 (ca65/ld65), Python 3.8+
# 任意: Mesen2 （環境変数 MESEN に実行ファイルを指定すると自動プレイ試験も走る）

AS      := ca65
LD      := ld65
PYTHON  := python3

BUILD   := build
OBJDIR  := $(BUILD)/obj
CHRDIR  := $(BUILD)/chr
ROM     := $(BUILD)/roaring.nes
MAP     := $(BUILD)/roaring.map
LABELS  := $(BUILD)/roaring.labels
CFG     := cfg/mmc3.cfg

ASFLAGS := -g -I src -I src/action -I src/ai --bin-include-dir $(CHRDIR)
LDFLAGS := -C $(CFG) -m $(MAP) -Ln $(LABELS)

SRCS    := $(shell find src -name '*.s' | sort)
# src/ 以下の .inc は全て依存に入れる。src/action/*.inc（調整値）が漏れると、
# 主が数値を書き換えても再ビルドされず、「触るだけで手触りが変わる」が成立しない。
INCS    := $(shell find src -name '*.inc' | sort)
OBJS    := $(patsubst src/%.s,$(OBJDIR)/%.o,$(SRCS))

CHRPNG  := $(wildcard chr/*.png)
CHRBIN  := $(patsubst chr/%.png,$(CHRDIR)/%.chr,$(CHRPNG))

.PHONY: all test budget clean rom

all: rom
rom: $(ROM)

$(ROM): $(OBJS) $(CFG)
	@mkdir -p $(BUILD)
	$(LD) $(LDFLAGS) -o $@ $(OBJS)
	@$(PYTHON) tools/romstat.py $@

# CHR は PNG が正。PNG が変われば ROM も作り直す。
$(CHRDIR)/%.chr: chr/%.png tools/png2chr.py tools/pnglib.py
	@mkdir -p $(CHRDIR)
	$(PYTHON) tools/png2chr.py $< $@ --pages 1

$(OBJDIR)/%.o: src/%.s $(INCS) $(CHRBIN)
	@mkdir -p $(dir $@)
	$(AS) $(ASFLAGS) -o $@ $<

test: $(ROM)
	$(PYTHON) test/run_tests.py $(ROM) --labels $(LABELS)

budget: $(ROM)
	$(PYTHON) tools/budget.py $(MAP) --rom $(ROM) --out docs/budget.md

clean:
	rm -rf $(BUILD)
