/* A stand-in for macOS AudioToolbox, for exercising emu/audioout.py's
 * AudioQueue backend off a Mac. It implements the same call signatures and
 * the same AudioQueueBuffer layout, runs a consumer thread that "plays"
 * each enqueued buffer and then fires the completion callback, and can be
 * told to misbehave so the failure modes can be reproduced on purpose. */
#define _GNU_SOURCE
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

typedef int32_t OSStatus;
typedef uint32_t UInt32;
typedef uint8_t Boolean;
typedef double Float64;

typedef struct {
    Float64 mSampleRate;
    UInt32 mFormatID, mFormatFlags, mBytesPerPacket, mFramesPerPacket;
    UInt32 mBytesPerFrame, mChannelsPerFrame, mBitsPerChannel, mReserved;
} ASBD;

typedef struct { UInt32 mFlags; UInt32 mReserved; } AQBufferTime;
typedef struct {
    Float64 mSampleTime; UInt32 mHostTime; Float64 mRateScalar;
    uint64_t mWordClock; uint64_t mSMPTETime[6];
    UInt32 mFlags; UInt32 mReserved;
} AQTimeStamp;

typedef struct AQBuffer {
    const UInt32 mAudioDataBytesCapacity;
    void *const mAudioData;
    UInt32 mAudioDataByteSize;
    void *mUserData;
    const UInt32 mPacketDescriptionCapacity;
    void *const mPacketDescriptions;
    UInt32 mPacketDescriptionCount;
} AQBuffer;

typedef void (*AQOutCB)(void *user, void *aq, AQBuffer *buf);
typedef void (*AQPropCB)(void *user, void *aq, UInt32 propId);

#define MAXBUF 64
struct queue {
    ASBD fmt;
    AQOutCB cb; void *cbUser;
    AQPropCB pcb; void *pcbUser; UInt32 pcbProp;
    AQBuffer *bufs[MAXBUF]; int nbufs;
    void *data[MAXBUF];
    AQBuffer *ring[MAXBUF]; int rhead, rcount;   /* enqueued, waiting to play */
    pthread_mutex_t m; pthread_cond_t cv;
    int running, disposed, started_count, stopped_count, callbacks;
    int fail_start_after, stop_on_underrun, played;
    pthread_t th; int haveTh;
};

static struct queue *g_q;

static long envl(const char *k, long d) {
    const char *v = getenv(k); return v ? strtol(v, 0, 10) : d;
}

static void notify_running(struct queue *q, int value) {
    if (q->pcb && q->pcbProp == 0x72756e6e /* 'runn' */)
        q->pcb(q->pcbUser, q, 0x72756e6e);
    (void)value;
}

static void *consumer(void *arg) {
    struct queue *q = arg;
    for (;;) {
        pthread_mutex_lock(&q->m);
        while (!q->rcount && !q->disposed)
            pthread_cond_wait(&q->cv, &q->m);
        if (q->disposed) { pthread_mutex_unlock(&q->m); return 0; }
        AQBuffer *b = q->ring[q->rhead];
        q->rhead = (q->rhead + 1) % MAXBUF; q->rcount--;
        q->played++;
        pthread_mutex_unlock(&q->m);
        /* one frame is 4 bytes at 48k: sleep roughly the buffer's duration */
        long us = (long)(b->mAudioDataByteSize / 4.0 / 48.0 * 1000.0);
        if (us > 200000) us = 200000;
        usleep(us > 0 ? us : 1);
        if (q->stop_on_underrun) {
            pthread_mutex_lock(&q->m);
            if (!q->rcount && q->running) { q->running = 0; q->stopped_count++; }
            pthread_mutex_unlock(&q->m);
            notify_running(q, 0);
        }
        pthread_mutex_lock(&q->m);
        q->callbacks++;
        AQOutCB cb = q->cb; void *u = q->cbUser;
        pthread_mutex_unlock(&q->m);
        if (cb) cb(u, q, b);
    }
}

OSStatus AudioQueueNewOutput(const ASBD *fmt, AQOutCB cb, void *cbUser,
                             void *rl, void *mode, UInt32 flags, void **outAQ) {
    struct queue *q = calloc(1, sizeof *q);
    q->fmt = *fmt; q->cb = cb; q->cbUser = cbUser;
    if (envl("AQSTUB_STALL", 0)) { g_q = q; *outAQ = q; return 0; }
    q->fail_start_after = envl("AQSTUB_FAIL_START_AFTER", -1);
    q->stop_on_underrun = envl("AQSTUB_STOP_ON_UNDERRUN", 0);
    pthread_mutex_init(&q->m, 0); pthread_cond_init(&q->cv, 0);
    pthread_create(&q->th, 0, consumer, q); q->haveTh = 1;
    g_q = q; *outAQ = q;
    return envl("AQSTUB_FAIL_NEW", 0);
}

