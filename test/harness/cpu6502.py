"""6502 (NES 2A03) 命令エミュレータ。

回帰テスト用。**公式オペコードのみ**を実装し、未定義オペコードの実行は
クラッシュとして即座に失敗させる（qa-runner の成功条件「クラッシュ検出」に直結する）。

サイクル精度は持たない（命令単位の近似）。
スプライト欠け・IRQ タイミング・VBlank 予算の検証は Mesen2 側の責務である（ADR-0004）。
"""

FLAG_C = 0x01
FLAG_Z = 0x02
FLAG_I = 0x04
FLAG_D = 0x08
FLAG_B = 0x10
FLAG_U = 0x20
FLAG_V = 0x40
FLAG_N = 0x80


class CpuCrash(Exception):
    """未定義オペコード、スタック破壊など、実機ならハングする状態。"""


def _table():
    t = {}

    def add(op, name, mode, cycles, penalty=False):
        t[op] = (name, mode, cycles, penalty)

    groups = {
        "ADC": (0x69, 0x65, 0x75, 0x6D, 0x7D, 0x79, 0x61, 0x71),
        "AND": (0x29, 0x25, 0x35, 0x2D, 0x3D, 0x39, 0x21, 0x31),
        "CMP": (0xC9, 0xC5, 0xD5, 0xCD, 0xDD, 0xD9, 0xC1, 0xD1),
        "EOR": (0x49, 0x45, 0x55, 0x4D, 0x5D, 0x59, 0x41, 0x51),
        "LDA": (0xA9, 0xA5, 0xB5, 0xAD, 0xBD, 0xB9, 0xA1, 0xB1),
        "ORA": (0x09, 0x05, 0x15, 0x0D, 0x1D, 0x19, 0x01, 0x11),
        "SBC": (0xE9, 0xE5, 0xF5, 0xED, 0xFD, 0xF9, 0xE1, 0xF1),
    }
    modes = ("imm", "zp", "zpx", "abs", "absx", "absy", "indx", "indy")
    cycs = (2, 3, 4, 4, 4, 4, 6, 5)
    pen = (False, False, False, False, True, True, False, True)
    for name, ops in groups.items():
        for op, mode, c, p in zip(ops, modes, cycs, pen):
            add(op, name, mode, c, p)

    # 読み書きを伴うシフト/インクリメント系
    for name, ops in (("ASL", (0x0A, 0x06, 0x16, 0x0E, 0x1E)),
                      ("LSR", (0x4A, 0x46, 0x56, 0x4E, 0x5E)),
                      ("ROL", (0x2A, 0x26, 0x36, 0x2E, 0x3E)),
                      ("ROR", (0x6A, 0x66, 0x76, 0x6E, 0x7E))):
        for op, mode, c in zip(ops, ("acc", "zp", "zpx", "abs", "absx"), (2, 5, 6, 6, 7)):
            add(op, name, mode, c)
    for name, ops in (("DEC", (0xC6, 0xD6, 0xCE, 0xDE)), ("INC", (0xE6, 0xF6, 0xEE, 0xFE))):
        for op, mode, c in zip(ops, ("zp", "zpx", "abs", "absx"), (5, 6, 6, 7)):
            add(op, name, mode, c)

    # 分岐
    for op, name in ((0x90, "BCC"), (0xB0, "BCS"), (0xF0, "BEQ"), (0x30, "BMI"),
                     (0xD0, "BNE"), (0x10, "BPL"), (0x50, "BVC"), (0x70, "BVS")):
        add(op, name, "rel", 2, True)

    # ロード/ストア
    for op, mode, c, p in ((0xA2, "imm", 2, False), (0xA6, "zp", 3, False), (0xB6, "zpy", 4, False),
                           (0xAE, "abs", 4, False), (0xBE, "absy", 4, True)):
        add(op, "LDX", mode, c, p)
    for op, mode, c, p in ((0xA0, "imm", 2, False), (0xA4, "zp", 3, False), (0xB4, "zpx", 4, False),
                           (0xAC, "abs", 4, False), (0xBC, "absx", 4, True)):
        add(op, "LDY", mode, c, p)
    for op, mode, c in ((0x85, "zp", 3), (0x95, "zpx", 4), (0x8D, "abs", 4), (0x9D, "absx", 5),
                        (0x99, "absy", 5), (0x81, "indx", 6), (0x91, "indy", 6)):
        add(op, "STA", mode, c)
    for op, mode, c in ((0x86, "zp", 3), (0x96, "zpy", 4), (0x8E, "abs", 4)):
        add(op, "STX", mode, c)
    for op, mode, c in ((0x84, "zp", 3), (0x94, "zpx", 4), (0x8C, "abs", 4)):
        add(op, "STY", mode, c)

    # 比較
    for op, mode, c in ((0xE0, "imm", 2), (0xE4, "zp", 3), (0xEC, "abs", 4)):
        add(op, "CPX", mode, c)
    for op, mode, c in ((0xC0, "imm", 2), (0xC4, "zp", 3), (0xCC, "abs", 4)):
        add(op, "CPY", mode, c)

    add(0x24, "BIT", "zp", 3)
    add(0x2C, "BIT", "abs", 4)
    add(0x4C, "JMP", "abs", 3)
    add(0x6C, "JMP", "ind", 5)
    add(0x20, "JSR", "abs", 6)
    add(0x60, "RTS", "imp", 6)
    add(0x40, "RTI", "imp", 6)
    add(0x00, "BRK", "imp", 7)

    for op, name in ((0x18, "CLC"), (0xD8, "CLD"), (0x58, "CLI"), (0xB8, "CLV"),
                     (0x38, "SEC"), (0xF8, "SED"), (0x78, "SEI"),
                     (0xCA, "DEX"), (0x88, "DEY"), (0xE8, "INX"), (0xC8, "INY"),
                     (0xAA, "TAX"), (0xA8, "TAY"), (0xBA, "TSX"), (0x8A, "TXA"),
                     (0x9A, "TXS"), (0x98, "TYA"), (0xEA, "NOP")):
        add(op, name, "imp", 2)
    for op, name, c in ((0x48, "PHA", 3), (0x08, "PHP", 3), (0x68, "PLA", 4), (0x28, "PLP", 4)):
        add(op, name, "imp", c)
    return t


