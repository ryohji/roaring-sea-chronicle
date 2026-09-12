"""NES バスと MMC3 マッパーの最小実装（回帰テスト用）。

PPU はレジスタの振る舞いと VRAM/OAM への副作用だけを再現する。
描画は行わない。スキャンライン単位のタイミング（スプライト欠け、MMC3 IRQ）は
再現しないので、その検証は Mesen2 側で行うこと（ADR-0004）。
"""
from cpu6502 import Cpu, CpuCrash

CYCLES_PER_FRAME = 29780        # NTSC の1フレームの CPU サイクル数（近似）
VBLANK_CYCLES = 2273            # VBlank 期間の CPU サイクル数（近似）

BUTTONS = ("A", "B", "SELECT", "START", "UP", "DOWN", "LEFT", "RIGHT")


class Rom:
    def __init__(self, path):
        with open(path, "rb") as f:
            data = f.read()
        if data[:4] != b"NES\x1a":
            raise ValueError("iNES ヘッダが不正: %s" % path)
        self.header = data[:16]
        self.prg_banks_16k = data[4]
        self.chr_banks_8k = data[5]
        self.mapper = (data[6] >> 4) | (data[7] & 0xF0)
        self.has_trainer = bool(data[6] & 0x04)
        self.has_battery = bool(data[6] & 0x02)
        off = 16 + (512 if self.has_trainer else 0)
        prg_size = self.prg_banks_16k * 16384
        chr_size = self.chr_banks_8k * 8192
        self.prg = data[off:off + prg_size]
        self.chr = data[off + prg_size:off + prg_size + chr_size]
        self.size = len(data)
        if len(self.prg) != prg_size:
            raise ValueError("PRG-ROM が短い: 期待 %d バイト、実際 %d バイト" % (prg_size, len(self.prg)))
        if len(self.chr) != chr_size:
            raise ValueError("CHR-ROM が短い: 期待 %d バイト、実際 %d バイト" % (chr_size, len(self.chr)))


class Ppu:
    """PPU のレジスタ側だけを持つスタブ。"""

    def __init__(self):
        self.ctrl = 0
        self.mask = 0
        self.status = 0
        self.oam_addr = 0
        self.oam = bytearray(256)
        self.vram = bytearray(0x4000)
        self.addr = 0
        self.latch = False
        self.scroll = [0, 0]
        self.in_vblank = False
        self.writes_outside_vblank = 0      # 描画中の $2007 書き込み（バグの温床）
        self.rendering_enabled = False

    @property
    def nmi_enabled(self):
        return bool(self.ctrl & 0x80)

    def read(self, reg):
        if reg == 2:                       # PPUSTATUS
            value = self.status | (0x80 if self.in_vblank else 0)
            self.in_vblank = False
            self.status &= 0x7F
            self.latch = False
            return value
        if reg == 4:                       # OAMDATA
            return self.oam[self.oam_addr]
        if reg == 7:                       # PPUDATA
            value = self.vram[self.addr & 0x3FFF]
            self.addr = (self.addr + (32 if self.ctrl & 0x04 else 1)) & 0x7FFF
            return value
        return 0

    def write(self, reg, value):
        if reg == 0:
            self.ctrl = value
        elif reg == 1:
            self.mask = value
            self.rendering_enabled = bool(value & 0x18)
        elif reg == 3:
            self.oam_addr = value
        elif reg == 4:
            self.oam[self.oam_addr] = value
            self.oam_addr = (self.oam_addr + 1) & 0xFF
        elif reg == 5:
            self.scroll[1 if self.latch else 0] = value
            self.latch = not self.latch
        elif reg == 6:
            if self.latch:
                self.addr = (self.addr & 0xFF00) | value
            else:
                self.addr = (self.addr & 0x00FF) | (value << 8)
            self.latch = not self.latch
        elif reg == 7:
            if self.rendering_enabled and not self.in_vblank:
                self.writes_outside_vblank += 1
            self.vram[self.addr & 0x3FFF] = value
            self.addr = (self.addr + (32 if self.ctrl & 0x04 else 1)) & 0x7FFF


