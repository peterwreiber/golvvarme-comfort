"use strict";
const $ = (id) => document.getElementById(id);
const DAYS = ["Mån", "Tis", "Ons", "Tor", "Fre", "Lör", "Sön"];
const NUMBERS = ["comfort_temp", "economy_temp", "setback_temp", "comfort_reduction", "expensive_percent", "expensive_min_price",
  "expensive_price", "cheap_boost_below", "heatup_rate", "cooldown_rate", "max_preheat_hours", "holding_share", "power_kw", "fallback_temp"];
const SELECTS = ["expensive_mode", "climate_entity", "price_entity", "conflict_automation"];
let settings = null;
let dirty = false;

async function api(path, body) {
  const options = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const response = await fetch(path, options);
  const value = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(value.error || `Fel ${response.status}`);
  return value;
}

const temp = (v) => (v === null || v === undefined ? "–" : `${Number(v).toFixed(1).replace(".", ",")} °C`);
const kr = (v) => (v === null || v === undefined ? "–" : `${Number(v).toFixed(2).replace(".", ",")} kr`);
const clock = (t) => new Date(t * 1000).toLocaleTimeString("sv-SE", { hour: "2-digit", minute: "2-digit" });
function when(t) {
  const d = new Date(t * 1000), today = new Date();
  const tomorrow = new Date(today); tomorrow.setDate(today.getDate() + 1);
  const day = d.toDateString() === today.toDateString() ? "" : d.toDateString() === tomorrow.toDateString() ? "i morgon " :
    d.toLocaleDateString("sv-SE", { weekday: "short" }) + " ";
  return day + clock(t);
}

function notice(text, error = false) {
  const el = $("notice");
  el.hidden = !text;
  el.textContent = text || "";
  el.classList.toggle("error", error);
}

function renderStatus(s) {
  const th = s.thermostat || {};
  const away = s.vacation && s.vacation.active;
  $("mode").textContent = away ? "Semester" : s.enabled ? "Styr termostaten" : "Bara plan";
  $("mode").classList.toggle("vacation-on", !!away);
  $("mode").classList.toggle("active", !!s.enabled);
  $("status-title").textContent = s.reason || (s.plan ? "–" : "Ingen plan");
  $("status-message").textContent = [s.message, s.error, s.price_error].filter(Boolean).join(" · ");
  $("now-current").textContent = temp(th.current);
  const setting = th.mode === "auto" ? "eget program" : th.target != null ? `inställd på ${temp(th.target)}` : th.mode || "";
  $("now-action").textContent = `${th.action === "heating" ? "Värmer" : "Vilar"} · ${setting}`;
  $("now-target").textContent = temp(s.target);
  $("now-reason").textContent = s.reason || "";
  $("now-price").textContent = s.price == null ? "–" : kr(s.price);
  $("now-level").textContent = [s.expensive ? "Dyr el" : s.price == null ? "" : "Normal", s.price_source].filter(Boolean).join(" · ");
  if (th.fallback) $("enabled-hint").textContent = `Av = bara visa plan. Termostaten kör då ${th.fallback}, och det gäller också om tillägget stoppas eller elpriset saknas.`;
  if (s.price_source) {
    $("price-hint").textContent = s.price_source.startsWith("Nord Pool (spot")
      ? "Nord Pool är spotpris utan skatt och nät – sätt gränsen lägre än för Tibber, t.ex. 0,5."
      : `Gäller priset från ${s.price_source}.`;
  }
  $("next-target").textContent = s.next_change ? temp(s.next_change.target) : "–";
  $("next-when").textContent = s.next_change ? `${when(s.next_change.t)} · ${s.next_change.reason}` : "Ingen ändring i planen";
  $("conflict").hidden = !(s.conflict && s.enabled);
  const lr = s.learned_rates;
  const rate = (v) => (v == null ? "för lite underlag" : `${String(v).replace(".", ",")} °C/h`);
  $("learned-rates").textContent = lr
    ? `Inlärt ur termostatens historik: uppvärmning ${rate(lr.heatup_rate)} (${lr.heat_runs} gånger), avsvalning ${rate(lr.cooldown_rate)} (${lr.cool_runs} gånger).${lr.in_use ? " Används i planen där de är kända." : " Används inte – värdena ovan gäller."}`
    : "Mäts ur termostatens historik (värmer/vilar) de senaste 10 dygnen. Värdena ovan gäller tills det finns minst tre uppvärmningar och tre avsvalningar.";
  if (s.conflict) $("conflict-text").textContent = `”${s.conflict.name}” ändrar också termostaten efter elpriset. Stäng av den så att de inte krockar.`;
  if (s.version) $("version").textContent = `Golvvärme Comfort ${s.version}`;
  renderChanges(s.plan);
}

