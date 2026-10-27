// M4 CopyDesk: tema, aggiornamento in tempo reale (polling locale) e azioni di controllo protette.
(function () {
  "use strict";
  const root = document.documentElement;
  try { const t = localStorage.getItem("m4-theme"); if (t) root.dataset.theme = t; } catch (e) { /* storage non disponibile */ }

  function token() { const m = document.querySelector('meta[name="m4-token"]'); return m ? m.content : ""; }

  async function control(btn) {
    const action = btn.dataset.action, label = btn.dataset.label;
    const msg = action === "stop"
      ? "Confermi ARRESTA? Il motore si ferma; per ripartire serve un comando umano."
      : action === "pause"
        ? "Confermi PAUSA NUOVI ACQUISTI? Le uscite protettive restano attive."
        : "Confermi RIPRENDI? Il sistema torna ad accettare nuovi acquisti simulati.";
    if (!window.confirm(msg)) return;
    btn.disabled = true;
    try {
      const r = await fetch("/api/control/" + action, {
        method: "POST", headers: { "Content-Type": "application/json", "X-M4-Token": token() },
        body: JSON.stringify({ confirm: true }), credentials: "same-origin",
      });
      if (!r.ok) { window.alert("Comando rifiutato (" + r.status + ")"); }
      await refresh(true);
    } finally { btn.disabled = false; }
  }

  let busy = false;
  async function refresh(force) {
    if (busy || (document.hidden && !force)) return;
    const el = document.getElementById("content");
    if (!el) return;
    busy = true;
    try {
      const open = new Set([...el.querySelectorAll("details[open]")].map((d) => d.dataset.key));
      const url = new URL(window.location.href); url.searchParams.set("partial", "1");
      const r = await fetch(url, { headers: { Accept: "text/html" }, credentials: "same-origin" });
      if (!r.ok) return;
      const html = await r.text();
      const tmp = document.createElement("div"); tmp.innerHTML = html;
      const fresh = tmp.querySelector("#content");
      if (fresh && fresh.innerHTML !== el.innerHTML) {
        el.innerHTML = fresh.innerHTML;
        el.querySelectorAll("details").forEach((d) => { if (open.has(d.dataset.key)) d.open = true; });
      }
    } catch (e) { /* server non raggiungibile: si riprova */ } finally { busy = false; }
  }

  async function liveAction(btn) {
    const what = btn.dataset.live;
    let body = {};
    if (what === "arm") {
      const t = window.prompt('ESECUZIONE REALE con soldi veri.\nIl bot comprerà e venderà da solo entro i limiti configurati.\nPer confermare digita: ARMA LIVE');
      if (t === null) return;
      body = { confirm: t };
    } else if (!window.confirm("Disarmare il live? Nessun nuovo acquisto reale; le vendite protettive restano attive.")) {
      return;
    }
    btn.disabled = true;
    try {
      const r = await fetch("/api/live/" + what, {
        method: "POST", headers: { "Content-Type": "application/json", "X-M4-Token": token() },
        body: JSON.stringify(body), credentials: "same-origin",
      });
      const j = await r.json().catch(() => ({}));
      window.alert(r.ok ? (j.messaggio || "Fatto") : ("Rifiutato: " + (j.detail || r.status)));
      await refresh(true);
    } finally { btn.disabled = false; }
  }

  async function runCheck(btn) {
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = "Controllo in corso… (10-30 s)";
    try {
      const r = await fetch("/api/check", { method: "POST", headers: { "Content-Type": "application/json",
        "X-M4-Token": token() }, body: "{}", credentials: "same-origin" });
      if (!r.ok) window.alert("Controllo non eseguito (" + r.status + ")");
      window.location.href = "/controllo";
    } finally { btn.disabled = false; btn.textContent = old; }
  }
  window.addEventListener("load", () => {
    const b = document.querySelector("button[data-check]");
    if (b && new URLSearchParams(window.location.search).get("run") === "1") runCheck(b);
  });

  document.addEventListener("click", (ev) => {
    const cb = ev.target.closest("button[data-check]");
    if (cb) { ev.preventDefault(); runCheck(cb); return; }
    const lb = ev.target.closest("button[data-live]");
    if (lb) { ev.preventDefault(); liveAction(lb); return; }
    const b = ev.target.closest("button[data-action]");
    if (b) { ev.preventDefault(); control(b); return; }
    if (ev.target.closest("#theme-toggle")) {
      const dark = root.dataset.theme ? root.dataset.theme === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
      root.dataset.theme = dark ? "light" : "dark";
      try { localStorage.setItem("m4-theme", root.dataset.theme); } catch (e) { /* ignora */ }
    }
  });
  setInterval(refresh, 4000);
})();
