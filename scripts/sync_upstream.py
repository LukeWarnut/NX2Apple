#!/usr/bin/env python3
"""Sync this fork with upstream seedds/nxnative.

The fork was started as a fresh repository (no shared git history), so GitHub's own
"Sync fork" does not apply. The sync is a mechanical procedure — take upstream's tree,
re-apply the fork's features, verify the result builds — and this script is that
procedure, so a scheduled CI job can run it unattended.

What it does:
  1. diffs the two upstream refs over the vendored paths (dm/ scripts/ docs/ AGENTS.md ref/), and
     adds any vendored file upstream has that the fork's tree lacks
  2. applies adds and modifications file by file, and deletions — except the fork's own
     files (the RomFS archive layer), which upstream must not delete
  3. re-applies the fork's patches to the files that carry them (PATCHED; one patcher each, in
     PATCHERS, with a marker only the fork's code contains); every patch is anchor-based and
     idempotent, and one that no longer fits fails the run loudly rather than producing a silently
     broken tree. Before any of that, check_unprotected() stops the run when a vendored file the
     fork edited by hand (it differs from the last sync base) is in none of OURS, PATCHED and
     DROPPED and upstream changes it now: the edit would be lost.
  4. verifies: every .py parses on the Python this fork supports as its floor (3.9),
     the iOS build script survives bash -n, the archive reader test passes, and the
     macOS host builds for a real profile (skippable with --skip-build)

On success the caller commits and pushes; on failure nothing is written outside the
working tree, so CI can open an issue with the log instead.

Every patcher is also run against the files of a real upstream ref by scripts/tests/test_sync_patchers.py
(CI, and locally with seedds/main fetched), so a patcher that no longer fits, or whose marker upstream's
own text satisfies, fails there before it can stop or silently skip a sync (issue #92).

Usage: sync_upstream.py --old <upstream sha> --new <upstream sha> [--skip-build]
       sync_upstream.py --old <last sync base> --check-protection   (list unprotected fork edits)
"""
import argparse, json, os, pwd, re, subprocess, sys, shutil, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENDORED = ('dm', 'scripts', 'docs', 'AGENTS.md', 'ref')
# The fork's own files, inside vendored paths: upstream deletes and rewrites must not touch them.
OURS = {'dm/host/romfs_arc.c', 'dm/host/romfs_arc.h', 'dm/tools/romfs_arc.py',
        'dm/host/tests/t_romfs_arc.c', 'docs/ROMFS_ARCHIVE.md',
        'dm/games/0100A00019DE0000/game.fork.tsv',
        # BC decode for GPUs without BC support (from d2r-mobile's bcdec line; tex_bc_cpu profiles)
        'dm/third_party/bcdec/bcdec.h', 'dm/third_party/bcdec/LICENSE',
        'dm/gfx/tex/nvn_bcdec.c', 'dm/gfx/tex/nvn_bcdec.h',
        'dm/gfx/tex/tests/t_bcdec.c',
        # The Mac front end's key and controller button map (seedds/nxnative#386, still open):
        # the launcher's Controls page writes DM_KEYMAP / DM_PAD_MAP, and patch_front_mac() makes
        # front_mac.m read them. Fork-only until #386 merges, so a sync must not delete them.
        'dm/host/input_map.h',
        'dm/host/tests/t_input_map.c',
        'dm/host/tests/t_launcher_contract.c',   # the write→read contract with the Controls page
        # The Increased Memory Limit guide the README links to; upstream has no such file, and the
        # first wholesale sync deleted it.
        'dm/ios/ENTITLEMENTS.md',
        # This script: fork-only, so a PR that changes it is not a vendored edit (pr-scope reads OURS)
        'scripts/sync_upstream.py', 'scripts/tests/test_sync_patchers.py',   # and its test (issue #92)
        # The NRO bound's tests (issue #14): fork-only, beside nso_code.py's lo bound
        'dm/tools/tests/test_nro_header.py',
        # The Files-app game-data pick (bookmark of the chosen folder): fork-only, upstream has no such path
        'dm/ios/Sources/DataBookmark.m', 'dm/ios/Sources/DataBookmark.h',
        # Switch 2 dumps (raw exefs/ and romfs/, no NSP): the packing script stages them. Fork-only.
        'scripts/switch2_build.py',
        'dm/tools/tests/test_switch2_build.py'}
# Vendored files that carry fork patches: taken fresh from upstream, then patched below.
PATCHED = ('dm/host/hle_fs.c', 'dm/host/Makefile', 'dm/host/tests/Makefile', 'dm/ios/tools/build_ipa.sh',
           'scripts/build_ipa_from_nsp.py',
           'dm/ios/Sources/main.m', 'dm/gfx/nvn/nvn_mtl.m', 'dm/host/dm.h',
           'dm/ios/tests/test_romfs_startup.py', 'dm/ios/tests/test_profile_pack.py', 'docs/env_switches.tsv', 'dm/host/front_mac.m', 'dm/tools/patch_nso.py', 'dm/games/0100A00019DE0000/game.tsv',
           # hand edits until issue #92, now patchers (see "Fork edits that had no protection" below)
           'dm/mac/launcher.c',
           'dm/host/hle_svc.c',
           'dm/host/hle_os.c',
           'dm/host/hle_fpslock.c',
           'dm/host/loader.c',
           'dm/ios/Sources/DmGamepad.m',
           'dm/ios/Sources/Launcher.swift',
           'dm/ios/project.yml',
           'dm/ios/tests/test_xcode_workflow.py',
           'dm/tools/env_switches.py',
           'dm/tools/tests/test_env_switches.py',
           'dm/tools/impact_map.tsv', 'dm/games/game.py',
           'dm/games/0100F2C0115B6000/game.tsv',
           'docs/agents/README.md',
           'docs/development.md')

def git(*a):
    r = subprocess.run(['git', *a], cwd=REPO, capture_output=True, text=True)
    if r.returncode: sys.exit(f'git {" ".join(a)} failed: {r.stderr.strip()}')
    return r.stdout

def blob(rev, path):
    r = subprocess.run(['git', 'cat-file', 'blob', f'{rev}:{path}'], cwd=REPO, capture_output=True)
    return r.stdout if r.returncode == 0 else None

def upstream_changes(old, new):
    """(kind, path) for every vendored file that differs, kind in AM D; plus an A for every vendored
    file upstream has that the fork's tree lacks, and an M for an upstream executable that lost its x bit here. The diff alone only sees files that changed between
    the two refs, so a file the first sync missed never arrived: docs/geometry-metal-research.md (which
    game notes link to) and 34 of docs/images, until test_docs.py found the dead links."""
    out = git('diff', '--name-status', old, new, '--', *VENDORED).splitlines()
    changes = []
    for line in out:
        parts = line.split('\t')
        kind, path = parts[0][0], parts[-1]
        changes.append((kind, path))
    named = {p for _, p in changes}
    for line in git('ls-tree', '-r', new, '--', *VENDORED).splitlines():
        meta, path = line.split('\t', 1)
        if path in named or path in OURS: continue
        dest = REPO / path
        if not dest.exists(): changes.append(('A', path))
        elif meta.split()[0] == '100755' and not os.access(dest, os.X_OK): changes.append(('M', path))   # an executable that lost its x bit
    return changes

# ---- the fork's patches. Each is (name, already-marker, [(old, new), ...]); a patch whose
# marker is present is skipped, one whose old text is absent fails the run. ----

HLE = 'dm/host/hle_fs.c'
def patch_hle_fs(t):
    P = []
    P.append(('include', '#include "romfs_arc.h"', [(
        '#include "hle.h"\n#include <mach/mach.h>\n#include "front.h"\n',
        '#include "hle.h"\n#include <mach/mach.h>\n#include "front.h"\n#include "romfs_arc.h"\n')]))
    P.append(('synthetic path', 'ROMFSARC_SYNTH_PREFIX "%s"', [(
        '        if (g_mnt[i].overlay[0] && !create) return rom_overlay_path(&g_mnt[i], rest, out, n);',
        '''        if (g_mnt[i].overlay[0] && !create) return rom_overlay_path(&g_mnt[i], rest, out, n);
        // Archive mount: the RomFS tree lives inside a ROMFSARC archive, so the host path is a
        // synthetic "@romfsarc:<guest path>" that the file calls below route to romfs_arc.c. Matched
        // by the mount directory (romfsarc_open recorded the base it actually opened), because the
        // mount name differs between games (rom, content).
        if (romfsarc_active() && g_mnt[i].ro && !create && g_mnt[i].dir[0] && !strcmp(g_mnt[i].dir, g_arc_base)) {
            // Fold runs of '/' the way the directory walk below does: the archive index stores one
            // separator per level, and a game may ask for "data:/bin/appli/icon_sick//bin/x.arc"
            // (Pokemon Sword's Bag: the doubled slash missed the lookup and the game then
            // dereferenced the null it got back — PR #9).
            char folded[1024]; const char *r = rest;
            if (strstr(rest, "//")) {
                size_t o = 0; int slash = 0;
                for (const char *q = rest; *q; q++) {
                    if (*q == '/') { if (slash) continue; slash = 1; } else slash = 0;
                    if (o + 1 >= sizeof folded) { errno = ENAMETOOLONG; return 0; }
                    folded[o++] = *q;
                }
                folded[o] = 0; r = folded;
            }
            if (snprintf(out, n, ROMFSARC_SYNTH_PREFIX "%s", r) >= (int)n) { errno = ENAMETOOLONG; return 0; }
            return 1;
        }''')]))
    P.append(('MountRom', 'romfsarc_open(dir, &h)', [(
        '''HLE_FORCE("_ZN2nn2fs8MountRomEPKcPvm", "fsp-srv; rom → host RomFS directory", Result, h_fs_MountRom, const char *name, void *cache, size_t sz) {
    (void)cache; (void)sz; char b[1024];
    Result r = mount(name, envdir("DM_ROMFS", "romfs", b, sizeof b), 1);''',
        '''HLE_FORCE("_ZN2nn2fs8MountRomEPKcPvm", "fsp-srv; rom → host RomFS directory (or a ROMFSARC archive)", Result, h_fs_MountRom, const char *name, void *cache, size_t sz) {
    (void)cache; (void)sz; char b[1024];
    const char *dir = envdir("DM_ROMFS", "romfs", b, sizeof b);
    // Archive mount: romfsarc_open tries <dir>.idx, <dir>/romfs.idx and <dir>/romfs/romfs.idx, so the
    // player may copy the shards either beside the folder or into Documents/romfs/ itself.
    romfsarc_hdr h;
    if (romfsarc_open(dir, &h) == 0) {
        // Mount the resolved archive base (romfsarc_open recorded the one that worked in g_arc_base).
        // host_path_ex compares the mount directory against it.
        Result r = mount(name, g_arc_base, 1);
        if (r != RES_OK) { romfsarc_close(); return r; }
        LOG("fs rom: archive %s (%llu files, %llu dirs, %u shards, tree hash %016llx)", g_arc_base,
            (unsigned long long)h.file_count, (unsigned long long)h.dir_count, h.shard_count, (unsigned long long)h.tree_hash);
        return RES_OK;
    }
    Result r = mount(name, dir, 1);''')]))
    P.append(('archive handles', 'static uint64_t arc_new', [(
        'HLE_FORCE("_ZN2nn2fs8OpenFileEPNS0_10FileHandleEPKci", "fsp-srv → open(2)", Result, h_fs_OpenFile, uint64_t *out, const char *path, int mode) {',
        '''// ---------- Archive-backed file handles ----------
// An archive file has no host fd. It gets a handle number from a private table, and every operation
// on it (ReadFile, GetFileSize, CloseFile) checks the table first. Handle numbers are distinct from
// fds: real handles are fds (>= 3 in practice), archive handles are tagged by index into g_arc[].
typedef struct { uint32_t shard; uint64_t off, size; int used; } archandle;
#define MAX_ARCH 4096
static archandle g_arc[MAX_ARCH];
static uint64_t g_arch_gen;   // bumped on open/close so a stale handle never hits a recycled slot
static uint64_t arc_new(uint32_t shard, uint64_t off, uint64_t size) {
    for (int i = 0; i < MAX_ARCH; i++) if (!g_arc[i].used) {
        g_arc[i].used = 1; g_arc[i].shard = shard; g_arc[i].off = off; g_arc[i].size = size;
        return ((uint64_t)1 << 63) | ((g_arch_gen & 0x7fffffff) << 32) | (uint32_t)i;
    }
    return 0;
}
static int arc_get(uint64_t h, archandle **out) {
    if (!(h >> 63)) return 0;
    uint32_t i = (uint32_t)h;
    if (i >= MAX_ARCH || !g_arc[i].used) return 0;
    if ((uint32_t)((h >> 32) & 0x7fffffff) != (uint32_t)(g_arch_gen & 0x7fffffff)) return 0;
    *out = &g_arc[i]; return 1;
}
static void arc_free(uint64_t h) {
    archandle *a; if (arc_get(h, &a)) a->used = 0;
}
static const char *arc_rel(const char *hp) {
    const char *rel = ROMFSARC_SYNTH_REL(hp);
    while (*rel == '/') rel++;
    return rel;
}
static int arc_file(const char *hp, uint32_t *shard, uint64_t *off, uint64_t *size) {
    return romfsarc_stat_file(arc_rel(hp), shard, off, size);
}
static int arc_dir(const char *hp) { return romfsarc_is_dir(arc_rel(hp)); }
static void arc_norm(const char *hp, char *rel, size_t n) { strlcpy(rel, arc_rel(hp), n); }
HLE_FORCE("_ZN2nn2fs8OpenFileEPNS0_10FileHandleEPKci", "fsp-srv → open(2)", Result, h_fs_OpenFile, uint64_t *out, const char *path, int mode) {''')]))
    P.append(('OpenFile', 'arc_new(shard, off, size)', [(
        '''    HP(path);
    if (mode & (MODE_WRITE | MODE_APPEND)) RW();
    int fd = open(hp, (mode & (MODE_WRITE | MODE_APPEND)) ? ((mode & MODE_READ) ? O_RDWR : O_WRONLY) : O_RDONLY);''',
        '''    HP(path);
    if (mode & (MODE_WRITE | MODE_APPEND)) RW();
    if (ROMFSARC_IS_SYNTH(hp)) {   // archive entry: no fd, the handle carries shard/offset/size
        uint32_t shard; uint64_t off, size;
        if (!arc_file(hp, &shard, &off, &size)) { errno = ENOENT; return err_res("open", hp); }
        uint64_t ah = arc_new(shard, off, size);
        if (!ah) { errno = EMFILE; return err_res("open", hp); }
        *out = ah;
        if (g_flag.fs) LOG("fs open %s mode=%d → archive handle (shard %u off %llu size %llu)", path, mode, shard, (unsigned long long)off, (unsigned long long)size);
        return RES_OK;
    }
    int fd = open(hp, (mode & (MODE_WRITE | MODE_APPEND)) ? ((mode & MODE_READ) ? O_RDWR : O_WRONLY) : O_RDONLY);''')]))
    P.append(('ReadFile', 'romfsarc_pread(ah->shard', [(
        '''    size_t got = 0; int reprep = 0, fd = (int)h; int64_t base = 0;
    if (IMG_H(h)) {   // an image entry: read the image's descriptor at the entry's offset, never past its end
        const img_ent *x = &g_mnt[IMG_M(h)].img->e[IMG_K(h)];
        fd = g_mnt[IMG_M(h)].img->fd; base = (int64_t)x->a;
        sz = off < 0 || (uint64_t)off >= x->b ? 0 : (size_t)(x->b - off) < sz ? (size_t)(x->b - off) : sz;
    }
    dm_guest_write_prepare(buf, sz);   // The destination may be a texture pool page write-protected by the graphics layer: a kernel write to a read-only page returns EFAULT instead of page-faulting, so unprotect and mark dirty first
    while (got < sz) {''',
        '''    size_t got = 0; int reprep = 0, fd = (int)h; int64_t base = 0;
    if (IMG_H(h)) {   // an image entry: read the image's descriptor at the entry's offset, never past its end
        const img_ent *x = &g_mnt[IMG_M(h)].img->e[IMG_K(h)];
        fd = g_mnt[IMG_M(h)].img->fd; base = (int64_t)x->a;
        sz = off < 0 || (uint64_t)off >= x->b ? 0 : (size_t)(x->b - off) < sz ? (size_t)(x->b - off) : sz;
    }
    dm_guest_write_prepare(buf, sz);   // The destination may be a texture pool page write-protected by the graphics layer: a kernel write to a read-only page returns EFAULT instead of page-faulting, so unprotect and mark dirty first
    archandle *ah;
    if (arc_get(h, &ah)) {   // archive entry: clamp to the file, read from its shard
        if (off < 0) { *out = 0; return RES_FAIL; }
        uint64_t avail = (uint64_t)off < ah->size ? ah->size - (uint64_t)off : 0;
        size_t want = sz < avail ? sz : (size_t)avail;
        if (want) { ssize_t r = romfsarc_pread(ah->shard, ah->off + (uint64_t)off, buf, want);
            if (r < 0) return err_res("pread (archive)", "handle"); got = (size_t)r; }
        *out = got;
        if (g_flag.fs) { static uint64_t n, bytes, next_b = 64 << 20; static pthread_mutex_t m = PTHREAD_MUTEX_INITIALIZER;
            pthread_mutex_lock(&m); n++; bytes += got;
            if (bytes >= next_b || n % 1000 == 0) { LOG("fs read total: %llu reads %.1f MiB (this one: archive offset %lld length %#zx)", n, bytes / 1048576.0, (long long)off, got); if (bytes >= next_b) next_b += 64 << 20; }
            pthread_mutex_unlock(&m); }
        return RES_OK;
    }
    while (got < sz) {''')]))
    P.append(('GetFileSize', 'ah->size; return RES_OK', [(
        '''HLE_FORCE("_ZN2nn2fs11GetFileSizeEPlNS0_10FileHandleE", "fsp-srv → fstat(2)", Result, h_fs_GetFileSize, int64_t *out, uint64_t h) {
    if (!h) return RES_FAIL;''',
        '''HLE_FORCE("_ZN2nn2fs11GetFileSizeEPlNS0_10FileHandleE", "fsp-srv → fstat(2)", Result, h_fs_GetFileSize, int64_t *out, uint64_t h) {
    if (!h) return RES_FAIL;
    archandle *ah; if (arc_get(h, &ah)) { *out = (int64_t)ah->size; return RES_OK; }''')]))
    P.append(('CloseFile', 'arc_free(h)', [(
        '''HLE_FORCE("_ZN2nn2fs9CloseFileENS0_10FileHandleE", "fsp-srv → close(2)", void, h_fs_CloseFile, uint64_t h) {
    if (!h || IMG_H(h)) return;   // the null handle after a failed open (see ReadFile): do not touch fd 0; an image entry holds no descriptor''',
        '''HLE_FORCE("_ZN2nn2fs9CloseFileENS0_10FileHandleE", "fsp-srv → close(2)", void, h_fs_CloseFile, uint64_t h) {
    if (!h || IMG_H(h)) return;   // the null handle after a failed open (see ReadFile): do not touch fd 0; an image entry holds no descriptor
    if (h >> 63) { arc_free(h); return; }   // archive entry: nothing to close''')]))
    P.append(('GetEntryType', 'romfsarc_stat_file(rel', [(
        '''HLE_FORCE("_ZN2nn2fs12GetEntryTypeEPNS0_18DirectoryEntryTypeEPKc", "fsp-srv → stat(2)", Result, h_fs_GetEntryType, int32_t *out, const char *path) {
    int mi; int64_t k;
    if (img_path(path, &mi, &k)) { if (k < 0) return err_res("stat", path); *out = g_mnt[mi].img->e[k].type ? ENT_FILE : ENT_DIR; return RES_OK; }
    HP(path); struct stat st;''',
        '''HLE_FORCE("_ZN2nn2fs12GetEntryTypeEPNS0_18DirectoryEntryTypeEPKc", "fsp-srv → stat(2)", Result, h_fs_GetEntryType, int32_t *out, const char *path) {
    int mi; int64_t k;
    if (img_path(path, &mi, &k)) { if (k < 0) return err_res("stat", path); *out = g_mnt[mi].img->e[k].type ? ENT_FILE : ENT_DIR; return RES_OK; }
    HP(path); struct stat st;
    if (ROMFSARC_IS_SYNTH(hp)) {   // archive entry or directory, from the index
        const char *rel = arc_rel(hp);   // the index stores paths without a leading slash (abe35a3)
        uint32_t sh; uint64_t of, szf;
        if (romfsarc_stat_file(rel, &sh, &of, &szf)) { *out = ENT_FILE; return RES_OK; }
        if (romfsarc_is_dir(rel)) { *out = ENT_DIR; return RES_OK; }
        errno = ENOENT; return err_res("stat", hp);
    }''')]))
    # Runtime profile: the save-exists check compares the program id against dm_profile()'s (the
    # compiled macro is what dm_profile() falls back to; merged with the stage-2 experiment).
    P.append(('save-exists profile', 'dm_profile()->program_id', [(
        # both occurrences on the line: the log message and the return value
        'app == DM_GAME_PROGRAM_ID ? "this game, yes" : "another program, no"); return app == DM_GAME_PROGRAM_ID;',
        'app == dm_profile()->program_id ? "this game, yes" : "another program, no"); '
        'return app == dm_profile()->program_id;')]))
    P.append(('snapshot helper', 'static void arc_snapshot', []))   # inserted below, needs dirh_t
    P.append(('OpenDirectory', 'arc_snapshot(ah, hp)', [(
        '''    HP(path); DIR *d = opendir(hp);
    if (!d) return err_res("opendir", hp);''',
        '''    HP(path);
    if (ROMFSARC_IS_SYNTH(hp)) {   // archive directory: no host DIR, only the snapshot
        if (!arc_dir(hp)) { errno = ENOTDIR; return err_res("opendir", hp); }
        dirh_t *ah = calloc(1, sizeof *ah); if (!ah) die("directory handle allocation failed");
        strlcpy(ah->path, hp, sizeof ah->path); ah->mode = mode;
        arc_snapshot(ah, hp);
        *out = (uint64_t)ah; return RES_OK;
    }
    DIR *d = opendir(hp);
    if (!d) return err_res("opendir", hp);''')]))
    P.append(('ReadDirectory guard', "an archive directory that turned out empty", [(
        '''    int dfd = dirfd(h->d);''',
        '''    if (!h->d) { *count = 0; return RES_OK; }   // an archive directory that turned out empty (no snapshot, no host DIR)
    int dfd = dirfd(h->d);''')]))
    P.append(('CloseDirectory guard', 'if (h->d) closedir(h->d)', [(
        '''    dirh_t *h = (dirh_t *)hh; closedir(h->d); if (h->od) closedir(h->od); free(h->snap); free(h);''',
        '''    dirh_t *h = (dirh_t *)hh; if (h->d) closedir(h->d); if (h->od) closedir(h->od); free(h->snap); free(h);''')]))

    for name, marker, pairs in P:
        if marker in t:
            skip(f'hle_fs.c {name} (already patched)'); continue
        for old, new in pairs:
            if old not in t: fail(f'{HLE}: patch "{name}" anchor missing: {old[:70]!r}')
            t = t.replace(old, new, 1)
        done(f'hle_fs.c {name}')
    # arc_snapshot goes right after the dirh_t typedef: it names the type
    if 'static void arc_snapshot' not in t:
        import re
        m = re.search(r'typedef struct \{ DIR \*d;.*?\} dirh_t;\n', t, re.S)
        if not m: fail(f'{HLE}: dirh_t typedef not found')
        t = t[:m.end()] + SNAPSHOT + t[m.end():]
        done('hle_fs.c arc_snapshot')
    return t

SNAPSHOT = '''
// An archive directory has no host DIR: its entries come from the index, copied into the same
// snapshot list the overlay mount uses, so ReadDirectory has one path for both.
static void arc_snapshot(dirh_t *h, const char *hp) {
    char rel[1024]; arc_norm(hp, rel, sizeof rel);
    int n = romfsarc_count(rel);
    if (n <= 0) return;
    romfsarc_ent *ents = malloc((size_t)n * sizeof *ents);
    if (!ents) die("archive directory snapshot allocation failed");
    int got = romfsarc_list(rel, ents, n);
    if (got <= 0) { free(ents); return; }
    h->snap = calloc((size_t)got, sizeof *h->snap); h->nsnap = got;
    if (!h->snap) die("archive directory snapshot allocation failed");
    for (int i = 0; i < got; i++) {
        strlcpy(h->snap[i].name, ents[i].name, sizeof h->snap[i].name);
        h->snap[i].type = ents[i].is_dir ? ENT_DIR : ENT_FILE;
        h->snap[i].size = ents[i].is_dir ? 0 : ents[i].size;
    }
    free(ents);
}
'''

def patch_makefile(t):
    if 'romfs_arc.c' in t: return skip('Makefile (already patched)')
    # Anchor on the stable SRC prefix: upstream adds and reorders files after hle.c (haptics_apple.m)
    old = 'SRC=main.c loader.c hle.c '
    if old not in t: fail('dm/host/Makefile: SRC anchor missing (upstream changed the source list)')
    return done('Makefile SRC', t.replace(old, 'SRC=main.c loader.c hle.c romfs_arc.c ', 1))

TESTS_MK = 'dm/host/tests/Makefile'
def patch_tests_makefile(t):
    """The three fs tests #include ../hle_fs.c, which now calls the archive reader: without
    romfs_arc.c on their command lines they stop with undefined _romfsarc_* symbols (PR #12).

    Three independent additions follow (the fs link lines, the touch-pad test, the key map
    test), each with its own "already there" test. This used to start with
    `if 'romfs_arc.c' in t: return skip(...)`, which made every later addition dead code: the
    touch-pad block never ran once the fs lines were in, and the marker check could not notice
    its absence. Only the first block skips now.
    """
    # Upstream spells the compiler `clang` (until ce99604a) or `$(CLANG)` (a variable that adds the
    # sanitizer flags; issue #92). Anchors and inserted rules use whichever the file uses, so the
    # patcher fits the tree before and after the sync that brings the variable.
    CL = r'(?:\$\(CLANG\)|clang)'
    cc = '$(CLANG)' if '$(CLANG)' in t else 'clang'
    if 'romfs_arc.c' not in t:
        P = [
            ('t_fs', 'clang -O1 -Wno-unused-function -I.. $(GINC) -o $@ t_fs.c t_fs_stub.c ../hle_bigstack.c\n',
             'clang -O1 -Wno-unused-function -I.. $(GINC) -o $@ t_fs.c t_fs_stub.c ../hle_bigstack.c ../romfs_arc.c\n'),
            ('t_fs_commit_list', 'clang -O1 -Wall -Wno-unused-function -I.. $(GINC) -o $@ t_fs_commit_list.c t_fs_stub.c ../hle_bigstack.c\n',
             'clang -O1 -Wall -Wno-unused-function -I.. $(GINC) -o $@ t_fs_commit_list.c t_fs_stub.c ../hle_bigstack.c ../romfs_arc.c\n'),
            ('t_fs_case', 'clang -O1 -Wno-unused-function -I.. $(GINC) -o $@ t_fs_case.c ../hle_bigstack.c\n',
             'clang -O1 -Wno-unused-function -I.. $(GINC) -o $@ t_fs_case.c ../hle_bigstack.c ../romfs_arc.c\n'),
        ]
        for name, old, new in P:
            old = old.replace('clang', cc); new = new.replace('clang', cc)
            if old not in t: fail(f'{TESTS_MK}: {name} link line missing (upstream changed the test rules)')
            t = t.replace(old, new, 1)
        done('tests/Makefile romfs_arc.c')
    # the touch-pad geometry test (dm/host/touchpad_geometry.h): header-only, so it needs its own
    # rule and a place in the run list
    if 't_touchpad_geometry' not in t:
        run_old = '$(B)/t_haptics $(B)/t_haptics_apple\n'
        if run_old not in t: fail(f'{TESTS_MK}: run list anchor missing')
        t = t.replace(run_old, '$(B)/t_haptics $(B)/t_haptics_apple $(B)/t_touchpad_geometry\n', 1)
        body_old = '\t$(B)/t_haptics_apple\n'
        if body_old not in t: fail(f'{TESTS_MK}: run body anchor missing')
        t = t.replace(body_old, body_old + '\t$(B)/t_touchpad_geometry\n', 1)
        rule_anchor = '$(B)/t_haptics_apple: t_haptics_apple.m'
        if rule_anchor not in t: fail(f'{TESTS_MK}: t_haptics_apple rule missing')
        t = t.replace(rule_anchor,
                      '$(B)/t_touchpad_geometry: t_touchpad_geometry.c ../touchpad_geometry.h ../dm_input.h | $(B)\n'
                      '\t' + cc + ' -O1 -Wall -I.. $(GINC) -o $@ t_touchpad_geometry.c\n' + rule_anchor, 1)
        done('tests/Makefile touchpad test')
    # the key and controller map test (dm/host/input_map.h, seedds/nxnative#386): header-only too
    if 't_input_map' not in t:
        run_old = '$(B)/t_hid $(B)/t_haptics $(B)/t_haptics_apple'
        if run_old not in t: fail(f'{TESTS_MK}: run list anchor for t_hid missing')
        t = t.replace(run_old, '$(B)/t_hid $(B)/t_input_map $(B)/t_haptics $(B)/t_haptics_apple', 1)
        body_old = '\t$(B)/t_hid\n'
        if body_old not in t: fail(f'{TESTS_MK}: run body anchor for t_hid missing')
        t = t.replace(body_old, body_old + '\t$(B)/t_input_map\n', 1)
        rule_anchor = '$(B)/t_hid: t_hid.c'
        if rule_anchor not in t: fail(f'{TESTS_MK}: t_hid rule missing')
        t = t.replace(rule_anchor,
                      '$(B)/t_input_map: t_input_map.c ../input_map.h ../dm_input.h | $(B)\n'
                      '\t' + cc + ' -O1 -Wall -Werror -Wno-unused-function -I.. -o $@ t_input_map.c\n'
                      '$(B)/t_launcher_contract: t_launcher_contract.c ../input_map.h ../dm_input.h | $(B)\n'
                      '\t' + cc + ' -O1 -Wall -Werror -Wno-unused-function -I.. -o $@ t_launcher_contract.c\n' + rule_anchor, 1)
        body_old2 = '\t$(B)/t_input_map\n'
        t = t.replace(body_old2, body_old2 + '\t$(B)/t_launcher_contract\n', 1)
        run2 = '$(B)/t_input_map $(B)/t_haptics'
        if run2 in t: t = t.replace(run2, '$(B)/t_input_map $(B)/t_launcher_contract $(B)/t_haptics', 1)
        done('tests/Makefile input map test')
    # the prerequisites too, so a change to the reader rebuilds them — and hle_profile.h, so a
    # change to the runtime profile does
    for name in ('t_fs', 't_fs_commit_list', 't_fs_case'):
        dep_old = f'$(B)/{name}:'
        i = t.index(dep_old)
        j = t.index('\n', i)
        line = t[i:j]
        if '../romfs_arc.c' not in line:
            line = line.replace(' ../hle.h', ' ../romfs_arc.c ../hle.h', 1)
        if '../hle_profile.h' not in line:
            line = line.replace(' | $(B)', ' ../hle_profile.c ../hle_profile.h | $(B)', 1)
        t = t[:i] + line + t[j:]
    # hle_profile.c: every test whose prerequisites reach a dm_profile() caller needs the accessor
    # at link time (the runtime-profile merge added those calls; the sync replaces this file, so the
    # rule has to live here or the host tests stop linking after every sync).
    USERS = ('../hle_svc.c', '../hle_thread.c', '../hle_fs.c', '../loader.c',
             '../hle_fpslock.c', '../hle_oe.c', '../hle_os.c', '../hle_sdk16.c')
    lines = t.splitlines(keepends=True)
    out, need = [], False
    for ln in lines:
        if ln.startswith('$(B)/'):
            # a rule header: does its prerequisite list reach a dm_profile() caller?
            need = any(u in ln for u in USERS)
            out.append(ln)
            continue
        if need:
            # the recipe line of that rule: append the accessor's object if absent
            if ln.startswith('\t') and re.match(r'\s*' + CL, ln) and 'hle_profile.c' not in ln:
                ln = ln.rstrip('\n') + ' ../hle_profile.c\n'
            elif not ln.startswith('\t') and ln.strip():
                need = False          # a non-recipe line ends the rule
        out.append(ln)
    t = ''.join(out)
    # the memory block compiles ../hle_svc.c from MEM_DEPS
    mem_old = 'MEM_DEPS = t_svc_memory.c memory_profile/game_profile.h ../hle_svc.c ../hle_guestthread.c ../hle.h ../dm.h'
    if mem_old in t and 'hle_profile.c' not in t.split(mem_old)[1].split('\n')[0]:
        t = t.replace(mem_old, mem_old + ' ../hle_profile.c ../hle_profile.h', 1)
        t = re.sub(r'(\t' + CL + r' \$\(MEM_FLAGS\)[^\n]*?\.c)(\n)',
                   lambda m: m.group(1) + ' ../hle_profile.c' + m.group(2), t)
    return done('tests/Makefile romfs_arc.c + hle_profile.c', t)

