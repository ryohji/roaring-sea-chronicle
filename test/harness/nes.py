"""NES バスと MMC3 マッパーの最小実装（回帰テスト用）。

PPU はレジスタの振る舞いと VRAM/OAM への副作用だけを再現する。
描画は行わない。スキャンライン単位のタイミング（スプライト欠け、MMC3 IRQ）は
再現しないので、その検証は Mesen2 側で行うこと（ADR-0004）。
"""
import os

from cpu6502 import Cpu, CpuCrash, FLAG_C, FLAG_Z, FLAG_N, FLAG_V

CYCLES_PER_FRAME = 29780        # NTSC の1フレームの CPU サイクル数（近似）
VBLANK_CYCLES = 2273            # VBlank 期間の CPU サイクル数（近似）

BUTTONS = ("A", "B", "SELECT", "START", "UP", "DOWN", "LEFT", "RIGHT")

# Nes.call() が「サブルーチンから戻ってきた」ことを検出するための番兵アドレス。
# ROM にも RAM にも属さない領域を選んである（ここが実行されることは無い）。
CALL_RETURN = 0x4100


# ---------------------------------------------------------------- NMI の転送量
# 「1回の NMI が $2007 に何バイト書いたか」を**全テストの全 NMI**で数える。
#
# なぜ抜き取りでは足りないか: 転送キューの記録の連なりが1バイトでもずれると、NMI は
# データの途中を記録のヘッダとして読む。長さ0のヘッダと読めば `ldy vq_n` が 0 のまま
# 256 回まわり、1回の NMI が $2007 に 256 バイト以上書く（VBlank を倍以上はみ出す）。
# これは**特定の位相でしか起きない**ので、「混んでいるフレームを 20 フレーム見る」
# 抜き取りでは位相しだいで素通りする（実際、位相をずらす掃引で初めて再現した）。
#
# 数えるだけはここで常時行い、予算との比較は run_tests.py が最後に1回まとめて行う。
# Nes は検証ごとに作り直されるので、記録はモジュール側（プロセス全体）に置く。
NMI_WRITE_WATCH = {
    "nmis": 0,          # 観測した NMI の回数
    "max": 0,           # 1回の NMI が $2007 に書いた最大バイト数
    "max_cycles": 0,    # そのときの NMI ハンドラのサイクル数
    "max_where": None,  # そのときの (ROM, フレーム番号)
}


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
        self.data_writes = 0                # $2007 への書き込み総数（NMI ごとの差分を取る）

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
            self.data_writes += 1
            if self.rendering_enabled and not self.in_vblank:
                self.writes_outside_vblank += 1
            self.vram[self.addr & 0x3FFF] = value
            self.addr = (self.addr + (32 if self.ctrl & 0x04 else 1)) & 0x7FFF


class Mmc3:
    """MMC3。PRG-RAM ($6000-$7FFF) は ADR-0001（案A）のセーブ領域なので、
    $A001 の有効ビット(bit7)と書込禁止ビット(bit6)を両方再現する。

    無効のまま $6000-$7FFF を読むと実機は開放バスを返す。ここでは近似として
    OPEN_BUS($FF) を返す（実機の値は直前にバスに乗っていた値で不定）。
    無効／書込禁止のときの書き込みは捨てる。「有効化を忘れたのにセーブできている」
    という偽の合格を出さないための再現である。
    """

    OPEN_BUS = 0xFF

    def __init__(self, rom):
        self.rom = rom
        self.prg_bank_count = len(rom.prg) // 8192
        self.select = 0
        self.regs = [0] * 8
        self.mirroring = 0
        self.irq_latch = 0
        self.irq_enabled = False
        self.prg_ram = bytearray(0x2000)
        self.prg_ram_enabled = False            # $A001 bit7
        self.prg_ram_write_protected = False    # $A001 bit6（1 = 書き込み禁止）
        self.prg_ram_reg = None                 # $A001 に最後に書かれた値（None = 未書き込み）
        self.prg_ram_reg_writes = 0
        self.prg_ram_writes_denied = 0          # 無効／書込禁止のまま捨てた書き込みの回数
        self.prg_ram_reads_disabled = 0         # 無効のまま読んだ回数（開放バスを返した）

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
            if not self.prg_ram_enabled:
                self.prg_ram_reads_disabled += 1
                return self.OPEN_BUS
            return self.prg_ram[addr - 0x6000]
        return self.rom.prg[self.prg_offset(addr)]

    def write(self, addr, value):
        if 0x6000 <= addr < 0x8000:
            if not self.prg_ram_enabled or self.prg_ram_write_protected:
                self.prg_ram_writes_denied += 1
                return
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
                self.prg_ram_reg = value
                self.prg_ram_reg_writes += 1
                self.prg_ram_enabled = bool(value & 0x80)
                self.prg_ram_write_protected = bool(value & 0x40)
        elif addr < 0xE000:
            if even:
                self.irq_latch = value
        else:
            self.irq_enabled = not even


