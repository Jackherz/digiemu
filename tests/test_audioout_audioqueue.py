"""emu/audioout.py's macOS AudioQueue backend, exercised off a Mac.

Live output on macOS has been observed to play for a moment at start-up and
then go silent for good, while the sequencer, the recording and the replay
of that recording all carry on -- so the samples are rendered and only the
path to the device stops. Nothing in the old backend could say why: every
OSStatus was discarded, the queue's own state was never read, and the buffer
callbacks were not counted.

This drives the real backend against a stand-in AudioToolbox
(tests/fixtures/aqstub/aqstub.c) that has the same call signatures, the same
AudioQueueBuffer layout and a consumer thread that plays each enqueued
buffer and then fires the completion callback. It can be told to stall, to
fail AudioQueueStart and to fail AudioQueueEnqueueBuffer, so each failure
mode is reproduced on purpose rather than waited for on a Mac.

Three regressions are pinned here, all reproduced on the code as it was
before this change:

  * a failed AudioQueueEnqueueBuffer put the buffer back on the free list
    whatever the status, including kAudioQueueErr_BufferInQueue, where the
    queue still owns it -- so the free list grew past the number of buffers
    (16 entries for 8) and queued() went negative (-8);
  * AudioQueueStart failing was swallowed: played kept climbing and the
    queue looked healthy while producing nothing;
  * write(block=True) with no abort spun forever. The emulator's own thread
    calls it, so a device that stopped handing buffers back froze the run.

Skipped where there is no C compiler.
"""
import contextlib
import ctypes
import io
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from emu import audioout

HERE = os.path.dirname(os.path.abspath(__file__))
STUB_SRC = os.path.join(HERE, 'fixtures', 'aqstub', 'aqstub.c')


