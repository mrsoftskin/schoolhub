// Command Center Session Sync - MV3 service worker.
//
// For each course site, read the cookies Chrome already holds (including
// httpOnly ones, via the official chrome.cookies API - no password, no
// decryption, no reaching into disk) and POST them to the local app so its
// sync/grades stay authenticated. Runs when a relevant cookie changes and on
// a periodic alarm, so as long as you stay logged in, the app never goes
// stale. Sends ONLY to 127.0.0.1:8177.
//
// SCOPE IS DELIBERATELY NARROW, and it was not always. chrome.cookies.getAll
// matches a domain AND ALL ITS SUBDOMAINS, so asking for ".google.com" hands
// back Gmail, Drive, YouTube and every other Google property - far beyond the
// Docs export this app actually performs, and far beyond what the docs
// claimed. Each entry below now names exact hosts, and everything returned is
// re-checked against that list before it is sent.

const APP = "http://127.0.0.1:8177/api/session/push";

// domain the cookies live on -> our site name, plus the cookie names that
// actually matter (we still send them all for that domain, but this documents
// the load-bearing ones).
const SITES = [
  { site: "oaks",    domain: "lms.cofc.edu" },
  { site: "vhl",     domain: "www.vhlcentral.com", extra: ["m3a.vhlcentral.com", ".vhlcentral.com"] },
  { site: "connect", domain: "newconnect.mheducation.com", extra: [".mheducation.com"] },
  { site: "blended", domain: "library.blended-teaching.com" },
  // Auxiliary sessions for OAKS 'Link' materials (docs behind org login).
  // links.py only ever fetches docs.google.com exports and the school's own
  // SharePoint, so nothing broader is collected: no mail, no drive, no
  // accounts.google.com, no tenant-wide .office.com.
  { site: "google",     domain: "docs.google.com" },
  { site: "sharepoint", domain: "cofc-my.sharepoint.com" },
];

// A cookie is kept only if its own domain is one of the hosts we asked for,
// or a parent of it. getAll is a suffix match, so this is the backstop that
// makes the SITES list an actual boundary rather than a hint.
function allowedCookie(cookie, domains) {
  const host = (cookie.domain || "").replace(/^\./, "").toLowerCase();
  return domains.some(d => {
    const want = d.replace(/^\./, "").toLowerCase();
    return host === want || want.endsWith("." + host) || host.endsWith("." + want);
  });
}

async function cookiesFor(domains) {
  const jar = {};
  for (const d of domains) {
    const list = await chrome.cookies.getAll({ domain: d });
    for (const c of list) {
      if (!allowedCookie(c, domains)) continue;
      jar[c.name] = c.value;
    }
  }
  return jar;
}

async function pushSite(entry) {
  const domains = [entry.domain, ...(entry.extra || [])];
  let cookies;
  try {
    cookies = await cookiesFor(domains);
  } catch (e) {
    return;
  }
  if (!cookies || Object.keys(cookies).length === 0) return;  // not logged in
  try {
    const res = await fetch(APP, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CC-Extension": "1" },
      body: JSON.stringify({ site: entry.site, cookies }),
    });
    const ok = res.ok;
    await chrome.storage.local.set({
      ["last_" + entry.site]: { at: Date.now(), ok, count: Object.keys(cookies).length },
    });
  } catch (e) {
    // App not running - fine, try again next tick.
    await chrome.storage.local.set({
      ["last_" + entry.site]: { at: Date.now(), ok: false, error: String(e) },
    });
  }
}

async function pushAll() {
  for (const entry of SITES) await pushSite(entry);
}

// ---- fetch org-locked link documents in the authenticated browser -------
// Google Docs / SharePoint files can't be fetched server-side (anti-scraping).
// Here we ARE the logged-in browser, so a credentialed fetch just works.

const PENDING = "http://127.0.0.1:8177/api/links/pending";
const CONTENT = "http://127.0.0.1:8177/api/links/content";
const IMPORTED = "http://127.0.0.1:8177/api/links/imported";
const REINDEX = "http://127.0.0.1:8177/api/links/reindex";

function toBase64(buf) {
  let bin = "";
  const bytes = new Uint8Array(buf);
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(bin);
}

