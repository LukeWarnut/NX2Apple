"""Switch 2 dumps: raw exefs/ and romfs/ staged like an NSP extraction. No game files, keys or Xcode."""
import importlib.util
import io
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'dm/games'))
sys.path.insert(0, str(REPO / 'dm/tools'))
import game as gamemod
import nacp
import profile_guess

spec = importlib.util.spec_from_file_location('build_ipa_from_nsp', REPO / 'scripts/build_ipa_from_nsp.py')
nsp = importlib.util.module_from_spec(spec); spec.loader.exec_module(nsp)
nsp.pg, nsp.nacp = profile_guess, nacp
sw = nsp.switch2mod()

APP = 0x040074A01BF12000
DLC = APP + 0x1000 + 1
BID = 'ab' * 16
BANANZA = Path('/Users/lwarn/Downloads/Donkey Kong Bananza (v3.2.0)/updates/458752')


def npdm(program, sysres=0):
    d = bytearray(0x80)
    d[0:4] = b'META'
    struct.pack_into('<I', d, 0x14, sysres)
    d += b'ACI0' + bytes(12) + struct.pack('<Q', program)
    return bytes(d)


def nso(path, bid=BID):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'NSO0' + bytes(0x3c) + bytes.fromhex(bid) + bytes(0x20))


def dump_tree(root, program=APP, version='458752', with_dlc=True, main_name='main'):
    """A game folder shaped like a Switch 2 dump: updates/<version>/{exefs,romfs} and dlc/<title id>/0/romfs."""
    ver = Path(root) / 'updates' / version
    ex, rom = ver / 'exefs', ver / 'romfs'
    nso(ex / main_name)
    (ex / 'main.npdm').write_bytes(npdm(program))
    rom.mkdir(parents=True)
    (rom / 'a.txt').write_bytes(b'rom')
    dlc = None
    if with_dlc:
        dlc = Path(root) / 'dlc' / f'{DLC:016x}' / '0' / 'romfs'
        dlc.mkdir(parents=True)
        (dlc / 'b.txt').write_bytes(b'dlc')
    return ver, rom, dlc


class DumpLayout(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmp))
        self.game = self.tmp / 'Donkey Kong Bananza (v3.2.0)'
        self.ver, self.rom, self.dlc = dump_tree(self.game)

    def test_version_folder_and_game_root(self):
        for folder in (self.ver, self.game):
            d = sw.load(folder)
            self.assertEqual(d.app, APP)
            self.assertEqual(list(d.versions), ['v458752'])
            self.assertEqual(d.name, 'Donkey Kong Bananza')
            self.assertEqual(d.display, '3.2.0')
            self.assertEqual([(i, p.resolve()) for i, p in d.dlc], [(1, self.dlc.resolve())])
            self.assertEqual(d.root.resolve(), self.game.resolve())

    def test_explicit_exefs_and_romfs_find_dlc(self):
        d = sw.load(exefs=self.ver / 'exefs', romfs=self.rom)
        self.assertEqual(d.app, APP)
        self.assertEqual(d.versions['v458752'].exefs, (self.ver / 'exefs').resolve())
        self.assertEqual([(i, p.resolve()) for i, p in d.dlc], [(1, self.dlc.resolve())])

    def test_update_program_id_is_the_application(self):
        game = self.tmp / 'Update (v1.0.0)'
        ver, rom, dlc = dump_tree(game, program=APP | 0x800)
        d = sw.load(ver)
        self.assertEqual(d.app, APP)
        self.assertEqual([(i, p.resolve()) for i, p in d.dlc], [(1, dlc.resolve())])

    def test_dlc_program_id_is_rejected(self):
        game = self.tmp / 'Bad'
        ver, _, _ = dump_tree(game, program=DLC, with_dlc=False)
        with self.assertRaises(sw.DumpError):
            sw.load(ver)

    def test_missing_pair_is_rejected(self):
        with self.assertRaises(sw.DumpError):
            sw.load(self.tmp)

    def test_index_zero_is_not_reported(self):
        """The host's aoc list ignores a name that does not start with 1-9, so index 0 is left out."""
        zero = self.game / 'dlc' / f'{APP + 0x1000:016x}' / '0' / 'romfs'
        zero.mkdir(parents=True)
        (zero / 'z.txt').write_bytes(b'z')
        d = sw.load(self.game)
        self.assertEqual([i for i, _ in d.dlc], [1])
        self.assertTrue(any('skipping' in w for w in d.warnings))

    def test_main_nso_is_linked_as_main(self):
        game = self.tmp / 'Renamed (v1.0)'
        ver, rom, _ = dump_tree(game, main_name='main.nso', with_dlc=False, version='1')
        d = sw.load(ver)
        dest = self.tmp / 'extract'
        out = sw.stage(d, dest, ['v1'])
        main = out['v1'] / 'ExeFS' / 'main'
        self.assertTrue(main.is_symlink())
        self.assertEqual(main.resolve(), (ver / 'exefs' / 'main.nso').resolve())
        self.assertFalse((out['v1'] / 'ExeFS' / 'main.nso').exists())

    def test_stage_links_and_a_second_stage_reuses(self):
        d = sw.load(self.ver)
        dest = self.tmp / 'extract'
        first = sw.stage(d, dest, ['v458752'])
        aoc = sw.stage_aoc(d.dlc, self.tmp / 'aoc')
        rom = first['v458752'] / 'RomFS'
        self.assertTrue(rom.is_symlink())
        self.assertEqual(rom.resolve(), self.rom.resolve())
        self.assertEqual((first['v458752'] / 'ExeFS').resolve(), (self.ver / 'exefs').resolve())
        self.assertTrue((aoc / '1').is_symlink())
        self.assertEqual((aoc / '1').resolve(), self.dlc.resolve())
        again = sw.stage(d, dest, ['v458752'])
        self.assertEqual(again['v458752'], first['v458752'])
        self.assertTrue((again['v458752'] / 'RomFS').is_symlink())

    def test_romfs_image(self):
        d = sw.load(self.ver)
        dest = self.tmp / 'extract'
        out = sw.stage(d, dest, ['v458752'], pack_image=True)
        img = out['v458752'] / 'RomFS.img'
        self.assertEqual(img.read_bytes()[:8], b'DMROMFS1')
        self.assertFalse((out['v458752'] / 'RomFS').exists())

    def test_uppercase_exefs_romfs(self):
        ver = self.tmp / 'Upper' / 'updates' / '3'
        (ver / 'ExeFS').mkdir(parents=True)
        nso(ver / 'ExeFS' / 'main')
        (ver / 'ExeFS' / 'main.npdm').write_bytes(npdm(APP))
        (ver / 'RomFS').mkdir()
        (ver / 'RomFS' / 'a.txt').write_bytes(b'x')
        d = sw.load(ver)
        self.assertEqual(d.versions['v3'].exefs.resolve(), (ver / 'ExeFS').resolve())
        self.assertEqual(d.versions['v3'].romfs.resolve(), (ver / 'RomFS').resolve())


