"use strict";

const KIND_LABELS = {
  revenue: "Recettes", fuel: "Énergie", maintenance: "Entretien courant", wages: "Salaires", depot: "Dépôt",
  repair: "Pannes", revision: "Révisions", overhaul: "Révisions générales", refurbish: "Remises à neuf intérieur",
  ct: "Contrôle technique", purchase: "Achats de bus", capital: "Capital", option: "Options",
  infrastructure: "Infrastructures",
};
const DAY_TYPES = { all: "Tous les jours", weekday: "Semaine", saturday: "Samedi", sunday: "Dimanche", weekend: "Week-end" };
const DAY_LABEL = { weekday: "Semaine", saturday: "Samedi", sunday: "Dimanche" };
const BANDS = { creuse: "Creuse", pointe: "Pointe", soir: "Soir" };
const ENERGY = { diesel: "Diesel", gnv: "GNV", hybride: "Hybride", electrique: "Électrique" };
const BODY = { standard: "Standard", articule: "Articulé", midi: "Midi" };
const UNIT = { diesel: "L", gnv: "kg", electrique: "kWh", hybride: "L" };
const WEEKDAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"];

const S = {
  state: null, models: [], vehicles: [], drivers: [], lines: [], infra: null, report: null, events: [],
  tab: "dashboard", filters: { status: "", body: "", energy: "" }, buyModel: "", user: null,
};

const esc = (x) => String(x ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const eur = (n) => new Intl.NumberFormat("fr-FR", { style: "currency", currency: "EUR", maximumFractionDigits: 0 }).format(n);
const eur2 = (n) => new Intl.NumberFormat("fr-FR", { style: "currency", currency: "EUR", minimumFractionDigits: 2 }).format(n);
const num = (n) => new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 0 }).format(n);
const num1 = (n) => new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 1 }).format(n);
const signed = (n) => `<span class="${n >= 0 ? "good" : "bad"}">${n >= 0 ? "+" : ""}${eur(n)}</span>`;
const frDate = (iso) => {
  const d = new Date(iso + "T00:00:00");
  return `${WEEKDAYS[(d.getDay() + 6) % 7]} ${d.toLocaleDateString("fr-FR")}`;
};

let toastTimer;
function toast(msg, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = isError ? "error" : "";
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), isError ? 5000 : 2500);
}

class Unauthorized extends Error {}

async function api(method, path, body) {
  const res = await fetch("/api" + path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && !path.startsWith("/login")) throw new Unauthorized();
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join(", ") : data.detail;
    throw new Error(detail || `Erreur ${res.status}`);
  }
  return data;
}

async function act(fn, okMsg) {
  try {
    await fn();
    if (okMsg) toast(okMsg);
  } catch (e) {
    if (e instanceof Unauthorized) return showAuth();
    toast(e.message, true);
  }
  await refresh();
}

async function refresh() {
  try {
    [S.state, S.models, S.vehicles, S.drivers, S.lines, S.infra, S.report, S.events] = await Promise.all([
      api("GET", "/state"), api("GET", "/models"), api("GET", "/vehicles"), api("GET", "/drivers"),
      api("GET", "/lines"), api("GET", "/infra"), api("GET", "/report"), api("GET", "/events?limit=12"),
    ]);
  } catch (e) {
    if (e instanceof Unauthorized) return showAuth();
    return toast(e.message, true);
  }
  render();
}

// ---------- Connexion ----------

function showAuth() {
  S.user = null;
  document.getElementById("login-form").hidden = false;
  document.getElementById("register-form").hidden = true;
  document.querySelectorAll("#auth-switch button").forEach((x) => x.classList.toggle("active", x.dataset.form === "login"));
  document.getElementById("app").hidden = true;
  document.getElementById("auth").hidden = false;
}

async function enterGame(username) {
  S.user = username;
  document.getElementById("auth").hidden = true;
  document.getElementById("app").hidden = false;
  document.getElementById("user-name").textContent = username;
  await refresh();
}

function authFormData(form) {
  const f = new FormData(form);
  return { username: f.get("username").trim(), password: f.get("password"), company_name: (f.get("company_name") || "").trim() };
}

document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const r = await api("POST", "/login", authFormData(e.target));
    e.target.reset();
    await enterGame(r.username);
  } catch (err) { toast(err.message, true); }
});

