
const BASE = "http://localhost:8000";
const MACHINES = ["CNC_01", "CNC_02", "PUMP_03", "CONVEYOR_04"];
const MAX_HIST = 60;
const MAX_EVENTS = 28;

const SENSOR_LABELS = {
  temperature_C: { label: "Temp", unit: "°C", color: "#ff6b6b" },
  vibration_mm_s: { label: "Vibr", unit: "mm/s", color: "#ffd93d" },
  rpm: { label: "RPM", unit: "rpm", color: "#6bffb8" },
  current_A: { label: "Curr", unit: "A", color: "#74b9ff" },
};

const state = {};
MACHINES.forEach(machineId => {
  state[machineId] = {
    reading: null,
    history: { temperature_C: [], vibration_mm_s: [], rpm: [], current_A: [] },
    connStatus: "connecting",
    evtSource: null,
    reconnects: 0,
    absenceCount: 0,
    lastEventAt: null,
    miniHoverIndex: null,
  };
});

let alerts = [];
let maintenance = [];
let rankedMachines = [...MACHINES];
let chartSensor = "temperature_C";
let opsFeed = [];
let dashboardEventsSource = null;
let analyticsData = null;
let analyticsError = "";
let analyticsSelection = {
  machineId: MACHINES[0],
  date: new Date().toISOString().slice(0, 10),
};

function levelClass(level, reading) {
  if (reading?.status === "data_absent") return "absent";
  return level || "unknown";
}

function signalLabel(machineId) {
  const s = state[machineId];
  if (s.absenceCount > 0) return `DATA ABSENT x${s.absenceCount}`;
  if (s.connStatus === "err") return "RECONNECTING";
  if (s.connStatus === "connecting") return "CONNECTING";
  return "STREAM OK";
}

function pushEvent(machineId, kind, message, tone = "ok") {
  const id = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  opsFeed.unshift({
    id,
    machineId,
    kind,
    message,
    tone,
    ts: new Date().toISOString(),
  });
  opsFeed = opsFeed.slice(0, MAX_EVENTS);
  renderOpsFeed();
}

function hydrateMachineStates(machineStates = {}) {
  let changed = false;

  Object.entries(machineStates).forEach(([machineId, reading]) => {
    const local = state[machineId];
    if (!local || !reading) return;

    local.reading = reading;
    local.lastEventAt = reading.timestamp || local.lastEventAt;

    if (local.connStatus !== "ok") {
      local.connStatus = "ok";
      setDot(machineId, "ok");
    }

    for (const key of Object.keys(local.history)) {
      const value = reading[key];
      if (typeof value !== "number") continue;
      const hist = local.history[key];
      if (!hist.length || hist[hist.length - 1] !== value) {
        hist.push(value);
        if (hist.length > MAX_HIST) hist.shift();
      }
    }

    changed = true;
  });

  if (changed) {
    buildCards();
    drawChart();
  }
}

async function loadInitialHistory() {
  try {
    const responses = await Promise.all(
      MACHINES.map(machineId =>
        fetch(`${BASE}/history/${machineId}?limit=${MAX_HIST}`)
          .then(res => res.ok ? res.json() : null)
          .catch(() => null)
      )
    );

    responses.forEach((payload, index) => {
      if (!payload || !Array.isArray(payload.data)) return;

      const machineId = MACHINES[index];
      const local = state[machineId];
      const readings = payload.data.slice(-MAX_HIST);

      Object.keys(local.history).forEach(sensorKey => {
        local.history[sensorKey] = readings
          .map(reading => reading[sensorKey])
          .filter(value => typeof value === "number");
      });

      const latest = readings[readings.length - 1];
      if (latest) {
        local.lastEventAt = latest.timestamp || local.lastEventAt;
      }
    });

    buildCards();
    drawChart();
    drawMachineMiniCharts();
  } catch (err) {
    pushEvent("SYSTEM", "history", "Failed to preload machine history.", "warn");
  }
}

