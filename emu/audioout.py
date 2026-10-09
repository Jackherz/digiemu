"""Host audio output for the emulated codec stream.

The SSI transmit model (`emu/ssi.py`) hands its `sink` every byte the
transmit DMA moves: on Digitakt mk1, stereo pairs of big-endian 32-bit words.
`frames_from_ssi` turns those into 16-bit little-endian stereo, and a player
queues them to the host.

`WaveOut` is Windows `winmm`, macOS AudioQueue or Linux libpulse-simple
(PulseAudio or PipeWire) through ctypes -- nothing to install. Elsewhere,
or when no device opens, `WavFile` records the same stream to a file so the
output can still be checked by ear afterwards.

The emulator runs slower than real time, so the stream arrives in bursts
with gaps between them. The player never blocks the emulator thread: a block
that finds every buffer still playing is dropped and counted, which is
audible as a gap rather than a stall.
"""
from __future__ import annotations

import array
import collections
import ctypes
import ctypes.util
import os
import struct
import sys
import threading
import time
import wave

WAVE_MAPPER = 0xFFFFFFFF
WAVE_FORMAT_PCM = 1
CALLBACK_NULL = 0
WHDR_DONE = 0x00000001
MMSYSERR_NOERROR = 0


def frames_from_ssi(data, word_bits=32, sample_bits=24):
    """Big-endian SSI words, left/right interleaved -> 16-bit LE stereo.

    `sample_bits` is how many of each `word_bits` word carry the sample,
    counted from the least significant bit (right-justified, the SSI's
    default). The top 16 of those bits are kept.
    """
    step = word_bits // 8
    count = len(data) // step
    if word_bits == 32 and sample_bits == 24:
        # The live-audio case, a few thousand times a second. Bits 23..8 of
        # a big-endian word are its bytes 1 and 2, and taken as a signed
        # 16-bit value they are exactly the 24-bit sample shifted right by 8
        # (the sign bit is among them). So: those two bytes, little-endian.
        data = bytes(data[:count * 4])
        out = bytearray(2 * count)
        out[0::2] = data[2::4]
        out[1::2] = data[1::4]
        return bytes(out)
    words = struct.unpack('>%d%s' % (count, 'I' if step == 4 else 'H'),
                          data[:count * step])
    shift = sample_bits - 16
    sign = 1 << (sample_bits - 1)
    mask = (1 << sample_bits) - 1
    out = []
    for w in words:
        v = w & mask
        if v & sign:
            v -= 1 << sample_bits
        out.append(v >> shift if shift >= 0 else v << -shift)
    return struct.pack('<%dh' % len(out), *out)


class _WaveFormatEx(ctypes.Structure):
    _fields_ = [('wFormatTag', ctypes.c_ushort),
                ('nChannels', ctypes.c_ushort),
                ('nSamplesPerSec', ctypes.c_uint),
                ('nAvgBytesPerSec', ctypes.c_uint),
                ('nBlockAlign', ctypes.c_ushort),
                ('wBitsPerSample', ctypes.c_ushort),
                ('cbSize', ctypes.c_ushort)]


class _WaveHdr(ctypes.Structure):
    pass


_WaveHdr._fields_ = [('lpData', ctypes.c_void_p),
                     ('dwBufferLength', ctypes.c_uint),
                     ('dwBytesRecorded', ctypes.c_uint),
                     ('dwUser', ctypes.c_size_t),
                     ('dwFlags', ctypes.c_uint),
                     ('dwLoops', ctypes.c_uint),
                     ('lpNext', ctypes.POINTER(_WaveHdr)),
                     ('reserved', ctypes.c_size_t)]


class _WinMMOut:
    """Queue 16-bit stereo PCM to the default Windows output device."""

    def __init__(self, rate=48000, channels=2, buffers=16, block_ms=20):
        if sys.platform != 'win32':
            raise OSError('WaveOut needs Windows')
        self.rate, self.channels = rate, channels
        self.gain = 1.0
        self.frame = 2 * channels
        self.block = max(self.frame,
                         rate * block_ms // 1000 * self.frame)
        self.block_ms = self.block * 1000 // (rate * self.frame)
        self.dropped = 0
        self.played = 0
        self._pending = bytearray()
        self._winmm = ctypes.WinDLL('winmm')
        fmt = _WaveFormatEx(WAVE_FORMAT_PCM, channels, rate,
                            rate * self.frame, self.frame, 16, 0)
        self._handle = ctypes.c_void_p()
        err = self._winmm.waveOutOpen(ctypes.byref(self._handle), WAVE_MAPPER,
                                      ctypes.byref(fmt), None, None,
                                      CALLBACK_NULL)
        if err != MMSYSERR_NOERROR:
            raise OSError('waveOutOpen failed: MMSYSERR %d' % err)
        self._bufs = [ctypes.create_string_buffer(self.block)
                      for _ in range(buffers)]
        self._hdrs = [_WaveHdr() for _ in range(buffers)]
        for buf, hdr in zip(self._bufs, self._hdrs):
            hdr.lpData = ctypes.cast(buf, ctypes.c_void_p)
            hdr.dwBufferLength = self.block
            hdr.dwFlags = 0
            self._winmm.waveOutPrepareHeader(self._handle, ctypes.byref(hdr),
                                             ctypes.sizeof(hdr))
            hdr.dwFlags |= WHDR_DONE          # free until first written

    def _free(self):
        for i, hdr in enumerate(self._hdrs):
            if hdr.dwFlags & WHDR_DONE:
                return i
        return None

    def queued(self):
        """Blocks handed to the device and not yet played."""
        return sum(1 for h in self._hdrs if not h.dwFlags & WHDR_DONE)

    def write(self, pcm, block=False, abort=None, timeout=None):
        """Append 16-bit LE PCM; full blocks go to the device at once.

        block=False drops a block that finds every buffer busy (live use:
        never stall the caller). block=True waits for a free buffer instead
        (playing a finished recording), until `abort()` says to give up.
        """
        pcm = apply_gain(pcm, self.gain)
        self._pending += pcm
        while len(self._pending) >= self.block:
            if not self._submit(bytes(self._pending[:self.block]),
                                block, abort):
                return
            del self._pending[:self.block]

    def _submit(self, chunk, block, abort):
        """Queue one full block. -> False if abandoned (abort)."""
        i = self._free()
        while i is None and block:
            if abort is not None and abort():
                return False
            time.sleep(0.002)
            i = self._free()
        if i is None:
            self.dropped += 1
            return True
        hdr = self._hdrs[i]
        ctypes.memmove(self._bufs[i], chunk, self.block)
        hdr.dwFlags &= ~WHDR_DONE
        self._winmm.waveOutWrite(self._handle, ctypes.byref(hdr),
                                 ctypes.sizeof(hdr))
        self.played += 1
        return True

    def drain(self, abort=None):
        """Send any partial block, then wait until everything has played.

        The tail is padded with silence to a whole block: a prepared header
        keeps the length it was prepared with.
        """
        if self._pending:
            tail = bytes(self._pending).ljust(self.block, b'\0')
            self._pending = bytearray()
            self._submit(tail, True, abort)
        while self.queued():
            if abort is not None and abort():
                return
            time.sleep(0.005)

    def close(self):
        if self._handle is None:
            return
        self._winmm.waveOutReset(self._handle)
        for hdr in self._hdrs:
            self._winmm.waveOutUnprepareHeader(self._handle, ctypes.byref(hdr),
                                               ctypes.sizeof(hdr))
        self._winmm.waveOutClose(self._handle)
        self._handle = None


class _AudioStreamBasicDescription(ctypes.Structure):
    _fields_ = [
        ('mSampleRate', ctypes.c_double),
        ('mFormatID', ctypes.c_uint32),
        ('mFormatFlags', ctypes.c_uint32),
        ('mBytesPerPacket', ctypes.c_uint32),
        ('mFramesPerPacket', ctypes.c_uint32),
        ('mBytesPerFrame', ctypes.c_uint32),
        ('mChannelsPerFrame', ctypes.c_uint32),
        ('mBitsPerChannel', ctypes.c_uint32),
        ('mReserved', ctypes.c_uint32),
    ]


class _AudioQueueBuffer(ctypes.Structure):
    _fields_ = [
        ('mAudioDataBytesCapacity', ctypes.c_uint32),
        ('mAudioData', ctypes.c_void_p),
        ('mAudioDataByteSize', ctypes.c_uint32),
        ('mUserData', ctypes.c_void_p),
        ('mPacketDescriptionCapacity', ctypes.c_uint32),
        ('mPacketDescriptions', ctypes.c_void_p),
        ('mPacketDescriptionCount', ctypes.c_uint32),
    ]


# AudioQueue's two callbacks. Declared once at module level so the same
# prototype is used both for the call and for the listener registration.
_AQ_BUFFER_CB = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_AudioQueueBuffer))
_AQ_PROP_CB = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)


