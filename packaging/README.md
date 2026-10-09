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

PR #1 added AudioQueue's minimum block size and restart-on-enqueue fix, but
live output was still reported silent. The follow-up in `emu/gui.py` uses a
200 ms Darwin prebuffer (also after underruns) and blocking Darwin writes so
bursts do not drop audio when the host queue is full. Windows and Linux retain
their 80 ms prebuffer and non-blocking live writes. Allow for the prebuffer
when checking startup latency. Record the Mac model, macOS version, output
device and the artifact's build commit alongside the listening results.
