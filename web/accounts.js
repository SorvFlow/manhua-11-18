/* Application accounts and private workspaces. No shared access-token prompt. */
(() => {
  "use strict";
  const nativeFetch = window.fetch.bind(window);
  // Remove the legacy shared-browser manuscript cache and shared access token.
  try { localStorage.removeItem("inkstone.activeJob"); sessionStorage.removeItem("inkstone_ui_token"); } catch (_) {}
  const t = (value) => window.InkstoneI18n?.translate(value) || value;
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (s) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[s]));
  let user = null;
  let csrf = "";
  let setupRequired = false;
  let currentView = "";
  const dialog = document.createElement("dialog");
  dialog.className = "account-dialog";
  dialog.id = "accountDialog";
  dialog.setAttribute("aria-label", "Account");
  document.body.appendChild(dialog);
  const button = document.getElementById("accountToggle");

  function syncTabs() {
    try { localStorage.setItem("sorvflow.authSync", String(Date.now()) + Math.random()); } catch (_) {}
  }
  window.addEventListener("storage", (event) => {
    if (event.key === "sorvflow.authSync") window.location.reload();
  });

  function updateButton() {
    button.textContent = user ? user.username : "Sign in";
    button.title = user ? "Account and my tasks" : "Sign in";
    button.setAttribute("aria-label", button.title);
  }

  window.fetch = async (input, init = {}) => {
    const actor = user?.id;
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    const internal = url.origin === location.origin && url.pathname.startsWith("/api/");
    const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
    const method = String(init.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    if (internal && !["GET", "HEAD", "OPTIONS"].includes(method) && csrf) headers.set("X-CSRF-Token", csrf);
    const result = await nativeFetch(input, { ...init, headers });
    if (internal && actor && actor !== user?.id) throw new Error(t("Session changed. Please reopen the task."));
    if (internal && result.status === 401 && !url.pathname.startsWith("/api/auth/")) {
      user = null;
      csrf = "";
      updateButton();
      window.dispatchEvent(new Event("sorvflow:signed-out"));
      open("login");
    }
    return result;
  };

  async function api(path, payload) {
    const response = await window.fetch(path, payload === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    return result;
  }

  function shell(title, description = "") {
    dialog.innerHTML = `<div class="account-dialog-head"><div><h2>${esc(title)}</h2>${description ? `<p>${esc(description)}</p>` : ""}</div><button class="account-dialog-close" type="button" aria-label="Close">×</button></div><div id="accountContent"></div><div id="accountMessage" class="account-message" role="status"></div>`;
    dialog.querySelector(".account-dialog-close").onclick = () => dialog.close();
    return dialog.querySelector("#accountContent");
  }

  function message(value, error = false) {
    const element = dialog.querySelector("#accountMessage");
    if (!element) return;
    element.textContent = value;
    element.classList.toggle("error", error);
  }

  async function submit(form, action) {
    const buttons = form.querySelectorAll("button");
    buttons.forEach((b) => { b.disabled = true; });
    message("");
    try { await action(new FormData(form)); }
    catch (error) { message(error.message, true); }
    finally { buttons.forEach((b) => { b.disabled = false; }); }
  }

  function login() {
    currentView = "login";
    const content = shell("Sign in to sorvflow", "Your projects, tasks and files belong to your account.");
    if (setupRequired) {
      content.innerHTML = `<div class="account-setup"><h3>Administrator setup required</h3><p>No accounts exist yet. Create the first administrator in the server terminal, then return here.</p><code>uv run inkstone accounts create-admin --username admin</code></div>`;
      return;
    }
    content.innerHTML = `<form id="loginForm"><label>Username<input name="username" autocomplete="username" required minlength="3" maxlength="32"></label><label>Password<input name="password" type="password" autocomplete="current-password" required maxlength="256"></label><button class="btn btn-primary" type="submit">Sign in</button></form><p>Ask your administrator for an account. Passwords are never shared with the model provider.</p>`;
    const form = content.querySelector("form");
    form.onsubmit = (event) => {
      event.preventDefault();
      submit(form, async (data) => {
        const result = await api("/api/auth/login", { username: data.get("username"), password: data.get("password") });
        user = result.user;
        csrf = result.csrf;
        updateButton();
        syncTabs();
        dialog.close();
        window.dispatchEvent(new Event("sorvflow:signed-in"));
        if (user.must_change_password) open("password");
      });
    };
  }

  function profile() {
    currentView = "profile";
    const content = shell("Account", user.username);
    content.innerHTML = `<div class="account-actions"><button class="btn btn-primary" data-view="tasks">My tasks</button><button class="btn btn-secondary" data-view="password">Change password</button>${user.role === "admin" ? '<button class="btn btn-secondary" data-view="admin">Manage accounts</button>' : ""}<button class="btn btn-secondary" id="logoutButton">Sign out</button></div>`;
    content.querySelectorAll("[data-view]").forEach((b) => { b.onclick = () => open(b.dataset.view); });
    content.querySelector("#logoutButton").onclick = async () => {
      try { await api("/api/auth/logout", {}); syncTabs(); window.location.reload(); }
      catch (error) { message(error.message, true); }
    };
  }

  function password() {
    currentView = "password";
    const content = shell("Change password", user.must_change_password ? "Change your initial password before creating comics." : "Updating your password signs out all sessions.");
    content.innerHTML = `<form><label>Current password<input name="current" type="password" autocomplete="current-password" required maxlength="256"></label><label>New password<input name="next" type="password" autocomplete="new-password" required minlength="10" maxlength="256"></label><label>Confirm new password<input name="confirm" type="password" autocomplete="new-password" required minlength="10" maxlength="256"></label><button class="btn btn-primary" type="submit">Save password</button></form>`;
    const form = content.querySelector("form");
    form.onsubmit = (event) => {
      event.preventDefault();
      submit(form, async (data) => {
        if (data.get("next") !== data.get("confirm")) throw new Error(t("Passwords do not match."));
        await api("/api/auth/password", { current_password: data.get("current"), new_password: data.get("next") });
        user = null;
        csrf = "";
        syncTabs();
        window.location.reload();
      });
    };
  }

  async function tasks() {
    currentView = "tasks";
    const content = shell("My tasks", "Queued tasks start automatically. Only your own projects appear here.");
    content.innerHTML = `<p>Loading…</p>`;
    try {
      const rows = await api("/api/jobs");
      if (currentView !== "tasks") return;
      const names = { queued: "Queued", running: "Running", done: "Completed", paused: "Paused", error: "Error", cancelled: "Cancelled" };
      content.innerHTML = '<button class="btn btn-secondary" id="refreshTasks">Refresh tasks</button>' + (rows.length ? rows.map((job) => `<div class="account-row"><div class="account-row-main"><strong>${esc(job.project_id)}</strong><small>${esc(t(names[job.status] || job.status))}${job.queue_position ? ` · ${esc(t("Queue position"))} ${job.queue_position}` : ""}</small></div><div class="account-actions"><button class="btn btn-secondary" data-open-job="${esc(job.job_id)}">Open task</button>${["queued", "running"].includes(job.status) ? `<button class="btn btn-secondary" data-stop-job="${esc(job.job_id)}">Stop</button>` : ""}</div></div>`).join("") : '<div class="account-empty">No tasks yet. Create your first comic.</div>');
      content.querySelector("#refreshTasks").onclick = tasks;
      content.querySelectorAll("[data-open-job]").forEach((b) => { b.onclick = () => {
        dialog.close();
        window.dispatchEvent(new CustomEvent("sorvflow:open-job", { detail: b.dataset.openJob }));
      }; });
      content.querySelectorAll("[data-stop-job]").forEach((b) => { b.onclick = async () => {
        b.disabled = true;
        try { await api(`/api/job/${b.dataset.stopJob}/stop`, {}); await tasks(); }
        catch (error) { message(error.message, true); b.disabled = false; }
      }; });
    } catch (error) { message(error.message, true); }
  }

  async function admin() {
    currentView = "admin";
    const content = shell("Manage accounts", "New accounts must change their initial password. Disabling an account revokes its sessions and stops its tasks.");
    content.innerHTML = `<h3>Create account</h3><form id="createAccount"><label>Username<input name="username" autocomplete="off" required minlength="3" maxlength="32" pattern="[a-zA-Z0-9][a-zA-Z0-9_.-]{2,31}"></label><label>Initial password<input name="password" type="password" autocomplete="new-password" required minlength="10" maxlength="256"></label><label>Role<select name="role"><option value="user">Member</option><option value="admin">Administrator</option></select></label><button class="btn btn-primary" type="submit">Create account</button></form><h3>Account list</h3><div id="accountList"><p>Loading…</p></div><h3>Model API check</h3><p>This makes one text-model tool call and may incur a charge. It does not generate an image.</p><button class="btn btn-secondary" id="apiCheckButton">Test text API</button>`;
    const form = content.querySelector("form");
    form.onsubmit = (event) => { event.preventDefault(); submit(form, async (data) => {
      await api("/api/admin/users", Object.fromEntries(data));
      form.reset();
      await loadUsers();
      message(t("Account created. Share the initial password privately."));
    }); };
    content.querySelector("#apiCheckButton").onclick = async (event) => {
      const target = event.currentTarget;
      target.disabled = true;
      message(t("Checking API…"));
      try { const result = await api("/api/admin/api-check", {}); message(result.message, !result.ok); }
      catch (error) { message(error.message, true); }
      finally { target.disabled = false; }
    };
    async function loadUsers() {
      const rows = await api("/api/admin/users");
      if (currentView !== "admin") return;
      const list = content.querySelector("#accountList");
      list.innerHTML = rows.map((row) => `<div class="account-row"><div class="account-row-main"><strong>${esc(row.username)}</strong><small>${esc(t(row.role === "admin" ? "Administrator" : "Member"))} · ${esc(t(row.active ? "Active" : "Disabled"))}</small></div><div class="account-actions">${row.id !== user.id ? `<button class="btn btn-secondary" data-user="${row.id}" data-active="${row.active ? "0" : "1"}">${row.active ? "Disable" : "Enable"}</button>` : ""}<button class="btn btn-secondary" data-reset="${row.id}">Reset password</button></div></div>`).join("");
      list.querySelectorAll("[data-active]").forEach((b) => { b.onclick = async () => {
        b.disabled = true;
        try { await api(`/api/admin/users/${b.dataset.user}/active`, { active: b.dataset.active === "1" }); await loadUsers(); }
        catch (error) { message(error.message, true); b.disabled = false; }
      }; });
      list.querySelectorAll("[data-reset]").forEach((b) => { b.onclick = () => {
        const account = rows.find((row) => row.id === b.dataset.reset);
        resetPassword(account);
      }; });
    }
    try { await loadUsers(); } catch (error) { message(error.message, true); }
  }

  function resetPassword(account) {
    currentView = "reset";
    const content = shell("Reset password", account.username);
    content.innerHTML = `<p>The account will be signed out and must change this initial password on its next login.</p><form><label>Initial password<input name="password" type="password" autocomplete="new-password" required minlength="10" maxlength="256"></label><button class="btn btn-primary" type="submit">Reset password</button></form>`;
    const form = content.querySelector("form");
    form.onsubmit = (event) => { event.preventDefault(); submit(form, async (data) => {
      await api(`/api/admin/users/${account.id}/password`, { password: data.get("password") });
      if (account.id === user.id) { syncTabs(); window.location.reload(); }
      else { await admin(); message(t("Password reset. Share the initial password privately.")); }
    }); };
  }

  function open(view) {
    if (!user) view = "login";
    else if (user.must_change_password) view = "password";
    ({ login, profile, password, tasks, admin })[view || "profile"]();
    if (!dialog.open) dialog.showModal();
  }
  button.onclick = () => open(user ? "profile" : "login");
  const ready = (async () => {
    try {
      const response = await nativeFetch("/api/auth/session");
      if (!response.ok) return;
      const result = await response.json();
      user = result.user || null;
      csrf = result.csrf || "";
      setupRequired = !!result.setup_required;
      updateButton();
      if (user?.must_change_password) open("password");
    } catch (_) { updateButton(); }
  })();
  window.SorvflowAccounts = { ready, open, get user() { return user; } };
})();