function buildCards() {
  const panel = document.getElementById("machines-panel");
  const machines = [...MACHINES].sort((a, b) => {
    const aScore = state[a].reading?.risk_score || 0;
    const bScore = state[b].reading?.risk_score || 0;
    return bScore - aScore;
  });
  rankedMachines = machines;

  panel.innerHTML = machines.map((machineId, index) => {
    const reading = state[machineId].reading || {};
    const level = levelClass(reading.risk_level, reading);
    const score = reading.risk_score || 0;
    const topPriority = index === 0 && score > 0;
    const cardClass = `machine-card ${level} ${topPriority ? "top-priority" : ""}`;
    const badgeLabel = level === "absent" ? "ABSENT" : (reading.risk_level || "unknown").toUpperCase();
    const badgeClass = `risk-badge ${level === "absent" ? "absent" : (reading.risk_level || "unknown")}`;
    const pillClass = `priority-pill ${topPriority ? "hot" : ""}`;
    const explanationClass = `explanation ${level}`;
    const signalTone = state[machineId].connStatus === "ok"
      ? "ok"
      : (state[machineId].absenceCount > 0 ? "absent" : state[machineId].connStatus);

    return `
      <div class="${cardClass}" id="card-${machineId}">
        <div class="${pillClass}">Priority #${index + 1}${topPriority ? " • active focus" : ""}</div>
        <div class="card-header">
          <div class="machine-name">${machineId}</div>
          <div class="${badgeClass}" id="badge-${machineId}">${badgeLabel}</div>
        </div>
        <div class="signal-row">
          <div class="signal-chip ${signalTone}">${signalLabel(machineId)}</div>
          <div class="signal-chip">RECONNECTS ${state[machineId].reconnects}</div>
          <div class="signal-chip">ALERT MODE ${score > 0.65 ? "ON" : "STANDBY"}</div>
        </div>
        <div class="sensors">
          ${Object.entries(SENSOR_LABELS).map(([key, meta]) => {
            const value = reading[key];
            const zScore = (reading.z_scores || {})[key] || 0;
            const sensorClass = "sensor-value" + (zScore > 4 ? " alert-high" : zScore > 2.5 ? " alert-mid" : "");
            return `
              <div class="sensor-item">
                <div class="sensor-label">${meta.label}</div>
                <div class="${sensorClass}">${value ?? "--"}<span class="sensor-unit">${meta.unit}</span></div>
              </div>
            `;
          }).join("")}
        </div>
        <div class="risk-bar-wrap">
          <div class="risk-bar-label">
            <span>RISK SCORE</span>
            <span>${score.toFixed(3)}</span>
          </div>
          <div class="risk-bar-track">
            <div class="risk-bar-fill" style="width:${Math.min(score * 100, 100)}%;background:${score > 0.65 ? "var(--red)" : score > 0.35 ? "var(--yellow)" : "var(--green)"}"></div>
          </div>
        </div>
        <div class="mini-chart-wrap">
          <div class="mini-chart-head">
            <div class="mini-chart-title">Live Sensor Trends</div>
            <div class="mini-chart-legend">
              ${Object.entries(SENSOR_LABELS).map(([, meta]) => `
                <span class="mini-chart-key">
                  <span class="mini-chart-swatch" style="background:${meta.color}"></span>
                  ${meta.label}
                </span>
              `).join("")}
            </div>
          </div>
          <canvas class="mini-chart" id="mini-chart-${machineId}" height="96"></canvas>
          <div class="mini-chart-tooltip" id="mini-tooltip-${machineId}"></div>
        </div>
        <div class="${explanationClass}">${reading.explanation || "Awaiting data..."}</div>
        <div class="card-footer">
          <span>${state[machineId].lastEventAt ? `UPDATED ${new Date(state[machineId].lastEventAt).toLocaleTimeString()}` : "UPDATED --"}</span>
          <span>${reading.suppressed ? "TRANSIENT SPIKE SUPPRESSED" : (score > 0.65 ? "CRITICAL PATH" : "MONITORING")}</span>
        </div>
      </div>
    `;
  }).join("");

  buildChartSelector();
  updateHeaderSummary();
  attachMiniChartInteractions();
  drawMachineMiniCharts();
}

function buildConnBar() {
  const bar = document.getElementById("conn-bar");
  bar.innerHTML = MACHINES.map(machineId => `
    <div class="conn-item">
      <div class="conn-dot connecting" id="dot-${machineId}"></div>
      <span>${machineId}</span>
    </div>
  `).join("") + `<span style="margin-left:auto">API: ${BASE}</span>`;
}