class CallResult:
    """Nes.call() の戻り。サブルーチンが返したレジスタとフラグ。"""

    def __init__(self, a, x, y, p, cycles, instructions, sp_delta):
        self.a = a
        self.x = x
        self.y = y
        self.p = p
        self.cycles = cycles
        self.instructions = instructions
        self.sp_delta = sp_delta        # 0 以外ならスタックの出し入れが釣り合っていない

    @property
    def carry(self):
        return bool(self.p & FLAG_C)

    @property
    def zero(self):
        return bool(self.p & FLAG_Z)

    @property
    def negative(self):
        return bool(self.p & FLAG_N)

    @property
    def overflow(self):
        return bool(self.p & FLAG_V)

    def __repr__(self):
        return ("CallResult(A=$%02X X=$%02X Y=$%02X C=%d Z=%d N=%d, %dサイクル)"
                % (self.a, self.x, self.y, self.carry, self.zero, self.negative, self.cycles))


class Nes:
    # 空回り検出の間隔（命令数）。run_cycles の注記を見よ。
    IDLE_SAMPLE = 48

    def __init__(self, rom_path, prg_ram_fill=0x00):
        """prg_ram_fill: 電源投入時の PRG-RAM の中身（1バイト or bytes）。

        本作の PRG-RAM は電池でバックアップされる（ADR-0001 案A）ので、
        電源投入時の内容は「前回の電源断の瞬間のまま」または電池切れ後の不定値である。
        起動時にゼロだと決めつけた検証をしないよう、埋め値を差し替えられるようにしてある。
        """
        self.rom = Rom(rom_path)
        if self.rom.mapper != 4:
            raise ValueError("このハーネスは MMC3 (mapper 4) 専用。ROM は mapper %d" % self.rom.mapper)
        self.rom_path = rom_path
        self.prg_ram_fill = prg_ram_fill
        self._power_on_state(None)

    def _power_on_state(self, prg_ram):
        """電源投入直後の状態を作る。prg_ram が bytes ならその内容を引き継ぐ。"""
        self.ram = bytearray(0x800)
        self.ppu = Ppu()
        self.mapper = Mmc3(self.rom)
        if prg_ram is not None:
            self.mapper.prg_ram[:] = prg_ram
        elif isinstance(self.prg_ram_fill, int):
            if self.prg_ram_fill:
                self.mapper.prg_ram[:] = bytes([self.prg_ram_fill]) * len(self.mapper.prg_ram)
        else:
            self.mapper.prg_ram[:] = self.prg_ram_fill
        self.cpu = Cpu(self)
        self.pad = 0
        self.pad_shift = 0
        self.pad_strobe = False
        self.dma_count = 0
        self.frame = 0
        self.oam_dma_page = None
        self.write_log = None       # list を入れると CPU の書き込みを (アドレス, 値) で記録する
        self.trace_nmi = False      # True にすると NMI ハンドラを単体で実行し、費用を測る
        self.nmi_cycles = None      # 直近の NMI ハンドラのサイクル数（trace_nmi 時のみ）
        self.nmi_writes = []        # 直近の NMI ハンドラが行った書き込み（trace_nmi 時のみ）
        # NMI ごとの $2007 書き込み数（常時。trace_nmi の有無に関係なく数える）
        self._nmi_stack = []
        self.nmi_data_writes = None     # 直近の NMI が $2007 に書いたバイト数
        self.nmi_data_writes_max = 0    # この Nes で観測した最大値
        self.bus_events = 0             # 副作用のある読み書きの回数（空回り検出に使う）
        self.idle_skips = 0             # 空回りを飛ばした回数（診断用）

    # ---- NMI の出入り（cpu6502 が呼ぶ。$2007 の書き込み数を NMI ごとに数えるため）----
    def on_nmi_enter(self):
        self._nmi_stack.append((self.ppu.data_writes, self.cpu.cycles))

    def on_rti(self):
        """rti で NMI（または IRQ）から戻った時点。書き込み数の差分を確定する。"""
        if not self._nmi_stack:
            return                      # IRQ ハンドラなど、入口を数えていない経路
        writes_at_entry, cycles_at_entry = self._nmi_stack.pop()
        count = self.ppu.data_writes - writes_at_entry
        cycles = self.cpu.cycles - cycles_at_entry
        self.nmi_data_writes = count
        if count > self.nmi_data_writes_max:
            self.nmi_data_writes_max = count
        NMI_WRITE_WATCH["nmis"] += 1
        if count > NMI_WRITE_WATCH["max"]:
            NMI_WRITE_WATCH["max"] = count
            NMI_WRITE_WATCH["max_cycles"] = cycles
            NMI_WRITE_WATCH["max_where"] = (os.path.basename(self.rom_path), self.frame)

    def snapshot_prg_ram(self):
        """セーブ領域 ($6000-$7FFF) の内容を取り出す（バスを経由しない検査用）。"""
        return bytes(self.mapper.prg_ram)

    def power_cycle(self, keep_prg_ram=True):
        """電源を入れ直す。CPU/RAM/PPU/マッパーは初期状態に戻し、
        電池でバックアップされる PRG-RAM だけ内容を引き継ぐ（keep_prg_ram=False で電池切れ）。

        P4 で campaign-dev がセーブを実装したら、
        「オートセーブ → power_cycle() → 再開で状態が一致する」
        「チェックサム破損 → 新規ゲーム扱い」の検証にこれを使う（ADR-0001 帰結節）。
        """
        saved = bytes(self.mapper.prg_ram) if keep_prg_ram else None
        self._power_on_state(saved)
        self.cpu.reset()

    # ---- バス ----
    # bus_events は「読み書きによって機械の状態が変わった回数」である。
    # run_cycles の空回り検出（下記 IDLE_SAMPLE）がこれを見て
    # 「この 48 命令は何の痕跡も残していない」と判定する。
    # 数えるのは *書き込み全部* と、*読むだけで状態が変わる番地*（$2000-$7FFF）だけ。
    # RAM と ROM の読み出しは副作用が無いので数えない（数えると命令フェッチで埋まる）。
    def read(self, addr):
        if addr < 0x2000:
            return self.ram[addr & 0x7FF]
        if addr >= 0x8000:
            return self.mapper.read(addr)         # ROM。読んでも何も変わらない
        self.bus_events += 1
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
        self.bus_events += 1
        if self.write_log is not None:
            self.write_log.append((addr, value))
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
        """count サイクルぶん進める。空回り（wait_nmi の待ちループ）は飛ばす。

        メインループは1フレームの仕事を終えたあと wait_nmi で NMI を待って回り続ける。
        実測でその待ちが**1フレームの命令の 82%**（9101 命令のうち 7455）を占めており、
        1命令ずつ回すとテスト全体の実行時間の大半がこの空回りに消える。

        飛ばしてよい条件は厳密に置く: IDLE_SAMPLE 命令の前後で
        **CPU のレジスタ・PC・スタックポインタ・フラグが全て同じで、かつ
        その間に bus_events が1つも増えていない**なら、機械の状態は
        IDLE_SAMPLE 命令前と完全に同一である（RAM も PPU も変わっていない）。
        同じ状態からは同じことが起きるので、割り込みが来るまで永久にこれを繰り返す。
        つまりサイクルを目標まで進めるのと観測上まったく区別がつかない。

        IDLE_SAMPLE を 48 にしてあるのは、長さ 1,2,3,4,6,8,12,16 命令の待ちループを
        取りこぼさない（48 がその倍数である）ためである。

        **ハングの検出はここの仕事ではない。**サイクルは空回りでも進むので、
        この関数の命令数の上限（budget_instructions）は元々1フレームでは到達しない。
        「1フレームぶんの更新が終わらない」は frame_end() が、
        「NMI が rti に来ない」は _run_nmi_handler() が名指しで捕まえる。
        """
        cpu = self.cpu
        target = cpu.cycles + count
        executed = 0
        mark = None
        while cpu.cycles < target:
            cpu.step()
            executed += 1
            if executed % self.IDLE_SAMPLE == 0:
                state = (cpu.pc, cpu.a, cpu.x, cpu.y, cpu.sp, cpu.p, self.bus_events)
                if state == mark:
                    self.idle_skips += 1
                    cpu.cycles = target       # 割り込みが来るまで何も起きない
                    return
                mark = state
            if executed > budget_instructions:
                raise CpuCrash("1フレーム内で命令数の上限を超えた（無限ループの疑い）: PC=$%04X"
                               % self.cpu.pc)

    def run_frame(self):
        """1フレーム進める。可視期間 -> VBlank(NMI) -> 復帰。"""
        self.run_cycles(CYCLES_PER_FRAME - VBLANK_CYCLES)
        self.ppu.in_vblank = True
        spent = 0
        if self.ppu.nmi_enabled:
            if self.trace_nmi:
                spent = self._run_nmi_handler()
            else:
                self.cpu.nmi()
        if spent < VBLANK_CYCLES:
            self.run_cycles(VBLANK_CYCLES - spent)
        self.ppu.in_vblank = False
        self.frame += 1

    def run_frames(self, n):
        for _ in range(n):
            self.run_frame()

    def _run_nmi_handler(self, budget_instructions=100000):
        """NMI を起動し、rti で戻るまでを単体で実行する。

        戻り値はハンドラが使ったサイクル数（NMI 応答の7サイクルと、$4014 への書き込みが
        止める 513 サイクルを含む）。このエミュレータは命令単位の近似なので、この値は
        「VBlank 予算に対しておおよそどれくらいか」を見るためのものである。
        スキャンライン単位の正確なタイミング検証は Mesen2 の仕事（ADR-0004）。
        """
        cpu = self.cpu
        sp_before = cpu.sp
        start = cpu.cycles
        outer_log = self.write_log
        self.write_log = []
        cpu.nmi()
        executed = 0
        try:
            while cpu.sp != sp_before:
                cpu.step()
                executed += 1
                if executed > budget_instructions:
                    raise CpuCrash("NMI ハンドラが %d 命令実行しても rti に到達しない: PC=$%04X。"
                                   "NMI の中で待ちループに入っている疑い" % (executed, cpu.pc))
        finally:
            self.nmi_writes = self.write_log
            self.write_log = outer_log
        self.nmi_cycles = cpu.cycles - start
        return self.nmi_cycles

    def call(self, addr, a=0, x=0, y=0, carry=False, budget_instructions=200000):
        """指定アドレスを jsr して rts で戻るまで実行し、CallResult を返す。

        power_cycle() と同じ性格の足場である。サブルーチン（rect_overlap のように
        引数がゼロページとレジスタで渡るもの）を、ゲームの進行を待たずに単体で叩くために使う。
        呼び出しの前後で CPU の状態（PC/SP/レジスタ）は復元するので、
        メインループを走らせている途中で割り込んで呼んでも続きを走らせられる。
        """
        cpu = self.cpu
        saved = (cpu.pc, cpu.sp, cpu.a, cpu.x, cpu.y, cpu.p)
        ret = (CALL_RETURN - 1) & 0xFFFF
        cpu.push(ret >> 8)
        cpu.push(ret & 0xFF)
        cpu.pc = addr & 0xFFFF
        cpu.a, cpu.x, cpu.y = a & 0xFF, x & 0xFF, y & 0xFF
        cpu.set_flag(FLAG_C, carry)
        start = cpu.cycles
        executed = 0
        try:
            while cpu.pc != CALL_RETURN:
                cpu.step()
                executed += 1
                if executed > budget_instructions:
                    raise CpuCrash("$%04X を呼んで %d 命令実行しても rts で戻ってこない: PC=$%04X。"
                                   "無限ループか、スタックを壊して別の場所へ飛んでいる"
                                   % (addr, executed, cpu.pc))
        finally:
            sp_after = cpu.sp
            result_regs = (cpu.a, cpu.x, cpu.y, cpu.p)
            cpu.pc, cpu.sp, cpu.a, cpu.x, cpu.y, cpu.p = saved
        return CallResult(result_regs[0], result_regs[1], result_regs[2], result_regs[3],
                          cpu.cycles - start, executed, sp_after - saved[1])

    def call_stepwise(self, addr, a=0, x=0, y=0, carry=False, budget_instructions=200000):
        """call() と同じ呼び出しを**1命令ずつ**進めるジェネレータ。

        yield のたびに「直前の命令を実行し終えた時点」で止まる。最初の yield は
        1命令も実行していない時点、最後の yield は rts で戻った時点である。
        yield が返す値はそこまでに実行した命令数。

        何のためにあるか: 6502 の命令は割り込みに中断されない。つまり NMI が入りうる
        のは**命令の切れ目だけ**である。したがって「メインが更新中に NMI が来ても
        壊れた値を読まない」という主張は、命令の切れ目を全部踏んで観測すれば
        **全数検査できる**（どのサイクルに NMI が来るかを再現する必要は無い）。
        run_nmi_now() と組み合わせれば、切れ目ごとに実際に NMI を走らせられる。

        注意: 呼び出し前後で CPU の状態（PC/SP/レジスタ）を復元するのは call() と同じだが、
        復元はジェネレータを**最後まで回すか close() した時点**で起きる。
        途中で捨てるときは close() すること（for 文を break で抜けたときは
        ジェネレータが回収される時点まで復元が遅れる）。
        """
        cpu = self.cpu
        saved = (cpu.pc, cpu.sp, cpu.a, cpu.x, cpu.y, cpu.p)
        ret = (CALL_RETURN - 1) & 0xFFFF
        cpu.push(ret >> 8)
        cpu.push(ret & 0xFF)
        cpu.pc = addr & 0xFFFF
        cpu.a, cpu.x, cpu.y = a & 0xFF, x & 0xFF, y & 0xFF
        cpu.set_flag(FLAG_C, carry)
        executed = 0
        try:
            while True:
                yield executed
                if cpu.pc == CALL_RETURN:
                    return
                cpu.step()
                executed += 1
                if executed > budget_instructions:
                    raise CpuCrash("$%04X を1命令ずつ呼んで %d 命令実行しても rts で戻ってこない: "
                                   "PC=$%04X" % (addr, executed, cpu.pc))
        finally:
            cpu.pc, cpu.sp, cpu.a, cpu.x, cpu.y, cpu.p = saved

    def run_nmi_now(self, in_vblank=True):
        """NMI ハンドラだけを1回起動し、その間の書き込み [(アドレス, 値), ...] を返す。

        run_frame() と違い、メインループを1サイクルも進めない。
        「いまこの瞬間に NMI が来たら何を書くか」を単体で見るための足場である
        （公開コピーと cam_x をわざと食い違わせて、NMI がどちらを読んでいるかを見るなど）。
        call_stepwise() の切れ目で呼べば、実機の競合と同じ粒度で割り込ませられる。
        """
        was_vblank = self.ppu.in_vblank
        self.ppu.in_vblank = in_vblank
        try:
            self._run_nmi_handler()
        finally:
            self.ppu.in_vblank = was_vblank
        return list(self.nmi_writes)