document.getElementById("register-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const r = await api("POST", "/register", authFormData(e.target));
    e.target.reset();
    await enterGame(r.username);
  } catch (err) { toast(err.message, true); }
});

document.getElementById("auth-switch").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-form]");
  if (!b) return;
  document.getElementById("login-form").hidden = b.dataset.form !== "login";
  document.getElementById("register-form").hidden = b.dataset.form !== "register";
  document.querySelectorAll("#auth-switch button").forEach((x) => x.classList.toggle("active", x === b));
});

document.getElementById("logout").addEventListener("click", async () => {
  await api("POST", "/logout").catch(() => {});
  showAuth();
});

// ---------- Rendu ----------

function bar(value) {
  const color = value >= 60 ? "var(--good)" : value >= 30 ? "var(--warn)" : "var(--bad)";
  return `<span class="bar" title="${value.toFixed(0)} / 100"><i style="width:${Math.max(2, value)}%;background:${color}"></i></span> ${value.toFixed(0)}`;
}

function dueLabel(days, warnAt) {
  if (days < 0) return `<span class="bad">dépassée de ${-days} j</span>`;
  return `<span class="${days <= warnAt ? "warn" : ""}">dans ${days} j</span>`;
}

function render() {
  const st = S.state;
  document.getElementById("company-name").textContent = st.name;
  document.getElementById("top-date").textContent = `Jour ${st.game_day} · ${frDate(st.date)}`;
  document.getElementById("top-cash").textContent = eur(st.cash);
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === S.tab));
  document.querySelectorAll(".tab").forEach((t) => (t.hidden = t.id !== "tab-" + S.tab));
  ({
    dashboard: renderDashboard, fleet: renderFleet, drivers: renderDrivers, lines: renderLines,
    depot: renderDepot, workshop: renderWorkshop, report: renderReport,
  })[S.tab]();
}

// Bus affectés au minimum, par tranche horaire, pour un type de jour
function bandCoverage(line, t) {
  const out = {};
  for (const [h, band] of Object.entries(line.bands[t])) {
    const assigned = line.buses_assigned[t][h] || 0;
    out[band] = Math.min(out[band] ?? Infinity, assigned);
  }
  return out;
}

function alerts() {
  const out = [];
  for (const v of S.vehicles) {
    if (v.ct_in_days < 0) out.push(["crit", `${v.plate} : contrôle technique expiré, le bus ne roule plus.`]);
    else if (v.ct_in_days <= 14) out.push(["warn", `${v.plate} : contrôle technique dans ${v.ct_in_days} j.`]);
    if (v.revision_in_days < 0) out.push(["warn", `${v.plate} : révision en retard (risque de panne accru).`]);
    else if (v.revision_in_days <= 7) out.push(["warn", `${v.plate} : révision dans ${v.revision_in_days} j.`]);
    if (v.engine_state < 35) out.push(["warn", `${v.plate} : moteur usé (${v.engine_state.toFixed(0)}), prévoir une révision générale.`]);
    if (v.interior_state < 25) out.push(["warn", `${v.plate} : intérieur très dégradé (${v.interior_state.toFixed(0)}), la réputation en souffre.`]);
    if (v.status === "workshop") out.push(["warn", `${v.plate} est à l'atelier jusqu'au jour ${v.available_from_day}.`]);
    if (!v.options.includes("valideur")) out.push(["warn", `${v.plate} n'a pas de valideur : ${num(S.state.rules.fraud_rate * 100)} % de ses recettes se perdent en fraude.`]);
  }
  for (const d of S.infra.depots) {
    for (const i of d.infra) {
      if (i.energy === "electrique") continue;
      if (i.capacity > 0 && i.stock / i.capacity < 0.15) out.push(["warn", `Citerne ${ENERGY[i.energy]} presque vide : le carburant coûtera plus cher en station.`]);
    }
    const electric = S.vehicles.filter((v) => v.energy === "electrique").length;
    const chargers = (d.infra.find((i) => i.energy === "electrique") || { capacity: 0 }).capacity;
    if (electric > chargers * S.state.rules.buses_per_charger) out.push(["crit", `${electric} bus électriques mais ${chargers} borne(s) (${S.state.rules.buses_per_charger} bus par borne) : certains ne rouleront pas.`]);
  }
  for (const line of S.lines) {
    if (!line.services.length) out.push(["warn", `${line.code} n'a aucun service.`]);
    for (const s of line.services) {
      if (!s.vehicle_id || !s.driver_id) {
        out.push(["warn", `${line.code} ${s.start_hour}h–${s.end_hour}h (${DAY_TYPES[s.day_type]}) : ${!s.vehicle_id ? "pas de bus" : "pas de conducteur"} affecté, le service ne roule pas.`]);
      }
    }
    const t = S.state.day_type;
    const cov = bandCoverage(line, t);
    const missing = Object.entries(line.buses_needed[t]).filter(([b, n]) => n > (cov[b] ?? 0));
    if (missing.length) out.push(["warn", `Ligne ${line.code} : fréquence cible non tenue (${DAY_LABEL[t].toLowerCase()}) — ${missing.map(([b, n]) => `${BANDS[b].toLowerCase()} ${cov[b] ?? 0}/${n} bus`).join(", ")}.`]);
  }
  return out;
}