function renderChanges(plan) {
  const list = $("changes");
  list.replaceChildren();
  if (!plan) return;
  let prev = null;
  for (const row of plan.slots) {
    if (prev && row.target === prev.target && row.reason === prev.reason) continue;
    if (prev) {
      const li = document.createElement("li");
      li.className = row.reason.startsWith("Förvärm") ? "preheat" : row.comfort ? "comfort" : row.target < prev.target ? "low" : "";
      li.textContent = `${when(row.t)} → ${temp(row.target)} · ${row.reason}`;
      list.append(li);
      if (list.children.length >= 8) break;
    }
    prev = row;
  }
  if (!list.children.length) list.innerHTML = "<li>Inga ändringar</li>";
}

function svg(tag, attrs, parent) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (parent) parent.append(el);
  return el;
}

function renderChart(status, history) {
  const box = $("chart");
  box.replaceChildren();
  const plan = status.plan;
  const now = status.now || Date.now() / 1000;
  const start = now - 12 * 3600;
  const end = plan && plan.slots.length ? plan.slots[plan.slots.length - 1].t + 900 : now + 24 * 3600;
  const W = Math.max(360, Math.min(1000, box.clientWidth || 1000)), H = W < 600 ? 300 : 330, L = 34, R = 44, T = 14, B = 34, priceH = 70;
  const tempBottom = H - B - priceH - 10;
  const points = (history && history.points) || [];
  const temps = [];
  points.forEach((p) => { if (p.current != null) temps.push(p.current); if (p.target != null) temps.push(p.target); });
  (plan ? plan.slots : []).forEach((r) => temps.push(r.target));
  let lo = Math.floor(Math.min(...temps, 16) - 0.5), hi = Math.ceil(Math.max(...temps, 22) + 0.5);
  const x = (t) => L + ((t - start) / (end - start)) * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (tempBottom - T);
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" }, box);

  for (let v = lo; v <= hi; v += hi - lo > 8 ? 2 : 1) {
    svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: "grid-line" }, root);
    svg("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" }, root).textContent = `${v}°`;
  }
  if (plan) {
    for (const [a, b] of plan.windows) {
      if (b < start || a > end) continue;
      svg("rect", { x: x(Math.max(a, start)), y: T, width: x(Math.min(b, end)) - x(Math.max(a, start)), height: tempBottom - T, class: "band-comfort" }, root);
    }
    for (const p of plan.preheats) {
      svg("rect", { x: x(Math.max(p.start, start)), y: T, width: x(p.comfort_start) - x(Math.max(p.start, start)), height: tempBottom - T, class: "band-preheat" }, root);
    }
  }
  // Price bars: history prices before now, plan prices after.
  const prices = [];
  ((history && history.prices) || []).forEach(([t, v]) => { if (t >= start && t < now - (now % 900)) prices.push({ t, price: v, expensive: false }); });
  (plan ? plan.slots : []).forEach((r) => { if (r.price != null) prices.push({ t: r.t, price: r.price, expensive: r.expensive }); });
  if (prices.length) {
    const max = Math.max(...prices.map((p) => p.price), 0.01);
    const base = H - B, bw = Math.max(1, x(start + 900) - x(start) - 0.6);
    for (const p of prices) {
      const h = (Math.max(p.price, 0) / max) * priceH;
      svg("rect", { x: x(p.t), y: base - h, width: bw, height: h, class: `bar${p.expensive ? " expensive" : ""}` }, root);
    }
    svg("text", { x: W - R + 6, y: base - priceH + 10 }, root).textContent = `${max.toFixed(1)} kr`;
    svg("text", { x: W - R + 6, y: base }, root).textContent = "0";
  }
  const stepPath = (rows, key) => {
    let d = "", last = null;
    for (const r of rows) {
      if (r[key] == null) { last = null; continue; }
      const px = x(Math.max(r.t, start)), py = y(r[key]);
      d += last === null ? `M${px},${py}` : `H${px}V${py}`;
      last = py;
    }
    return d;
  };
  const past = points.filter((p) => p.t >= start - 86400).map((p) => ({ ...p, t: Math.max(p.t, start) }));
  if (past.length) {
    const tail = { ...past[past.length - 1], t: now };
    const linePath = (key) => {
      let d = "";
      [...past, tail].forEach((p, i) => { if (p[key] != null) d += `${d ? "L" : "M"}${x(p.t)},${y(p[key])}`; });
      return d;
    };
    svg("path", { d: stepPath([...past.map((p) => ({ ...p, target: p.mode === "auto" || p.mode === "heat" ? p.target : null })), tail], "target") + `H${x(now)}`, class: "line series-1" }, root);
    svg("path", { d: linePath("current"), class: "line series-0" }, root);
  }
  if (plan && plan.slots.length) {
    const rows = plan.slots.map((r) => ({ t: Math.max(r.t, now), target: r.target }));
    svg("path", { d: stepPath(rows, "target") + `H${x(end)}`, class: "line dashed series-2" }, root);
  }
  svg("line", { x1: x(now), x2: x(now), y1: T, y2: H - B, class: "now-line" }, root);
  svg("text", { x: x(now) + 4, y: T + 12 }, root).textContent = "nu";
  const tick = W < 600 ? 21600 : 10800;
  const zone = new Date(start * 1000).getTimezoneOffset() * 60;  // align ticks to local midnight
  const first = Math.ceil((start - zone) / tick) * tick + zone;
  for (let t = first; t < end; t += tick) {
    const label = svg("text", { x: x(t), y: H - 12, "text-anchor": "middle" }, root);
    const d = new Date(t * 1000);
    label.textContent = d.getHours() === 0 ? d.toLocaleDateString("sv-SE", { weekday: "short" }) : clock(t);
  }
  $("chart-note").textContent = plan ? `Planen räcker till ${when(end)}` : "";
}