def boot(nes, limit=12):
    """起動処理（リセット → 最初の NMI）が終わるまでフレームを進め、かかったフレーム数を返す。

    「起動は N フレームで終わる」という前提をテストの各所に散らさないための足場である。
    起動処理の長さは P3/P4 で初期転送が増えれば延びる。延びたこと自体は
    run_tests.py が **専用の検証**（BOOT_FRAMES_MAX）で名指しで捕まえる。
    それ以外のテストは「起動が終わったら」という条件だけを使うこと。

    最初の NMI が来たかどうかは OAM DMA の有無で見る（NMI ハンドラの最初の仕事）。
    limit フレーム以内に来なければ None を返す（呼び出し側が失敗として報告すること）。
    """
    for i in range(1, limit + 1):
        nes.run_frames(1)
        if nes.dma_count:
            return i
    return None


# ---------------------------------------------------------------- 観測の作法
# ここから下は「いつ読むか」を決めるための足場である。読む量は2種類あり、
# 作法が違う。取り違えると、**プロダクトが正しいのにテストだけが落ちる**。
#
#   (a) 落ち着けば動かなくなる量 — 位置・OAM・カメラ・VRAM の中身
#       入力を離して数フレーム走らせれば（run_tests.py の settle()）、
#       フレーム内のどの位相で読んでも同じ値になる。位相をずらして確かめられる
#       （sample_phases）。
#
#   (b) 作業変数 — ent_lane_step / ent_lane_acc / ent_lane_dy、
#       これから増える act_timer / act_hitstop / act_invuln のようなタイマー類、
#       oam_next / oam_used / sort_count のような組み立て中の数
#       これらは**毎フレーム更新の途中で何度も書き換わる**。run_frames() が戻るのは
#       フレームの切れ目ではなく更新の**最中**なので、そこで生に読むと
#       「更新の途中の値」が見える。静止させても (a) のようには救えない。
#       更新の最中に読んでいる限り、メインループの命令数が変われば
#       （＝誰かが src/ に処理を1つ足せば）観測点が別の隙間に移動して答えが変わる。
#
#       (b) を読むときは frame_end() / step_frame() を通し、
#       **そのフレームの更新が終わった点**で読むこと。
#       「更新が終われば作業変数はこうなっている」はエンジンの不変条件であり、
#       そこでなら観測点の位置に依存しない。
#
#       実例: 「レーン移動の完了後に補間の作業変数が片付いている」は、
#       lane_step_one の到着処理（dec ent_lane_step → 足元Yの吸着 → acc/dy の消去）の
#       **途中**、dec の直後の分岐で観測していた。ent_lane_step だけ 0 になっていて
#       ent_lane_dy はまだ 20 のままの点である。エンジンは正しく、テストが覗く位置が
#       悪かった。src/action/ が入ってメインループが伸びた日に落ちた。