function renderDashboard() {
  const st = S.state;
  const al = alerts();
  const depots = S.infra.depots.map((d) => `${esc(d.name)} : ${d.used}/${d.capacity} places`).join(" · ");
  const last = st.last_day;
  document.getElementById("tab-dashboard").innerHTML = `
    <div class="cards">
      <div class="card"><div class="label">Trésorerie</div><div class="value">${eur(st.cash)}</div><div class="sub">${depots}</div></div>
      <div class="card"><div class="label">Prochain jour à simuler</div><div class="value">Jour ${st.game_day}</div><div class="sub">${frDate(st.date)} · ${DAY_LABEL[st.day_type].toLowerCase()}</div></div>
      <div class="card"><div class="label">Réputation</div><div class="value">${st.reputation.toFixed(0)} / 100</div><div class="sub">demande ×${(0.7 + 0.6 * st.reputation / 100).toFixed(2)}</div></div>
      <div class="card"><div class="label">Dernier jour (${last ? "jour " + last.day : "—"})</div><div class="value">${last ? signed(last.profit) : "—"}</div><div class="sub">${S.vehicles.length} bus · ${S.drivers.length} conducteurs</div></div>
    </div>
    ${st.dev ? `<div class="row" style="margin-top:14px"><button class="btn primary" data-act="tick" data-days="1">Passer au jour suivant</button>
      <button class="btn" data-act="tick" data-days="7">+ 7 jours</button>
      <span class="hint">Mode développement : en production, le calcul est lancé chaque nuit par un script planifié.</span></div>` : ""}
    <h2>À surveiller</h2>
    ${al.length ? `<ul class="alerts">${al.map(([c, m]) => `<li class="${c}">${esc(m)}</li>`).join("")}</ul>` : `<p class="muted">Rien à signaler.</p>`}
    <h2>Derniers événements</h2>
    ${S.events.length ? `<ul class="events">${S.events.map((e) => `<li><span class="muted">J${e.day}</span> ${esc(e.message)}</li>`).join("")}</ul>` : `<p class="muted">Aucun événement pour l'instant.</p>`}`;
}

function modelLabel(m) {
  const range = m.range_km ? `, ${num(m.range_km)} km d'autonomie` : "";
  const used = m.status === "occasion" ? " (occasion)" : "";
  return `${m.name}${used} — ${BODY[m.body]}, ${m.capacity} places${range} — ${eur(m.price)}`;
}

function filteredModels() {
  const f = S.filters;
  return S.models.filter((m) => (!f.status || m.status === f.status) && (!f.body || m.body === f.body) && (!f.energy || m.energy === f.energy));
}

function modelDetail(m) {
  if (!m) return "";
  const unit = UNIT[m.energy];
  const used = m.status === "occasion"
    ? `<div>Occasion : ${num(m.initial_km)} km, moteur ${num(m.initial_engine_state)}, intérieur ${num(m.initial_interior_state)}, CT dans ${m.initial_ct_days} j, révision dans ${m.initial_revision_days} j.</div>` : "";
  return `<div class="panel" style="margin-top:8px">
    <div><b>${esc(m.name)}</b> · ${ENERGY[m.energy]} · ${BODY[m.body]} · ${m.capacity} places · ${eur(m.price)}</div>
    <div class="muted">Consommation ${num1(m.consumption_per_100km)} ${unit}/100 km${m.range_km ? ` · autonomie ${num(m.range_km)} km par jour (recharge la nuit)` : ""} · entretien ${eur2(m.maintenance_cost_per_km)}/km · fiabilité ${(m.reliability * 100).toFixed(1)} %</div>
    ${used}</div>`;
}

