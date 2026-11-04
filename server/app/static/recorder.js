// Recorder: mic -> MediaRecorder chunks -> ordered, retried uploads -> finalize.
//
// Every chunk stays in memory until the server has acknowledged it, and
// chunks are sent strictly in order. A network drop just pauses the upload
// loop; recording continues, and the queue drains when the network is back.
// The server ignores a chunk it already has (same bytes), so retries are safe.
(() => {
  const app = document.getElementById("recorder-app");
  if (!app) return;
  const { t, errText, api: call } = DS(app);
  const CHUNK_MS = Number(app.dataset.chunkSeconds || 10) * 1000;
  const MAX_S = Number(app.dataset.maxSeconds || 14400);
  const $ = (id) => document.getElementById(id);

  const ui = {
    consent: $("consent"), allow: $("mic-allow"), select: $("mic-select"), dsp: $("dsp"),
    bar: $("meter-bar"), record: $("btn-record"), pause: $("btn-pause"), stop: $("btn-stop"),
    timer: $("timer"), status: $("rec-status"), hint: $("rec-hint"),
    done: $("rec-done"), doneLink: $("rec-done-link"),
  };

  // Exposed for the browser tests (dev/recorder-test); harmless in production.
  const state = (window.__recorder = {
    phase: "idle", // idle | recording | paused | finishing | done | error
    recId: null, nextSeq: 0, acked: 0, queue: [], online: navigator.onLine,
    retries: 0, error: null, mime: null,
  });

  let stream = null, recorder = null, meterCtx = null, wakeLock = null;
  let elapsed = 0, tick = null, pumping = false, backoff = 0;

  const api = (method, url, body) => call(method, url, body);

  const fmt = (s) => {
    s = Math.floor(s);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    const mm = String(m).padStart(2, "0"), ss = String(sec).padStart(2, "0");
    return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
  };

  const showStatus = () => {
    const pending = state.queue.length;
    let msg = "";
    if (state.phase === "recording" || state.phase === "paused" || state.phase === "finishing") {
      msg = t("rec.status_chunks", { acked: state.acked, total: state.nextSeq });
      if (!state.online) msg += " · " + t("rec.status_offline");
      else if (pending > 1) msg += " · " + t("rec.status_catching_up", { n: pending });
      if (state.phase === "finishing") msg = t("rec.status_finishing") + " · " + msg;
    } else if (state.phase === "done") msg = t("rec.status_done");
    else if (state.phase === "error") msg = t("rec.status_error", { error: state.error || "" });
    ui.status.textContent = msg;
  };

  // --- microphone ----------------------------------------------------------------

  const constraints = () => {
    const dsp = ui.dsp.checked; // off by default: devices like the XVF3800 do their own DSP
    const audio = {
      echoCancellation: dsp, noiseSuppression: dsp, autoGainControl: dsp,
      channelCount: { ideal: 2 }, // keep both channels of multi-channel mics (D22)
    };
    // Only once the list holds real devices (after permission); before that,
    // let the browser pick its default microphone.
    if (!ui.select.disabled && ui.select.value) audio.deviceId = { exact: ui.select.value };
    return { audio, video: false };
  };

  const listDevices = async () => {
    const devices = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "audioinput");
    const current = ui.select.value;
    ui.select.replaceChildren(
      ...devices.map((d, i) => {
        const o = document.createElement("option");
        o.value = d.deviceId;
        o.textContent = d.label || t("rec.mic_n", { n: i + 1 });
        return o;
      }),
    );
    if (current && devices.some((d) => d.deviceId === current)) ui.select.value = current;
    ui.select.disabled = devices.length === 0;
  };

  const openMic = async () => {
    if (stream) stream.getTracks().forEach((tr) => tr.stop());
    stream = await navigator.mediaDevices.getUserMedia(constraints());
    await listDevices(); // labels are only visible after permission
    startMeter();
    ui.record.disabled = false;
    ui.allow.textContent = t("rec.mic_ready");
    setButtons();
  };

  const startMeter = () => {
    if (meterCtx) meterCtx.close();
    meterCtx = new AudioContext();
    const src = meterCtx.createMediaStreamSource(stream);
    const an = meterCtx.createAnalyser();
    an.fftSize = 1024;
    src.connect(an);
    const buf = new Float32Array(an.fftSize);
    const draw = () => {
      if (!meterCtx || meterCtx.state === "closed") return;
      an.getFloatTimeDomainData(buf);
      let sum = 0;
      for (const v of buf) sum += v * v;
      const level = Math.min(1, Math.sqrt(sum / buf.length) * 4);
      ui.bar.style.width = `${Math.round(level * 100)}%`;
      requestAnimationFrame(draw);
    };
    draw();
  };

  const pickMime = () => {
    const options = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4", "audio/webm"];
    return options.find((m) => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || "";
  };

  // --- upload queue ------------------------------------------------------------------

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const waitForRetry = (ms) =>
    new Promise((resolve) => {
      const done = () => { window.removeEventListener("online", done); clearTimeout(timer); resolve(); };
      const timer = setTimeout(done, ms);
      window.addEventListener("online", done, { once: true });
    });

  const pump = async () => {
    if (pumping) return;
    pumping = true;
    try {
      while (state.queue.length) {
        const item = state.queue[0];
        let res;
        try {
          res = await api("PUT", `/api/recordings/${state.recId}/chunks/${item.seq}`, item.blob);
        } catch {
          res = { ok: false, status: 0 };
        } // status 0 = network error: offline, reset, timeout
        if (res.ok) {
          state.queue.shift();
          state.acked += 1;
          backoff = 0;
          showStatus();
          continue;
        }
        if (res.status === 401) { fail(t("rec.err_session")); return; }
        if ([404, 409, 413, 415].includes(res.status)) {
          fail(errText(res));
          return;
        }
        // 0 / 5xx / anything else: wait and retry the same chunk.
        state.retries += 1;
        backoff = Math.min(backoff ? backoff * 2 : 1000, 15000);
        showStatus();
        await waitForRetry(backoff);
      }
    } finally {
      pumping = false;
    }
    if (state.phase === "finishing" && !state.queue.length) await finalize();
  };

  const finalize = async () => {
    for (let attempt = 0; attempt < 30; attempt += 1) {
      let res;
      try {
        res = await api("POST", `/api/recordings/${state.recId}/finalize`, { last_seq: state.nextSeq - 1 });
      } catch {
        res = { ok: false, status: 0 };
      }
      if (res.ok) {
        state.phase = "done";
        showStatus();
        setButtons();
        ui.doneLink.href = `/recordings/${state.recId}`;
        ui.done.hidden = false;
        return;
      }
      if (res.status === 409 && res.data && res.data.missing) {
        fail(t("rec.err_missing", { n: res.data.missing.length }));
        return;
      }
      if (res.status && res.status < 500) { fail(errText(res)); return; }
      await waitForRetry(Math.min(1000 * 2 ** attempt, 15000));
    }
    fail(t("rec.err_finalize"));
  };

  const fail = (msg) => {
    state.phase = "error";
    state.error = msg;
    stopRecorder();
    showStatus();
    setButtons();
  };

  // --- recording ----------------------------------------------------------------------

  const setButtons = () => {
    const p = state.phase;
    ui.record.disabled = !stream || p === "recording" || p === "paused" || p === "finishing";
    ui.pause.disabled = !(p === "recording" || p === "paused");
    ui.stop.disabled = !(p === "recording" || p === "paused");
    ui.pause.textContent = p === "paused" ? t("rec.resume") : t("rec.pause");
    const live = p === "recording" || p === "paused";
    ui.record.classList.toggle("on", p === "recording");
    ui.record.setAttribute("aria-label", live ? t("rec.stop") : t("rec.record"));
    if (live) ui.record.disabled = false; // the big button stops a running recording
    ui.hint.textContent = !stream ? t("rec.hint_allow") : p === "recording" ? t("rec.hint_recording")
      : p === "paused" ? t("rec.hint_paused") : p === "finishing" ? t("rec.status_finishing") : t("rec.hint_ready");
    ui.select.disabled = p === "recording" || p === "paused" || p === "finishing";
    ui.dsp.disabled = ui.select.disabled;
  };

  const lockScreen = async () => {
    try { wakeLock = await navigator.wakeLock?.request("screen"); } catch { wakeLock = null; }
  };
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && (state.phase === "recording" || state.phase === "paused")) lockScreen();
  });

  const start = async () => {
    if (!ui.consent.checked) {
      ui.status.textContent = t("rec.err_consent");
      ui.consent.focus();
      return;
    }
    const mime = pickMime();
    if (!mime) { fail(t("rec.err_unsupported")); return; }
    const when = new Intl.DateTimeFormat(app.dataset.locale || undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date());
    const res = await api("POST", "/api/recordings", {
      mime_type: mime, consent: true, title: t("rec.default_title", { when }),
    });
    if (!res.ok) { fail(res.status ? errText(res) : t("rec.err_start")); return; }
    ui.done.hidden = true;
    ui.timer.textContent = fmt(0);
    Object.assign(state, {
      phase: "recording", recId: res.data.id, nextSeq: 0, acked: 0, queue: [], error: null, retries: 0, mime,
    });
    recorder = new MediaRecorder(stream, { mimeType: mime, audioBitsPerSecond: 64000 });
    recorder.ondataavailable = (ev) => {
      if (!ev.data || !ev.data.size) return;
      state.queue.push({ seq: state.nextSeq, blob: ev.data });
      state.nextSeq += 1;
      showStatus();
      pump();
    };
    recorder.onstop = () => {
      state.phase = "finishing";
      showStatus();
      setButtons();
      pump();
    };
    recorder.start(CHUNK_MS);
    elapsed = 0;
    tick = setInterval(() => {
      if (state.phase !== "recording") return;
      elapsed += 1;
      ui.timer.textContent = fmt(elapsed);
      if (elapsed >= MAX_S) stop();
    }, 1000);
    lockScreen();
    setButtons();
    showStatus();
  };

  const stopRecorder = () => {
    clearInterval(tick);
    if (recorder && recorder.state !== "inactive") recorder.stop();
    if (wakeLock) wakeLock.release().catch(() => {});
    wakeLock = null;
  };

  const stop = () => {
    if (!(state.phase === "recording" || state.phase === "paused")) return;
    stopRecorder(); // onstop -> finishing -> drain queue -> finalize
  };

  const togglePause = () => {
    if (state.phase === "recording") { recorder.pause(); state.phase = "paused"; }
    else if (state.phase === "paused") { recorder.resume(); state.phase = "recording"; }
    setButtons();
    showStatus();
  };

  // --- wiring ----------------------------------------------------------------------------------

  ui.allow.addEventListener("click", () =>
    openMic().catch((e) => { ui.status.textContent = t("rec.err_mic", { error: e.message || e.name }); }));
  ui.select.addEventListener("change", () => openMic().catch(() => {}));
  ui.dsp.addEventListener("change", () => stream && openMic().catch(() => {}));
  ui.record.addEventListener("click", () => (state.phase === "recording" || state.phase === "paused" ? stop() : start()));
  ui.pause.addEventListener("click", togglePause);
  ui.stop.addEventListener("click", stop);
  window.addEventListener("online", () => { state.online = true; showStatus(); pump(); });
  window.addEventListener("offline", () => { state.online = false; showStatus(); });
  window.addEventListener("beforeunload", (ev) => {
    if (["recording", "paused", "finishing"].includes(state.phase) || state.queue.length) {
      ev.preventDefault();
      ev.returnValue = "";
    }
  });
  navigator.mediaDevices?.addEventListener?.("devicechange", () => listDevices().catch(() => {}));

  setButtons();
})();
