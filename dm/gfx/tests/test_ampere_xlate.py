"""Ampere (Switch 2 NVN2) translation: synthetic programs through dmsh_xlate's AmpereDecoder, no game data.
The encodings follow Mesa NAK's sm70_encode.rs (the field layouts AmpereDecoder reads). The translator is DMSH_TEST_XLATE, else the in-repo dmsh_xlate_bin."""
import json
import os
import re
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / 'tools'
XLATE = os.environ.get('DMSH_TEST_XLATE') or str(TOOLS / 'dmsh_xlate_bin/dmsh_xlate')
SPIRV_CROSS = os.environ.get('SPIRV_CROSS') or str(TOOLS / 'spirv_cross_bin/spirv-cross')
RZ, PT, URZ = 255, 7, 63


def runnable(path):
    try:
        return subprocess.run([path, '--source-sha'], capture_output=True, timeout=60).returncode == 0
    except OSError:
        return False


def inst(op12, *fields, pred=PT):
    """One 128-bit instruction: opcode bits 0-11, guard predicate 12-15, then (start, width, value) fields"""
    w = op12 | pred << 12
    for start, width, value in fields:
        w |= (value & ((1 << width) - 1)) << start
    return w.to_bytes(16, 'little')


def alu(op9, form, rd, ra=RZ, *fields):
    return inst(op9 | form << 9, (16, 8, rd), (24, 8, ra), *fields)


def cbuf(bank, offset):
    return [(38, 16, offset), (54, 5, bank)]


def rel(at, target):   # a branch's offset (bits 32-81) from the next instruction
    return (32, 50, target - (at + 16))


EXIT = inst(0x94d, (87, 3, PT))


def bra_self(at):
    return inst(0x947, rel(at, at), (87, 3, PT))


def program(*insts):
    code = b''.join(insts)
    return code + bra_self(len(code))


def sph(words):
    out = [0] * 20
    for k, v in words.items():
        out[k] = v
    return struct.pack('<20I', *out) + bytes(0x30)   # the SPH, zeros to 0x80 (AmpereDecoder.HeaderSize)


# Maxwell's SPH layout (version 4): word 0 type (1 VTG, 2 pixel) | version << 5 | shader type << 10; a pixel shader's input map in words 5-13, its output targets in word 18
PS_SPH = sph({0: 0x000c5482, 5: 0x80000000, 6: 0x0a00, 18: 0xf})
VS_SPH = sph({0: 0x020c0481, 6: 0x3, 13: 0x000ff000})