function renderFleet() {
  const rows = S.vehicles.map((v) => `
    <tr>
      <td><b>${esc(v.plate)}</b></td>
      <td>${esc(v.model_name)}<div class="muted" style="font-size:12px">${BODY[v.body]} · ${v.capacity} places${v.range_km ? ` · ${num(v.range_km)} km/j` : ""}</div></td>
      <td class="num">${num(v.km)} km</td>
      <td>${bar(v.engine_state)}</td>
      <td>${bar(v.interior_state)}</td>
      <td>${dueLabel(v.revision_in_days, 7)}</td>
      <td>${dueLabel(v.ct_in_days, 14)}</td>
      <td>${v.options.includes("valideur") ? `<span class="tag ok">valideur</span>` : `<span class="tag workshop">sans valideur</span>`}</td>
      <td>${v.status === "workshop" ? `<span class="tag workshop">atelier → J${v.available_from_day}</span>` : `<span class="tag ok">en service</span>`}<div class="muted" style="font-size:12px">${v.services} service(s)</div></td>
      <td><div class="row">
        <button class="btn small" data-act="vehicle" data-id="${v.id}" data-op="revision" title="${eur(S.state.rules.revision_cost)}">Révision</button>
        <button class="btn small" data-act="vehicle" data-id="${v.id}" data-op="ct" title="${eur(S.state.rules.ct_cost)}">CT</button>
        <button class="btn small" data-act="vehicle" data-id="${v.id}" data-op="overhaul" title="${eur(S.state.rules.overhaul_cost)}">Moteur</button>
        <button class="btn small" data-act="vehicle" data-id="${v.id}" data-op="refurbish" title="${eur(S.state.rules.refurbish_cost)}">Intérieur</button>
      </div></td>
    </tr>`).join("");
  const list = filteredModels();
  if (!list.some((m) => String(m.id) === S.buyModel)) S.buyModel = list.length ? String(list[0].id) : "";
  const opt = (obj, cur, all) => `<option value="">${all}</option>` + Object.entries(obj).map(([k, v]) => `<option value="${k}" ${cur === k ? "selected" : ""}>${v}</option>`).join("");
  const selected = S.models.find((m) => String(m.id) === S.buyModel);
  document.getElementById("tab-fleet").innerHTML = `
    <h2>Parc</h2>
    <table><thead><tr><th>Bus</th><th>Modèle</th><th class="num">Km</th><th>Moteur</th><th>Intérieur</th><th>Révision</th><th>CT</th><th>Option</th><th>Statut</th><th>Atelier</th></tr></thead>
    <tbody>${rows || `<tr><td colspan="10" class="muted">Aucun bus.</td></tr>`}</tbody></table>
    <p class="hint">Survolez un bouton d'atelier pour voir son prix. Révision : +25 d'état moteur, 1 jour. Moteur : révision générale (${S.state.rules.overhaul_days} j). Intérieur : remise à neuf (${S.state.rules.refurbish_days} j). Un bus à l'atelier ne roule pas. Les valideurs s'installent depuis l'onglet Atelier.</p>
    <h2>Acheter un bus</h2>
    <div class="row">
      <select data-filter="status">${opt({ neuf: "Neuf", occasion: "Occasion" }, S.filters.status, "Neuf et occasion")}</select>
      <select data-filter="body">${opt(BODY, S.filters.body, "Toutes carrosseries")}</select>
      <select data-filter="energy">${opt(ENERGY, S.filters.energy, "Toutes énergies")}</select>
    </div>
    <div class="row" style="margin-top:8px">
      <select id="buy-model" style="max-width:100%">${list.map((m) => `<option value="${m.id}" ${String(m.id) === S.buyModel ? "selected" : ""}>${esc(modelLabel(m))}</option>`).join("")}</select>
      <button class="btn primary" data-act="buy" ${list.length ? "" : "disabled"}>Acheter</button>
    </div>
    ${modelDetail(selected)}`;
}

