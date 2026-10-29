"""zepto8's loose NVN shader files (PicoMix), without game data."""
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import shader_split as split


def shader_file(stage, own=None, extra=b'', tail=None):
    code = bytearray(0x100)
    ctl = bytearray(0x880 + len(extra))
    ctl[:4] = split.CTL_MAGIC
    struct.pack_into('<I', ctl, 0x20, len(ctl) if own is None else own)
    struct.pack_into('<I', ctl, 0x704, len(code))
    if stage == 'cs': struct.pack_into('<3I', ctl, 0x748, 1, 1, 1)
    code[:4] = split.CS_MAGIC if stage == 'cs' else split.GFX_MAGIC
    if stage != 'cs': struct.pack_into('<I', code, 0x30, (split.STAGES.index(stage) + 1) << 10 | 1)
    if stage == 'ps': struct.pack_into('<I', code, 0x48, 1)   # reads generic location 0
    if extra: ctl[0x880:] = extra
    if tail is None: tail = b'\x01\xff\xff\x00image\x00\x02\x00pos\x00\x00'
    return struct.pack('<I', len(ctl)) + bytes(ctl) + struct.pack('<I', len(code)) + bytes(code) + tail, bytes(ctl), bytes(code)


class ZeptoShaderTests(unittest.TestCase):
    def test_loose_file_reaches_the_split(self):
        # The early-out used to skip a file with no BNSH, GLSLC or other container magic.
        data, ctl, code = shader_file('ps')
        directory = tempfile.mkdtemp()
        path = Path(directory) / 'scanlines.frag'
        path.write_bytes(data)
        rel, uniq, _pairs, _extra = split.scan_file((str(path), 'shaders/scanlines.frag'))
        self.assertEqual(rel, 'shaders/scanlines.frag')
        self.assertEqual([(st, c, k) for _w, st, c, k in uniq.values()], [('ps', ctl, code)])
        self.assertIn('#ZEPTO@', next(iter(uniq.values()))[0])

    def test_stages_and_the_single_stage_cover(self):
        found = []
        for stage in ('vs', 'ps', 'cs'):
            data, ctl, code = shader_file(stage)
            got = split.zepto_shaders(data, f'/shaders/g1.{stage}')
            self.assertEqual([(st, c, k) for _w, st, c, k in got], [(stage, ctl, code)])
            found += got
        keys = {split.skey(ctl, code): (where, st, ctl, code) for where, st, ctl, code in found}
        pairs, cover = split.separable_pairs(keys)
        self.assertEqual(len(cover), 1)
        self.assertEqual(pairs, {(split.skey(found[0][2], found[0][3]), cover[0])})

    def test_copied_control_can_differ_from_its_own_length(self):
        # Bytes past control +0x20 stay in the copy the game makes. A header longer than the copy still yields the stored bytes.
        longer, ctl, _code = shader_file('vs', own=0x880, extra=b'\xab' * 0x20)
        self.assertEqual(split.zepto_shaders(longer, '/shaders/fantasy_02.frag')[0][2], ctl)
        shorter, ctl, _code = shader_file('ps', own=0x900)
        self.assertEqual(len(ctl), 0x880)
        self.assertEqual(split.zepto_shaders(shorter, '/shaders/fantasy.frag')[0][2], ctl)
        self.assertEqual(struct.unpack_from('<I', ctl, 0x20)[0], 0x900)

    def test_empty_attribute_name_and_a_file_that_is_not_one(self):
        data, _ctl, _code = shader_file('vs', tail=b'\x02\x00\x00\x00')
        self.assertEqual(split.zepto_shaders(data, '/shaders/g1.vert')[0][1], 'vs')
        good = shader_file('ps')[0]
        for bad in (b'pico-8 cartridge', good[:-1], good + b'\x00', good[:8], bytes(0x2000)):
            with self.subTest(size=len(bad)):
                self.assertEqual(split.zepto_shaders(bad, '/unrelated'), [])
        wrong = bytearray(good)
        struct.pack_into('<I', wrong, 4 + struct.unpack_from('<I', wrong, 0)[0], struct.unpack_from('<I', wrong, 0x708)[0] + 4)
        self.assertEqual(split.zepto_shaders(bytes(wrong), '/shaders/bad.frag'), [])


if __name__ == '__main__':
    unittest.main()