ENT = '''# The unsigned IPA: an unsigned binary has no signature for a sideloader to read entitlements from,
# so the request would be lost (AltStore reads the signature it replaces). Sign ad-hoc with the
# entitlement first; the sideloader strips this signature and carries the entitlement over.
if [ "$PREPARE_XCODE" != YES ] && [ -n "${DM_ENTITLEMENTS-default}" ]; then
  if [ -n "${DM_ENTITLEMENTS:-}" ]; then cp "$DM_ENTITLEMENTS" "$APP/DmIOS.entitlements" || die "copy DM_ENTITLEMENTS"
  else printf '%s\\n' '<?xml version="1.0" encoding="UTF-8"?>' '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">' \\
    '<plist version="1.0"><dict><key>com.apple.developer.kernel.increased-memory-limit</key><true/></dict></plist>' > "$APP/DmIOS.entitlements" || die "entitlements"; fi
  # codesign refuses a bundle whose files carry resource forks, Finder information, quarantine or
  # provenance attributes ("resource fork, Finder information, or similar detritus not allowed");
  # files copied into the bundle (the icon, resources staged from a Downloads folder) can bring them
  xattr -cr "$APP" 2>/dev/null
  codesign --force --sign - --entitlements "$APP/DmIOS.entitlements" "$APP" || die "ad-hoc sign with entitlements (the sideloader re-signs; this only carries the request)"
  codesign -d --entitlements :- "$APP" 2>/dev/null | grep -q increased-memory-limit || die "entitlements did not make it into the placeholder signature"
  t "entitlements: increased-memory-limit embedded in the placeholder signature"
fi
'''

def patch_build_ipa(t):
    if 'placeholder signature' in t: return skip('build_ipa.sh entitlements (already patched)')
    old = '''APP=$DD/Build/Products/Release-iphoneos/DmIOS.app
[ -n "$APP" ] && [ -d "$APP" ] || die "no built app"; t "app: $APP"
'''
    if old not in t: fail('dm/ios/tools/build_ipa.sh: APP anchor missing (upstream moved the app definition)')
    t = t.replace(old, old + ENT, 1)
    # The RomFS mapping moved into the `P=…; MAPS=(…)` assignment (upstream folded it in when it
    # added the pso/ mapping), so the anchor is the assignment plus the line that follows the
    # whole block — not the two adjacent lines the old form had. Both spellings are accepted: a
    # checkout from before that change still syncs.
    slim = '''# DM_FULL_ROMFS=1 packs the RomFS into the package (upstream behaviour; only sensible for small
# games — a full package of a large game is many GB and iOS refuses to install it with
# PackageExtractionFailed). The default is the slim package: the app reads the game files from
# Documents/romfs (a directory tree, or a ROMFSARC archive: romfs.idx + romfs.NN shards).'''
    # Upstream picks the mapping form by what the RomFS path is (a .img file or a folder)
    old2img = '''if [ -f "$ROMFS" ]; then MAPS=("$P/romfs.img=$ROMFS"); else MAPS=("$P/romfs=$ROMFS"); fi'''
    if old2img in t:
        t = t.replace(old2img, '''MAPS=()
# DM_FULL_ROMFS=1 packs the game files into the package (upstream behaviour; only sensible for small
# games — a full package of a large game is many GB and iOS refuses to install it with
# PackageExtractionFailed). The default is the slim package: the app reads them from
# Documents/games/<id>/ (romfs.img and aoc/<index>.img), which the launcher copies in one go and
# which can also live on an external drive.
if [ -n "${DM_FULL_ROMFS:-}" ]; then if [ -f "$ROMFS" ]; then MAPS=("$P/romfs.img=$ROMFS"); else MAPS=("$P/romfs=$ROMFS"); fi; fi''', 1)
        # Add-on content follows the same rule: with the slim default it goes to
        # Documents/games/<id>/aoc/<index>.img (the host already mounts *.img there, hle_fs.c
        # aoc_dir), so the IPA stays small and the game data is one folder the user copies.
        old_aoc = '''if [ -n "$AOC" ] && [ -d "$AOC" ]; then MAPS+=("$P/aoc=$AOC"); t "add-on content: $(ls "$AOC" | tr '\\n' ' ')"; fi'''
        if old_aoc in t and 'if [ -n "${DM_FULL_ROMFS:-}" ] && [ -n "$AOC" ]' not in t:
            t = t.replace(old_aoc, '''if [ -n "${DM_FULL_ROMFS:-}" ] && [ -n "$AOC" ] && [ -d "$AOC" ]; then
  MAPS+=("$P/aoc=$AOC"); t "add-on content packed into the IPA: $(ls "$AOC" | tr '\\n' ' ')"
elif [ -n "$AOC" ] && [ -d "$AOC" ]; then
  t "add-on content alongside the game data (DM_FULL_ROMFS=1 packs it): $(ls "$AOC" | tr '\\n' ' ')"
fi''', 1)
        old3 = '''t "pack IPA (already-compressed game files stored, everything else deflated)"'''
        if old3 not in t: fail('dm/ios/tools/build_ipa.sh: pack line missing after the MAPS assignment')
        t = t.replace(old3, '''if [ -n "${DM_FULL_ROMFS:-}" ]; then
  t "pack full IPA with RomFS (already-compressed game files stored, everything else deflated)"
else
  t "pack slim IPA without RomFS (game files come from Documents/romfs; DM_FULL_ROMFS=1 packs them)"
fi''', 1)
        return done('build_ipa.sh slim + entitlements', t)
    old2 = '''P=Payload/DmIOS.app; MAPS=("$P/romfs=$ROMFS")'''
    if old2 in t:
        # current upstream: drop the RomFS from the initial assignment, add it back inside the
        # DM_FULL_ROMFS branch, and leave every other mapping (aoc, pso, msl, app) untouched
        t = t.replace(old2, '''P=Payload/DmIOS.app; MAPS=()
# DM_FULL_ROMFS=1 packs the RomFS into the package (upstream behaviour; only sensible for small
# games — a full package of a large game is many GB and iOS refuses to install it with
# PackageExtractionFailed). The default is the slim package: the app reads the game files from
# Documents/romfs (a directory tree, or a ROMFSARC archive: romfs.idx + romfs.NN shards).
if [ -n "${DM_FULL_ROMFS:-}" ]; then MAPS=("$P/romfs=$ROMFS"); fi''', 1)
        old3 = '''t "pack IPA (already-compressed game files stored, everything else deflated)"'''
        if old3 not in t: fail('dm/ios/tools/build_ipa.sh: pack line missing after the MAPS assignment')
        t = t.replace(old3, '''if [ -n "${DM_FULL_ROMFS:-}" ]; then
  t "pack full IPA with RomFS (already-compressed game files stored, everything else deflated)"
else
  t "pack slim IPA without RomFS (game files come from Documents/romfs; DM_FULL_ROMFS=1 packs them)"
fi''', 1)
        return done('build_ipa.sh slim + entitlements', t)
    # older upstream: the RomFS mapping was its own statement right before the pack line
    old4 = '''MAPS+=("$P/romfs=$ROMFS")
t "pack IPA (already-compressed game files stored, everything else deflated)"'''
    if old4 not in t: fail('dm/ios/tools/build_ipa.sh: pack anchor missing (neither the folded '
                           'MAPS assignment nor the separate RomFS line is present)')
    return done('build_ipa.sh slim + entitlements', t.replace(old4, '''# DM_FULL_ROMFS=1 packs the RomFS into the package (upstream behaviour; only sensible for small
# games — a full package of a large game is many GB and iOS refuses to install it with
# PackageExtractionFailed). The default is the slim package: the app reads the game files from
# Documents/romfs (a directory tree, or a ROMFSARC archive: romfs.idx + romfs.NN shards).
if [ -n "${DM_FULL_ROMFS:-}" ]; then
  MAPS+=("$P/romfs=$ROMFS")
  t "pack full IPA with RomFS (already-compressed game files stored, everything else deflated)"
else
  t "pack slim IPA without RomFS (game files come from Documents/romfs; DM_FULL_ROMFS=1 packs them)"
fi''', 1))