function renderHistory(h) {
  const show = (s, hours, cost) => {
    $(hours).textContent = s ? `${s.hours.toFixed(1).replace(".", ",")} h` : "–";
    $(cost).textContent = s ? `≈ ${s.kwh.toFixed(1).replace(".", ",")} kWh · ${kr(s.cost)}` : "";
  };
  show(h.today, "today-hours", "today-cost");
  show(h.last24, "day-hours", "day-cost");
  // Same kWh at the day's average price minus what they cost: a gain from timing, not from using less.
  const shift = h.last24 && h.last24.price_shift;
  $("shift-label").textContent = shift && shift.difference < 0 ? "Dyrare än snittpris · 24 h" : "Tjänat på prisflytt · 24 h";
  $("shift-value").textContent = shift ? kr(Math.abs(shift.difference)) : "–";
  $("shift-detail").textContent = shift
    ? `Snitt ${kr(shift.average_price)}/kWh, golvet ${kr(shift.actual_price)}/kWh${shift.complete ? "" : " · priser saknas för en del"}`
    : "Kräver elpriser för perioden";
}

const pad = (n) => String(n).padStart(2, "0");

// 24-hour time as two dropdowns (hour, minute). Native time inputs follow the phone's 12/24 h setting
// and their picker is unreliable inside the Home Assistant app.
function timePicker(cls, label, value, allowMidnightEnd) {
  const [hour, minute] = String(value || "00:00").split(":").map(Number);
  const wrap = document.createElement("span");
  wrap.className = `time ${cls}`;
  const hours = document.createElement("select");
  hours.className = "t-hour";
  hours.setAttribute("aria-label", `${label}, timme`);
  for (let h = 0; h <= (allowMidnightEnd ? 24 : 23); h++) hours.append(new Option(pad(h), h));
  const minutes = document.createElement("select");
  minutes.className = "t-minute";
  minutes.setAttribute("aria-label", `${label}, minut`);
  const steps = [0, 15, 30, 45];
  if (!steps.includes(minute)) steps.push(minute);
  steps.sort((a, b) => a - b).forEach((m) => minutes.append(new Option(pad(m), m)));
  hours.value = String(hour);
  minutes.value = String(minute);
  const sync = () => { minutes.disabled = hours.value === "24"; if (minutes.disabled) minutes.value = "0"; };
  hours.addEventListener("change", sync);
  sync();
  const colon = document.createElement("b");
  colon.textContent = ":";
  wrap.append(hours, colon, minutes);
  return wrap;
}

