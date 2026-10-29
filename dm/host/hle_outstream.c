// nn::audio OutputStream (Switch 2's SDK): a PCM stream the game writes frames into, played by a virtual output device. On the real device it is a session of the audio
// service the SDK opens itself (detail::OutputStreamImpl); Donkey Kong Bananza's sound engine plays through it (main+0x57cc2c, main+0x187cf50) and had no sound under the host.
// Host: the stream feeds the host's PCM output (hle_audio.c dm_pcmout_*: the AudioUnit, or the silent sink). Written frames are converted to PCM16 and queued in blocks of at most
// 5 ms, so space frees up as the device plays rather than a whole buffer at a time; a thread takes the played blocks back and signals the game's event.
// The SDK's layout of OutputStreamType is not used: the host keeps its state by the guest object's address and writes only the impl pointer at +0, which the SDK's
// GetOutputStreamState reads as "not open" when it is null.
// StreamFormat (read from main's call sites): u32 sample format (1 int8, 2 int16, 3 int24, 4 int32, 5 float), u32 channel count, i32 sample rate.
// OpenOutputStream's two ints: the capacity in frames, and the free frames at which the event is signaled.
#include "hle.h"
#include <stdatomic.h>

void *dm_pcmout_open(int ch);
void dm_pcmout_close(void *k);
void dm_pcmout_start(void *k);
void dm_pcmout_stop(void *k);
int dm_pcmout_append(void *k, void *b);
void *dm_pcmout_released(void *k);
void dm_pcmout_wait(void *k, int ms);

enum { OS_CLOSED = 0, OS_STOPPED = 1, OS_STARTED = 2 };   // GetOutputStreamState (OutputStreamImpl +0x110: Open 1, Start 2, Stop 1)
enum { NBLK = 48, BLK_MIN = 240 };                      // blocks per stream (the host output queues at most 64), frames per block at least 5 ms at 48 kHz
typedef struct { void *ptr; uint64_t cap, size; } pcm_block;   // the host output's buffer descriptor (hle_audio.c gbuf)
typedef struct {
    void *pcm; void *ev;
    int fmt, ch, rate, cap, notify, blk;                 // blk: frames per block
    pcm_block b[NBLK]; int16_t *mem;
    int free_list[NBLK]; int nfree;                     // under m
    _Atomic int queued, state, run;
    pthread_mutex_t m; pthread_t th;
} ostream;

static ostream *os_get(const void *g) { ostream *s = objmap_get(g); if (!s) die("OutputStream %p was not opened by the host", g); return s; }
static int fmt_bytes(int f) { return f == 1 ? 1 : f == 2 ? 2 : f == 3 ? 3 : f == 4 || f == 5 ? 4 : 0; }

static void *os_reclaim(void *p) {
    ostream *s = p;
    while (atomic_load_explicit(&s->run, memory_order_acquire)) {
        dm_pcmout_wait(s->pcm, 20);
        int got = 0; pcm_block *b;
        while ((b = dm_pcmout_released(s->pcm))) {
            int frames = (int)(b->size / (2 * (uint64_t)s->ch));
            pthread_mutex_lock(&s->m); s->free_list[s->nfree++] = (int)(b - s->b); pthread_mutex_unlock(&s->m);
            atomic_fetch_sub_explicit(&s->queued, frames, memory_order_acq_rel); got = 1;
        }
        if (got && s->ev && s->cap - atomic_load_explicit(&s->queued, memory_order_acquire) >= s->notify) hle_signal_system_event(s->ev);
    }
    return NULL;
}