# 観測点をフレーム内でずらす量（サイクル）。メインループの途中・処理が終わって
# wait_nmi で待っている間・次の NMI の直前、をそれぞれ踏むように選んである。
PHASE_STEPS = (1200, 10000, 16000)

# メインループが「そのフレームの更新を終えて次の NMI を待っている」点の目印。
# main.s の wait_nmi（nmi_done を 0 にしてから立つまで回るループ）である。
WAIT_LABEL = "wait_nmi"


def sample_phases(nes, read, steps=PHASE_STEPS):
    """同じ量をフレーム内の複数の位置で観測し、値の列を返す。

    静止しているはずの量がここで揺れたら、そのテストは「フレームのどのサイクルで
    観測したか」に合否が依存している。run_cycles で位相をずらすだけなので、
    フレームの数え上げ（NMI の回数）には影響しない。
    """
    values = [read()]
    for delta in steps:
        nes.run_cycles(delta)
        values.append(read())
    return values


def _wait_span(labels):
    """wait_nmi のアドレス範囲 (先頭, 末尾+1) を返す。

    末尾は「次に来るグローバルラベル」で決める。ローカルラベル（@wait など）は
    proc の内側にあるので境界に使えない（使うと待ちループ自体が範囲から外れる）。
    """
    lo = labels.get(WAIT_LABEL)
    if lo is None:
        raise CpuCrash("ラベル %s が build/roaring.labels に無い。メインループの "
                       "「フレームの更新が終わった点」を特定できないので、"
                       "作業変数を安全に読めない（main.s の wait_nmi が消えたか名前が変わった）"
                       % WAIT_LABEL)
    after = [a for name, a in labels.items() if a > lo and "@" not in name]
    span = min(min(after) - lo, 64) if after else 64
    return lo, lo + span


