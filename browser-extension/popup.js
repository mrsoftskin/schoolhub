const SITES = ["oaks", "vhl", "connect", "blended"];

function ago(ts) {
  if (!ts) return "never";
  const m = Math.round((Date.now() - ts) / 60000);
  if (m < 1) return "just now";
  if (m < 60) return m + "m ago";
  return Math.round(m / 60) + "h ago";
}

async function render() {
  const keys = SITES.map(s => "last_" + s).concat("last_docs");
  const store = await chrome.storage.local.get(keys);
  const rows = SITES.map(s => {
    const d = store["last_" + s];
    if (!d) return `<div class="row"><span class="site">${s}</span><span class="muted">not synced</span></div>`;
    const cls = d.ok ? "ok" : "bad";
    const txt = d.ok ? `synced ${ago(d.at)} (${d.count})` : `failed ${ago(d.at)}`;
    return `<div class="row"><span class="site">${s}</span><span class="${cls}">${txt}</span></div>`;
  });
  document.getElementById("rows").innerHTML = rows.join("");
  const docs = store.last_docs;
  document.getElementById("docs-status").textContent = docs
    ? (docs.ok ? `Documents: ${docs.done}/${docs.total} fetched ${ago(docs.at)}`
               : `Documents: ${docs.error || "failed"} ${ago(docs.at)}`)
    : "";
}

document.getElementById("sync").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ cmd: "sync" }).catch(() => {});
  setTimeout(render, 1200);
});

document.getElementById("fetchdocs").addEventListener("click", async () => {
  document.getElementById("docs-status").textContent = "Fetching documents…";
  await chrome.runtime.sendMessage({ cmd: "fetchdocs" }).catch(() => {});
  setTimeout(render, 1500);
});

render();
