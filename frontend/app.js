// Claude Voice — PWA client
(() => {
  const statusEl = document.getElementById('status');
  const logEl = document.getElementById('log');
  const pttEl = document.getElementById('ptt');
  const pttLabelEl = pttEl.querySelector('.ptt-label');
  const sessionEl = document.getElementById('session');
  const newSessionBtn = document.getElementById('new-session');
  const stopAudioBtn = document.getElementById('stop-audio');
  const modeToggleEl = document.getElementById('mode-toggle');
  const modeIconEl = modeToggleEl.querySelector('.mode-icon');
  const modeLabelEl = modeToggleEl.querySelector('.mode-label');

  const MODE_KEY = 'claude-voice-mode';
  let mode = localStorage.getItem(MODE_KEY) === 'text' ? 'text' : 'voice';
  applyModeUI();

  function applyModeUI() {
    modeToggleEl.dataset.mode = mode;
    if (mode === 'voice') {
      modeIconEl.textContent = '🎙️';
      modeLabelEl.textContent = 'Voice';
    } else {
      modeIconEl.textContent = '⌨️';
      modeLabelEl.textContent = 'Text';
    }
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

  modeToggleEl.addEventListener('click', () => {
    setMode(mode === 'voice' ? 'text' : 'voice');
  });

  // --- Tunables ---
  const MIN_HOLD_MS = 220;          // below this: treat press as accidental tap
  const MIN_RECORD_MS = 500;        // below this after recording started: drop
  const CANCEL_SWIPE_PX = 80;       // drag up > this many px: cancel recording

  let ws = null;
  let mediaRecorder = null;
  let mediaStream = null;
  let recordedChunks = [];
  let audioQueue = [];
  let isPlayingAudio = false;
  let currentAudio = null;
  let ttsCurrentChunks = [];
  let assistantLiveEl = null;

  let pressStartMs = 0;
  let pressStartY = 0;
  let holdTimer = null;
  let recordingStarted = false;
  let recordingStartMs = 0;
  let cancelRequested = false;
  let activePointerId = null;

  function setStatus(text, cls) {
    statusEl.textContent = text;
    statusEl.className = 'status' + (cls ? ' ' + cls : '');
  }

  function vibrate(ms) {
    if (navigator.vibrate) { try { navigator.vibrate(ms); } catch (_) {} }
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

  function stopAllAudio() {
    audioQueue.length = 0;
    if (currentAudio) { try { currentAudio.pause(); } catch (_) {} currentAudio = null; }
    isPlayingAudio = false;
  }

  function enqueueAudio(blob) {
    audioQueue.push(blob);
    if (!isPlayingAudio) playNext();
  }

  function playNext() {
    if (audioQueue.length === 0) { isPlayingAudio = false; return; }
    isPlayingAudio = true;
    const blob = audioQueue.shift();
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    currentAudio = audio;
    const cleanup = () => { URL.revokeObjectURL(url); currentAudio = null; playNext(); };
    audio.onended = cleanup;
    audio.onerror = cleanup;
    audio.play().catch(err => { console.warn('audio play failed', err); cleanup(); });
  }

  function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${location.host}/ws`;
    setStatus('Connecting…');
    ws = new WebSocket(url);
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => {
      setStatus('Connected');
      ws.send(JSON.stringify({ type: 'hello', mode }));
    };

    ws.onclose = () => {
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
      case 'transcribing': setStatus('Transcribing…'); break;
      case 'transcript': addMsg('user', data.text); break;
      case 'thinking': setStatus('Thinking…'); break;
      case 'tool_use': addMsg('assistant', `[tool: ${data.name}]`, 'tool'); break;
      case 'text_delta': appendAssistantDelta(data.text); break;
      case 'tts_start':
        ttsCurrentChunks = [];
        setStatus('Speaking…');
        break;
      case 'tts_end': {
        if (ttsCurrentChunks.length > 0) {
          enqueueAudio(new Blob(ttsCurrentChunks, { type: 'audio/mpeg' }));
        }
        ttsCurrentChunks = [];
        break;
      }
      case 'tts_error':
        console.warn('tts error', data.message);
        ttsCurrentChunks = [];
        break;
      case 'done':
        finalizeAssistant();
        setStatus('Ready · hold to talk');
        break;
      case 'error':
        addMsg('assistant', `Error: ${data.message}`, 'error');
        finalizeAssistant();
        setStatus('Ready · hold to talk');
        break;
      default: console.log('unknown msg', data);
    }
  }

  async function actuallyStartRecording() {
    if (!ws || ws.readyState !== 1) { setStatus('Not connected yet'); return false; }
    try {
      mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
      });
      recordedChunks = [];
      const mime = pickMime();
      mediaRecorder = new MediaRecorder(mediaStream, mime ? { mimeType: mime } : undefined);
      mediaRecorder.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) recordedChunks.push(e.data);
      };
      mediaRecorder.onstop = async () => {
        stopStream();
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

        stopAllAudio(); // barge-in: user talking cancels playback
        ws.send(JSON.stringify({ type: 'audio_start', format: blob.type.includes('webm') ? 'webm' : 'ogg' }));
        const arr = await blob.arrayBuffer();
        const CHUNK = 32 * 1024;
        for (let i = 0; i < arr.byteLength; i += CHUNK) {
          ws.send(arr.slice(i, i + CHUNK));
        }
        ws.send(JSON.stringify({ type: 'audio_end' }));
      };
      mediaRecorder.start();
      recordingStarted = true;
      recordingStartMs = Date.now();
      pttEl.classList.add('recording');
      pttLabelEl.textContent = 'Release to send · swipe up to cancel';
      setStatus('Recording…', 'recording');
      vibrate(30);
      return true;
    } catch (err) {
      console.error(err);
      setStatus('Microphone permission denied');
      return false;
    }
  }

  function stopStream() {
    if (mediaStream) {
      mediaStream.getTracks().forEach(t => t.stop());
      mediaStream = null;
    }
  }

  function finishRecording() {
    if (mediaRecorder && mediaRecorder.state === 'recording') {
      mediaRecorder.stop();
    }
    recordingStarted = false;
    pttEl.classList.remove('recording', 'cancel-zone');
    pttLabelEl.textContent = 'Hold to talk';
  }

  function pickMime() {
    const candidates = [
      'audio/webm;codecs=opus',
      'audio/webm',
      'audio/mp4',
      'audio/ogg;codecs=opus',
    ];
    for (const m of candidates) {
      if (MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(m)) return m;
    }
    return '';
  }

  function onPointerDown(e) {
    if (pttEl.disabled) return;
    if (activePointerId !== null) return;
    activePointerId = e.pointerId;
    pressStartMs = Date.now();
    pressStartY = e.clientY;
    cancelRequested = false;
    recordingStarted = false;
    try { pttEl.setPointerCapture(e.pointerId); } catch (_) {}
    e.preventDefault();

    pttEl.classList.add('pressing');
    holdTimer = setTimeout(async () => {
      holdTimer = null;
      const ok = await actuallyStartRecording();
      if (!ok) {
        pttEl.classList.remove('pressing');
        activePointerId = null;
      }
    }, MIN_HOLD_MS);
  }

  function onPointerMove(e) {
    if (e.pointerId !== activePointerId) return;
    const dy = pressStartY - e.clientY;
    const inCancelZone = dy > CANCEL_SWIPE_PX;
    if (inCancelZone) {
      if (!pttEl.classList.contains('cancel-zone')) {
        pttEl.classList.add('cancel-zone');
        if (recordingStarted) {
          pttLabelEl.textContent = 'Release to cancel';
          setStatus('Release to cancel recording', 'cancel');
        }
      }
      cancelRequested = true;
    } else {
      if (pttEl.classList.contains('cancel-zone')) {
        pttEl.classList.remove('cancel-zone');
        if (recordingStarted) {
          pttLabelEl.textContent = 'Release to send · swipe up to cancel';
          setStatus('Recording…', 'recording');
        }
      }
      cancelRequested = false;
    }
  }

  function onPointerUp(e) {
    if (e.pointerId !== activePointerId) return;
    activePointerId = null;
    pttEl.classList.remove('pressing');

    if (holdTimer) {
      clearTimeout(holdTimer);
      holdTimer = null;
      setStatus('Ready · hold to talk');
      return;
    }

    if (recordingStarted) {
      finishRecording();
    }
  }

  pttEl.addEventListener('pointerdown', onPointerDown);
  pttEl.addEventListener('pointermove', onPointerMove);
  pttEl.addEventListener('pointerup', onPointerUp);
  pttEl.addEventListener('pointercancel', onPointerUp);
  pttEl.addEventListener('contextmenu', e => e.preventDefault());

  newSessionBtn.addEventListener('click', () => {
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({ type: 'new_session' }));
      logEl.innerHTML = '';
      setStatus('New session');
    }
  });

  stopAudioBtn.addEventListener('click', stopAllAudio);

  connectWS();
})();