const timeValue = (wrap) => `${pad(wrap.querySelector(".t-hour").value)}:${pad(wrap.querySelector(".t-minute").value)}`;

function periodRow(p) {
  const row = document.createElement("div");
  row.className = "period";
  const days = document.createElement("div");
  days.className = "days";
  DAYS.forEach((name, i) => {
    const label = document.createElement("label");
    label.innerHTML = `<input type="checkbox" value="${i}">${name}`;
    label.querySelector("input").checked = p.days.includes(i);
    days.append(label);
  });
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "link";
  remove.setAttribute("aria-label", "Ta bort");
  remove.textContent = "✕";
  remove.onclick = () => { row.remove(); dirty = true; $("save-status").textContent = "Osparade ändringar"; };
  row.append(timePicker("p-start", "Från", p.start), timePicker("p-end", "Till", p.end, true), days, remove);
  return row;
}

function fillSelect(id, items, value, emptyLabel) {
  const select = $(id);
  select.replaceChildren();
  if (emptyLabel) select.append(new Option(emptyLabel, ""));
  if (value && !items.some((e) => e.id === value)) items = [{ id: value, name: value }, ...items];
  items.forEach((e) => select.append(new Option(`${e.name} (${e.id})`, e.id)));
  select.value = value;
}

function renderSettings(s, entities) {
  settings = s;
  $("learn_rates").checked = s.learn_rates;
  $("enabled").checked = s.enabled;
  NUMBERS.forEach((k) => { $(k).value = s[k]; });
  $("expensive_mode").value = s.expensive_mode;
  fillSelect("climate_entity", entities.climate || [], s.climate_entity, s.climate_entity ? "" : "Välj termostat…");
  fillSelect("price_entity", entities.price || [], s.price_entity, s.price_entity ? "" : "Välj elprisgivare…");
  fillSelect("conflict_automation", entities.automation || [], s.conflict_automation, "Ingen");
  $("periods").replaceChildren(...s.comfort_periods.map(periodRow));
  renderVacation(s.vacation);
  modeFields();
  dirty = false;
}

function modeFields() {
  const mode = $("expensive_mode").value;
  document.querySelectorAll("[data-mode]").forEach((el) => { el.hidden = el.dataset.mode !== mode; });
}

function collect() {
  const value = { enabled: $("enabled").checked, learn_rates: $("learn_rates").checked };
  NUMBERS.forEach((k) => { value[k] = Number($(k).value); });
  SELECTS.forEach((k) => { value[k] = $(k).value; });
  value.comfort_periods = [...document.querySelectorAll(".period")].map((row) => ({
    start: timeValue(row.querySelector(".p-start")),
    end: timeValue(row.querySelector(".p-end")),
    days: [...row.querySelectorAll(".days input:checked")].map((c) => Number(c.value)),
  }));
  value.vacation = {
    enabled: $("vacation-enabled").checked,
    start: whenValue($("vacation-start")),
    end: whenValue($("vacation-end")),
    temp: Number($("vacation-temp").value),
  };
  return value;
}

// Date + hour as dropdowns (local time, 24 h). Value format: YYYY-MM-DDTHH:MM.
const isoDate = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