OSStatus AudioQueueAllocateBuffer(void *aq, UInt32 size, AQBuffer **out) {
    struct queue *q = aq;
    if (q->nbufs >= MAXBUF) return -66682;
    void *data = calloc(1, size);
    /* the struct has const members; build it over raw storage */
    struct { UInt32 a; void *b; UInt32 c; void *d; UInt32 e; void *f; UInt32 g; } *raw
        = calloc(1, sizeof *raw);
    raw->a = size; raw->b = data; raw->c = 0; raw->d = 0;
    raw->e = 0; raw->f = 0; raw->g = 0;
    q->bufs[q->nbufs] = (AQBuffer *)raw; q->data[q->nbufs] = data; q->nbufs++;
    *out = (AQBuffer *)raw;
    return 0;
}

OSStatus AudioQueueEnqueueBuffer(void *aq, AQBuffer *b, UInt32 n, void *pd) {
    struct queue *q = aq;
    OSStatus forced = envl("AQSTUB_FAIL_ENQUEUE", 0);
    /* AQSTUB_ENQUEUE_TOOK models kAudioQueueErr_BufferInQueue: the call
     * reports a failure but the queue does own the buffer afterwards, which
     * is the one status whose buffer must not go back on the free list. */
    int took = envl("AQSTUB_ENQUEUE_TOOK", 0);
    if (forced && !took) return forced;  /* a failure means it took nothing */
    pthread_mutex_lock(&q->m);
    for (int i = 0; i < q->rcount; i++)
        if (q->ring[(q->rhead + i) % MAXBUF] == b) {
            pthread_mutex_unlock(&q->m);
            return -66679;  /* kAudioQueueErr_BufferInQueue */
        }
    if (q->rcount >= MAXBUF) { pthread_mutex_unlock(&q->m); return -66686; }
    if (q->disposed) { pthread_mutex_unlock(&q->m); return -66685; }
    q->ring[(q->rhead + q->rcount) % MAXBUF] = b; q->rcount++;
    pthread_cond_signal(&q->cv);
    pthread_mutex_unlock(&q->m);
    return forced;
}

OSStatus AudioQueueStart(void *aq, const AQTimeStamp *t) {
    struct queue *q = aq;
    pthread_mutex_lock(&q->m);
    q->started_count++;
    int n = q->started_count;
    pthread_mutex_unlock(&q->m);
    if (q->fail_start_after >= 0 && n > q->fail_start_after)
        return -66681; /* kAudioQueueErr_CannotStart */
    pthread_mutex_lock(&q->m); q->running = 1; pthread_mutex_unlock(&q->m);
    notify_running(q, 1);
    return 0;
}

OSStatus AudioQueueStop(void *aq, Boolean imm) {
    struct queue *q = aq;
    pthread_mutex_lock(&q->m);
    q->running = 0; q->stopped_count++;
    if (imm) { q->rcount = 0; q->rhead = 0; }
    pthread_mutex_unlock(&q->m);
    notify_running(q, 0);
    return 0;
}

OSStatus AudioQueueReset(void *aq) {
    struct queue *q = aq;
    pthread_mutex_lock(&q->m); q->rcount = 0; q->rhead = 0; pthread_mutex_unlock(&q->m);
    return 0;
}

OSStatus AudioQueueDispose(void *aq, Boolean imm) {
    struct queue *q = aq;
    pthread_mutex_lock(&q->m);
    q->disposed = 1; q->running = 0;
    pthread_cond_broadcast(&q->cv);
    pthread_mutex_unlock(&q->m);
    if (q->haveTh) { pthread_join(q->th, 0); q->haveTh = 0; }
    for (int i = 0; i < q->nbufs; i++) { free(q->data[i]); free(q->bufs[i]); }
    pthread_mutex_destroy(&q->m); pthread_cond_destroy(&q->cv);
    free(q);
    return 0;
}

OSStatus AudioQueueGetProperty(void *aq, UInt32 prop, void *out, UInt32 *ioSize) {
    struct queue *q = aq;
    UInt32 need;
    switch (prop) {
    case 0x72756e6e: { UInt32 v; pthread_mutex_lock(&q->m); v = q->running;
                       pthread_mutex_unlock(&q->m); need = 4; memcpy(out, &v, 4); break; }
    case 0x64657669: { UInt32 v = 0x4275696c /* 'Buil' */; need = 4;
                       memcpy(out, &v, 4); break; }
    case 0x64737274: { Float64 v = q->fmt.mSampleRate; need = 8;
                       memcpy(out, &v, 8); break; }
    case 0x6d6f6663: { UInt32 v = 512; need = 4; memcpy(out, &v, 4); break; }
    default: return -66684; /* kAudioQueueErr_InvalidProperty */
    }
    if (ioSize) *ioSize = need;
    return 0;
}

OSStatus AudioQueueAddPropertyListener(void *aq, UInt32 prop, AQPropCB cb, void *u) {
    struct queue *q = aq;
    q->pcb = cb; q->pcbUser = u; q->pcbProp = prop;
    return 0;
}

OSStatus AudioQueueRemovePropertyListener(void *aq, UInt32 prop, AQPropCB cb, void *u) {
    struct queue *q = aq; q->pcb = 0; q->pcbUser = 0;
    return 0;
}

/* test hooks */
int aqstub_started(void) { return g_q ? g_q->started_count : -1; }
int aqstub_callbacks(void) { return g_q ? g_q->callbacks : -1; }
int aqstub_stopped(void) { return g_q ? g_q->stopped_count : -1; }
int aqstub_running(void) { return g_q ? g_q->running : -1; }