def patch_nsp(t):
    P = []
    P.append(('work-dir arg', '--work-dir', [(
        "    ap.add_argument('--env-file', help='environment switches for a diagnostic build (one KEY=VALUE per line)')",
        "    ap.add_argument('--work-dir', help='where the build folder goes (default: <NSP folder>/<title ID>_build); the extracted files, shader cache and temporary files all live there')\n"
        "    ap.add_argument('--env-file', help='environment switches for a diagnostic build (one KEY=VALUE per line)')")]))
    # Two separate one-line anchors: upstream inserts lines between them whenever it adds a check
    # (ffcd910 put a mac_pack check right above `work`), which broke the old three-line anchor and
    # stopped the daily sync for two days (issue #13). `work` and that out_ipa line each appear once.
    # `out_ipa` reads `work`, so `work` must be assigned first: patched separately they once landed
    # in the wrong order (PR #15 — an UnboundLocalError on every build without -o). One replacement
    # per shape of upstream's block keeps them in order:
    #   - before upstream #278: one out_ipa line, the two checks, then `work`;
    #   - after #278: out_ipa under `if want_ios:` (8 spaces) plus an out_app block, `work` still last.
    # Each shape is ONE (old, new) pair rather than a delete plus an insert: a two-step edit that
    # removes `work` and puts it back leaves the tree without the assignment if the second step ever
    # stops matching.
    work_line = "    work = Path(os.path.expanduser(args.work_dir)).resolve() if args.work_dir else nsp_dir / f'{g.id}_build'   # the folder holding the NSPs by default; --work-dir puts the build elsewhere"
    oipa_old = "out_ipa = Path(os.path.expanduser(args.out or OUT_IPA)).resolve() if (args.out or OUT_IPA) else nsp_dir / g.get('ipa')   # by default in the NSP folder, next to the NSPs and <id>_build/"
    oipa_new = "out_ipa = Path(os.path.expanduser(args.out or OUT_IPA)).resolve() if (args.out or OUT_IPA) else work.parent / g.get('ipa')   # by default next to the NSPs and <id>_build/"
    shadowed_line = "    if shadowed and not (args.out or OUT_IPA): out_ipa = out_ipa.with_name(out_ipa.stem + '-provisional.ipa')   # never over the IPA of the game's real profile"
    macpack_line = "    if mac_pack and (args.out or OUT_IPA) and out_ipa.suffix != '.app': raise BuildError(f'the Mac app (-o / OUT_IPA) must end in .app: {out_ipa}')"
    if "\n    out_ipa = None\n    if want_ios:\n" in t:   # upstream #278's shape
        P.append(('work-dir paths', 'args.work_dir', [(
            "    out_ipa = None\n    if want_ios:\n        " + oipa_old + "\n",
            work_line + "\n    out_ipa = None\n    if want_ios:\n        " + oipa_new + "\n")]))
    else:
        P.append(('work-dir paths', 'args.work_dir', [(
            "    " + oipa_old + "\n" + shadowed_line + "\n" + macpack_line + "\n    work = nsp_dir / f'{g.id}_build'",
            work_line + "\n"
            "    # out_ipa reads `work`, so `work` must be assigned first (upstream's order had them the other\n"
            "    # way round; the --work-dir patch moved `work` up here and this line had to follow it).\n"
            "    " + oipa_new + "\n" + shadowed_line + "\n" + macpack_line)]))
    # The shader translator (dmsh_xlate_bin/dmsh_xlate) is a framework-dependent apphost, and a
    # set-but-wrong DOTNET_ROOT is worse than none: the apphost stops searching once the variable is
    # there, so a stale root from the environment kills the shader step while `dotnet` on PATH keeps
    # working — nx_extract builds fine, then dmsh_build refuses with "'xlate_source: lock … / actual' empty".
    # Upstream's configure_dotnet() (called right before this) derives the root from `dotnet
    # --list-runtimes`; what it does not do is check that the translator runs with it. This runs the
    # translator, and when it answers nothing tries candidate roots and keeps the first that works;
    # with none verified the variable is dropped, so the apphost searches its default locations.
    # The marker is `xlate_answers`, a name only this code has: the earlier marker, `list-runtimes`,
    # became upstream's own text (configure_dotnet) and silently switched this patch off at the sync
    # to 75065df (issue #92). The first half of the old patch (parse the root from --list-runtimes)
    # is upstream's now and is not carried any more.
    P.append(('DOTNET_ROOT verification', 'xlate_answers', [(
        "    configure_dotnet(env)\n",
        """    configure_dotnet(env)
    xlate_bin = repo / 'dm/gfx/tools/dmsh_xlate_bin/dmsh_xlate'   # the fork's check of the root configure_dotnet chose (sync_upstream.py, issue #92)
    def xlate_answers(root):
        e = dict(env)
        if root: e['DOTNET_ROOT'] = str(root)
        else: e.pop('DOTNET_ROOT', None)
        try:
            r = subprocess.run([str(xlate_bin), '--source-sha'], capture_output=True, text=True, env=e, timeout=120)
            return r.returncode == 0 and bool(r.stdout.strip())
        except Exception:
            return False
    if xlate_bin.exists() and not xlate_answers(env.get('DOTNET_ROOT')):
        cands = []
        if dn := shutil.which('dotnet', path=env['PATH']):
            real = Path(dn).resolve()
            for base in (real.parent.parent, real.parent.parent.parent):
                cands += [base / 'libexec', base]   # Homebrew's bin/../libexec; share/dotnet and aliases
        cands += [Path.home() / '.dotnet', Path('/usr/local/share/dotnet'), Path('/opt/homebrew/opt/dotnet/libexec')]
        if hit := next((c for c in cands if c.is_dir() and xlate_answers(c)), None):
            env['DOTNET_ROOT'] = os.environ['DOTNET_ROOT'] = str(hit)
            say(f'DOTNET_ROOT: {hit} (verified — the shader translator runs)')
        elif env.get('DOTNET_ROOT'):
            del env['DOTNET_ROOT']; os.environ.pop('DOTNET_ROOT', None)
            say('DOTNET_ROOT: removed (none verified — the apphost searches its default locations)')
""")]))
    P.append(('pwd import', 'import ast, collections, ctypes, getpass, pwd', [(
        "import ast, collections, ctypes, getpass, hashlib, importlib.util, json, os, re, shlex, shutil, struct, subprocess, sys, tempfile, threading, time, types",
        "import ast, collections, ctypes, getpass, pwd, hashlib, importlib.util, json, os, re, shlex, shutil, struct, subprocess, sys, tempfile, threading, time, types")]))
    # HOME can be missing or empty under a GUI launch (issue #10): .NET's first-run configure then
    # builds a mutex path from "" and dies with NuGet-Migrations + lstat("") before reading a project.
    P.append(('HOME fallback', 'DOTNET_CLI_HOME', [(
        "    env.setdefault('USER', getpass.getuser())   # xcodegen fails without USER (Couldn't find current username)",
        """    env.setdefault('USER', getpass.getuser())   # xcodegen fails without USER (Couldn't find current username)
    # HOME and DOTNET_CLI_HOME for every child: a GUI launch (LaunchServices) can hand the app a
    # process environment with HOME missing or empty, and .NET's first-run configure then builds a
    # mutex path from "" and dies with 'NuGet-Migrations' + lstat("") == ENOENT — before any project
    # is read (issue #10, where the app's own DOTNET_ROOT workaround did not help because the empty
    # HOME came from the launch, not from the app). Derive it from the password database when the
    # environment has nothing usable, so the pipeline is correct however it was started.
    if not env.get('HOME') or not os.path.isdir(env['HOME']):
        try: home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        except Exception: home = Path.home()
        if home.is_dir():
            env['HOME'] = os.environ['HOME'] = str(home)
            say(f'HOME: {home} (taken from the password database; the environment had none)')
    env.setdefault('DOTNET_CLI_HOME', env['HOME'])   # .NET writes its first-run state here""")]))
    # Issue #10 also asked why the first-run experience is not simply skipped. It can be, with the
    # two knobs .NET still supports (SKIP_FIRST_TIME_EXPERIENCE was retired in .NET Core 3.0): the
    # banner and the telemetry call are noise for a build, and the telemetry endpoint can hang the
    # very first run on a mainland connection — which is how a "stuck at dotnet" report looks.
    P.append(('dotnet quiet', 'DOTNET_CLI_TELEMETRY_OPTOUT', [(
        "    env.setdefault('DOTNET_CLI_HOME', env['HOME'])   # .NET writes its first-run state here",
        """    env.setdefault('DOTNET_CLI_HOME', env['HOME'])   # .NET writes its first-run state here
    # No first-run banner, no telemetry: a build neither wants nor reads either, and the telemetry
    # call can hang the first run on a network that cannot reach Microsoft. setdefault, so a user
    # who wants telemetry on keeps it.
    env.setdefault('DOTNET_CLI_TELEMETRY_OPTOUT', '1')
    env.setdefault('DOTNET_NOLOGO', '1')""")]))
    # An AppleDouble file ("._<name>.nsp": macOS writes one beside every real file on an exFAT/FAT
    # drive) carries the real file's name and title ID, so scan_nsps counted the one update NSP as two and
    # left both out (7ae7ba65, the report's Hades II build). Hand-edited into the tree until issue #92,
    # which had no protection: the next sync would have replaced the file with upstream's.
    P.append(('AppleDouble skip', 'an AppleDouble file', [(
        "        if p.stat().st_size == 0:   # an empty file left by an incomplete download",
        "        if p.name.startswith('._'):\n"
        "            continue   # an AppleDouble file: macOS writes one beside every real file on exFAT/FAT drives, and its name carries the same [title ID] — counting it made the one real update NSP read as two, and both were then left out (the report's Hades II build)\n"
        "        if p.stat().st_size == 0:   # an empty file left by an incomplete download")]))
    # An external work folder (an exFAT USB drive, the report's own setup) can report entries
    # during shutil.rmtree's walk that vanish by their unlink — FileNotFoundError mid-delete — and
    # the partially deleted folder then breaks the copytree right after. "Make this path gone" is
    # the contract, so ENOENT passes and everything else gets one retry.
    P.append(('rmtree exfat', '_rmtree_enoent', [(
        """def rmtree(p):
    if p.is_symlink() or p.is_file(): p.unlink()
    elif p.exists(): shutil.rmtree(p)""",
        """def _rmtree_enoent(func, p, exc):
    if isinstance(exc[1], FileNotFoundError): return   # already gone: the goal is achieved
    time.sleep(0.2); func(p)                           # one retry: exFAT directory-cache races clear


def rmtree(p):
    if p.is_symlink() or p.is_file(): p.unlink()
    elif p.exists(): shutil.rmtree(p, onerror=_rmtree_enoent)
    if p.exists(): shutil.rmtree(p, onerror=_rmtree_enoent)   # the caller needs it gone (copytree follows)""")]))
    # A shader-chain bump (an upstream translator update) silently invalidates every cached
    # library: the row stayed green, the build "worked", and hours of Metal compile looked like a
    # mystery re-run (the report: "why is it building shaders again, the artifacts are on the
    # drive"). Say it loudly, with a marker both front ends turn into a user-facing notice.
    P.append(('chain-change note', "[NOTE] chain-changed", [(
        """    chain = chain_of(repo)
    out = sh / 'cache' if mac else ios""",
        """    chain = chain_of(repo)
    stale = sorted(d.name for d in (sh / 'cache').glob('b-*') if d.name != f'b-{chain}' and any(d.glob('*_b.metallib'))
                   ) + sorted(d.name for d in ios.glob('b-*') if d.name != f'b-{chain}' and any(d.glob('*_b.metallib')))
    if stale: say(f'[NOTE] chain-changed: {", ".join(stale)} -> b-{chain}: the upstream update changed the shader '
                  'translator, so the cached libraries cannot be reused and this build rebuilds them with it '
                  '(once per update; later builds reuse as before)')
    out = sh / 'cache' if mac else ios""")]))
    # Xcode 15 and later download the iOS platform separately: xcodebuild and the Metal compiler can
    # both be present with no iOS SDK, and the failure then surfaced deep inside xcodebuild. Naming
    # the SDK makes the error actionable (found while auditing environment assumptions, 2026-10-26).
    P.append(('iOS SDK check', "the iOS SDK is not installed", [(
        """    gone = [f'{t} ({how})' for t, how in tools.items() if not shutil.which(t, path=env['PATH'])]
    if gone: raise BuildError('tools not found: ' + ', '.join(gone))
""",
        """    gone = [f'{t} ({how})' for t, how in tools.items() if not shutil.which(t, path=env['PATH'])]
    if gone: raise BuildError('tools not found: ' + ', '.join(gone))
    if not mac:
        # Xcode 15 and later ship the iOS platform as a separate download: xcodebuild and the Metal
        # compiler can both be there while the iOS SDK is not, and the failure then surfaces deep in
        # xcodebuild. Check the SDK itself so the message names the right command.
        sdk = subprocess.run(['xcrun', '--sdk', 'iphoneos', '--show-sdk-version'], capture_output=True, text=True, env=env)
        if sdk.returncode:
            raise BuildError('the iOS SDK is not installed (Xcode 15 and later download the platform separately): run xcodebuild -downloadPlatform iOS, or pick it in Xcode → Settings → Components')
""")]))
    # An Intel Python under Rosetta (an Intel Homebrew in /usr/local on Apple silicon) has children
    # that prefer the x86_64 slice: the shader translator's universal apphost then asks for an x86_64
    # .NET, finds only the arm64 one, prints nothing, and the build stops at the iOS shader step with
    # an empty "xlate_source: lock … / actual" — which reads like a broken translator. ufooer found
    # this on an iPad bring-up (PR #31, item 3); saying it up front turns a mystery into a one-line fix.
    P.append(('Rosetta check', 'is an Intel Python running under Rosetta', [(
        "    if not keys.is_file(): raise BuildError(f'no such file: {keys}')\n",
        """    if not keys.is_file(): raise BuildError(f'no such file: {keys}')
    # An Intel Python (an Intel Homebrew under /usr/local) runs under Rosetta on an Apple silicon Mac, and its children prefer the
    # x86_64 slice: the shader translator's universal apphost then wants an x86_64 .NET, finds the arm64 one, prints nothing, and
    # the build stops at the iOS shader step with "xlate_source: lock … / actual" (empty), which reads like a stale translator.
    # Say so here instead (ufooer's PR #31, item 3).
    if sys.platform == 'darwin' and subprocess.run(['sysctl', '-n', 'sysctl.proc_translated'], capture_output=True, text=True).stdout.strip() == '1':
        raise BuildError(f'{sys.executable} is an Intel Python running under Rosetta; run the build with a native arm64 Python (e.g. /usr/bin/python3 or Homebrew in /opt/homebrew)')
""")]))
    # glslangValidator is only needed by an OpenGL game, and build_gl_shaders already checks for it —
    # but by then the extraction and the capture have run for minutes, and the check raises before
    # layer_shaders creates gl_shaders.log, so the failure had no log to read (issue #71). Checked
    # next to check_metal, which is in the same place for the same reason.
    P.append(('GL shader tool check', 'check_glslang(env)', [(
        "    if gfx == 'nvn': check_metal(py, repo, env)   # before the captures and the shader split, which take minutes\n",
        "    if gfx == 'nvn': check_metal(py, repo, env)   # before the captures and the shader split, which take minutes\n"
        "    if gfx == 'gl': check_glslang(env)\n"), (
        # the function itself, beside check_metal
        """    raise BuildError(msg.replace('set DM_ALLOW_OTHER_METAL=1 (the NSP script\\'s --allow-other-metal)', 'ALLOW_OTHER_METAL = True (--allow-other-metal)'))
""",
        """    raise BuildError(msg.replace('set DM_ALLOW_OTHER_METAL=1 (the NSP script\\'s --allow-other-metal)', 'ALLOW_OTHER_METAL = True (--allow-other-metal)'))


def check_glslang(env):
    \"\"\"An OpenGL game's GLSL goes through glslangValidator; build_gl_shaders checks for it, but only
    after the extraction and the runtime capture have run for minutes, and it raises before
    layer_shaders creates gl_shaders.log — so the GUI showed a failure with no log to read (issue
    #71). Checked before those steps, as check_metal is and for the same reason.\"\"\"
    if not shutil.which('glslangValidator', path=env['PATH']):
        raise BuildError("glslangValidator is not installed: an OpenGL game's GLSL shaders need it to compile to SPIR-V (brew install glslang)")
""")]))
    # Every extracted version but the packed one holds a complete extra copy of the game; the fork drops
    # them once the build is done (1887fe93: a game with several updates filled the drive). Hand-edited
    # until issue #92; the function is added before extract(), and each ending of main() calls it.
    PRUNE_FUNC = r'''def prune_intermediate_extracts(dirs, versions, pkg_ver):
    """Every extracted version but the packed one: each holds a complete extra copy of the game (one
    merged RomFS image per version), and only the packed version's files are reused by the next
    build — when its inputs change, the intermediates are extracted again anyway (the packed version
    is merged from them). The shader captures above still read them, so they are dropped only once
    the build is done. A game without an update extracts one version and this deletes nothing."""
    spare = [dirs[v] for v in versions if v != pkg_ver and dirs[v].is_dir()]
    if not spare: return
    say('deleting the intermediate extractions, one full copy of the game each (the packed version keeps its files): '
        + ', '.join(d.name for d in spare))
    for d in spare: rmtree(d)


'''
    P.append(('prune function', 'def prune_intermediate_extracts', [(
        'def extract(nx, keys, ex, name, base, upd, cmp_dir, logs, g):', PRUNE_FUNC + 'def extract(nx, keys, ex, name, base, upd, cmp_dir, logs, g):')]))
    for what, tail in (("Mac app", "        say(f'done: {out_ipa} ({tree_size(out_ipa)[1]"), ("Mac run", "        say(f'done: the game runs on the Mac"),
                       ("Xcode project", "        say(f'ready: {workspace}')")):
        P.append((f'prune after the {what}', "rmtree(work / 'tmp')\n        prune_intermediate_extracts(dirs, versions, pkg_ver)\n" + tail, [(
            "rmtree(work / 'tmp')\n" + tail, "rmtree(work / 'tmp')\n        prune_intermediate_extracts(dirs, versions, pkg_ver)\n" + tail)]))
    P.append(('prune after the IPA', "{ex}'); rmtree(ex)\n    else:\n        prune_intermediate_extracts", [(
        "        say(f'deleting the extracted game files: {ex}'); rmtree(ex)\n",
        "        say(f'deleting the extracted game files: {ex}'); rmtree(ex)\n    else:\n        prune_intermediate_extracts(dirs, versions, pkg_ver)\n")]))
    # Switch 2 dumps: raw exefs/ and romfs/ (scripts/switch2_build.py stages them). Each marker is text only that hunk adds.
    P.append(('switch2 header', 'No prod.keys, and no NSP.', [
        ("# Uses only three things: this repository, a folder holding one game's NSPs (the base game, with or without an update; DLC NSPs, when present, are packed too and unlock the add-on content already in the game), and prod.keys.\n", "# Uses only three things: this repository, a folder holding one game's NSPs (the base game, with or without an update; DLC NSPs, when present, are packed too and unlock the add-on content already in the game), and prod.keys.\n# Switch 2 (--switch2, or SWITCH2 in Spyder): the same build from a dump of raw exefs/ and romfs/ (a version folder such as updates/458752, or a game folder with updates/<version>/ and dlc/<title id>/) instead of NSP files. No prod.keys, and no NSP. scripts/switch2_build.py links the directories into the build; the rewrite, shaders and pack are unchanged.\n"),
    ]))
    P.append(('switch2 settings', "SWITCH2 = ''", [
        ("PROD_KEYS = ''                          # path of prod.keys (empty = ask at run time)\n", "PROD_KEYS = ''                          # path of prod.keys (empty = ask at run time)\nSWITCH2 = ''                            # Switch 2 dump instead of NSPs: a version folder with exefs/ and romfs/ (updates/458752), or a game folder with updates/<version>/ and dlc/<title id>/. Empty = NSP files. No prod.keys\nEXEFS = ''                              # with ROMFS, the raw ExeFS directory (Switch 2 mode; overrides the exefs inside SWITCH2)\nROMFS = ''                              # with EXEFS, the raw RomFS directory\nROMFS_IMAGE = False                     # Switch 2: pack a RomFS.img instead of linking the romfs directory (one file, as an NSP extraction writes)\n"),
    ]))
    P.append(('switch2 survey', 'def survey_body', [
        ('def survey(nsp_dir, app, fam, keys, repo, py, env):\n    work = nsp_dir / f\'{app:016X}_survey\'; logs, ex = work / \'logs\', work / \'extract\'\n    say(f\'no game profile for {app:016X}: surveying first\')\n    PROG.plan(4)   # nx_extract, the manifest, the code modules (reading them, then the rewrite-site census), the imports\n    PROG.stage(\'survey: build nx_extract\')\n    nx = build_nx_extract(repo, work, logs, env)\n    ups = sorted(fam[\'updates\'], key=lambda u: u[0] or 0)\n    upd = ups[-1][1] if ups else None\n    tmp = work / \'extract.partial\'; rmtree(tmp)\n    def listed():   # files in the manifest so far\n        try: return {\'done\': (tmp / \'romfs_manifest.tsv\').read_bytes().count(b\'\\n\'), \'unit\': \'files\'}\n        except OSError: return None\n    PROG.stage(\'survey: RomFS manifest\', poll=listed)\n    say(f\'extracting the code modules and a RomFS manifest (no game files written) → {ex}\')\n    run([nx, keys, fam[\'base\'], upd or \'-\', tmp, \'--manifest\'] + ([\'--allow-damaged\'] if ALLOW_DAMAGED else []), logs / \'extract.log\')\n    swap_in(tmp, ex)\n    PROG.stage(\'survey: code modules\', step=\'reading\')\n    xlog = (logs / \'extract.log\').read_text(errors=\'replace\')\n    dv = re.search(r\'display version (\\S+)\', xlog)\n    F = pg.facts_of(ex / \'ExeFS\', ex / \'romfs_manifest.tsv\'); mods, rom = F[\'mods\'], F[\'romfs\']   # dm/games/profile_guess.py: what the code modules and the RomFS show\n    R = [f\'# Survey: {nacp.name(ex / "control.nacp") or "?"} ({app:016X})\', \'\', f\'- display version: {dv.group(1) if dv else "?"}\', f\'- base: `{fam["base"].name}` ({fam["base"].stat().st_size / 2**30:.2f} GB)\']\n    R += [f\'- update: `{p.name}` (v{v}, {p.stat().st_size / 2**30:.2f} GB){" (used for this survey)" if p == upd else ""}\' for v, p in ups]\n    R += [f\'- DLC: `{p.name}` ({p.stat().st_size / 1024:.0f} KB{", licence only, no content" if p.stat().st_size < 1 << 20 else ""})\' for _, _, p in fam[\'dlc\']]\n\n    # Code modules\n    R += [\'\', \'## Code modules\', \'\', \'| module | what | build ID | text / ro / data (MB) | bss (MB) |\', \'|---|---|---|---|---|\']\n    R += [f\'| {n} | {m["kind"]} | `{m["bid"]}` | \' + \' / \'.join(f\'{z / 2**20:.1f}\' for z in m[\'sizes\']) + f\' | {m["bss"] / 2**20:.1f} |\' for n, m in mods.items()]\n    if F[\'program\'] is not None: R.append(f\'\\n- main.npdm program ID: `{F["program"]:016X}`\')\n    if (ex / \'ExeFS/main.npdm\').exists(): R.append(f\'- system resource: {F["sysres"] / 2**20:.0f} MB\')\n    R.append(f\'- SDK: {", ".join(sorted(F["sdkver"])) or "?"} (Mario: 16_2_0 / 16_2_12)\')\n    R.append(\'- SDK components: \' + (\', \'.join(sorted(F[\'comps\'])) or \'—\'))\n    if F[\'unity\']: R.append(\'- Unity: \' + \', \'.join(sorted(F[\'unity\'])))\n    R.append(\'- engine and API markers: \' + (\'; \'.join(f\'{k} ({", ".join(v)})\' for k, v in F[\'markers\'].items()) or \'—\'))\n    extra = [n for n in mods if n not in (\'main\', \'sdk\', \'rtld\')]\n\n    # RomFS manifest\n    R += [\'\', f\'## RomFS: {rom["n"]} files, {rom["bytes"] / 2**30:.2f} GB\', \'\', \'| top level | files | GB |\', \'|---|---|---|\']\n    R += [f\'| {t} | {n} | {rom["topb"][t] / 2**30:.2f} |\' for t, n in rom[\'tops\'].most_common(20)]\n    R += [\'\', \'| header magic | files | GB |\', \'|---|---|---|\'] + [f\'| {m} | {n} | {rom["magb"][m] / 2**30:.2f} |\' for m, n in rom[\'mags\'].most_common()]\n    R += [\'\', \'extensions: \' + \', \'.join(f\'`{e}` {n}\' for e, n in rom[\'exts\'].most_common(20))]\n\n    # Rewrite-site census and SDK surface\n    R += [\'\', \'## Rewrite-site census (insn_census.py)\', \'\', \'```\']\n    census = [ex / \'ExeFS\' / n for n in mods if n != \'rtld\']   # rtld is replaced by the host\n    for i, m in enumerate(census):\n        PROG.set(step=f\'rewrite sites in {m.name}\', done=i, total=len(census), unit=\'modules\')\n        run([py, repo / \'dm/tools/insn_census.py\', m], logs / f\'census_{m.name}.log\', env=env)\n        R += (logs / f\'census_{m.name}.log\').read_text(errors=\'replace\').rstrip().splitlines()\n    R += [\'```\', \'\', \'## Imports vs host hooks (nso_syms.py)\', \'\', \'```\']\n    PROG.stage(\'survey: imports vs host hooks\')\n    run([py, repo / \'dm/tools/nso_syms.py\', repo / \'dm/host\', \'-\', ex / \'ExeFS\'], logs / \'nso_syms.log\', env=env)\n    PROG.end()\n    R += (logs / \'nso_syms.log\').read_text(errors=\'replace\').rstrip().splitlines() + [\'```\']\n\n    # What it means for a port\n    G = pg.guess(F)   # the provisional profile\'s keys (write_profile writes the same)\n    keep = G[\'modules\'][0].split()   # how the build will get the shaders (decided again from the extraction at build time; the survey has only a RomFS manifest, so Unreal paks are guessed from main\'s markers)\n    S = pg.shader_sources(mods[\'main\'][\'imports\'], F[\'glsl_src\'], [m for m in keep if \'glslcCompile\' in mods[m][\'exports\']], rom[\'mags\'][\'SPIR-V\'],\n                          rom[\'exts\'].get(\'.pak\', 0) if F.get(\'main_markers\', set()) & {\'Unreal\', \'Unreal 4\'} else 0, rom[\'mags\'][\'Trinity pack\'], gl_names=F.get(\'gl_names\', 0),\n                          gl_src=F.get(\'gl_src\', 0), gl_files=sum(rom[\'exts\'].get(e, 0) for e in pg.GL_FILES), oodle_pkz=rom[\'mags\'][\'PlatinumGames PKZ\'])   # the manifest has no name tables: every PKZ counts\n    vulkan, gl = S[\'graphics\'][0] == \'vulkan\', S[\'graphics\'][0] == \'gl\'\n    notes = []\n    if not vulkan and not gl and \'nvnBootstrapLoader\' not in mods[\'main\'][\'imports\']: notes.append(\'main imports neither nvnBootstrapLoader nor Vulkan functions nor OpenGL through EGL: the host implements only NVN, Vulkan and OpenGL\')\n    if vulkan: notes.append(f\'{S["graphics"][1]}; the NVN shader steps are skipped\' + (\', and its shaders are captured by booting it on the Mac (no SPIR-V file in the RomFS)\' if S[\'vk_capture\'][0] else \'\'))\n    if gl: notes.append(f\'{S["graphics"][1]}; the NVN shader steps are skipped: gl_shaders.py translates its GLSL sources (MVGL archives, main\\\'s strings\'\n                        + (\', and the shaders it puts together at run time, captured by booting it on the Mac)\' if S[\'gl_capture\'][0] else \')\'))\n    notes += [S[k][1] for k in (\'glslc_capture\',) if S[k][0]]\n    if S[\'ue_decode\'][0]:   # the manifest has no pak indexes, so both kinds are named; the build tells them apart by the footer\'s version and the index (uepak.py inline_shaders)\n        notes.append(f\'{rom["exts"].get(".pak", 0)} Unreal pak(s): version 11 paks keep the shaders compressed in shader code archives (Octopath Traveler 0), decoded by booting the game on the Mac; \'\n                     \'legacy paks (version 4, DRAGON QUEST XI S) and version 11 paks without shader code archives (OCTOPATH TRAVELER II) keep them inline in the cooked packages, which the shader split reads file by file\')\n    if extra: notes.append(\'modules besides main / sdk: {}; the provisional profile loads {} (load order main, subsdk*, sdk)\'.format(", ".join(f\'{n} ({mods[n]["kind"]})\' for n in extra), G["modules"][0]))\n    if rom[\'mags\'][\'UnityFS\'] or \'Unity IL2CPP\' in F[\'markers\']: notes.append(\'Unity: the shaders are Shader objects in UnityFS bundles and data.unity3d, which shader_split.py reads through unityfs.py (written for Unity 2020.3\\\'s Switch format, also read for 2017.4, 2018.3, 2022.3 and 6000.0; check other Unity versions first)\')\n    if F[\'glslc\']: notes.append(f\'main has {F["glslc"]} GLSLC compiler output magics (0x19866891): shader_split.py takes those that are whole outputs (13 Sentinels and the KTGL games compile their shaders into main; Unity\\\'s player has 11 of its own)\')\n    if rom[\'mags\'][\'Trinity pack\']: notes.append(\'Trinity packs (Game Freak, arc/data.trpfs with its index data.trpfd): the shaders are BNSH inside Oodle Mermaid streams; shader_split.py decodes them with the game\\\'s own OodleLZ_Decompress in the Mac host, which dm/host/hle_oodle.c finds by the library\\\'s name string\')\n    elif rom[\'mags\'][\'PlatinumGames PKZ\']: notes.append(\'PlatinumGames PKZ archives (pkzl; Bayonetta 3): DAT archives inside, compressed per entry (OodleKraken, ZStandard or None); the shaders are shader objects (VSO / PSO / CSO …) in DATs, which shader_split.py reads (pkz_entries, pg_shader), Oodle Kraken through the game\\\'s own streaming decoder in the Mac host (dm/host/hle_oodle.c)\')\n    elif rom[\'mags\'][\'MT Framework ARC\']: notes.append(\'MT Framework ARC archives (Capcom, The Great Ace Attorney Chronicles): the shaders are in a shader package (SPK) inside one of them, which shader_split.py reads (mtarc_members, mtspk_shaders)\')\n    elif rom[\'mags\'][\'Bezel Engine archive\']: notes.append(\'NintendoWare Bezel Engine archives (.bea, magic SCNE; Big Brain Academy: Brain vs. Brain): every entry is a zstd frame, and the shaders are BNSH in their .bfsha entries, which shader_split.py reads (bea_members)\')\n    elif rom[\'mags\'][\'CRI CPK\'] and not F[\'glslc\']: notes.append(\'CRI CPK archives (Bayonetta 2; Vanillaware\\\'s and Atlus\\\'s games keep their shaders elsewhere): files compressed one by one with CRILAYLA; shader_split.py decodes every entry (dm/gfx/tools/cpk.py, cpk_jobs) and scans it as a loose file (Bayonetta 2: BNSH files in PlatinumGames DAT archives)\')\n    elif rom[\'mags\'][\'XNB\'] and \'MonoGame\' in F[\'markers\']: notes.append(\'MonoGame (XNB content): the shaders are MonoGame effects (MGFX version 8, NVN control sections and code areas) in the XNB files EffectReader reads, and SpriteBatch\\\'s in main, which shader_split.py reads (mgfx_effect; Celeste), a compressed XNB (MonoGame\\\'s LZX or LZ4, Stardew Valley) after decompressing it (xnb_decompress)\')\n    elif not (rom[\'mags\'][\'BNSH\'] or rom[\'mags\'][\'BAHS\']) and not rom[\'mags\'][\'UnityFS\'] and not vulkan and not gl and not F[\'glslc\']:\n        notes.append(\'no BNSH / BAHS at the top level: the shaders are inside compressed or other containers or in main, and must be located first\')\n    major = {v.split(\'_\')[0] for v in F[\'sdkver\']}\n    if not major: notes.append(\'no SDK version string found\')\n    elif major != {\'16\'}: notes.append(f\'SDK {"/".join(sorted(major))}, not Mario\\\'s 16: NVN object sizes, API version and service layouts differ by SDK (the host was converted from D2R\\\'s SDK 12 to SDK 16 sizes); check them one by one per docs/reference/nvn-objects.md\')\n    if any(l.split()[:1] == [\'nn::ro\'] for l in R):\n        nro, nrr = rom[\'exts\'][\'.nro\'], rom[\'exts\'][\'.nrr\']\n        # NRR files register NROs by hash; with none loose the NROs are inside an archive (Super Smash Bros. Ultimate: 14 NRRs, 98 NROs in data.arc, whose exports main imports)\n        notes.append(f\'imports nn::ro (loads NRO modules at run time): the RomFS has {nro} .nro files and {nrr} .nrr files; registered NROs are collected and rewritten beside the other modules\'\n                     + (\' (look inside archives for the registered modules)\' if nrr and not nro else \'\') + \' and embedded in the iOS app (gen_embed.py)\')\n    if fam[\'dlc\']: notes.append(\'DLC present: it is extracted into the IPA and the host reports the indices as owned and mounts them; check that the host hooks every aoc / fs add-on content call main imports\')\n    R += [\'\', \'## What it means for a port\', \'\'] + [f\'- {n}\' for n in notes or [\'—\']]\n    R += [\'\', \'## Provisional profile keys (dm/games/profile_guess.py)\', \'\', \'What the game files decide; mem_used follows the rewrite, and a game with virtual address memory that sets a heap gets a heap check on the Mac (scripts/build_ipa_from_nsp.py heap_check).\', \'\', \'```\']\n    R += [f\'{k}\\t{v}\' + (f\'\\t# {why}\' if why else \'\') for k, (v, why) in G.items()] + [\'```\']\n    R += [\'\', \'Next steps: docs/agents (02 survey, 03 bring-up, symptoms.md for each failure).\']\n    (work / \'survey.md\').write_text(\'\\n\'.join(R) + \'\\n\')\n    for n in notes: say(n)\n    say(f\'survey report: {work / "survey.md"}\')\n    upv = ups[-1][0] if ups else None\n    return {\'F\': F, \'name\': nacp.name(ex / \'control.nacp\') or \'\', \'display\': dv.group(1) if dv else \'?\', \'version\': \'v0\' if upd is None else f\'v{upv or 1}\'}\n\n', 'def survey(nsp_dir, app, fam, keys, repo, py, env):\n    work = nsp_dir / f\'{app:016X}_survey\'; logs, ex = work / \'logs\', work / \'extract\'\n    say(f\'no game profile for {app:016X}: surveying first\')\n    PROG.plan(4)   # nx_extract, the manifest, the code modules (reading them, then the rewrite-site census), the imports\n    PROG.stage(\'survey: build nx_extract\')\n    nx = build_nx_extract(repo, work, logs, env)\n    ups = sorted(fam[\'updates\'], key=lambda u: u[0] or 0)\n    upd = ups[-1][1] if ups else None\n    tmp = work / \'extract.partial\'; rmtree(tmp)\n    def listed():   # files in the manifest so far\n        try: return {\'done\': (tmp / \'romfs_manifest.tsv\').read_bytes().count(b\'\\n\'), \'unit\': \'files\'}\n        except OSError: return None\n    PROG.stage(\'survey: RomFS manifest\', poll=listed)\n    say(f\'extracting the code modules and a RomFS manifest (no game files written) → {ex}\')\n    run([nx, keys, fam[\'base\'], upd or \'-\', tmp, \'--manifest\'] + ([\'--allow-damaged\'] if ALLOW_DAMAGED else []), logs / \'extract.log\')\n    swap_in(tmp, ex)\n    xlog = (logs / \'extract.log\').read_text(errors=\'replace\')\n    dv = re.search(r\'display version (\\S+)\', xlog)\n    R = [f\'# Survey: {nacp.name(ex / "control.nacp") or "?"} ({app:016X})\', \'\', f\'- display version: {dv.group(1) if dv else "?"}\', f\'- base: `{fam["base"].name}` ({fam["base"].stat().st_size / 2**30:.2f} GB)\']\n    R += [f\'- update: `{p.name}` (v{v}, {p.stat().st_size / 2**30:.2f} GB){" (used for this survey)" if p == upd else ""}\' for v, p in ups]\n    R += [f\'- DLC: `{p.name}` ({p.stat().st_size / 1024:.0f} KB{", licence only, no content" if p.stat().st_size < 1 << 20 else ""})\' for _, _, p in fam[\'dlc\']]\n    name = nacp.name(ex / \'control.nacp\') or \'\'\n    upv = ups[-1][0] if ups else None\n    return survey_body(nsp_dir, app, ex / \'ExeFS\', ex / \'romfs_manifest.tsv\', R, bool(fam[\'dlc\']), name, dv.group(1) if dv else \'?\', \'v0\' if upd is None else f\'v{upv or 1}\', repo, py, env)\n\n\ndef survey_body(nsp_dir, app, exefs, romfs, R, dlc, name, display, version, repo, py, env):\n    """The rest of survey(), from code modules onward. survey() extracts an NSP first; a Switch 2 dump already has exefs and a RomFS folder (or a manifest) and calls this directly."""\n    work = nsp_dir / f\'{app:016X}_survey\'; logs = work / \'logs\'; logs.mkdir(parents=True, exist_ok=True)\n    PROG.stage(\'survey: code modules\', step=\'reading\')\n    exefs = Path(exefs)\n    F = pg.facts_of(exefs, romfs); mods, rom = F[\'mods\'], F[\'romfs\']   # dm/games/profile_guess.py: what the code modules and the RomFS show\n    # Code modules\n    R += [\'\', \'## Code modules\', \'\', \'| module | what | build ID | text / ro / data (MB) | bss (MB) |\', \'|---|---|---|---|---|\']\n    R += [f\'| {n} | {m["kind"]} | `{m["bid"]}` | \' + \' / \'.join(f\'{z / 2**20:.1f}\' for z in m[\'sizes\']) + f\' | {m["bss"] / 2**20:.1f} |\' for n, m in mods.items()]\n    if F[\'program\'] is not None: R.append(f\'\\n- main.npdm program ID: `{F["program"]:016X}`\')\n    if (exefs / \'main.npdm\').exists(): R.append(f\'- system resource: {F["sysres"] / 2**20:.0f} MB\')\n    R.append(f\'- SDK: {", ".join(sorted(F["sdkver"])) or "?"} (Mario: 16_2_0 / 16_2_12)\')\n    R.append(\'- SDK components: \' + (\', \'.join(sorted(F[\'comps\'])) or \'—\'))\n    if F[\'unity\']: R.append(\'- Unity: \' + \', \'.join(sorted(F[\'unity\'])))\n    R.append(\'- engine and API markers: \' + (\'; \'.join(f\'{k} ({", ".join(v)})\' for k, v in F[\'markers\'].items()) or \'—\'))\n    extra = [n for n in mods if n not in (\'main\', \'sdk\', \'rtld\')]\n\n    # RomFS manifest\n    R += [\'\', f\'## RomFS: {rom["n"]} files, {rom["bytes"] / 2**30:.2f} GB\', \'\', \'| top level | files | GB |\', \'|---|---|---|\']\n    R += [f\'| {t} | {n} | {rom["topb"][t] / 2**30:.2f} |\' for t, n in rom[\'tops\'].most_common(20)]\n    R += [\'\', \'| header magic | files | GB |\', \'|---|---|---|\'] + [f\'| {m} | {n} | {rom["magb"][m] / 2**30:.2f} |\' for m, n in rom[\'mags\'].most_common()]\n    R += [\'\', \'extensions: \' + \', \'.join(f\'`{e}` {n}\' for e, n in rom[\'exts\'].most_common(20))]\n\n    # Rewrite-site census and SDK surface\n    R += [\'\', \'## Rewrite-site census (insn_census.py)\', \'\', \'```\']\n    census = [exefs / n for n in mods if n != \'rtld\']   # rtld is replaced by the host\n    for i, m in enumerate(census):\n        PROG.set(step=f\'rewrite sites in {m.name}\', done=i, total=len(census), unit=\'modules\')\n        run([py, repo / \'dm/tools/insn_census.py\', m], logs / f\'census_{m.name}.log\', env=env)\n        R += (logs / f\'census_{m.name}.log\').read_text(errors=\'replace\').rstrip().splitlines()\n    R += [\'```\', \'\', \'## Imports vs host hooks (nso_syms.py)\', \'\', \'```\']\n    PROG.stage(\'survey: imports vs host hooks\')\n    run([py, repo / \'dm/tools/nso_syms.py\', repo / \'dm/host\', \'-\', exefs], logs / \'nso_syms.log\', env=env)\n    PROG.end()\n    R += (logs / \'nso_syms.log\').read_text(errors=\'replace\').rstrip().splitlines() + [\'```\']\n\n    # What it means for a port\n    G = pg.guess(F)   # the provisional profile\'s keys (write_profile writes the same)\n    keep = G[\'modules\'][0].split()   # how the build will get the shaders (decided again from the extraction at build time; the survey has only a RomFS manifest, so Unreal paks are guessed from main\'s markers)\n    S = pg.shader_sources(mods[\'main\'][\'imports\'], F[\'glsl_src\'], [m for m in keep if \'glslcCompile\' in mods[m][\'exports\']], rom[\'mags\'][\'SPIR-V\'],\n                          rom[\'exts\'].get(\'.pak\', 0) if F.get(\'main_markers\', set()) & {\'Unreal\', \'Unreal 4\'} else 0, rom[\'mags\'][\'Trinity pack\'], gl_names=F.get(\'gl_names\', 0),\n                          gl_src=F.get(\'gl_src\', 0), gl_files=sum(rom[\'exts\'].get(e, 0) for e in pg.GL_FILES), oodle_pkz=rom[\'mags\'][\'PlatinumGames PKZ\'])   # the manifest has no name tables: every PKZ counts\n    vulkan, gl = S[\'graphics\'][0] == \'vulkan\', S[\'graphics\'][0] == \'gl\'\n    notes = []\n    if not vulkan and not gl and \'nvnBootstrapLoader\' not in mods[\'main\'][\'imports\']: notes.append(\'main imports neither nvnBootstrapLoader nor Vulkan functions nor OpenGL through EGL: the host implements only NVN, Vulkan and OpenGL\')\n    if vulkan: notes.append(f\'{S["graphics"][1]}; the NVN shader steps are skipped\' + (\', and its shaders are captured by booting it on the Mac (no SPIR-V file in the RomFS)\' if S[\'vk_capture\'][0] else \'\'))\n    if gl: notes.append(f\'{S["graphics"][1]}; the NVN shader steps are skipped: gl_shaders.py translates its GLSL sources (MVGL archives, main\\\'s strings\'\n                        + (\', and the shaders it puts together at run time, captured by booting it on the Mac)\' if S[\'gl_capture\'][0] else \')\'))\n    notes += [S[k][1] for k in (\'glslc_capture\',) if S[k][0]]\n    if S[\'ue_decode\'][0]:   # the manifest has no pak indexes, so both kinds are named; the build tells them apart by the footer\'s version and the index (uepak.py inline_shaders)\n        notes.append(f\'{rom["exts"].get(".pak", 0)} Unreal pak(s): version 11 paks keep the shaders compressed in shader code archives (Octopath Traveler 0), decoded by booting the game on the Mac; \'\n                     \'legacy paks (version 4, DRAGON QUEST XI S) and version 11 paks without shader code archives (OCTOPATH TRAVELER II) keep them inline in the cooked packages, which the shader split reads file by file\')\n    if extra: notes.append(\'modules besides main / sdk: {}; the provisional profile loads {} (load order main, subsdk*, sdk)\'.format(", ".join(f\'{n} ({mods[n]["kind"]})\' for n in extra), G["modules"][0]))\n    if rom[\'mags\'][\'UnityFS\'] or \'Unity IL2CPP\' in F[\'markers\']: notes.append(\'Unity: the shaders are Shader objects in UnityFS bundles and data.unity3d, which shader_split.py reads through unityfs.py (written for Unity 2020.3\\\'s Switch format, also read for 2017.4, 2018.3, 2022.3 and 6000.0; check other Unity versions first)\')\n    if F[\'glslc\']: notes.append(f\'main has {F["glslc"]} GLSLC compiler output magics (0x19866891): shader_split.py takes those that are whole outputs (13 Sentinels and the KTGL games compile their shaders into main; Unity\\\'s player has 11 of its own)\')\n    if rom[\'mags\'][\'Trinity pack\']: notes.append(\'Trinity packs (Game Freak, arc/data.trpfs with its index data.trpfd): the shaders are BNSH inside Oodle Mermaid streams; shader_split.py decodes them with the game\\\'s own OodleLZ_Decompress in the Mac host, which dm/host/hle_oodle.c finds by the library\\\'s name string\')\n    elif rom[\'mags\'][\'PlatinumGames PKZ\']: notes.append(\'PlatinumGames PKZ archives (pkzl; Bayonetta 3): DAT archives inside, compressed per entry (OodleKraken, ZStandard or None); the shaders are shader objects (VSO / PSO / CSO …) in DATs, which shader_split.py reads (pkz_entries, pg_shader), Oodle Kraken through the game\\\'s own streaming decoder in the Mac host (dm/host/hle_oodle.c)\')\n    elif rom[\'mags\'][\'MT Framework ARC\']: notes.append(\'MT Framework ARC archives (Capcom, The Great Ace Attorney Chronicles): the shaders are in a shader package (SPK) inside one of them, which shader_split.py reads (mtarc_members, mtspk_shaders)\')\n    elif rom[\'mags\'][\'Bezel Engine archive\']: notes.append(\'NintendoWare Bezel Engine archives (.bea, magic SCNE; Big Brain Academy: Brain vs. Brain): every entry is a zstd frame, and the shaders are BNSH in their .bfsha entries, which shader_split.py reads (bea_members)\')\n    elif rom[\'mags\'][\'CRI CPK\'] and not F[\'glslc\']: notes.append(\'CRI CPK archives (Bayonetta 2; Vanillaware\\\'s and Atlus\\\'s games keep their shaders elsewhere): files compressed one by one with CRILAYLA; shader_split.py decodes every entry (dm/gfx/tools/cpk.py, cpk_jobs) and scans it as a loose file (Bayonetta 2: BNSH files in PlatinumGames DAT archives)\')\n    elif rom[\'mags\'][\'XNB\'] and \'MonoGame\' in F[\'markers\']: notes.append(\'MonoGame (XNB content): the shaders are MonoGame effects (MGFX version 8, NVN control sections and code areas) in the XNB files EffectReader reads, and SpriteBatch\\\'s in main, which shader_split.py reads (mgfx_effect; Celeste), a compressed XNB (MonoGame\\\'s LZX or LZ4, Stardew Valley) after decompressing it (xnb_decompress)\')\n    elif not (rom[\'mags\'][\'BNSH\'] or rom[\'mags\'][\'BAHS\']) and not rom[\'mags\'][\'UnityFS\'] and not vulkan and not gl and not F[\'glslc\']:\n        notes.append(\'no BNSH / BAHS at the top level: the shaders are inside compressed or other containers or in main, and must be located first\')\n    major = {v.split(\'_\')[0] for v in F[\'sdkver\']}\n    if not major: notes.append(\'no SDK version string found\')\n    elif major != {\'16\'}: notes.append(f\'SDK {"/".join(sorted(major))}, not Mario\\\'s 16: NVN object sizes, API version and service layouts differ by SDK (the host was converted from D2R\\\'s SDK 12 to SDK 16 sizes); check them one by one per docs/reference/nvn-objects.md\')\n    if any(l.split()[:1] == [\'nn::ro\'] for l in R):\n        nro, nrr = rom[\'exts\'][\'.nro\'], rom[\'exts\'][\'.nrr\']\n        # NRR files register NROs by hash; with none loose the NROs are inside an archive (Super Smash Bros. Ultimate: 14 NRRs, 98 NROs in data.arc, whose exports main imports)\n        notes.append(f\'imports nn::ro (loads NRO modules at run time): the RomFS has {nro} .nro files and {nrr} .nrr files; registered NROs are collected and rewritten beside the other modules\'\n                     + (\' (look inside archives for the registered modules)\' if nrr and not nro else \'\') + \' and embedded in the iOS app (gen_embed.py)\')\n    if dlc: notes.append(\'DLC present: it is extracted into the IPA and the host reports the indices as owned and mounts them; check that the host hooks every aoc / fs add-on content call main imports\')\n    R += [\'\', \'## What it means for a port\', \'\'] + [f\'- {n}\' for n in notes or [\'—\']]\n    R += [\'\', \'## Provisional profile keys (dm/games/profile_guess.py)\', \'\', \'What the game files decide; mem_used follows the rewrite, and a game with virtual address memory that sets a heap gets a heap check on the Mac (scripts/build_ipa_from_nsp.py heap_check).\', \'\', \'```\']\n    R += [f\'{k}\\t{v}\' + (f\'\\t# {why}\' if why else \'\') for k, (v, why) in G.items()] + [\'```\']\n    R += [\'\', \'Next steps: docs/agents (02 survey, 03 bring-up, symptoms.md for each failure).\']\n    (work / \'survey.md\').write_text(\'\\n\'.join(R) + \'\\n\')\n    for n in notes: say(n)\n    say(f\'survey report: {work / "survey.md"}\')\n    return {\'F\': F, \'name\': name, \'display\': display, \'version\': version}\n\n'),
    ]))
    P.append(('switch2 derive display', 'display or nacp.version', [
        ('def derive_profile(g, pdir, ver, d, mods, app=None, shader_only=False):', 'def derive_profile(g, pdir, ver, d, mods, app=None, shader_only=False, display=None):'),
        ("    T, disp = '\\t', nacp.version(d / 'control.nacp') or '?'", "    T, disp = '\\t', display or nacp.version(d / 'control.nacp') or '?'   # display: a Switch 2 dump has no control.nacp; the folder name's version stands in"),
    ]))
    P.append(('switch2 arguments', "metavar='DIR'", [
        ('        epilog=\'example: python3 scripts/build_ipa_from_nsp.py ~/nsp/"Super Mario Bros Wonder" -k ~/keys/prod.keys\')\n    ap.add_argument(\'nsp_dir\', nargs=\'?\', help=\'folder holding one game\\\'s NSPs (default: the NSP_DIR setting, else asked)\')\n    ap.add_argument(\'-k\', \'--keys\', help=\'path of prod.keys (default: the PROD_KEYS setting, else asked)\')\n', '        epilog=\'examples: python3 scripts/build_ipa_from_nsp.py ~/nsp/"Super Mario Bros Wonder" -k ~/keys/prod.keys\\n\'\n               \'         python3 scripts/build_ipa_from_nsp.py --switch2 ~/dumps/"Game (v3.2.0)"/updates/458752 --platform mac\')\n    ap.add_argument(\'nsp_dir\', nargs=\'?\', help=\'folder holding one game\\\'s NSPs (default: the NSP_DIR setting, else asked). With --switch2 and no DIR, this folder is the dump\')\n    ap.add_argument(\'-k\', \'--keys\', help=\'path of prod.keys (default: the PROD_KEYS setting, else asked). Not used with --switch2\')\n    ap.add_argument(\'--switch2\', nargs=\'?\', const=\'\', metavar=\'DIR\', help=\'build from a Switch 2 dump of raw exefs/ and romfs/ instead of NSP files. DIR is a version folder (updates/458752, containing exefs/ and romfs/) or a game folder (updates/<version>/ and dlc/<title id>/). Without DIR, the positional folder is the dump. No prod.keys\')\n    ap.add_argument(\'--exefs\', help=\'raw ExeFS directory (with --romfs; Switch 2 mode)\')\n    ap.add_argument(\'--romfs\', help=\'raw RomFS directory (with --exefs; Switch 2 mode)\')\n    ap.add_argument(\'--dlc\', action=\'append\', help=\'DLC romfs, a dlc/<title id>/ folder, or a dlc/ directory (repeatable; also discovered beside updates/)\')\n    ap.add_argument(\'--romfs-image\', action=\'store_true\', help=\'Switch 2: pack the romfs directory into a RomFS.img instead of linking it\')\n'),
    ]))
    P.append(('switch2 helpers', 'def switch2_requested', [
        ('def main(args=None):\n', 'def switch2_requested(args):\n    """True when this run takes a Switch 2 dump (raw exefs and romfs) instead of NSP files."""\n    if getattr(args, \'exefs\', None) or getattr(args, \'romfs\', None) or EXEFS or ROMFS: return True\n    if getattr(args, \'switch2\', None) is not None: return True\n    return bool(SWITCH2)\n\n\ndef switch2_paths(args):\n    """(folder or None, exefs or None, romfs or None, dlc paths). folder is a version directory or a game directory."""\n    exefs, romfs = args.exefs or EXEFS or None, args.romfs or ROMFS or None\n    dlc = list(args.dlc or [])\n    if exefs or romfs:\n        if not exefs or not romfs: raise BuildError(\'Switch 2 mode with a raw ExeFS needs the RomFS too (--exefs and --romfs, or EXEFS and ROMFS)\')\n        folder = args.switch2 if args.switch2 not in (None, \'\') else (args.nsp_dir or None)\n        return folder, exefs, romfs, dlc\n    folder = args.switch2 if args.switch2 not in (None, \'\') else (args.nsp_dir or SWITCH2 or NSP_DIR)\n    if not folder: folder = ask(\'Switch 2 folder (exefs and romfs): \', \'the Switch 2 folder (--switch2)\')\n    return folder, None, None, dlc\n\n\ndef switch2mod():\n    p = Path(__file__).resolve().parent / \'switch2_build.py\'\n    spec = importlib.util.spec_from_file_location(\'switch2_build\', p)\n    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n    return mod\n\n\ndef layout_build(args, nsp_dir, g, shadowed, mac_pack, prepare_xcode, keep, repo, env, mac):\n    """Work folder, IPA path, Xcode project and saved signing. Shared by the NSP path and a Switch 2 dump."""\n    work = Path(os.path.expanduser(args.work_dir)).resolve() if args.work_dir else nsp_dir / f\'{g.id}_build\'   # the folder holding the NSPs by default; --work-dir puts the build elsewhere\n    # out_ipa reads `work`, so `work` must be assigned first (upstream\'s order had them the other\n    # way round; the --work-dir patch moved `work` up here and this line had to follow it).\n    out_ipa = Path(os.path.expanduser(args.out or OUT_IPA)).resolve() if (args.out or OUT_IPA) else work.parent / g.get(\'ipa\')   # by default next to the NSPs and <id>_build/\n    if shadowed and not (args.out or OUT_IPA): out_ipa = out_ipa.with_name(out_ipa.stem + \'-provisional.ipa\')   # never over the IPA of the game\'s real profile\n    if mac_pack and (args.out or OUT_IPA) and out_ipa.suffix != \'.app\': raise BuildError(f\'the Mac app (-o / OUT_IPA) must end in .app: {out_ipa}\')\n    project = work / \'xcode/DmIOS.xcodeproj\'\n    workspace = Path(os.path.expanduser(args.xcode_workspace or XCODE_WORKSPACE)).resolve() if (args.xcode_workspace or XCODE_WORKSPACE) else nsp_dir.parent / \'NxNative.xcworkspace\'\n    if prepare_xcode and workspace.suffix != \'.xcworkspace\': raise BuildError(\'--xcode-workspace must end in .xcworkspace\')\n    saved_signing = {}\n    if prepare_xcode:\n        sys.path.insert(0, str(repo / \'dm/ios/tools\')); import xcode_projects\n        try:\n            saved_signing = xcode_projects.signing_settings(project)\n            # Preserve the user\'s selections when migrating the earlier single generated project.\n            legacy_config = repo / \'dm/ios/Gen/dm.xcconfig\'\n            if not saved_signing and legacy_config.exists() and f\'/gen/{g.id}/\' in legacy_config.read_text():\n                saved_signing = xcode_projects.signing_settings(repo / \'dm/ios/DmIOS.xcodeproj\')\n        except ValueError as e: raise BuildError(str(e)) from e\n    say(\'Mac app: the Mac shaders and the Mac host, packed into a Mac app; no iOS shaders, Xcode project or IPA\' if mac_pack else \'Mac-only build for development: the Mac shaders and the Mac host, no iOS shaders, Xcode project or IPA\' if mac else f\'Xcode project: {project}\' if prepare_xcode else f\'output IPA: {out_ipa}\')\n    say(f\'work folder (extracted game files in extract/, {"kept for the next build" if keep else "deleted after the IPA is built"}): {work}\')\n    xlate = repo / \'dm/gfx/tools/dmsh_xlate_bin/dmsh_xlate\'\n    # DM_GAME picks the profile; DM_ROOT / WORK point at <id>_build: the tools\' default paths and temporary directories all stay inside it\n    env.update(DM_GAME=g.id, DM_ROOT=str(work), WORK=str(work / \'tmp\'))\n    logs = work / \'logs\'\n    check_prereqs(env, xlate, mac)\n    return types.SimpleNamespace(work=work, out_ipa=out_ipa, project=project, workspace=workspace, saved_signing=saved_signing, logs=logs)\n\n\ndef prepare_switch2(args, repo, env, py, gamemod, mac, mac_pack, prepare_xcode, keep):\n    """Stage a Switch 2 dump into extract/ and resolve the profile. None means --survey-only: the caller returns."""\n    sw = switch2mod()\n    folder, exefs, romfs, dlc = switch2_paths(args)\n    try: dump = sw.load(folder, exefs, romfs, dlc)\n    except sw.DumpError as e: raise BuildError(str(e)) from e\n    app, nsp_dir = dump.app, dump.root\n    say(f\'Switch 2: title {app:016X}\' + (f\', {dump.name}\' if dump.name else \'\') + (f\', {dump.display}\' if dump.display else \'\'))\n    for ver in sorted(dump.versions, key=vnum): say(f\'  {ver}: {dump.versions[ver].root}\')\n    if dump.dlc: say(\'  DLC indices: \' + \', \'.join(str(i) for i, _ in dump.dlc))\n    for w in dump.warnings: say(f\'[RED] {w}\')\n    loose_name, loose_display = dump.name, dump.display\n    newest = dump.versions[max(dump.versions, key=vnum)]\n    if newest.nacp:\n        loose_name = nacp.name(newest.nacp) or loose_name\n        loose_display = nacp.version(newest.nacp) or loose_display\n    force = args.provisional or PROVISIONAL\n    g, provisional, shadowed = None if force else gamemod.by_title(app), False, False\n    alias = None\n    if g is None and not force:\n        main_nso = newest.exefs / \'main\' if (newest.exefs / \'main\').is_file() else newest.exefs / \'main.nso\'\n        bid = build_id(main_nso) if main_nso.is_file() else None\n        found = gamemod.by_build(bid) if bid else None\n        if found:\n            ver = next(ver for ver, b in found.builds().items() if b.get(\'main\') == bid)\n            g, alias = found, (found, ver, bid)\n            say(f\'no game profile for {app:016X}, but its main build {bid} is game profile {g.id}\\\'s {ver} ({g.get("name", "")}) under another title ID\')\n    if g is None:\n        missing = [m for m in (\'lz4.block\', \'capstone\') if not _importable(m)]\n        if missing: raise BuildError(f\'the current Python ({sys.executable}) is missing modules: {", ".join(missing)}\')\n        if not shutil.which(\'dotnet\', path=env[\'PATH\']): raise BuildError(\'dotnet not found (.NET SDK 10)\')\n        pdir = Path(os.path.expanduser(args.profile_out or PROFILE_OUT)).resolve() if (args.profile_out or PROFILE_OUT) else nsp_dir / f\'{app:016X}_build\' / \'profile\'\n        if not (args.survey_only or SURVEY_ONLY): rmtree(derived_key(pdir / f\'{app:016X}\'))\n        if (pdir / f\'{app:016X}\' / \'game.tsv\').exists() and not (args.survey_only or SURVEY_ONLY):\n            say(f\'no game profile for {app:016X}: reusing the provisional profile from the last run (delete it to survey again)\')\n        else:\n            ver = args.package or PACKAGE or max(dump.versions, key=vnum)\n            if ver not in dump.versions: raise BuildError(f\'the version to pack (-p / PACKAGE) is not in the dump, which holds {", ".join(sorted(dump.versions, key=vnum))}\')\n            c = dump.versions[ver]\n            header = [f\'# Survey: {loose_name or "?"} ({app:016X})\', \'\', f\'- display version: {loose_display or "?"}\', f\'- Switch 2 dump: `{c.root}`\', f\'- version: {ver}\']\n            header += [f\'- DLC index {i}: `{rom}`\' for i, rom in dump.dlc]\n            PROG.plan(2)\n            facts = survey_body(nsp_dir, app, c.exefs, c.romfs, header, bool(dump.dlc), loose_name, loose_display or \'?\', ver, repo, py, env)\n            if args.survey_only or SURVEY_ONLY: return None\n            facts[\'survey\'] = str(nsp_dir / f\'{app:016X}_survey\' / \'survey.md\')\n            write_profile(pdir / f\'{app:016X}\', app, facts)\n        os.environ[\'DM_GAMES_DIR\'] = env[\'DM_GAMES_DIR\'] = os.pathsep.join([str(pdir)] + [d for d in os.environ.get(\'DM_GAMES_DIR\', \'\').split(os.pathsep) if d and d != str(pdir)])\n        if force and gamemod.by_title(app):\n            os.environ[\'DM_GAMES_FIRST\'] = env[\'DM_GAMES_FIRST\'] = \'1\'; shadowed = True\n        g, provisional = gamemod.load(f\'{app:016X}\'), True\n        say(\'[RED] provisional game profile: values guessed from the survey, the game may not boot; bring it up per docs/agents after packing\')\n    say(f\'game profile: {g.id}{" (provisional: " + str(g.dir) + ")" if provisional else ""}\')\n    builds, need = g.builds(), g.words(\'shader_versions\')\n    derived = False\n    if provisional:\n        pkg_ver = args.package or PACKAGE or g.get(\'package\')\n        if pkg_ver not in builds: raise BuildError(f"the version to pack (-p / PACKAGE) must be one the game profile registers: {\', \'.join(builds)}")\n        if pkg_ver not in dump.versions: raise BuildError(f\'the dump has no {pkg_ver} (it holds {", ".join(sorted(dump.versions, key=vnum))})\')\n    else:\n        pkg_ver = args.package or PACKAGE or max(dump.versions, key=vnum)\n        if pkg_ver not in dump.versions: raise BuildError(f\'the version to pack (-p / PACKAGE) is not in the dump, which holds {", ".join(sorted(dump.versions, key=vnum))}\')\n        if pkg_ver not in builds:\n            registered_builds = ", ".join(f\'{v} {b["display"]}\' for v, b in builds.items())\n            derived = f\'{pkg_ver} is not a version game profile {g.id} registers ({registered_builds})\'\n        elif not all(v in dump.versions for v in need): derived = f\'the dump has no {", ".join(v for v in need if v not in dump.versions)}, whose files game profile {g.id}\\\'s shader tables were made from\'\n        if alias and not derived: derived = f\'title {app:016X} is not game profile {g.id}\\\'s title {g.title_id:016X}\'\n        if derived: say(f\'{derived}: packing {pkg_ver} from a profile derived from it\')\n    versions = [pkg_ver] if derived else need + ([pkg_ver] if pkg_ver not in need else [])\n    missing = [v for v in versions if v not in dump.versions]\n    if missing: raise BuildError(f\'the dump has no {", ".join(missing)} (it holds {", ".join(sorted(dump.versions, key=vnum))})\')\n    lay = layout_build(args, nsp_dir, g, shadowed, mac_pack, prepare_xcode, keep, repo, env, mac)\n    picks = {v: (None, None if (v == \'v0\' or \'v0\' not in dump.versions) else dump.versions[v].root) for v in versions}\n    ex = lay.work / \'extract\'; ex.mkdir(parents=True, exist_ok=True); (lay.work / \'tmp\').mkdir(exist_ok=True)\n    heapck = provisional and HEAP_CHECK and not args.no_heap_check and not heap_checked(g)\n    PROG.plan(1 + 4 - (prepare_xcode or (mac and not mac_pack)))   # one stage for the dump, then the same tail an NSP build plans after extraction\n    PROG.stage(\'stage game files\', step=\'linking exefs and romfs\')\n    try:\n        dirs = sw.stage(dump, ex, versions, pack_image=bool(getattr(args, \'romfs_image\', False) or ROMFS_IMAGE))\n        aoc = sw.stage_aoc(dump.dlc, lay.work / \'aoc\')\n    except sw.DumpError as e: raise BuildError(str(e)) from e\n    PROG.end(f\'{", ".join(versions)}; DLC {len(dump.dlc)}\')\n    bad = [] if derived else [f\'{v}: {x}\' for v in versions for x in build_mismatch(dirs[v], v, g)]\n    if bad and provisional: raise BuildError(f\'not the builds provisional game profile {g.id} registers: {"; ".join(bad)}\')\n    if bad:\n        derived = f\'not the builds game profile {g.id} registers: {"; ".join(bad)}\'\n        say(f\'{derived}: packing {pkg_ver} from a profile derived from it\')\n        versions, dirs = [pkg_ver], {pkg_ver: dirs[pkg_ver]}\n        picks = {pkg_ver: picks[pkg_ver]}\n    reg = g\n    if derived:\n        pdir = Path(os.path.expanduser(args.profile_out or PROFILE_OUT)).resolve() if (args.profile_out or PROFILE_OUT) else nsp_dir / f\'{app:016X}_build\' / \'profile\'\n        disp = derive_profile(g, pdir / g.id, pkg_ver, dirs[pkg_ver], g.words(\'modules\'), app, display=loose_display or None)\n        os.environ[\'DM_GAMES_DIR\'] = env[\'DM_GAMES_DIR\'] = os.pathsep.join([str(pdir)] + [d for d in os.environ.get(\'DM_GAMES_DIR\', \'\').split(os.pathsep) if d and d != str(pdir)])\n        os.environ[\'DM_GAMES_FIRST\'] = env[\'DM_GAMES_FIRST\'] = \'1\'\n        g = gamemod.load(g.id)\n        say(f\'derived game profile for {pkg_ver} ({disp}): {g.dir}\')\n    return types.SimpleNamespace(app=app, nsp_dir=nsp_dir, g=g, provisional=provisional, shadowed=shadowed, derived=derived, pkg_ver=pkg_ver,\n                                 versions=versions, need=need, work=lay.work, out_ipa=lay.out_ipa, project=lay.project, workspace=lay.workspace,\n                                 saved_signing=lay.saved_signing, picks=picks, ex=ex, dirs=dirs, aoc=aoc, heapck=heapck, logs=lay.logs, reg=reg,\n                                 loose_name=loose_name, loose_display=loose_display)\n\n\ndef main(args=None):\n'),
    ]))
    P.append(('switch2 keys', 'loose = switch2_requested', [
        ("    nsp_dir = Path(os.path.expanduser(args.nsp_dir or NSP_DIR or ask('NSP folder: ', 'the NSP folder'))).resolve()\n    keys = Path(os.path.expanduser(args.keys or PROD_KEYS or ask('path of prod.keys: ', 'prod.keys (-k)'))).resolve()\n    if not nsp_dir.is_dir(): raise BuildError(f'not a folder: {nsp_dir}')\n    if not keys.is_file(): raise BuildError(f'no such file: {keys}')\n", "    loose = switch2_requested(args)\n    if loose:\n        nsp_dir, keys = None, None\n    else:\n        nsp_dir = Path(os.path.expanduser(args.nsp_dir or NSP_DIR or ask('NSP folder: ', 'the NSP folder'))).resolve()\n        keys = Path(os.path.expanduser(args.keys or PROD_KEYS or ask('path of prod.keys: ', 'prod.keys (-k)'))).resolve()\n        if not nsp_dir.is_dir(): raise BuildError(f'not a folder: {nsp_dir}')\n        if not keys.is_file(): raise BuildError(f'no such file: {keys}')\n"),
    ]))
    P.append(('switch2 branch', 'staged = prepare_switch2', [
        ('    app, fam = scan_nsps(nsp_dir)\n    fam = decompress_nsz(nsp_dir, app, fam, keys, env)\n    force =args.provisional or PROVISIONAL   # a provisional profile even when dm/games has one (testing the guesses)\n    g, provisional, shadowed = None if force else gamemod.by_title(app), False, False\n    alias = None if force or g else same_build(nsp_dir, app, fam, keys, repo, env)   # a known build under another title ID: packed from a profile derived from that build\'s\n    if alias:\n        g = alias[0]; say(f\'no game profile for {app:016X}, but its main build {alias[2]} is game profile {g.id}\\\'s {alias[1]} ({g.get("name", "")}) under another title ID\')\n    if g is None:   # no game profile: survey, then (unless --survey-only) a provisional profile outside the repository and a best-effort IPA\n        missing = [m for m in (\'lz4.block\', \'capstone\') if not _importable(m)]\n        if missing: raise BuildError(f\'the current Python ({sys.executable}) is missing modules: {", ".join(missing)}\')\n        if not shutil.which(\'dotnet\', path=env[\'PATH\']): raise BuildError(\'dotnet not found (.NET SDK 10)\')\n        pdir = Path(os.path.expanduser(args.profile_out or PROFILE_OUT)).resolve() if (args.profile_out or PROFILE_OUT) else nsp_dir / f\'{app:016X}_build\' / \'profile\'\n        if not (args.survey_only or SURVEY_ONLY): rmtree(derived_key(pdir / f\'{app:016X}\'))\n        if (pdir / f\'{app:016X}\' / \'game.tsv\').exists() and not (args.survey_only or SURVEY_ONLY):\n            say(f\'no game profile for {app:016X}: reusing the provisional profile from the last run (delete it to survey again)\')\n        else:\n            facts = survey(nsp_dir, app, fam, keys, repo, py, env)\n            if args.survey_only or SURVEY_ONLY: return\n            facts[\'survey\'] = str(nsp_dir / f\'{app:016X}_survey\' / \'survey.md\')\n            write_profile(pdir / f\'{app:016X}\', app, facts)\n        # the profile folder joins the search path of game.py, which the shader tools, make and build_ipa.sh all use (DM_GAMES_DIR)\n        os.environ[\'DM_GAMES_DIR\'] = env[\'DM_GAMES_DIR\'] = os.pathsep.join([str(pdir)] + [d for d in os.environ.get(\'DM_GAMES_DIR\', \'\').split(os.pathsep) if d and d != str(pdir)])\n        if force and gamemod.by_title(app):   # dm/games has the game too: every tool (game.py, make, build_ipa.sh) takes the provisional profile first (DM_GAMES_FIRST)\n            os.environ[\'DM_GAMES_FIRST\'] = env[\'DM_GAMES_FIRST\'] = \'1\'; shadowed = True\n        g, provisional = gamemod.load(f\'{app:016X}\'), True\n        say(\'[RED] provisional game profile: values guessed from the survey, the game may not boot; bring it up per docs/agents after packing\')\n    say(f\'game profile: {g.id}{" (provisional: " + str(g.dir) + ")" if provisional else ""}\')\n    builds, need = g.builds(), g.words(\'shader_versions\')\n    have, derived = nsp_versions(fam, builds), False\n    if provisional:   # the version the survey saw\n        pkg_ver = args.package or PACKAGE or g.get(\'package\')\n        if pkg_ver not in builds: raise BuildError(f"the version to pack (-p / PACKAGE) must be one the game profile registers: {\', \'.join(builds)}")\n    else:   # the newest version in the folder, or the one asked for. The profile is used as it is for a version it registers when every version its shader tables were made from is there;\n        # any other version (no update, another update) is packed from a profile derived from it (derive_profile), with its own shaders\n        pkg_ver = args.package or PACKAGE or max(have, key=vnum)\n        if pkg_ver not in have: raise BuildError(f\'the version to pack (-p / PACKAGE) is not in the folder, which holds {", ".join(sorted(have, key=vnum))}\')\n        if pkg_ver not in builds:\n            registered_builds = ", ".join(f\'{v} {b["display"]}\' for v, b in builds.items())\n            derived = f\'{pkg_ver} is not a version game profile {g.id} registers ({registered_builds})\'\n        elif not all(v in have for v in need): derived = f\'the folder has no {", ".join(v for v in need if v not in have)}, whose files game profile {g.id}\\\'s shader tables were made from\'\n        if alias and not derived: derived = f\'title {app:016X} is not game profile {g.id}\\\'s title {g.title_id:016X}\'\n        if derived: say(f\'{derived}: packing {pkg_ver} from a profile derived from it\')\n    versions = [pkg_ver] if derived else need + ([pkg_ver] if pkg_ver not in need else [])\n    work = Path(os.path.expanduser(args.work_dir)).resolve() if args.work_dir else nsp_dir / f\'{g.id}_build\'   # the folder holding the NSPs by default; --work-dir puts the build elsewhere\n    # out_ipa reads `work`, so `work` must be assigned first (upstream\'s order had them the other\n    # way round; the --work-dir patch moved `work` up here and this line had to follow it).\n    out_ipa = Path(os.path.expanduser(args.out or OUT_IPA)).resolve() if (args.out or OUT_IPA) else work.parent / g.get(\'ipa\')   # by default next to the NSPs and <id>_build/\n    if shadowed and not (args.out or OUT_IPA): out_ipa = out_ipa.with_name(out_ipa.stem + \'-provisional.ipa\')   # never over the IPA of the game\'s real profile\n    if mac_pack and (args.out or OUT_IPA) and out_ipa.suffix != \'.app\': raise BuildError(f\'the Mac app (-o / OUT_IPA) must end in .app: {out_ipa}\')\n    project = work / \'xcode/DmIOS.xcodeproj\'\n    workspace = Path(os.path.expanduser(args.xcode_workspace or XCODE_WORKSPACE)).resolve() if (args.xcode_workspace or XCODE_WORKSPACE) else nsp_dir.parent / \'NxNative.xcworkspace\'\n    if prepare_xcode and workspace.suffix != \'.xcworkspace\': raise BuildError(\'--xcode-workspace must end in .xcworkspace\')\n    saved_signing = {}\n    if prepare_xcode:\n        sys.path.insert(0, str(repo / \'dm/ios/tools\')); import xcode_projects\n        try:\n            saved_signing = xcode_projects.signing_settings(project)\n            # Preserve the user\'s selections when migrating the earlier single generated project.\n            legacy_config = repo / \'dm/ios/Gen/dm.xcconfig\'\n            if not saved_signing and legacy_config.exists() and f\'/gen/{g.id}/\' in legacy_config.read_text():\n                saved_signing = xcode_projects.signing_settings(repo / \'dm/ios/DmIOS.xcodeproj\')\n        except ValueError as e: raise BuildError(str(e)) from e\n    say(\'Mac app: the Mac shaders and the Mac host, packed into a Mac app; no iOS shaders, Xcode project or IPA\' if mac_pack else \'Mac-only build for development: the Mac shaders and the Mac host, no iOS shaders, Xcode project or IPA\' if mac else f\'Xcode project: {project}\' if prepare_xcode else f\'output IPA: {out_ipa}\')\n    say(f\'work folder (extracted game files in extract/, {"kept for the next build" if keep else "deleted after the IPA is built"}): {work}\')\n    xlate = repo / \'dm/gfx/tools/dmsh_xlate_bin/dmsh_xlate\'\n    # DM_GAME picks the profile; DM_ROOT / WORK point at <id>_build: the tools\' default paths and temporary directories all stay inside it\n    env.update(DM_GAME=g.id, DM_ROOT=str(work), WORK=str(work / \'tmp\'))\n    logs = work / \'logs\'\n    check_prereqs(env, xlate, mac)\n    picks = pick_nsps(fam, have, versions)\n\n    ex = work / \'extract\'; ex.mkdir(parents=True, exist_ok=True); (work / \'tmp\').mkdir(exist_ok=True)\n    gb = extract_bytes(picks, False) / 2**30 + 6   # every merged image is complete, including on APFS; shader cache + slack\n    if free_gb(work) < gb and not all((ex / v).exists() for v in versions): raise BuildError(f\'less than {gb:.0f} GB free on the volume of {work}\')\n    heapck = provisional and HEAP_CHECK and not args.no_heap_check and not heap_checked(g)   # the heap check, when the game needs one (once per provisional profile: it stamps the profile)\n    PROG.plan(1 + len(versions) + bool(fam[\'dlc\']) + 4 - (prepare_xcode or (mac and not mac_pack)))   # prepare Xcode (a Mac-only build: the Mac host) replaces app build + IPA pack with one stage (a Mac app: Mac host + Mac app)\n    PROG.stage(\'build nx_extract\')\n    nx = build_nx_extract(repo, work, logs, env)\n    PROG.end()\n    dirs = {}\n    for ver in versions:   # each merged version streams directly into its own image\n        b, u = picks[ver]\n        dirs[ver] = extract(nx, keys, ex, ver, b, u, None, logs, g)\n    bad = [] if derived else [f\'{v}: {x}\' for v in versions for x in build_mismatch(dirs[v], v, g)]   # every module\'s build ID must be the one the profile registers for its version\n    if bad and provisional: raise BuildError(f\'not the builds provisional game profile {g.id} registers: {"; ".join(bad)}\')\n    if bad:   # files of other builds under a registered version\'s tag: packed like any other version\n        derived = f\'not the builds game profile {g.id} registers: {"; ".join(bad)}\'\n        say(f\'{derived}: packing {pkg_ver} from a profile derived from it\')\n        versions, dirs = [pkg_ver], {pkg_ver: dirs[pkg_ver]}\n    reg = g   # the profile as registered (a derived build reports what it lacks against it)\n    if derived:   # the derived profile goes where a provisional one would, and every tool (game.py, make, build_ipa.sh) takes it first (DM_GAMES_FIRST)\n        pdir = Path(os.path.expanduser(args.profile_out or PROFILE_OUT)).resolve() if (args.profile_out or PROFILE_OUT) else nsp_dir / f\'{app:016X}_build\' / \'profile\'\n        disp = derive_profile(g, pdir / g.id, pkg_ver, dirs[pkg_ver], g.words(\'modules\'), app)\n        os.environ[\'DM_GAMES_DIR\'] = env[\'DM_GAMES_DIR\'] = os.pathsep.join([str(pdir)] + [d for d in os.environ.get(\'DM_GAMES_DIR\', \'\').split(os.pathsep) if d and d != str(pdir)])\n        os.environ[\'DM_GAMES_FIRST\'] = env[\'DM_GAMES_FIRST\'] = \'1\'\n        g = gamemod.load(g.id)\n        say(f\'derived game profile for {pkg_ver} ({disp}): {g.dir}\')\n', '    loose_name, loose_display = \'\', \'\'\n    if loose:\n        staged = prepare_switch2(args, repo, env, py, gamemod, mac, mac_pack, prepare_xcode, keep)\n        if staged is None: return\n        app, nsp_dir = staged.app, staged.nsp_dir\n        g, provisional, shadowed, derived = staged.g, staged.provisional, staged.shadowed, staged.derived\n        pkg_ver, versions, need = staged.pkg_ver, staged.versions, staged.need\n        work, out_ipa, project, workspace = staged.work, staged.out_ipa, staged.project, staged.workspace\n        saved_signing, picks, ex, dirs = staged.saved_signing, staged.picks, staged.ex, staged.dirs\n        aoc, heapck, logs, reg = staged.aoc, staged.heapck, staged.logs, staged.reg\n        loose_name, loose_display = staged.loose_name, staged.loose_display\n        xlate = repo / \'dm/gfx/tools/dmsh_xlate_bin/dmsh_xlate\'\n    else:\n        app, fam = scan_nsps(nsp_dir)\n        fam = decompress_nsz(nsp_dir, app, fam, keys, env)\n        force =args.provisional or PROVISIONAL   # a provisional profile even when dm/games has one (testing the guesses)\n        g, provisional, shadowed = None if force else gamemod.by_title(app), False, False\n        alias = None if force or g else same_build(nsp_dir, app, fam, keys, repo, env)   # a known build under another title ID: packed from a profile derived from that build\'s\n        if alias:\n            g = alias[0]; say(f\'no game profile for {app:016X}, but its main build {alias[2]} is game profile {g.id}\\\'s {alias[1]} ({g.get("name", "")}) under another title ID\')\n        if g is None:   # no game profile: survey, then (unless --survey-only) a provisional profile outside the repository and a best-effort IPA\n            missing = [m for m in (\'lz4.block\', \'capstone\') if not _importable(m)]\n            if missing: raise BuildError(f\'the current Python ({sys.executable}) is missing modules: {", ".join(missing)}\')\n            if not shutil.which(\'dotnet\', path=env[\'PATH\']): raise BuildError(\'dotnet not found (.NET SDK 10)\')\n            pdir = Path(os.path.expanduser(args.profile_out or PROFILE_OUT)).resolve() if (args.profile_out or PROFILE_OUT) else nsp_dir / f\'{app:016X}_build\' / \'profile\'\n            if not (args.survey_only or SURVEY_ONLY): rmtree(derived_key(pdir / f\'{app:016X}\'))\n            if (pdir / f\'{app:016X}\' / \'game.tsv\').exists() and not (args.survey_only or SURVEY_ONLY):\n                say(f\'no game profile for {app:016X}: reusing the provisional profile from the last run (delete it to survey again)\')\n            else:\n                facts = survey(nsp_dir, app, fam, keys, repo, py, env)\n                if args.survey_only or SURVEY_ONLY: return\n                facts[\'survey\'] = str(nsp_dir / f\'{app:016X}_survey\' / \'survey.md\')\n                write_profile(pdir / f\'{app:016X}\', app, facts)\n            # the profile folder joins the search path of game.py, which the shader tools, make and build_ipa.sh all use (DM_GAMES_DIR)\n            os.environ[\'DM_GAMES_DIR\'] = env[\'DM_GAMES_DIR\'] = os.pathsep.join([str(pdir)] + [d for d in os.environ.get(\'DM_GAMES_DIR\', \'\').split(os.pathsep) if d and d != str(pdir)])\n            if force and gamemod.by_title(app):   # dm/games has the game too: every tool (game.py, make, build_ipa.sh) takes the provisional profile first (DM_GAMES_FIRST)\n                os.environ[\'DM_GAMES_FIRST\'] = env[\'DM_GAMES_FIRST\'] = \'1\'; shadowed = True\n            g, provisional = gamemod.load(f\'{app:016X}\'), True\n            say(\'[RED] provisional game profile: values guessed from the survey, the game may not boot; bring it up per docs/agents after packing\')\n        say(f\'game profile: {g.id}{" (provisional: " + str(g.dir) + ")" if provisional else ""}\')\n        builds, need = g.builds(), g.words(\'shader_versions\')\n        have, derived = nsp_versions(fam, builds), False\n        if provisional:   # the version the survey saw\n            pkg_ver = args.package or PACKAGE or g.get(\'package\')\n            if pkg_ver not in builds: raise BuildError(f"the version to pack (-p / PACKAGE) must be one the game profile registers: {\', \'.join(builds)}")\n        else:   # the newest version in the folder, or the one asked for. The profile is used as it is for a version it registers when every version its shader tables were made from is there;\n            # any other version (no update, another update) is packed from a profile derived from it (derive_profile), with its own shaders\n            pkg_ver = args.package or PACKAGE or max(have, key=vnum)\n            if pkg_ver not in have: raise BuildError(f\'the version to pack (-p / PACKAGE) is not in the folder, which holds {", ".join(sorted(have, key=vnum))}\')\n            if pkg_ver not in builds:\n                registered_builds = ", ".join(f\'{v} {b["display"]}\' for v, b in builds.items())\n                derived = f\'{pkg_ver} is not a version game profile {g.id} registers ({registered_builds})\'\n            elif not all(v in have for v in need): derived = f\'the folder has no {", ".join(v for v in need if v not in have)}, whose files game profile {g.id}\\\'s shader tables were made from\'\n            if alias and not derived: derived = f\'title {app:016X} is not game profile {g.id}\\\'s title {g.title_id:016X}\'\n            if derived: say(f\'{derived}: packing {pkg_ver} from a profile derived from it\')\n        versions = [pkg_ver] if derived else need + ([pkg_ver] if pkg_ver not in need else [])\n        work = Path(os.path.expanduser(args.work_dir)).resolve() if args.work_dir else nsp_dir / f\'{g.id}_build\'   # the folder holding the NSPs by default; --work-dir puts the build elsewhere\n        # out_ipa reads `work`, so `work` must be assigned first (upstream\'s order had them the other\n        # way round; the --work-dir patch moved `work` up here and this line had to follow it).\n        out_ipa = Path(os.path.expanduser(args.out or OUT_IPA)).resolve() if (args.out or OUT_IPA) else work.parent / g.get(\'ipa\')   # by default next to the NSPs and <id>_build/\n        if shadowed and not (args.out or OUT_IPA): out_ipa = out_ipa.with_name(out_ipa.stem + \'-provisional.ipa\')   # never over the IPA of the game\'s real profile\n        if mac_pack and (args.out or OUT_IPA) and out_ipa.suffix != \'.app\': raise BuildError(f\'the Mac app (-o / OUT_IPA) must end in .app: {out_ipa}\')\n        project = work / \'xcode/DmIOS.xcodeproj\'\n        workspace = Path(os.path.expanduser(args.xcode_workspace or XCODE_WORKSPACE)).resolve() if (args.xcode_workspace or XCODE_WORKSPACE) else nsp_dir.parent / \'NxNative.xcworkspace\'\n        if prepare_xcode and workspace.suffix != \'.xcworkspace\': raise BuildError(\'--xcode-workspace must end in .xcworkspace\')\n        saved_signing = {}\n        if prepare_xcode:\n            sys.path.insert(0, str(repo / \'dm/ios/tools\')); import xcode_projects\n            try:\n                saved_signing = xcode_projects.signing_settings(project)\n                # Preserve the user\'s selections when migrating the earlier single generated project.\n                legacy_config = repo / \'dm/ios/Gen/dm.xcconfig\'\n                if not saved_signing and legacy_config.exists() and f\'/gen/{g.id}/\' in legacy_config.read_text():\n                    saved_signing = xcode_projects.signing_settings(repo / \'dm/ios/DmIOS.xcodeproj\')\n            except ValueError as e: raise BuildError(str(e)) from e\n        say(\'Mac app: the Mac shaders and the Mac host, packed into a Mac app; no iOS shaders, Xcode project or IPA\' if mac_pack else \'Mac-only build for development: the Mac shaders and the Mac host, no iOS shaders, Xcode project or IPA\' if mac else f\'Xcode project: {project}\' if prepare_xcode else f\'output IPA: {out_ipa}\')\n        say(f\'work folder (extracted game files in extract/, {"kept for the next build" if keep else "deleted after the IPA is built"}): {work}\')\n        xlate = repo / \'dm/gfx/tools/dmsh_xlate_bin/dmsh_xlate\'\n        # DM_GAME picks the profile; DM_ROOT / WORK point at <id>_build: the tools\' default paths and temporary directories all stay inside it\n        env.update(DM_GAME=g.id, DM_ROOT=str(work), WORK=str(work / \'tmp\'))\n        logs = work / \'logs\'\n        check_prereqs(env, xlate, mac)\n        picks = pick_nsps(fam, have, versions)\n\n        ex = work / \'extract\'; ex.mkdir(parents=True, exist_ok=True); (work / \'tmp\').mkdir(exist_ok=True)\n        gb = extract_bytes(picks, False) / 2**30 + 6   # every merged image is complete, including on APFS; shader cache + slack\n        if free_gb(work) < gb and not all((ex / v).exists() for v in versions): raise BuildError(f\'less than {gb:.0f} GB free on the volume of {work}\')\n        heapck = provisional and HEAP_CHECK and not args.no_heap_check and not heap_checked(g)   # the heap check, when the game needs one (once per provisional profile: it stamps the profile)\n        PROG.plan(1 + len(versions) + bool(fam[\'dlc\']) + 4 - (prepare_xcode or (mac and not mac_pack)))   # prepare Xcode (a Mac-only build: the Mac host) replaces app build + IPA pack with one stage (a Mac app: Mac host + Mac app)\n        PROG.stage(\'build nx_extract\')\n        nx = build_nx_extract(repo, work, logs, env)\n        PROG.end()\n        dirs = {}\n        for ver in versions:   # each merged version streams directly into its own image\n            b, u = picks[ver]\n            dirs[ver] = extract(nx, keys, ex, ver, b, u, None, logs, g)\n        bad = [] if derived else [f\'{v}: {x}\' for v in versions for x in build_mismatch(dirs[v], v, g)]   # every module\'s build ID must be the one the profile registers for its version\n        if bad and provisional: raise BuildError(f\'not the builds provisional game profile {g.id} registers: {"; ".join(bad)}\')\n        if bad:   # files of other builds under a registered version\'s tag: packed like any other version\n            derived = f\'not the builds game profile {g.id} registers: {"; ".join(bad)}\'\n            say(f\'{derived}: packing {pkg_ver} from a profile derived from it\')\n            versions, dirs = [pkg_ver], {pkg_ver: dirs[pkg_ver]}\n        reg = g   # the profile as registered (a derived build reports what it lacks against it)\n        if derived:   # the derived profile goes where a provisional one would, and every tool (game.py, make, build_ipa.sh) takes it first (DM_GAMES_FIRST)\n            pdir = Path(os.path.expanduser(args.profile_out or PROFILE_OUT)).resolve() if (args.profile_out or PROFILE_OUT) else nsp_dir / f\'{app:016X}_build\' / \'profile\'\n            disp = derive_profile(g, pdir / g.id, pkg_ver, dirs[pkg_ver], g.words(\'modules\'), app)\n            os.environ[\'DM_GAMES_DIR\'] = env[\'DM_GAMES_DIR\'] = os.pathsep.join([str(pdir)] + [d for d in os.environ.get(\'DM_GAMES_DIR\', \'\').split(os.pathsep) if d and d != str(pdir)])\n            os.environ[\'DM_GAMES_FIRST\'] = env[\'DM_GAMES_FIRST\'] = \'1\'\n            g = gamemod.load(g.id)\n            say(f\'derived game profile for {pkg_ver} ({disp}): {g.dir}\')\n'),
    ]))
    P.append(('switch2 aoc', 'if not loose:', [
        ('    aoc = extract_aoc(nx, keys, repo, work, app, fam, logs)\n', '    if not loose:\n        aoc = extract_aoc(nx, keys, repo, work, app, fam, logs)\n'),
    ]))
    P.append(('switch2 names', "nacp.name(base / 'control.nacp') or loose_name", [
        ("    app_name = args.app_name or APP_NAME or nacp.name(base / 'control.nacp')\n", "    app_name = args.app_name or APP_NAME or nacp.name(base / 'control.nacp') or loose_name\n"),
        ("    app_version = nacp.version(game / 'control.nacp') or nacp.version(base / 'control.nacp')\n", "    app_version = nacp.version(game / 'control.nacp') or nacp.version(base / 'control.nacp') or loose_display\n"),
    ]))
    for name, marker, pairs in P:
        if marker in t:
            skip(f'NSP script {name} (already patched)'); continue
        for old, new in pairs:
            if old not in t:
                fail(f'scripts/build_ipa_from_nsp.py: patch "{name}" anchor missing')
            t = t.replace(old, new, 1)
        done(f'NSP script {name}')
    return t