function whenPicker(box, value, fallbackDays, fallbackHour) {
  const now = new Date();
  let date = value ? value.slice(0, 10) : "";
  const hour = value ? Number(value.slice(11, 13)) : fallbackHour;
  if (!date) {
    const d = new Date(now);
    d.setDate(d.getDate() + fallbackDays);
    date = isoDate(d);
  }
  const dates = document.createElement("select");
  dates.className = "w-date";
  dates.setAttribute("aria-label", "Datum");
  const first = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  for (let i = 0; i < 180; i++) {
    const d = new Date(first);
    d.setDate(first.getDate() + i);
    const label = d.toLocaleDateString("sv-SE", { weekday: "short", day: "numeric", month: "short",
      year: d.getFullYear() === now.getFullYear() ? undefined : "numeric" });
    dates.append(new Option(i === 0 ? `i dag (${label})` : i === 1 ? `i morgon (${label})` : label, isoDate(d)));
  }
  if (![...dates.options].some((o) => o.value === date)) dates.prepend(new Option(date, date));
  dates.value = date;
  const hours = document.createElement("select");
  hours.setAttribute("aria-label", "Klockslag");
  for (let h = 0; h < 24; h++) hours.append(new Option(`${pad(h)}:00`, h));
  hours.value = String(hour);
  box.replaceChildren(dates, hours);
}

const whenValue = (box) => {
  const [dates, hours] = box.querySelectorAll("select");
  return `${dates.value}T${pad(hours.value)}:00`;
};

function renderVacation(v) {
  v = v || { enabled: false, start: "", end: "", temp: 15 };
  $("vacation-enabled").checked = v.enabled;
  $("vacation-temp").value = v.temp;
  whenPicker($("vacation-start"), v.start, 0, 12);
  whenPicker($("vacation-end"), v.end, 7, 16);
  $("vacation-fields").classList.toggle("off", !v.enabled);
}

async function refresh() {
  try {
    const status = await api("api/status");
    renderStatus(status);
    let history = null;
    try { history = await api("api/history"); renderHistory(history); } catch (e) { /* chart without history */ }
    renderChart(status, history);
    const events = await api("api/events");
    $("events").replaceChildren(...events.map((e) => {
      const li = document.createElement("li");
      li.className = e.kind;
      li.innerHTML = "<small></small><span></span>";
      li.firstChild.textContent = new Date(e.t * 1000).toLocaleString("sv-SE", { dateStyle: "short", timeStyle: "short" });
      li.lastChild.textContent = e.message;
      return li;
    }));
    if (!events.length) $("events").innerHTML = "<li>Inga händelser ännu.</li>";
    if (!document.querySelector("#notice.error.save")) notice("");
  } catch (e) {
    notice(`Kunde inte hämta status: ${e.message}`, true);
  }
}

async function init() {
  try {
    const [s, entities] = await Promise.all([api("api/settings"), api("api/entities").catch(() => ({}))]);
    renderSettings(s, entities);
  } catch (e) {
    notice(`Kunde inte läsa inställningar: ${e.message}`, true);
  }
  $("settings").addEventListener("input", () => { dirty = true; $("save-status").textContent = "Osparade ändringar"; });
  $("expensive_mode").addEventListener("change", modeFields);
  $("vacation-enabled").addEventListener("change", () => $("vacation-fields").classList.toggle("off", !$("vacation-enabled").checked));
  $("add-period").onclick = () => { $("periods").append(periodRow({ start: "06:00", end: "08:00", days: [0, 1, 2, 3, 4, 5, 6] })); dirty = true; $("save-status").textContent = "Osparade ändringar"; };
  $("settings").addEventListener("submit", async (event) => {
    event.preventDefault();
    $("save-status").textContent = "Sparar…";
    try {
      const saved = await api("api/settings", collect());
      settings = saved;
      dirty = false;
      $("save-status").textContent = "Sparat";
      setTimeout(refresh, 2500);
    } catch (e) {
      $("save-status").textContent = e.message;
    }
  });
  $("conflict-off").onclick = async () => {
    try { await api("api/disable-conflict", {}); notice("Automationen är avstängd."); refresh(); } catch (e) { notice(e.message, true); }
  };
  refresh();
  setInterval(refresh, 60000);
}

init();