OPCODES = _table()


class Cpu:
    def __init__(self, bus):
        self.bus = bus
        self.a = self.x = self.y = 0
        self.sp = 0xFD
        self.pc = 0
        self.p = FLAG_I | FLAG_U
        self.cycles = 0
        self.pending_nmi = False
        self.pending_irq = False
        self.last_pc = None

    # ---- ヘルパ ----
    def read(self, addr):
        return self.bus.read(addr & 0xFFFF) & 0xFF

    def write(self, addr, value):
        self.bus.write(addr & 0xFFFF, value & 0xFF)

    def read16(self, addr):
        return self.read(addr) | (self.read(addr + 1) << 8)

    def push(self, value):
        self.write(0x0100 + self.sp, value)
        self.sp = (self.sp - 1) & 0xFF

    def pop(self):
        self.sp = (self.sp + 1) & 0xFF
        return self.read(0x0100 + self.sp)

    def set_flag(self, mask, on):
        if on:
            self.p |= mask
        else:
            self.p &= ~mask & 0xFF

    def get_flag(self, mask):
        return bool(self.p & mask)

    def set_nz(self, value):
        value &= 0xFF
        self.set_flag(FLAG_Z, value == 0)
        self.set_flag(FLAG_N, value & 0x80)
        return value

    # ---- 割り込み ----
    def reset(self):
        self.pc = self.read16(0xFFFC)
        self.sp = 0xFD
        self.p = FLAG_I | FLAG_U
        self.cycles = 0

    def nmi(self):
        self.push(self.pc >> 8)
        self.push(self.pc & 0xFF)
        self.push((self.p | FLAG_U) & ~FLAG_B & 0xFF)
        self.set_flag(FLAG_I, True)
        self.pc = self.read16(0xFFFA)
        self.cycles += 7

    def irq(self):
        if self.get_flag(FLAG_I):
            return False
        self.push(self.pc >> 8)
        self.push(self.pc & 0xFF)
        self.push((self.p | FLAG_U) & ~FLAG_B & 0xFF)
        self.set_flag(FLAG_I, True)
        self.pc = self.read16(0xFFFE)
        self.cycles += 7
        return True

    # ---- アドレッシング ----
    def _operand(self, mode, penalty):
        """(アドレス, 追加サイクル) を返す。acc/imp は アドレス None。"""
        extra = 0
        if mode in ("imp", "acc"):
            return None, 0
        if mode == "imm":
            addr = self.pc
            self.pc += 1
        elif mode == "zp":
            addr = self.read(self.pc)
            self.pc += 1
        elif mode == "zpx":
            addr = (self.read(self.pc) + self.x) & 0xFF
            self.pc += 1
        elif mode == "zpy":
            addr = (self.read(self.pc) + self.y) & 0xFF
            self.pc += 1
        elif mode == "abs":
            addr = self.read16(self.pc)
            self.pc += 2
        elif mode in ("absx", "absy"):
            base = self.read16(self.pc)
            self.pc += 2
            idx = self.x if mode == "absx" else self.y
            addr = (base + idx) & 0xFFFF
            if penalty and (base & 0xFF00) != (addr & 0xFF00):
                extra = 1
        elif mode == "ind":
            ptr = self.read16(self.pc)
            self.pc += 2
            # 実機のページ跨ぎバグを再現する
            lo = self.read(ptr)
            hi = self.read((ptr & 0xFF00) | ((ptr + 1) & 0xFF))
            addr = lo | (hi << 8)
        elif mode == "indx":
            zp = (self.read(self.pc) + self.x) & 0xFF
            self.pc += 1
            addr = self.read(zp) | (self.read((zp + 1) & 0xFF) << 8)
        elif mode == "indy":
            zp = self.read(self.pc)
            self.pc += 1
            base = self.read(zp) | (self.read((zp + 1) & 0xFF) << 8)
            addr = (base + self.y) & 0xFFFF
            if penalty and (base & 0xFF00) != (addr & 0xFF00):
                extra = 1
        elif mode == "rel":
            off = self.read(self.pc)
            self.pc += 1
            if off & 0x80:
                off -= 256
            addr = (self.pc + off) & 0xFFFF
        else:
            raise CpuCrash("未知のアドレッシングモード: %s" % mode)
        return addr, extra

    # ---- 実行 ----
    def step(self):
        self.last_pc = self.pc
        op = self.read(self.pc)
        entry = OPCODES.get(op)
        if entry is None:
            raise CpuCrash("未定義オペコード $%02X を PC=$%04X で実行した "
                           "(6502 の非公式命令は使わない方針)" % (op, self.pc))
        name, mode, base_cycles, penalty = entry
        self.pc = (self.pc + 1) & 0xFFFF
        addr, extra = self._operand(mode, penalty)
        cycles = base_cycles + extra
        cycles += getattr(self, "_op_" + name)(addr, mode)
        self.cycles += cycles
        return cycles

    # --- 演算 ---
    def _op_LDA(self, addr, mode):
        self.a = self.set_nz(self.read(addr)); return 0

    def _op_LDX(self, addr, mode):
        self.x = self.set_nz(self.read(addr)); return 0

    def _op_LDY(self, addr, mode):
        self.y = self.set_nz(self.read(addr)); return 0

    def _op_STA(self, addr, mode):
        self.write(addr, self.a); return 0

    def _op_STX(self, addr, mode):
        self.write(addr, self.x); return 0

    def _op_STY(self, addr, mode):
        self.write(addr, self.y); return 0

    def _op_ADC(self, addr, mode):
        m = self.read(addr)
        total = self.a + m + (1 if self.get_flag(FLAG_C) else 0)
        self.set_flag(FLAG_C, total > 0xFF)
        result = total & 0xFF
        self.set_flag(FLAG_V, bool((~(self.a ^ m) & (self.a ^ result)) & 0x80))
        self.a = self.set_nz(result)
        return 0

    def _op_SBC(self, addr, mode):
        m = self.read(addr) ^ 0xFF
        total = self.a + m + (1 if self.get_flag(FLAG_C) else 0)
        self.set_flag(FLAG_C, total > 0xFF)
        result = total & 0xFF
        self.set_flag(FLAG_V, bool((~(self.a ^ m) & (self.a ^ result)) & 0x80))
        self.a = self.set_nz(result)
        return 0

    def _op_AND(self, addr, mode):
        self.a = self.set_nz(self.a & self.read(addr)); return 0

    def _op_ORA(self, addr, mode):
        self.a = self.set_nz(self.a | self.read(addr)); return 0

    def _op_EOR(self, addr, mode):
        self.a = self.set_nz(self.a ^ self.read(addr)); return 0

    def _compare(self, reg, addr):
        m = self.read(addr)
        self.set_flag(FLAG_C, reg >= m)
        self.set_nz((reg - m) & 0xFF)
        return 0

    def _op_CMP(self, addr, mode):
        return self._compare(self.a, addr)

    def _op_CPX(self, addr, mode):
        return self._compare(self.x, addr)

    def _op_CPY(self, addr, mode):
        return self._compare(self.y, addr)

    def _op_BIT(self, addr, mode):
        m = self.read(addr)
        self.set_flag(FLAG_Z, (self.a & m) == 0)
        self.set_flag(FLAG_N, m & 0x80)
        self.set_flag(FLAG_V, m & 0x40)
        return 0

    def _rmw(self, addr, mode, fn):
        if mode == "acc":
            self.a = fn(self.a)
        else:
            self.write(addr, fn(self.read(addr)))
        return 0

    def _op_ASL(self, addr, mode):
        def f(v):
            self.set_flag(FLAG_C, v & 0x80)
            return self.set_nz((v << 1) & 0xFF)
        return self._rmw(addr, mode, f)

    def _op_LSR(self, addr, mode):
        def f(v):
            self.set_flag(FLAG_C, v & 0x01)
            return self.set_nz(v >> 1)
        return self._rmw(addr, mode, f)

    def _op_ROL(self, addr, mode):
        def f(v):
            carry = 1 if self.get_flag(FLAG_C) else 0
            self.set_flag(FLAG_C, v & 0x80)
            return self.set_nz(((v << 1) | carry) & 0xFF)
        return self._rmw(addr, mode, f)

    def _op_ROR(self, addr, mode):
        def f(v):
            carry = 0x80 if self.get_flag(FLAG_C) else 0
            self.set_flag(FLAG_C, v & 0x01)
            return self.set_nz((v >> 1) | carry)
        return self._rmw(addr, mode, f)

    def _op_INC(self, addr, mode):
        self.write(addr, self.set_nz(self.read(addr) + 1)); return 0

    def _op_DEC(self, addr, mode):
        self.write(addr, self.set_nz(self.read(addr) - 1)); return 0

    def _op_INX(self, addr, mode):
        self.x = self.set_nz(self.x + 1); return 0

    def _op_INY(self, addr, mode):
        self.y = self.set_nz(self.y + 1); return 0

    def _op_DEX(self, addr, mode):
        self.x = self.set_nz(self.x - 1); return 0

    def _op_DEY(self, addr, mode):
        self.y = self.set_nz(self.y - 1); return 0

    def _op_TAX(self, addr, mode):
        self.x = self.set_nz(self.a); return 0

    def _op_TAY(self, addr, mode):
        self.y = self.set_nz(self.a); return 0

    def _op_TXA(self, addr, mode):
        self.a = self.set_nz(self.x); return 0

    def _op_TYA(self, addr, mode):
        self.a = self.set_nz(self.y); return 0

    def _op_TSX(self, addr, mode):
        self.x = self.set_nz(self.sp); return 0

    def _op_TXS(self, addr, mode):
        self.sp = self.x; return 0

    def _op_PHA(self, addr, mode):
        self.push(self.a); return 0

    def _op_PHP(self, addr, mode):
        self.push(self.p | FLAG_B | FLAG_U); return 0

    def _op_PLA(self, addr, mode):
        self.a = self.set_nz(self.pop()); return 0

    def _op_PLP(self, addr, mode):
        self.p = (self.pop() & ~FLAG_B & 0xFF) | FLAG_U; return 0

    def _op_JMP(self, addr, mode):
        self.pc = addr; return 0

    def _op_JSR(self, addr, mode):
        ret = (self.pc - 1) & 0xFFFF
        self.push(ret >> 8)
        self.push(ret & 0xFF)
        self.pc = addr
        return 0

    def _op_RTS(self, addr, mode):
        lo = self.pop()
        hi = self.pop()
        self.pc = ((hi << 8) | lo) + 1 & 0xFFFF
        return 0

    def _op_RTI(self, addr, mode):
        self.p = (self.pop() & ~FLAG_B & 0xFF) | FLAG_U
        lo = self.pop()
        hi = self.pop()
        self.pc = ((hi << 8) | lo) & 0xFFFF
        return 0

    def _op_BRK(self, addr, mode):
        self.pc = (self.pc + 1) & 0xFFFF
        self.push(self.pc >> 8)
        self.push(self.pc & 0xFF)
        self.push(self.p | FLAG_B | FLAG_U)
        self.set_flag(FLAG_I, True)
        self.pc = self.read16(0xFFFE)
        return 0

    def _branch(self, addr, taken):
        if not taken:
            return 0
        extra = 1
        if (self.pc & 0xFF00) != (addr & 0xFF00):
            extra += 1
        self.pc = addr
        return extra

    def _op_BCC(self, addr, mode):
        return self._branch(addr, not self.get_flag(FLAG_C))

    def _op_BCS(self, addr, mode):
        return self._branch(addr, self.get_flag(FLAG_C))

    def _op_BEQ(self, addr, mode):
        return self._branch(addr, self.get_flag(FLAG_Z))

    def _op_BNE(self, addr, mode):
        return self._branch(addr, not self.get_flag(FLAG_Z))

    def _op_BMI(self, addr, mode):
        return self._branch(addr, self.get_flag(FLAG_N))

    def _op_BPL(self, addr, mode):
        return self._branch(addr, not self.get_flag(FLAG_N))

    def _op_BVC(self, addr, mode):
        return self._branch(addr, not self.get_flag(FLAG_V))

    def _op_BVS(self, addr, mode):
        return self._branch(addr, self.get_flag(FLAG_V))

    def _op_CLC(self, addr, mode):
        self.set_flag(FLAG_C, False); return 0

    def _op_SEC(self, addr, mode):
        self.set_flag(FLAG_C, True); return 0

    def _op_CLI(self, addr, mode):
        self.set_flag(FLAG_I, False); return 0

    def _op_SEI(self, addr, mode):
        self.set_flag(FLAG_I, True); return 0

    def _op_CLD(self, addr, mode):
        self.set_flag(FLAG_D, False); return 0

    def _op_SED(self, addr, mode):
        self.set_flag(FLAG_D, True); return 0

    def _op_CLV(self, addr, mode):
        self.set_flag(FLAG_V, False); return 0

    def _op_NOP(self, addr, mode):
        return 0