class Translating(unittest.TestCase):
    def translate(self, stage, code, c1=b'', **extra):
        with tempfile.TemporaryDirectory() as d:
            cp = Path(d) / 'code.bin'
            cp.write_bytes(code)
            job = {'key': 'k', 'stage': stage, 'code': str(cp), 'tex': {}, 'img': {}, 'attr': {}, 'fout': {}, 'c1': c1.hex(), 'ampere': True, 'debug': True, 'reduced': True, **extra}
            (Path(d) / 'jobs.json').write_text(json.dumps([job]))
            r = subprocess.run([XLATE, str(Path(d) / 'jobs.json'), d], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            res = json.loads((Path(d) / 'k.res.json').read_text())
            msl = subprocess.run([SPIRV_CROSS, '--msl', '--msl-version', '20300', str(Path(d) / 'k.spv')], capture_output=True, text=True)
            self.assertEqual(msl.returncode, 0, msl.stderr)
            return res, msl.stdout


@unittest.skipUnless(runnable(XLATE), 'no runnable translator (dmsh_xlate_bin needs the .NET 10 runtime)')
class AmpereTranslation(Translating):
    def test_pixel_shader_texture_and_uniforms(self):
        # IPA w, 1/w, IPA a[0x90..0x94] * 1/w, TEX.SCR 2D on the handle in c[2][0x20] (NVN's slot 0), colour = texel * c[3][0x0..] + c[3][0x10..], alpha saturated
        code = PS_SPH + program(
            inst(0x326, (16, 8, 6), (32, 8, RZ), (64, 8, 0x7c >> 2)),
            inst(0x326, (16, 8, 1), (32, 8, RZ), (64, 8, 0x90 >> 2)),
            inst(0x326, (16, 8, 5), (32, 8, RZ), (64, 8, 0x94 >> 2)),
            alu(0x108, 1, 6, RZ, (32, 8, 6), (74, 6, 4)),                                   # MUFU.RCP R6, R6
            alu(0x020, 1, 4, 6, (32, 8, 1), (84, 3, 4)),                                    # FMUL R4, R6, R1
            alu(0x020, 1, 5, 6, (32, 8, 5), (84, 3, 4)),                                    # FMUL R5, R6, R5
            inst(0xb60, (16, 8, 0), (24, 8, 4), (32, 8, 5), (40, 14, 0x20 >> 2), (54, 5, 2), (60, 1, 1), (61, 3, 1), (64, 8, 2), (72, 4, 0xf), (81, 3, PT)),
            *[alu(0x023, 3, c, c, (64, 8, 7), *cbuf(3, 0x10 + 4 * c), *([(77, 1, 1)] if c == 3 else [])) for c in range(4)],   # FFMA Rc, Rc, R7, c[3][0x10 + 4c]
            EXIT)
        res, msl = self.translate('ps', code)
        self.assertEqual(res['notes'], [])
        self.assertEqual([(t['handle'], t['cbuf_slot']) for t in res['textures']], [(8, -1)])   # word 8 of c[2]: the slot dmsh_build.py decodes as 0
        self.assertEqual([b['slot'] for b in res['cbufs']], [3])
        self.assertEqual(res['frag_out_map'], 15)
        self.assertIn('.sample(', msl)
        self.assertIn('gl_FragCoord.w', msl)
        self.assertIn('clamp(', msl)

    def test_switch_table_in_c1(self):
        # index = min(c[3][0], 1); BRX through the table at c[1][0] (offsets from the first instruction), each case writes the position's x
        head = [
            alu(0x002, 5, 4, RZ, *cbuf(3, 0)),                                            # MOV R4, c[3][0]
            alu(0x017, 4, 2, 4, (32, 32, 1), (87, 3, PT)),                                  # IMNMX.U32 R2, R4, 1, PT
            alu(0x019, 4, 9, 2, (32, 32, 2), (64, 8, RZ), (73, 2, 3)),                       # SHF.L.U32 R9, R2, 2, RZ
            inst(0xb82, (16, 8, 2), (24, 8, 9), *cbuf(1, 0), (73, 3, 4)),                   # LDC R2, c[1][R9]
        ]
        brx_at = 16 * len(head)
        case0, case1, join = brx_at + 16, brx_at + 48, brx_at + 80
        body = [
            inst(0x949, (24, 8, 2), rel(brx_at, 0), (87, 3, PT)),                          # BRX R2 to the first instruction + R2
            alu(0x002, 4, 8, RZ, (32, 32, 0x3f800000)),                                     # case 0: x = 1.0
            inst(0x947, rel(case0 + 16, join), (87, 3, PT)),
            alu(0x002, 4, 8, RZ, (32, 32, 0x40000000)),                                     # case 1: x = 2.0
            inst(0x947, rel(case1 + 16, join), (87, 3, PT)),
            inst(0x322, (24, 8, RZ), (32, 8, 8), (40, 10, 0x70), (64, 8, RZ)),              # AST a[0x70], R8
            EXIT,
        ]
        res, msl = self.translate('vs', VS_SPH + program(*head, *body), c1=struct.pack('<2I', case0, case1))
        self.assertEqual(res['notes'], [])
        # the address is compared with the targets (the last one needs no comparison) and both cases are kept
        self.assertTrue(f'== {case0}' in msl or f'== {case1}' in msl, msl)
        self.assertIn(str(0x3f800000), msl)
        self.assertIn(str(0x40000000), msl)

    def test_global_memory_through_c0_addresses(self):
        # ULDC.64 UR4, c[0][0x80]; LDG.128 R0, [R0 + UR4] reads its address before writing R0-R3; the sum goes to [c[0][0x90]]
        code = program(
            inst(0x919, (16, 8, 0), (72, 8, 0x21)),                                         # S2R R0, SR_TID.X
            alu(0x019, 4, 0, 0, (32, 32, 4), (64, 8, RZ), (73, 2, 3)),                       # SHF.L.U32 R0, R0, 4, RZ
            inst(0xab9, (16, 6, 4), *cbuf(0, 0x80), (73, 3, 5)),
            inst(0x981, (16, 8, 0), (24, 8, 0), (32, 6, 4), (72, 1, 1), (73, 3, 6), (91, 1, 1)),
            alu(0x021, 1, 4, 0, (32, 8, 1)),
            alu(0x021, 1, 4, 4, (32, 8, 2)),
            alu(0x021, 1, 4, 4, (32, 8, 3)),
            inst(0xab9, (16, 6, 6), *cbuf(0, 0x90), (73, 3, 5)),
            inst(0x986, (24, 8, RZ), (32, 8, 4), (64, 6, 6), (72, 1, 1), (73, 3, 4), (91, 1, 1)),
            EXIT)
        res, msl = self.translate('cs', code, local=[32, 1, 1])
        self.assertEqual(res['notes'], [])
        self.assertEqual(sorted((b['cb_slot'], b['cb_offset']) for b in res['sbufs']), [(0, 0x80 // 4), (0, 0x90 // 4)])
        # the four loads index one base: none takes its index from another load's result (R0 is both the address and the first destination)
        loads = re.findall(r'(\w+) = [^;]*?\._m0\[([^\]]+)\]', msl)
        self.assertEqual(len(loads), 4, msl)
        loaded = {name for name, _ in loads}
        self.assertFalse([index for _, index in loads if loaded & set(re.findall(r'\w+', index))], msl)

    def test_sine_takes_turns(self):
        # Volta and later MUFU.SIN / COS take the angle in turns (NAK's fsin_normalized_2_pi)
        code = PS_SPH + program(
            inst(0x326, (16, 8, 0), (32, 8, RZ), (64, 8, 0x90 >> 2)),
            alu(0x108, 1, 0, RZ, (32, 8, 0), (74, 6, 1)),
            alu(0x002, 4, 1, RZ, (32, 32, 0)), alu(0x002, 4, 2, RZ, (32, 32, 0)), alu(0x002, 4, 3, RZ, (32, 32, 0)),
            EXIT)
        _, msl = self.translate('ps', code)
        self.assertIn('6.2831', msl)
        self.assertIn('sin(', msl)


# ---- On the GPU: one thread of a translated compute program reads IN from the storage buffer at c[0][0x80], runs the cases and writes one word each to the one at c[0][0x90]

RUNNER = r"""// Runs one compute kernel from MSL source: run.swift <kernel.metal> <input words hex file> <output word count>
// Buffers are bound in the order the kernel's [[buffer(n)]] parameters appear: index 0 = input, 1 = output (the translator's storage buffers, by c[0] offset)
import Foundation
import Metal

let args = CommandLine.arguments
let src = try! String(contentsOfFile: args[1], encoding: .utf8)
let inputWords = try! String(contentsOfFile: args[2], encoding: .utf8).split(separator: " ").map { UInt32($0, radix: 16)! }
let outCount = Int(args[3])!
let dev = MTLCreateSystemDefaultDevice()!
let lib = try! dev.makeLibrary(source: src, options: nil)
let fn = lib.makeFunction(name: "main0")!
let pso = try! dev.makeComputePipelineState(function: fn)
let inBuf = dev.makeBuffer(bytes: inputWords, length: max(4, inputWords.count * 4), options: .storageModeShared)!
let outBuf = dev.makeBuffer(length: outCount * 4, options: .storageModeShared)!
memset(outBuf.contents(), 0xcd, outCount * 4)
let q = dev.makeCommandQueue()!
let cb = q.makeCommandBuffer()!
let enc = cb.makeComputeCommandEncoder()!
enc.setComputePipelineState(pso)
enc.setBuffer(inBuf, offset: 0, index: 0)
enc.setBuffer(outBuf, offset: 0, index: 1)
enc.dispatchThreadgroups(MTLSize(width: 1, height: 1, depth: 1), threadsPerThreadgroup: MTLSize(width: 1, height: 1, depth: 1))
enc.endEncoding()
cb.commit()
cb.waitUntilCompleted()
let p = outBuf.contents().bindMemory(to: UInt32.self, capacity: outCount)
print((0..<outCount).map { String(format: "%08x", p[$0]) }.joined(separator: " "))
"""

U32 = 0xffffffff
f2u = lambda f: struct.unpack('<I', struct.pack('<f', f))[0]
u2f = lambda u: struct.unpack('<f', struct.pack('<I', u))[0]

IN = [0xfffffff0, 0x00000020, 0x12345678, 0x80000001, 5, 36, f2u(1.5), f2u(-123.0), f2u(4294967296.0), 0x7fc00000, 0xdeadbeef, 0xa5a5a5a5]

def ldg128(rd, off): return inst(0x981, (16, 8, rd), (24, 8, RZ), (32, 6, 4), (40, 24, off), (72, 1, 1), (73, 3, 6), (90, 1, 1), (91, 1, 1))
def stg(r, k): return inst(0x986, (24, 8, RZ), (32, 8, r), (40, 24, 4 * k), (64, 6, 6), (72, 1, 1), (73, 3, 4), (90, 1, 1), (91, 1, 1))

cases = []   # (instructions, result register, expected u32 or callable check, name)
def case(name, insts, reg, expect): cases.append((name, insts, reg, expect))

R2P_CLEAR = inst(0x804, (24, 8, RZ), (32, 32, 0x7f))                       # P0-P6 = 0
P2R = lambda rd, mask: inst(0x803, (16, 8, rd), (24, 8, RZ), (32, 32, mask))

a, b, c, d = IN[0], IN[1], IN[2], IN[3]
case('IADD3 carry out', [R2P_CLEAR, alu(0x010, 1, 20, 0, (32, 8, 1), (64, 8, RZ), (77, 4, 0xf), (81, 3, 0), (84, 3, 1), (87, 4, 0xf)), P2R(21, 3)], 21, ((a + b) >> 32) & 1)
case('IADD3 sum', [], 20, (a + b) & U32)
case('IADD3.X carry in', [alu(0x010, 1, 22, 3, (32, 8, RZ), (64, 8, RZ), (74, 1, 1), (77, 3, 7), (80, 1, 1), (81, 3, 7), (84, 3, 7), (87, 3, 0))], 22, (d + 1) & U32)
SHF = lambda rd, lo, sh, hi, ty, right, hiw, wrap=0: alu(0x019, 1, rd, lo, (32, 8, sh), (64, 8, hi), (73, 2, ty), (75, 1, wrap), (76, 1, right), (80, 1, hiw))
case('SHF.R.U32.HI', [SHF(23, RZ, 4, 2, 3, 1, 1)], 23, c >> 5)
case('SHF.R.S32.HI clamped', [SHF(24, RZ, 5, 3, 2, 1, 1)], 24, U32)
case('SHF.L.U64 low', [SHF(25, 2, 5, 3, 1, 0, 0)], 25, ((d << 32 | c) << 36) & U32)
case('SHF.L.U64.HI', [SHF(26, 2, 5, 3, 1, 0, 1)], 26, (((d << 32 | c) << 36) >> 32) & U32)
case('SHF.R.U64 low', [SHF(27, 2, 4, 3, 1, 1, 0)], 27, ((d << 32 | c) >> 5) & U32)
case('LEA.HI', [alu(0x011, 1, 28, 2, (32, 8, 3), (64, 8, 0), (75, 5, 4), (80, 1, 1), (81, 3, 7))], 28, ((((a << 32 | c) << 4) >> 32) + d) & U32)
case('LEA', [alu(0x011, 1, 29, 2, (32, 8, 1), (64, 8, RZ), (75, 5, 3), (81, 3, 7))], 29, ((c << 3) + b) & U32)
case('PRMT reverse', [alu(0x016, 4, 30, 2, (32, 32, 0x0123), (64, 8, RZ))], 30, 0x78563412)
case('PRMT sign', [alu(0x016, 4, 31, 3, (32, 32, 0xb321), (64, 8, RZ))], 31, 0xff800000)
ISETP = lambda pd, ra, rb, signed, ex, low=7: alu(0x00c, 1, 0, ra, (32, 8, rb), (68, 3, low), (72, 1, ex), (73, 1, signed), (76, 3, 1), (81, 3, pd), (84, 3, 7), (87, 3, 7))
case('ISETP.EX', [R2P_CLEAR, ISETP(0, 0, 2, 0, 0), ISETP(1, 1, 3, 1, 1, 0), ISETP(2, 1, 3, 0, 1, 0), P2R(32, 7)], 32,
     (int((a | b << 32) < (c | d << 32)) << 2) | (int(((a | b << 32) - (1 << 64) * 0) < ((c | d << 32) - (1 << 64))) << 1) | int(a < c))
wide = a * b + (IN[10] | IN[11] << 32)
case('IMAD.WIDE.U32 low', [alu(0x025, 1, 34, 0, (32, 8, 1), (64, 8, 10))], 34, wide & U32)
case('IMAD.WIDE.U32 high', [], 35, (wide >> 32) & U32)
F2I = lambda rd, rb, signed, dsz, rnd: alu(0x105, 1, rd, RZ, (32, 8, rb), (72, 1, signed), (75, 2, dsz), (77, 1, 1), (78, 2, rnd), (84, 2, 2))
case('F2I.S32.TRUNC', [F2I(36, 7, 1, 2, 3)], 36, (-123) & U32)
case('F2I.U32 saturates', [F2I(37, 8, 0, 2, 3)], 37, U32)
case('F2I NaN', [F2I(38, 9, 1, 2, 3)], 38, 0)
case('F2I.U16 nearest even', [F2I(39, 6, 0, 1, 0)], 39, 2)
case('I2F.U8 byte 2', [alu(0x106, 1, 40, RZ, (32, 8, 10), (60, 2, 2), (75, 2, 2), (84, 2, 0))], 40, f2u(float((IN[10] >> 16) & 0xff)))
case('I2F.S16 half 1', [alu(0x106, 1, 41, RZ, (32, 8, 11), (60, 2, 1), (74, 1, 1), (75, 2, 2), (84, 2, 1))], 41, f2u(float(((IN[11] >> 16) & 0xffff) - 0x10000)))
case('FLO.U32', [alu(0x100, 1, 42, RZ, (32, 8, 2))], 42, 28)
case('FLO.U32.SH', [alu(0x100, 1, 43, RZ, (32, 8, 2), (74, 1, 1))], 43, 3)
case('POPC', [alu(0x109, 1, 44, RZ, (32, 8, 10))], 44, bin(IN[10]).count('1'))
case('SGXT', [alu(0x01a, 4, 45, 10, (32, 32, 8), (73, 1, 1))], 45, 0xffffffef)
case('BMSK', [alu(0x01b, 4, 46, 4, (32, 32, 3))], 46, 0x7 << 5)
case('FSET.GT', [alu(0x00a, 1, 47, 6, (32, 8, 7), (76, 4, 4), (87, 3, 7))], 47, f2u(1.0))
case('FMNMX min', [alu(0x009, 1, 48, 6, (32, 8, 7), (87, 3, 7))], 48, f2u(-123.0))
case('FMUL.D2', [alu(0x020, 1, 49, 6, (32, 8, 6), (84, 3, 5))], 49, f2u(1.125))
case('MUFU.SIN turns', [alu(0x002, 4, 51, RZ, (32, 32, f2u(0.25))), alu(0x108, 1, 50, RZ, (32, 8, 51), (74, 6, 1))], 50, lambda v: abs(u2f(v) - 1.0) < 1e-3)
case('LOP3 pred', [R2P_CLEAR, alu(0x012, 4, 52, 2, (32, 32, 0x10000000), (64, 8, RZ), (72, 8, 0xc0), (81, 3, 3), (87, 3, 7), (90, 1, 1)), P2R(53, 0x8)], 53, 0x8)
case('LOP3 value', [], 52, c & 0x10000000)

FRND = lambda rd, rnd: alu(0x107, 1, rd, RZ, (32, 8, 6), (75, 2, 2), (78, 2, rnd), (84, 2, 2))
case('FRND nearest even', [FRND(54, 0)], 54, f2u(2.0))
case('FRND floor', [FRND(55, 1)], 55, f2u(1.0))
case('FRND ceil', [FRND(56, 2)], 56, f2u(2.0))
case('FRND trunc', [FRND(57, 3)], 57, f2u(1.0))
case('I2FP.F32.S32', [alu(0x045, 1, 58, RZ, (32, 8, 0), (74, 1, 1))], 58, f2u(-16.0))
case('F2FP.PACK_AB', [alu(0x03e, 1, 59, 6, (32, 8, 7), (64, 8, RZ))], 59, 0x3e00d7b0)
case('SEL by P3', [alu(0x007, 1, 60, 0, (32, 8, 1), (87, 3, 3))], 60, a)
case('IADD3 -Ra', [alu(0x010, 1, 61, 0, (32, 8, 1), (64, 8, RZ), (72, 1, 1), (77, 4, 0xf), (81, 3, 7), (84, 3, 7), (87, 4, 0xf))], 61, (-a + b) & U32)
case('R2UR UIADD3 MOV UR', [inst(0x3c2, (16, 6, 8), (24, 8, 2), (81, 3, 7)), inst(0x890, (16, 6, 9), (24, 6, 8), (32, 32, 0x10), (64, 6, 63), (77, 4, 0xf), (81, 3, 7), (84, 3, 7), (87, 4, 0xf), (91, 1, 1)), alu(0x002, 6, 62, RZ, (32, 6, 9), (91, 1, 1))], 62, (c + 0x10) & U32)
case('HADD2.F32 H0_H0', [alu(0x030, 1, 63, RZ, (32, 8, 59), (60, 2, 2), (72, 1, 1), (78, 1, 1))], 63, f2u(-123.0))


def ldg128(rd, off): return inst(0x981, (16, 8, rd), (24, 8, RZ), (32, 6, 4), (40, 24, off), (72, 1, 1), (73, 3, 6), (90, 1, 1), (91, 1, 1))
def stg(r, k): return inst(0x986, (24, 8, RZ), (32, 8, r), (40, 24, 4 * k), (64, 6, 6), (72, 1, 1), (73, 3, 4), (90, 1, 1), (91, 1, 1))


def swift_metal():
    try:
        return subprocess.run(['swift', '--version'], capture_output=True, timeout=60).returncode == 0 and subprocess.run(['xcrun', '-sdk', 'macosx', 'metal', '--version'], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@unittest.skipUnless(runnable(XLATE) and swift_metal(), 'no runnable translator, swift or Metal compiler')
class AmpereOnGpu(Translating):
    def test_cases(self):
        body = [inst(0xab9, (16, 6, 4), *cbuf(0, 0x80), (73, 3, 5)), inst(0xab9, (16, 6, 6), *cbuf(0, 0x90), (73, 3, 5)), ldg128(0, 0), ldg128(4, 16), ldg128(8, 32)]
        for _name, insts, _reg, _expect in cases:
            body += insts
        body += [stg(reg, k) for k, (_name, _insts, reg, _expect) in enumerate(cases)]
        res, msl = self.translate('cs', program(*body, EXIT), local=[1, 1, 1])
        self.assertEqual(sorted(b['cb_offset'] for b in res['sbufs']), [0x80 // 4, 0x90 // 4])
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'run.swift').write_text(RUNNER)
            (Path(d) / 'k.metal').write_text(msl)
            (Path(d) / 'in.txt').write_text(' '.join(f'{v:08x}' for v in IN))
            out = subprocess.run(['swift', str(Path(d) / 'run.swift'), str(Path(d) / 'k.metal'), str(Path(d) / 'in.txt'), str(len(cases))], capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
        for (name, _insts, _reg, expect), v in zip(cases, [int(x, 16) for x in out.stdout.split()]):
            with self.subTest(name):
                self.assertTrue(expect(v) if callable(expect) else v == expect, f'{name}: {v:08x}' + ('' if callable(expect) else f', want {expect:08x}'))


if __name__ == '__main__':
    unittest.main()