class Mmc3:
    def __init__(self, rom):
        self.rom = rom
        self.prg_bank_count = len(rom.prg) // 8192
        self.select = 0
        self.regs = [0] * 8
        self.mirroring = 0
        self.irq_latch = 0
        self.irq_enabled = False
        self.prg_ram = bytearray(0x2000)
        self.prg_ram_enabled = False

    def prg_offset(self, addr):
        slot = (addr - 0x8000) // 0x2000
        last = self.prg_bank_count - 1
        if self.select & 0x40:
            banks = (last - 1, self.regs[7], self.regs[6], last)
        else:
            banks = (self.regs[6], self.regs[7], last - 1, last)
        bank = banks[slot] % self.prg_bank_count
        return bank * 0x2000 + (addr & 0x1FFF)

    def read(self, addr):
        if 0x6000 <= addr < 0x8000:
            return self.prg_ram[addr - 0x6000]
        return self.rom.prg[self.prg_offset(addr)]

    def write(self, addr, value):
        if 0x6000 <= addr < 0x8000:
            self.prg_ram[addr - 0x6000] = value
            return
        even = (addr & 1) == 0
        if addr < 0xA000:
            if even:
                self.select = value
            else:
                self.regs[self.select & 7] = value
        elif addr < 0xC000:
            if even:
                self.mirroring = value & 1
            else:
                self.prg_ram_enabled = bool(value & 0x80)
        elif addr < 0xE000:
            if even:
                self.irq_latch = value
        else:
            self.irq_enabled = not even


class Nes:
    def __init__(self, rom_path):
        self.rom = Rom(rom_path)
        if self.rom.mapper != 4:
            raise ValueError("このハーネスは MMC3 (mapper 4) 専用。ROM は mapper %d" % self.rom.mapper)
        self.ram = bytearray(0x800)
        self.ppu = Ppu()
        self.mapper = Mmc3(self.rom)
        self.cpu = Cpu(self)
        self.pad = 0
        self.pad_shift = 0
        self.pad_strobe = False
        self.dma_count = 0
        self.frame = 0
        self.oam_dma_page = None

    # ---- バス ----
    def read(self, addr):
        if addr < 0x2000:
            return self.ram[addr & 0x7FF]
        if addr < 0x4000:
            return self.ppu.read(addr & 7)
        if addr == 0x4016:
            value = self.pad_shift & 1
            self.pad_shift = (self.pad_shift >> 1) | 0x80
            return value
        if addr == 0x4017:
            return 0
        if addr < 0x4020:
            return 0
        if addr < 0x6000:
            return 0
        return self.mapper.read(addr)

    def write(self, addr, value):
        if addr < 0x2000:
            self.ram[addr & 0x7FF] = value
        elif addr < 0x4000:
            self.ppu.write(addr & 7, value)
        elif addr == 0x4014:
            self.oam_dma_page = value
            base = value << 8
            for i in range(256):
                self.ppu.oam[(self.ppu.oam_addr + i) & 0xFF] = self.read(base + i)
            self.dma_count += 1
            self.cpu.cycles += 513
        elif addr == 0x4016:
            strobe = bool(value & 1)
            if self.pad_strobe and not strobe:
                self.pad_shift = self.pad
            self.pad_strobe = strobe
            if strobe:
                self.pad_shift = self.pad
        elif addr < 0x4020:
            pass                                  # APU は無視する
        elif addr >= 0x6000:
            self.mapper.write(addr, value)

    # ---- 実行 ----
    def set_buttons(self, names):
        """押しているボタン名の集合を設定する。例: {"RIGHT", "A"}"""
        # シフトレジスタは A から順に bit0 側から出てくる
        value = 0
        for i, name in enumerate(BUTTONS):
            if name in names:
                value |= 1 << i
        self.pad = value

    def reset(self):
        self.cpu.reset()

    def run_cycles(self, count, budget_instructions=2_000_000):
        target = self.cpu.cycles + count
        executed = 0
        while self.cpu.cycles < target:
            self.cpu.step()
            executed += 1
            if executed > budget_instructions:
                raise CpuCrash("1フレーム内で命令数の上限を超えた（無限ループの疑い）: PC=$%04X"
                               % self.cpu.pc)

    def run_frame(self):
        """1フレーム進める。可視期間 -> VBlank(NMI) -> 復帰。"""
        self.run_cycles(CYCLES_PER_FRAME - VBLANK_CYCLES)
        self.ppu.in_vblank = True
        if self.ppu.nmi_enabled:
            self.cpu.nmi()
        self.run_cycles(VBLANK_CYCLES)
        self.ppu.in_vblank = False
        self.frame += 1

    def run_frames(self, n):
        for _ in range(n):
            self.run_frame()


def load_labels(path):
    """ld65 の -Ln が出すラベルファイルを {名前: アドレス} に読む。"""
    labels = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 3 and parts[0] == "al":
                labels[parts[2].lstrip(".")] = int(parts[1], 16)
    return labels
