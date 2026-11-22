"use strict";

const KIND_LABELS = {
  revenue: "Recettes", fuel: "Énergie", maintenance: "Entretien courant", wages: "Salaires", depot: "Dépôt",
  repair: "Pannes", revision: "Révisions", overhaul: "Révisions générales", refurbish: "Remises à neuf intérieur",
  ct: "Contrôle technique", purchase: "Achats de bus", capital: "Capital",
};
const DAY_TYPES = { all: "Tous les jours", weekday: "Semaine", weekend: "Week-end" };
const ENERGY = { diesel: "Diesel", gnv: "GNV", electrique: "Électrique" };
const BODY = { standard: "Standard", articule: "Articulé" };
const WEEKDAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"];

const S = { state: null, models: [], vehicles: [], drivers: [], lines: [], report: null, events: [], tab: "dashboard" };

const esc = (x) => String(x ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const eur = (n) => new Intl.NumberFormat("fr-FR", { style: "currency", currency: "EUR", maximumFractionDigits: 0 }).format(n);
const eur2 = (n) => new Intl.NumberFormat("fr-FR", { style: "currency", currency: "EUR", minimumFractionDigits: 2 }).format(n);
const num = (n) => new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 0 }).format(n);
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

async function api(method, path, body) {
  const res = await fetch("/api" + path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
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
    toast(e.message, true);
  }
  await refresh();
}

async function refresh() {
  [S.state, S.models, S.vehicles, S.drivers, S.lines, S.report, S.events] = await Promise.all([
    api("GET", "/state"), api("GET", "/models"), api("GET", "/vehicles"), api("GET", "/drivers"),
    api("GET", "/lines"), api("GET", "/report"), api("GET", "/events?limit=12"),
  ]);
  render();
}

function bar(value) {
  const color = value >= 60 ? "var(--good)" : value >= 30 ? "var(--warn)" : "var(--bad)";
  return `<span class="bar" title="${value.toFixed(0)} / 100"><i style="width:${Math.max(2, value)}%;background:${color}"></i></span> ${value.toFixed(0)}`;
}

function dueLabel(days, warnAt) {
  if (days < 0) return `<span class="bad">dépassée de ${-days} j</span>`;
  return `<span class="${days <= warnAt ? "warn" : ""}">dans ${days} j</span>`;
}

// ---------- Rendu ----------

function render() {
  const st = S.state;
  document.getElementById("company-name").textContent = st.name;
  document.getElementById("top-date").textContent = `Jour ${st.game_day} · ${frDate(st.date)}`;
  document.getElementById("top-cash").textContent = eur(st.cash);
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === S.tab));
  document.querySelectorAll(".tab").forEach((t) => (t.hidden = t.id !== "tab-" + S.tab));
  ({ dashboard: renderDashboard, fleet: renderFleet, drivers: renderDrivers, lines: renderLines, report: renderReport })[S.tab]();
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
  }
  for (const line of S.lines) {
    for (const s of line.services) {
      if (!s.vehicle_id || !s.driver_id) {
        out.push(["warn", `${line.code} ${s.start_hour}h–${s.end_hour}h (${DAY_TYPES[s.day_type]}) : ${!s.vehicle_id ? "pas de bus" : "pas de conducteur"} affecté, le service ne roule pas.`]);
      }
    }
    if (!line.services.length) out.push(["warn", `${line.code} n'a aucun service.`]);
  }
  return out;
}