NVM = 'dm/gfx/nvn/nvn_mtl.m'
def patch_nvn_mtl(t):
    # BC decode for GPUs without BC support (six hunks). The fork's two save-load freeze hunks (NX2Apple
    # issue #4: 'up_begin publish' and 'finish publish') are gone: upstream has both (the up_begin
    # rec_publish and the mtl_finish_gpu on_sub_thread() rec_publish, b87a0cc; checked against
    # seedds/main for issue #92), and the carried hunks only said "already patched".
    P = [
        # BC decode on GPUs without BC support (profile tex_bc_cpu; nvn_bcdec.c is an OURS file)
        ('bc cpu globals', ['g_bc_cpu;'], [(
            'static id<MTLDevice> g_dev;',
            '''static id<MTLDevice> g_dev;
static int g_bc_gpu = 1;   // the device advertises BC texture compression (set where the device is made)
static int g_bc_cpu;       // decode BC on the CPU: no GPU support and the profile allows it''')]),
        ('bc cpu include', ['nvn_bcdec.h'], [(
            '#include "../tex/nvn_fmt_mtl.h"',
            '#include "../tex/nvn_fmt_mtl.h"\n#include "../tex/nvn_bcdec.h"   // BC on the CPU for GPUs without BC support (profile tex_bc_cpu)')]),
        ('bc cpu detect', ['supportsBCTextureCompression'], [(
            '''    g_dev = MTLCreateSystemDefaultDevice();
    if (!g_dev) MDIE("no Metal device");
    if (!g_dev.hasUnifiedMemory) MDIE("needs a unified-memory device (zero-copy memory pools)");''',
            '''    g_dev = MTLCreateSystemDefaultDevice();
    if (!g_dev) MDIE("no Metal device");
    if (!g_dev.hasUnifiedMemory) MDIE("needs a unified-memory device (zero-copy memory pools)");
    g_bc_gpu = [g_dev supportsBCTextureCompression];
    g_bc_cpu = !g_bc_gpu && DM_GAME_TEX_BC_CPU;
    if (g_bc_cpu) MLOG("BC textures: this GPU has no BC support, decoding them on the CPU (profile tex_bc_cpu)");''')]),
        # BC decode on the CPU when the GPU has no BC support (profile tex_bc_cpu): the texture is
        # created uncompressed, and the upload expands the blocks before the blit.
        ('bc cpu tex_desc', ['nvn_bc_unpacked(t->d.format, &bc_unpacked'], [(
            '    uint32_t pf = 0; uint8_t swz[4];\n    if (!nvn_fmt_to_mtl(t->d.format, &pf, swz) || !pf) {',
            '    uint32_t pf = 0; uint8_t swz[4];\n    // A BC format on a GPU without BC support: create the texture in the uncompressed format the\n    // decoder produces, and let the upload path fill it through nvn_bc_decode (below). Without\n    // this the Metal format is a BC one the device cannot allocate, and every such texture is nil.\n    uint32_t bc_unpacked = 0; int bc_bpp = 0;\n    if (g_bc_cpu && nvn_bc_unpacked(t->d.format, &bc_unpacked, &bc_bpp)) pf = bc_unpacked;\n    if (!pf && (!nvn_fmt_to_mtl(t->d.format, &pf, swz) || !pf)) {')]),
        ('bc cpu upload', ['nvn_bc_decode(t->d.format'], [(
            '    up_begin();\n    id<MTLBuffer> stg = [g_dev newBufferWithLength:total ? total : 16 options:MTLResourceStorageModeShared];\n    res_add(stg); [g_up_keep addObject:stg];\n    size_t off = 0;\n    int zb = zblock_on() && t->d.format >= 0x79 && t->d.format <= 0x94; if (zb) { g_zb_ser = t->serial; g_zb_n = (int)MIN(d.mipmapLevelCount, 16u); memset(g_zb, 0, sizeof g_zb); memset(g_zb_tot, 0, sizeof g_zb_tot); }\n    for (unsigned l = 0; l < d.mipmapLevelCount; l++) {\n        uint32_t w = MAX(t->d.width >> l, 1u), h = MAX(t->d.height >> l, 1u), z = t->d.target == 2 ? MAX(t->d.depth >> l, 1u) : 1;\n        size_t pitch = (size_t)((w + bw - 1) / bw) * bpb, rows = (h + bh - 1) / bh;\n        for (unsigned y = 0; y < layers; y++) {\n            if (nvn_deswizzle(&t->d, l, y, src, (uint8_t *)stg.contents + off, pitch)) { RED_ONCE(9, "deswizzle failed format %#x target %d", t->d.format, t->d.target); break; }\n            if (zb && l < 16) { const uint64_t *q = (const uint64_t *)((const uint8_t *)stg.contents + off); size_t nb = pitch * rows * z / 16;\n                for (size_t i = 0; i < nb; i++) g_zb[l] += !(q[2 * i] | q[2 * i + 1]);\n                g_zb_tot[l] += (uint32_t)nb; }\n            up_blit_add(stg, off, pitch, pitch * rows, MTLSizeMake(w, h, z), mt, y, l);\n',
            '    // A BC texture on a GPU without BC support: tex_desc made the Metal texture RGBA8 (or R8/RG8,\n    // RGBA16F for BC6H), so the staging slot is per-pixel, not per-block, and the deswizzled block\n    // data is expanded through nvn_bc_decode before the blit. bc_cpu stays 0 on Macs and A19+,\n    // where the compressed path below is used unchanged.\n    uint32_t bc_pf = 0; int bc_bpp = 0;\n    int bc_cpu = g_bc_cpu && nvn_bc_unpacked(t->d.format, &bc_pf, &bc_bpp);\n    size_t total_bc = 0;\n    if (bc_cpu) {\n        total_bc = 0;\n        for (unsigned l = 0; l < d.mipmapLevelCount; l++) {\n            uint32_t w = MAX(t->d.width >> l, 1u), h = MAX(t->d.height >> l, 1u), z = t->d.target == 2 ? MAX(t->d.depth >> l, 1u) : 1;\n            total_bc += (size_t)w * h * z * layers * (size_t)bc_bpp;\n        }\n    }\n    up_begin();\n    id<MTLBuffer> stg = [g_dev newBufferWithLength:(bc_cpu ? total_bc : total) ? (bc_cpu ? total_bc : total) : 16 options:MTLResourceStorageModeShared];\n    res_add(stg); [g_up_keep addObject:stg];\n    size_t off = 0;\n    int zb = zblock_on() && t->d.format >= 0x79 && t->d.format <= 0x94; if (zb) { g_zb_ser = t->serial; g_zb_n = (int)MIN(d.mipmapLevelCount, 16u); memset(g_zb, 0, sizeof g_zb); memset(g_zb_tot, 0, sizeof g_zb_tot); }\n    // CPU-side scratch for the compressed block data when bc_cpu (one level-slice at a time)\n    uint8_t *bc_tmp = NULL; size_t bc_tmp_sz = 0;\n    for (unsigned l = 0; l < d.mipmapLevelCount; l++) {\n        uint32_t w = MAX(t->d.width >> l, 1u), h = MAX(t->d.height >> l, 1u), z = t->d.target == 2 ? MAX(t->d.depth >> l, 1u) : 1;\n        size_t pitch = (size_t)((w + bw - 1) / bw) * bpb, rows = (h + bh - 1) / bh;\n        for (unsigned y = 0; y < layers; y++) {\n            if (bc_cpu) {\n                size_t need = pitch * rows * z;\n                if (need > bc_tmp_sz) { free(bc_tmp); bc_tmp = malloc(need); bc_tmp_sz = need; if (!bc_tmp) MDIE("BC decode scratch of %zu bytes failed", need); }\n                if (nvn_deswizzle(&t->d, l, y, src, bc_tmp, pitch)) { RED_ONCE(9, "deswizzle failed format %#x target %d", t->d.format, t->d.target); break; }\n                size_t dst_pitch = (size_t)w * bc_bpp;\n                for (uint32_t zz = 0; zz < z; zz++)\n                    nvn_bc_decode(t->d.format, bc_tmp + (size_t)zz * pitch * rows, pitch, w, h,\n                                  (uint8_t *)stg.contents + off + (size_t)zz * dst_pitch * h, dst_pitch);\n                up_blit_add(stg, off, dst_pitch, dst_pitch * h, MTLSizeMake(w, h, z), mt, y, l);\n                off += dst_pitch * h * z;\n                continue;\n            }\n            if (nvn_deswizzle(&t->d, l, y, src, (uint8_t *)stg.contents + off, pitch)) { RED_ONCE(9, "deswizzle failed format %#x target %d", t->d.format, t->d.target); break; }\n            if (zb && l < 16) { const uint64_t *q = (const uint64_t *)((const uint8_t *)stg.contents + off); size_t nb = pitch * rows * z / 16;\n                for (size_t i = 0; i < nb; i++) g_zb[l] += !(q[2 * i] | q[2 * i + 1]);\n                g_zb_tot[l] += (uint32_t)nb; }\n            up_blit_add(stg, off, pitch, pitch * rows, MTLSizeMake(w, h, z), mt, y, l);\n')]),
        ('bc cpu free', ['free(bc_tmp);\n    free(part);'], [(
            "    free(part);\n    up_flush();\n    return mt;",
            "    free(bc_tmp);\n    free(part);\n    up_flush();\n    return mt;")]),
    ]
    for name, markers, pairs in P:
        if any(m in t for m in markers):
            skip(f'nvn_mtl.m {name} (already patched)'); continue
        for old, new in pairs:
            if old not in t: fail(f'{NVM}: patch "{name}" anchor missing: {old[:70]!r}')
            t = t.replace(old, new, 1)
        done(f'nvn_mtl.m {name}')
    return t

