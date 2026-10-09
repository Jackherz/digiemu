# macOS audio smoke test

A pull request touching `packaging/` runs the Release workflow and uploads
`digiemu-macos-arm64-<version>` (the DMG, checksums and build info). Download
that artifact from the PR's **macos-build** check, install the app, and use
your own supported firmware and samples. No firmware ships in the artifact.

The frozen self-test checks the bundle but does not verify audible live
output. On a Mac with a working default audio device:

1. Open Digitakt or Digitone, enable live audio, and play a non-silent pattern.
   Confirm sound comes from the default speakers/headphones, not just the
   Audio section's **PLAY** button.
2. Stop and restart the pattern; mute and unmute live output. Check that sound
   resumes. If an underrun occurs, verify that live output recovers.
3. Record a short passage and use **PLAY** to confirm recording/playback still
   works independently of the live stream.
4. Close the panel while playing and confirm the app closes and saves normally.

## What is actually known, and what to report back

PR #1 added AudioQueue's minimum block size and restart-on-enqueue. PR #2
raised the Darwin prebuffer to 200 ms and made Darwin live writes blocking,
on the theory that the queue was being starved. **That theory is disproven by
observation**: on the PR #2 artifact sound plays briefly at start-up and then
stops for good, while the sequencer keeps moving, the recording's duration
keeps growing, and playing that recording back is fine. The samples are
rendered and the recording path is healthy, so only the path to the device
stops -- and a bigger cushion cannot be what was missing. Mute and unmute
does not bring it back either, which a buffering problem would.

The AudioQueue backend now reports its own lifecycle instead of swallowing
it. Every OSStatus from `AudioQueueNewOutput`, `AllocateBuffer`,
`EnqueueBuffer`, `Start`, `Stop` and `Dispose` is decoded to its name and
counted; the buffer-completion callbacks are counted with their timing; and
`kAudioQueueProperty_IsRunning`, `CurrentDevice`, `DeviceSampleRate` and
`MaxOutputFrameCount` are read on open, on change and on a poll every 10 s.
All of it goes to the log the panel already writes.

### How to capture a diagnosis

1. Build the artifact as above, add your firmware, and start a session with
   live audio enabled.
2. Let it play until the sound stops, then keep it running for another
   minute so at least a few poll lines are written.
3. Quit the panel and send back
   `~/Library/Application Support/digiemu/<firmware>/logs/panel.log`,
   together with the Mac model, macOS version, the output device in use at
   the time, and the artifact's build commit.

The lines to look for are `[audio] audioqueue:` (opened, and every 10 s),
`[audio] live:` (the periodic poll) and `[audio] audioqueue: ... (closed …)`
with the event log under it. Read them like this:

| In the log | It means |
| --- | --- |
| `start N/M err` with M > 0 | `AudioQueueStart` stopped working; `last status` names the error. The queue is silent while looking busy. |
| `running 0` while `cb` still climbs | the queue stopped itself rather than running out of samples. |
| `cb` frozen while `enq` climbs | the device stopped taking buffers. |
| `dev` changing | the default output moved out from under the queue (display, Bluetooth, aggregate device). |
| `hwrate` not 48000 Hz | the device is at another rate. |
| `wait-timeouts` > 0 | the queue ran dry of free buffers and a blocking write gave up. |
| `double-recycle` or `unknown-cb` > 0 | buffer bookkeeping lost track of a buffer. |

### Two real defects fixed on the way, both reproduced here

* A `AudioQueueEnqueueBuffer` failure put the buffer back on the free list
  whatever the status, including `kAudioQueueErr_BufferInQueue`, where the
  queue still owns it. The free list then outgrew the buffers (16 entries for
  8) and `queued()` went negative. Only that status is treated as "still
  owned" now.
* PR #2's blocking Darwin write had no bound and no abort, and it runs on the
  emulator's own thread: a device that stopped handing buffers back froze the
  run, and the recording and panel with it. The live path now waits at most
  `Emulator.LIVE_WRITE_WAIT_S` per block and logs each time it gives up.

`tests/test_audioout_audioqueue.py` drives the backend against a stand-in
AudioToolbox (`tests/fixtures/aqstub/aqstub.c`) that can stall, fail
`AudioQueueStart` and fail `AudioQueueEnqueueBuffer`, so all three are
reproduced and pinned without a Mac.