function buildChartSelector() {
  const wrap = document.getElementById("chart-selector");
  wrap.innerHTML =
    Object.entries(SENSOR_LABELS).map(([key, meta]) =>
      `<button class="chart-btn${key === chartSensor ? " active" : ""}" onclick="selectChartSensor('${key}')">${meta.label}</button>`
    ).join("");
}

function updateHeaderSummary() {
  const topMachine = rankedMachines[0];
  const topReading = topMachine ? state[topMachine].reading : null;
  const topScore = topReading?.risk_score || 0;
  document.getElementById("summary-highest-risk").textContent = topScore.toFixed(3);
  document.getElementById("summary-highest-machine").textContent = topMachine
    ? `${topMachine} • ${(topReading?.risk_level || "unknown").toUpperCase()}`
    : "Awaiting data";
  document.getElementById("summary-alerts").textContent = String(alerts.length);
  document.getElementById("summary-maintenance").textContent = String(maintenance.length);
  document.getElementById("active-alerts-header").textContent = `${alerts.length} ALERTS`;
  document.getElementById("header-priority").textContent = topMachine
    ? `TOP PRIORITY: ${topMachine} ${topScore.toFixed(3)}`
    : "TOP PRIORITY: --";
  document.getElementById("ranked-at").textContent = `RANKED: ${new Date().toLocaleTimeString()}`;
}

function setDot(machineId, status) {
  const dot = document.getElementById(`dot-${machineId}`);
  if (dot) dot.className = `conn-dot ${status}`;
}

function connectMachine(machineId) {
  const s = state[machineId];
  if (s.evtSource) s.evtSource.close();

  const es = new EventSource(`${BASE}/stream/${machineId}`);
  s.evtSource = es;
  s.connStatus = "connecting";
  setDot(machineId, "connecting");
  buildCards();
  pushEvent(machineId, "stream", "Connecting to scored SSE stream.", "warn");

  es.onmessage = event => {
    const data = JSON.parse(event.data);
    s.connStatus = "ok";
    s.lastEventAt = data.timestamp || new Date().toISOString();

    if (data.signal === "data_absent") {
      s.absenceCount = data.consecutive_gaps || (s.absenceCount + 1);
      s.reading = {
        ...s.reading,
        status: "data_absent",
        risk_level: "critical",
        risk_score: Math.max(s.reading?.risk_score || 0, 0.8),
        explanation: `No data received from the machine feed. Gap count: ${s.absenceCount}.`,
        timestamp: data.timestamp,
      };
      setDot(machineId, "absent");
      pushEvent(machineId, "absence", `Data absence signal detected. Gap #${s.absenceCount}.`, "absent");
      buildCards();
      drawChart();
      return;
    }

    const wasAbsent = s.absenceCount > 0;
    s.absenceCount = 0;
    s.reading = data;
    setDot(machineId, "ok");

    for (const key of Object.keys(s.history)) {
      s.history[key].push(data[key] ?? 0);
      if (s.history[key].length > MAX_HIST) s.history[key].shift();
    }

    if (wasAbsent) {
      pushEvent(machineId, "recovered", "Stream recovered after data absence.", "ok");
    }
    if ((data.risk_score || 0) > 0.65 && !data.suppressed) {
      pushEvent(machineId, "critical", `Critical scoring event ${data.risk_score.toFixed(3)} queued for alerting.`, "err");
    } else if (data.suppressed) {
      pushEvent(machineId, "suppressed", "Transient spike detected and suppressed by the scorer.", "warn");
    }

    buildCards();
    drawChart();
  };

  es.onerror = () => {
    s.connStatus = "err";
    s.reconnects += 1;
    setDot(machineId, "err");
    pushEvent(machineId, "reconnect", `Stream interrupted. Retrying connection attempt ${s.reconnects}.`, "err");
    buildCards();
    es.close();
    setTimeout(() => connectMachine(machineId), 3000);
  };
}