function renderDrivers() {
  const rows = S.drivers.map((d) => `
    <tr><td>${esc(d.name)}</td><td>${d.articulated_licence ? "oui" : "non"}</td>
    <td class="num">${d.services}</td>
    <td class="num ${d.max_daily_hours > S.state.rules.driver_max_hours ? "bad" : ""}">${d.max_daily_hours} h / ${S.state.rules.driver_max_hours} h</td>
    <td class="num">${eur(d.daily_wage)}</td></tr>`).join("");
  document.getElementById("tab-drivers").innerHTML = `
    <h2>Conducteurs</h2>
    <table><thead><tr><th>Nom</th><th>Permis articulé</th><th class="num">Services</th><th class="num">Conduite max / jour</th><th class="num">Salaire / jour</th></tr></thead>
    <tbody>${rows || `<tr><td colspan="5" class="muted">Aucun conducteur.</td></tr>`}</tbody></table>
    <p class="hint">Les salaires sont payés chaque jour à tous les conducteurs, qu'ils roulent ou non.</p>
    <h2>Embaucher</h2>
    <div class="row"><input type="text" id="hire-name" placeholder="Nom" maxlength="60">
    <label><input type="checkbox" id="hire-art"> permis articulé</label>
    <button class="btn primary" data-act="hire">Embaucher</button></div>`;
}

function sparkline(demand) {
  const vals = Array.from({ length: 24 }, (_, h) => demand[h] || 0);
  const max = Math.max(...vals, 1);
  return `<div class="spark" title="Demande par heure (0 h → 23 h)">${vals.map((v, h) => `<i style="height:${Math.max(2, (v / max) * 100)}%" title="${h} h : ${v}"></i>`).join("")}</div>`;
}

function frequencyTable(line) {
  const head = Object.values(BANDS).map((b) => `<th class="num">${b}</th>`).join("");
  const rows = Object.entries(DAY_LABEL).map(([t, label]) => {
    const cov = bandCoverage(line, t);
    const cells = Object.keys(BANDS).map((b) => {
      const need = line.buses_needed[t][b], have = cov[b] ?? 0;
      const cls = have >= need ? "good" : have === 0 ? "bad" : "warn";
      return `<td class="num">${num1(line.frequency[t][b])}/h<div class="${cls}" style="font-size:12px">${have}/${need} bus</div></td>`;
    }).join("");
    return `<tr><td>${label}</td>${cells}</tr>`;
  }).join("");
  return `<table class="freq"><thead><tr><th>Fréquence cible</th>${head}</tr></thead><tbody>${rows}</tbody></table>`;
}

function renderLines() {
  const vehOptions = (sel) => `<option value="">— aucun —</option>` +
    S.vehicles.map((v) => `<option value="${v.id}" ${sel === v.id ? "selected" : ""}>${esc(v.plate)} (${BODY[v.body]}, ${v.capacity} pl.)</option>`).join("");
  const drvOptions = (sel) => `<option value="">— aucun —</option>` +
    S.drivers.map((d) => `<option value="${d.id}" ${sel === d.id ? "selected" : ""}>${esc(d.name)}${d.articulated_licence ? " (art.)" : ""}</option>`).join("");
  const cards = S.lines.map((line) => {
    const rows = line.services.map((s) => `
      <tr><td>${s.start_hour} h → ${s.end_hour} h</td><td>${DAY_TYPES[s.day_type]}</td>
      <td><select data-sel="vehicle" data-service="${s.id}">${vehOptions(s.vehicle_id)}</select></td>
      <td><select data-sel="driver" data-service="${s.id}">${drvOptions(s.driver_id)}</select></td>
      <td><button class="btn small danger" data-act="del-service" data-id="${s.id}">Supprimer</button></td></tr>`).join("");
    const peak = Math.max(...Object.values(line.demand.weekday), 0);
    const bodies = line.allowed_bodies.split(",").map((b) => BODY[b]).join(" ou ");
    return `<div class="panel" style="margin-bottom:16px">
      <div class="line-head">
        <h3>${esc(line.code)} · ${esc(line.name)}</h3>
        <span class="muted">${line.distance_km} km · ${line.duration_min} min · ${eur2(line.fare)}/passager · ${esc(bodies)}</span>
      </div>
      <div class="row" style="justify-content:space-between;align-items:flex-end">
        ${sparkline(line.demand.weekday)}
        <span class="hint">Pointe en semaine : ${num(peak)} voyageurs/h. Un bus assure ${num1(30 / line.duration_min)} départ/h dans chaque sens.</span>
      </div>
      ${frequencyTable(line)}
      <table style="margin-top:8px"><thead><tr><th>Horaire</th><th>Jours</th><th>Bus</th><th>Conducteur</th><th></th></tr></thead>
      <tbody>${rows || `<tr><td colspan="5" class="muted">Aucun service : ajoutez-en un.</td></tr>`}</tbody></table>
      <div class="row" style="margin-top:8px">
        <input type="number" min="0" max="23" value="6" style="width:70px" data-new="start" data-line="${line.id}"> h →
        <input type="number" min="1" max="24" value="14" style="width:70px" data-new="end" data-line="${line.id}"> h
        <select data-new="day" data-line="${line.id}">${Object.entries(DAY_TYPES).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
        <button class="btn" data-act="add-service" data-line="${line.id}">Ajouter un service</button>
      </div>
    </div>`;
  }).join("");
  document.getElementById("tab-lines").innerHTML = `
    <h2>Lignes et services</h2>
    <p class="hint">Un service est une plage horaire sur une ligne, tenue par un bus et un conducteur (9 h de conduite maximum par jour). La fréquence cible est un nombre de départs par heure dans chaque sens ; « 2/3 bus » signifie que 2 bus sont affectés sur les 3 nécessaires. Une fréquence tenue améliore la réputation.</p>
    ${cards}`;
}

