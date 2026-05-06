(function () {
  const cfg = window.RECORDING_CONFIG;
  const texts = cfg.texts || [];
  const rounds = Number(cfg.rounds || 10);
  const sampleRate1 = Number(cfg.sample_rate_1 || 32000);
  const sampleRate2 = Number(cfg.sample_rate_2 || 16000);
  const channels = Number(cfg.channels || 1);
  const bitDepth = Number(cfg.bit_depth || 16);
  const initialSpeakerId = (window.INITIAL_SPEAKER_ID || "").trim();

  const speakerInput = document.getElementById("speaker_id");
  const startSessionBtn = document.getElementById("start_session_btn");
  const switchSessionBtn = document.getElementById("switch_session_btn");
  const sessionMessage = document.getElementById("session_message");
  const promptTextEl = document.getElementById("prompt_text");
  const currentTextEl = document.getElementById("current_text");
  const currentRoundEl = document.getElementById("current_round");
  const taskStatusEl = document.getElementById("task_status");
  const recordStartBtn = document.getElementById("record_start_btn");
  const recordStopBtn = document.getElementById("record_stop_btn");
  const saveBtn = document.getElementById("save_btn");
  const redoBtn = document.getElementById("redo_btn");
  const incompleteList = document.getElementById("incomplete_id_list");
  const incompleteEmpty = document.getElementById("incomplete_id_empty");
  const currentRecordsBody = document.getElementById("current_records_body");
  const currentRecordsTextFilter = document.getElementById("current_records_text_filter");
  const currentRecordsFilterReset = document.getElementById("current_records_filter_reset");
  const waveformCanvas = document.getElementById("waveform_canvas");
  const waveformCtx = waveformCanvas.getContext("2d");
  const preview32000 = document.getElementById("preview_32000");
  const preview16000 = document.getElementById("preview_16000");

  let speakerId = "";
  let textIndex = 0;
  let roundIndex = 1;
  let sessionReady = false;
  let mediaRecorder = null;
  let mediaStream = null;
  let chunks = [];
  let wavBlob32000 = null;
  let wavBlob16000 = null;
  let audioCtx = null;
  let analyser = null;
  let micSource = null;
  let waveformData = null;
  let waveformAnimId = null;
  let waitingForSaveOrRedo = false;
  let currentSpeakerRecords = [];

  function drawWaveformIdle(text) {
    const width = waveformCanvas.width;
    const height = waveformCanvas.height;
    waveformCtx.fillStyle = "#0b1220";
    waveformCtx.fillRect(0, 0, width, height);
    waveformCtx.strokeStyle = "#2d3a50";
    waveformCtx.lineWidth = 1;
    waveformCtx.beginPath();
    waveformCtx.moveTo(0, height / 2);
    waveformCtx.lineTo(width, height / 2);
    waveformCtx.stroke();
    waveformCtx.fillStyle = "#9fb3c8";
    waveformCtx.font = "14px Arial, sans-serif";
    waveformCtx.fillText(text, 14, 24);
  }

  function stopWaveform() {
    if (waveformAnimId) {
      cancelAnimationFrame(waveformAnimId);
      waveformAnimId = null;
    }
    if (micSource) {
      micSource.disconnect();
      micSource = null;
    }
    if (analyser) {
      analyser.disconnect();
      analyser = null;
    }
    if (audioCtx) {
      audioCtx.close().catch(() => {});
      audioCtx = null;
    }
    waveformData = null;
  }

  function unlockForNewSession() {
    if (mediaRecorder && mediaRecorder.state === "recording") {
      mediaRecorder.stop();
    }
    if (mediaStream) {
      mediaStream.getTracks().forEach((t) => t.stop());
      mediaStream = null;
    }
    stopWaveform();
    resetPreview();

    speakerId = "";
    sessionReady = false;
    textIndex = 0;
    roundIndex = 1;
    chunks = [];
    wavBlob32000 = null;
    wavBlob16000 = null;
    waitingForSaveOrRedo = false;
    currentSpeakerRecords = [];

    speakerInput.disabled = false;
    startSessionBtn.disabled = false;
    recordStartBtn.disabled = true;
    recordStopBtn.disabled = true;
    saveBtn.disabled = true;
    redoBtn.disabled = true;
    sessionMessage.textContent = "已解锁，可输入新 ID 开始新的录制任务。";
    setStatus("请填写新 ID，点击“确认ID并开始”。");
    drawWaveformIdle("等待新ID开始录音");
    syncProgress();
    renderCurrentRecords([]);
    speakerInput.focus();
    speakerInput.select();
  }

  function startWaveform(stream) {
    stopWaveform();
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 2048;
    analyser.smoothingTimeConstant = 0.85;
    micSource = audioCtx.createMediaStreamSource(stream);
    micSource.connect(analyser);
    waveformData = new Uint8Array(analyser.fftSize);

    const width = waveformCanvas.width;
    const height = waveformCanvas.height;
    const middle = height / 2;

    const draw = () => {
      waveformAnimId = requestAnimationFrame(draw);
      analyser.getByteTimeDomainData(waveformData);
      waveformCtx.fillStyle = "#0b1220";
      waveformCtx.fillRect(0, 0, width, height);

      waveformCtx.lineWidth = 2;
      waveformCtx.strokeStyle = "#2f80ed";
      waveformCtx.beginPath();

      const sliceWidth = width / waveformData.length;
      let x = 0;
      for (let i = 0; i < waveformData.length; i += 1) {
        const v = waveformData[i] / 128.0;
        const y = (v * middle);
        if (i === 0) {
          waveformCtx.moveTo(x, y);
        } else {
          waveformCtx.lineTo(x, y);
        }
        x += sliceWidth;
      }
      waveformCtx.lineTo(width, middle);
      waveformCtx.stroke();

      waveformCtx.fillStyle = "#9fb3c8";
      waveformCtx.font = "12px Arial, sans-serif";
      waveformCtx.fillText(`实时波形：文本 "${texts[textIndex]}" 第 ${roundIndex} 轮`, 12, 18);
    };

    draw();
  }

  function resetPreview() {
    if (preview32000.src) URL.revokeObjectURL(preview32000.src);
    if (preview16000.src) URL.revokeObjectURL(preview16000.src);
    preview32000.src = "";
    preview16000.src = "";
  }

  function syncProgress() {
    if (textIndex >= texts.length) {
      currentTextEl.textContent = "已完成";
      currentRoundEl.textContent = "-";
      promptTextEl.textContent = "全部完成";
      taskStatusEl.textContent = "所有文本录制完成。";
      recordStartBtn.disabled = true;
      recordStopBtn.disabled = true;
      saveBtn.disabled = true;
      redoBtn.disabled = true;
      drawWaveformIdle("全部录音完成");
      return;
    }
    const currentText = texts[textIndex];
    currentTextEl.textContent = currentText;
    currentRoundEl.textContent = `${roundIndex}`;
    promptTextEl.textContent = currentText;
  }

  function setStatus(msg) {
    taskStatusEl.textContent = msg;
  }

  async function parseJsonSafely(resp) {
    const raw = await resp.text();
    let data = null;
    try {
      data = JSON.parse(raw);
    } catch (err) {
      if (!resp.ok) {
        throw new Error(`服务器异常（HTTP ${resp.status}），请稍后重试。`);
      }
      throw new Error("服务器返回格式异常，无法解析。");
    }
    return data;
  }

  function renderIncompleteIds(items) {
    if (!incompleteList && !incompleteEmpty) return;
    if (!items || items.length === 0) {
      if (incompleteList) {
        incompleteList.innerHTML = "";
        incompleteList.style.display = "none";
      }
      if (incompleteEmpty) incompleteEmpty.style.display = "";
      return;
    }
    if (incompleteEmpty) incompleteEmpty.style.display = "none";
    const ul = incompleteList || (() => {
      const created = document.createElement("ul");
      created.id = "incomplete_id_list";
      created.style.paddingLeft = "18px";
      created.style.margin = "8px 0";
      const container = document.querySelector(".card h3")?.parentElement || document.body;
      container.appendChild(created);
      return created;
    })();
    ul.style.display = "";
    ul.innerHTML = "";
    items.forEach((row) => {
      const li = document.createElement("li");
      li.style.marginBottom = "8px";
      li.innerHTML = `<a href="/record?speaker_id=${encodeURIComponent(row.speaker_id)}">${row.speaker_id}</a> <span class="muted">(${row.completion_percent}%)</span>`;
      ul.appendChild(li);
    });
  }

  function setCurrentTextFilterOptions(records) {
    const textSet = new Set(records.map((r) => r.text_content));
    currentRecordsTextFilter.innerHTML = '<option value="">全部</option>';
    [...textSet].sort().forEach((txt) => {
      const opt = document.createElement("option");
      opt.value = txt;
      opt.textContent = txt;
      currentRecordsTextFilter.appendChild(opt);
    });
  }

  function applyCurrentRecordFilter() {
    const filterText = currentRecordsTextFilter.value.trim();
    const rows = currentRecordsBody.querySelectorAll("tr[data-text]");
    rows.forEach((row) => {
      const textVal = row.dataset.text || "";
      row.style.display = !filterText || filterText === textVal ? "" : "none";
    });
  }

  function rerouteToRecord(record) {
    const targetTextIndex = texts.indexOf(record.text_content);
    if (targetTextIndex < 0) {
      setStatus(`未找到文本配置：${record.text_content}`);
      return;
    }
    textIndex = targetTextIndex;
    roundIndex = Number(record.round_index);
    waitingForSaveOrRedo = false;
    wavBlob32000 = null;
    wavBlob16000 = null;
    resetPreview();
    syncProgress();
    setStatus(`已定位到重录：文本“${record.text_content}”第 ${record.round_index} 轮。点击“开始录制”。`);
    recordStartBtn.disabled = false;
    recordStopBtn.disabled = true;
    saveBtn.disabled = true;
    redoBtn.disabled = true;
    drawWaveformIdle(`准备重录：文本 "${record.text_content}" 第 ${record.round_index} 轮`);
  }

  async function deleteRecord(recordId) {
    const ok = window.confirm("确认删除这条录音吗？删除后不可恢复。");
    if (!ok) return;
    const resp = await fetch("/api/delete-record", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ record_id: Number(recordId) }),
    });
    const data = await parseJsonSafely(resp);
    if (!resp.ok || !data.ok) {
      setStatus(data.message || "删除失败。");
      return;
    }
    setStatus("删除成功。");
    renderIncompleteIds(data.incomplete_groups || []);
    await loadCurrentSpeakerRecords();
  }

  function renderCurrentRecords(records) {
    currentSpeakerRecords = records || [];
    currentRecordsBody.innerHTML = "";
    if (!speakerId) {
      currentRecordsBody.innerHTML = '<tr><td colspan="9" class="muted">请先确认 ID。</td></tr>';
      return;
    }
    if (!currentSpeakerRecords.length) {
      currentRecordsBody.innerHTML = '<tr><td colspan="9" class="muted">该ID暂无录音记录。</td></tr>';
      return;
    }
    currentSpeakerRecords.forEach((record) => {
      const tr = document.createElement("tr");
      tr.dataset.text = record.text_content;
      tr.innerHTML = `
        <td>${record.speaker_id}</td>
        <td>${record.text_content}</td>
        <td>${record.round_index}</td>
        <td>${record.sample_rate}</td>
        <td>${Number(record.duration_seconds || 0).toFixed(3)}</td>
        <td>${record.filename}</td>
        <td><audio controls preload="none" src="${record.audio_url}"></audio></td>
        <td><button type="button" class="btn-rerecord">重录</button></td>
        <td><button type="button" class="btn-delete">删除</button></td>
      `;
      tr.querySelector(".btn-rerecord").addEventListener("click", () => rerouteToRecord(record));
      tr.querySelector(".btn-delete").addEventListener("click", () => {
        deleteRecord(record.id).catch((err) => setStatus(`删除失败：${err.message}`));
      });
      currentRecordsBody.appendChild(tr);
    });
    setCurrentTextFilterOptions(currentSpeakerRecords);
    applyCurrentRecordFilter();
  }

  async function loadCurrentSpeakerRecords() {
    if (!speakerId) {
      renderCurrentRecords([]);
      return;
    }
    const resp = await fetch(`/api/speaker-recordings?speaker_id=${encodeURIComponent(speakerId)}`);
    const data = await parseJsonSafely(resp);
    if (!resp.ok || !data.ok) {
      renderCurrentRecords([]);
      return;
    }
    renderCurrentRecords(data.records || []);
  }

  async function refreshIncompleteGroups() {
    const resp = await fetch("/api/incomplete-ids");
    const data = await parseJsonSafely(resp);
    if (resp.ok && data.ok) {
      renderIncompleteIds(data.incomplete_groups || []);
    }
  }

  function applyProgress(data) {
    if (data.complete) {
      textIndex = texts.length;
      roundIndex = 1;
      syncProgress();
      setStatus("该 ID 已完成全部录制。");
      return;
    }
    textIndex = Number(data.next_text_index || 0);
    roundIndex = Number(data.next_round || 1);
    syncProgress();
  }

  async function startSession() {
    const val = speakerInput.value.trim();
    if (!val) {
      sessionMessage.textContent = "ID 不能为空。";
      return;
    }

    const resp = await fetch("/api/start-session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ speaker_id: val }),
    });
    const data = await parseJsonSafely(resp);
    if (!resp.ok || !data.ok) {
      sessionMessage.textContent = data.message || "ID 校验失败";
      return;
    }

    speakerId = val;
    sessionReady = true;
    speakerInput.disabled = true;
    startSessionBtn.disabled = true;
    sessionMessage.textContent = data.message;
    applyProgress(data);
    if (data.complete) {
      recordStartBtn.disabled = true;
      setStatus("该 ID 已完成，不可继续录制。");
      await loadCurrentSpeakerRecords();
      await refreshIncompleteGroups();
      return;
    }
    recordStartBtn.disabled = false;
    setStatus(`ID 已锁定，可从文本"${data.next_text}"第 ${data.next_round} 轮继续。`);
    await loadCurrentSpeakerRecords();
    await refreshIncompleteGroups();
  }

  async function startRecording() {
    if (!sessionReady || textIndex >= texts.length || waitingForSaveOrRedo) return;
    chunks = [];
    wavBlob32000 = null;
    wavBlob16000 = null;
    resetPreview();

    mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: channels, sampleRate: sampleRate1 },
      video: false,
    });
    startWaveform(mediaStream);

    mediaRecorder = new MediaRecorder(mediaStream);
    mediaRecorder.ondataavailable = function (event) {
      if (event.data && event.data.size > 0) chunks.push(event.data);
    };

    mediaRecorder.onstop = async function () {
      const blob = new Blob(chunks, { type: "audio/webm" });
      try {
        const rendered = await convertToDualWav(blob, [sampleRate1, sampleRate2], channels, bitDepth);
        wavBlob32000 = rendered[sampleRate1];
        wavBlob16000 = rendered[sampleRate2];

        preview32000.src = URL.createObjectURL(wavBlob32000);
        preview16000.src = URL.createObjectURL(wavBlob16000);

        saveBtn.disabled = false;
        redoBtn.disabled = false;
        setStatus("已结束录制，可保存或重录。");
      } catch (err) {
        setStatus(`音频处理失败：${err.message}`);
      }
    };

    mediaRecorder.start();
    waitingForSaveOrRedo = false;
    recordStartBtn.disabled = true;
    recordStopBtn.disabled = false;
    saveBtn.disabled = true;
    redoBtn.disabled = true;
    setStatus(`录制中：请朗读“${texts[textIndex]}”（第 ${roundIndex} 轮），点击“结束录制”停止。`);
  }

  function stopRecording() {
    if (!mediaRecorder || mediaRecorder.state !== "recording") return;
    mediaRecorder.stop();
    recordStopBtn.disabled = true;
    recordStartBtn.disabled = true;
    waitingForSaveOrRedo = true;
    if (mediaStream) {
      mediaStream.getTracks().forEach((t) => t.stop());
    }
    stopWaveform();
  }

  function redoRecording() {
    wavBlob32000 = null;
    wavBlob16000 = null;
    resetPreview();
    saveBtn.disabled = true;
    redoBtn.disabled = true;
    waitingForSaveOrRedo = false;
    drawWaveformIdle(`等待重录：文本 "${texts[textIndex]}" 第 ${roundIndex} 轮`);
    setStatus(`已清除当前录音，请重新录制文本“${texts[textIndex]}”第 ${roundIndex} 轮。`);
    recordStartBtn.disabled = false;
  }

  async function uploadRecording(blob, sampleRate) {
    const form = new FormData();
    form.append("speaker_id", speakerId);
    form.append("text", texts[textIndex]);
    form.append("round_index", String(roundIndex));
    form.append("sample_rate", String(sampleRate));
    form.append("audio", blob, `${speakerId}-${texts[textIndex]}-${roundIndex}-${sampleRate}.wav`);

    const resp = await fetch("/api/save-recording", {
      method: "POST",
      body: form,
    });
    const data = await parseJsonSafely(resp);
    if (!resp.ok || !data.ok) {
      throw new Error(data.message || "上传失败");
    }
  }

  async function saveAndNext() {
    if (!wavBlob32000 || !wavBlob16000) {
      setStatus("请先完成录制并结束。");
      return;
    }

    saveBtn.disabled = true;
    redoBtn.disabled = true;
    recordStartBtn.disabled = true;
    setStatus("保存中...");

    const prevTextIndex = textIndex;
    try {
      await uploadRecording(wavBlob32000, sampleRate1);
      await uploadRecording(wavBlob16000, sampleRate2);
      const progressResp = await fetch(`/api/session-progress?speaker_id=${encodeURIComponent(speakerId)}`);
      const progressData = await progressResp.json();
      if (progressResp.ok && progressData.ok) {
        applyProgress(progressData);
      }
      await loadCurrentSpeakerRecords();
      await refreshIncompleteGroups();
    } catch (err) {
      setStatus(`保存失败：${err.message}`);
      saveBtn.disabled = false;
      redoBtn.disabled = false;
      recordStartBtn.disabled = true;
      return;
    }

    waitingForSaveOrRedo = false;
    wavBlob32000 = null;
    wavBlob16000 = null;
    resetPreview();
    if (textIndex >= texts.length) {
      setStatus("全部录音任务完成。");
      recordStartBtn.disabled = true;
    } else {
      const movedToNextText = textIndex > prevTextIndex;
      if (movedToNextText) {
        const finishedText = texts[prevTextIndex];
        const nextText = texts[textIndex];
        const confirmed = window.confirm(`文本“${finishedText}”已完成，是否开始下一个文本“${nextText}”？`);
        if (confirmed) {
          setStatus(`请开始下一个文本：文本“${nextText}”第 ${roundIndex} 轮。`);
        } else {
          setStatus(`已完成文本“${finishedText}”。准备好后再开始文本“${nextText}”第 ${roundIndex} 轮。`);
        }
      } else {
        setStatus(`保存成功，下一条：文本“${texts[textIndex]}”第 ${roundIndex} 轮。`);
      }
      recordStartBtn.disabled = false;
    }
  }

  function encodeWavFromBuffer(audioBuffer, outRate, outChannels, outBitDepth) {
    const frameCount = audioBuffer.length;
    const bytesPerSample = outBitDepth / 8;
    const blockAlign = outChannels * bytesPerSample;
    const byteRate = outRate * blockAlign;
    const dataSize = frameCount * blockAlign;
    const buffer = new ArrayBuffer(44 + dataSize);
    const view = new DataView(buffer);

    writeString(view, 0, "RIFF");
    view.setUint32(4, 36 + dataSize, true);
    writeString(view, 8, "WAVE");
    writeString(view, 12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, outChannels, true);
    view.setUint32(24, outRate, true);
    view.setUint32(28, byteRate, true);
    view.setUint16(32, blockAlign, true);
    view.setUint16(34, outBitDepth, true);
    writeString(view, 36, "data");
    view.setUint32(40, dataSize, true);

    const channelsData = [];
    for (let c = 0; c < outChannels; c += 1) {
      channelsData.push(audioBuffer.getChannelData(c));
    }
    let offset = 44;

    for (let i = 0; i < frameCount; i += 1) {
      for (let c = 0; c < outChannels; c += 1) {
        let sample = channelsData[c][i];
        sample = Math.max(-1, Math.min(1, sample));
        const int16 = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
        view.setInt16(offset, int16, true);
        offset += 2;
      }
    }

    return new Blob([buffer], { type: "audio/wav" });
  }

  function writeString(view, offset, str) {
    for (let i = 0; i < str.length; i += 1) {
      view.setUint8(offset + i, str.charCodeAt(i));
    }
  }

  async function convertToDualWav(blob, rates, outChannels, outBitDepth) {
    const arrayBuffer = await blob.arrayBuffer();
    const ac = new AudioContext();
    const decoded = await ac.decodeAudioData(arrayBuffer.slice(0));
    await ac.close();

    const result = {};
    for (const r of rates) {
      const targetLength = Math.ceil(decoded.duration * r);
      const offCtx = new OfflineAudioContext(outChannels, targetLength, r);
      const src = offCtx.createBufferSource();

      let inputBuffer = decoded;
      if (decoded.numberOfChannels !== outChannels && outChannels === 1) {
        const mono = offCtx.createBuffer(1, decoded.length, decoded.sampleRate);
        const data = mono.getChannelData(0);
        for (let c = 0; c < decoded.numberOfChannels; c += 1) {
          const srcData = decoded.getChannelData(c);
          for (let i = 0; i < srcData.length; i += 1) {
            data[i] += srcData[i] / decoded.numberOfChannels;
          }
        }
        inputBuffer = mono;
      }

      src.buffer = inputBuffer;
      src.connect(offCtx.destination);
      src.start(0);
      const rendered = await offCtx.startRendering();
      result[r] = encodeWavFromBuffer(rendered, r, outChannels, outBitDepth);
    }
    return result;
  }

  startSessionBtn.addEventListener("click", () => {
    startSession().catch((err) => {
      sessionMessage.textContent = `请求失败：${err.message}`;
    });
  });

  recordStartBtn.addEventListener("click", () => {
    startRecording().catch((err) => {
      setStatus(`无法开始录音：${err.message}`);
    });
  });

  recordStopBtn.addEventListener("click", stopRecording);
  saveBtn.addEventListener("click", () => {
    saveAndNext().catch((err) => setStatus(`保存异常：${err.message}`));
  });
  redoBtn.addEventListener("click", redoRecording);
  switchSessionBtn.addEventListener("click", unlockForNewSession);
  currentRecordsTextFilter.addEventListener("change", applyCurrentRecordFilter);
  currentRecordsFilterReset.addEventListener("click", () => {
    currentRecordsTextFilter.value = "";
    applyCurrentRecordFilter();
  });

  syncProgress();
  drawWaveformIdle("等待开始录音");
  renderCurrentRecords([]);
  if (initialSpeakerId) {
    speakerInput.value = initialSpeakerId;
    startSession().catch((err) => {
      sessionMessage.textContent = `自动继续失败：${err.message}`;
    });
  }
})();