function connectDashboardEvents() {
  if (dashboardEventsSource) dashboardEventsSource.close();

  const es = new EventSource(`${BASE}/dashboard-events`);
  dashboardEventsSource = es;

  es.onmessage = event => {
    const data = JSON.parse(event.data);
    const kind = data.event;
    const payload = data.payload || {};

    if (kind === "snapshot") {
      alerts = payload.alerts || alerts;
      maintenance = payload.maintenance || maintenance;
    } else if (kind === "alert") {
      alerts = [payload, ...alerts.filter(item => item.alert_id !== payload.alert_id)].slice(0, 50);
      pushEvent(payload.machine_id || "SYSTEM", "alert", "Alert queue updated in real time.", "err");
    } else if (kind === "maintenance") {
      maintenance = [payload, ...maintenance.filter(item => item.booking_id !== payload.booking_id)].slice(0, 20);
      pushEvent(payload.machine_id || "SYSTEM", "maintenance", "Maintenance booking received in real time.", "warn");
    }

    renderAlerts();
    renderMaintenance();
    updateHeaderSummary();
  };

  es.onerror = () => {
    if (dashboardEventsSource) dashboardEventsSource.close();
    setTimeout(connectDashboardEvents, 3000);
  };
}

async function pollDashboardData() {
  try {
    const [dashboardRes, statusRes] = await Promise.all([
      fetch(`${BASE}/dashboard-data`),
      fetch(`${BASE}/status`),
    ]);

    const data = dashboardRes.ok ? await dashboardRes.json() : {};
    const statusData = statusRes.ok ? await statusRes.json() : {};

    alerts = alerts.length ? alerts : (data.recent_alerts || []);
    maintenance = maintenance.length ? maintenance : (data.recent_maintenance || []);
    hydrateMachineStates(data.machine_states || statusData.machine_states || {});
    renderAlerts();
    renderMaintenance();
    updateHeaderSummary();
  } catch (err) {
    pushEvent("SYSTEM", "poll", "Failed to refresh dashboard aggregates from backend.", "err");
  }
}

function renderAlerts() {
  const list = document.getElementById("alert-list");
  document.getElementById("alert-count").textContent = String(alerts.length);

  if (!alerts.length) {
    list.innerHTML = '<div class="list-empty">NO ACTIVE ALERTS</div>';
    return;
  }

  list.innerHTML = alerts.slice(0, 12).map(alert => {
    const warning = alert.risk_score < 0.65;
    return `
      <div class="alert-item ${warning ? "warning" : ""}">
        <div class="list-row">
          <div class="list-label">${alert.machine_id}</div>
          <div class="list-score ${warning ? "warning" : ""}">RISK ${Number(alert.risk_score || 0).toFixed(3)}</div>
        </div>
        <div class="list-copy">${alert.reason}</div>
        <div class="list-meta">${new Date(alert.timestamp).toLocaleTimeString()} • ${alert.auto ? "AUTO" : "MANUAL"} ALERT</div>
      </div>
    `;
  }).join("");
}

function renderMaintenance() {
  const list = document.getElementById("maintenance-list");
  document.getElementById("maintenance-count").textContent = String(maintenance.length);

  if (!maintenance.length) {
    list.innerHTML = '<div class="list-empty">NO BOOKINGS YET</div>';
    return;
  }

  list.innerHTML = maintenance.slice(0, 10).map(slot => `
    <div class="maintenance-item">
      <div class="list-row">
        <div class="list-label">${slot.machine_id}</div>
        <div class="list-score" style="color:var(--accent)">${String(slot.priority || "normal").toUpperCase()}</div>
      </div>
      <div class="list-copy">Scheduled for ${new Date(slot.scheduled_at).toLocaleString()}</div>
      <div class="list-meta">BOOKING ${slot.booking_id || "--"} • REQUESTED BY ${slot.requested_by || "agent"}</div>
    </div>
  `).join("");
}

function renderOpsFeed() {
  const list = document.getElementById("ops-list");
  document.getElementById("ops-count").textContent = String(opsFeed.length);

  if (!opsFeed.length) {
    list.innerHTML = '<div class="list-empty">AWAITING EVENTS</div>';
    return;
  }

  list.innerHTML = opsFeed.map(item => `
    <div class="ops-item ${item.tone}">
      <div class="list-row">
        <div class="list-label">${item.machineId}</div>
        <div class="list-meta">${item.kind.toUpperCase()}</div>
      </div>
      <div class="ops-copy">${item.message}</div>
      <div class="list-meta">${new Date(item.ts).toLocaleTimeString()}</div>
    </div>
  `).join("");
}

