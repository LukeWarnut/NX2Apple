"""Ampere NVN2 control sections inside BNSH (Switch 2), without game data."""
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import shader_split as split


def nvn2_ctl(ncode, own=0xd8, arch=0xb, group=(0, 0, 0)):
    ctl = bytearray(own)
    ctl[:4] = split.CTL_MAGIC
    struct.pack_into('<II', ctl, 4, 0xc9, arch)
    for off in (0x30, 0x98, 0xc4, 0xc8):
        if off + 4 <= own: struct.pack_into('<I', ctl, off, own)
    struct.pack_into('<I', ctl, 0x44, ncode)
    struct.pack_into('<3I', ctl, 0x58, *group)
    return bytes(ctl)


def bnsh(stages):
    """One variation. stages: [(stage, code)]. Offsets match shader_split.bnsh_shaders."""
    d = bytearray(0x200)
    d[0:8] = b'BNSH\x00\x00\x00\x00'
    d[0x60:0x64] = b'grsc'
    struct.pack_into('<I', d, 0x7c, 1)
    struct.pack_into('<Q', d, 0x80, 0x100)
    struct.pack_into('<Q', d, 0x110, 0x140)   # variation +0x10, the program
    cursor = 0x200
    for i, (st, code) in enumerate(stages):
        sc = 0x180 + i * 0x28
        struct.pack_into('<Q', d, 0x148 + 8 * split.STAGES.index(st), sc)
        ctl = nvn2_ctl(len(code))
        pctl, pcode = cursor, cursor + len(ctl)
        d += ctl + code
        cursor += len(ctl) + len(code)
        struct.pack_into('<QQII', d, sc + 8, pctl, pcode, len(code), len(ctl))
    struct.pack_into('<I', d, 0x1c, len(d))
    return bytes(d)


class Nvn2ShaderTests(unittest.TestCase):
    def test_a_short_maxwell_section_is_not_nvn2(self):
        ctl = bytearray(0x850)
        ctl[:4] = split.CTL_MAGIC
        struct.pack_into('<I', ctl, 0x20, 0x850)
        self.assertIsNone(split.nvn2_control(bytes(ctl)))
        self.assertEqual(split.ctl_len(bytes(ctl)), 0x850)
        full = bytearray(0x880)
        full[:4] = split.CTL_MAGIC
        struct.pack_into('<I', full, 0x20, 0x880)
        self.assertEqual(split.ctl_len(bytes(full)), 0x880)

    def test_bnsh_variation_is_read(self):
        vs, ps = b'\x02\x0c\x04\x81' + bytes(0xfc), b'\x00\x0c\x54\x82' + bytes(0x4c)
        got = split.bnsh_shaders(bnsh([('vs', vs), ('ps', ps)]), 0, 'main')
        self.assertEqual([(st, code) for _w, st, _c, code in got], [('vs', vs), ('ps', ps)])
        self.assertEqual(split.ctl_len(got[0][2]), 0xd8)
        self.assertIsNone(split.check('main', 'vs', got[0][2], vs))
        bad = bytearray(got[0][2])
        struct.pack_into('<I', bad, 0x44, len(vs) + 4)
        self.assertEqual(split.check('main', 'vs', bytes(bad), vs), 'control +0x44 does not match the code area length')
        short = nvn2_ctl(len(vs), own=0xd0, arch=2)
        self.assertEqual(split.nvn2_control(short), (0xd0, len(vs)))
        self.assertIsNone(split.check('effect', 'vs', short, vs))
        comp = nvn2_ctl(0x100, own=0xd0, arch=0, group=(32, 1, 1))
        self.assertEqual(split.nvn2_group(comp), (32, 1, 1))

    def test_bahs_reduced_control_is_read(self):
        """agl / gsys shader packs: a 0xd8 control with only +0xc4 set, the stage from the binary record's kind (bahs_shaders)"""
        def reduced(ncode):
            ctl = bytearray(nvn2_ctl(ncode))
            for off in (0x30, 0x98, 0xc8): struct.pack_into('<I', ctl, off, 0)
            return bytes(ctl)
        self.assertEqual(split.nvn2_control(reduced(0x200)), (0xd8, 0x200))
        half = bytearray(reduced(0x200)); struct.pack_into('<I', half, 0x98, 0xd8)
        self.assertIsNone(split.nvn2_control(bytes(half)))   # a length at +0x98 but not at +0x30 is neither form
        vs, cs = b'\x81\x04\x0c\x02' + bytes(0x1fc), bytes(0x100)
        region = 0x400
        d = bytearray(region)
        d[0:4] = b'BAHS'; struct.pack_into('<II', d, 0x18, 0x300, region)
        recs = ((0x40, 0, 0x000, vs), (0x80, 3, 0x200, cs))
        for i, (a, kind, off, code) in enumerate(recs):
            c = 0x200 + i * 0xe0
            struct.pack_into('<4I', d, a - 0x10, 0x28, kind, 0, 0)
            struct.pack_into('<IIIi', d, a, off, 0, 0xd8, c - a)
            d[c:c + 0xd8] = reduced(len(code))
        d += vs + cs + bytes(0x300 - len(vs) - len(cs))
        got = split.bahs_shaders(bytes(d), 'gsys')
        self.assertEqual([(st, code) for _w, st, _c, code in got], [('vs', vs), ('cs', cs)])
        self.assertEqual(split.ctl_len(got[0][2]), 0xd8)

    def test_bananza_main_if_present(self):
        main = Path('/Users/lwarn/Downloads/Donkey Kong Bananza (v3.2.0)/040074A01BF12000_build/extract/v458752/ExeFS/main')
        if not main.is_file(): self.skipTest('Bananza dump is not on this machine')
        img = split.nso_image(str(main))
        found = []
        split.scan_blob(img, 'main', found)
        nvn2 = [s for s in found if split.nvn2_control(s[2])]
        self.assertEqual(len(nvn2), 96)
        self.assertTrue(all(len(ctl) == 0xd8 and nv[1] == len(code) for _w, _st, ctl, code in nvn2 for nv in [split.nvn2_control(ctl)]))


if __name__ == '__main__':
    unittest.main()