function renderDashboard() {
  const st = S.state;
  const al = alerts();
  const depots = st.depots.map((d) => `${esc(d.name)} : ${d.used}/${d.capacity} places`).join(" · ");
  const last = st.last_day;
  document.getElementById("tab-dashboard").innerHTML = `
    <div class="cards">
      <div class="card"><div class="label">Trésorerie</div><div class="value">${eur(st.cash)}</div><div class="sub">${depots}</div></div>
      <div class="card"><div class="label">Prochain jour à simuler</div><div class="value">Jour ${st.game_day}</div><div class="sub">${frDate(st.date)} · ${st.day_type === "weekend" ? "week-end" : "semaine"}</div></div>
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

function renderFleet() {
  const rows = S.vehicles.map((v) => `
    <tr>
      <td><b>${esc(v.plate)}</b></td>
      <td>${esc(v.model_name)}<div class="muted" style="font-size:12px">${v.capacity} places</div></td>
      <td class="num">${num(v.km)} km</td>
      <td>${bar(v.engine_state)}</td>
      <td>${bar(v.interior_state)}</td>
      <td>${dueLabel(v.revision_in_days, 7)}</td>
      <td>${dueLabel(v.ct_in_days, 14)}</td>
      <td>${v.status === "workshop" ? `<span class="tag workshop">atelier → J${v.available_from_day}</span>` : `<span class="tag ok">en service</span>`}<div class="muted" style="font-size:12px">${v.services} service(s)</div></td>
      <td><div class="row">
        <button class="btn small" data-act="vehicle" data-id="${v.id}" data-op="revision" title="${eur(S.state.rules.revision_cost)}">Révision</button>
        <button class="btn small" data-act="vehicle" data-id="${v.id}" data-op="ct" title="${eur(S.state.rules.ct_cost)}">CT</button>
        <button class="btn small" data-act="vehicle" data-id="${v.id}" data-op="overhaul" title="${eur(S.state.rules.overhaul_cost)}">Moteur</button>
        <button class="btn small" data-act="vehicle" data-id="${v.id}" data-op="refurbish" title="${eur(S.state.rules.refurbish_cost)}">Intérieur</button>
      </div></td>
    </tr>`).join("");
  const options = S.models.map((m) =>
    `<option value="${m.id}">${esc(m.name)} — ${ENERGY[m.energy]}, ${BODY[m.body]}, ${m.capacity} places — ${eur(m.price)}</option>`).join("");
  document.getElementById("tab-fleet").innerHTML = `
    <h2>Parc</h2>
    <table><thead><tr><th>Bus</th><th>Modèle</th><th class="num">Km</th><th>Moteur</th><th>Intérieur</th><th>Révision</th><th>CT</th><th>Statut</th><th>Atelier</th></tr></thead>
    <tbody>${rows || `<tr><td colspan="9" class="muted">Aucun bus.</td></tr>`}</tbody></table>
    <p class="hint">Survolez un bouton d'atelier pour voir son prix. Révision : +25 d'état moteur, 1 jour. Moteur : révision générale, état remis à 100, plusieurs jours. Intérieur : remise à neuf, plusieurs jours. Un bus à l'atelier ne roule pas.</p>
    <h2>Acheter un bus</h2>
    <div class="row"><select id="buy-model">${options}</select><button class="btn primary" data-act="buy">Acheter</button></div>`;
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
    const tripsPerHour = 60 / line.duration_min;
    return `<div class="panel" style="margin-bottom:16px">
      <div class="line-head">
        <h3>${esc(line.code)} · ${esc(line.name)}</h3>
        <span class="muted">${line.distance_km} km · ${line.duration_min} min · ${eur2(line.fare)}/passager · ${line.allowed_body === "standard" ? "bus standard uniquement" : "tous bus"}</span>
      </div>
      <div class="row" style="justify-content:space-between;align-items:flex-end">
        ${sparkline(line.demand.weekday)}
        <span class="hint">Pointe en semaine : ${num(peak)} voyageurs/h — un bus standard de 90 places en transporte ${num(90 * tripsPerHour)}/h sur cette ligne.</span>
      </div>
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
    <p class="hint">Un service est une plage horaire sur une ligne, tenue par un bus et un conducteur (9 h de conduite maximum par jour). Un bus ou un conducteur peut enchaîner plusieurs services, sans chevauchement.</p>
    ${cards}`;
}

function renderReport() {
  const r = S.report;
  if (r.day === null) {
    document.getElementById("tab-report").innerHTML = `<h2>Bilan</h2><p class="muted">Aucun jour calculé pour l'instant.</p>`;
    return;
  }
  const lineRows = r.lines.map((l) => `
    <tr><td><b>${esc(l.code)}</b> ${esc(l.name)}</td><td class="num">${num(l.demand)}</td><td class="num">${num(l.served)}</td>
    <td class="num ${l.demand - l.served > 20 ? "warn" : ""}">${num(l.demand - l.served)}</td>
    <td class="num">${num(l.km)} km</td><td class="num">${eur(l.revenue)}</td></tr>`).join("");
  const total = r.ledger.reduce((a, x) => a + x.total, 0);
  const ledgerRows = r.ledger.map((x) => `<tr><td>${esc(KIND_LABELS[x.kind] || x.kind)}</td><td class="num">${signed(x.total)}</td></tr>`).join("");
  const maxAbs = Math.max(...r.history.map((h) => Math.abs(h.profit)), 1);
  const hist = r.history.map((h) => `<div title="Jour ${h.day} : ${eur(h.profit)}" style="height:${Math.max(3, (Math.abs(h.profit) / maxAbs) * 100)}%;background:${h.profit >= 0 ? "var(--good)" : "var(--bad)"}"></div>`).join("");
  document.getElementById("tab-report").innerHTML = `
    <h2>Bilan du jour ${r.day}</h2>
    <table><thead><tr><th>Ligne</th><th class="num">Demande</th><th class="num">Transportés</th><th class="num">Laissés à quai</th><th class="num">Km</th><th class="num">Recettes</th></tr></thead><tbody>${lineRows}</tbody></table>
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

document.body.addEventListener("click", (e) => {
  const el = e.target.closest("[data-act]");
  if (!el) return;
  const id = el.dataset.id;
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
      act(() => api("POST", "/vehicles", { model_id: Number(document.getElementById("buy-model").value) }), "Bus acheté");
      break;
    case "hire": {
      const name = document.getElementById("hire-name").value.trim();
      if (!name) return toast("Indiquez un nom", true);
      act(() => api("POST", "/drivers", { name, articulated_licence: document.getElementById("hire-art").checked }), "Conducteur embauché");
      break;
    }
    case "add-service": {
      const q = (k) => document.querySelector(`[data-new="${k}"][data-line="${el.dataset.line}"]`).value;
      act(() => api("POST", "/services", { line_id: Number(el.dataset.line), start_hour: Number(q("start")), end_hour: Number(q("end")), day_type: q("day") }), "Service ajouté");
      break;
    }
    case "del-service":
      if (confirm("Supprimer ce service ?")) act(() => api("DELETE", `/services/${id}`), "Service supprimé");
      break;
  }
});

document.body.addEventListener("change", (e) => {
  const sel = e.target.closest("select[data-sel]");
  if (!sel) return;
  const kind = sel.dataset.sel; // vehicle | driver
  const id = Number(sel.dataset.service);
  const body = sel.value === "" ? { [`unassign_${kind}`]: true } : { [`${kind}_id`]: Number(sel.value) };
  act(() => api("PATCH", `/services/${id}`, body));
});

refresh().catch((e) => toast(e.message, true));