MAIN_M_ROMFS = ('''static NSString *romfs_locate(void) {
    NSFileManager *fm = NSFileManager.defaultManager;
    NSString *doc = NSSearchPathForDirectoriesInDomains(NSDocumentDirectory, NSUserDomainMask, YES)[0];
    NSString *img = [doc stringByAppendingPathComponent:@"romfs.img"];
    if (romfs_image_complete(img)) {
        // A folder copy from before images is now dead weight of up to many GB (often cut off partway). Deleting its 300,000 files here took longer than iOS allows an app to start
        // (Tears of the Kingdom: killed on a black screen), so it is moved aside at once and deleted in the background, leftovers of an interrupted delete included
        NSString *old = [doc stringByAppendingPathComponent:@"romfs"], *tmp = NSTemporaryDirectory();
        if ([fm fileExistsAtPath:old] && [fm moveItemAtPath:old toPath:[tmp stringByAppendingPathComponent:[@"romfs-old-" stringByAppendingString:NSUUID.UUID.UUIDString]] error:nil])
            fprintf(stderr, "boot: Documents/romfs moved aside for deletion (Documents/romfs.img replaces it)\\n");
        dispatch_async(dispatch_get_global_queue(QOS_CLASS_BACKGROUND, 0), ^{
            NSFileManager *bfm = [NSFileManager new];
            for (NSString *n in [bfm contentsOfDirectoryAtPath:tmp error:nil]) if ([n hasPrefix:@"romfs-old-"]) [bfm removeItemAtPath:[tmp stringByAppendingPathComponent:n] error:nil];
        });
        return img;
    }
    NSString *bundledImage = [NSBundle.mainBundle.resourcePath stringByAppendingPathComponent:@"romfs.img"];
    NSString *documentFolder = [doc stringByAppendingPathComponent:@"romfs"];
    if ([fm fileExistsAtPath:[documentFolder stringByAppendingPathComponent:@DM_GAME_ROMFS_MARKER]]) return documentFolder;
    if (romfs_image_complete(bundledImage)) return bundledImage;
    for (NSString *d in @[[doc stringByAppendingPathComponent:@"romfs"], [NSBundle.mainBundle.resourcePath stringByAppendingPathComponent:@"romfs"]])
        if ([fm fileExistsAtPath:[d stringByAppendingPathComponent:@DM_GAME_ROMFS_MARKER]]) return d;   // only a directory containing the game profile\'s romfs_marker path counts (Mario: System/Resource, the resource size table)
    return nil;
}

''', '''// A folder holds this game\'s files if it has a ROMFSARC index or the profile\'s marker path.
// Shared by romfs_locate and the per-game layout below, so both answer the same question.
static BOOL romfs_dir_ok(NSString *d) {
    NSFileManager *fm = NSFileManager.defaultManager;
    // A ROMFSARC archive: <dir>/romfs.idx next to romfs.NN shards. The host opens it when DM_ROMFS
    // points at the base; the marker check does not apply (there are no directories to walk).
    if ([fm fileExistsAtPath:[d stringByAppendingPathComponent:@"romfs.idx"]]) return YES;
    // A plain directory tree: only one containing the game profile\'s romfs_marker path counts
    // (Mario: System/Resource, the resource size table). Runtime profile: the marker is a profile
    // value now; the compiled macro is only what dm_profile() falls back to without DM_PROFILE.
    return [fm fileExistsAtPath:[d stringByAppendingPathComponent:@(dm_profile()->romfs_marker)]];
}

// Where one game\'s data lives under the per-game layout: Documents/games/<title ID>/, holding
//   romfs.img   the whole RomFS as one image file (what scripts/push_romfs.sh sends), or
//   romfs/      a ROMFSARC archive or a plain tree, for copies made before images
//   msl/        this game\'s Metal shader libraries (optional; the app\'s own dm/ is used otherwise)
//   save/ temp/ this game\'s saves and scratch space
// The layout exists so several games can share one installed app and so the game data can sit on
// an external drive the Files app exposes (a folder copied there keeps this shape).
static NSString *game_dir_for(const char *title_id) {
    NSString *doc = NSSearchPathForDirectoriesInDomains(NSDocumentDirectory, NSUserDomainMask, YES)[0];
    return [[doc stringByAppendingPathComponent:@"games"] stringByAppendingPathComponent:@(title_id)];
}

static NSString *romfs_locate(void) {
    NSFileManager *fm = NSFileManager.defaultManager;
    NSString *doc = NSSearchPathForDirectoriesInDomains(NSDocumentDirectory, NSUserDomainMask, YES)[0];
    // What the player picked in the Files app wins over everything: it is the one path they chose
    // on purpose, possibly on an external drive, and it costs nothing when they never picked one
    // (the bookmark is absent and this falls straight through). An image or a folder both work,
    // and a pick that no longer holds this game's data falls through rather than failing the boot.
    {
        BOOL stale = NO;
        NSString *picked = dm_picked_data_path(&stale);
        if (stale) fprintf(stderr, "boot: the picked game-data location needs to be chosen again (the app is signed differently now)\\n");
        if (picked.length) {
            if (romfs_image_complete(picked)) { fprintf(stderr, "boot: game data from the picked image %s\\n", picked.UTF8String); return picked; }
            NSString *pimg = [picked stringByAppendingPathComponent:@"romfs.img"];
            if (romfs_image_complete(pimg)) { fprintf(stderr, "boot: game data from the picked folder %s\\n", pimg.UTF8String); return pimg; }
            if (romfs_dir_ok(picked)) { fprintf(stderr, "boot: game data from the picked folder %s\\n", picked.UTF8String); return picked; }
            fprintf(stderr, "boot: the picked location %s holds no game data for this title; using the app's own\\n", picked.UTF8String);
        }
    }
    // Per-game first: Documents/games/<id>/romfs.img (one file, what push_romfs.sh sends) or
    // Documents/games/<id>/romfs/ — the launcher-style layout, and what lets a user keep several
    // games\' data side by side or on an external drive. The marker check inside romfs_dir_ok is
    // what keeps a stray folder from being taken for this game.
    NSString *gdir = game_dir_for(dm_profile()->id);
    NSString *gimg = [gdir stringByAppendingPathComponent:@"romfs.img"];
    if (romfs_image_complete(gimg)) return gimg;
    NSString *gfolder = [gdir stringByAppendingPathComponent:@"romfs"];
    if ([fm fileExistsAtPath:gfolder] && romfs_dir_ok(gfolder)) return gfolder;
    NSString *img = [doc stringByAppendingPathComponent:@"romfs.img"];
    if (romfs_image_complete(img)) {
        // A folder copy from before images is now dead weight of up to many GB (often cut off partway). Deleting its 300,000 files here took longer than iOS allows an app to start
        // (Tears of the Kingdom: killed on a black screen), so it is moved aside at once and deleted in the background, leftovers of an interrupted delete included
        NSString *old = [doc stringByAppendingPathComponent:@"romfs"], *tmp = NSTemporaryDirectory();
        if ([fm fileExistsAtPath:old] && [fm moveItemAtPath:old toPath:[tmp stringByAppendingPathComponent:[@"romfs-old-" stringByAppendingString:NSUUID.UUID.UUIDString]] error:nil])
            fprintf(stderr, "boot: Documents/romfs moved aside for deletion (Documents/romfs.img replaces it)\\n");
        dispatch_async(dispatch_get_global_queue(QOS_CLASS_BACKGROUND, 0), ^{
            NSFileManager *bfm = [NSFileManager new];
            for (NSString *n in [bfm contentsOfDirectoryAtPath:tmp error:nil]) if ([n hasPrefix:@"romfs-old-"]) [bfm removeItemAtPath:[tmp stringByAppendingPathComponent:n] error:nil];
        });
        return img;
    }
    NSString *bundledImage = [NSBundle.mainBundle.resourcePath stringByAppendingPathComponent:@"romfs.img"];
    NSString *documentFolder = [doc stringByAppendingPathComponent:@"romfs"];
    if ([fm fileExistsAtPath:[documentFolder stringByAppendingPathComponent:@(dm_profile()->romfs_marker)]]) return documentFolder;
    if (romfs_image_complete(bundledImage)) return bundledImage;
    for (NSString *d in @[[doc stringByAppendingPathComponent:@"romfs"], [NSBundle.mainBundle.resourcePath stringByAppendingPathComponent:@"romfs"]])
        if (romfs_dir_ok(d)) return d;
    return nil;
}

''')