function renderDepot() {
  const r = S.state.rules;
  const blocks = S.infra.depots.map((d) => {
    const rows = d.infra.map((i) => {
      if (i.energy === "electrique") {
        const electric = S.vehicles.filter((v) => v.energy === "electrique").length;
        return `<tr><td>Électricité</td><td>${i.capacity} borne(s)</td><td>${i.capacity * r.buses_per_charger} bus rechargés par nuit</td><td>${electric} bus électrique(s) au parc</td><td></td></tr>`;
      }
      const ratio = i.capacity ? (i.stock / i.capacity) * 100 : 0;
      const use = d.daily_use[i.energy];
      const days = use ? `${num1(i.stock / use)} j de réserve` : "pas encore de consommation";
      return `<tr><td>${ENERGY[i.energy]}</td><td>${bar(ratio)}</td>
        <td>${num(i.stock)} / ${num(i.capacity)} ${UNIT[i.energy]}</td><td>${days}${use ? ` (${num(use)} ${UNIT[i.energy]}/j)` : ""}</td>
        <td><div class="row"><input type="number" min="1" value="${Math.max(1, Math.round(i.capacity - i.stock))}" style="width:100px" data-fuel-qty="${d.id}-${i.energy}">
        <button class="btn small" data-act="fuel" data-depot="${d.id}" data-energy="${i.energy}" title="${eur2(r.energy_price[i.energy])} / ${UNIT[i.energy]}">Remplir</button></div></td></tr>`;
    }).join("");
    return `<div class="panel" style="margin-bottom:16px">
      <div class="line-head"><h3>${esc(d.name)}</h3><span class="muted">${d.used} / ${d.capacity} places</span></div>
      <table><thead><tr><th>Énergie</th><th>Niveau</th><th>Stock</th><th>Autonomie</th><th>Livraison</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="5" class="muted">Aucune citerne ni borne.</td></tr>`}</tbody></table>
      <p class="hint">Sans carburant en citerne, les bus font le plein en station publique : diesel/hybride +${num((r.external_fuel_premium.diesel - 1) * 100)} %, GNV +${num((r.external_fuel_premium.gnv - 1) * 100)} %.</p>
      <div class="row">
        <select data-build="energy-${d.id}"><option value="diesel">Citerne diesel</option><option value="gnv">Citerne GNV</option></select>
        <input type="number" min="1000" step="1000" value="10000" style="width:110px" data-build="cap-${d.id}"> L ou kg
        <button class="btn" data-act="tank" data-depot="${d.id}">Construire / agrandir</button>
        <span class="muted">diesel ${eur2(r.tank_cost_per_unit.diesel)} · GNV ${eur2(r.tank_cost_per_unit.gnv)} par unité de capacité</span>
      </div>
      <div class="row" style="margin-top:8px">
        <input type="number" min="1" max="50" value="1" style="width:70px" data-build="chargers-${d.id}"> borne(s) de recharge
        <button class="btn" data-act="chargers" data-depot="${d.id}">Installer (${eur(r.charger_cost)} pièce)</button>
        <span class="muted">${r.buses_per_charger} bus par borne</span>
      </div>
    </div>`;
  }).join("");
  document.getElementById("tab-depot").innerHTML = `<h2>Dépôt et infrastructures</h2>${blocks}`;
}

function renderWorkshop() {
  const opts = S.infra.options;
  const rows = opts.map((o) => `
    <tr><td><b>${esc(o.name)}</b><div class="muted" style="font-size:12px">${esc(o.description)}</div></td>
    <td class="num">${eur(o.price)}</td><td class="num">${o.install_days} j</td>
    <td class="num"><b>${o.stock}</b></td><td class="num">${o.installed} / ${S.vehicles.length} bus</td>
    <td><div class="row"><input type="number" min="1" max="500" value="1" style="width:70px" data-buy-qty="${o.code}">
    <button class="btn small primary" data-act="buy-option" data-code="${o.code}">Acheter</button></div></td></tr>`).join("");
  const installRows = opts.map((o) => {
    const free = S.vehicles.filter((v) => !v.options.includes(o.code) && v.status === "active");
    return `<div class="row" style="margin-bottom:8px"><span style="min-width:200px">${esc(o.name)} (stock : ${o.stock})</span>
      <select data-install-vehicle="${o.code}">${free.map((v) => `<option value="${v.id}">${esc(v.plate)} — ${esc(v.model_name)}</option>`).join("") || `<option value="">aucun bus disponible</option>`}</select>
      <button class="btn" data-act="install" data-code="${o.code}" ${free.length && o.stock ? "" : "disabled"}>Installer (bus immobilisé ${o.install_days} j)</button></div>`;
  }).join("");
  document.getElementById("tab-workshop").innerHTML = `
    <h2>Atelier : stock d'options</h2>
    <table><thead><tr><th>Option</th><th class="num">Prix</th><th class="num">Installation</th><th class="num">En stock</th><th class="num">Installées</th><th>Acheter</th></tr></thead><tbody>${rows}</tbody></table>
    <p class="hint">Un bus sans valideur perd ${num(S.state.rules.fraud_rate * 100)} % de ses recettes en fraude. L'installation consomme une pièce du stock et immobilise le bus à l'atelier : il ne roule pas pendant ce temps.</p>
    <h2>Installer une option</h2>${installRows}`;
}

function renderReport() {
  const r = S.report;
  if (r.day === null) {
    document.getElementById("tab-report").innerHTML = `<h2>Bilan</h2><p class="muted">Aucun jour calculé pour l'instant.</p>`;
    return;
  }
  const lineRows = r.lines.map((l) => {
    const freq = l.target_departures ? (l.delivered_departures / l.target_departures) * 100 : 100;
    return `<tr><td><b>${esc(l.code)}</b> ${esc(l.name)}</td><td class="num">${num(l.demand)}</td><td class="num">${num(l.served)}</td>
    <td class="num ${l.demand - l.served > 20 ? "warn" : ""}">${num(l.demand - l.served)}</td>
    <td class="num ${freq < 90 ? "warn" : ""}">${num(freq)} %</td>
    <td class="num">${num(l.km)} km</td><td class="num">${eur(l.revenue)}</td>
    <td class="num ${l.fraud_loss > 0 ? "warn" : ""}">${eur(l.fraud_loss)}</td></tr>`;
  }).join("");
  const total = r.ledger.reduce((a, x) => a + x.total, 0);
  const ledgerRows = r.ledger.map((x) => `<tr><td>${esc(KIND_LABELS[x.kind] || x.kind)}</td><td class="num">${signed(x.total)}</td></tr>`).join("");
  const maxAbs = Math.max(...r.history.map((h) => Math.abs(h.profit)), 1);
  const hist = r.history.map((h) => `<div title="Jour ${h.day} : ${eur(h.profit)}" style="height:${Math.max(3, (Math.abs(h.profit) / maxAbs) * 100)}%;background:${h.profit >= 0 ? "var(--good)" : "var(--bad)"}"></div>`).join("");
  document.getElementById("tab-report").innerHTML = `
    <h2>Bilan du jour ${r.day}</h2>
    <table><thead><tr><th>Ligne</th><th class="num">Demande</th><th class="num">Transportés</th><th class="num">Laissés à quai</th><th class="num">Fréquence tenue</th><th class="num">Km</th><th class="num">Recettes</th><th class="num">Fraude</th></tr></thead><tbody>${lineRows}</tbody></table>
    <h2>Comptes du jour</h2>
    <table style="max-width:460px"><tbody>${ledgerRows}<tr><td><b>Résultat</b></td><td class="num"><b>${signed(total)}</b></td></tr></tbody></table>
    <h2>Résultat des 30 derniers jours</h2>
    <div class="history">${hist}</div>`;
}

