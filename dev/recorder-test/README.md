# Recorder test (fake microphone + tone markers)

**Test harness only.** Records through the real page in Chromium, with a fake
microphone that plays a known clip, then checks that the server stored the
same audio ("more or less": Opus is lossy, so bytes differ but content
doesn't).

- `markers.py make clip.wav`: 8 s stereo clip. Left channel: second k plays
  600 + 150·k Hz. Right channel: steady 300 Hz.
- `markers.py check file`: decodes the recording and reads the tones back. It
  passes when every tone lasts ~1 s and they follow in order (looping). A
  lost, duplicated or reordered piece shows up as a short tone or a jump.
  `--pauses N` allows N jumps, for pause/resume. Checked against a copy with
  one second cut out: correctly fails.
- `e2e_recorder.cjs`: the browser scenarios (20 checks):

| Scenario | Checks |
|---|---|
| normal | all chunks acked, finalized, audio intact, server duration matches, 2 channels kept |
| network drop (4 s offline mid-recording) | chunks queue, drain on reconnect, audio intact |
| flaky network (dropped request, lost response after the server stored it, a 500) | retried, chunk stored once, audio intact |
| pause / resume | exactly one jump at the pause, paused time not recorded |
| tab closed mid-recording | kept as "interrupted", the part that arrived is intact |
| upload an existing mp3 through the page | audio intact |
| Safari-style fragmented MP4/AAC chunks (via the API) | reassembled, audio intact |
| another user | can't list or download it |

Run in CI on every push with local accounts. Run in the sandbox logged in as
Samba AD users (`asmith`, other user `jdoe`; see `dev/ad-test`). The app needs
`RECORDER_CHUNK_SECONDS=1` and `RECORDING_STALE_SECONDS=5`.

Not covered: real Safari/iOS (screen lock, Wake Lock), real Android power
saving, a real XVF3800, Firefox's recorder (Playwright's Firefox can only fake
a tone, not play a file).