static uint32_t os_open(void **g, void *ev, const uint32_t *fmt, int cap, int notify) {
    int f = (int)fmt[0], ch = (int)fmt[1], rate = (int)fmt[2];
    if (!fmt_bytes(f) || f == 3 || ch < 1 || ch > 8 || cap <= 0) { LOG("[RED] OpenOutputStream: format %d, %d channels, capacity %d frames not supported", f, ch, cap); return 0x899; }
    if (rate != 48000) LOG("[RED] OpenOutputStream: %d Hz is played at 48 kHz (the host output does not resample)", rate);
    ostream *s = calloc(1, sizeof *s);
    s->fmt = f; s->ch = ch; s->rate = rate; s->cap = cap; s->notify = notify > 0 && notify <= cap ? notify : 1; s->ev = ev;
    s->blk = (cap + NBLK - 1) / NBLK; if (s->blk < BLK_MIN) s->blk = BLK_MIN;
    s->mem = calloc((size_t)NBLK * s->blk * ch, sizeof *s->mem);
    for (int i = 0; i < NBLK; i++) { s->b[i].ptr = s->mem + (size_t)i * s->blk * ch; s->b[i].cap = (uint64_t)s->blk * ch * 2; s->free_list[i] = NBLK - 1 - i; }
    s->nfree = NBLK;
    pthread_mutex_init(&s->m, NULL);
    atomic_init(&s->queued, 0); atomic_init(&s->state, OS_STOPPED); atomic_init(&s->run, 1);
    if (ev) hle_bind_system_event(ev, 1);
    s->pcm = dm_pcmout_open(ch);
    if (pthread_create(&s->th, NULL, os_reclaim, s)) die("cannot start the OutputStream thread");
    objmap_put(g, s); g[0] = g + 1;   // the impl pointer the SDK's own calls test
    LOG("audio OpenOutputStream %p: format %d, %d channels, %d Hz, capacity %d frames, event at %d free frames%s, blocks of %d frames", (void *)g, f, ch, rate, cap, s->notify, ev ? "" : " (no event)", s->blk);
    return 0;
}
HLE_FORCE("_ZN2nn5audio16OpenOutputStreamEPNS0_16OutputStreamTypeEPNS_2os11SystemEventEPvmRKNS0_15AudioDeviceNameERKNS0_12StreamFormatEii",
          "open an output stream: the host PCM output", uint32_t, h_os_Open, void **g, void *ev, void *work, size_t wsize, const char *dev, const uint32_t *fmt, int cap, int notify) {
    (void)work; (void)wsize; (void)dev; return os_open(g, ev, fmt, cap, notify);
}
HLE_FORCE("_ZN2nn5audio16OpenOutputStreamEPNS0_16OutputStreamTypeEPNS_2os11SystemEventEPvmRKNS0_15AudioDeviceNameERKNS0_12StreamFormatEiiNS_4util10BitFlagSetILi32ENS0_16OutputStreamFlagEEE",
          "open an output stream (with flags): the host PCM output, the flags are not used", uint32_t, h_os_OpenF, void **g, void *ev, void *work, size_t wsize, const char *dev, const uint32_t *fmt, int cap, int notify, uint32_t flags) {
    (void)work; (void)wsize; (void)dev; (void)flags; return os_open(g, ev, fmt, cap, notify);
}
HLE_FORCE("_ZN2nn5audio17StartOutputStreamEPNS0_16OutputStreamTypeE", "start an output stream", uint32_t, h_os_Start, void *g) {
    ostream *s = os_get(g);
    if (atomic_exchange_explicit(&s->state, OS_STARTED, memory_order_acq_rel) != OS_STARTED) dm_pcmout_start(s->pcm);
    return 0;
}
HLE_FORCE("_ZN2nn5audio16StopOutputStreamEPNS0_16OutputStreamTypeE", "stop an output stream: what was written is dropped", void, h_os_Stop, void *g) {
    ostream *s = os_get(g);
    if (atomic_exchange_explicit(&s->state, OS_STOPPED, memory_order_acq_rel) == OS_STARTED) dm_pcmout_stop(s->pcm);   // the output hands every queued block back
}
HLE_FORCE("_ZN2nn5audio17CloseOutputStreamEPNS0_16OutputStreamTypeE", "close an output stream", void, h_os_Close, void **g) {
    ostream *s = objmap_del(g); if (!s) return;
    atomic_store_explicit(&s->run, 0, memory_order_release); pthread_join(s->th, NULL);
    dm_pcmout_close(s->pcm);
    pthread_mutex_destroy(&s->m); free(s->mem); free(s);
    g[0] = NULL;
}
HLE_FORCE("_ZN2nn5audio20GetOutputStreamStateEPKNS0_16OutputStreamTypeE", "output stream state: 0 not open, 1 stopped, 2 started", int, h_os_State, const void *g) {
    ostream *s = objmap_get(g); return s ? atomic_load_explicit(&s->state, memory_order_acquire) : OS_CLOSED;
}
HLE_FORCE("_ZN2nn5audio29GetOutputStreamFreeFrameCountEPKNS0_16OutputStreamTypeE", "output stream free frames", int, h_os_Free, const void *g) {
    ostream *s = os_get(g); int n = s->cap - atomic_load_explicit(&s->queued, memory_order_acquire);
    return n > 0 ? n : 0;
}
HLE_FORCE("_ZN2nn5audio33GetOutputStreamCapacityFrameCountEPKNS0_16OutputStreamTypeE", "output stream capacity in frames", int, h_os_Cap, const void *g) { return os_get(g)->cap; }
HLE_FORCE("_ZN2nn5audio31GetOutputStreamFilledFrameCountEPKNS0_16OutputStreamTypeE", "output stream frames written and not played", int, h_os_Filled, const void *g) { return atomic_load_explicit(&os_get(g)->queued, memory_order_acquire); }