MAIN_M_ENV = ('    setenv("DM_ROMFS", romfs.fileSystemRepresentation, 0);\n    setenv("DM_SAVE", [doc stringByAppendingPathComponent:@"save"].fileSystemRepresentation, 0);\n    setenv("DM_TEMP", [doc stringByAppendingPathComponent:@"temp"].fileSystemRepresentation, 0);\n    NSString *dm = [res stringByAppendingPathComponent:@"dm"];\n    setenv("DM_NVN_SHMAP", [dm stringByAppendingPathComponent:@"shader_map.tsv"].fileSystemRepresentation, 0);\n    setenv("DM_MSL_DIR", [res stringByAppendingPathComponent:@"msl"].fileSystemRepresentation, 0);\n', '    setenv("DM_ROMFS", romfs.fileSystemRepresentation, 0);\n    // Saves and scratch space: Documents/games/<id>/{save,temp} when the per-game layout is in use\n    // (so two games in one app never share a save folder), else the old Documents/{save,temp}. An\n    // env file still wins — DM_SAVE/DM_TEMP set there are already in the environment and\n    // setenv(..., 0) leaves them alone.\n    NSString *gdir = game_dir_for(dm_profile()->id);\n    // Tie the data folder to where the game files actually came from: if romfs_locate fell back to\n    // Documents/romfs, the saves must stay beside it (a game folder without romfs is not in use).\n    BOOL per_game = [romfs hasPrefix:gdir];\n    NSString *save_dir = per_game ? [gdir stringByAppendingPathComponent:@"save"] : [doc stringByAppendingPathComponent:@"save"];\n    NSString *temp_dir = per_game ? [gdir stringByAppendingPathComponent:@"temp"] : [doc stringByAppendingPathComponent:@"temp"];\n    for (NSString *d in @[save_dir, temp_dir]) [fm createDirectoryAtPath:d withIntermediateDirectories:YES attributes:nil error:nil];\n    setenv("DM_SAVE", save_dir.fileSystemRepresentation, 0);\n    setenv("DM_TEMP", temp_dir.fileSystemRepresentation, 0);\n    if (per_game) fprintf(stderr, "boot: per-game data folder %s\\n", gdir.fileSystemRepresentation);\n    NSString *dm = [res stringByAppendingPathComponent:@"dm"];\n    setenv("DM_NVN_SHMAP", [dm stringByAppendingPathComponent:@"shader_map.tsv"].fileSystemRepresentation, 0);\n    // Shader libraries: a game\'s own <gdir>/msl first (a per-game layout may carry its own), else\n    // the app\'s shared <App>/msl.\n    NSString *per_game_msl = [gdir stringByAppendingPathComponent:@"msl"];\n    NSString *msl = (per_game && [fm fileExistsAtPath:per_game_msl]) ? per_game_msl : [res stringByAppendingPathComponent:@"msl"];\n    setenv("DM_MSL_DIR", msl.fileSystemRepresentation, 0);\n')

def patch_main_m(t):
    """romfs_locate + the per-game data layout in dm/ios/Sources/main.m.

    Two replacements, both anchored on upstream text:
    - romfs_locate becomes romfs_dir_ok() + game_dir_for() + a locate that tries
      Documents/games/<id>/romfs first (the launcher-style layout, which is also what lets the
      game data sit in a folder the Files app exposes on an external drive).
    - the DM_ROMFS/DM_SAVE/DM_TEMP/DM_MSL_DIR block gains the per-game folders and the shader
      library fallback.
    A missing anchor is an error: these are the fork's only two changes here.
    """
    P = [
        # the runtime profile replaced the compiled header: hle_profile.h gives dm_profile(), which
        # the marker check below reads (merged with the stage-2 experiment, 21fbba1)
        ('runtime profile include',
         ('#include "game_profile.h"   // game profile constants (the RomFS marker path)',
          '#include "hle_profile.h"   // the runtime game profile (DM_PROFILE): the RomFS marker path comes from it')),
        # the Files-app pick (DataBookmark.m): romfs_locate asks it before anything else
        ('picked data include',
         ('#include "romfs_image.h"\n', '#include "romfs_image.h"\n#include "DataBookmark.h"\n')),
        ('romfs per-game', MAIN_M_ROMFS),
        ('per-game data dirs', MAIN_M_ENV),
    ]
    for name, (old, new) in P:
        if new in t:
            skip(f'main.m {name} (already patched)')
            continue
        if old not in t:
            fail(f'dm/ios/Sources/main.m: patch "{name}" anchor missing (upstream moved romfs_locate '
                 'or the environment block)')
        t = t.replace(old, new, 1)
        done(f'main.m {name}')
    return t

def patch_patch_nso(t):
    """The NRO header skip in dm/tools/patch_nso.py (issue #14), as sent upstream in seedds/nxnative#397.

    An NRO's text segment starts at offset 0 with the file's own header in it (Hades' EOSSDK-Switch-Shipping.nro: text
    {0x0, 0xa41000}), so scanning it from 0 read the header's build ID as instructions ("44 ldr x18, #0xfffffffffff40370" on
    Final Fantasy X/X-2 HD Remaster). code_start() is 0x80 for an NRO and 0 for an NSO; code_ranges() starts no code range
    before it. The segment table cannot give the bound (it says 0): the TEXT_LO = segs[0][1] form this patcher applied
    before kept scanning the header, and it used TEXT_LO in the site bound 13 lines before defining it, so every rewrite
    stopped with a NameError. When #397 is merged upstream, every hunk is found applied and this patcher can go.
    """
    if 'def code_start(d):' in t and 'TEXT_LO' not in t: return skip('patch_nso.py NRO header (already patched)')
    P = [
        ('code_start and code_ranges', "src, outdir, NAME = sys.argv[1], sys.argv[2], sys.argv[3]\nd = open(src, 'rb').read()\n",
         'def code_start(d):\n    """Where the code in the text segment starts: 0x80 in an NRO, 0 in an NSO. An NRO\'s text segment begins at file and memory\n    offset 0 with the file\'s own header in it (the start word, the MOD0 offset, NRO0 at +0x10, the segment table at +0x20, the\n    build ID at +0x40), so scanning it from 0 read the header as instructions: the build ID decoded as "ldr x18, #…" and stopped\n    Final Fantasy X/X-2 HD Remaster\'s rewrite (GitHub issue LNPANDY/NX2Apple#14). An NSO\'s text holds code from 0."""\n    return 0x80 if d[0x10:0x14] == b\'NRO0\' else 0\n\ndef code_ranges(data_blocks, text_end, start=0):\n    """The text\'s code ranges: the gaps between its data blocks (nso_code.data_blocks), none of them before `start` (code_start)"""\n    gaps = zip([0] + [e for _, e in data_blocks], [s for s, _ in data_blocks] + [text_end])\n    return [(max(s, start), e) for s, e in gaps if e > max(s, start)]\n\nsrc, outdir, NAME = sys.argv[1], sys.argv[2], sys.argv[3]\nd = open(src, \'rb\').read()\nCODE_START = code_start(d)\n'),
        ('site starts bound', 'starts = sorted(x for x in starts if x < TEXT_END)',
         'starts = sorted(x for x in starts if CODE_START <= x < TEXT_END)'),
        ('code ranges', 'CODE_RANGES = list(zip([0] + [e for _, e in DATA_BLOCKS], [s for s, _ in DATA_BLOCKS] + [TEXT_END]))',
         'CODE_RANGES = code_ranges(DATA_BLOCKS, TEXT_END, CODE_START)'),
    ]
    for name, old, new in P:
        if new in t:
            skip(f'patch_nso.py {name} (already patched)')
            continue
        if old not in t:
            fail(f'dm/tools/patch_nso.py: patch "{name}" anchor missing (upstream reworked the text '
                 'scan; merge the NRO header skip by hand, or take seedds/nxnative#397 if it is merged)')
            continue
        t = t.replace(old, new, 1)
        done(f'patch_nso.py {name}')
    return t

def patch_front_mac(t):
    """DM_KEYMAP and DM_PAD_MAP in the Mac front end, from upstream seedds/nxnative#386.

    The launcher's Controls page (Settings → Controls) writes these two variables, but until this
    landed here nothing on this side read them: the page moved the fields and the game kept its
    default keys. Upstream's #386 is still open, so the change has to live here as a carrier: the
    tables below are its hunks, and the day #386 merges, a sync brings them in whole and this
    patcher finds every anchor already applied (or, if upstream rewords one, fails loudly).

    input_map.h is fork-only for now (upstream's file, but shipped here by this commit) and is in
    OURS so a sync cannot take it away.
    """
    if 'key_map_default(&g_keys)' in t: return skip('front_mac.m DM_KEYMAP (already patched)')
    P = [
        ('key map comment',
         '// Key map (Nintendo layout): WASD left stick, arrow keys D-pad, J=A K=B U=X I=Y, Q=L E=R, Z=ZL C=ZR, F=press left stick G=press right stick, Return=+ Tab=−, Space=shake the controller.\n',
         '// Key map (Nintendo layout): WASD left stick, arrow keys D-pad, J=A K=B U=X I=Y, Q=L E=R, Z=ZL C=ZR, F=press left stick G=press right stick, Return=+ Tab=−, Space=shake the controller;\n'
         "// DM_KEYMAP moves a function to another key (the right stick too), DM_PAD_MAP changes what a controller's buttons send (input_map.h).\n"),
        ('input_map include',
         '#include "pad_slots.h"\n',
         '#include "pad_slots.h"\n#include "input_map.h"\n'),
        ('the four stick flags',
         'static int g_w, g_a, g_s, g_d;\n',
         ''),
        ('key state',
         'static void push(void) {\n    int32_t lx = (g_d - g_a) * 32767, ly = (g_w - g_s) * 32767;\n    dm_input_set(g_btn, lx, ly, 0, 0);\n}\n'
         'static int key_bit(unsigned short code) {\n'
         '    switch (code) {\n'
         '        case 38: return 0;  case 40: return 1;  case 32: return 2;  case 34: return 3;    // J K U I → A B X Y\n'
         '        case 3: return 4;   case 5: return 5;                                            // F G → press stick\n'
         '        case 12: return 6;  case 14: return 7;  case 6: return 8;   case 8: return 9;    // Q E Z C → L R ZL ZR\n'
         '        case 36: return 10; case 48: return 11;                                          // Return Tab → + −\n'
         '        case 123: return 12; case 126: return 13; case 124: return 14; case 125: return 15; // ← ↑ → ↓\n'
         '    }\n'
         '    return -1;\n'
         '}\n',
         '// Keyboard: each key holds one function of the key map (input_map.h; DM_KEYMAP changes it), the held functions make the buttons and both sticks\n'
         'static key_map g_keys;\n'
         'static uint32_t g_held_fn;   // bit f: a key holding function f is down\n'
         'static void push(void) {\n'
         '    uint32_t h = g_held_fn;\n'
         '    #define D(f) ((int32_t)(h >> (f) & 1))\n'
         '    dm_input_set(g_btn, (D(IM_LRIGHT) - D(IM_LLEFT)) * 32767, (D(IM_LUP) - D(IM_LDOWN)) * 32767, (D(IM_RRIGHT) - D(IM_RLEFT)) * 32767, (D(IM_RUP) - D(IM_RDOWN)) * 32767);\n'
         '    #undef D\n'
         '}\n'),
        ('on_key',
         '    unsigned short c = e.keyCode;\n'
         '    switch (c) { case 13: g_w = down; break; case 0: g_a = down; break; case 1: g_s = down; break; case 2: g_d = down; break; }\n'
         '    int b = key_bit(c);\n'
         '    uint64_t m = b >= 0 ? 1ull << b : c == 49 ? DM_BTN_SHAKE : 0;   // Space: shaken while held\n'
         '    g_btn = down ? (g_btn | m) : (g_btn & ~m);\n',
         '    int f = key_map_fn(&g_keys, e.keyCode); if (f < 0) return;\n'
         '    g_held_fn = down ? (g_held_fn | 1u << f) : (g_held_fn & ~(1u << f));\n'
         '    g_btn = (g_held_fn & 0xffff) | (g_held_fn >> IM_SHAKE & 1 ? DM_BTN_SHAKE : 0);\n'),
        ('pad map state',
         'static int g_merge, g_kbd_own;\n',
         'static int g_merge, g_kbd_own;\nstatic pad_map g_padmap;   // DM_PAD_MAP (input_map.h): what each button sends, applied to every controller\n'),
        ('pad map apply',
         '    #undef K\n    dm_pad_set_player(p, m, pad_axis(g.leftThumbstick.xAxis.value), pad_axis(g.leftThumbstick.yAxis.value), pad_axis(g.rightThumbstick.xAxis.value), pad_axis(g.rightThumbstick.yAxis.value));\n',
         '    #undef K\n    m = pad_map_apply(&g_padmap, m);\n    dm_pad_set_player(p, m, pad_axis(g.leftThumbstick.xAxis.value), pad_axis(g.leftThumbstick.yAxis.value), pad_axis(g.rightThumbstick.xAxis.value), pad_axis(g.rightThumbstick.yAxis.value));\n'),
        ('start: read the two variables',
         '    const char *e = getenv("DM_PAD_MERGE"); g_merge = e && atoi(e);\n',
         '    key_map_default(&g_keys); pad_map_default(&g_padmap);\n'
         '    const char *km = getenv("DM_KEYMAP"), *pm = getenv("DM_PAD_MAP");\n'
         '    if (km && *km) { int bad = key_map_parse(&g_keys, km); LOG("front end: keyboard map from DM_KEYMAP (%s)%s", km, bad ? ", malformed entries left out" : ""); }\n'
         '    if (pm && *pm) { int bad = pad_map_parse(&g_padmap, pm); LOG("front end: controller buttons from DM_PAD_MAP (%s)%s", pm, bad ? ", malformed entries left out" : ""); }\n'
         '    const char *e = getenv("DM_PAD_MERGE"); g_merge = e && atoi(e);\n'),
        # two call sites, one per indentation: input_release (4 spaces) and front_text_input (8)
        ('release on focus loss',
         '    g_btn = 0; g_w = g_a = g_s = g_d = 0; push();\n    if (g_mouse_down)',
         '    g_btn = 0; g_held_fn = 0; push();\n    if (g_mouse_down)'),
        ('release for the text box',
         '        g_btn = 0; g_w = g_a = g_s = g_d = 0; push();\n        dm_touch_set(NULL, 0);',
         '        g_btn = 0; g_held_fn = 0; push();\n        dm_touch_set(NULL, 0);'),
    ]
    for name, old, new in P:
        # A deletion entry has an empty `new`, which is "in" every string, so it would report
        # "already patched" on the first run and never delete anything. It is applied when its
        # `old` is gone instead.
        if (old not in t) if not new else (new in t):
            skip(f'front_mac.m {name} (already patched)')
            continue
        if old not in t:
            fail(f'dm/host/front_mac.m: patch "{name}" anchor missing (upstream moved the key handling; '
                 'merge seedds/nxnative#386 by hand)')
        t = t.replace(old, new, 1)
        done(f'front_mac.m {name}')
    return t

def patch_dm_h(t):
    """dm.h pulls in hle_profile.h, so every host source can call dm_profile().

    The runtime-profile work (merged from the stage-2 experiment, 21fbba1) turned the compiled
    game_profile.h constants into values read from a profile at start, and dm_profile() is how
    every source reaches them — but only hle_svc.c and hle_profile.c included the header. It
    compiled then because the other callers were older upstream revisions; after a sync rewrote
    them, loader.c, hle_fs.c, hle_fpslock.c and five more failed with "call to undeclared function
    dm_profile". Including it from dm.h (which they all include) is what keeps the accessor in
    scope without touching each file, and this entry is what keeps it in scope across a sync.
    """
    if 'hle_profile.h' in t: return skip('dm.h hle_profile.h (already patched)')
    old = '#include "game_profile.h"   // constants of the current game profile'
    if old not in t: fail('dm/host/dm.h: game_profile.h include anchor missing')
    line = next(ln for ln in t.splitlines() if ln.startswith(old))
    return done('dm.h hle_profile.h', t.replace(line, line + '\n#include "hle_profile.h"'
        '   // the runtime profile (DM_PROFILE): every constant above can also come from it;'
        ' dm_profile() is the accessor', 1))


ROMFS_TEST_STUB = ('typedef struct { const char *id, *romfs_marker; } fixture_profile;   /* the fork reads both from the runtime profile */\n'
                   'static const fixture_profile *dm_profile(void) { static const fixture_profile p = {"0100000000000000", "marker"}; return &p; }\n')

def patch_test_romfs_startup(t):
    """The test compiles main.m's RomFS locator alone, with a stub for each name it needs. The fork's
    locator reads the game's id and romfs_marker through dm_profile() (the runtime profile, patch_main_m),
    which upstream's stubs do not provide: clang stopped on "call to undeclared function dm_profile".
    The fixture gets a dm_profile() with the marker the test already uses. The same locator asks
    dm_picked_data_path() (DataBookmark.m, the Files-app pick), so the fixture answers it from a variable
    (argv[4]) and a test puts the player's choice anywhere on disk."""
    if 'fixture_profile' in t: skip('test_romfs_startup dm_profile stub (already patched)')
    else:
        old = "#define DM_GAME_ROMFS_MARKER \"marker\"\\n'"
        if old not in t: fail('dm/ios/tests/test_romfs_startup.py: the fixture preamble anchor is missing'); return t
        t = done('test_romfs_startup dm_profile stub', t.replace(old, old + "\n                          " + repr(ROMFS_TEST_STUB), 1))
    return anchored('dm/ios/tests/test_romfs_startup.py', PICKED_EDITS)(t)

def patch_test_profile_pack(t):
    """Upstream packs the RomFS into the IPA; the fork's default is the slim package (patch_build_ipa:
    DM_FULL_ROMFS=1 packs it), so the test checks that the RomFS stays out instead."""
    if 'slim package' in t: return skip('test_profile_pack slim IPA (already patched)')
    old = "                    self.assertEqual(archive.read('Payload/DmIOS.app/romfs/marker'), b'romfs')"
    if old not in t: fail('dm/ios/tests/test_profile_pack.py: the RomFS assertion anchor is missing'); return t
    new = ("                    # the fork's slim package: the RomFS stays out of the IPA (DM_FULL_ROMFS=1 packs it)\n"
           "                    self.assertFalse(any(n.startswith('Payload/DmIOS.app/romfs') for n in names))")
    return done('test_profile_pack slim IPA', t.replace(old, new, 1))


# The switches only the fork's code reads, with their rows in docs/env_switches.tsv (name, class, meaning)
FORK_SWITCHES = {
    'DM_FULL_ROMFS': ('diagnostic', '`=1` in `build_ipa.sh` packs the game data into the IPA as it did before the slim package; the default leaves it in `Documents/games/<id>/` (`romfs.img`, `aoc/<index>.img`) and the DLC beside it. Only for small games — a full package of a large one is many GB and iOS refuses it (`PackageExtractionFailed`)'),
    'DM_PROFILE': ('path', '`=<file>`: the runtime game profile (layout, switches, tables) the host reads at start, which is how one host binary serves every game (the launcher architecture); the compiled `game_profile.h` stays the fallback, and `DM_PROF_<key>` overrides sit on top of it (`hle_profile.h`)'),
    # seedds/nxnative#386 (the Controls page); the rows go when it merges and the table comes from upstream
    'DM_KEYMAP': ('player', 'Mac keyboard: `<name>=<key code>,…` puts a button (`A` … `Down`), a stick direction (`LUp` … `RRight`) or `Shake` on another key (macOS virtual key code, or `none`); one key holds one function (`input_map.h`)'),
    'DM_PAD_MAP': ('player', 'Mac controllers: `<name>=<name>,…` makes the button that sends the first by default send the second (or `none`), e.g. `A=B,B=A` swaps them (`input_map.h`)'),
}

def patch_env_switches(t):
    """docs/env_switches.tsv is upstream's table of every switch the code reads, and test_env_switches.py
    fails when a switch the code reads has no row. The fork's own switches (FORK_SWITCHES) lose their rows
    whenever upstream rewrites the table; they go back in name order."""
    rows = t.rstrip('\n').split('\n')
    have = {r.split('\t', 1)[0] for r in rows}
    missing = [n for n in FORK_SWITCHES if n not in have]
    if not missing: return skip('env_switches.tsv fork rows (already patched)')
    head, body = rows[0], rows[1:] + [f'{n}\t{c}\t{m}' for n, (c, m) in FORK_SWITCHES.items() if n in missing]
    return done('env_switches.tsv fork rows: ' + ' '.join(missing), '\n'.join([head] + sorted(body, key=lambda r: r.split('\t', 1)[0])) + '\n')


def patch_hades2(t):
    """The fork's Hades II profile carries what we verified end to end — the update (v1,
    1.0.137848) registered and packed, the measured NVN texture slots (35: slot 34 lies past the
    translator's built-in 32) and the widescreen investigation notes — and upstream's is v0-only.
    The fork's full file ships beside it as game.fork.tsv and is restored wholesale when the
    sync loses the marker; if upstream reworks the profile, merge by hand."""
    if 'build	v1	1.0.137848' in t: return skip('Hades II profile (already patched)')
    fork = (REPO / 'dm/games/0100A00019DE0000/game.fork.tsv').read_text(encoding='utf-8')
    say('  … restoring the fork\'s Hades II profile (update v1, measured slots, notes)')
    return done('Hades II profile v1', fork)

LAUNCHER_HELPERS = r'''static int dir_has_entries(const char *path) {   // anything inside at all (a save folder, a log folder)
    DIR *d = opendir(path); if (!d) return 0;
    struct dirent *e; int any = 0;
    while ((e = readdir(d))) if (strcmp(e->d_name, ".") && strcmp(e->d_name, "..")) { any = 1; break; }
    closedir(d); return any;
}

static int copy_file(const char *from, const char *to) {
    FILE *in = fopen(from, "rb"); if (!in) return 0;
    FILE *out = fopen(to, "wb"); if (!out) { fclose(in); return 0; }
    char buf[1 << 16]; size_t n; int ok = 1;
    while ((n = fread(buf, 1, sizeof buf, in)) > 0) if (fwrite(buf, 1, n, out) != n) { ok = 0; break; }
    if (ferror(in)) ok = 0;
    fclose(in); if (fclose(out)) ok = 0;
    if (!ok) unlink(to);
    return ok;
}

/// Recursive copy for the one-time migration of a save folder. Symlinks are followed (a save is
/// plain files); a file that already exists is left alone, so a half-finished copy can be resumed.
static void copy_tree(const char *from, const char *to) {
    mkdirs(to);
    DIR *d = opendir(from); if (!d) return;
    struct dirent *e;
    while ((e = readdir(d))) {
        if (!strcmp(e->d_name, ".") || !strcmp(e->d_name, "..")) continue;
        char a[PATH_MAX], b[PATH_MAX];
        snprintf(a, sizeof a, "%s/%s", from, e->d_name);
        snprintf(b, sizeof b, "%s/%s", to, e->d_name);
        struct stat st;
        if (stat(a, &st)) continue;
        if (S_ISDIR(st.st_mode)) copy_tree(a, b);
        else if (S_ISREG(st.st_mode) && access(b, F_OK)) copy_file(a, b);
    }
    closedir(d);
}

'''