# AudioQueue property IDs, as the four-character codes AudioToolbox uses.
K_AQ_IS_RUNNING = 0x72756e6e            # 'runn'
K_AQ_CURRENT_DEVICE = 0x64657669        # 'devi'
K_AQ_DEVICE_SAMPLE_RATE = 0x64737274    # 'dsrt'
K_AQ_MAX_OUTPUT_FRAME_COUNT = 0x6d6f6663  # 'mofc'

# The OSStatus values AudioQueue reports. Every one of them is swallowed by
# the old code: a live output that went silent reported a healthy queue.
AQ_ERRORS = {
    -66687: 'kAudioQueueErr_InvalidBuffer',
    -66686: 'kAudioQueueErr_BufferEmpty',
    -66685: 'kAudioQueueErr_DisposalPending',
    -66684: 'kAudioQueueErr_InvalidProperty',
    -66683: 'kAudioQueueErr_InvalidPropertySize',
    -66682: 'kAudioQueueErr_InvalidParameter',
    -66681: 'kAudioQueueErr_CannotStart',
    -66680: 'kAudioQueueErr_InvalidDevice',
    -66679: 'kAudioQueueErr_BufferInQueue',
    -66678: 'kAudioQueueErr_InvalidRunState',
    -66677: 'kAudioQueueErr_InvalidOfflineState',
    -66676: 'kAudioQueueErr_Permissions',
    -66675: 'kAudioQueueErr_InvalidPropertyValue',
    -66674: 'kAudioQueueErr_PrimeTimedOut',
    -66673: 'kAudioQueueErr_CodecNotFound',
    -66672: 'kAudioQueueErr_InvalidCodecAccess',
    -66671: 'kAudioQueueErr_QueueInvalidated',
    -66668: 'kAudioQueueErr_RecordUnderrun',
    -66632: 'kAudioQueueErr_EnqueueDuringReset',
    -66626: 'kAudioQueueErr_InvalidOfflineMode',
}
# The one failure that means the queue still owns the buffer, so it must not
# go back on the free list: doing so hands the same buffer out twice, and
# every later enqueue of it fails the same way.
AQ_BUFFER_IN_QUEUE = -66679


def osstatus_text(err):
    """A readable name for an OSStatus, for the log lines."""
    if err == 0:
        return 'noErr'
    name = AQ_ERRORS.get(err)
    if name is not None:
        return '%s (%d)' % (name, err)
    # Core Audio also reports four-character codes as an OSStatus.
    raw = struct.pack('>I', err & 0xFFFFFFFF)
    if all(0x20 <= b <= 0x7E for b in raw):
        return "'%s' (%d)" % (raw.decode('ascii'), err)
    return str(err)


def _fourcc(value):
    """A UInt32 as 'Buil' when it is printable, else as a plain number."""
    raw = value & 0xFFFFFFFF
    text = struct.pack('>I', raw)
    if all(0x20 <= b <= 0x7E for b in text):
        return "'%s' (%d)" % (text.decode('ascii'), value)
    return str(value)


