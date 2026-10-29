#!/usr/bin/env python3
# A Switch 2 dump is already decrypted: a version folder holds exefs/ and romfs/ (updates/458752), and add-on content sits beside it at dlc/<title id>/<index>/romfs.
# The packing script stages those directories into the same extract layout an NSP extraction produces (ExeFS, RomFS or RomFS.img, aoc/<index>) and then runs the ordinary build. No prod.keys.
# The title ID is main.npdm's program ID. A folder whose name is a number is that package version (458752 → v458752). A parent named like "Game (v3.2.0)" supplies the display version when there is no control.nacp.
import os, re, shutil, struct, sys
from pathlib import Path

_SKIP = {'updates', 'dlc', 'exefs', 'romfs', 'base'}


class DumpError(Exception):
    pass


class Content:
    def __init__(self, version, root, exefs, romfs, nacp, icon):
        self.version, self.root, self.exefs, self.romfs, self.nacp, self.icon = version, root, exefs, romfs, nacp, icon


class Dump:
    def __init__(self, app, root, name, display, versions, dlc, warnings):
        self.app, self.root, self.name, self.display = app, root, name, display
        self.versions, self.dlc, self.warnings = versions, dlc, warnings


def child_ci(d, name):
    if not Path(d).is_dir(): return None
    want = name.lower()
    for p in Path(d).iterdir():
        if p.name.lower() == want and p.is_dir(): return p
    return None


def version_of_dir(name):
    if re.fullmatch(r'v\d+', name): return name
    if re.fullmatch(r'\d+', name): return 'v' + name
    return None


def titles_from(root):
    """(game name, display version) from a folder named 'Game (v3.2.0)', walking up past updates/ and the version number."""
    chain, here = [], Path(root)
    for _ in range(4):
        chain.append(here)
        if here.parent == here: break
        here = here.parent
    for p in chain:
        m = re.fullmatch(r'(?P<name>.*?)\s*\(v(?P<ver>\d+(?:\.\d+){0,2})\)\s*', p.name)
        if m and m.group('name').strip(): return m.group('name').strip(), m.group('ver')
    for p in chain:
        if p.name.lower() not in _SKIP and not re.fullmatch(r'v?\d+', p.name) and not re.fullmatch(r'[0-9A-Fa-f]{16}', p.name):
            return p.name, ''
    return '', ''


def find_named(directory, name):
    directory = Path(directory)
    for p in (directory / name, directory / 'control' / name):
        if p.is_file(): return p
    return None


def program_id(npdm):
    d = Path(npdm).read_bytes()
    a = d.find(b'ACI0')
    if a < 0 or a + 0x18 > len(d): raise DumpError(f'{npdm} has no program ID (no ACI0)')
    return struct.unpack_from('<Q', d, a + 0x10)[0]


def application_id(program):
    """The base title. An update's program ID ends in 800; add-on content is not a game's main.npdm."""
    low = program & 0xFFF
    if low == 0: return program
    if low == 0x800: return program & ~0xFFF
    raise DumpError(f'main.npdm program ID {program:016X} is neither an application (…000) nor an update (…800)')


def check_main(exefs):
    exefs = Path(exefs)
    main = exefs / 'main' if (exefs / 'main').is_file() else exefs / 'main.nso'
    if not main.is_file(): raise DumpError(f'no main module in {exefs}')
    magic = main.read_bytes()[:4]
    if magic != b'NSO0': raise DumpError(f'{main} is not an NSO (the file starts with {magic!r}, expected NSO0)')
    if not (exefs / 'main.npdm').is_file(): raise DumpError(f'no main.npdm in {exefs}')
    return main


def content_of(path):
    path = Path(path)
    exefs, romfs = child_ci(path, 'exefs'), child_ci(path, 'romfs')
    if exefs is None or romfs is None: raise DumpError(f'{path} has no exefs/ and romfs/')
    check_main(exefs)
    ver = version_of_dir(path.name) or 'v0'
    return Content(ver, path, exefs, romfs, find_named(path, 'control.nacp'), find_named(path, 'icon.jpg'))


def romfs_under(path):
    """romfs directories one or two levels down (dlc/<title id>/romfs, or dlc/<title id>/0/romfs)."""
    out, path = [], Path(path)
    direct = child_ci(path, 'romfs')
    if direct: out.append(direct)
    for sub in path.iterdir():
        if sub.is_dir() and not sub.name.startswith('.') and sub.name.lower() != 'romfs':
            found = child_ci(sub, 'romfs')
            if found: out.append(found)
    return out


