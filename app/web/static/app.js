"use strict";
(function () {
  const $ = (id) => document.getElementById(id);
  const RANGES = ["1h", "6h", "24h", "7d", "30d", "1y"];

  let tz = "America/New_York";
  let pollInterval = 5;
  let range = readStore("range");
  if (RANGES.indexOf(range) < 0) range = "24h";
  let eventPage = 0;
  let eventSize = parseInt(readStore("eventSize"), 10);
  if ([25, 50, 100].indexOf(eventSize) < 0) eventSize = 25;
  let eventTotal = 0;
  let lastCurrent = null;
  let rdBanner = null;
  let readinessState = "idle";
  let pendingSince = null;
  let lastBucket = 60;
  const charts = {};

  function readStore(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }
  }
  function writeStore(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* storage blocked */ }
  }

  // ---------------------------------------------------------------- formatting

  function parts(ts, opts) {
    const out = {};
    new Intl.DateTimeFormat("en-US", Object.assign({ timeZone: tz, hourCycle: "h23" }, opts))
      .formatToParts(new Date(ts * 1000))
      .forEach((p) => { out[p.type] = p.value; });
    return out;
  }
  function fmtDateTime(ts) {
    const p = parts(ts, { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", timeZoneName: "short" });
    return p.year + "-" + p.month + "-" + p.day + " " + p.hour + ":" + p.minute + ":" + p.second + " " + p.timeZoneName;
  }
  function fmtClock(ts) {
    const p = parts(ts, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    return p.hour + ":" + p.minute + ":" + p.second;
  }
  function fmtTick(ts) {
    if (range === "1h" || range === "6h" || range === "24h") {
      const p = parts(ts, { hour: "2-digit", minute: "2-digit" });
      return p.hour + ":" + p.minute;
    }
    const p = parts(ts, { month: "short", day: "numeric" });
    return p.month + " " + p.day;
  }
  // Seconds between x-axis ticks per range, aligned to local midnight/hour boundaries.
  const TICK_STEPS = { "1h": 600, "6h": 3600, "24h": 10800, "7d": 86400, "30d": 432000, "1y": 2592000 };
  function tzOffset(ts) {
    const p = parts(ts, { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
    return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second) / 1000 - Math.floor(ts);
  }
  function fmtDuration(s) {
    s = Math.max(0, Math.round(s));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    if (h) return h + " h " + m + " min";
    if (m) return m + " min " + sec + " s";
    return sec + " s";
  }
  function num(v, digits) {
    return v === null || v === undefined || isNaN(v) ? "–" : Number(v).toFixed(digits === undefined ? 1 : digits);
  }
  function humanize(type) { return type.replace(/_/g, " "); }

  async function getJson(url) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 10000);
    try {
      const resp = await fetch(url, { signal: ctl.signal, cache: "no-store" });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return await resp.json();
    } finally {
      clearTimeout(timer);
    }
  }

  function el(tag, className, text) {
    const e = document.createElement(tag);
    if (className) e.className = className;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  // ---------------------------------------------------------------- current status

  function setText(id, text) { $(id).textContent = text; }

  function statusBadge(flags, stale) {
    if (stale) return ["NO DATA", "badge-unknown"];
    const has = (f) => flags.indexOf(f) >= 0;
    if (has("LB")) return ["LOW BATTERY", "badge-crit"];
    if (has("OB") && has("CAL")) return ["SELF-TEST", "badge-warn"];
    if (has("OB")) return ["ON BATTERY", "badge-crit"];
    if (has("OL")) return ["ONLINE", "badge-ok"];
    return [flags.join(" ") || "UNKNOWN", "badge-warn"];
  }

  function banner(level, text) {
    const b = el("div", "banner banner-" + level, text);
    b.setAttribute("role", level === "crit" ? "alert" : "status");
    return b;
  }

  function renderBanners(d, flags, stale) {
    const box = $("banners");
    box.replaceChildren();
    const sample = d.sample;
    const sd = d.shutdown;
    if (!d.monitor.alive) {
      const age = d.monitor.last_poll_ts ? fmtDuration(d.now - d.monitor.last_poll_ts) + " ago" : "never";
      box.appendChild(banner("crit", "The monitor service is not reporting (last poll " + age + "). Values below may be out of date."));
    } else if (d.monitor.ups_ok === false) {
      box.appendChild(banner("crit", "The monitor cannot read the UPS (" + (d.monitor.error_kind || "error") + "). Gaps appear in the charts."));
    }
    if (sample && !stale && flags.indexOf("OB") >= 0 && flags.indexOf("CAL") < 0) {
      const mins = sample.battery_runtime === null ? null : sample.battery_runtime / 60;
      let text = "Running on battery" + (mins === null ? "." : ": about " + num(mins, 0) + " min of runtime left.");
      if (sd.enabled) text += " Shutdown starts at " + sd.runtime_threshold_minutes + " min or less" + (sd.dry_run ? " (dry run, nothing will be powered off)." : ".");
      box.appendChild(banner("crit", text));
    }
    if (!sd.enabled) box.appendChild(banner("warn", "Automatic shutdown is disabled in config.yaml."));
    else if (sd.dry_run) box.appendChild(banner("info", "Shutdown is in DRY RUN mode: no machine will actually be powered off."));
    if (rdBanner) box.appendChild(banner(rdBanner.level, rdBanner.text));
  }

  function kv(dl, rows) {
    dl.replaceChildren();
    rows.forEach((r) => {
      if (r[1] === null || r[1] === undefined || r[1] === "") return;
      dl.appendChild(el("dt", null, r[0]));
      dl.appendChild(el("dd", null, String(r[1])));
    });
  }

  function renderCurrent(d) {
    lastCurrent = d;
    tz = d.settings.timezone;
    pollInterval = d.settings.poll_interval_s;
    const s = d.sample;
    const info = d.info;
    const stale = !s || d.sample_age_s > d.settings.stale_after_s;
    const flags = s && s.status ? s.status.split(/\s+/) : [];

    const model = [info["ups.mfr"], info["ups.model"]].filter(Boolean).join(" ");
    setText("subtitle", model || "Waiting for first reading");
    const badge = statusBadge(flags, stale);
    const b = $("status-badge");
    b.textContent = badge[0];
    b.className = "badge " + badge[1];
    setText("updated", s ? "Last reading " + fmtClock(s.ts) + " (" + fmtDuration(d.sample_age_s) + " ago)" : "");
    $("cards").classList.toggle("stale", stale);

    const sd = d.shutdown;
    const runtimeMin = s && s.battery_runtime !== null ? s.battery_runtime / 60 : null;
    setText("v-charge", s ? num(s.battery_charge, 0) + "%" : "–");
    setText("n-charge", s && s.battery_voltage !== null ? num(s.battery_voltage, 1) + " V battery" : "");
    setText("v-runtime", runtimeMin === null ? "–" : num(runtimeMin, 0) + " min");
    setText("n-runtime", "shutdown at " + sd.runtime_threshold_minutes + " min");
    setText("v-load", s ? num(s.load_pct, 0) + "%" : "–");
    setText("n-load", s ? num(s.realpower, 0) + " W · " + num(s.power_va, 0) + " VA" : "");
    setText("v-input", s ? num(s.input_voltage, 1) + " V" : "–");
    const lo = info["input.transfer.low"], hi = info["input.transfer.high"];
    setText("n-input", s ? num(s.input_frequency, 1) + " Hz" + (lo && hi ? " · transfer " + lo + "–" + hi + " V" : "") : "");
    setText("v-output", s ? num(s.output_voltage, 1) + " V" : "–");
    setText("n-output", s ? num(s.output_current, 2) + " A · " + num(s.output_frequency, 1) + " Hz" : "");
    setText("v-temp", s && s.temperature !== null ? num(s.temperature, 1) + " °C" : "–");
    setText("n-temp", "");

    const days = info["ups.test.interval"] ? Math.round(Number(info["ups.test.interval"]) / 86400) : null;
    kv($("ups-info"), [
      ["Model", model],
      ["Serial", info["ups.serial"]],
      ["Type", info["ups.type"]],
      ["Status flags", s ? s.status : null],
      ["Last self-test", info["ups.test.result"]],
      ["Self-test date (UTC)", info["ups.test.date"]],
      ["Self-test interval", days === null ? null : days + " days"],
      ["Nominal power", info["ups.realpower.nominal"] && info["ups.power.nominal"] ? info["ups.realpower.nominal"] + " W / " + info["ups.power.nominal"] + " VA" : null],
      ["Nominal input", info["input.voltage.nominal"] ? info["input.voltage.nominal"] + " V" : null],
      ["Low battery (raw)", info["battery.low"]],
    ]);
    const order = sd.machines.map((m, i) => (i + 1) + ". " + m);
    if (sd.shutdown_self) order.push((sd.machines.length + 1) + ". this host (last)");
    kv($("policy-info"), [
      ["Automatic shutdown", sd.enabled ? (sd.dry_run ? "enabled, DRY RUN" : "enabled, live") : "disabled"],
      ["Trigger", "on battery and ≤ " + sd.runtime_threshold_minutes + " min runtime, " + sd.consecutive_polls + " readings in a row"],
      ["Order", order.join("  →  ") || "none"],
    ]);
    setText("info-summary", [
      model,
      s ? s.status : null,
      sd.enabled ? (sd.dry_run ? "shutdown: dry run" : "shutdown: live") : "shutdown: disabled",
      "at ≤ " + sd.runtime_threshold_minutes + " min",
    ].filter(Boolean).join(" · "));
    renderBanners(d, flags, stale);
  }

  async function refreshCurrent() {
    try {
      renderCurrent(await getJson("/api/current"));
    } catch (e) {
      const box = $("banners");
      box.replaceChildren(banner("crit", "Cannot load data from the web service: " + e.message));
      setText("status-badge", "OFFLINE");
      $("status-badge").className = "badge badge-unknown";
    }
  }

  // ---------------------------------------------------------------- charts

  function colors() {
    const cs = getComputedStyle(document.documentElement);
    const v = (n) => cs.getPropertyValue(n).trim();
    return { text: v("--muted"), grid: v("--grid"), ob: v("--ob-shade"), c1: v("--c1"), c2: v("--c2"), c3: v("--c3"), c4: v("--c4") };
  }

  Chart.register({
    id: "obShading",
    beforeDatasetsDraw(chart) {
      const spans = chart.$ob;
      if (!spans || !spans.length) return;
      const area = chart.chartArea, x = chart.scales.x, ctx = chart.ctx;
      ctx.save();
      ctx.fillStyle = colors().ob;
      spans.forEach((s) => {
        const x0 = Math.max(area.left, x.getPixelForValue(s[0]));
        const x1 = Math.min(area.right, x.getPixelForValue(s[1]));
        if (x1 > x0) ctx.fillRect(x0, area.top, x1 - x0, area.height);
      });
      ctx.restore();
    },
  });

  function axis(title, c, position, extra) {
    return Object.assign({
      position: position || "left",
      title: { display: true, text: title, color: c.text },
      ticks: { color: c.text },
      grid: { color: c.grid, drawOnChartArea: position !== "right" },
    }, extra || {});
  }

  function makeChart(id, datasets, yAxes) {
    const c = colors();
    const scales = {
      x: {
        type: "linear",
        ticks: { color: c.text, maxTicksLimit: 12, callback: (v) => fmtTick(v) },
        grid: { color: c.grid },
        afterBuildTicks(scale) {
          const step = TICK_STEPS[range], off = tzOffset(scale.max);
          const ticks = [];
          for (let v = Math.ceil((scale.min + off) / step) * step - off; v <= scale.max; v += step) ticks.push({ value: v });
          if (ticks.length >= 2 && ticks.length <= 40) scale.ticks = ticks;
        },
      },
    };
    Object.keys(yAxes).forEach((k) => { scales[k] = yAxes[k](c); });
    charts[id] = new Chart($(id), {
      type: "line",
      data: { datasets: datasets.map((d) => Object.assign({ borderWidth: 1.6, pointRadius: 0, pointHoverRadius: 3, spanGaps: false, tension: 0, data: [], backgroundColor: d.borderColor }, d)) },
      options: {
        animation: false, parsing: false, normalized: true, maintainAspectRatio: false,
        interaction: { mode: "nearest", axis: "x", intersect: false },
        scales,
        plugins: {
          legend: { labels: { color: c.text } },
          tooltip: { callbacks: { title: (items) => (items.length ? fmtDateTime(items[0].parsed.x) : "") } },
        },
      },
    });
  }

  function buildCharts() {
    const c = colors();
    makeChart("c-battery",
      [{ label: "Charge %", yAxisID: "y", borderColor: c.c1 }, { label: "Runtime min", yAxisID: "y1", borderColor: c.c2 }],
      { y: (cc) => axis("%", cc, "left", { min: 0, max: 100 }), y1: (cc) => axis("min", cc, "right", { min: 0 }) });
    makeChart("c-voltage",
      [{ label: "Input V", borderColor: c.c1 }, { label: "Output V", borderColor: c.c3 }],
      { y: (cc) => axis("V", cc, "left", { suggestedMin: 105, suggestedMax: 130 }) });
    makeChart("c-load",
      [{ label: "Load %", yAxisID: "y", borderColor: c.c1 }, { label: "Real power W", yAxisID: "y1", borderColor: c.c2 }, { label: "Apparent VA", yAxisID: "y1", borderColor: c.c4 }],
      { y: (cc) => axis("%", cc, "left", { min: 0 }), y1: (cc) => axis("W / VA", cc, "right", { min: 0 }) });
    makeChart("c-temp",
      [{ label: "Temperature °C", borderColor: c.c2 }],
      { y: (cc) => axis("°C", cc, "left", { suggestedMin: 15, suggestedMax: 40 }) });
  }

  function applyChartTheme() {
    const c = colors();
    const palette = { "c-battery": [c.c1, c.c2], "c-voltage": [c.c1, c.c3], "c-load": [c.c1, c.c2, c.c4], "c-temp": [c.c2] };
    Object.keys(charts).forEach((id) => {
      const ch = charts[id];
      ch.data.datasets.forEach((ds, i) => { ds.borderColor = palette[id][i]; ds.backgroundColor = palette[id][i]; });
      Object.keys(ch.options.scales).forEach((k) => {
        const sc = ch.options.scales[k];
        sc.ticks.color = c.text;
        sc.grid.color = c.grid;
        if (sc.title) sc.title.color = c.text;
      });
      ch.options.plugins.legend.labels.color = c.text;
      ch.update("none");
    });
  }

  function points(t, ys, bucket) {
    const gap = Math.max(bucket * 2.5, pollInterval * 3);
    const out = [];
    for (let i = 0; i < t.length; i++) {
      if (i > 0 && t[i] - t[i - 1] > gap) out.push({ x: t[i - 1] + bucket, y: null });
      out.push({ x: t[i], y: ys[i] });
    }
    return out;
  }

  function onBatterySpans(t, ob, bucket) {
    const spans = [];
    let start = null;
    for (let i = 0; i < t.length; i++) {
      if (ob[i] && start === null) start = t[i];
      if (start !== null && (!ob[i] || i === t.length - 1)) {
        spans.push([start, ob[i] ? t[i] + bucket : t[i]]);
        start = null;
      }
    }
    return spans;
  }

  function setChart(id, d, seriesNames) {
    const ch = charts[id];
    ch.data.datasets.forEach((ds, i) => {
      ds.data = points(d.t, d[seriesNames[i]], d.bucket_s);
      ds.pointRadius = d.t.length < 150 ? 2 : 0; // sparse data would otherwise be invisible
    });
    ch.options.scales.x.min = d.start;
    ch.options.scales.x.max = d.end;
    ch.$ob = onBatterySpans(d.t, d.ob, d.bucket_s);
    ch.update("none");
  }

  async function refreshHistory() {
    try {
      const d = await getJson("/api/history?range=" + encodeURIComponent(range));
      lastBucket = d.bucket_s;
      setChart("c-battery", d, ["charge", "runtime_min"]);
      setChart("c-voltage", d, ["input_v", "output_v"]);
      setChart("c-load", d, ["load_pct", "realpower", "power_va"]);
      setChart("c-temp", d, ["temperature"]);
      setText("history-note", d.t.length
        ? "Last " + range + ", each point is the average over " + fmtDuration(d.bucket_s) + "."
        : "No data recorded in the last " + range + ".");
    } catch (e) {
      setText("history-note", "Could not load history: " + e.message);
    }
  }

  function markRange() {
    document.querySelectorAll("#ranges button").forEach((b) => {
      b.setAttribute("aria-pressed", b.dataset.range === range ? "true" : "false");
    });
  }

  // ---------------------------------------------------------------- events and shutdown runs

  function eventPages() { return Math.max(1, Math.ceil(eventTotal / eventSize)); }

  function renderPager() {
    const atFirst = eventPage === 0, atLast = eventPage >= eventPages() - 1;
    $("ev-first").disabled = atFirst;
    $("ev-prev").disabled = atFirst;
    $("ev-next").disabled = atLast;
    $("ev-last").disabled = atLast;
    setText("ev-page", "Page " + (eventPage + 1) + " of " + eventPages() + " · " + eventTotal + " events" +
      (atFirst ? "" : " · live updates paused"));
  }

  function gotoEventPage(page) {
    eventPage = Math.max(0, page);
    refreshEvents(true);
  }

  async function refreshEvents(force) {
    if (eventPage > 0 && !force) return; // keep an older page from shifting under the reader
    try {
      const d = await getJson("/api/events?limit=" + eventSize + "&offset=" + eventPage * eventSize);
      eventTotal = d.total;
      if (eventPage > eventPages() - 1) {
        eventPage = eventPages() - 1;
        return refreshEvents(true);
      }
      const body = $("events-body");
      body.replaceChildren();
      d.events.forEach((e) => {
        const tr = el("tr");
        tr.appendChild(el("td", "time", fmtDateTime(e.ts)));
        const lvl = el("td");
        lvl.appendChild(el("span", "sev sev-" + e.severity, e.severity));
        tr.appendChild(lvl);
        tr.appendChild(el("td", null, humanize(e.type)));
        tr.appendChild(el("td", null, e.message));
        body.appendChild(tr);
      });
      if (!d.events.length) {
        const tr = el("tr");
        const td = el("td", "sub", "No events recorded yet.");
        td.colSpan = 4;
        tr.appendChild(td);
        body.appendChild(tr);
      }
      renderPager();
    } catch (e) {
      /* keep the previous table; the status banner already reports connection problems */
    }
  }

  async function refreshRuns() {
    try {
      const d = await getJson("/api/shutdown-runs?limit=5");
      $("shutdown-panel").hidden = d.runs.length === 0;
      const box = $("shutdown-runs");
      box.replaceChildren();
      d.runs.forEach((r) => {
        const run = el("div", "run");
        run.appendChild(el("strong", null, fmtDateTime(r.started_ts) + " · " + r.state + (r.dry_run ? " · dry run" : "")));
        run.appendChild(el("div", "sub", r.reason));
        const ol = el("ol");
        r.steps.forEach((s) => {
          const li = el("li");
          li.appendChild(el("span", "state-" + s.state, s.machine + ": " + s.state));
          if (s.detail) li.appendChild(document.createTextNode(" – " + s.detail));
          ol.appendChild(li);
        });
        run.appendChild(ol);
        box.appendChild(run);
      });
    } catch (e) { /* ignore, see refreshEvents */ }
  }

  // ---------------------------------------------------------------- shutdown readiness

  const OVERALL = {
    ready: ["READY", "badge-ok"],
    partial: ["PARTIAL", "badge-warn"],
    attention: ["ATTENTION", "badge-crit"],
    unverified: ["NOT VERIFIED", "badge-unknown"],
  };
  const NO_PICKUP_MS = 180000;

  function stepCell(step) {
    const td = el("td");
    if (!step) {
      td.appendChild(el("span", "step step-na", "–"));
      return td;
    }
    const label = step.ok === true ? "✔ OK" : step.ok === false ? "✘ Failed" : "– n/a";
    const cls = step.ok === true ? "step-ok" : step.ok === false ? "step-bad" : "step-na";
    td.appendChild(el("span", "step " + cls, label));
    if (step.ok !== true && step.detail) td.appendChild(el("div", "note", step.detail));
    return td;
  }

  function renderReadiness(d) {
    readinessState = d.state;
    const panel = $("readiness-panel");
    panel.hidden = d.targets.length === 0;
    if (panel.hidden) {
      rdBanner = null;
      return;
    }
    const body = $("rd-body");
    body.replaceChildren();
    d.targets.forEach((t) => {
      const tr = el("tr");
      tr.appendChild(el("td", null, String(t.position)));
      const name = el("td");
      name.appendChild(el("div", null, t.name));
      if (t.remote_hostname) name.appendChild(el("div", "host", "reports “" + t.remote_hostname + "”"));
      tr.appendChild(name);
      const badge = OVERALL[t.overall] || OVERALL.unverified;
      const res = el("td");
      res.appendChild(el("span", "badge " + badge[1], badge[0]));
      tr.appendChild(res);
      ["ping", "ssh", "sudo", "dry"].forEach((k) => tr.appendChild(stepCell(t.steps[k])));
      const when = el("td", "time");
      if (t.checked_ts) {
        when.appendChild(el("div", null, fmtDateTime(t.checked_ts)));
        when.appendChild(el("div", "host", fmtDuration(d.now - t.checked_ts) + " ago"));
      } else {
        when.appendChild(el("span", "step step-na", "never"));
      }
      tr.appendChild(when);
      body.appendChild(tr);
    });

    const ready = d.targets.filter((t) => t.overall === "ready").length;
    const parts = [ready + " of " + d.targets.length + " fully ready"];
    if (d.checked_ts) parts.unshift("Last check " + fmtDateTime(d.checked_ts) + (d.trigger ? " (" + d.trigger + ")" : ""));
    parts.push(d.interval_hours > 0 ? "runs automatically every " + d.interval_hours + " h" : "automatic checks are off");
    setText("rd-summary", parts.join(" · "));

    const btn = $("rd-check");
    btn.hidden = !d.allow_check_now;
    btn.disabled = d.state !== "idle" || !d.monitor_alive;
    btn.textContent = d.state === "running" ? "Checking…" : d.state === "pending" ? "Waiting for monitor…" : "Check now";
    if (d.state === "pending") {
      if (pendingSince === null) pendingSince = Date.now();
    } else {
      pendingSince = null;
    }
    let status = "";
    if (!d.monitor_alive) status = "The monitor is not running.";
    else if (d.state === "pending" && Date.now() - pendingSince > NO_PICKUP_MS) status = "The monitor has not picked up the request.";
    else if (d.state === "idle" && d.note) status = d.note;
    setText("rd-status", status);

    const failed = d.targets.filter((t) => t.overall === "attention").map((t) => t.name);
    const unverified = d.targets.filter((t) => t.overall === "unverified").map((t) => t.name);
    rdBanner = null;
    if (d.shutdown_enabled && failed.length) {
      rdBanner = { level: "warn", text: "Shutdown readiness: the last check failed for " + failed.join(", ") + ". See Shutdown readiness below." };
    } else if (d.shutdown_enabled && unverified.length) {
      rdBanner = { level: "warn", text: "Shutdown readiness has not been verified recently for " + unverified.join(", ") + "." };
    }
    if (lastCurrent) renderCurrent(lastCurrent);
  }

  async function refreshReadiness() {
    try {
      renderReadiness(await getJson("/api/readiness"));
    } catch (e) { /* the status banner already reports connection problems */ }
  }

  async function requestCheck() {
    $("rd-check").disabled = true;
    setText("rd-status", "Requesting…");
    try {
      const resp = await fetch("/api/check-now", { method: "POST", headers: { "X-Requested-With": "ups-monitor-ui" }, cache: "no-store" });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok && resp.status !== 409) {
        setText("rd-status", body.error || "Request failed (HTTP " + resp.status + ")");
        return;
      }
    } catch (e) {
      setText("rd-status", "Request failed: " + e.message);
      return;
    } finally {
      await refreshReadiness();
    }
  }

  // ---------------------------------------------------------------- wiring

  function loop(fn, delayMs) {
    const tick = async () => {
      if (!document.hidden) await fn();
      setTimeout(tick, typeof delayMs === "function" ? delayMs() : delayMs);
    };
    setTimeout(tick, typeof delayMs === "function" ? delayMs() : delayMs);
  }

  function setTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    writeStore("theme", theme);
    $("theme-toggle").textContent = theme === "dark" ? "Light mode" : "Dark mode";
    applyChartTheme();
  }

  async function start() {
    const info = $("info-panel");
    info.open = readStore("infoOpen") === "1"; // collapsed unless the user opened it before
    info.addEventListener("toggle", () => writeStore("infoOpen", info.open ? "1" : "0"));
    $("theme-toggle").textContent = document.documentElement.getAttribute("data-theme") === "dark" ? "Light mode" : "Dark mode";
    $("theme-toggle").addEventListener("click", () => {
      setTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark");
    });
    document.querySelectorAll("#ranges button").forEach((b) => {
      b.addEventListener("click", () => { range = b.dataset.range; writeStore("range", range); markRange(); refreshHistory(); });
    });
    $("ev-first").addEventListener("click", () => gotoEventPage(0));
    $("ev-prev").addEventListener("click", () => gotoEventPage(eventPage - 1));
    $("ev-next").addEventListener("click", () => gotoEventPage(eventPage + 1));
    $("ev-last").addEventListener("click", () => gotoEventPage(eventPages() - 1));
    $("ev-size").value = String(eventSize);
    $("ev-size").addEventListener("change", () => {
      eventSize = parseInt($("ev-size").value, 10);
      writeStore("eventSize", String(eventSize));
      gotoEventPage(0);
    });
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) { refreshCurrent(); refreshHistory(); refreshEvents(); refreshRuns(); refreshReadiness(); }
    });

    markRange();
    buildCharts();
    await refreshCurrent();
    refreshHistory();
    refreshEvents();
    refreshRuns();
    refreshReadiness();
    $("rd-check").addEventListener("click", requestCheck);

    loop(refreshCurrent, () => Math.max(pollInterval, 2) * 1000);
    loop(refreshHistory, () => (range === "1h" || range === "6h" ? 30000 : 120000));
    loop(() => Promise.all([refreshEvents(), refreshRuns()]), 15000);
    loop(refreshReadiness, () => (readinessState === "idle" ? 30000 : 2000));
  }

  start();
})();