function formatAnalyticsTimestamp(value) {
  if (!value) return "--";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function renderAnalytics() {
  const machineLabel = document.getElementById("analytics-machine-label");
  const dateLabel = document.getElementById("analytics-date-label");
  const status = document.getElementById("analytics-status");
  const summaryGrid = document.getElementById("analytics-summary-grid");
  const sensorGrid = document.getElementById("analytics-sensor-grid");
  const warningList = document.getElementById("analytics-warning-list");
  const historyList = document.getElementById("analytics-history-list");
  const warningCount = document.getElementById("analytics-warning-count");
  const historyTotal = document.getElementById("analytics-history-total");

  machineLabel.textContent = analyticsSelection.machineId || "--";
  dateLabel.textContent = analyticsSelection.date || "--";

  if (!analyticsData) {
    status.textContent = "WAITING";
    const emptyMessage = analyticsError || "Load analytics to inspect one machine in detail.";
    summaryGrid.innerHTML = `<div class="analytics-empty">${emptyMessage}</div>`;
    sensorGrid.innerHTML = '<div class="analytics-empty">Sensor stats will appear here.</div>';
    warningList.innerHTML = '<div class="analytics-empty">No analytics loaded yet.</div>';
    historyList.innerHTML = '<div class="analytics-empty">No historical warning data loaded yet.</div>';
    warningCount.textContent = "0 EVENTS";
    historyTotal.textContent = "0 TOTAL";
    return;
  }

  status.textContent = "LIVE";
  const summary = analyticsData.summary || {};
  const currentState = summary.current_state || {};
  summaryGrid.innerHTML = [
    { key: "Readings", value: summary.total_readings ?? 0, note: `For ${analyticsData.date}` },
    { key: "Warnings", value: summary.warning_count ?? 0, note: "Status=warning on selected day" },
    { key: "Faults", value: summary.fault_count ?? 0, note: "Status=fault on selected day" },
    { key: "Current Risk", value: Number(currentState.risk_score || 0).toFixed(3), note: (currentState.risk_level || "unknown").toUpperCase() },
  ].map(item => `
    <div class="analytics-summary-item">
      <div class="analytics-key">${item.key}</div>
      <div class="analytics-value">${item.value}</div>
      <div class="analytics-note">${item.note}</div>
    </div>
  `).join("");

  const sensorStats = analyticsData.sensor_stats || {};
  sensorGrid.innerHTML = Object.entries(SENSOR_LABELS).map(([key, meta]) => {
    const stats = sensorStats[key] || {};
    return `
      <div class="analytics-sensor-item">
        <div class="analytics-key">${meta.label}</div>
        <div class="analytics-sensor-value">
          MIN ${stats.min ?? "--"} ${meta.unit}<br/>
          MAX ${stats.max ?? "--"} ${meta.unit}<br/>
          AVG ${stats.avg ?? "--"} ${meta.unit}
        </div>
      </div>
    `;
  }).join("");

  const warningsToday = analyticsData.warnings_today || {};
  const warningEvents = warningsToday.events || [];
  warningCount.textContent = `${warningEvents.length} EVENTS`;
  warningList.innerHTML = warningEvents.length
    ? warningEvents.slice().reverse().map(item => `
      <div class="analytics-list-item ${(item.status || "").toLowerCase()}">
        <div class="list-row">
          <div class="list-label">${(item.status || "warning").toUpperCase()}</div>
          <div class="list-meta">${formatAnalyticsTimestamp(item.timestamp)}</div>
        </div>
        <div class="list-copy">Temp ${item.temperature_C ?? "--"} • Vibr ${item.vibration_mm_s ?? "--"} • RPM ${item.rpm ?? "--"} • Curr ${item.current_A ?? "--"}</div>
      </div>
    `).join("")
    : '<div class="analytics-empty">No warnings or faults recorded for this machine on the selected day.</div>';

  const warningHistory = analyticsData.warning_history || {};
  const recentEvents = warningHistory.recent_events || [];
  const alertsCount = analyticsData.alerts?.count || 0;
  const maintenanceCountValue = analyticsData.maintenance?.count || 0;
  historyTotal.textContent = `${warningHistory.total_count || 0} TOTAL`;
  historyList.innerHTML = `
    <div class="analytics-list-item">
      <div class="list-row">
        <div class="list-label">ALERTS</div>
        <div class="list-score">${alertsCount}</div>
      </div>
      <div class="list-copy">Recent alerts raised for ${analyticsData.machine_id}.</div>
    </div>
    <div class="analytics-list-item">
      <div class="list-row">
        <div class="list-label">MAINTENANCE</div>
        <div class="list-score" style="color:var(--accent)">${maintenanceCountValue}</div>
      </div>
      <div class="list-copy">Bookings created for this machine.</div>
    </div>
    ${recentEvents.slice().reverse().map(item => `
      <div class="analytics-list-item ${(item.status || "").toLowerCase()}">
        <div class="list-row">
          <div class="list-label">${(item.status || "warning").toUpperCase()}</div>
          <div class="list-meta">${formatAnalyticsTimestamp(item.timestamp)}</div>
        </div>
        <div class="list-copy">Temp ${item.temperature_C ?? "--"} • Vibr ${item.vibration_mm_s ?? "--"} • RPM ${item.rpm ?? "--"} • Curr ${item.current_A ?? "--"}</div>
      </div>
    `).join("") || '<div class="analytics-empty">No historical warning records available for this machine.</div>'}
  `;
}

async function loadAnalytics() {
  const machineId = analyticsSelection.machineId;
  const selectedDate = analyticsSelection.date;
  const status = document.getElementById("analytics-status");
  status.textContent = "LOADING";

  try {
    const res = await fetch(`${BASE}/analytics/${machineId}?for_date=${selectedDate}`);
    const payload = await res.json().catch(() => null);
    analyticsData = res.ok ? payload : null;
    if (!res.ok) {
      const detail = payload?.detail || payload?.error || `HTTP ${res.status}`;
      throw new Error(`Analytics request failed: ${detail}`);
    }
    analyticsError = "";
    status.textContent = "READY";
  } catch (err) {
    analyticsData = null;
    analyticsError = String(err.message || err);
    status.textContent = "ERROR";
    pushEvent(machineId || "SYSTEM", "analytics", "Failed to load machine analytics.", "err");
  }

  renderAnalytics();
}

function initAnalyticsControls() {
  const machineSelect = document.getElementById("analytics-machine");
  const dateInput = document.getElementById("analytics-date");
  const refreshBtn = document.getElementById("analytics-refresh");

  machineSelect.innerHTML = MACHINES.map(machineId => `<option value="${machineId}">${machineId}</option>`).join("");
  machineSelect.value = analyticsSelection.machineId;
  dateInput.value = analyticsSelection.date;

  machineSelect.addEventListener("change", () => {
    analyticsSelection.machineId = machineSelect.value;
    loadAnalytics();
  });

  dateInput.addEventListener("change", () => {
    analyticsSelection.date = dateInput.value || analyticsSelection.date;
    loadAnalytics();
  });

  refreshBtn.addEventListener("click", loadAnalytics);
  renderAnalytics();
}

function drawChart() {
  const wrap = document.getElementById("history-grid");
  if (!wrap) return;

  const meta = SENSOR_LABELS[chartSensor];
  wrap.innerHTML = rankedMachines.map(machineId => {
    const reading = state[machineId]?.reading || {};
    const level = levelClass(reading.risk_level, reading);
    return `
      <div class="history-card">
        <div class="history-card-head">
          <div class="history-machine">${machineId}</div>
          <div class="history-level ${level}">${(reading.risk_level || level || "unknown").toUpperCase()}</div>
        </div>
        <canvas id="history-chart-${machineId}" height="180"></canvas>
        <div class="history-meta">
          ${meta.label} (${meta.unit}) ${state[machineId]?.lastEventAt ? `• updated ${new Date(state[machineId].lastEventAt).toLocaleTimeString()}` : "• awaiting data"}
        </div>
      </div>
    `;
  }).join("");

  rankedMachines.forEach(machineId => drawHistoryChart(machineId, chartSensor));
}

function drawHistoryChart(machineId, sensorId) {
  const canvas = document.getElementById(`history-chart-${machineId}`);
  if (!canvas) return;

  const ctx = canvas.getContext("2d");
  const w = canvas.offsetWidth || canvas.clientWidth || 300;
  const h = canvas.offsetHeight || 180;
  canvas.width = w;
  canvas.height = h;
  ctx.clearRect(0, 0, w, h);

  const hist = state[machineId]?.history[sensorId];
  const meta = SENSOR_LABELS[sensorId];
  if (!hist || hist.length < 2) {
    ctx.fillStyle = "#7f97b6";
    ctx.font = "11px 'Share Tech Mono'";
    ctx.textAlign = "center";
    ctx.fillText("Awaiting trend data", w / 2, h / 2 + 4);
    return;
  }

  const color = meta.color;
  const min = Math.min(...hist);
  const max = Math.max(...hist);
  const range = max - min || 1;
  const pad = { t: 18, b: 26, l: 34, r: 8 };
  const pw = w - pad.l - pad.r;
  const ph = h - pad.t - pad.b;

  ctx.strokeStyle = "rgba(255,255,255,0.08)";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 3; i++) {
    const y = pad.t + (ph / 3) * i;
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(w - pad.r, y);
    ctx.stroke();
  }

  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.shadowColor = color;
  ctx.shadowBlur = 5;
  ctx.beginPath();
  hist.forEach((v, i) => {
    const x = pad.l + (i / (hist.length - 1)) * pw;
    const y = pad.t + ph - ((v - min) / range) * ph;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.shadowBlur = 0;

  const grad = ctx.createLinearGradient(0, pad.t, 0, pad.t + ph);
  grad.addColorStop(0, color + "33");
  grad.addColorStop(1, color + "00");
  ctx.fillStyle = grad;
  ctx.lineTo(pad.l + pw, pad.t + ph);
  ctx.lineTo(pad.l, pad.t + ph);
  ctx.closePath();
  ctx.fill();

  ctx.fillStyle = "#7f97b6";
  ctx.font = "10px 'Share Tech Mono'";
  ctx.textAlign = "right";
  ctx.fillText(max.toFixed(1), pad.l - 4, pad.t + 4);
  ctx.fillText(min.toFixed(1), pad.l - 4, pad.t + ph + 4);
}

function drawMachineMiniCharts() {
  rankedMachines.forEach(machineId => {
    const canvas = document.getElementById(`mini-chart-${machineId}`);
    if (!canvas) return;

    const ctx = canvas.getContext("2d");
    const width = canvas.clientWidth || canvas.offsetWidth || canvas.width || 300;
    const height = canvas.clientHeight || canvas.offsetHeight || canvas.height || 96;
    canvas.width = width;
    canvas.height = height;
    ctx.clearRect(0, 0, width, height);

    const series = Object.entries(SENSOR_LABELS)
      .map(([key, meta]) => ({
        key,
        meta,
        values: state[machineId]?.history[key] || [],
      }))
      .filter(item => item.values.length > 1);

    if (!series.length) {
      ctx.fillStyle = "#3a4a5c";
      ctx.font = "10px 'Share Tech Mono'";
      ctx.textAlign = "center";
      ctx.fillText("Awaiting trend data", width / 2, height / 2 + 3);
      return;
    }

    const pad = { t: 8, r: 6, b: 8, l: 6 };
    const plotWidth = width - pad.l - pad.r;
    const plotHeight = height - pad.t - pad.b;
    const hoverIndex = state[machineId]?.miniHoverIndex;

    ctx.strokeStyle = "rgba(255,255,255,0.06)";
    ctx.lineWidth = 1;
    for (let i = 0; i < 3; i++) {
      const y = pad.t + (plotHeight / 2) * i;
      ctx.beginPath();
      ctx.moveTo(pad.l, y);
      ctx.lineTo(width - pad.r, y);
      ctx.stroke();
    }

    series.forEach(({ values, meta }) => {
      const min = Math.min(...values);
      const max = Math.max(...values);
      const range = max - min || 1;

      ctx.strokeStyle = meta.color;
      ctx.lineWidth = 1.8;
      ctx.beginPath();
      values.forEach((value, index) => {
        const x = pad.l + (index / (values.length - 1)) * plotWidth;
        const y = pad.t + plotHeight - ((value - min) / range) * plotHeight;
        if (index === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
    });

    if (Number.isInteger(hoverIndex)) {
      const maxLength = Math.max(...series.map(item => item.values.length));
      if (maxLength > 1 && hoverIndex >= 0 && hoverIndex < maxLength) {
        const x = pad.l + (hoverIndex / (maxLength - 1)) * plotWidth;
        ctx.strokeStyle = "rgba(255,255,255,0.4)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x, pad.t);
        ctx.lineTo(x, height - pad.b);
        ctx.stroke();
      }
    }
  });
}

function attachMiniChartInteractions() {
  rankedMachines.forEach(machineId => {
    const canvas = document.getElementById(`mini-chart-${machineId}`);
    if (!canvas || canvas.dataset.hoverReady === "true") return;

    canvas.dataset.hoverReady = "true";

    canvas.addEventListener("mousemove", event => {
      const series = Object.keys(SENSOR_LABELS)
        .map(key => state[machineId]?.history[key] || []);
      const maxLength = Math.max(0, ...series.map(values => values.length));
      if (maxLength < 2) return;

      const rect = canvas.getBoundingClientRect();
      const relativeX = Math.min(Math.max(event.clientX - rect.left, 0), rect.width);
      const index = Math.round((relativeX / rect.width) * (maxLength - 1));
      state[machineId].miniHoverIndex = index;
      updateMiniTooltip(machineId, index, relativeX, event.clientY - rect.top, rect.width);
      drawMachineMiniCharts();
    });

    canvas.addEventListener("mouseleave", () => {
      state[machineId].miniHoverIndex = null;
      hideMiniTooltip(machineId);
      drawMachineMiniCharts();
    });
  });
}

function updateMiniTooltip(machineId, index, x, y, width) {
  const tooltip = document.getElementById(`mini-tooltip-${machineId}`);
  if (!tooltip) return;

  const rows = Object.entries(SENSOR_LABELS).map(([key, meta]) => {
    const values = state[machineId]?.history[key] || [];
    const value = values[index];
    return `
      <div class="mini-tooltip-row">
        <span class="mini-tooltip-label">${meta.label}</span>
        <span>${value !== undefined ? `${Number(value).toFixed(2)} ${meta.unit}` : "--"}</span>
      </div>
    `;
  }).join("");

  tooltip.innerHTML = `
    <div class="mini-tooltip-title">${machineId} sample ${index + 1}</div>
    ${rows}
  `;

  const tooltipWidth = 150;
  const left = Math.max(8, Math.min(x + 12, width - tooltipWidth - 8));
  tooltip.style.left = `${left}px`;
  tooltip.style.top = `${Math.max(8, y - 12)}px`;
  tooltip.classList.add("visible");
}

function hideMiniTooltip(machineId) {
  const tooltip = document.getElementById(`mini-tooltip-${machineId}`);
  if (tooltip) tooltip.classList.remove("visible");
}

function selectChartSensor(sensorId) {
  chartSensor = sensorId;
  buildChartSelector();
  drawChart();
}

function tick() {
  document.getElementById("global-ts").textContent = new Date().toLocaleTimeString();
}

async function initDashboard() {
  buildConnBar();
  buildCards();
  renderAlerts();
  renderMaintenance();
  renderOpsFeed();
  initAnalyticsControls();
  await loadInitialHistory();
  await pollDashboardData();
  await loadAnalytics();
  connectDashboardEvents();
  MACHINES.forEach(connectMachine);
  setInterval(pollDashboardData, 3000);
  setInterval(tick, 1000);
  setInterval(drawChart, 2000);
  setInterval(drawMachineMiniCharts, 2000);
  tick();
  window.addEventListener("resize", () => {
    drawChart();
    drawMachineMiniCharts();
  });
}

initDashboard();