def frame_end(nes, labels, max_instructions=100_000):
    """メインループがそのフレームぶんの更新を終え、次の NMI を待ち始める点まで進める。

    run_frames() の戻り位置は更新の**最中**である。作業変数（上の (b)）を読む前に
    これを通すと、「そのフレームの更新が終わった状態」を読める。
    NMI を1回も起こさないので、フレームの数え上げは変わらない（位相だけが進む）。

    止まるのは wait_nmi の**待ちループを回っている**点であって、wait_nmi に入った瞬間ではない。
    入口には nmi_done を 0 に戻す `lda #0 / sta nmi_done` があり、そこで止めてしまうと、
    「NMI を起こしてから再開する」使い方（frame_instructions）で立てたばかりの nmi_done を
    消してしまい、永久に待ち続ける。ループを1周して同じ番地に戻ったことで判定する。

    すでに待ちに入っているときは数命令で戻る（二重に呼んでも安全）。
    """
    lo, hi = _wait_span(labels)
    executed = 0
    seen = set()
    while True:
        pc = nes.cpu.pc
        if lo <= pc < hi:
            if pc in seen:              # 待ちループを1周した＝更新は終わっている
                return executed
            seen.add(pc)
        nes.cpu.step()
        executed += 1
        if executed > max_instructions:
            raise CpuCrash("%d 命令進めてもメインループが %s ($%04X-$%04X) の待ちループに戻らない: "
                           "PC=$%04X。1フレームぶんの更新が終わらない＝"
                           "どこかで別の待ちループに入っている"
                           % (executed, WAIT_LABEL, lo, hi - 1, nes.cpu.pc))