// Download each pending doc via chrome.downloads (no CORS), then tell the app
// to import it from Downloads/cc-links. Resolves to the number imported.
function downloadDocs(pending) {
  return new Promise(resolve => {
    let imported = 0, settled = 0;
    if (!pending.length) return resolve(0);
    const byDownloadId = {};
    const finish = () => { if (settled >= pending.length) resolve(imported); };

    const onChanged = async delta => {
      const item = byDownloadId[delta.id];
      if (!item) return;
      if (delta.state && delta.state.current === "complete") {
        delete byDownloadId[delta.id];
        try {
          const res = await fetch(IMPORTED, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CC-Extension": "1" },
            body: JSON.stringify({ id: item.id }),
          });
          if (res.ok) imported++;
        } catch (e) { /* app will import on next sync */ }
        settled++; finish();
      } else if (delta.state && delta.state.current === "interrupted") {
        delete byDownloadId[delta.id];
        settled++; finish();
      }
    };
    chrome.downloads.onChanged.addListener(onChanged);

    for (const item of pending) {
      chrome.downloads.download(
        { url: item.fetch_url, filename: `cc-links/${item.id}.${item.ext || "txt"}`,
          conflictAction: "overwrite", saveAs: false },
        downloadId => {
          if (chrome.runtime.lastError || downloadId == null) { settled++; finish(); return; }
          byDownloadId[downloadId] = item;
        });
    }
    // Safety timeout so the popup never hangs.
    setTimeout(() => { chrome.downloads.onChanged.removeListener(onChanged); resolve(imported); }, 60000);
  });
}

async function fetchDocuments() {
  let pending;
  try {
    pending = (await (await fetch(PENDING)).json()).pending || [];
  } catch (e) {
    await chrome.storage.local.set({ last_docs: { at: Date.now(), ok: false, error: "app not running" } });
    return { ok: false, done: 0, total: 0 };
  }
  // fetch() can't read Google's export: its file host redirects to
  // googleusercontent.com with a wildcard Allow-Origin, which the browser
  // refuses to expose to a credentialed request. chrome.downloads is NOT
  // subject to CORS - it performs a real, authenticated browser download,
  // exactly like clicking "Download as text". The file lands in
  // Downloads/cc-links/<id>.<ext>; the app imports it from there.
  const done = await downloadDocs(pending);
  const courses = new Set(pending.map(p => p.course));
  if (courses.size) {
    try {
      await fetch(REINDEX, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CC-Extension": "1" },
        body: JSON.stringify({ courses: [...courses] }),
      });
    } catch (e) { /* server will pick it up on next index */ }
  }
  await chrome.storage.local.set({
    last_docs: { at: Date.now(), ok: true, done, total: pending.length },
  });
  return { ok: true, done, total: pending.length };
}

// On install / browser start: push once and set a heartbeat.
chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("cc-sync", { periodInMinutes: 30 });
  pushAll();
});
chrome.runtime.onStartup.addListener(() => pushAll().then(fetchDocuments));
chrome.alarms.onAlarm.addListener(a => {
  if (a.name === "cc-sync") pushAll().then(fetchDocuments);
});

// React immediately when a relevant cookie changes (login, refresh, logout).
const WATCHED = SITES.flatMap(s => [s.domain, ...(s.extra || [])])
  .map(d => d.replace(/^\./, ""));
// Popup buttons.
chrome.runtime.onMessage.addListener((msg, _s, reply) => {
  if (msg && msg.cmd === "sync") {
    pushAll().then(() => fetchDocuments()).then(() => reply({ ok: true }));
    return true;
  }
  if (msg && msg.cmd === "fetchdocs") {
    fetchDocuments().then(r => reply(r));
    return true;
  }
});

chrome.cookies.onChanged.addListener(info => {
  const dom = (info.cookie.domain || "").replace(/^\./, "");
  if (WATCHED.some(w => dom === w || dom.endsWith("." + w) || w.endsWith("." + dom))) {
    const entry = SITES.find(s => {
      const all = [s.domain, ...(s.extra || [])].map(d => d.replace(/^\./, ""));
      return all.some(w => dom === w || dom.endsWith("." + w));
    });
    if (entry) pushSite(entry);
  }
});