LAUNCHER_BLOCK = r'''    // Saves and logs beside the game, not under ~/Library: a library folder the user can move or
    // keep on an external drive has to carry its saves with it, or they are effectively lost the
    // moment the folder moves (the files are still in ~/Library, where nobody looks for them).
    //
    // The library root is the folder the app sits in, when that folder also holds games/ — the
    // layout the Mac front end writes (<out>/<id>.app, <out>/games/<id>/romfs.img). A bundle
    // copied somewhere else keeps the old ~/Library paths, so nothing breaks for a user who never
    // used the launcher's folder.
    {
        char appdir[PATH_MAX]; snprintf(appdir, sizeof appdir, "%s", exe);
        // Four cuts: <…>/<id>.app/Contents/MacOS/<launcher> → MacOS → Contents → <id>.app → the folder
        // holding it. Three landed on the .app itself, so the probe looked for <id>.app/games — which
        // never exists — and the migration never ran (the report: saves stayed in ~/Library).
        *strrchr(appdir, '/') = 0; *strrchr(appdir, '/') = 0;
        *strrchr(appdir, '/') = 0; *strrchr(appdir, '/') = 0;
        // The marker file the Mac front end writes beside the .app (a dot file, so the Finder hides
        // it): this folder is a game library, and the game's saves and log live in it. DM_ROOT (the
        // learned shader state, env.txt), temp/ and everything else stay under ~/Library — only
        // what is needed and reused comes along, so the library folder stays lean.
        char probe[PATH_MAX]; snprintf(probe, sizeof probe, "%s/.nx2apple-library", appdir);
        if (!stat(probe, &st)) {
            snprintf(save, sizeof save, "%s/save/%s", appdir, id);
            snprintf(logs, sizeof logs, "%s/log", appdir);
            // First run after this change: bring the old saves across rather than starting the
            // player on an empty save slot. Copied, not moved — an older build of the same app
            // still reads the old place, and a copy is cheap next to a save.
            char oldsave[PATH_MAX], oldlog[PATH_MAX];
            snprintf(oldsave, sizeof oldsave, "%s/save", sup);
            snprintf(oldlog, sizeof oldlog, "%s/Library/Logs/nxnative/%s.log", home, id);
            mkdirs(save); mkdirs(logs);
            if (dir_has_entries(oldsave) && !dir_has_entries(save)) {
                copy_tree(oldsave, save);
                fprintf(stderr, "launcher: saves copied from %s to %s (they now live with the game)\n", oldsave, save);
            }
            // The previous session's log becomes the .prev.log, as the rotation below would have
            // it: the .log itself is opened fresh right after, and copying into it would be
            // truncated away by that open.
            char newprev[PATH_MAX]; snprintf(newprev, sizeof newprev, "%s/%s.prev.log", logs, id);
            // Explicit compares, not !stat(): stat() returns -1 when the file is missing, which is
            // truthy — reading "!" as "does not exist" here inverted the condition and skipped the
            // copy (found by the end-to-end test below).
            if (stat(oldlog, &st) == 0 && stat(newprev, &st) != 0) {
                if (copy_file(oldlog, newprev)) fprintf(stderr, "launcher: the previous log was copied from %s\n", oldlog);
            }
        }
    }
'''

LAUNCHER_SAY = r'''    // Say where this game reads and writes, on every start: a player checking whether their saves
    // moved with the game should not have to know the migration rules.
    fprintf(stderr, "launcher: saves %s\nlauncher: log %s\n", save, log);
'''

LAUNCHER_SWIFT_SECTION = r'''/// Where this game's data comes from, and a picker to change it.
///
/// The app can only read files it owns unless the player hands it a location through the document
/// picker — so a game's RomFS kept on an external drive or in iCloud Drive could not be used at all
/// before this. Picking an image (romfs.img) or a folder stores a bookmark (DataBookmark.m), and
/// romfs_locate() prefers it from the next launch on.
private struct GameDataSection: View {
    @State private var picked: String = ""
    @State private var note: String = ""
    @State private var picking = false

    var body: some View {
        Section {
            HStack {
                Text("Game data")
                Spacer()
                Text(picked.isEmpty ? "This app's folder" : (picked as NSString).lastPathComponent)
                    .foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
            }
            Button(picked.isEmpty ? "Choose game data…" : "Choose a different location…") { picking = true }
            if !picked.isEmpty {
                Button("Use the app's own data again", role: .destructive) {
                    dm_clear_picked_data(); refresh(); note = ""
                }
            }
        } footer: {
            Text(note.isEmpty
                 ? "Pick the game's romfs.img or its RomFS folder — an external drive or iCloud Drive works. The app remembers the choice; a re-signed build asks once more."
                 : note)
        }
        .sheet(isPresented: $picking) {
            DocumentPicker { url in
                picking = false
                if dm_store_picked_data(url) {
                    note = "Saved. The game uses this location from the next start."
                    refresh()
                } else {
                    note = "That location could not be remembered — try a different one."
                }
            } onCancel: { picking = false }
        }
        .onAppear(perform: refresh)
    }

    private func refresh() {
        // ObjC BOOL* is ObjCBool, not Bool: the bookmark header takes an indirect flag for
        // "the bookmark exists but does not resolve under this signature", which is the one
        // state worth telling the player about.
        var stale: ObjCBool = false
        picked = dm_picked_data_path(&stale) ?? ""
        if stale.boolValue && picked.isEmpty {
            note = "The location you picked before is no longer readable (the app was signed differently). Please choose it again."
        }
    }
}

/// UIDocumentPickerViewController for files and folders, in the two callbacks a SwiftUI sheet wants.
private struct DocumentPicker: UIViewControllerRepresentable {
    let onPick: (URL) -> Void
    let onCancel: () -> Void

    func makeUIViewController(context: Context) -> UIDocumentPickerViewController {
        // .open (not .import): the file stays where it is and the bookmark keeps pointing at it,
        // which is the whole point — an import would copy 16 GB into the app's container.
        let picker = UIDocumentPickerViewController(forOpeningContentTypes: [.item], asCopy: false)
        picker.allowsMultipleSelection = false
        picker.delegate = context.coordinator
        return picker
    }

    func updateUIViewController(_ vc: UIDocumentPickerViewController, context: Context) {}

    func makeCoordinator() -> Coordinator { Coordinator(onPick: onPick, onCancel: onCancel) }

    final class Coordinator: NSObject, UIDocumentPickerDelegate {
        let onPick: (URL) -> Void
        let onCancel: () -> Void
        init(onPick: @escaping (URL) -> Void, onCancel: @escaping () -> Void) {
            self.onPick = onPick; self.onCancel = onCancel
        }
        func documentPicker(_ controller: UIDocumentPickerViewController, didPickDocumentsAt urls: [URL]) {
            if let url = urls.first { onPick(url) } else { onCancel() }
        }
        func documentPickerWasCancelled(_ controller: UIDocumentPickerViewController) { onCancel() }
    }
}

'''

ENV_SWITCHES_VERIFY = r'''def verify_texts(paths):
    """Every DM_* name these files mention must be one the sources read (docs/env_switches.tsv), or a
    family a row documents: DM_PROF_<key> builds its name at run time, so no row can exist per member
    and the table names the family instead. A name ending in _ is a family prefix itself.

    Written after two replies in one day recommended DM_LOG_UEMALLOC and DM_NVN_ALIAS_TRACE, neither
    of which exists: a name recalled from memory rather than read out of the table. Run this over a
    draft before posting it.
    """
    known, family = set(), set()
    if TABLE.exists():
        for l in TABLE.read_text().split('\n'):
            f = l.split('\t')
            if not f or not f[0].startswith('DM_'): continue
            known.add(f[0])
            for m in re.finditer(r'(' + NAME + r')_[<*]', '\t'.join(f[1:])):
                family.add(m.group(1))
    bad = []
    for p in paths:
        for n in sorted(set(re.findall(NAME, Path(p).read_text(encoding='utf-8', errors='replace')))):
            if n.endswith('_'): continue                    # a family prefix itself ("DM_NVN_*")
            if n not in known and not any(k.startswith(n + '_') for k in known) and n not in family:
                bad.append((p, n))                          # unknown, and no row starts with it
    return bad, len(known)

'''

ENV_SWITCHES_MAIN = r'''    if sys.argv[1:2] == ['--verify']:
        problems, known = verify_texts(sys.argv[2:])
        for p, n in problems: print(f'{p}: {n} is not a switch the sources read')
        print(f'{len(problems)} unknown name(s) against {known} known switches')
        raise SystemExit(1 if problems else 0)
'''

TEST_ENV_DOC = r'''A switch the code reads that the table does not list, a row for a switch nothing reads any more, or a row without a meaning fails here.

`--verify <file>...` checks a text instead: every DM_* it names must be a real switch. It exists because two replies wrote DM_LOG_UEMALLOC and DM_NVN_ALIAS_TRACE from memory in
one day, and neither is a switch — a reader who trusts them wastes a run and learns nothing.'''

TEST_ENV_TESTS = r'''    def test_verify_accepts_a_real_switch_and_rejects_an_invented_one(self):
        with tempfile.TemporaryDirectory() as d:
            good = Path(d, 'good.md'); good.write_text('Set `DM_IMPORTS=1` to see the imports.\n')
            r = subprocess.run([sys.executable, str(REPO / 'dm/tools/env_switches.py'), '--verify', str(good)], cwd=REPO, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

            bad = Path(d, 'bad.md'); bad.write_text('Set `DM_LOG_UEMALLOC=1` to see the allocations.\n')
            r = subprocess.run([sys.executable, str(REPO / 'dm/tools/env_switches.py'), '--verify', str(bad)], cwd=REPO, capture_output=True, text=True)
            self.assertEqual(r.returncode, 1, 'an invented switch must fail the check')
            self.assertIn('@UEM@', r.stdout)

    def test_verify_ignores_a_prefix_and_a_family(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d, 'prefixes.md'); f.write_text('`DM_PROF_<key>` overrides; `DM_NVN_*` are the graphics ones; `DM_MEMSTAT` is real.\n')
            r = subprocess.run([sys.executable, str(REPO / 'dm/tools/env_switches.py'), '--verify', str(f)], cwd=REPO, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
'''.replace('@UEM@', 'DM_LOG_' + 'UEMALLOC')   # not spelled out: env_switches.py would read a quoted name in this file as a switch

TEST_PICKED_METHOD = r'''    def test_the_location_the_player_picked_wins_over_everything(self):
        """The Files-app pick (DataBookmark): an image, a folder holding one, or a plain tree."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); doc = root / 'Documents'; doc.mkdir()
            tmp = root / 'temp'; tmp.mkdir(); bundle = root / 'App'; bundle.mkdir()
            source = root / 'source'; source.mkdir(); (source / 'marker').write_text('resource')
            image.pack(source, bundle / 'romfs.img')
            # something the app would otherwise use, so "wins" means something
            image.pack(source, doc / 'romfs.img')
            external = root / 'external'; external.mkdir()

            def locate(picked=None):
                argv = [str(self.locator), str(doc), str(tmp), str(bundle)] + ([str(picked)] if picked else [])
                result = subprocess.run(argv, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()

            # 1. the picked path IS the image
            image.pack(source, external / 'mine.img')
            self.assertEqual(locate(external / 'mine.img'), str(external / 'mine.img'))
            # 2. the picked path is a folder holding romfs.img
            image.pack(source, external / 'romfs.img')
            self.assertEqual(locate(external), str(external / 'romfs.img'))
            # 3. the picked path is a plain RomFS tree
            (external / 'romfs.img').unlink(); (external / 'marker').write_text('resource')
            self.assertEqual(locate(external), str(external))
            # 4. a picked path holding none of this game's data falls through to the app's own copy
            empty = root / 'empty'; empty.mkdir()
            self.assertEqual(locate(empty), str(doc / 'romfs.img'))'''

# ---- Fork edits that had no protection until issue #92. Each was made by hand in a vendored file the
# sync takes from upstream whenever upstream changes it; each is now a patcher, written against the
# text of seedds/nxnative main (checked with scripts/tests/test_sync_patchers.py), with its own marker. ----

def anchored(path, edits):
    """A patcher from (name, marker, old, new) edits: `new` replaces the one occurrence of `old`, and an
    edit whose marker is already in the text is skipped (idempotent). An anchor that is missing or
    occurs more than once fails the run loudly."""
    def patch(t):
        for name, marker, old, new in edits:
            if marker in t:
                skip(f'{path} {name} (already patched)'); continue
            if t.count(old) != 1:
                fail(f'{path}: patch "{name}" anchor {"missing" if old not in t else "not unique"}: {old[:70]!r}')
            t = t.replace(old, new, 1)
            done(f'{path} {name}')
        return t
    return patch

# The Mac launcher's library-folder saves and log (c7ea1f56, 0deafaf5, 2b7e9e89, 0f779f5e). Temporary:
# seedds/nxnative#460 adds `dataFolder` (and DM_SAVE / DM_LOG_DIR relative to the app), which does the
# same job properly (it moves more than the save and the log, and needs no marker file). When #460 is in
# the fork, this patcher goes and the front end writes the preference per app instead.
LAUNCHER_C = 'dm/mac/launcher.c'
patch_launcher_c = anchored(LAUNCHER_C, [
    ('dirent include', '<dirent.h>', '#include <sys/stat.h>\n', '#include <sys/stat.h>\n#include <dirent.h>\n'),
    ('copy helpers', 'static int dir_has_entries',
     'static void env_file(const char *path) {', LAUNCHER_HELPERS + 'static void env_file(const char *path) {'),
    ('library folder', '.nx2apple-library',
     '    snprintf(logs, sizeof logs, "%s/Library/Logs/nxnative", home);\n',
     '    snprintf(logs, sizeof logs, "%s/Library/Logs/nxnative", home);\n' + LAUNCHER_BLOCK),
    ('say where', 'launcher: saves %s',
     '    mkdirs(save); mkdirs(temp); mkdirs(logs);\n    rename(log, prev);\n',
     '    mkdirs(save); mkdirs(temp); mkdirs(logs);\n' + LAUNCHER_SAY + '    rename(log, prev);\n'),
])

# The runtime game profile (73a89fb0): one host binary serves every game, so the constants the host took
# from game_profile.h at compile time (DM_GAME_<KEY> macros) are read through dm_profile() (hle_profile.c;
# without DM_PROFILE it falls back to the compiled macros, so behaviour is unchanged). Done as a
# substitution of the macro names, outside strings and comments, so it follows whatever upstream does
# around them (d8b8d642 rewrote mem_init_once and added DM_ALIAS_SIZE). A macro with no field in
# struct dm_profile (DM_GAME_STDALLOC_CAP) stays a compile-time constant.
PROFILE_FIELDS = {
    'DM_GAME_FPS_TARGET': 'fps_target', 'DM_GAME_ENV': 'genv', 'DM_GAME_ALLOC_BIG_ALIGN': 'alloc_big_align',
    'DM_GAME_PAD_LAYOUT': 'pad_layout', 'DM_GAME_MEM_RESERVE': 'mem_reserve', 'DM_GAME_MEM_ALIAS': 'mem_alias',
    'DM_GAME_MEM_STACK': 'mem_stack', 'DM_GAME_APP_POOL': 'app_pool', 'DM_GAME_MEM_USED': 'mem_used',
    'DM_GAME_MEM_MAP_LIMIT': 'mem_map_limit', 'DM_GAME_SYS_RESOURCE': 'sys_resource', 'DM_GAME_PROGRAM_ID': 'program_id',
}
# a string literal or a comment (kept as it is), else one of the macro names
_PROFILE_TOKEN = re.compile(r'("(?:[^"\\\n]|\\.)*"|//[^\n]*|/\*.*?\*/)|\b(' + '|'.join(PROFILE_FIELDS) + r')\b', re.S)
PROFILE_PATHS = ('dm/host/hle_svc.c', 'dm/host/hle_os.c', 'dm/host/hle_fpslock.c', 'dm/host/loader.c', 'dm/ios/Sources/DmGamepad.m')

def profile_patcher(path, paren=False):
    """paren: wrap the replacement (hle_svc.c defines macros from them, RESERVE and the like)."""
    def patch(t):
        n = 0
        def sub(m):
            nonlocal n
            if m.group(1): return m.group(1)
            n += 1
            r = 'dm_profile()->' + PROFILE_FIELDS[m.group(2)]
            return f'({r})' if paren else r
        out = _PROFILE_TOKEN.sub(sub, t)
        if n: done(f'{path} runtime profile ({n} constants read through dm_profile())')
        else: skip(f'{path} runtime profile (already patched)')
        return out
    return patch

patch_hle_svc = profile_patcher('dm/host/hle_svc.c', paren=True)
patch_hle_os = profile_patcher('dm/host/hle_os.c')
patch_hle_fpslock = profile_patcher('dm/host/hle_fpslock.c')
patch_loader = profile_patcher('dm/host/loader.c')
patch_dmgamepad = profile_patcher('dm/ios/Sources/DmGamepad.m')

# The Files-app "Game data" pick on the iPhone (DataBookmark.m, main.m's romfs_locate; main.m is patched
# by patch_main_m). Launcher.swift gets the section and the picker, project.yml the bridging header.
LAUNCHER_SWIFT = 'dm/ios/Sources/Launcher.swift'
patch_launcher_swift = anchored(LAUNCHER_SWIFT, [
    ('game data section', 'struct GameDataSection', 'private struct LauncherView: View {', LAUNCHER_SWIFT_SECTION + 'private struct LauncherView: View {'),
    ('game data row', 'GameDataSection()',
     '                    Section {\n                        HStack {\n                            Text("Memory available")',
     '                    GameDataSection()\n                    Section {\n                        HStack {\n                            Text("Memory available")'),
])
patch_project_yml = anchored('dm/ios/project.yml', [
    ('bridging header', 'SWIFT_OBJC_BRIDGING_HEADER',
     '      SWIFT_VERSION: "5.0"                         # Launcher.swift (SwiftUI)\n',
     '      SWIFT_VERSION: "5.0"                         # Launcher.swift (SwiftUI)\n'
     '      SWIFT_OBJC_BRIDGING_HEADER: Sources/DmIOS-Bridging-Header.h   # DataBookmark (the Files app pick)\n'),
])
# test_xcode_workflow mocks every check build_ipa_from_nsp.main runs; check_glslang is the fork's (patch_nsp)
patch_test_xcode_workflow = anchored('dm/ios/tests/test_xcode_workflow.py', [
    ('check_glslang mock', "'check_glslang': None",
     "'check_prereqs': None, 'check_metal': None,", "'check_prereqs': None, 'check_metal': None, 'check_glslang': None,"),
])

# `env_switches.py --verify <file>...` (a draft's DM_* names against the table) and its tests: fork-only additions
patch_env_switches_py = anchored('dm/tools/env_switches.py', [
    ('--verify', 'def verify_texts',
     "if __name__ == '__main__':\n    text, bad, unread = rows()",
     ENV_SWITCHES_VERIFY + "if __name__ == '__main__':\n" + ENV_SWITCHES_MAIN + "    text, bad, unread = rows()"),
])
patch_test_env_switches = anchored('dm/tools/tests/test_env_switches.py', [
    ('docstring', '`--verify <file>...`',
     'A switch the code reads that the table does not list, a row for a switch nothing reads any more, or a row without a meaning fails here."""\n',
     TEST_ENV_DOC + '"""\n'),
    ('tempfile import', 'import tempfile\n', 'import sys\nimport unittest\n', 'import sys\nimport tempfile\nimport unittest\n'),
    ('verify tests', 'test_verify_accepts_a_real_switch', "\n\nif __name__ == '__main__':",
     '\n' + TEST_ENV_TESTS + "\n\nif __name__ == '__main__':"),
])
# the impact map has a row per host file (the front end's key map is the fork's, seedds/nxnative#386)
patch_impact_map = anchored('dm/tools/impact_map.tsv', [
    ('input_map.h row', 'dm/host/input_map.h',
     "dm/host/front_mac.m\tpresent:*\tthe Mac front end\n",
     "dm/host/front_mac.m\tpresent:*\tthe Mac front end\ndm/host/input_map.h\tpresent:*\tthe Mac front end's key and controller button map\n"),
])

DOCS_AGENTS_BULLETS = r'''- **A test that has not failed is not evidence.** A fix's test must fail before the fix and pass after it; run it both ways, or at least confirm it is collected and that its assertion touches the changed behaviour. Three failures in two days, all of them mine: a test appended *after* the class's closing brace (XCTest never collected it — the suite reported the same count before and after it existed, and I read that as a pass); `assertIn('TEXT_LO = segs[0][1]', src)`, a string match that stays green while the module raises `NameError` on import; and a release-notes splitter whose two failing cases only appeared once the test existed. A green suite is a claim about the code only if the test would have been red.
- **A switch you recommend exists.** Before naming a `DM_*` in a reply, an issue body or a PR, check it against `docs/env_switches.tsv` (generated from the sources): `python3 dm/tools/env_switches.py --verify draft.md` fails on every name that is neither a row nor a documented family. Two replies written from memory in one day told reporters to set `DM_LOG_UEMALLOC` and `DM_NVN_ALIAS_TRACE`, neither of which exists; they wasted a run each and learned nothing.'''
DEV_KEYMAP_SENTENCE = r'''`DM_KEYMAP=<name>=<key code>,…` puts a button, a stick direction (the right stick too, on no key by default) or `Shake` on another key, and `DM_PAD_MAP=<name>=<name>,…` changes what a controller's buttons send (`A=B,B=A` swaps them; `input_map.h`).'''

# The fork's own lines in vendored docs and in one game profile (tex_bc_cpu, the CPU BC decoder).
patch_hades_bc_profile = anchored('dm/games/0100F2C0115B6000/game.tsv', [
    ('tex_bc_cpu', 'tex_bc_cpu\ton',
     'tex_flush_upload\ton\n',
     'tex_flush_upload\ton\n# BC textures on GPUs without BC support (A15/A16 and older): decode them on the CPU into\n'
     '# RGBA8 (nvn_bcdec.c). No effect on Macs and A19+ devices, which keep the GPU path.\ntex_bc_cpu\ton\n'),
])
patch_docs_agents_readme = anchored('docs/agents/README.md', [
    ('fork rules', 'A switch you recommend exists', '\n\n## Checklist for a new title', '\n' + DOCS_AGENTS_BULLETS + '\n\n## Checklist for a new title'),
])
patch_docs_development = anchored('docs/development.md', [
    ('key map sentence', '`DM_KEYMAP=<name>=<key code>',
     'with the controllers from player 2 on. The window goes fullscreen from',
     'with the controllers from player 2 on. ' + DEV_KEYMAP_SENTENCE + ' The window goes fullscreen from'),
    ('switch2 dump', 'already decrypted exefs and romfs',
     '`--platform` takes `ios` (the default: the IPA, or the Xcode project with `--prepare-xcode`), `mac` ([A Mac-only build for development](#a-mac-only-build-for-development)) and `mac-app` ([A Mac app](#a-mac-app)).\n',
     '`--platform` takes `ios` (the default: the IPA, or the Xcode project with `--prepare-xcode`), `mac` ([A Mac-only build for development](#a-mac-only-build-for-development)) and `mac-app` ([A Mac app](#a-mac-app)).\n'
     '\nA Switch 2 dump is already decrypted exefs and romfs, not an NSP. `--switch2 DIR` (`SWITCH2` in Spyder) takes a version folder (`updates/458752` with `exefs/` and `romfs/`) or a game folder with `updates/<version>/` and `dlc/<title id>/<index>/romfs`. `--exefs` and `--romfs` name the directories when they are not laid out that way. No prod.keys. The title ID comes from `main.npdm`; the package version from the folder\'s number (`458752` is `v458752`); `(v3.2.0)` in a parent folder\'s name is the display version when there is no `control.nacp`. The build links those directories into `<id>_build/extract/` (or packs a `RomFS.img` with `--romfs-image`) and the DLC into `aoc/<index>/`, then runs the same rewrite, shaders and pack. A game with no profile is surveyed from the loose files.\n'),
    ('switch2 spyder settings', 'ROMFS_IMAGE',
     'In Spyder, open the script, fill in the settings at the top (`NSP_DIR`, `PROD_KEYS`, and optionally `PACKAGE`, `OUT_IPA`, `APP_NAME`, `BUNDLE_ID`, `LANGUAGE`, `KEEP_EXTRACTED`, `SHOW_FPS`, `PSO_GPUS`, `PLATFORM`; for a game without a profile `HEAP_CHECK`, `HEAP_CHECK_SECS`, `PROVISIONAL`), and press F5. Empty paths are asked for at run time.',
     'In Spyder, open the script, fill in the settings at the top (`NSP_DIR`, `PROD_KEYS`, and optionally `PACKAGE`, `OUT_IPA`, `APP_NAME`, `BUNDLE_ID`, `LANGUAGE`, `KEEP_EXTRACTED`, `SHOW_FPS`, `PSO_GPUS`, `PLATFORM`; for a Switch 2 dump `SWITCH2`, `EXEFS`, `ROMFS`, `ROMFS_IMAGE`; for a game without a profile `HEAP_CHECK`, `HEAP_CHECK_SECS`, `PROVISIONAL`), and press F5. Empty paths are asked for at run time.'),
])

GAME_PY_PROFILE = r'''    elif len(a) == 3 and a[0] == 'profile':   # the runtime profile the host reads from DM_PROFILE (hle_profile.h): the header's values, as the TSV form
        g = load(a[1]); out = Path(a[2]); out.parent.mkdir(parents=True, exist_ok=True)
        M = dict(re.findall(r'^#define (DM_GAME_\w+) (.*)$', header(g), re.M))   # exactly what the compiled profile would hold
        # macro → the runtime profile's key (hle_profile.c set_kv); the header's spelling of the value is the file's
        KEYS = [('title_id', 'ID'), ('program_id', 'PROGRAM_ID'), ('name', 'NAME'), ('romfs_marker', 'ROMFS_MARKER'),
                ('modules', 'MODULES'), ('mem_reserve', 'MEM_RESERVE'), ('mem_alias', 'MEM_ALIAS'), ('mem_stack', 'MEM_STACK'),
                ('app_pool', 'APP_POOL'), ('mem_used', 'MEM_USED'), ('sys_resource', 'SYS_RESOURCE'), ('mem_map_limit', 'MEM_MAP_LIMIT'),
                ('present_interval', 'PRESENT_DISPLAY'), ('core_preempt', 'CORE_PREEMPT'), ('alloc_big_align', 'ALLOC_BIG_ALIGN'),
                ('fps_target', 'FPS_TARGET'), ('fps_hold', 'FPS_HOLD'), ('present_snap', 'PRESENT_SNAP'), ('pso_mtl4', 'PSO_MTL4'),
                ('shared_font', 'SHARED_FONT_TTF'), ('pad_layout', 'PAD_LAYOUT'), ('op_mode', 'OPMODE'), ('perf_mode', 'PERF_BOOST'),
                ('feedback_snap', 'FEEDBACK_SNAP'), ('tex_flush_upload', 'TEX_FLUSH_UPLOAD'), ('render_cond', 'RENDER_COND'),
                ('tex_dirty_upload', 'TEX_DIRTY_UPLOAD'), ('tex_dyn_slots', 'TEX_DYN_SLOTS'), ('gpu_timestamps', 'GPU_TS_SEGMENT'),
                ('vconv_range', 'VCONV_RANGE'), ('game_args', 'ARGS'), ('game_env', 'ENV')]
        def val(macro):   # the header's C spelling → the file's plain spelling
            v = M.get('DM_GAME_' + macro)
            if v is None: return None
            v = v.strip()
            if v.startswith('{') and v.endswith('}'):   # {"main", "sdk"} → main sdk
                return ' '.join(x.strip().strip('"') for x in v[1:-1].split(',') if x.strip())
            if v.startswith('"'): return v[1:-1]
            return v[:-3] if v.endswith('ull') else v
        L = [f'# Runtime game profile of {g.id} ({g.get("name")}), written by dm/games/game.py profile from {g.dir}/game.tsv',
             '# Read by the host from DM_PROFILE (hle_profile.c set_kv): KEY<TAB>VALUE, # starts a comment. Every value the compiled',
             '# profile would hold is written, so the file alone describes the game.']
        for key, macro in KEYS:
            v = val(macro)
            if v is not None: L.append(f'{key}\t{v}')
        api = [val('NVN_API_MAJOR'), val('NVN_API_MINOR')]
        if api[0] is not None: L.append(f'device_api\t{api[0]}.{api[1] or 0}')
        for b, d in re.findall(r'\{"([0-9a-f]+)", "([^"]*)"\}', M.get('DM_GAME_DISPLAY_VERSIONS', '')):
            L.append(f'display_version\t{b}\t{d}')
        text = '\n'.join(L) + '\n'
        if not (out.exists() and out.read_text() == text): out.write_text(text, encoding='utf-8')   # unchanged: keep the mtime
        print(out)
'''
GAME_PY_TBC = r'''    tbc = g.get('tex_bc_cpu', 'off')   # decode BC-compressed textures on the CPU when the GPU has no BC support (A15/A16 and older; nvn_mtl.m uses nvn_bcdec.c): on / off
    if tbc not in ('on', 'off'): sys.exit(f'{g.id}: tex_bc_cpu must be on or off: {tbc}')
'''
# dm/games/game.py: the tex_bc_cpu profile key (nvn_bcdec.c) and the `profile` subcommand that writes the runtime
# profile (hle_profile.h). Fork-owned as a whole (OURS) until issue #92, which made the sync stop: upstream
# changes this file too (tex_flush_upload ...), so the three additions are re-applied on top of its version.
patch_game_py = anchored('dm/games/game.py', [
    ('tex_bc_cpu check', "tbc = g.get('tex_bc_cpu'",
     "    tds = g.get('tex_dyn_slots', 'off')", GAME_PY_TBC + "    tds = g.get('tex_dyn_slots', 'off')"),
    ('tex_bc_cpu macro', 'DM_GAME_TEX_BC_CPU',
     "         f'#define DM_GAME_TEX_DYN_SLOTS {int(tds == \"on\")}',\n",
     "         f'#define DM_GAME_TEX_BC_CPU {int(tbc == \"on\")}',\n         f'#define DM_GAME_TEX_DYN_SLOTS {int(tds == \"on\")}',\n"),
    ('profile subcommand', "a[0] == 'profile'",
     "    elif len(a) == 2 and a[0] == 'dir':", GAME_PY_PROFILE + "    elif len(a) == 2 and a[0] == 'dir':"),
])