def step_frame(nes, labels, n=1):
    """n フレーム進め、最後のフレームの更新が終わった点で止める。

    作業変数を毎フレーム観測しながら進むループは run_frames ではなくこれを使うこと。
    """
    nes.run_frames(n)
    return frame_end(nes, labels)


def frame_instructions(nes, labels, max_instructions=100_000):
    """1フレームぶんの更新を**1命令ずつ**進めるジェネレータ。yield ごとに命令の切れ目で止まる。

    frame_end() は「更新が終わった点」という1箇所を選んで読む足場だが、
    こちらは**更新中のあらゆる命令の切れ目**を踏む。
    「静止しているエンティティの作業変数は、フレームのどこを覗いても 0 のままである」
    のような、位相に一切依存しないことそのものを主張したいときに使う
    （call_stepwise() が cam_publish に対してやっているのと同じ考え方の、フレーム版）。

    呼ぶ前にフレームの切れ目へ揃える。NMI ハンドラは単体で走らせてから、
    メインループの更新だけを刻む。
    """
    frame_end(nes, labels)                # 待ちループの中（nmi_done は 0 に戻された後）
    lo, hi = _wait_span(labels)
    nes.run_nmi_now()                     # nmi_done を立てる。待ちループが抜ける
    executed = 0
    while lo <= nes.cpu.pc < hi:          # まず待ちループから出る
        nes.cpu.step()
        executed += 1
        if executed > max_instructions:
            raise CpuCrash("NMI を起こしても %s から出てこない: PC=$%04X。"
                           "nmi_done が立っていない疑い" % (WAIT_LABEL, nes.cpu.pc))
    while not (lo <= nes.cpu.pc < hi):    # 更新が終わって待ちに戻るまで刻む
        yield executed
        nes.cpu.step()
        executed += 1
        if executed > max_instructions:
            raise CpuCrash("%d 命令進めてもメインループが %s に戻らない: PC=$%04X"
                           % (executed, WAIT_LABEL, nes.cpu.pc))
    yield executed


def load_labels(path):
    """ld65 の -Ln が出すラベルファイルを {名前: アドレス} に読む。"""
    labels = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 3 and parts[0] == "al":
                labels[parts[2].lstrip(".")] = int(parts[1], 16)
    return labels