// ---------- Événements ----------

document.getElementById("tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]");
  if (b) { S.tab = b.dataset.tab; render(); }
});

const val = (sel) => document.querySelector(sel).value;

document.body.addEventListener("click", (e) => {
  const el = e.target.closest("[data-act]");
  if (!el) return;
  const id = el.dataset.id;
  const depot = Number(el.dataset.depot);
  switch (el.dataset.act) {
    case "tick":
      act(async () => {
        const r = await api("POST", "/tick", { days: Number(el.dataset.days) });
        const sum = r.days.reduce((a, d) => a + d.profit, 0);
        toast(`${r.days.length} jour(s) calculé(s) : ${sum >= 0 ? "+" : ""}${eur(sum)}`);
      });
      break;
    case "vehicle": {
      const labels = { revision: "Révision lancée", ct: "Contrôle technique passé", overhaul: "Révision générale lancée", refurbish: "Remise à neuf lancée" };
      act(() => api("POST", `/vehicles/${id}/${el.dataset.op}`), labels[el.dataset.op]);
      break;
    }
    case "buy":
      act(() => api("POST", "/vehicles", { model_id: Number(val("#buy-model")) }), "Bus acheté");
      break;
    case "hire": {
      const name = val("#hire-name").trim();
      if (!name) return toast("Indiquez un nom", true);
      act(() => api("POST", "/drivers", { name, articulated_licence: document.getElementById("hire-art").checked }), "Conducteur embauché");
      break;
    }
    case "add-service": {
      const q = (k) => val(`[data-new="${k}"][data-line="${el.dataset.line}"]`);
      act(() => api("POST", "/services", { line_id: Number(el.dataset.line), start_hour: Number(q("start")), end_hour: Number(q("end")), day_type: q("day") }), "Service ajouté");
      break;
    }
    case "del-service":
      if (confirm("Supprimer ce service ?")) act(() => api("DELETE", `/services/${id}`), "Service supprimé");
      break;
    case "tank":
      act(() => api("POST", "/infra/tank", { depot_id: depot, energy: val(`[data-build="energy-${depot}"]`), capacity: Number(val(`[data-build="cap-${depot}"]`)) }), "Citerne construite");
      break;
    case "chargers":
      act(() => api("POST", "/infra/chargers", { depot_id: depot, count: Number(val(`[data-build="chargers-${depot}"]`)) }), "Bornes installées");
      break;
    case "fuel":
      act(() => api("POST", "/infra/fuel", { depot_id: depot, energy: el.dataset.energy, quantity: Number(val(`[data-fuel-qty="${depot}-${el.dataset.energy}"]`)) }), "Citerne remplie");
      break;
    case "buy-option":
      act(() => api("POST", "/options/buy", { code: el.dataset.code, quantity: Number(val(`[data-buy-qty="${el.dataset.code}"]`)) }), "Option achetée, ajoutée au stock");
      break;
    case "install": {
      const vid = val(`[data-install-vehicle="${el.dataset.code}"]`);
      if (!vid) return;
      act(() => api("POST", "/options/install", { vehicle_id: Number(vid), code: el.dataset.code }), "Installation lancée : le bus est à l'atelier");
      break;
    }
  }
});

document.body.addEventListener("change", (e) => {
  const filter = e.target.closest("select[data-filter]");
  if (filter) {
    S.filters[filter.dataset.filter] = filter.value;
    return renderFleet();
  }
  if (e.target.id === "buy-model") {
    S.buyModel = e.target.value;
    return renderFleet();
  }
  const sel = e.target.closest("select[data-sel]");
  if (!sel) return;
  const kind = sel.dataset.sel; // vehicle | driver
  const id = Number(sel.dataset.service);
  const body = sel.value === "" ? { [`unassign_${kind}`]: true } : { [`${kind}_id`]: Number(sel.value) };
  act(() => api("PATCH", `/services/${id}`, body));
});

(async () => {
  try {
    const me = await api("GET", "/me");
    await enterGame(me.username);
  } catch (e) {
    showAuth();
  }
})();
