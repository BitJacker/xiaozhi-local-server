/* Husky locale: lingua (IT/EN), tema (automatico/chiaro/scuro), barra in alto e piccoli aiuti comuni.
   Caricato nell'<head> di ogni pagina, dopo strings.js: il tema si applica subito, senza lampi. */
(function () {
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* navigazione privata: pazienza */ } },
  };
  const STR = window.HUSKY_STRINGS || { it: {}, en: {} };
  let lang = store.get("husky-lang") || ((navigator.language || "it").toLowerCase().startsWith("it") ? "it" : "en");
  if (!STR[lang]) lang = "it";
  let theme = store.get("husky-theme") || "auto";

  function applyTheme() {
    if (theme === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", theme);
  }
  applyTheme();
  document.documentElement.lang = lang;

  function t(key, vars) {
    let s = (STR[lang] && STR[lang][key]) ?? (STR.it && STR.it[key]) ?? key;
    if (vars) for (const [k, v] of Object.entries(vars)) s = s.split("{" + k + "}").join(String(v));
    return s;
  }
  function applyI18n(root = document) {
    for (const e of root.querySelectorAll("[data-i18n]")) e.textContent = t(e.dataset.i18n);
    for (const e of root.querySelectorAll("[data-i18n-html]")) e.innerHTML = t(e.dataset.i18nHtml);  // solo testi nostri
    for (const e of root.querySelectorAll("[data-i18n-placeholder]")) e.placeholder = t(e.dataset.i18nPlaceholder);
    for (const e of root.querySelectorAll("[data-i18n-title]")) e.title = t(e.dataset.i18nTitle);
    for (const e of root.querySelectorAll("[data-i18n-aria]")) e.setAttribute("aria-label", t(e.dataset.i18nAria));
    const tt = document.querySelector("title[data-i18n]");
    if (tt) document.title = t(tt.dataset.i18n);
  }
  function setLang(l) {
    lang = l; store.set("husky-lang", l); document.documentElement.lang = l;
    applyI18n(); renderSwitches();
    document.dispatchEvent(new CustomEvent("husky:lang", { detail: l }));
  }
  function setTheme(v) { theme = v; store.set("husky-theme", v); applyTheme(); renderSwitches(); }

  function el(tag, text, cls, attrs) {
    const e = document.createElement(tag);
    if (text != null) e.textContent = text;
    if (cls) e.className = cls;
    if (attrs) for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    return e;
  }
  async function api(url, method = "GET", data) {
    const opt = { method, headers: { "X-Requested-With": "husky" } };
    if (data !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(data); }
    const r = await fetch(url, opt);
    if (r.status === 401 && !url.startsWith("/api/login")) {
      location.href = "/login?next=" + encodeURIComponent(location.pathname + location.hash);
      throw new Error("login");
    }
    return { status: r.status, ok: r.ok, data: await r.json().catch(() => ({})) };
  }
  function msg(node, text, ok) { node.textContent = text || ""; node.className = "msg " + (text ? (ok ? "ok" : "err") : ""); }

  // ------------------------------------------------------------ barra in alto
  let me = null;
  // modalità "token fisso" (server.tokens): il token arriva nell'indirizzo e va passato anche al server e ai link
  const TOKEN = new URLSearchParams(location.search).get("token") || "";
  const withToken = (url) => TOKEN ? url + (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(TOKEN) : url;
  function seg(label, options, current, onPick) {
    const g = el("div", null, "seg", { role: "group", "aria-label": label });
    for (const [value, text, title] of options) {
      const b = el("button", text, null, { type: "button", "aria-pressed": String(value === current), title: title || text });
      b.onclick = () => onPick(value);
      g.append(b);
    }
    return g;
  }
  function renderSwitches() {
    const box = document.getElementById("switches");
    if (!box) return;
    box.replaceChildren(
      seg(t("ui.language"), [["it", "IT", "Italiano"], ["en", "EN", "English"]], lang, setLang),
      seg(t("ui.theme"), [["auto", "A", t("ui.theme_auto")], ["light", "☀", t("ui.theme_light")], ["dark", "☾", t("ui.theme_dark")]], theme, setTheme),
    );
  }
  function renderNav(current) {
    const nav = document.getElementById("nav");
    if (!nav) return;
    const links = [["/", "nav.assistant"], ["/cronologia", "nav.history"]];
    if (me && me.auth_enabled) links.push(["/account", "nav.account"]);
    if (me && me.role === "admin") links.push(["/admin", "nav.admin"]);
    nav.replaceChildren(...links.map(([href, key]) => {
      const a = el("a", t(key), null, { href: withToken(href) });
      if (href === current) a.setAttribute("aria-current", "page");
      return a;
    }));
    if (me && me.auth_enabled) {
      const out = el("a", t("nav.logout"), null, { href: "#" });
      out.onclick = async (e) => { e.preventDefault(); await api("/api/logout", "POST", {}); location.href = "/login"; };
      nav.append(out);
    }
  }
  async function topbar(current, opts = {}) {
    const host = document.getElementById("topbar");
    if (host && !host.firstChild) {
      host.className = "topbar";
      host.innerHTML = `<div class="topbar-in"><a class="brand" href="/"><span class="brand-mark" aria-hidden="true">H</span><span>Husky</span></a>
        <nav class="nav" id="nav" aria-label="Menu"></nav><div class="switches" id="switches"></div></div>`;
    }
    renderSwitches();
    if (opts.noUser) return null;
    try { const r = await fetch(withToken("/api/me")); me = r.ok ? await r.json() : null; } catch { me = null; }
    renderNav(current);
    document.addEventListener("husky:lang", () => renderNav(current));
    return me;
  }

  window.HUSKY = { t, applyI18n, setLang, setTheme, el, api, msg, topbar, get lang() { return lang; }, get me() { return me; } };
  document.addEventListener("DOMContentLoaded", () => applyI18n());
})();
