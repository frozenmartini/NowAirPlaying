"""The setup page at http://<hostname>.local:8080/ (docs/SETUP-API.md#the-setup-page).

One plain page, no framework, calling the same API as Home Assistant without a
token. Kept in a .py file because the installer ships speakerd/*.py only.
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NowAirPlaying</title>
<style>
  :root { --bg:#fafafa; --fg:#1d1d1f; --muted:#6e6e73; --card:#fff; --line:#e5e5ea;
          --ok:#1a7f37; --bad:#c62828; --accent:#0a66c2; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#111; --fg:#f2f2f7; --muted:#98989f; --card:#1c1c1e; --line:#2c2c2e;
            --ok:#3fb950; --bad:#ff6b6b; --accent:#4c9aff; }
  }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:16px/1.45 system-ui, -apple-system, sans-serif; }
  main { max-width:560px; margin:0 auto; padding:16px; }
  h1 { font-size:22px; margin:8px 0 2px; }
  .sub { color:var(--muted); margin:0 0 16px; }
  section { background:var(--card); border:1px solid var(--line); border-radius:12px;
            padding:14px 16px; margin:0 0 14px; }
  h2 { font-size:16px; margin:0 0 10px; }
  button { font:inherit; padding:8px 14px; margin:4px 6px 4px 0; border-radius:8px;
           border:1px solid var(--line); background:var(--card); color:var(--fg); cursor:pointer; }
  button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
  button:disabled { opacity:.5; cursor:default; }
  ul { list-style:none; padding:0; margin:0; }
  li { padding:6px 0; border-top:1px solid var(--line); }
  li:first-child { border-top:0; }
  .ok { color:var(--ok); } .bad { color:var(--bad); } .muted { color:var(--muted); }
  #msg { min-height:1.4em; color:var(--muted); }
</style>
</head>
<body>
<main>
  <h1 id="name">NowAirPlaying</h1>
  <p class="sub" id="sub">Loading…</p>
  <p id="msg"></p>

  <section>
    <h2>Amplifier</h2>
    <p id="amp" class="muted">…</p>
    <button id="ampConnect">Connect</button>
    <button id="ampDisconnect">Disconnect</button>
    <button id="audioRestart">Restart audio</button>
    <span class="owner"><button id="ampReconnect">Reconnect</button>
    <button id="ampForget">Forget</button></span>
  </section>

  <section class="owner">
    <h2>Pair the amplifier</h2>
    <p class="muted">Put the amplifier in pairing mode on the Anthem+ screen, then press Scan.</p>
    <button class="primary" id="scan">Scan</button>
    <ul id="found"></ul>
  </section>

  <section class="owner">
    <h2>Phones</h2>
    <button id="pairPhone">Let a phone pair (2 minutes)</button>
    <p id="pairing" class="muted"></p>
    <ul id="phones"></ul>
  </section>

  <section>
    <h2>Health</h2>
    <ul id="checks"><li class="muted">Checking…</li></ul>
  </section>
</main>
<script>
const API = "/api/v2";
const $ = (id) => document.getElementById(id);
let claimed = false;

function say(text) { $("msg").textContent = text || ""; }

async function call(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(API + path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.message || data.error || r.status);
  return data;
}

async function act(label, fn) {
  say(label + "…");
  try { await fn(); say(label + ": done"); }
  catch (e) { say(label + ": " + e.message); }
  refresh();
}

function li(html) { const el = document.createElement("li"); el.innerHTML = html; return el; }
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

async function refresh() {
  const info = await call("GET", "/info").catch(() => null);
  if (!info) { $("sub").textContent = "The node is not answering."; return; }
  claimed = info.state === "claimed";
  $("name").textContent = info.name;
  $("sub").textContent = claimed
    ? "Managed by Home Assistant at " + (info.claimed_by || "your Home Assistant")
    : "Version " + info.version;
  document.querySelectorAll(".owner").forEach(el => el.hidden = claimed);
  const amp = info.amp;
  $("amp").textContent = amp
    ? amp.name + " — " + (!amp.connected ? "not connected"
                          : amp.audio === false ? "connected, but no audio link" : "connected")
    : "No amplifier paired yet.";
  $("ampConnect").disabled = $("ampDisconnect").disabled = !amp;
  if (!claimed) {
    const st = await call("GET", "/state").catch(() => null);
    if (st) {
      const p = st.phones;
      $("pairing").textContent = p.pairing.open ? "Open until " + p.pairing.until : "";
      const list = $("phones"); list.replaceChildren();
      if (!p.devices.length) list.append(li('<span class="muted">No phones paired.</span>'));
      for (const d of p.devices) {
        const el = li(esc(d.name || d.mac) + " — " + (d.connected ? "connected" : "not connected") + " ");
        for (const [label, fn] of [
          ["Connect", () => call("POST", "/phones/" + d.mac + "/connect")],
          ["Disconnect", () => call("POST", "/phones/" + d.mac + "/disconnect")],
          ["Forget", () => call("DELETE", "/phones/" + d.mac)]]) {
          const b = document.createElement("button"); b.textContent = label;
          b.onclick = () => act(label, fn); el.append(b);
        }
        list.append(el);
      }
    }
  }
  const v = await call("GET", "/verify").catch(() => null);
  if (v) {
    const list = $("checks"); list.replaceChildren();
    for (const c of v.checks) {
      list.append(li('<span class="' + (c.ok ? "ok" : "bad") + '">' + (c.ok ? "✓" : "✗") +
                     "</span> " + esc(c.id) + ' <span class="muted">' + esc(c.detail) + "</span>"));
    }
  }
}

async function showFound() {
  const f = await call("GET", "/amp/found").catch(() => ({ devices: [] }));
  const list = $("found"); list.replaceChildren();
  for (const d of f.devices) {
    const el = li(esc(d.name || d.mac) + (d.likely_amp ? " (likely the amplifier) " : " "));
    const b = document.createElement("button"); b.textContent = "Pair"; b.className = "primary";
    b.onclick = () => act("Pairing " + (d.name || d.mac), () => call("POST", "/amp/pair", { mac: d.mac }));
    el.append(b); list.append(el);
  }
  if (f.scanning) setTimeout(showFound, 2000);
}

$("ampConnect").onclick = () => act("Connect", () => call("POST", "/amp/connect"));
$("ampDisconnect").onclick = () => act("Disconnect", () => call("POST", "/amp/disconnect"));
$("ampReconnect").onclick = () => act("Reconnect", () => call("POST", "/amp/reconnect"));
$("audioRestart").onclick = async () => {
  say("Restarting audio…");
  try {
    await call("POST", "/audio/restart");
    say("Restarting audio: the sound comes back in about 10 to 30 seconds");
  } catch (e) { say("Restart audio: " + e.message); }
};
$("ampForget").onclick = () => { if (confirm("Forget the amplifier?")) act("Forget", () => call("POST", "/amp/forget")); };
$("scan").onclick = () => act("Scan", async () => { await call("POST", "/amp/scan", { seconds: 20 }); showFound(); });
$("pairPhone").onclick = () => act("Pairing window", () => call("POST", "/phones/pairing", { seconds: 120 }));

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""