def _build_stub():
    """Compile the AudioToolbox stand-in. -> its path, or None."""
    cc = shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')
    if cc is None or not os.path.exists(STUB_SRC):
        return None
    keep = tempfile.mkdtemp(prefix='digiemu-aqstub-')
    lib = os.path.join(
        keep, 'libaqstub' + ('.dylib' if sys.platform == 'darwin' else '.so'))
    try:
        subprocess.run([cc, '-O2', '-fPIC', '-shared', '-pthread', '-o', lib,
                        STUB_SRC], check=True, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        shutil.rmtree(keep, ignore_errors=True)
        return None
    return lib


STUB = _build_stub()
NEEDS_STUB = unittest.skipUnless(STUB, 'needs a C compiler and aqstub.c')


@NEEDS_STUB
class AudioQueueLifecycleTest(unittest.TestCase):
    """The queue's lifecycle, with every status the backend can see."""

    def setUp(self):
        self._saved = dict(os.environ)
        os.environ['DIGIEMU_AUDIOTOOLBOX'] = STUB
        for key in list(os.environ):
            if key.startswith('AQSTUB_'):
                del os.environ[key]
        self.log = io.StringIO()
        redirect = contextlib.redirect_stdout(self.log)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def stub_fails(self, **knobs):
        os.environ.update({('AQSTUB_' + k).upper(): str(v)
                           for k, v in knobs.items()})

    def queue(self, buffers=8, block_ms=20, rate=48000):
        return audioout._AudioQueueOut(rate, 2, buffers=buffers,
                                       block_ms=block_ms)

    @staticmethod
    def pcm_for(out):
        return struct.pack('<hh', 1000, -1000) * (out.block // 4)

    def test_healthy_queue_cycles_every_buffer_and_says_so(self):
        out = self.queue()
        try:
            pcm = self.pcm_for(out)
            for _ in range(out.buffers):
                out.write(pcm)
            deadline = time.monotonic() + 5.0
            while out.queued() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(out.cb, out.buffers)
            self.assertEqual(out.enqueued, out.buffers)
            self.assertEqual(out.start_err, 0)
            self.assertEqual(out.enqueue_err, 0)
            self.assertEqual(out.cb_recycled_twice, 0)
            self.assertEqual(out.cb_unknown, 0)
            self.assertEqual(out.queued(), 0)
            self.assertEqual(len(out._free_indices), out.buffers)
            # The device sample rate is a Float64; read as an integer it is
            # a number no sample rate ever was.
            self.assertEqual(out._props_seen['hwrate'], '48000 Hz')
            self.assertEqual(out._props_seen['running'], 1)
            self.assertIn('block 21 ms', out.diagnostics())
            self.assertIn('start 8/0 err', out.diagnostics())
        finally:
            out.close()

    def test_buffer_in_queue_does_not_double_recycle_a_buffer(self):
        """kAudioQueueErr_BufferInQueue means the queue still owns it.

        The free list used to outgrow the buffers (16 entries for 8) and
        queued() went to -8, and from there every later enqueue of that
        buffer failed the same way, so the corruption only grew.
        """
        self.stub_fails(fail_enqueue=-66679,     # kAudioQueueErr_BufferInQueue
                        enqueue_took=1)
        out = self.queue()
        try:
            pcm = self.pcm_for(out)
            for _ in range(out.buffers):
                out.write(pcm)
            time.sleep(0.3)
            self.assertEqual(out.enqueue_err, out.buffers)
            self.assertLessEqual(len(out._free_indices), out.buffers)
            self.assertGreaterEqual(out.queued(), 0)
            self.assertEqual(out.last_status, -66679)
            self.assertIn('kAudioQueueErr_BufferInQueue', out.diagnostics())
        finally:
            out.close()

    def test_a_rejected_buffer_is_put_back_in_service(self):
        """The other case: the queue took nothing, so it is free again."""
        self.stub_fails(fail_enqueue=-66687)      # kAudioQueueErr_InvalidBuffer
        out = self.queue()
        try:
            pcm = self.pcm_for(out)
            for _ in range(out.buffers):
                out.write(pcm)
            time.sleep(0.2)
            self.assertEqual(out.enqueue_err, out.buffers)
            self.assertEqual(len(out._free_indices), out.buffers)
            self.assertEqual(out.queued(), 0)
            self.assertEqual(out.last_status, -66687)
        finally:
            out.close()

    def test_a_failed_start_is_recorded_rather_than_swallowed(self):
        """played used to climb while AudioQueueStart failed every time."""
        self.stub_fails(fail_start_after=2)           # fails from the 3rd call
        out = self.queue()
        try:
            pcm = self.pcm_for(out)
            for _ in range(6):
                out.write(pcm)
            time.sleep(0.3)
            self.assertEqual(out.start_ok, 2)
            self.assertEqual(out.start_err, 4)
            self.assertEqual(out.last_status, -66681)
            self.assertIn('kAudioQueueErr_CannotStart', out.diagnostics())
            self.assertIn('start 2/4 err', out.diagnostics())
            # The failure is the point of the log, so it must be in it.
            self.assertIn('start', '\n'.join(out.event_log()))
        finally:
            out.close()

    def test_a_blocking_write_gives_up_instead_of_wedging_the_caller(self):
        """The emulator's own thread writes here: it must not hang."""
        self.stub_fails(stall=1)                 # nothing ever comes back
        out = self.queue()
        try:
            pcm = self.pcm_for(out)
            finished = []

            def feed():
                for _ in range(out.buffers + 3):
                    out.write(pcm, block=True, timeout=0.2)
                finished.append(True)

            thread = threading.Thread(target=feed, daemon=True)
            thread.start()
            thread.join(15.0)
            self.assertFalse(thread.is_alive(),
                             'a blocking write still wedges its thread')
            self.assertEqual(finished, [True])
            self.assertGreater(out.wait_to, 0)
            self.assertIn('wait_to', '\n'.join(out.event_log()))
            self.assertIn('blocking write gave up', self.log.getvalue())
        finally:
            out.close()

    def test_a_stalled_device_does_not_take_the_emulator_thread_with_it(self):
        """The panel's own policy: a bounded wait on Darwin."""
        self.stub_fails(stall=1)
        out = self.queue()
        try:
            pcm = self.pcm_for(out)
            started = time.monotonic()
            for _ in range(out.buffers + 2):
                out.write(pcm, block=True, timeout=0.2)
            elapsed = time.monotonic() - started
            # Two blocks past the buffers, so two bounded waits at most.
            self.assertLess(elapsed, 5.0)
        finally:
            out.close()

    def test_diagnostics_survives_a_closed_queue(self):
        """close() logs, so diagnostics() must work after the queue is gone."""
        out = self.queue()
        out.write(self.pcm_for(out))
        out.close()
        line = out.diagnostics()
        self.assertIn('audioqueue:', line)
        self.assertIn('closed', self.log.getvalue())

    def test_a_device_change_is_reported_by_the_poll(self):
        """An output device that moves out from under the queue goes silent."""
        out = self.queue()
        try:
            before = out._props_seen['dev']
            self.assertFalse(out.poll())        # nothing has changed yet
            self.assertEqual(out._props_seen['dev'], before)
            # Force the value the poll compares against to differ.
            out._props_seen['dev'] = 'gone'
            self.assertTrue(out.poll())
            self.assertEqual(out._props_seen['dev'], before)
            self.assertIn('dev', self.log.getvalue())
        finally:
            out.close()

    def test_osstatus_names_are_decoded(self):
        cases = {
            0: 'noErr',
            -66681: 'kAudioQueueErr_CannotStart (-66681)',
            -66679: 'kAudioQueueErr_BufferInQueue (-66679)',
            -66632: 'kAudioQueueErr_EnqueueDuringReset (-66632)',
            -66671: 'kAudioQueueErr_QueueInvalidated (-66671)',
            -1: '-1',
        }
        for value, want in cases.items():
            with self.subTest(value=value):
                self.assertEqual(audioout.osstatus_text(value), want)
        # A four-character Core Audio code, as an OSStatus.
        text = audioout.osstatus_text(0x21706c61)
        self.assertIn("'!pla'", text)


if __name__ == '__main__':
    unittest.main()


@NEEDS_STUB
class LiveRunSurvivesADeadDeviceTest(unittest.TestCase):
    """The reported symptom, end to end through the panel's own audio loop.

    Live output goes silent while the sequencer keeps moving and the
    recording keeps growing. Driven through Emulator._update_audio, the real
    _live_write and the real _AudioQueueOut, with the device failing.
    """

    def setUp(self):
        try:
            from emu import gui
        except Exception as exc:                              # noqa: BLE001
            self.skipTest('emu.gui needs tkinter: %s' % exc)
        self.gui = gui
        self._saved = dict(os.environ)
        os.environ['DIGIEMU_AUDIOTOOLBOX'] = STUB
        for key in list(os.environ):
            if key.startswith('AQSTUB_'):
                del os.environ[key]
        self.log = io.StringIO()
        redirect = contextlib.redirect_stdout(self.log)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def run_audio(self, chunks, frames_per_chunk=2048, wait_s=0.05):
        """Feed the panel's audio loop, on its own thread, as in a run."""
        gui = self.gui
        emu = gui.Emulator('gui.snap')
        emu.audio_on = True
        emu.audio_live = True
        emu.audio_muted = False
        emu.audio_cfg = {'rate': 48000, 'sample_bits': 24}
        emu.set_volume(1.0)
        # The shipped bound is 0.5 s; a shorter one keeps this test quick
        # while still exercising the bounded-wait path it exists to pin.
        emu.LIVE_WRITE_WAIT_S = wait_s
        frame = struct.pack('>II', 0x00123400, 0x00567800)
        seen = []
        errors = []

        def loop():
            try:
                for _ in range(chunks):
                    emu._audio_raw += frame * frames_per_chunk
                    emu._update_audio()
                    seen.append(emu.audio_seconds())
            except Exception as exc:                          # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        thread.join(60.0)
        return emu, thread, seen, errors

    def test_a_device_that_stops_returning_buffers_does_not_freeze_the_run(self):
        os.environ['AQSTUB_STALL'] = '1'
        gui = self.gui
        with mock.patch.object(gui.sys, 'platform', 'darwin'), \
                mock.patch.object(gui.audioout, 'WaveOut',
                                  lambda rate, ch, **kw:
                                  audioout._AudioQueueOut(rate, ch,
                                                          buffers=4)):
            emu, thread, seen, errors = self.run_audio(24)
        self.assertEqual(errors, [])
        self.assertFalse(thread.is_alive(),
                         'the audio loop wedged: the run would have frozen')
        # The recording carried on regardless, which is what was reported.
        self.assertEqual(seen, sorted(seen))
        self.assertGreater(seen[-1], 0.0)
        out = emu._live_out
        self.assertGreater(out.wait_to, 0)
        self.assertIn('blocking write gave up', self.log.getvalue())

    def test_a_failing_start_is_named_in_the_panel_log(self):
        """played kept climbing before, with nothing in the log to go on."""
        os.environ['AQSTUB_FAIL_START_AFTER'] = '1'
        gui = self.gui
        with mock.patch.object(gui.sys, 'platform', 'darwin'), \
                mock.patch.object(gui.audioout, 'WaveOut',
                                  lambda rate, ch, **kw:
                                  audioout._AudioQueueOut(rate, ch,
                                                          buffers=4)):
            emu, thread, seen, errors = self.run_audio(12)
        self.assertEqual(errors, [])
        self.assertFalse(thread.is_alive())
        out = emu._live_out
        self.assertGreater(out.start_err, 0)
        logged = self.log.getvalue()
        self.assertIn('kAudioQueueErr_CannotStart', logged)
        self.assertIn('start 1/', logged)