class ScriptFlags(unittest.TestCase):
    def test_switch2_is_opt_in(self):
        self.assertFalse(nsp.switch2_requested(nsp.parse_args([])))
        self.assertTrue(nsp.switch2_requested(nsp.parse_args(['--switch2', '/tmp/dump'])))
        self.assertTrue(nsp.switch2_requested(nsp.parse_args(['--switch2'])))
        self.assertTrue(nsp.switch2_requested(nsp.parse_args(['--exefs', '/e', '--romfs', '/r'])))
        folder, ex, rom, dlc = nsp.switch2_paths(nsp.parse_args(['--switch2', '/tmp/dump', '--dlc', '/tmp/dlc']))
        self.assertEqual(folder, '/tmp/dump')
        self.assertIsNone(ex)
        self.assertEqual(dlc, ['/tmp/dlc'])

    def test_settings_select_switch2(self):
        with mock.patch.object(nsp, 'SWITCH2', '/tmp/from-settings'):
            args = nsp.parse_args([])
            self.assertTrue(nsp.switch2_requested(args))
            self.assertEqual(nsp.switch2_paths(args)[0], '/tmp/from-settings')

    def test_exefs_without_romfs_is_an_error(self):
        with self.assertRaises(nsp.BuildError):
            nsp.switch2_paths(nsp.parse_args(['--exefs', '/e']))

    def test_help_mentions_switch2(self):
        buf = io.StringIO()
        with mock.patch('sys.stdout', buf), self.assertRaises(SystemExit) as e:
            nsp.parse_args(['-h'])
        self.assertEqual(e.exception.code, 0)
        self.assertIn('--switch2', buf.getvalue())
        self.assertIn('--romfs-image', buf.getvalue())


class PrepareKnownProfile(unittest.TestCase):
    def test_stages_a_registered_build_without_extracting(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(tmp))
        game_dir = tmp / 'Donkey Kong Bananza (v3.2.0)'
        ver, rom, dlc = dump_tree(game_dir)
        prof = tmp / 'games' / f'{APP:016X}'
        prof.mkdir(parents=True)
        (prof / 'game.tsv').write_text('\n'.join([
            f'title_id\t{APP:016X}', f'program_id\t{APP:016X}', 'name\tDonkey Kong Bananza',
            'ipa\tDonkeyKongBananza.ipa', 'modules\tmain',
            f'build\tv458752\t3.2.0\tmain={BID}', 'package\tv458752', 'shader_versions\tv458752',
        ]) + '\n')
        work = tmp / 'work'
        args = nsp.parse_args(['--switch2', str(ver), '--platform', 'mac', '--work-dir', str(work)])
        env = dict(os.environ, PATH=os.environ.get('PATH', '/usr/bin:/bin'))
        with mock.patch.dict(os.environ, {'DM_GAMES_DIR': str(tmp / 'games')}), \
             mock.patch.object(nsp, 'check_prereqs', lambda *a, **k: None):
            staged = nsp.prepare_switch2(args, REPO, env, sys.executable, gamemod, True, False, False, True)
        self.assertEqual(staged.app, APP)
        self.assertEqual(staged.pkg_ver, 'v458752')
        self.assertFalse(staged.provisional)
        self.assertFalse(staged.derived)
        self.assertEqual(staged.loose_name, 'Donkey Kong Bananza')
        self.assertEqual(staged.loose_display, '3.2.0')
        exe = staged.dirs['v458752'] / 'ExeFS'
        self.assertTrue(exe.is_symlink())
        self.assertEqual(nsp.build_id(exe / 'main'), BID)
        self.assertEqual((staged.dirs['v458752'] / 'RomFS').resolve(), rom.resolve())
        self.assertEqual((staged.aoc / '1').resolve(), dlc.resolve())
        self.assertIsNone(staged.picks['v458752'][1])


class BananzaOnDisk(unittest.TestCase):
    def test_real_dump_resolves(self):
        if not (BANANZA / 'exefs' / 'main.npdm').is_file():
            self.skipTest('Bananza dump is not on this machine')
        d = sw.load(BANANZA)
        self.assertEqual(d.app, APP)
        self.assertIn('v458752', d.versions)
        self.assertEqual(d.name, 'Donkey Kong Bananza')
        self.assertEqual(d.display, '3.2.0')
        self.assertIn(1, [i for i, _ in d.dlc])


if __name__ == '__main__':
    unittest.main()