// One sample of the stream's format as PCM16
static int16_t to16(const uint8_t *p, int f) {
    switch (f) {
    case 1: return (int16_t)((int8_t)p[0] * 256);
    case 2: { int16_t v; memcpy(&v, p, 2); return v; }
    case 4: { int32_t v; memcpy(&v, p, 4); return (int16_t)(v >> 16); }
    default: { float v; memcpy(&v, p, 4); v *= 32768.f; return (int16_t)(v > 32767.f ? 32767.f : v < -32768.f ? -32768.f : v); }
    }
}
HLE_FORCE("_ZN2nn5audio17WriteOutputStreamEPNS0_16OutputStreamTypeEPKvi", "write frames to an output stream: as many as fit, returns the count", int, h_os_Write, void *g, const void *data, int frames) {
    ostream *s = os_get(g); const uint8_t *src = data; int fb = fmt_bytes(s->fmt) * s->ch, done = 0;
    int room = s->cap - atomic_load_explicit(&s->queued, memory_order_acquire);
    if (frames > room) frames = room;
    while (done < frames) {
        pthread_mutex_lock(&s->m); int i = s->nfree ? s->free_list[--s->nfree] : -1; pthread_mutex_unlock(&s->m);
        if (i < 0) break;   // every block is queued: the rest waits for the next write
        int n = frames - done; if (n > s->blk) n = s->blk;
        int16_t *dst = s->b[i].ptr;
        if (s->fmt == 2) memcpy(dst, src + (size_t)done * fb, (size_t)n * fb);
        else for (int k = 0; k < n * s->ch; k++) dst[k] = to16(src + (size_t)done * fb + (size_t)k * fmt_bytes(s->fmt), s->fmt);
        s->b[i].size = (uint64_t)n * s->ch * 2;
        atomic_fetch_add_explicit(&s->queued, n, memory_order_acq_rel);
        if (!dm_pcmout_append(s->pcm, &s->b[i])) {   // cannot happen with fewer blocks than the output's queue; undone if it does
            atomic_fetch_sub_explicit(&s->queued, n, memory_order_acq_rel);
            pthread_mutex_lock(&s->m); s->free_list[s->nfree++] = i; pthread_mutex_unlock(&s->m);
            break;
        }
        done += n;
    }
    return done;
}
