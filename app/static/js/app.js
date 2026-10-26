// Agrynex dashboard. Each page calls the same JSON API as the mobile app.

const page = document.body.dataset.page;
const isAdmin = document.body.dataset.role === "admin";

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 401) {
    location.href = "/login?next=" + encodeURIComponent(location.pathname);
    throw new Error("Not signed in");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

let toastTimer;
function toast(message) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.style.display = "block";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.style.display = "none"), 4000);
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children) if (c != null) node.append(c);
  return node;
}

function formatTime(iso) {
  return new Date(iso).toLocaleString();
}

function timeAgo(iso) {
  if (!iso) return "never";
  const s = Math.round((Date.now() - new Date(iso)) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return formatTime(iso);
}

function describeAlert(alert) {
  const labels = [...new Set(alert.detections.map((d) => (d.kind === "human" ? "person" : d.label)))];
  return labels.join(", ");
}

// --- Alert cards (dashboard and alerts page) ---

function alertCard(alert, onDelete) {
  const media = el("div", { class: "media" });
  showMedia(media, alert, "snapshot");

  const actions = el("div", { class: "actions" });
  if (alert.snapshot_status !== "none") {
    actions.append(el("button", { onclick: () => showMedia(media, alert, "snapshot") }, "Photo"));
  }
  if (alert.clip_status !== "none") {
    actions.append(el("button", { onclick: () => showMedia(media, alert, "clip") }, "Clip"));
  }
  actions.append(el("button", {
    class: "danger",
    onclick: async () => {
      if (!confirm("Delete this alert and its media?")) return;
      try {
        await api("DELETE", `/api/alerts/${alert.id}`);
        onDelete(alert.id);
      } catch (e) { toast(e.message); }
    },
  }, "Delete"));

  return el("div", { class: "card alert-item", "data-id": alert.id },
    media,
    el("div", { class: "body" },
      el("strong", {}, describeAlert(alert)),
      el("div", { class: "muted small" }, formatTime(alert.created_at)),
      actions,
    ),
  );
}

function showMedia(container, alert, kind) {
  container.replaceChildren();
  const status = alert[`${kind}_status`];
  const url = alert[`${kind}_url`];
  if (status === "ready" && kind === "snapshot") {
    container.append(el("img", { src: url, alt: "Alert snapshot" }));
  } else if (status === "ready" && kind === "clip") {
    container.append(el("video", { src: url, controls: "", autoplay: "", playsinline: "" }));
  } else {
    const text = { pending: "Uploading…", failed: "Upload failed", none: "Not recorded" }[status];
    container.append(el("span", {}, `${kind === "clip" ? "Clip" : "Photo"}: ${text}`));
  }
}

function upsertAlert(list, alert, onDelete, prepend = true) {
  const card = alertCard(alert, onDelete);
  const existing = list.querySelector(`[data-id="${alert.id}"]`);
  if (existing) existing.replaceWith(card);
  else if (prepend) list.prepend(card);
  else list.append(card);
}

function connectSocket() {
  // The session cookie signs the socket in.
  return io("/client", { transports: ["websocket"] });
}

// --- Dashboard ---

function initDashboard() {
  const pill = document.getElementById("device-pill");
  const detail = document.getElementById("device-detail");
  const inputs = Object.fromEntries(
    [...document.querySelectorAll("[data-control]")].map((i) => [i.dataset.control, i]),
  );
  const list = document.getElementById("alert-list");
  const liveBox = document.getElementById("live");
  const liveButton = document.getElementById("live-toggle");
  let snapshot = null;
  let watching = false;
  let frameUrl = null;

  function outputDetail(output) {
    if (!output || !output.on) return "Off";
    const how = output.manual ? "Switched on by hand" : "Running after a detection";
    const left = output.off_at ? Math.max(0, Math.round(output.off_at - Date.now() / 1000)) : null;
    return left == null ? how : `${how}, ${left}s left`;
  }

  function render() {
    if (!snapshot) return;
    const { device, settings } = snapshot;
    const state = device.state || {};
    if (!device.online) {
      pill.className = "pill off";
      pill.textContent = "Offline";
      detail.textContent = `Last seen ${timeAgo(device.last_seen)}`;
    } else if (!state.system_on) {
      pill.className = "pill warn";
      pill.textContent = "Online · system off";
      detail.textContent = "Detection and camera are paused";
    } else if (!settings.animal_detection && !settings.human_detection) {
      pill.className = "pill warn";
      pill.textContent = "Online · detection off";
      detail.textContent = "Animal and person detection are both off";
    } else {
      pill.className = "pill on";
      pill.textContent = "Online · watching";
      detail.textContent = state.camera_ok === false ? "Camera isn't working" :
        state.last_detection_at ? `Last detection ${timeAgo(state.last_detection_at)}` : "No detections yet";
    }
    inputs.system.checked = settings.system_on;
    inputs.animal_detection.checked = settings.animal_detection;
    inputs.human_detection.checked = settings.human_detection;
    for (const name of ["siren", "sprinkler"]) {
      inputs[name].checked = !!(state[name] && state[name].on);
      inputs[name].disabled = !device.online;
      document.getElementById(`${name}-detail`).textContent =
        device.online ? outputDetail(state[name]) : "Pi is offline";
    }
    renderLive();
  }

  function liveProblem() {
    if (!snapshot) return "Loading…";
    const { device, settings } = snapshot;
    if (!settings.live_view_enabled) return "Live view is turned off in Settings";
    if (!device.online) return "The Raspberry Pi is offline";
    if (!settings.system_on) return "The system is off, so the camera is paused";
    return null;
  }

  function renderLive() {
    const problem = liveProblem();
    liveButton.textContent = watching ? "Stop" : "Start";
    if (!watching || problem) {
      liveBox.replaceChildren(el("span", {}, problem || "Press Start to watch"));
      frameUrl && URL.revokeObjectURL(frameUrl);
      frameUrl = null;
    } else if (!frameUrl) {
      liveBox.replaceChildren(el("span", {}, "Waiting for video…"));
    }
  }

  for (const [target, input] of Object.entries(inputs)) {
    input.addEventListener("change", async () => {
      const on = input.checked;
      try {
        await api("POST", "/api/control", { target, on });
      } catch (e) {
        input.checked = !on;
        toast(e.message);
      }
    });
  }

  const socket = connectSocket();
  socket.on("status", (s) => { snapshot = s; render(); });
  socket.on("alert", (a) => { upsertAlert(list, a, removeCard); trimList(); });
  socket.on("alert_updated", (a) => list.querySelector(`[data-id="${a.id}"]`) && upsertAlert(list, a, removeCard));
  socket.on("connect", () => watching && socket.emit("live_start"));
  socket.on("frame", (data) => {
    if (!watching) return;
    const url = URL.createObjectURL(new Blob([data], { type: "image/jpeg" }));
    let img = liveBox.querySelector("img");
    if (!img) {
      img = el("img", { alt: "Live camera" });
      liveBox.replaceChildren(img);
    }
    img.src = url;
    frameUrl && URL.revokeObjectURL(frameUrl);
    frameUrl = url;
  });

  liveButton.addEventListener("click", () => {
    watching = !watching;
    socket.emit(watching ? "live_start" : "live_stop");
    renderLive();
  });

  function removeCard(id) {
    list.querySelector(`[data-id="${id}"]`)?.remove();
  }
  function trimList() {
    while (list.children.length > 6) list.lastChild.remove();
  }

  api("GET", "/api/alerts?limit=6").then(({ alerts }) => {
    alerts.forEach((a) => upsertAlert(list, a, removeCard, false));
    if (!alerts.length) list.append(el("p", { class: "muted" }, "No alerts yet."));
  }).catch((e) => toast(e.message));

  // Keep the "seconds left" and "last seen" text fresh.
  setInterval(render, 1000);
}

// --- Alerts page ---

function initAlerts() {
  const list = document.getElementById("alert-list");
  const more = document.getElementById("load-more");
  const empty = document.getElementById("empty");
  let nextBefore = null;

  function removeCard(id) {
    list.querySelector(`[data-id="${id}"]`)?.remove();
    empty.hidden = list.children.length > 0;
  }

  async function load() {
    const q = nextBefore ? `&before=${encodeURIComponent(nextBefore)}` : "";
    try {
      const data = await api("GET", `/api/alerts?limit=24${q}`);
      data.alerts.forEach((a) => upsertAlert(list, a, removeCard, false));
      nextBefore = data.next_before;
      more.hidden = !nextBefore;
      empty.hidden = list.children.length > 0;
    } catch (e) { toast(e.message); }
  }

  more.addEventListener("click", load);
  const socket = connectSocket();
  socket.on("alert", (a) => { upsertAlert(list, a, removeCard); empty.hidden = true; });
  socket.on("alert_updated", (a) => list.querySelector(`[data-id="${a.id}"]`) && upsertAlert(list, a, removeCard));
  load();
}

// --- Settings ---

function initSettings() {
  const form = document.getElementById("settings-form");
  const fields = [...form.querySelectorAll("input")];

  api("GET", "/api/settings").then(({ settings }) => {
    for (const input of fields) {
      if (input.type === "checkbox") input.checked = settings[input.name];
      else input.value = settings[input.name];
      input.disabled = !isAdmin;
    }
  }).catch((e) => toast(e.message));

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const changes = {};
    for (const input of fields) {
      changes[input.name] = input.type === "checkbox" ? input.checked : Number(input.value);
    }
    try {
      await api("PUT", "/api/settings", changes);
      toast("Settings saved");
    } catch (e) { toast(e.message); }
  });
}

