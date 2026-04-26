// Claude Voice — PWA client.
// Press-to-talk: hold the mic button to record, release to send. The recording
// is streamed over WebSocket; the server transcribes with Whisper, runs a
// Claude turn, and streams back text and TTS audio chunks.
(() => {
  // ---------------------------------------------------------------------------
  // Constants
  // ---------------------------------------------------------------------------
  const MODE_KEY = 'claude-voice-mode';
  const TOKEN_KEY = 'claude-voice-token';

  const MIN_RECORD_MS = 500;     // below this on release: treat as accidental tap and drop
  const CANCEL_SWIPE_PX = 80;    // drag up > this many px: cancel recording

  const MIME_CANDIDATES = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/mp4',
    'audio/ogg;codecs=opus',
  ];

  // ---------------------------------------------------------------------------
  // DOM references
  // ---------------------------------------------------------------------------
  const statusEl = document.getElementById('status');
  const logEl = document.getElementById('log');
  const pttEl = document.getElementById('ptt');
  const pttLabelEl = pttEl.querySelector('.ptt-label');
  const sessionEl = document.getElementById('session');
  const newSessionBtn = document.getElementById('new-session');
  const stopAudioBtn = document.getElementById('stop-audio');
  const speakerToggleEl = document.getElementById('speaker-toggle');
  const textFormEl = document.getElementById('text-form');
  const textInputEl = document.getElementById('text-input');
  const sendTextBtn = document.getElementById('send-text');
  const loginModalEl = document.getElementById('login-modal');
  const loginFormEl = document.getElementById('login-form');
  const loginPasswordEl = document.getElementById('login-password');
  const loginErrorEl = document.getElementById('login-error');
  const loginSubmitEl = document.getElementById('login-submit');

  // ---------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------
  let mode = localStorage.getItem(MODE_KEY) === 'text' ? 'text' : 'voice';
  let ws = null;
  let authToken = localStorage.getItem(TOKEN_KEY) || '';
  let authRequired = false;  // set from /auth/status on boot
  let refreshTimer = null;

  // Recording state
  let mediaRecorder = null;
  let mediaStream = null;
  let recordedChunks = [];
  let recordingStarted = false;
  let recordingStartMs = 0;
  let cancelRequested = false;

  // Gesture state
  let activePointerId = null;
  let pressStartY = 0;

  // TTS playback state
  let audioQueue = [];
  let isPlayingAudio = false;
  let ttsCurrentChunks = [];
  let audioUnlocked = false;
  // True between server `done` and the last queued audio finishing, so the
  // UI transitions to Ready only after the user actually stops hearing Claude.
  let doneWaitingForAudio = false;

  // Chat render state
  let assistantLiveEl = null;

  // ---------------------------------------------------------------------------
  // Utilities
  // ---------------------------------------------------------------------------
  function setStatus(text, cls) {
    statusEl.textContent = text;
    statusEl.className = 'status' + (cls ? ' ' + cls : '');
  }

  function vibrate(ms) {
    if (navigator.vibrate) {
      try { navigator.vibrate(ms); } catch (_) {}
    }
  }

  function addMsg(role, text, classes = '') {
    const div = document.createElement('div');
    div.className = `msg ${role} ${classes}`.trim();
    div.textContent = text;
    logEl.appendChild(div);
    logEl.scrollTop = logEl.scrollHeight;
    return div;
  }

  function appendAssistantDelta(text) {
    if (!assistantLiveEl) assistantLiveEl = addMsg('assistant', '');
    assistantLiveEl.textContent += text;
    logEl.scrollTop = logEl.scrollHeight;
  }

  function finalizeAssistant() { assistantLiveEl = null; }

  function pickMime() {
    for (const m of MIME_CANDIDATES) {
      if (MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(m)) return m;
    }
    return '';
  }

  function makeTurnId() {
    if (crypto.randomUUID) return crypto.randomUUID();
    return Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
  }

  // ---------------------------------------------------------------------------
  // Speaker toggle (mode = 'voice' means TTS on, 'text' means TTS off)
  // ---------------------------------------------------------------------------
  function applyModeUI() {
    const on = mode === 'voice';
    speakerToggleEl.classList.toggle('on', on);
    speakerToggleEl.classList.toggle('off', !on);
    speakerToggleEl.setAttribute('aria-pressed', on ? 'true' : 'false');
    speakerToggleEl.title = on ? 'Voice output on' : 'Voice output off';
  }

  function setMode(next) {
    if (next !== 'voice' && next !== 'text') return;
    if (mode === next) return;
    mode = next;
    localStorage.setItem(MODE_KEY, mode);
    applyModeUI();
    if (mode === 'text') stopAllAudio();
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({ type: 'set_mode', mode }));
    }
  }

  speakerToggleEl.addEventListener('click', () => {
    setMode(mode === 'voice' ? 'text' : 'voice');
  });
  applyModeUI();

  // ---------------------------------------------------------------------------
  // Audio playback (TTS)
  // ---------------------------------------------------------------------------
  // iOS Safari only lets us play audio on an element whose first play() was
  // triggered inside a user gesture. A brand-new `Audio()` created later in a
  // WebSocket callback will silently fail with NotAllowedError. Workaround:
  // use ONE persistent element, unlock it on the first tap, and just swap its
  // `src` for each sentence.
  const ttsAudio = new Audio();
  ttsAudio.preload = 'auto';
  let currentUrl = null;

  function revokeCurrentUrl() {
    if (currentUrl) {
      try { URL.revokeObjectURL(currentUrl); } catch (_) {}
      currentUrl = null;
    }
  }

  function stopAllAudio() {
    audioQueue.length = 0;
    try { ttsAudio.pause(); } catch (_) {}
    try { ttsAudio.removeAttribute('src'); ttsAudio.load(); } catch (_) {}
    revokeCurrentUrl();
    isPlayingAudio = false;
    doneWaitingForAudio = false;
    updateStopAudioVisibility();
    // If we were showing Speaking/Thinking because of TTS, release the UI.
    if (/Speaking|Thinking/i.test(statusEl.textContent)) {
      setStatus('Ready · hold to talk');
    }
  }

  function updateStopAudioVisibility() {
    const shouldShow = isPlayingAudio || audioQueue.length > 0 || ttsCurrentChunks.length > 0;
    stopAudioBtn.classList.toggle('hidden', !shouldShow);
  }

  const SILENT_WAV = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAESsAACJWAAACABAAZGF0YQAAAAA=';
  function unlockAudio() {
    if (audioUnlocked) return;
    try {
      ttsAudio.src = SILENT_WAV;
      const p = ttsAudio.play();
      if (p && typeof p.then === 'function') {
        p.then(() => {
          audioUnlocked = true;
          try { ttsAudio.pause(); } catch (_) {}
        }).catch(err => {
          console.warn('audio unlock failed', err);
        });
      } else {
        audioUnlocked = true;
      }
    } catch (err) {
      console.warn('audio unlock threw', err);
    }
  }

  ttsAudio.addEventListener('ended', () => {
    revokeCurrentUrl();
    playNext();
  });
  ttsAudio.addEventListener('error', (e) => {
    console.warn('tts audio error', ttsAudio.error);
    revokeCurrentUrl();
    playNext();
  });

  function enqueueAudio(blob) {
    audioQueue.push(blob);
    updateStopAudioVisibility();
    if (!isPlayingAudio) playNext();
  }

  function playNext() {
    if (audioQueue.length === 0) {
      isPlayingAudio = false;
      updateStopAudioVisibility();
      // Queue drained. If the turn already finished server-side, flip to
      // Ready now (not when `done` arrived — audio is what the user hears).
      // Otherwise Claude is still generating; show Thinking, not Speaking.
      if (doneWaitingForAudio) {
        doneWaitingForAudio = false;
        setStatus('Ready · hold to talk');
      } else if (/Speaking/i.test(statusEl.textContent)) {
        setStatus('Thinking…');
      }
      return;
    }
    isPlayingAudio = true;
    updateStopAudioVisibility();
    setStatus('Speaking…');

    const blob = audioQueue.shift();
    revokeCurrentUrl();
    currentUrl = URL.createObjectURL(blob);
    ttsAudio.src = currentUrl;
    const p = ttsAudio.play();
    if (p && typeof p.then === 'function') {
      p.catch(err => {
        console.warn('tts audio play() rejected', err);
        revokeCurrentUrl();
        isPlayingAudio = false;
        playNext();
      });
    }
  }

  // ---------------------------------------------------------------------------
  // WebSocket
  // ---------------------------------------------------------------------------
  function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const qs = authToken ? `?token=${encodeURIComponent(authToken)}` : '';
    const url = `${proto}://${location.host}/ws${qs}`;
    setStatus('Connecting…');

    ws = new WebSocket(url);
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => {
      setStatus('Connected');
      ws.send(JSON.stringify({ type: 'hello', mode }));
    };
    ws.onclose = (ev) => {
      // 4401 = our application-level "bad token". Don't retry in a loop —
      // prompt the user for the password and reconnect after they unlock.
      if (ev && ev.code === 4401) {
        authToken = '';
        try { localStorage.removeItem(TOKEN_KEY); } catch (_) {}
        setStatus('Locked — enter password');
        pttEl.disabled = true;
        showLoginModal('Session expired. Please sign in again.');
        return;
      }
      setStatus('Disconnected — retrying in 3s…');
      pttEl.disabled = true;
      setTimeout(connectWS, 3000);
    };
    ws.onerror = (e) => { console.error('ws error', e); };
    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        let data;
        try { data = JSON.parse(ev.data); } catch (_) { return; }
        handleServerMsg(data);
      } else {
        ttsCurrentChunks.push(ev.data);
      }
    };
  }

  function handleServerMsg(data) {
    switch (data.type) {
      case 'ready':
        sessionEl.textContent = data.session_id.slice(0, 8);
        pttEl.disabled = false;
        setStatus('Ready · hold to talk');
        break;
      case 'transcribing':
        setStatus('Transcribing…');
        break;
      case 'transcript':
        addMsg('user', data.text);
        break;
      case 'thinking':
        setStatus('Thinking…');
        break;
      case 'tool_use':
        // Close any in-flight assistant bubble so post-tool text streams into
        // a new bubble *below* the tool marker (otherwise the final reply
        // renders above it and looks like nothing was said).
        finalizeAssistant();
        addMsg('assistant', `[tool: ${data.name}]`, 'tool');
        break;
      case 'text_delta':
        appendAssistantDelta(data.text);
        break;
      case 'tts_start':
        // Don't flip status here — playback is what the user hears. playNext
        // sets Speaking the moment it actually starts audio.
        ttsCurrentChunks = [];
        break;
      case 'tts_end':
        if (ttsCurrentChunks.length > 0) {
          enqueueAudio(new Blob(ttsCurrentChunks, { type: 'audio/mpeg' }));
        }
        ttsCurrentChunks = [];
        break;
      case 'tts_error':
        console.warn('tts error', data.message);
        ttsCurrentChunks = [];
        break;
      case 'done':
        finalizeAssistant();
        // If there's still audio queued or playing, wait for it to drain
        // before showing Ready — otherwise the UI says "ready" while Claude
        // is still audibly talking.
        if (isPlayingAudio || audioQueue.length > 0) {
          doneWaitingForAudio = true;
        } else {
          doneWaitingForAudio = false;
          setStatus('Ready · hold to talk');
        }
        break;
      case 'error':
        addMsg('assistant', `Error: ${data.message}`, 'error');
        finalizeAssistant();
        doneWaitingForAudio = false;
        setStatus('Ready · hold to talk');
        break;
      default:
        console.log('unknown msg', data);
    }
  }

  // ---------------------------------------------------------------------------
  // Recording
  // ---------------------------------------------------------------------------
  async function ensureMicStream() {
    if (mediaStream && mediaStream.getTracks().some(t => t.readyState === 'live')) {
      return mediaStream;
    }
    mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    return mediaStream;
  }

  // ---------------------------------------------------------------------------
  // Web Speech API: prefer in-browser SpeechRecognition (iOS Safari uses Apple
  // dictation, Android Chrome uses Google ASR, Desktop Chrome has its own).
  // Quality is comparable to whisper-medium for short voice turns and runs
  // entirely on-device — no GPU, no network upload of audio.
  //
  // Falls back to MediaRecorder + server-side STT only if SpeechRecognition
  // isn't available (some Firefox builds, very old Safari).
  // ---------------------------------------------------------------------------
  const SpeechRecognitionImpl = window.SpeechRecognition || window.webkitSpeechRecognition;
  const SR_AVAILABLE = !!SpeechRecognitionImpl;
  let speechRec = null;
  let speechFinalText = '';
  let speechInterimText = '';

  function detectSpeechLang() {
    // Best-effort guess from page/system. UI is fine in zh; prefer zh-CN.
    const browserLang = (navigator.language || 'zh-CN').toLowerCase();
    if (browserLang.startsWith('zh')) return 'zh-CN';
    if (browserLang.startsWith('en')) return 'en-US';
    return browserLang;
  }

  async function actuallyStartRecording_SR() {
    if (!ws || ws.readyState !== 1) {
      setStatus('Not connected yet');
      return false;
    }

    try {
      // SpeechRecognition triggers its own permission prompt on iOS, but we
      // also need mic stream warmed up for some browsers. Cheap to call.
      await ensureMicStream();
    } catch (err) {
      console.error(err);
      setStatus('Microphone permission denied');
      return false;
    }

    speechFinalText = '';
    speechInterimText = '';
    speechRec = new SpeechRecognitionImpl();
    speechRec.continuous = true;
    speechRec.interimResults = true;
    speechRec.lang = detectSpeechLang();

    speechRec.onresult = (event) => {
      let finalAdd = '';
      let interim = '';
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const r = event.results[i];
        if (r.isFinal) {
          finalAdd += r[0].transcript;
        } else {
          interim += r[0].transcript;
        }
      }
      if (finalAdd) speechFinalText += finalAdd;
      speechInterimText = interim;
      // Live preview in status line
      const preview = (speechFinalText + speechInterimText).trim().slice(-40);
      if (preview) setStatus('🎤 ' + preview, 'recording');
    };

    speechRec.onerror = (event) => {
      console.warn('SpeechRecognition error:', event.error);
      // 'no-speech' / 'aborted' are harmless for press-to-talk; show real ones.
      if (event.error && event.error !== 'aborted' && event.error !== 'no-speech') {
        setStatus('Voice error: ' + event.error);
      }
    };

    speechRec.onend = () => {
      // onend fires when stop() is called OR after auto-cutoff. We rely on
      // pointerup to handle send; this is just cleanup.
      speechRec = null;
    };

    try {
      speechRec.start();
    } catch (err) {
      console.error('SpeechRecognition.start failed:', err);
      setStatus('Could not start voice recognition');
      speechRec = null;
      return false;
    }

    recordingStarted = true;
    recordingStartMs = Date.now();
    pttEl.classList.add('recording');
    pttLabelEl.textContent = 'Release to send · swipe up to cancel';
    setStatus('🎤 Listening…', 'recording');
    vibrate(30);
    return true;
  }

  function finishRecording_SR() {
    const sr = speechRec;
    speechRec = null;
    recordingStarted = false;
    pttEl.classList.remove('recording', 'cancel-zone');
    pttLabelEl.textContent = 'Hold to talk';

    if (sr) {
      try { sr.stop(); } catch (_) {}
    }

    if (cancelRequested) {
      setStatus('Cancelled');
      return;
    }

    const duration = Date.now() - recordingStartMs;
    if (duration < MIN_RECORD_MS) {
      setStatus('Too short — hold longer');
      vibrate(40);
      return;
    }

    // Wait briefly for any trailing final result still in the pipeline.
    setTimeout(() => {
      const text = (speechFinalText + speechInterimText).trim();
      if (!text) {
        setStatus('No speech detected — try again');
        vibrate(40);
        return;
      }
      // Send as text turn — backend treats this exactly like typed input.
      const turnId = makeTurnId();
      ws.send(JSON.stringify({ type: 'text', content: text, turn_id: turnId }));
      setStatus('Sent: ' + text.slice(0, 40), 'processing');
    }, 250);
  }

  // ---------------------------------------------------------------------------
  // Legacy MediaRecorder path — only used as fallback when SpeechRecognition
  // isn't available in the browser (rare, mostly old Firefox).
  // ---------------------------------------------------------------------------
  async function actuallyStartRecording_Legacy() {
    if (!ws || ws.readyState !== 1) {
      setStatus('Not connected yet');
      return false;
    }

    try {
      await ensureMicStream();
    } catch (err) {
      console.error(err);
      setStatus('Microphone permission denied');
      return false;
    }

    recordedChunks = [];
    const mime = pickMime();
    mediaRecorder = new MediaRecorder(mediaStream, mime ? { mimeType: mime } : undefined);

    mediaRecorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) recordedChunks.push(e.data);
    };

    // Guard: some browsers fire `onstop` more than once per stop() call.
    let audioDispatched = false;
    mediaRecorder.onstop = async () => {
      if (audioDispatched) {
        console.warn('mediaRecorder.onstop fired twice — skipping duplicate send');
        return;
      }
      audioDispatched = true;

      const duration = Date.now() - recordingStartMs;
      const blob = new Blob(recordedChunks, { type: mediaRecorder.mimeType || 'audio/webm' });

      if (cancelRequested) {
        setStatus('Cancelled');
        return;
      }
      if (duration < MIN_RECORD_MS || blob.size < 1500) {
        setStatus('Too short — hold longer');
        vibrate(40);
        return;
      }

      setStatus('Uploading…', 'processing');

      const turnId = makeTurnId();
      const format = blob.type.includes('webm') ? 'webm' : 'ogg';
      ws.send(JSON.stringify({ type: 'audio_start', format, turn_id: turnId }));

      const arr = await blob.arrayBuffer();
      const CHUNK = 32 * 1024;
      for (let i = 0; i < arr.byteLength; i += CHUNK) {
        ws.send(arr.slice(i, i + CHUNK));
      }
      ws.send(JSON.stringify({ type: 'audio_end', turn_id: turnId }));
    };

    mediaRecorder.start();
    recordingStarted = true;
    recordingStartMs = Date.now();
    pttEl.classList.add('recording');
    pttLabelEl.textContent = 'Release to send · swipe up to cancel';
    setStatus('Recording…', 'recording');
    vibrate(30);
    return true;
  }

  function finishRecording_Legacy() {
    const wasRecording = mediaRecorder && mediaRecorder.state === 'recording';
    if (wasRecording) mediaRecorder.stop();

    recordingStarted = false;
    pttEl.classList.remove('recording', 'cancel-zone');
    pttLabelEl.textContent = 'Hold to talk';

    if (wasRecording && !cancelRequested) {
      setStatus('Processing…', 'processing');
    }
  }

  // ---------------------------------------------------------------------------
  // Dispatch: prefer in-browser SpeechRecognition; fall back to MediaRecorder.
  // ---------------------------------------------------------------------------
  async function actuallyStartRecording() {
    if (SR_AVAILABLE) return actuallyStartRecording_SR();
    return actuallyStartRecording_Legacy();
  }

  function finishRecording() {
    if (SR_AVAILABLE) return finishRecording_SR();
    return finishRecording_Legacy();
  }

  // ---------------------------------------------------------------------------
  // PTT gestures
  // ---------------------------------------------------------------------------
  function onPointerDown(e) {
    if (pttEl.disabled) return;
    if (activePointerId !== null) return;

    activePointerId = e.pointerId;
    pressStartY = e.clientY;
    cancelRequested = false;
    recordingStarted = false;

    unlockAudio();
    stopAllAudio(); // barge-in: pressing to talk interrupts playback immediately

    try { pttEl.setPointerCapture(e.pointerId); } catch (_) {}
    e.preventDefault();

    pttEl.classList.add('pressing');
    // Start recording immediately — no hold delay. Accidental taps are filtered
    // on release via MIN_RECORD_MS so we don't eat the first words of speech.
    actuallyStartRecording().then(ok => {
      if (!ok && activePointerId === e.pointerId) {
        pttEl.classList.remove('pressing');
        activePointerId = null;
      }
    });
  }

  function onPointerMove(e) {
    if (e.pointerId !== activePointerId) return;

    const inCancelZone = pressStartY - e.clientY > CANCEL_SWIPE_PX;
    if (inCancelZone && !pttEl.classList.contains('cancel-zone')) {
      pttEl.classList.add('cancel-zone');
      if (recordingStarted) {
        pttLabelEl.textContent = 'Release to cancel';
        setStatus('Release to cancel recording', 'cancel');
      }
      cancelRequested = true;
    } else if (!inCancelZone && pttEl.classList.contains('cancel-zone')) {
      pttEl.classList.remove('cancel-zone');
      if (recordingStarted) {
        pttLabelEl.textContent = 'Release to send · swipe up to cancel';
        setStatus('Recording…', 'recording');
      }
      cancelRequested = false;
    }
  }

  function onPointerUp(e) {
    if (e.pointerId !== activePointerId) return;
    activePointerId = null;
    pttEl.classList.remove('pressing');

    if (recordingStarted) {
      finishRecording();
    } else if (mediaRecorder && mediaRecorder.state === 'recording') {
      // Released before the async start() finished marking recordingStarted.
      // Treat as accidental tap.
      cancelRequested = true;
      mediaRecorder.stop();
      pttEl.classList.remove('recording', 'cancel-zone');
      pttLabelEl.textContent = 'Hold to talk';
      setStatus('Ready · hold to talk');
    }
  }

  pttEl.addEventListener('pointerdown', onPointerDown);
  pttEl.addEventListener('pointermove', onPointerMove);
  pttEl.addEventListener('pointerup', onPointerUp);
  pttEl.addEventListener('pointercancel', onPointerUp);
  pttEl.addEventListener('contextmenu', e => e.preventDefault());

  // ---------------------------------------------------------------------------
  // Secondary buttons
  // ---------------------------------------------------------------------------
  newSessionBtn.addEventListener('click', () => {
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({ type: 'new_session' }));
      logEl.innerHTML = '';
      setStatus('New session');
    }
  });

  stopAudioBtn.addEventListener('click', stopAllAudio);

  // ---------------------------------------------------------------------------
  // Text input
  // ---------------------------------------------------------------------------
  textInputEl.addEventListener('input', () => {
    sendTextBtn.disabled = textInputEl.value.trim().length === 0;
  });

  textFormEl.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = textInputEl.value.trim();
    if (!text) return;
    if (!ws || ws.readyState !== 1) {
      setStatus('Not connected yet');
      return;
    }
    stopAllAudio();
    ws.send(JSON.stringify({ type: 'text', text, turn_id: makeTurnId() }));
    textInputEl.value = '';
    sendTextBtn.disabled = true;
    setStatus('Thinking…', 'processing');
  });

  // ---------------------------------------------------------------------------
  // Microphone permission priming
  // ---------------------------------------------------------------------------
  // Try to prompt for mic permission on page load so that the first PTT press
  // doesn't block on a permission dialog. iOS Safari requires a user gesture,
  // so if the eager prime fails we wait for the first tap anywhere on the page.
  let micPrimed = false;

  async function primeMic(silent = false) {
    if (micPrimed) return true;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return false;
    try {
      // Keep the stream alive across recordings so the next press captures
      // audio instantly — no getUserMedia latency eating the first syllable.
      await ensureMicStream();
      micPrimed = true;
      return true;
    } catch (err) {
      if (!silent) {
        if (err && err.name === 'NotAllowedError') {
          setStatus('Microphone denied — enable it in browser settings', 'warn');
        } else {
          console.warn('mic prime failed', err);
        }
      }
      return false;
    }
  }

  async function checkMicPermission() {
    if (!navigator.permissions || !navigator.permissions.query) return 'unknown';
    try {
      const p = await navigator.permissions.query({ name: 'microphone' });
      return p.state; // 'granted' | 'prompt' | 'denied'
    } catch (_) {
      return 'unknown';
    }
  }

  (async () => {
    const state = await checkMicPermission();
    if (state === 'granted') { micPrimed = true; return; }
    if (await primeMic(true)) return;

    const onFirstGesture = async () => {
      document.removeEventListener('pointerdown', onFirstGesture, true);
      unlockAudio();
      await primeMic(false);
    };
    document.addEventListener('pointerdown', onFirstGesture, true);
  })();

  // ---------------------------------------------------------------------------
  // Login modal
  // ---------------------------------------------------------------------------
  function showLoginModal(message) {
    loginModalEl.classList.remove('hidden');
    loginModalEl.setAttribute('aria-hidden', 'false');
    if (message) {
      loginErrorEl.textContent = message;
      loginErrorEl.classList.remove('hidden');
    } else {
      loginErrorEl.textContent = '';
      loginErrorEl.classList.add('hidden');
    }
    loginPasswordEl.value = '';
    // Defer focus so the modal fade-in finishes first — iOS is picky about
    // programmatic focus during transitions.
    setTimeout(() => { try { loginPasswordEl.focus(); } catch (_) {} }, 50);
  }

  function hideLoginModal() {
    loginModalEl.classList.add('hidden');
    loginModalEl.setAttribute('aria-hidden', 'true');
    loginErrorEl.textContent = '';
    loginErrorEl.classList.add('hidden');
  }

  // Parse token expiry from the HMAC token format `<expires_at>.<hmac>`.
  // Client-side only — the server still verifies the signature on every
  // request, so a tampered token just fails validation.
  function tokenExpiryMs(token) {
    if (!token) return 0;
    const dot = token.indexOf('.');
    if (dot <= 0) return 0;
    const exp = parseInt(token.slice(0, dot), 10);
    return Number.isFinite(exp) ? exp * 1000 : 0;
  }

  function scheduleRefresh() {
    if (refreshTimer) { clearTimeout(refreshTimer); refreshTimer = null; }
    if (!authToken) return;
    const expMs = tokenExpiryMs(authToken);
    if (!expMs) return;
    // Refresh when <7d remain. setTimeout max is ~24.8 days, so clamp.
    const REFRESH_BEFORE_MS = 7 * 24 * 3600 * 1000;
    const delay = Math.min(
      Math.max(expMs - Date.now() - REFRESH_BEFORE_MS, 60 * 1000),
      20 * 24 * 3600 * 1000,
    );
    refreshTimer = setTimeout(refreshToken, delay);
  }

  async function refreshToken() {
    if (!authToken) return;
    try {
      const r = await fetch('/auth/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: authToken }),
      });
      if (!r.ok) {
        // Token is past the refresh window (expired). Wipe it so the next
        // boot lands on the login modal instead of bouncing off the WS
        // auth gate silently.
        authToken = '';
        try { localStorage.removeItem(TOKEN_KEY); } catch (_) {}
        return;
      }
      const { token } = await r.json();
      if (token) {
        authToken = token;
        try { localStorage.setItem(TOKEN_KEY, token); } catch (_) {}
        scheduleRefresh();
      }
    } catch (_) {
      // Transient network error — try again in 5 minutes. The WS layer
      // will surface a persistent outage anyway.
      refreshTimer = setTimeout(refreshToken, 5 * 60 * 1000);
    }
  }

  async function exchangePasswordForToken(password) {
    const r = await fetch('/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    });
    if (!r.ok) {
      let msg = 'Login failed';
      try { const j = await r.json(); if (j && j.error) msg = j.error; } catch (_) {}
      const err = new Error(msg);
      err.status = r.status;
      throw err;
    }
    const { token } = await r.json();
    authToken = token;
    try { localStorage.setItem(TOKEN_KEY, token); } catch (_) {}
    scheduleRefresh();
    return token;
  }

  loginFormEl.addEventListener('submit', async (e) => {
    e.preventDefault();
    const password = loginPasswordEl.value;
    if (!password) return;
    loginSubmitEl.disabled = true;
    loginErrorEl.classList.add('hidden');
    try {
      await exchangePasswordForToken(password);
      hideLoginModal();
      connectWS();
    } catch (err) {
      loginErrorEl.textContent = err.message || 'Network error — try again.';
      loginErrorEl.classList.remove('hidden');
    } finally {
      loginSubmitEl.disabled = false;
    }
  });

  // ---------------------------------------------------------------------------
  // Kick off
  // ---------------------------------------------------------------------------
  // Extract a login password handed to us in the URL hash, e.g.
  // https://…/#login=XXXX. We use the hash (not a query param) so the
  // password never hits server access logs or proxies. The QR code
  // produced by `expose.sh` embeds this format, which is how the user
  // avoids typing a 22-character password on their phone.
  function consumeHashLogin() {
    if (!location.hash) return '';
    const m = /(?:^#|&)login=([^&]+)/.exec(location.hash);
    if (!m) return '';
    const pwd = decodeURIComponent(m[1]);
    // Strip the hash immediately so a page reload or shared screenshot
    // doesn't leak the password. ``replaceState`` avoids adding a history
    // entry.
    try {
      const clean = location.pathname + location.search;
      history.replaceState(null, '', clean);
    } catch (_) {}
    return pwd;
  }

  (async () => {
    try {
      const r = await fetch('/auth/status');
      const j = await r.json();
      authRequired = !!j.auth_required;
    } catch (_) {
      // If /auth/status is unreachable the server is likely down; let the
      // usual WS reconnect loop surface the error instead of blocking here.
      authRequired = false;
    }

    // If we arrived via a QR-code link and don't already have a valid
    // token, redeem the embedded password silently. Failure falls through
    // to the regular login modal so the user can try typing manually.
    if (authRequired) {
      const hashPwd = consumeHashLogin();
      if (hashPwd && !authToken) {
        try {
          await exchangePasswordForToken(hashPwd);
        } catch (err) {
          showLoginModal(err.message || 'Auto-login failed.');
          return;
        }
      }
    }

    if (authRequired && !authToken) {
      showLoginModal();
      return;
    }
    if (authRequired && authToken) scheduleRefresh();
    connectWS();
  })();
})();