def dlc_from_tid_dir(path, app, warnings):
    tid = int(path.name, 16)
    idx = tid - app - 0x1000
    if not 1 <= idx <= 2000:
        warnings.append(f'skipping {path.name}: not add-on content of {app:016X}')
        return None
    roms = romfs_under(path)
    if not roms:
        warnings.append(f'skipping {path.name}: no romfs/')
        return None
    zeros = [r for r in roms if r.parent.name == '0']
    rom = zeros[0] if zeros else roms[0]
    if len(roms) > 1 and not zeros: warnings.append(f'{path.name} has {len(roms)} romfs directories; using {rom}')
    return idx, rom


def find_dlc(dlc_root, app, warnings):
    out = []
    if not dlc_root or not Path(dlc_root).is_dir(): return out
    for child in sorted(Path(dlc_root).iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name.startswith('.'): continue
        if re.fullmatch(r'[0-9A-Fa-f]{16}', child.name):
            item = dlc_from_tid_dir(child, app, warnings)
            if item: out.append(item)
        elif re.fullmatch(r'\d+', child.name) and child_ci(child, 'romfs'):
            idx = int(child.name)
            if 1 <= idx <= 2000: out.append((idx, child_ci(child, 'romfs')))
            else: warnings.append(f'skipping DLC index {idx}: the host reports indices 1 to 2000')
    return out


def extra_dlc(paths, app):
    out, warnings = [], []
    for raw in paths:
        p = Path(raw).expanduser().resolve()
        if not p.exists(): raise DumpError(f'no such DLC path: {p}')
        if p.is_dir() and p.name.lower() == 'dlc':
            out.extend(find_dlc(p, app, warnings)); continue
        if p.is_dir() and re.fullmatch(r'[0-9A-Fa-f]{16}', p.name):
            item = dlc_from_tid_dir(p, app, warnings)
            if item: out.append(item)
            continue
        rom = p if p.name.lower() == 'romfs' else (child_ci(p, 'romfs') or p)
        item = None
        for anc in (rom.parent, rom.parent.parent, rom.parent.parent.parent):
            if re.fullmatch(r'[0-9A-Fa-f]{16}', anc.name):
                item = dlc_from_tid_dir(anc, app, warnings)
                break
        if item is None and re.fullmatch(r'\d+', rom.parent.name):
            idx = int(rom.parent.name)
            if 1 <= idx <= 2000: item = (idx, rom if rom.name.lower() == 'romfs' else rom)
        if item is None:
            raise DumpError(f'cannot tell which add-on content {p} is: put it under dlc/<title id>/, or name a parent folder with the title ID')
        out.append(item)
    return out, warnings


def merge_dlc(groups, warnings):
    """groups: iterable of (index, romfs). The same index twice keeps the later path."""
    out = {}
    for idx, rom in groups:
        if idx in out and out[idx] != rom: warnings.append(f'DLC index {idx} is given twice; using {rom}')
        out[idx] = rom
    return sorted(out.items())


def _add_ver(versions, content, warnings):
    if content.version in versions: warnings.append(f'{content.version} is in the dump twice; using {content.root}')
    versions[content.version] = content


def finish(root, versions, warnings):
    if not versions: raise DumpError(f'{root} has no exefs/ and romfs/ (pass a version folder such as updates/458752, or a game folder that contains updates/<version>/)')
    newest = versions[max(versions, key=lambda v: int(v[1:]))]
    app = application_id(program_id(newest.exefs / 'main.npdm'))
    name, display = titles_from(root)
    dlc = find_dlc(child_ci(root, 'dlc'), app, warnings)
    return Dump(app, Path(root), name, display, versions, dlc, warnings)


def from_folder(folder):
    path = Path(folder).expanduser().resolve()
    if not path.is_dir(): raise DumpError(f'not a folder: {path}')
    warnings, versions = [], {}
    if has_pair(path):
        _add_ver(versions, content_of(path), warnings)
        root = path.parent.parent if path.parent.name.lower() == 'updates' else path
        return finish(root, versions, warnings)
    updates = child_ci(path, 'updates')
    if updates:
        for d in sorted(p for p in updates.iterdir() if p.is_dir() and not p.name.startswith('.')):
            if has_pair(d): _add_ver(versions, content_of(d), warnings)
    base = child_ci(path, 'base')
    if base and has_pair(base):
        c = content_of(base); c.version = 'v0'; _add_ver(versions, c, warnings)
    return finish(path, versions, warnings)


def has_pair(path):
    return child_ci(path, 'exefs') is not None and child_ci(path, 'romfs') is not None


def from_pair(exefs, romfs, folder):
    exefs, romfs = Path(exefs).expanduser().resolve(), Path(romfs).expanduser().resolve()
    if not exefs.is_dir() or not romfs.is_dir(): raise DumpError(f'exefs and romfs must be folders: {exefs}, {romfs}')
    check_main(exefs)
    parent = exefs.parent
    ver = version_of_dir(parent.name) or 'v0'
    if folder: root = Path(folder).expanduser().resolve()
    elif parent.parent.name.lower() == 'updates': root = parent.parent.parent   # .../updates/<version>/exefs → the game folder, where dlc/ sits
    else: root = parent
    content = Content(ver, parent, exefs, romfs, find_named(parent, 'control.nacp'), find_named(parent, 'icon.jpg'))
    return finish(root, {ver: content}, [])


def load(folder=None, exefs=None, romfs=None, dlc=()):
    """The dump. folder is a version directory or a game directory; exefs and romfs name the two directories when they are given on their own."""
    dlc = list(dlc or [])
    if exefs or romfs:
        if not (exefs and romfs): raise DumpError('a raw ExeFS needs the RomFS too (--exefs and --switch2-romfs)')
        dump = from_pair(exefs, romfs, folder)
    else:
        if not folder: raise DumpError('give a Switch 2 folder, or --exefs and --switch2-romfs')
        dump = from_folder(folder)
    extra, warns = extra_dlc(dlc, dump.app)
    dump.warnings.extend(warns)
    dump.dlc = merge_dlc(list(dump.dlc) + extra, dump.warnings)
    return dump


def _remove(path):
    path = Path(path)
    if path.is_symlink() or path.is_file(): path.unlink()
    elif path.is_dir(): shutil.rmtree(path)


def place(src, dst):
    """A symlink at dst pointing at src. An existing link that already does is left in place. A copy when the filesystem refuses the link."""
    src, dst = Path(src).resolve(), Path(dst)
    if not src.exists(): raise DumpError(f'missing {src}')
    if dst.is_symlink():
        try:
            if dst.resolve() == src: return 'reused'
        except OSError: pass
    _remove(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        dst.symlink_to(src, target_is_directory=src.is_dir())
        return 'linked'
    except OSError:
        if src.is_dir(): shutil.copytree(src, dst)
        else: shutil.copy2(src, dst)
        return 'copied'


def place_optional(src, dst):
    if src and Path(src).is_file(): place(src, dst)
    else: _remove(dst)


def link_exefs(src, dst):
    """The whole exefs directory when it already uses the host's names (main, sdk, rtld). main.nso is linked as main."""
    files = [p for p in Path(src).iterdir() if p.is_file() and not p.name.startswith('.') and not p.name.startswith('._')]
    if any(p.name == 'main' for p in files):
        place(src, dst); return
    _remove(dst); dst.mkdir(parents=True, exist_ok=True)
    for p in files:
        place(p, dst / (p.name[:-4] if p.name.endswith('.nso') else p.name))


def _romfs_image():
    tools = Path(__file__).resolve().parents[1] / 'dm/tools'
    if str(tools) not in sys.path: sys.path.insert(0, str(tools))
    import romfs_image
    return romfs_image


def attach_romfs(src, dest, pack_image):
    """Link romfs/ as dest/RomFS. pack_image writes dest/RomFS.img instead (one file, what an NSP extraction produces). A filesystem that cannot link falls through to the image."""
    src = Path(src).resolve()
    folder, img = Path(dest) / 'RomFS', Path(dest) / 'RomFS.img'
    if not pack_image:
        if folder.is_symlink():
            try:
                if folder.resolve() == src:
                    _remove(img); return 'reused'
            except OSError: pass
        _remove(img); _remove(folder)
        try:
            folder.symlink_to(src, target_is_directory=True)
            return 'linked'
        except OSError: pass
    _remove(folder)
    _romfs_image().pack(str(src), str(img), reuse=True)
    return 'image'


def stage_one(content, dest, pack_image):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    link_exefs(content.exefs, dest / 'ExeFS')
    place_optional(content.nacp, dest / 'control.nacp')
    place_optional(content.icon, dest / 'icon.jpg')
    how = attach_romfs(content.romfs, dest, pack_image)
    return dest, how


def stage(dump, extract_root, versions, pack_image=False):
    """{version: extract directory}. ExeFS and RomFS are links into the dump, so the dump stays where it is."""
    extract_root = Path(extract_root)
    out = {}
    for ver in versions:
        if ver not in dump.versions: raise DumpError(f'the dump has no {ver}')
        out[ver] = stage_one(dump.versions[ver], extract_root / ver, pack_image)[0]
    return out


def stage_aoc(pairs, dest):
    """aoc/<index> links. None when there is no DLC. An index that the dump no longer has is removed, so a rebuild does not keep reporting it."""
    dest = Path(dest)
    if not pairs:
        if dest.exists(): shutil.rmtree(dest)
        return None
    dest.mkdir(parents=True, exist_ok=True)
    want = {str(i) for i, _ in pairs}
    for p in list(dest.iterdir()):
        if p.name not in want: _remove(p)
    for idx, rom in pairs:
        if not Path(rom).is_dir(): raise DumpError(f'DLC index {idx}: romfs is not a folder: {rom}')
        place(rom, dest / str(idx))
    return dest
