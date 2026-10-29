// Shared-font ownership and LoadFont forwarding through the real forced registration; no game module or font file is needed.
#include "../hle_sdk16.c"
#include <mach-o/getsect.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <sys/resource.h>

static int checks, fails, calls, lookups, missing;
static void *seen_self;
static char *seen_name;
static const void *seen_data;
static unsigned seen_index, seen_capacity;
static uint32_t status;
static int oom_left, init_calls, fini_calls;
static void *init_self, *init_buf;
static unsigned init_sz;
#define CHECK(c, msg) do { checks++; if (!(c)) { fails++; fprintf(stderr, "FAIL: %s\n", msg); } } while (0)
static uint32_t original(void *self, char *name, const void *data, unsigned index, unsigned capacity) {
    calls++; seen_self = self; seen_name = name; seen_data = data;
    seen_index = index; seen_capacity = capacity;
    if (oom_left) { oom_left--; return 0xc9; }
    return status;
}
static uint32_t fake_init(void *self, void *buf, unsigned sz) { init_calls++; init_self = self; init_buf = buf; init_sz = sz; return 0; }
static void fake_fini(void *self) { (void)self; fini_calls++; }
void *guest_sym(const char *name) {
    lookups++;
    if (!strcmp(name, "_ZN2nn6fontll18ScalableFontEngine10InitializeEPvj")) return fake_init;
    if (!strcmp(name, "_ZN2nn6fontll18ScalableFontEngine8FinalizeEv")) return fake_fini;
    CHECK(!strcmp(name, "_ZN2nn6fontll18ScalableFontEngine8LoadFontEPcPKvjj"), "original symbol lookup");
    return missing ? NULL : original;
}
module_t g_mod[MAX_MOD];
int g_nmod;
uint64_t g_t0_ns;
uint64_t now_ns(void) { return 0; }
int dm_sdk_major(void) { return 20; }
int dm_main_imports(const char *prefix) { (void)prefix; return 1; }
module_t *module_of(const void *p) { (void)p; return NULL; }
void dm_backtrace(uint64_t fp) { (void)fp; }
void hle_bind_system_event(void *ev, int autoclear) { (void)ev; (void)autoclear; }
void hle_signal_system_event(void *ev) { (void)ev; }
uint64_t hle_system_event_binding(void *ev) { (void)ev; return 1; }
int hle_signal_system_event_binding(uint64_t binding) { (void)binding; return 1; }
uint32_t hle_hid_connected_npad(void) { return 0; }
void dm_host_helper_qos(qos_class_t q) { (void)q; }
extern const struct mach_header_64 _mh_execute_header;
int main(void) {
    unsigned long bytes = 0;
    const hle_force_ent *e = (const hle_force_ent *)getsectiondata(&_mh_execute_header, "__DATA", "swforce", &bytes);
    uint32_t (*load)(void *, char *, const void *, unsigned, unsigned) = NULL;
    unsigned count = 0;
    for (size_t i = 0; e && i < bytes / sizeof *e; i++) {
        if (strcmp(e[i].name, "_ZN2nn6fontll18ScalableFontEngine8LoadFontEPcPKvjj")) continue;
        load = e[i].fn; count++;
        CHECK(e[i].why && e[i].why[0], "takeover has a reason");
    }
    CHECK(count == 1 && load, "exact LoadFont forced registration exists once");
    if (!load) return 1;
    // A missing SDK implementation must fail loudly rather than fabricate a successful font load.
    pid_t child = fork();
    CHECK(child >= 0, "missing-original child starts");
    if (!child) { struct rlimit r = {0, 0}; setrlimit(RLIMIT_CORE, &r); missing = 1; load(NULL, NULL, NULL, 0, 0); _exit(1); }
    if (child > 0) { int s; waitpid(child, &s, 0); CHECK(WIFSIGNALED(s) && WTERMSIG(s) == SIGABRT, "missing original aborts"); }
    uint8_t standard[64] = {0}, extension[64] = {0}, unrelated[64] = {0};
    uint32_t marker = 0x18029a7fu;
    memcpy(standard, &marker, 4); memcpy(extension, &marker, 4); memcpy(unrelated, &marker, 4);
    char name[50] = {0}; int self;
    const void *before[] = {NULL, unrelated, standard, extension};
    for (unsigned i = 0; i < sizeof before / sizeof *before; i++) {
        status = 403; int n = calls;
        CHECK(load(&self, name, before[i], 17, sizeof name) == status, "uninitialized fonts preserve result");
        CHECK(calls == n + 1 && seen_data == before[i], "uninitialized fonts leave all data unchanged");
    }
    std_f = (font_t){standard, sizeof standard}; ext_f = (font_t){extension, sizeof extension};
    const void *input[] = {standard, extension, standard + 8, extension + 8, standard + 1, extension + 9, unrelated, NULL};
    const void *expect[] = {standard + 8, extension + 8, standard + 8, extension + 8, standard + 1, extension + 9, unrelated, NULL};
    const uint32_t results[] = {0, 403, 301, 0x80000001u, ~0u};
    for (unsigned r = 0; r < sizeof results / sizeof *results; r++) for (unsigned i = 0; i < sizeof input / sizeof *input; i++) {
        int n = calls; status = results[r];
        CHECK(load(&self, name, input[i], ~0u, sizeof name) == status, "SDK status preserved");
        CHECK(calls == n + 1 && seen_data == expect[i], "only exact host bases skip the header, once");
        CHECK(seen_self == &self && seen_name == name && seen_index == ~0u && seen_capacity == sizeof name, "all other arguments preserved");
    }
    ext_f = std_f;
    CHECK(load(&self, name, standard, 0, 0) == status && seen_data == standard + 8, "extension fallback to standard");
    CHECK(seen_index == 0 && seen_capacity == 0, "zero unsigned arguments preserved");
    CHECK(lookups == 1, "original resolved once");
    CHECK(!memcmp(standard, unrelated, sizeof standard) && !memcmp(extension, unrelated, sizeof extension), "input bytes unchanged");
    // Error 201 on a font the host does not own is the game's. On the host stand-in, the engine is rebuilt and the load is repeated with a smaller font.
    uint32_t (*init)(void *, void *, unsigned) = NULL;
    for (size_t i = 0; e && i < bytes / sizeof *e; i++)
        if (!strcmp(e[i].name, "_ZN2nn6fontll18ScalableFontEngine10InitializeEPvj")) init = e[i].fn;
    CHECK(init, "Initialize forced registration exists");
    if (!init) { printf("shared font: %d checks, %d failed\n", checks, fails); return fails != 0; }
    uint8_t eng, work[8], other;
    char ll[] = "/tmp/nx2-fontll-XXXXXX"; int fd = mkstemp(ll);
    CHECK(fd >= 0 && write(fd, "OTTO", 4) == 4, "smaller-font file");
    if (fd >= 0) close(fd);
    setenv("DM_FONT_LL", ll, 1);
    oom_left = 1; int n = calls;
    CHECK(load(&other, name, standard, 3, 9) == 0xc9, "OOM without a recorded work buffer is returned");
    CHECK(calls == n + 1 && fini_calls == 0, "no retry without a work buffer");
    oom_left = 1; n = calls;
    CHECK(load(&eng, name, unrelated, 3, 9) == 0xc9, "a game font that does not fit is not replaced");
    CHECK(calls == n + 1, "foreign font is not retried");
    CHECK(init(&eng, work, 0xc800) == 0 && init_calls == 1 && init_self == &eng && init_buf == work && init_sz == 0xc800, "Initialize recorded the work buffer");
    oom_left = 1; status = 0; n = calls; int in = init_calls, fn = fini_calls;
    CHECK(load(&eng, name, standard, 4, 11) == 0, "stand-in OOM retries with the smaller font");
    CHECK(calls == n + 2 && seen_data != standard && seen_data != standard + 8, "second load is the smaller font");
    CHECK(seen_self == &eng && seen_name == name && seen_index == 4 && seen_capacity == 11, "retry keeps the other arguments");
    CHECK(fini_calls == fn + 1 && init_calls == in + 1 && init_buf == work && init_sz == 0xc800, "engine rebuilt in the same work buffer");
    unlink(ll);
    printf("shared font: %d checks, %d failed\n", checks, fails);
    return fails != 0;
}