// --- Users ---

function initUsers() {
  const rows = document.getElementById("user-rows");
  const form = document.getElementById("add-user");

  async function load() {
    const { users } = await api("GET", "/api/users");
    rows.replaceChildren(...users.map((u) => el("tr", {},
      el("td", {}, u.name || "—"),
      el("td", {}, u.email),
      el("td", {}, u.role),
      el("td", {},
        el("button", {
          onclick: async () => {
            const password = prompt(`New password for ${u.email} (at least 8 characters):`);
            if (!password) return;
            try {
              await api("POST", `/api/users/${u.id}/password`, { password });
              toast("Password changed");
            } catch (e) { toast(e.message); }
          },
        }, "Reset password"), " ",
        el("button", {
          class: "danger",
          onclick: async () => {
            if (!confirm(`Delete ${u.email}?`)) return;
            try {
              await api("DELETE", `/api/users/${u.id}`);
              load();
            } catch (e) { toast(e.message); }
          },
        }, "Delete"),
      ),
    )));
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(form));
    try {
      await api("POST", "/api/users", data);
      form.reset();
      toast("User added");
      load();
    } catch (e) { toast(e.message); }
  });
  load().catch((e) => toast(e.message));
}

// --- Account ---

function initAccount() {
  const form = document.getElementById("change-password");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("POST", "/api/auth/change-password", Object.fromEntries(new FormData(form)));
      form.reset();
      toast("Password changed");
    } catch (e) { toast(e.message); }
  });
}

({
  dashboard: initDashboard,
  alerts: initAlerts,
  settings: initSettings,
  users: initUsers,
  account: initAccount,
})[page]?.();
