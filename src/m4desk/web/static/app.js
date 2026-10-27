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

  document.addEventListener("click", (ev) => {
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