# Fork edits to vendored files that the sync replaces with upstream's version on purpose. The
# unprotected-edit check (check_unprotected) accepts a file listed here.
DROPPED = {
    # data_blocks(img, text_end, md, lo=0): a leftover of the first form of the NRO fix (TEXT_LO); patch_nso.py's
    # code_start()/code_ranges() replaced it and nothing passes `lo`, so upstream's data_blocks is the right one.
    'dm/tools/nso_code.py': 'dead edit: the NRO bound lives in patch_nso.py (patch_patch_nso), nothing passes lo',
    # CreateWithAdjustableSystemClock and the snapshot type bytes (PR #26) were OURS until issue #92; upstream has
    # them since 4d2dc7db (#385, same function names, same behaviour), so the sync takes upstream's files again.
    'dm/host/hle_time.c': 'the fork-only parts landed upstream as 4d2dc7db (#385)',
    'dm/host/tests/t_time.c': 'the fork-only parts landed upstream as 4d2dc7db (#385)',
}

# The picked-location part of the fixture (the Files-app pick): the stub, argv[4], and the test.
PICKED_EDITS = [
    ('picked stub', 'fixture_picked;',
     r"""'#define NSSearchPathForDirectoriesInDomains(a,b,c) @[fixture_doc]\n#define NSTemporaryDirectory() fixture_tmp\n' + locator +""",
     r"""'#define NSSearchPathForDirectoriesInDomains(a,b,c) @[fixture_doc]\n#define NSTemporaryDirectory() fixture_tmp\n'
                          # The picked-location bookmark (DataBookmark.m): the fixture answers it from a
                          # variable, so a test can put the player's choice anywhere on disk.
                          'static NSString *fixture_picked;\n'
                          'NSString *dm_picked_data_path(BOOL *stale) { if (stale) *stale = NO; return fixture_picked; }\n' + locator +"""),
    ('picked argv', 'if (argc > 4) fixture_picked',
     r"""'fixture_doc = @(argv[1]); fixture_tmp = @(argv[2]); fixture_bundle = argc > 3 ? @(argv[3]) : @"/nonexistent-fixture"; NSString *p = romfs_locate();\n'""",
     r"""'fixture_doc = @(argv[1]); fixture_tmp = @(argv[2]); fixture_bundle = argc > 3 ? @(argv[3]) : @"/nonexistent-fixture";\n'
                          'if (argc > 4) fixture_picked = @(argv[4]);   // the location the player picked, when a test sets one\n'
                          'NSString *p = romfs_locate();\n'"""),
    ('picked test', 'test_the_location_the_player_picked', "\n\n\nif __name__ == '__main__': unittest.main()",
     "\n\n" + TEST_PICKED_METHOD + "\n\n\nif __name__ == '__main__': unittest.main()"),
]

# One marker per carrier: the string that exists only when the fork's patch is in place.
MARKERS = {
    'dm/host/hle_fs.c': 'romfsarc',
    'dm/host/Makefile': 'romfs_arc.c',
    'dm/host/tests/Makefile': 'romfs_arc.c',
    'dm/ios/tools/build_ipa.sh': 'DM_FULL_ROMFS',
    'scripts/build_ipa_from_nsp.py': '--work-dir',
    'dm/ios/Sources/main.m': 'romfs_dir_ok',
    'dm/gfx/nvn/nvn_mtl.m': '../tex/nvn_bcdec.h',
    'dm/games/0100A00019DE0000/game.tsv': 'build	v1	1.0.137848',
    'dm/host/dm.h': 'hle_profile.h',
    'dm/host/front_mac.m': 'DM_KEYMAP',
    'dm/tools/patch_nso.py': 'def code_start(d):',
    'dm/ios/tests/test_romfs_startup.py': 'fixture_profile',
    'dm/ios/tests/test_profile_pack.py': 'slim package',
    'docs/env_switches.tsv': 'DM_FULL_ROMFS\t',
    'dm/mac/launcher.c': '.nx2apple-library',
    'dm/host/hle_svc.c': 'dm_profile()->mem_reserve',
    'dm/host/hle_os.c': 'dm_profile()->genv',
    'dm/host/hle_fpslock.c': 'dm_profile()->fps_target',
    'dm/host/loader.c': 'dm_profile()->fps_target',
    'dm/ios/Sources/DmGamepad.m': 'dm_profile()->pad_layout',
    'dm/ios/Sources/Launcher.swift': 'GameDataSection()',
    'dm/ios/project.yml': 'SWIFT_OBJC_BRIDGING_HEADER',
    'dm/ios/tests/test_xcode_workflow.py': "'check_glslang': None",
    'dm/tools/env_switches.py': 'def verify_texts',
    'dm/tools/tests/test_env_switches.py': 'test_verify_accepts_a_real_switch',
    'dm/tools/impact_map.tsv': 'dm/host/input_map.h',
    'dm/games/game.py': 'DM_GAME_TEX_BC_CPU',
    'dm/games/0100F2C0115B6000/game.tsv': 'tex_bc_cpu\ton',
    'docs/agents/README.md': 'A switch you recommend exists',
    'docs/development.md': '`DM_KEYMAP=<name>=<key code>',
}
PATCHERS = {
    'dm/host/hle_fs.c': patch_hle_fs,
    'dm/host/Makefile': patch_makefile,
    'dm/host/tests/Makefile': patch_tests_makefile,
    'dm/ios/tools/build_ipa.sh': patch_build_ipa,
    'scripts/build_ipa_from_nsp.py': patch_nsp,
    'dm/ios/Sources/main.m': patch_main_m,
    'dm/gfx/nvn/nvn_mtl.m': patch_nvn_mtl,
    'dm/games/0100A00019DE0000/game.tsv': patch_hades2,
    'dm/host/dm.h': patch_dm_h,
    'dm/host/front_mac.m': patch_front_mac,
    'dm/tools/patch_nso.py': patch_patch_nso,
    'dm/ios/tests/test_romfs_startup.py': patch_test_romfs_startup,
    'dm/ios/tests/test_profile_pack.py': patch_test_profile_pack,
    'docs/env_switches.tsv': patch_env_switches,
    'dm/mac/launcher.c': patch_launcher_c,
    'dm/host/hle_svc.c': patch_hle_svc,
    'dm/host/hle_os.c': patch_hle_os,
    'dm/host/hle_fpslock.c': patch_hle_fpslock,
    'dm/host/loader.c': patch_loader,
    'dm/ios/Sources/DmGamepad.m': patch_dmgamepad,
    'dm/ios/Sources/Launcher.swift': patch_launcher_swift,
    'dm/ios/project.yml': patch_project_yml,
    'dm/ios/tests/test_xcode_workflow.py': patch_test_xcode_workflow,
    'dm/tools/env_switches.py': patch_env_switches_py,
    'dm/tools/tests/test_env_switches.py': patch_test_env_switches,
    'dm/tools/impact_map.tsv': patch_impact_map,
    'dm/games/game.py': patch_game_py,
    'dm/games/0100F2C0115B6000/game.tsv': patch_hades_bc_profile,
    'docs/agents/README.md': patch_docs_agents_readme,
    'docs/development.md': patch_docs_development,
}

# PATCHED, MARKERS and PATCHERS are three views of one set and have to agree, or a carrier works
# while something else refuses to let it through. v0lumehi hit exactly that (PR #59): front_mac.m,
# patch_nso.py and Hades II's game.tsv had patchers and markers but were missing from PATCHED, so
# pr-scope refused the PR that carried them. Checked here rather than remembered.
assert set(PATCHED) == set(MARKERS) == set(PATCHERS), (
    'the carrier tables disagree: PATCHED-only %s, MARKERS-only %s, PATCHERS-only %s' % (
        sorted(set(PATCHED) - set(MARKERS) | set(PATCHED) - set(PATCHERS)),
        sorted(set(MARKERS) - set(PATCHED) | set(MARKERS) - set(PATCHERS)),
        sorted(set(PATCHERS) - set(PATCHED) | set(PATCHERS) - set(MARKERS))))

report, problems = [], []
class SyncProblem(Exception): pass
def say(s): report.append(s); print(s, flush=True)
def fail(s):
    problems.append(s); say('[FAIL] ' + s)
    raise SyncProblem(s)
def skip(name): say(f'  = {name}')
def done(name, t=None):
    say(f'  + {name}')
    return t

def upstream_mode(rev, path):
    """The file's git mode upstream ('100755' for an executable), None when it is not there."""
    out = git('ls-tree', rev, '--', path).split()
    return out[0] if out else None

def take_from_upstream(rev, path, dest):
    data = blob(rev, path)
    if data is None: fail(f'{path}: not in upstream {rev}'); return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    # The mode too: written as bytes, an executable came out 0644, and the fork's spirv-cross binary, its build.sh and six
    # scripts lost their x bit (test_spirv_cross_pin: Permission denied running the committed spirv-cross)
    dest.chmod(0o755 if upstream_mode(rev, path) == '100755' else 0o644)

def verify(skip_build, upstream_ref='upstream/main'):
    say('--- verify ---')
    py = '/usr/bin/python3' if os.path.exists('/usr/bin/python3') else sys.executable
    bad = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in ('.git', 'third_party', '__pycache__', '.build', 'node_modules')]
        for f in files:
            if not f.endswith('.py'): continue
            p = Path(root) / f
            r = subprocess.run([py, '-c', 'import ast,sys\ntry: ast.parse(open(sys.argv[1],encoding="utf-8",errors="replace").read())\nexcept SyntaxError as e: print(f"{e.lineno}: {e.msg}")', str(p)], capture_output=True, text=True)
            if r.stdout.strip(): bad.append(f'{p.relative_to(REPO)}:{r.stdout.strip()}')
    if bad:
        for b in bad: fail(f'Python 3.9 syntax: {b}')
    else: say(f'  + every .py parses on the Python 3.9 floor')
    r = subprocess.run(['bash', '-n', 'dm/ios/tools/build_ipa.sh'], cwd=REPO, capture_output=True, text=True)
    if r.returncode: fail(f'build_ipa.sh: bash -n failed: {r.stderr.strip()}')
    else: say('  + build_ipa.sh: bash -n OK')
    # archive reader: compile and run against a small generated archive
    with tempfile.TemporaryDirectory() as td:
        src, arc = Path(td) / 'src', Path(td) / 'romfs'
        (src / 'sub').mkdir(parents=True)
        (src / 'a.txt').write_bytes(b'hello'); (src / 'sub' / 'b.txt').write_bytes(b'world')
        r = subprocess.run([sys.executable, 'dm/tools/romfs_arc.py', 'pack', str(src), str(arc), '--shard-mib', '1'], cwd=REPO, capture_output=True, text=True)
        if r.returncode: fail(f'archive pack failed: {r.stderr.strip()[:200]}'); return
        exe = Path(td) / 't_arc'
        r = subprocess.run(['clang', '-O1', '-I', 'dm/host', '-o', str(exe), 'dm/host/tests/t_romfs_arc.c', 'dm/host/romfs_arc.c'], cwd=REPO, capture_output=True, text=True)
        if r.returncode: fail(f'archive test compile failed: {r.stderr.strip()[:400]}'); return
        r = subprocess.run([str(exe), str(arc)], capture_output=True, text=True)
        if r.returncode or 'all checks passed' not in r.stdout: fail(f'archive test failed: {r.stdout.strip()[:200]} {r.stderr.strip()[:200]}')
        else: say('  + archive reader test passed')
    if skip_build: say('  = host build skipped (--skip-build)'); return
    games = sorted(p.name for p in (REPO / 'dm/games').iterdir() if p.name.startswith('0100'))
    game = '01006F8002326000' if '01006F8002326000' in games else games[0]
    say(f'  … building the macOS host for {game} (a few minutes)')
    r = subprocess.run(['make', '-C', 'dm/host', f'GAME={game}', 'dmhost', '-j4'], cwd=REPO, capture_output=True, text=True)
    if r.returncode:
        tail = '\n'.join((r.stdout + r.stderr).splitlines()[-25:])
        fail(f'host build failed:\n{tail}')
    else: say(f'  + host build OK ({game})')
    # Host unit tests: the fs tests #include ../hle_fs.c and link romfs_arc.c, so a fork change that
    # the test rules do not know about breaks them at link time and they stop running silently
    # (PR #12 — they had been unbuildable). Cheap (seconds) and catches that class here.
    say(f'  … host unit tests for {game}')
    r = subprocess.run(['make', '-C', 'dm/host/tests', f'GAME={game}', 'run', '-j4'], cwd=REPO, capture_output=True, text=True)
    if r.returncode:
        tail = '\n'.join((r.stdout + r.stderr).splitlines()[-25:])
        fail(f'host unit tests failed:\n{tail}')
    else: say('  + host unit tests OK')
    check_patcher_consistency(upstream_ref)


def check_patcher_consistency(upstream_ref='upstream/main'):
    """Every carrier's patched form must equal the file in the tree, code line by code line.

    A fork change made to a carrier's source but not to its patcher entry is invisible today (the
    marker is present, so the patcher skips) and reappears the next time upstream touches that file
    — PR #11 was exactly that: GetEntryType had been fixed to arc_rel in the source while the patcher
    still inserted ROMFSARC_SYNTH_REL. Comparing the patcher's output against the working tree makes
    the drift show up on the sync that introduces it rather than months later.

    Comments and blank lines are ignored: a comment reworded in the tree is not drift, and demanding
    a matching comment edit in the patcher would train people to ignore this check.
    """
    say('  … patcher consistency (patched upstream file vs the tree, comments ignored)')
    def code(text):
        out = []
        for ln in text.splitlines():
            s = ln.strip()
            if not s or s.startswith(('#', '//', '/*', '*', '<!--')):
                continue
            # strip a trailing comment too: `code;  // why` is the same statement however the why
            # is worded, and a reworded trailing comment must not read as drift
            cut = ln.find('  //')
            if cut < 0:
                cut = ln.find('  #')
            if cut > 0:
                ln = ln[:cut]
            out.append(ln.rstrip())
        return out
    drift = []
    for path, patcher in PATCHERS.items():
        src = REPO / path
        if not src.exists():
            continue
        upstream = subprocess.run(['git', 'show', f'{upstream_ref}:{path}'], cwd=REPO,
                                  capture_output=True, text=True)
        if upstream.returncode:
            continue                      # a fork-only file has no upstream form to compare
        try:
            out = patcher(upstream.stdout)
        except SyncProblem:
            continue                      # an anchor miss is reported by apply_changes already
        if out is None:
            out = upstream.stdout
        if code(out) != code(src.read_text(encoding='utf-8', errors='replace')):
            drift.append(path)
    if drift:
        for d in drift:
            say(f'  ! {d}: the patcher and the tree disagree')
        fail('patcher drift: ' + ', '.join(drift) +
             ' — the source was changed without its patcher entry (or the other way round); '
             'a future sync would silently revert the change')
    else:
        say(f'  + {len(PATCHERS)} patchers agree with the tree')

def unprotected_edits(old, head='HEAD'):
    """The fork's hand edits that nothing protects (issue #92): (edited, added) lists of vendored paths.

    The sync takes upstream's version of a vendored file wherever upstream changed it, so a fork edit
    survives only in an OURS file, a PATCHED carrier (its patcher re-applies it) or a DROPPED file (the
    loss is deliberate). `edited` are files that exist in the last sync base `old` and differ in the
    fork's tree `head` (or were deleted there) without any of the three; `added` are fork-only files
    under the vendored paths that are not in OURS either (harmless until upstream adds the same path:
    apply_changes would then overwrite the fork's file)."""
    edited, added = [], []
    for line in git('diff', '--name-status', '--no-renames', old, head, '--', *VENDORED).splitlines():
        kind, path = line.split('\t')[0][0], line.split('\t')[-1]
        if path in OURS or path in PATCHED or path in DROPPED: continue
        (added if kind == 'A' else edited).append(path)
    return sorted(edited), sorted(added)

def check_unprotected(old, changes):
    """Report fork edits the sync would lose. An edited file that upstream changes in this very sync is a
    failure (its fork change is about to be overwritten); one that upstream leaves alone is only listed
    (it survives this sync, and is lost at the first sync that touches it)."""
    edited, added = unprotected_edits(old)
    upstream = {p for _, p in changes}
    lost = [p for p in edited if p in upstream]
    for p in edited:
        if p not in lost: say(f'  ! {p}: edited in the fork, not in OURS / PATCHED / DROPPED; upstream does not change it in this sync, but a later one will')
    if added: say(f'  . {len(added)} fork-only file(s) under the vendored paths are not in OURS (harmless unless upstream adds the same path; --check-protection lists them)')
    for p in lost:
        say(f'  ! {p}: edited in the fork and changed by upstream in this sync, with no patcher')
    if lost:
        fail('fork edits that this sync would overwrite (add a patcher to PATCHERS, or OURS / DROPPED with the reason): ' + ', '.join(lost))
    return edited, added

def apply_changes(a, changes):
    """Take every changed file from upstream, then re-apply the fork's patches to the carriers."""
    touched = 0
    for kind, path in changes:
        dest = REPO / path
        if kind == 'D':
            if path in OURS: fail(f'upstream deleted a fork file: {path}')
            if dest.exists(): dest.unlink(); say(f'  - {path}'); touched += 1
            continue
        if path in OURS: fail(f'upstream writes over a fork file: {path} (resolve by hand)')
        take_from_upstream(a.new, path, dest); touched += 1
        if path in PATCHED:
            r = PATCHERS[path](dest.read_text(encoding='utf-8', errors='replace'))
            if r is not None and r != dest.read_text(encoding='utf-8', errors='replace'):
                dest.write_text(r, encoding='utf-8')
    say(f'{touched} files taken from upstream')
    # Second pass over every carrier, including ones upstream did not change this time: a fork
    # patch can be lost without the file ever appearing in the diff (a wholesale tree replace,
    # a bad merge), so presence is asserted, not assumed.
    for path, patcher in PATCHERS.items():
        p = REPO / path
        if not p.exists(): fail(f'{path}: missing before patching')
        t = p.read_text(encoding='utf-8', errors='replace')
        r = patcher(t)
        if r is not None and r != t: p.write_text(r, encoding='utf-8')
        now = p.read_text(encoding='utf-8', errors='replace')
        if MARKERS[path] not in now:
            fail(f'{path}: fork patch marker missing after sync: {MARKERS[path]!r}')


def game_rows():
    """Every profile in the tree, as the README table wants it: name, title ID, update, status.

    Read from `dm/games/<title ID>/game.tsv`, which is the only authority: the table used to be
    maintained by hand, so a sync that brought in new profiles (three did, with 467 commits) left
    the README claiming 72 games while the tree had 75.

    `build` holds the update's version and version number (`v131072 1.0.2 main=…`); `package` is
    the version the profile needs. A profile with no build line needs no update at all.
    """
    rows = []
    for d in sorted((REPO / 'dm/games').iterdir()):
        if not d.is_dir() or not d.name.startswith('0100'):
            continue
        tsv = d / 'game.tsv'
        if not tsv.is_file():
            continue
        fields: dict[str, str] = {}
        for ln in tsv.read_text(encoding='utf-8', errors='replace').splitlines():
            if not ln or ln.startswith('#') or '\t' not in ln:
                continue
            k, v = ln.split('\t', 1)
            fields.setdefault(k.strip(), v.strip())
        name = fields.get('name')
        if not name:
            continue
        # `build` repeats once per version this profile knows (v0 the base game, then each
        # update), and `package` names the one the profile is built for. Reading the first build
        # line reported the base version for every game, which is how "无（v0）" became "1.0.0 (v0)"
        # and Super Mario Bros. Wonder's 1.2.1 became 1.0.0.
        builds = [ln.split('\t', 1)[1].strip()
                  for ln in tsv.read_text(encoding='utf-8', errors='replace').splitlines()
                  if ln.startswith('build\t')]
        want = fields.get('package', 'v0').strip() or 'v0'
        chosen = next((b for b in builds if b.split() and b.split()[0] == want), None)
        parts = (chosen or '').split()
        if len(parts) >= 2 and parts[0] != 'v0':
            update = f'{parts[1]} ({parts[0]})'
        else:
            update = '无（v0）'
        rows.append((d.name, name, update))
    return rows


#: The status a profile gets until someone writes something better. Kept as one string so the
#: wording matches what the table already uses for unverified titles.
STATUS_UNVERIFIED = 'Profile present; not verified on a phone'
STATUS_UPSTREAM = 'Playable on an iPhone (upstream)'

#: Rows whose status is this fork's own testing (build → install → play here). Matched by title ID
#: so the text can change without the sync dropping the claim.
VERIFIED_HERE = {
    '0100000000010000': '**Verified here** — builds, installs and runs on an iPhone; '
                        '304,243 files into 127 archive shards, byte-verified',
    '0100F2C0115B6000': '**Verified here** — builds, installs and runs on an iPhone; '
                        '33-file archive',
    '01006F8002326000': '**Verified here** — builds, installs and runs smoothly on an iPhone; '
                        '103,645 files into 72 archive shards',
}


def refresh_game_table():
    """Rebuild the README's game table from `dm/games/`, keeping every hand-written status.

    Two things must not be lost when this rewrites the table:
    - a status someone wrote by hand (a verification, a caveat about a controller or touch
      controls). The existing row's status column is carried over verbatim; only rows that did
      not exist before get a default.
    - the sort order, which is by title ID and is what makes the table readable.

    The row count in the prose ("72 game profiles") and the "N verified" line are updated too,
    because those are the numbers a reader trusts and the ones that went stale.
    """
    readme_path = REPO / 'README.md'
    text = readme_path.read_text(encoding='utf-8')
    rows = game_rows()
    if not rows:
        say('  ! no game profiles found; the table is left alone')
        return

    # existing statuses, by title ID
    have: dict[str, str] = {}
    for ln in text.splitlines():
        m = re.match(r'\|\s*([^|]+?)\s*\|\s*`(0100[0-9A-F]{12})`\s*\|\s*([^|]*?)\s*\|\s*(.+?)\s*\|\s*$', ln)
        if m:
            have[m.group(2)] = m.group(4)

    lines = ['| Game | Title ID | Update required | Status |', '|---|---|---|---|']
    added = 0
    for tid, name, update in rows:
        if tid in have:
            status = have[tid]
        elif tid in VERIFIED_HERE:
            status = VERIFIED_HERE[tid]
        else:
            status = STATUS_UNVERIFIED
            added += 1
        lines.append(f'| {name} | `{tid}` | {update} | {status} |')
    table = '\n'.join(lines)

    # replace the table in place: from its header row to the last row that starts with "| "
    start = text.find('| Game | Title ID | Update required | Status |')
    if start < 0:
        say('  ! game table not found in README.md; profiles are in the tree but not listed')
        return
    end = start
    for ln in text[start:].splitlines(keepends=True):
        if ln.startswith('| ') or ln.startswith('|---'):
            end += len(ln)
        else:
            break
    new_text = text[:start] + table + '\n' + text[end:]

    # the counts in the prose and in the table's own caption
    n = len(rows)
    verified = sum(1 for ln in table.splitlines() if '**Verified here**' in ln)
    # Only the number changes; whatever emoji or wording precedes it is left exactly as written
    # (the feature table uses 🧩, the hero caption uses 🎯). A lookahead keeps the match to the
    # digits, so no part of the surrounding text is rewritten.
    new_text = re.sub(r'\d+(?= game profiles\*\*)', str(n), new_text)
    new_text = re.sub(r'\*\*\d+ verified end to end on this fork\*\*',
                      f'**{verified} verified end to end on this fork**', new_text)

    if new_text != text:
        readme_path.write_text(new_text, encoding='utf-8')
        say(f'  README table: {n} profiles' + (f' ({added} new)' if added else '') +
            f', {verified} verified here')
    else:
        say(f'  README table: {n} profiles, unchanged')


def refresh_game_table_zh():
    """Add rows for profiles the Chinese README's table is missing, and nothing else.

    The English table is rebuilt because its game names come straight from `game.tsv`. The
    Chinese one cannot be: its names are translated by hand ("超级马力欧 奥德赛" for "Super Mario
    Odyssey"), and so are its statuses. So this only appends rows for title IDs that are absent,
    using the English name as a placeholder — visible, and better than a game that silently is
    not listed — with the Chinese wording for "unverified" so it does not stand out as English
    text in a Chinese table. A translator replaces the name later; the row is never regenerated
    once it exists, so the translation is safe.

    Sorting is by title ID, like the English table, so the insert position is computed rather
    than appended at the end.
    """
    path = REPO / 'README.zh-CN.md'
    if not path.is_file():
        return
    text = path.read_text(encoding='utf-8')
    header = '| 游戏 | Title ID | 需要的更新包 | 状态 |'
    start = text.find(header)
    if start < 0:
        say('  ! Chinese game table not found; skipped')
        return
    # the rows of the table, in order
    block_start = start
    block_end = start
    for ln in text[start:].splitlines(keepends=True):
        if ln.startswith('| ') or ln.startswith('|---'):
            block_end += len(ln)
        else:
            break
    block = text[block_start:block_end]
    present = set(re.findall(r'`(0100[0-9A-F]{12})`', block))

    missing = [r for r in game_rows() if r[0] not in present]
    if not missing:
        say(f'  README.zh-CN table: complete ({len(present)} profiles)')
        return

    lines = block.rstrip('\n').splitlines()
    for tid, name, update in missing:
        row = f'| {name} | `{tid}` | {update} | 有 profile，未在手机上验证 |'
        # insert before the first row whose title ID sorts after this one
        pos = len(lines)
        for i, ln in enumerate(lines):
            m = re.search(r'`(0100[0-9A-F]{12})`', ln)
            if m and m.group(1) > tid:
                pos = i
                break
        lines.insert(pos, row)
    new_block = '\n'.join(lines) + '\n'
    new_text = text[:block_start] + new_block + text[block_end:]
    n = len(game_rows())
    new_text = re.sub(r'\*\*(?:[^ *] )?(\d+)( game profiles\*\*)',
                      lambda m: f'**{n}{m.group(2)}', new_text)
    path.write_text(new_text, encoding='utf-8')
    say(f'  README.zh-CN table: +{len(missing)} row(s) '
        f'({", ".join(t for t, _, _ in missing)}) — names are English until translated')


def refresh_badges():
    """Keep docs/badges/*.json in step with the tree, so the README badges need no hand edit.

    The game count is the number of title-ID profile folders (dm/games/0100…), which is what the
    README means by "N game profiles". The verified count is the rows marked verified in the
    README's own table, so the source of truth stays the README text rather than a second list."""
    gdir = REPO / 'dm/games'
    # Counted the same way the table is built (game_rows: a folder needs a game.tsv with a name),
    # not by directory name: upstream has documentation folders under dm/games/ that carry no
    # profile, and the badge said 80 while the table listed 79.
    games = len(game_rows())
    readme = (REPO / 'README.md').read_text(encoding='utf-8')
    verified = sum(1 for ln in readme.splitlines()
                   if ln.startswith('| ') and ('**Verified here**' in ln or 'Verified —' in ln))
    for name, label, value, color in [('games', 'games', games, 'blue'),
                                      ('verified', 'verified on an iPhone', verified, 'success')]:
        p = REPO / 'docs/badges' / f'{name}.json'
        old = p.read_text(encoding='utf-8') if p.exists() else ''
        new = json.dumps({'schemaVersion': 1, 'label': label, 'message': str(value), 'color': color},
                         indent=2) + '\n'
        if old != new:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(new, encoding='utf-8')
            say(f'  badge {name}: {value}')
    say(f'badges: {games} games, {verified} verified')
    # The README banners carry the same two numbers, so they are redrawn here: both scripts read
    # them from the tree (repo_numbers()), so this only runs when the tree changed.
    for script in ('frontend/screenshots/gen_hero.py', 'frontend/screenshots/gen_hero_en.py'):
        sp = REPO / script
        if not sp.exists():
            continue
        r = subprocess.run([sys.executable, str(sp)], capture_output=True, text=True, timeout=180)
        if r.returncode == 0:
            say(f'  banner: {Path(script).name} redrawn')
        else:
            say(f'  ! banner {Path(script).name} failed: {(r.stderr or "").strip()[:160]}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--old', required=True, help='upstream ref the fork is currently at')
    ap.add_argument('--new', help='upstream ref to sync to (not needed with --check-protection)')
    ap.add_argument('--skip-build', action='store_true')
    ap.add_argument('--check-protection', action='store_true',
                    help='only list the fork edits under the vendored paths that no patcher, OURS or DROPPED entry protects '
                         '(files that differ from --old in the fork\'s tree); exit 1 when there are any')
    a = ap.parse_args()
    if a.check_protection:
        edited, added = unprotected_edits(a.old)
        for p in edited: print(f'unprotected edit: {p}')
        for p in added: print(f'fork-only file (not in OURS): {p}')
        print(f'{len(edited)} unprotected edit(s), {len(added)} fork-only file(s) outside OURS')
        sys.exit(1 if edited else 0)
    if not a.new: ap.error('--new is required')

    say(f'sync {a.old[:7]} → {a.new[:7]}')
    changes = upstream_changes(a.old, a.new)
    say(f'{len(changes)} upstream file changes under {" ".join(VENDORED)}')
    try:
        check_unprotected(a.old, changes)
        apply_changes(a, changes)
        verify(a.skip_build, a.new)   # the ref that was synced, whatever the remote is called here
        refresh_game_table()
        refresh_game_table_zh()
        refresh_badges()
    except SyncProblem: pass
    if problems:
        say(f'\nSYNC FAILED: {len(problems)} problem(s) — nothing committed; the working tree is left as the script wrote it, `git checkout -- .` restores it')
        sys.exit(1)
    say('\nSYNC OK — commit and push from the caller')


if __name__ == '__main__':
    main()