class _AudioQueueOut:
    """Queue 16-bit stereo PCM to the default macOS output device via AudioQueue.

    Live output on macOS has been observed to play for a moment at start-up
    and then go silent for good, while the recording made from the same
    stream stays complete -- so the samples are rendered, and only the path
    to the device stops. Every OSStatus, every buffer callback and every
    queue property transition this backend sees is therefore counted and
    logged: `diagnostics()` is one line naming them, `close()` prints it,
    and the panel polls it on its own cadence while audio is live
    (Emulator.LIVE_DIAG_S in emu/gui.py).

    The counts that decide between the candidate causes:

    * `start_err` non-zero -- AudioQueueStart has stopped working, and the
      queue is silent while looking busy. `last_status` names the error.
    * `running=0` while `cb` still climbs -- the queue stopped itself.
    * `cb` frozen while `enq` climbs -- the device stopped taking buffers.
    * `dev` changing -- the default output device moved out from under the
      queue (display, Bluetooth, an aggregate device).
    * `hwrate` differing from `rate` -- the device is not at 48 kHz.
    * `wait_to` non-zero -- the queue ran out of free buffers and a blocking
      write gave up rather than wedge the emulator thread.
    """

    # A blocking write waits at most this long for a free buffer before it
    # drops the block (live use). Without a limit, a device that stops
    # handing buffers back wedges the calling thread forever -- the emulator
    # worker calls write(block=True), so it would take the run with it.
    WRITE_WAIT_S = 2.0
    EVENTS_KEPT = 64        # lifecycle events kept for the log

    _now = staticmethod(time.monotonic)
    _props = ((K_AQ_IS_RUNNING, 'running', ctypes.c_uint32, None),
              (K_AQ_CURRENT_DEVICE, 'dev', ctypes.c_uint32, 'fourcc'),
              (K_AQ_DEVICE_SAMPLE_RATE, 'hwrate', ctypes.c_double, 'rate'),
              (K_AQ_MAX_OUTPUT_FRAME_COUNT, 'maxframes', ctypes.c_uint32, None))

    def __init__(self, rate=48000, channels=2, buffers=16, block_ms=20):
        path = (os.environ.get('DIGIEMU_AUDIOTOOLBOX')
                or ctypes.util.find_library('AudioToolbox')
                or '/System/Library/Frameworks/AudioToolbox.framework/'
                   'AudioToolbox')
        try:
            self._lib = ctypes.CDLL(path)
        except OSError as exc:
            raise OSError('cannot load AudioToolbox: %s' % exc) from exc

        self.rate, self.channels = rate, channels
        self.frame = 2 * channels
        self.block = max(self.frame, rate * block_ms // 1000 * self.frame)
        # macOS AudioQueue can stay silent with very small buffers (see
        # StackOverflow reports of 150-sample buffers producing no sound).
        # Enforce a minimum that is known to work: at least 1024 frames and
        # at least 20 ms, matching the PulseAudio backend's 1024-frame
        # minimum and the Player's default.
        min_frames = 1024
        min_20ms = rate * 20 // 1000
        self.block = max(self.block, min_frames * self.frame,
                         min_20ms * self.frame)
        self.block_ms = self.block * 1000 // (rate * self.frame)
        self.buffers = buffers
        self.dropped = 0
        self.played = 0
        self.gain = 1.0
        self._pending = bytearray()
        self._lock = threading.Lock()
        self._closed = False

        # Lifecycle instrumentation. Everything here is read by the panel
        # and by close(); nothing in it may block or raise.
        self._t0 = self._now()
        self.enqueued = 0
        self.enqueue_err = 0
        self.start_ok = 0
        self.start_err = 0
        self.cb = 0                     # buffer-completion callbacks
        self.cb_bytes = 0
        self.cb_unknown = 0             # callbacks for a buffer we lost track of
        self.cb_recycled_twice = 0      # a buffer returned while already free
        self.lost_buffers = 0           # never came back from the queue
        self.wait_to = 0                # blocking writes that gave up
        self.last_status = 0
        self.last_cb_t = None
        self.last_cb_gap_s = 0.0
        self.running_events = 0
        self._events = collections.deque(maxlen=self.EVENTS_KEPT)
        self._props_seen = {}

        self._bind(self._lib)

        # kAudioFormatLinearPCM ('lpcm'), signed integer and packed: plain
        # interleaved 16-bit stereo, one frame per packet.
        fmt = _AudioStreamBasicDescription(
            float(rate), 0x6c70636d, (1 << 2) | (1 << 3), self.frame, 1,
            self.frame, channels, 16, 0
        )
        self._cb_type = _AQ_BUFFER_CB
        self._cb_fn = self._cb_type(self._on_buffer_done)
        self._prop_cb_fn = _AQ_PROP_CB(self._on_property)
        self._aq = ctypes.c_void_p()
        err = self._lib.AudioQueueNewOutput(
            ctypes.byref(fmt), self._cb_fn, None, None, None, 0,
            ctypes.byref(self._aq))
        if err != 0:
            self.last_status = err
            raise OSError('AudioQueueNewOutput failed: %s'
                          % osstatus_text(err))
        self._event('new', err)

        self._bufs = []
        self._buf_map = {}
        self._free_indices = []
        self._in_queue = set()          # indices the queue currently owns
        for i in range(buffers):
            buf = ctypes.POINTER(_AudioQueueBuffer)()
            err = self._lib.AudioQueueAllocateBuffer(self._aq, self.block,
                                                     ctypes.byref(buf))
            if err != 0:
                self.last_status = err
                self.close()
                raise OSError('AudioQueueAllocateBuffer failed: %s'
                              % osstatus_text(err))
            self._bufs.append(buf)
            # Use the buffer's own address as the key: addressof(contents)
            # can be fragile if contents returns a temporary copy, while the
            # pointer value itself is the stable identity AudioQueue returns
            # in the callback.
            self._buf_map[ctypes.cast(buf, ctypes.c_void_p).value] = i
            self._free_indices.append(i)

        err = self._lib.AudioQueueAddPropertyListener(
            self._aq, K_AQ_IS_RUNNING, self._prop_cb_fn, None)
        if err != 0:
            # Not fatal: the poll in _update_properties() still sees it.
            self._event('listener', err)
        self._update_properties()
        print('[audio] audioqueue: rate %d, block %d B (%d ms), %d buffers, '
              'device %s, hw rate %s'
              % (rate, self.block, self.block_ms, buffers,
                 self._props_seen.get('dev'),
                 self._props_seen.get('hwrate')), flush=True)

    @staticmethod
    def _bind(lib):
        """Declare the AudioQueue prototypes.

        Without argtypes, ctypes guesses each argument's type and assumes an
        int result; on 64-bit that is enough to lose an error code, which is
        the one thing this diagnosis must not do.
        """
        aq = ctypes.c_void_p
        buf = ctypes.POINTER(_AudioQueueBuffer)
        u32 = ctypes.c_uint32
        p_u32 = ctypes.POINTER(ctypes.c_uint32)
        p_aq = ctypes.POINTER(ctypes.c_void_p)
        lib.AudioQueueNewOutput.restype = ctypes.c_int32
        lib.AudioQueueNewOutput.argtypes = [
            ctypes.POINTER(_AudioStreamBasicDescription), _AQ_BUFFER_CB,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, u32, p_aq]
        lib.AudioQueueAllocateBuffer.restype = ctypes.c_int32
        lib.AudioQueueAllocateBuffer.argtypes = [aq, u32,
                                                 ctypes.POINTER(buf)]
        lib.AudioQueueEnqueueBuffer.restype = ctypes.c_int32
        lib.AudioQueueEnqueueBuffer.argtypes = [aq, buf, u32, ctypes.c_void_p]
        lib.AudioQueueStart.restype = ctypes.c_int32
        lib.AudioQueueStart.argtypes = [aq, ctypes.c_void_p]
        lib.AudioQueueStop.restype = ctypes.c_int32
        lib.AudioQueueStop.argtypes = [aq, ctypes.c_uint8]
        lib.AudioQueueReset.restype = ctypes.c_int32
        lib.AudioQueueReset.argtypes = [aq]
        lib.AudioQueueDispose.restype = ctypes.c_int32
        lib.AudioQueueDispose.argtypes = [aq, ctypes.c_uint8]
        lib.AudioQueueGetProperty.restype = ctypes.c_int32
        lib.AudioQueueGetProperty.argtypes = [aq, u32, ctypes.c_void_p, p_u32]
        lib.AudioQueueAddPropertyListener.restype = ctypes.c_int32
        lib.AudioQueueAddPropertyListener.argtypes = [aq, u32, _AQ_PROP_CB,
                                                      ctypes.c_void_p]
        lib.AudioQueueRemovePropertyListener.restype = ctypes.c_int32
        lib.AudioQueueRemovePropertyListener.argtypes = [aq, u32, _AQ_PROP_CB,
                                                         ctypes.c_void_p]

    # -- instrumentation -------------------------------------------------

    def _event(self, tag, status, extra=''):
        """Record one lifecycle event, for the log."""
        self._events.append((self._now() - self._t0, tag, status, extra))

    def _on_buffer_done(self, user_data, aq, buf_ptr):
        """AudioQueue has finished with a buffer: put it back in service.

        Runs on AudioQueue's own thread. It must not block and must not
        raise: an exception here costs a buffer for good, and once every
        buffer has been lost that way the queue is silent while every
        counter still looks healthy.
        """
        try:
            now = self._now()
            with self._lock:
                self.cb += 1
                if self.last_cb_t is not None:
                    self.last_cb_gap_s = now - self.last_cb_t
                self.last_cb_t = now
                addr = ctypes.cast(buf_ptr, ctypes.c_void_p).value
                idx = self._buf_map.get(addr)
                if idx is None:
                    self.cb_unknown += 1
                    return
                size = 0
                try:
                    size = buf_ptr.contents.mAudioDataByteSize
                except Exception:                              # noqa: BLE001
                    pass
                self.cb_bytes += size
                if idx in self._in_queue:
                    self._in_queue.discard(idx)
                if idx in self._free_indices:
                    # Returned twice: the queue handed back a buffer that
                    # was already free. Handing it out again would enqueue
                    # one buffer twice.
                    self.cb_recycled_twice += 1
                    return
                self._free_indices.append(idx)
        except Exception as exc:                               # noqa: BLE001
            self._event('cb_raise', -1, repr(exc))

    def _on_property(self, user_data, aq, prop_id):
        """A queue property changed. Only kAudioQueueProperty_IsRunning is
        watched, and only its transitions are interesting: they say whether
        the queue stopped itself rather than being starved of samples."""
        try:
            if prop_id != K_AQ_IS_RUNNING:
                return
            with self._lock:
                self.running_events += 1
                was = self._props_seen.get('running')
            now, _ = self._read_property(K_AQ_IS_RUNNING, ctypes.c_uint32)
            with self._lock:
                self._props_seen['running'] = now
            self._event('running', 0, '%s->%s' % (was, now))
        except Exception as exc:                               # noqa: BLE001
            self._event('prop_raise', -1, repr(exc))

    def _read_property(self, prop_id, ctype, render=None):
        """-> (value, status). value is None when the read failed.

        `ctype` is what AudioToolbox writes: kAudioQueueProperty_DeviceSample
        Rate is a Float64, the others a UInt32. Reading the rate as an
        integer gives a number that is not a sample rate at all.
        """
        aq = self._aq
        if aq is None or not aq.value:
            return None, -1
        data = ctype()
        n = ctypes.c_uint32(ctypes.sizeof(data))
        err = self._lib.AudioQueueGetProperty(aq, prop_id,
                                              ctypes.byref(data),
                                              ctypes.byref(n))
        if err != 0:
            return None, err
        value = data.value
        if render == 'fourcc':
            return _fourcc(value), 0
        if render == 'rate':
            return '%g Hz' % value, 0
        return value, 0

    def _update_properties(self):
        """Refresh the polled properties; -> {name: (before, after)}."""
        changed = {}
        for prop_id, name, ctype, render in self._props:
            value, err = self._read_property(prop_id, ctype, render)
            if err != 0:
                value = 'err:%s' % osstatus_text(err)
            with self._lock:
                before = self._props_seen.get(name)
                self._props_seen[name] = value
            if before != value:
                changed[name] = (before, value)
                self._event('prop', 0, '%s %s->%s' % (name, before, value))
        return changed

    def diagnostics(self):
        """One line describing the queue, for the log and the panel.

        Safe from any thread. Never raises: a diagnosis must not be able to
        take the panel down with it.
        """
        try:
            if not self._closed:
                self._update_properties()
            with self._lock:
                free = len(self._free_indices)
                in_queue = len(self._in_queue)
                cb, cb_bytes = self.cb, self.cb_bytes
                last_cb = self.last_cb_t
                gap = self.last_cb_gap_s
                props = dict(self._props_seen)
                counts = (self.enqueued, self.enqueue_err, self.start_ok,
                          self.start_err, self.dropped, self.played,
                          self.cb_unknown, self.cb_recycled_twice,
                          self.wait_to, self.running_events,
                          self.last_status)
            (enq, enq_err, s_ok, s_err, drop, played, unk, twice, wait_to,
             run_ev, last_status) = counts
            now = self._now()
            cb_age = 'n/a' if last_cb is None else '%.1fs' % (now - last_cb)
            return ('audioqueue: %s, block %d ms, enq %d (err %d), '
                    'start %d/%d err, cb %d (%.1f kB, %s ago, gap %.2fs), '
                    'free %d/%d, in-queue %d, dropped %d, wait-timeouts %d, '
                    'unknown-cb %d, double-recycle %d, running-events %d, '
                    'last status %s'
                    % ('up %.1fs' % (now - self._t0), self.block_ms,
                       enq, enq_err, s_ok, s_err, cb, cb_bytes / 1024.0,
                       cb_age, gap, free, len(self._buf_map), in_queue,
                       drop, wait_to, unk, twice, run_ev,
                       osstatus_text(last_status))) + ''.join(
                        ', %s %s' % (k, v)
                        for k, v in sorted(props.items()))
        except Exception as exc:                               # noqa: BLE001
            return 'audioqueue: diagnostics failed: %r' % (exc,)

    def event_log(self):
        """The last EVENTS_KEPT lifecycle events, oldest first."""
        with self._lock:
            items = list(self._events)
        return ['%7.3fs %-10s %s%s' % (t, tag, osstatus_text(status),
                                       ' ' + extra if extra else '')
                for t, tag, status, extra in items]

    def log(self, why=''):
        """Print the diagnostics line, and the event log when something is
        wrong. Called by close() and periodically by the panel."""
        line = self.diagnostics()
        if why:
            line = '%s (%s)' % (line, why)
        print('[audio] %s' % line, flush=True)
        with self._lock:
            bad = (self.enqueue_err or self.start_err or self.cb_unknown
                   or self.cb_recycled_twice or self.wait_to)
        if bad:
            for row in self.event_log():
                print('[audio]   %s' % row, flush=True)

    def faulted(self):
        """True once the queue has reported anything wrong.

        The panel asks before each periodic line, so a queue that fails in
        the first seconds is logged at once instead of at the next poll.
        """
        with self._lock:
            return bool(self.enqueue_err or self.start_err or self.wait_to
                        or self.cb_unknown or self.cb_recycled_twice)

    def poll(self):
        """Refresh the properties and report any change. -> True if one.

        The panel calls this on its own cadence while live output runs, so a
        device change or a queue that stopped itself shows up in the log
        near the time it happened."""
        changed = self._update_properties()
        for name, (before, after) in sorted(changed.items()):
            print('[audio] audioqueue: %s %s -> %s'
                  % (name, before, after), flush=True)
        return bool(changed)

    # -- the stream ------------------------------------------------------

    def queued(self):
        """Blocks handed to the device and not yet played."""
        with self._lock:
            if not self._bufs:
                return 0
            return len(self._in_queue)

    def _free(self):
        with self._lock:
            if self._free_indices:
                return self._free_indices.pop(0)
            return None

    def write(self, pcm, block=False, abort=None, timeout=None):
        """Append 16-bit LE PCM; full blocks go to the device at once.

        block=True waits for a free buffer instead of dropping, up to
        `timeout` seconds (WRITE_WAIT_S by default) or until `abort()` says
        to give up. The wait is bounded because the emulator's own thread
        writes here: an unbounded one would freeze the run, and with it the
        recording, the panel and the display.
        """
        pcm = apply_gain(pcm, self.gain)
        self._pending += pcm
        while len(self._pending) >= self.block:
            if not self._submit(bytes(self._pending[:self.block]), block,
                                abort, timeout):
                return
            del self._pending[:self.block]

    def _submit(self, chunk, block, abort=None, timeout=None):
        """Queue one full block. -> False if abandoned (abort)."""
        if self._closed or self._aq is None or not self._aq.value:
            return False
        i = self._free()
        if i is None and block:
            limit = self.WRITE_WAIT_S if timeout is None else timeout
            deadline = self._now() + limit
            while i is None:
                if abort is not None and abort():
                    return False
                if self._now() >= deadline:
                    # The queue has stopped handing buffers back. Drop the
                    # block, say so, and let the run carry on. The full dump
                    # happens on the first one only: a dead device would
                    # otherwise print this every WRITE_WAIT_S for the rest
                    # of the session, and the periodic poll covers the rest.
                    with self._lock:
                        self.wait_to += 1
                        first = self.wait_to == 1
                    self._event('wait_to', -1, '%.1fs' % limit)
                    if first:
                        self.log('blocking write gave up after %.1fs' % limit)
                    self.dropped += 1
                    return True
                time.sleep(0.002)
                i = self._free()
        if i is None:
            self.dropped += 1
            return True
        buf = self._bufs[i]
        ctypes.memmove(buf.contents.mAudioData, chunk, self.block)
        buf.contents.mAudioDataByteSize = self.block
        err = self._lib.AudioQueueEnqueueBuffer(self._aq, buf, 0, None)
        if err != 0:
            with self._lock:
                self.enqueue_err += 1
                self.last_status = err
                if err != AQ_BUFFER_IN_QUEUE:
                    # The queue did not take the buffer, so it is free
                    # again. BufferInQueue means it did: recycling it here
                    # is what turns one failure into a permanently
                    # corrupted free list.
                    self._free_indices.append(i)
            self._event('enqueue', err, 'buffer %d' % i)
            self.dropped += 1
            return True
        with self._lock:
            self._in_queue.add(i)
            self.enqueued += 1
        # Always (re)start the queue after a successful enqueue. If the
        # queue had run dry and stopped (or was never started), this
        # restarts it; if it is already running, AudioQueueStart is
        # idempotent and does nothing. The previous code only started once,
        # so after the first underrun live audio would stay silent forever
        # on macOS.
        err_start = self._lib.AudioQueueStart(self._aq, None)
        with self._lock:
            if err_start == 0:
                self.start_ok += 1
            else:
                self.start_err += 1
                self.last_status = err_start
        self._event('start', err_start)
        self.played += 1
        return True

    def drain(self, abort=None):
        """Send any partial block, then wait until everything has played."""
        if self._pending:
            tail = bytes(self._pending).ljust(self.block, b'\0')
            self._pending = bytearray()
            self._submit(tail, True, abort)
        while self.queued():
            if abort is not None and abort():
                return
            time.sleep(0.005)

    def close(self):
        if self._closed:
            return
        self._closed = True
        with self._lock:
            self.lost_buffers = len(self._in_queue)
            aq = self._aq
            self._aq = ctypes.c_void_p()
        try:
            if aq is not None and aq.value:
                self._lib.AudioQueueRemovePropertyListener(
                    aq, K_AQ_IS_RUNNING, self._prop_cb_fn, None)
                self._lib.AudioQueueStop(aq, True)
                self._lib.AudioQueueDispose(aq, True)
        except Exception:                                      # noqa: BLE001
            pass
        self.log('closed, %d buffer(s) still in the queue'
                 % self.lost_buffers)


PA_SAMPLE_S16LE = 3
PA_STREAM_PLAYBACK = 1
PA_DEFAULT = 0xFFFFFFFF


class _PaSampleSpec(ctypes.Structure):
    _fields_ = [('format', ctypes.c_int),
                ('rate', ctypes.c_uint32),
                ('channels', ctypes.c_uint8)]


class _PaBufferAttr(ctypes.Structure):
    _fields_ = [('maxlength', ctypes.c_uint32),
                ('tlength', ctypes.c_uint32),
                ('prebuf', ctypes.c_uint32),
                ('minreq', ctypes.c_uint32),
                ('fragsize', ctypes.c_uint32)]


class _PulseOut:
    """Queue 16-bit stereo PCM to the default Linux output via libpulse-simple.

    PipeWire serves the same API through pipewire-pulse. pa_simple_write
    blocks, so a writer thread feeds the server from a queue of at most
    `buffers` blocks; write() never waits on it, and a block that finds the
    queue full is dropped, as with the other backends. While that thread
    runs, nothing else uses the connection.

    The server is asked for a short buffer (tlength: two blocks, and at
    least 1024 frames, 21 ms). pa_simple sets PA_STREAM_ADJUST_LATENCY, so
    that is the target for the sink's latency and the stream's buffer
    together, and the server may settle on another figure.

    queued() counts the blocks still here and those the server has taken
    and not yet played. The second part is reckoned here, from what was
    written and when (_account): the server's own figure,
    pa_simple_get_latency, includes the device's latency, which is not ours
    to count, so it serves only as an upper limit.

    A write that fails (the server went away: PipeWire was restarted) ends
    the connection, and the writer thread tries for a new one every
    RETRY_S. Until it has one, queued() is 0, a block is dropped and a
    blocking write waits.

    DIGIEMU_PULSE_MS overrides tlength, in milliseconds. After a session
    that dropped blocks or lost the server, close() prints the counts.
    """

    RETRY_S = 1.0           # between tries for a new connection
    CLOSE_WAIT_S = 1.0      # how long close() waits for the writer thread
    SETTLE_WRITES = 10      # a new stream's latency figures are not used
    _libs = None            # (libpulse-simple, pa_strerror), loaded once
    _now = staticmethod(time.monotonic)

    @classmethod
    def _load(cls):
        """-> (libpulse-simple, pa_strerror), or OSError. Kept, because
        find_library runs ldconfig and a replay opens a new output."""
        if cls._libs is not None:
            return cls._libs
        try:
            lib = ctypes.CDLL(ctypes.util.find_library('pulse-simple')
                              or 'libpulse-simple.so.0')
            pa = ctypes.CDLL(ctypes.util.find_library('pulse')
                             or 'libpulse.so.0')
        except OSError as exc:
            raise OSError('cannot load libpulse-simple: %s' % exc) from exc
        pa.pa_strerror.restype = ctypes.c_char_p
        pa.pa_strerror.argtypes = [ctypes.c_int]
        lib.pa_simple_new.restype = ctypes.c_void_p
        lib.pa_simple_new.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
            ctypes.c_char_p, ctypes.POINTER(_PaSampleSpec), ctypes.c_void_p,
            ctypes.POINTER(_PaBufferAttr), ctypes.POINTER(ctypes.c_int)]
        err_p = ctypes.POINTER(ctypes.c_int)
        for name in ('pa_simple_drain', 'pa_simple_flush'):
            getattr(lib, name).argtypes = [ctypes.c_void_p, err_p]
        lib.pa_simple_write.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_size_t, err_p]
        lib.pa_simple_free.argtypes = [ctypes.c_void_p]
        lib.pa_simple_free.restype = None
        lib.pa_simple_get_latency.argtypes = [ctypes.c_void_p, err_p]
        lib.pa_simple_get_latency.restype = ctypes.c_uint64
        cls._libs = (lib, pa.pa_strerror)
        return cls._libs

    def __init__(self, rate=48000, channels=2, buffers=16, block_ms=20):
        self._lib, self._strerror = self._load()
        self.rate, self.channels = rate, channels
        self.gain = 1.0
        self.frame = 2 * channels
        self.block = max(self.frame, rate * block_ms // 1000 * self.frame)
        self.block_ms = self.block * 1000 // (rate * self.frame)
        self.buffers = buffers
        self.dropped = 0
        self.played = 0
        self.lost = 0               # connections ended by a failed call
        self._pending = bytearray()
        self._spec = _PaSampleSpec(PA_SAMPLE_S16LE, rate, channels)
        # 1024 frames is about 21 ms. A graph whose quantum is that long or
        # longer leaves the stream no room: raise DIGIEMU_PULSE_MS there.
        self._tlength = max(2 * self.block, 1024 * self.frame)
        try:
            ms = int(os.environ.get('DIGIEMU_PULSE_MS', '0'))
        except ValueError:
            ms = 0
        if ms > 0:
            self._tlength = max(self.block, rate * ms // 1000 * self.frame)
        self._attr = _PaBufferAttr(PA_DEFAULT, self._tlength, PA_DEFAULT,
                                   PA_DEFAULT, PA_DEFAULT)
        self._pa = self._connect()
        self._queue = []
        self._busy = False
        # What the server holds of ours (_account). It plays nothing until
        # it has prebuf, which it sets to tlength: _pre is the seconds it
        # is holding back until then, and None once it plays; _play_end is
        # when what it has will have been played.
        self._pre = 0.0
        self._play_end = 0.0
        self._writes = 0
        self._fresh = 0             # writes on this connection
        self._draining = False      # drain() waits for pa_simple_drain
        self._closing = False
        self._closed = False
        self._exited = False        # the writer thread has returned
        self._abandoned = False     # close() did not wait for it
        self._cond = threading.Condition()
        self._thread = threading.Thread(target=self._writer, daemon=True,
                                        name='digiemu-pulse')
        self._thread.start()

    def _why(self, err):
        return (self._strerror(err.value) or b'?').decode(errors='replace')

    def _seconds(self, nbytes):
        return nbytes / (self.rate * self.frame)

    def _connect(self):
        """-> a new pa_simple playback stream, or OSError."""
        err = ctypes.c_int(0)
        pa = self._lib.pa_simple_new(None, b'digiemu', PA_STREAM_PLAYBACK,
                                     None, b'playback',
                                     ctypes.byref(self._spec), None,
                                     ctypes.byref(self._attr),
                                     ctypes.byref(err))
        if not pa:
            raise OSError('pa_simple_new failed: %s' % self._why(err))
        return pa

    def _writer(self):
        try:
            self._serve()
        finally:
            # After an exception too: nothing may wait on a thread that
            # has gone.
            with self._cond:
                self._exited = True
                self._busy = False
                self._queue.clear()
                if self._abandoned:         # close() gave up waiting
                    self._release()
                self._cond.notify_all()

    def _serve(self):
        err = ctypes.c_int(0)
        while True:
            with self._cond:
                while (self._pa and not self._queue and not self._draining
                       and not self._closing):
                    self._cond.wait()
                if self._closing:
                    return
                pa, chunk = self._pa, None
                if pa and self._queue:
                    chunk = self._queue.pop(0)
                self._busy = bool(pa)
            if not pa:
                self._reconnect()
            elif chunk is None:             # drain(), and the queue is empty
                if self._lib.pa_simple_drain(pa, ctypes.byref(err)) < 0:
                    self._disconnect(pa, err, 0)
                    continue
                with self._cond:
                    self._pre, self._play_end = 0.0, 0.0
                    self._busy = self._draining = False
                    self._cond.notify_all()
            elif self._lib.pa_simple_write(pa, chunk, len(chunk),
                                           ctypes.byref(err)) < 0:
                self._disconnect(pa, err, 1)
            else:
                lat = self._lib.pa_simple_get_latency(pa, ctypes.byref(err))
                with self._cond:
                    self._account(self._seconds(len(chunk)), lat / 1e6)
                    self._busy = False
                    self._cond.notify_all()

    def _account(self, took, latency):
        """The server has just taken `took` seconds of audio and gives
        `latency` as the time until it is heard (writer thread, lock held).

        It plays in real time from the moment it has prebuf, so what it
        holds is what it was given since then less the time gone by. Two
        limits keep the count from drifting off, as the device's clock is
        not this one: it never holds more than tlength and the block it
        just took, and never more than its own latency figure, which
        counts the device's latency as well. (A failed figure is
        PA_USEC_INVALID, the largest there is, and limits nothing.)"""
        now = self._now()
        self._writes += 1
        self._fresh += 1
        if self._pre is None and self._play_end <= now:
            self._pre = 0.0                 # it ran dry, and holds back again
        if self._pre is None:
            self._play_end += took
        else:
            self._pre += took
            if self._pre < self._seconds(self._tlength):
                return
            self._pre, self._play_end = None, now + self._pre
        limit = self._seconds(self._tlength) + took
        if self._fresh > self.SETTLE_WRITES:
            limit = min(limit, latency)
        self._play_end = min(self._play_end, now + limit)

    def _disconnect(self, pa, err, held):
        """A call failed: free the connection and count the blocks it had
        (`held` in the call, and the queue) as dropped (writer thread)."""
        self._lib.pa_simple_free(pa)
        with self._cond:
            closing = self._closing
            gone = len(self._queue) + held
            self._queue.clear()
            self.played -= gone
            self.dropped += gone
            self.lost += 1
            self._pa = None
            self._busy = self._draining = False
            self._pre, self._play_end, self._fresh = 0.0, 0.0, 0
            self._cond.notify_all()
        if not closing:
            print('[audio] pulse: lost the server (%s); trying for a new '
                  'connection every %g s' % (self._why(err), self.RETRY_S),
                  flush=True)

    def _reconnect(self):
        """Wait RETRY_S, or until close(), then try once for a new
        connection (writer thread)."""
        with self._cond:
            if self._cond.wait_for(lambda: self._closing, self.RETRY_S):
                return
        try:
            pa = self._connect()
        except OSError:
            return
        with self._cond:
            self._pa = pa
            self._cond.notify_all()
            if self._closing:
                return
        print('[audio] pulse: connected again', flush=True)

    def _server_left(self):
        """Seconds of ours the server has and has not played (_account)."""
        with self._cond:
            if self._pre is not None:
                return self._pre
            return self._play_end - self._now()

    def queued(self):
        """Blocks handed to the device and not yet played: the ones still
        here and the ones the server has. A block counts until it has been
        played to the end, as with the other backends, so this is 0 only
        when the stream has run dry. Leaving the server's out made a
        stream that had just moved everything to the server look dry, and
        the caller then queued a second cushion on top of it -- as
        latency."""
        with self._cond:
            n = len(self._queue) + self._busy
        left = self._server_left()
        if left > 0:
            n += -(-round(left * self.rate) * self.frame // self.block)
        return n

    def write(self, pcm, block=False, abort=None, timeout=None):
        """Append 16-bit LE PCM; full blocks go to the device at once."""
        if self._closed:
            return
        pcm = apply_gain(pcm, self.gain)
        self._pending += pcm
        while len(self._pending) >= self.block:
            if not self._submit(bytes(self._pending[:self.block]),
                                block, abort):
                return
            del self._pending[:self.block]

    def _submit(self, chunk, block, abort):
        """Queue one full block. -> False if abandoned (abort)."""
        with self._cond:
            while (block and not self._closing and not self._exited
                   and (not self._pa or len(self._queue) >= self.buffers)):
                if abort is not None and abort():
                    return False
                self._cond.wait(0.005)
            if self._closing or self._exited:
                return False
            if not self._pa or len(self._queue) >= self.buffers:
                self.dropped += 1
                return True
            self._queue.append(chunk)
            self.played += 1
            self._cond.notify_all()
        return True

    def drain(self, abort=None):
        """Send any partial block, then wait until everything has played:
        the writer thread calls pa_simple_drain once the queue is empty,
        which also plays what the server was holding back for prebuf."""
        if self._pending:
            tail = bytes(self._pending).ljust(self.block, b'\0')
            self._pending = bytearray()
            self._submit(tail, True, abort)
        if abort is not None and abort():
            return                  # stopped: close() drops what is left
        with self._cond:
            if not self._pa:
                return
            self._draining = True
            self._cond.notify_all()
            while (self._draining and not self._closing
                   and not self._exited):
                if abort is not None and abort():
                    return
                self._cond.wait(0.005)

    def close(self):
        if self._closed:
            return
        self._closed = True
        with self._cond:
            self._closing = True
            self.played -= len(self._queue)
            self._queue.clear()
            self._cond.notify_all()
        self._thread.join(self.CLOSE_WAIT_S)
        with self._cond:
            if not self._exited:
                # A call that has not come back: the server stopped
                # answering. Freeing the connection under it is not safe,
                # so the writer thread (a daemon) frees it if it returns.
                self._abandoned = True
                print('[audio] pulse: the server is not answering; closed '
                      'without it', flush=True)
                return
        if self.dropped or self.lost:
            print('[audio] pulse: tlength %d ms, %d writes, dropped %d, '
                  'server lost %d times'
                  % (self._tlength * 1000 // (self.rate * self.frame),
                     self._writes, self.dropped, self.lost), flush=True)
        self._release()

    def _release(self):
        """Drop what the server still holds and free the connection."""
        if self._pa:
            err = ctypes.c_int(0)
            self._lib.pa_simple_flush(self._pa, ctypes.byref(err))
            self._lib.pa_simple_free(self._pa)
            self._pa = None


class WaveOut:
    """Queue 16-bit stereo PCM to the default host output device."""

    def __new__(cls, rate=48000, channels=2, buffers=16, block_ms=20):
        if sys.platform == 'win32':
            return _WinMMOut(rate, channels, buffers, block_ms)
        if sys.platform == 'darwin':
            return _AudioQueueOut(rate, channels, buffers, block_ms)
        if sys.platform.startswith('linux'):
            return _PulseOut(rate, channels, buffers, block_ms)
        raise OSError('WaveOut needs Windows, macOS or Linux (PulseAudio/PipeWire)')


class WavFile:
    """The same interface, recording to a .wav file instead."""

    def __init__(self, path, rate=48000, channels=2):
        self.rate, self.channels = rate, channels
        self.gain = 1.0
        self.dropped = 0
        self.played = 0
        self._w = wave.open(path, 'wb')
        self._w.setnchannels(channels)
        self._w.setsampwidth(2)
        self._w.setframerate(rate)

    def queued(self):
        return 0

    def write(self, pcm):
        pcm = apply_gain(pcm, self.gain)
        self._w.writeframes(pcm)
        self.played += 1

    def close(self):
        if self._w is not None:
            self._w.close()
            self._w = None


def trim_silence(pcm, channels=2, threshold=8, rate=48000, pad_ms=10):
    """16-bit LE PCM with leading and trailing near-silence cut off.

    Keeps `pad_ms` either side of the first and last sample whose magnitude
    exceeds `threshold`. -> b'' when nothing does.
    """
    frame = 2 * channels
    n = len(pcm) // frame
    if not n:
        return b''
    samples = array.array('h', bytes(pcm[:n * frame]))
    if sys.byteorder == 'big':
        samples.byteswap()
    first = next((i for i in range(len(samples))
                  if abs(samples[i]) > threshold), None)
    if first is None:
        return b''
    last = next(i for i in range(len(samples) - 1, -1, -1)
                if abs(samples[i]) > threshold)
    pad = rate * pad_ms // 1000
    start = max(0, first // channels - pad)
    end = min(n, last // channels + 1 + pad)
    return bytes(pcm[start * frame:end * frame])


def write_wav(path, pcm, rate=48000, channels=2):
    with wave.open(path, 'wb') as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def apply_gain(pcm, gain):
    """Multiply 16-bit LE PCM by `gain`, clipping to int16 range.

    Used by the panel's Master Volume knob: the hardware's volume pot is
    analog (not in the firmware's code table), so the emulator applies the
    knob position as a software gain before the samples reach the host
    audio device.
    """
    if gain == 1.0 or not pcm:
        return pcm
    samples = array.array('h')
    samples.frombytes(pcm)
    if sys.byteorder == 'big':
        samples.byteswap()
    # Promote to int32 so a gain > 1.0 doesn't overflow before clipping.
    out = array.array('h', (max(-32768, min(32767, int(s * gain)))
                              for s in samples))
    if sys.byteorder == 'big':
        out.byteswap()
    return out.tobytes()


class Player:
    """Plays one finished recording at a time, on its own thread.

    A new play() stops the one before. Never touches the emulator: the
    caller hands it a copy of the PCM.
    """

    def __init__(self, rate=48000, channels=2):
        self.rate, self.channels = rate, channels
        self.gain = 1.0             # the panel's Master Volume
        self.error = None
        self._thread = None
        self._stop = threading.Event()

    @property
    def playing(self):
        return self._thread is not None and self._thread.is_alive()

    def play(self, pcm):
        self.stop()
        self._stop = threading.Event()
        stop = self._stop
        self._thread = threading.Thread(target=self._run, args=(pcm, stop),
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None

    def _run(self, pcm, stop):
        try:
            out = WaveOut(self.rate, self.channels)
        except OSError as exc:
            self.error = str(exc)
            return
        # A tenth of a second at a time, so turning Master Volume during a
        # replay is heard at once rather than on the next PLAY.
        step = self.rate * self.channels * 2 // 10
        try:
            for i in range(0, len(pcm), step):
                if stop.is_set():
                    break
                out.gain = self.gain
                out.write(pcm[i:i + step], block=True, abort=stop.is_set)
            out.drain(abort=stop.is_set)
        finally:
            out.close()


def open_output(rate=48000, channels=2, fallback_path=None):
    """The host device if there is one, else a WAV file if a path is given."""
    try:
        return WaveOut(rate, channels)
    except OSError as exc:
        if fallback_path is None:
            raise
        print('[audio] no output device (%s); recording to %s'
              % (exc, fallback_path), flush=True)
        return WavFile(fallback_path, rate, channels)
