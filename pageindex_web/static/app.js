import DOMPurify from "./vendor/purify.es.mjs";
import { marked } from "./vendor/marked.esm.js";

const state = {
  docs: [],
  conversations: [],
  selected: new Set(),
  activeDoc: null,
  currentConversationId: "",
  mode: "standard",
  taskTimers: new Map(),
  documentTasks: new Map(),
  taskStates: new Map(),
  expandedNodes: new Set(),
  activeOutlineNode: "",
  activeUploadController: null,
  uploadCancelled: false,
  currentAskId: "",
  currentAskController: null,
  activeRuns: new Map(),
  activeOutlineDocId: "",
  outlineStructureCache: new Map(),
  outlineLoadToken: 0,
  docFilter: "",
  docSort: "recent",
  uploadRetries: new Map(),
  renameTargetId: "",
  adminData: null,
  adminFilters: { users: "", documents: "", conversations: "" },
  adminPages: { users: 1, documents: 1, conversations: 1 },
  user: null,
  adminNotice: "",
};

const $ = (id) => document.getElementById(id);
const appShell = document.querySelector(".app-shell");
const chatPanel = $("chatPanel");
const documentPanel = $("documentPanel");
const docContextMenu = $("docContextMenu");
const historyList = $("historyList");
const docList = $("docList");
const treeView = $("treeView");
const messages = $("messages");
const confirmDialog = $("confirmDialog");
let confirmResolve = null;

function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2300);
}

function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderMarkdown(text) {
  const html = marked.parse(String(text ?? ""), { async: false, breaks: false, gfm: true });
  return renderMath(DOMPurify.sanitize(html, { USE_PROFILES: { html: true } }));
}

function renderMath(html) {
  const katexRuntime = globalThis.katex || globalThis.window?.katex || globalThis.self?.katex;
  if (!katexRuntime || typeof document === "undefined") return html;
  const template = document.createElement("template");
  template.innerHTML = html;
  const skipped = new Set(["CODE", "PRE", "SCRIPT", "STYLE", "TEXTAREA"]);
  const walker = document.createTreeWalker(template.content, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (![...skipped].some((tag) => node.parentElement?.closest(tag.toLowerCase()))) nodes.push(node);
  }
  const pattern = /(\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)|(?<!\\)\$(?!\s)(?:\\.|[^\n$])+?(?<!\\)\$)/g;
  for (const node of nodes) {
    const text = node.nodeValue || "";
    if (!pattern.test(text)) continue;
    pattern.lastIndex = 0;
    const fragment = document.createDocumentFragment();
    let cursor = 0;
    for (const match of text.matchAll(pattern)) {
      const raw = match[0];
      const index = match.index || 0;
      if (index > cursor) fragment.appendChild(document.createTextNode(text.slice(cursor, index)));
      const displayMode = raw.startsWith("$$") || raw.startsWith("\\[");
      const expr = raw.startsWith("$$")
        ? raw.slice(2, -2)
        : raw.startsWith("\\[")
          ? raw.slice(2, -2)
          : raw.startsWith("\\(")
            ? raw.slice(2, -2)
            : raw.slice(1, -1);
      const span = document.createElement(displayMode ? "div" : "span");
      span.className = displayMode ? "math-block" : "math-inline";
      try {
        span.innerHTML = katexRuntime.renderToString(expr, { displayMode, throwOnError: false, output: "html" });
      } catch {
        span.textContent = raw;
      }
      fragment.appendChild(span);
      cursor = index + raw.length;
    }
    if (cursor < text.length) fragment.appendChild(document.createTextNode(text.slice(cursor)));
    node.replaceWith(fragment);
  }
  return template.innerHTML;
}

globalThis.LumenIndexMarkdown = { render: renderMarkdown };
if (typeof window !== "undefined") window.LumenIndexMarkdown = globalThis.LumenIndexMarkdown;
if (typeof self !== "undefined") self.LumenIndexMarkdown = globalThis.LumenIndexMarkdown;
if (typeof document !== "undefined") {
  document.documentElement.dataset.markdownMath = renderMarkdown("$x^2$").includes("katex") ? "ready" : "missing";
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    const text = await res.text();
    let message = text || res.statusText;
    try {
      const payload = JSON.parse(text);
      message = payload.detail?.error || payload.error || payload.detail || message;
    } catch {}
    if (res.status === 401) showAuth();
    throw new Error(String(message));
  }
  return res.json();
}

async function sha256File(file) {
  if (!globalThis.crypto?.subtle) return "";
  const buffer = await file.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return "sha256:" + [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, "0")).join("");
}

function sizeText(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let value = n;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(value >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function statusLabel(status) {
  return ({
    accepted: "Accepted",
    cached: "Cached",
    cancelled: "Cancelled",
    completed: "Completed",
    failed: "Failed",
    hashing: "Hashing",
    indexed: "Indexed",
    indexing: "Indexing",
    pending: "Pending",
    uploading: "Uploading",
  })[status] || String(status || "Unknown");
}

function taskMessageLabel(message, status) {
  const text = String(message || "");
  const exact = {
    "\u5206\u6790\u76ee\u5f55\u7ed3\u6784": "Analyzing document outline",
    "\u7d22\u5f15\u5b8c\u6210": "Indexing completed",
    "\u7d22\u5f15\u5931\u8d25": "Indexing failed",
    "\u7d22\u5f15\u5df2\u53d6\u6d88": "Indexing cancelled",
    "\u5df2\u53d6\u6d88": "Cancelled",
  };
  if (exact[text]) return exact[text];
  if (text.includes("\u63d0\u53d6") || text.includes("\u8bfb\u53d6")) return "Extracting document content";
  if (text.includes("\u76ee\u5f55")) return "Analyzing document outline";
  if (text.includes("\u7f13\u5b58")) return "Writing cache";
  if (text.includes("\u5b8c\u6210")) return "Completed";
  if (text.includes("\u5931\u8d25")) return "Failed";
  if (text.includes("\u53d6\u6d88")) return "Cancelled";
  return text || statusLabel(status);
}

function traceTitleLabel(text) {
  const value = String(text || "");
  const exact = {
    "\u6574\u7406\u5bf9\u8bdd\u4e0a\u4e0b\u6587": "Compacting conversation context",
    "\u4e0a\u4e0b\u6587\u538b\u7f29\u5931\u8d25": "Context compaction failed",
    "\u6a21\u578b\u4e0a\u4e0b\u6587\u8d85\u9650\uff0c\u5df2\u538b\u7f29\u540e\u91cd\u8bd5": "Model context exceeded. Retrying after compaction.",
    "\u6700\u7ec8\u7b54\u6848\u524d\u6574\u7406\u4e0a\u4e0b\u6587": "Compacting context before final answer",
    "\u6700\u7ec8\u7b54\u6848\u524d\u4e0a\u4e0b\u6587\u538b\u7f29\u5931\u8d25": "Final-answer context compaction failed",
  };
  return exact[value] || value;
}

function appConfirm({ title = "Confirm action", message = "Continue?", okText = "Confirm", danger = false } = {}) {
  $("confirmTitle").textContent = title;
  $("confirmMessage").textContent = message;
  $("confirmOkBtn").textContent = okText;
  $("confirmOkBtn").classList.toggle("danger-primary", Boolean(danger));
  if (confirmDialog.open) confirmDialog.close("cancel");
  confirmDialog.showModal();
  return new Promise((resolve) => {
    confirmResolve = resolve;
  });
}

function finishConfirm(value) {
  if (confirmResolve) {
    confirmResolve(value);
    confirmResolve = null;
  }
}

function showAuth() {
  $("authScreen").classList.add("open");
}

function hideAuth() {
  $("authScreen").classList.remove("open");
  setAuthError("");
}

function setAuthError(message = "") {
  const target = $("authError");
  target.textContent = message;
  target.hidden = !message;
}

function setRegisterError(message = "") {
  const target = $("registerError");
  target.textContent = message;
  target.hidden = !message;
}

function setUser(user) {
  state.user = user || null;
  $("currentUserLabel").textContent = user ? user.username : "";
  const showManage = Boolean(user?.is_admin);
  $("manageBtn").hidden = !showManage;
  $("manageBtn").setAttribute("aria-hidden", showManage ? "false" : "true");
}

async function checkAuth() {
  const data = await api("/api/auth/me");
  if (!data.authenticated) {
    showAuth();
    return false;
  }
  setUser(data.user);
  hideAuth();
  return true;
}

async function submitAuth(action, form) {
  setAuthError("");
  const body = Object.fromEntries(new FormData(form).entries());
  try {
    const data = await api(`/api/auth/${action}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
    setUser(data.user);
    hideAuth();
    await startAppData();
  } catch (err) {
    setAuthError(err.message || "Sign in failed");
    throw err;
  }
}

function openRegisterDialog(action) {
  setRegisterError("");
  const isAdmin = action === "register-admin";
  $("registerDialog").dataset.authAction = action;
  $("registerTitle").textContent = isAdmin ? "Create admin account" : "Create account";
  $("registerHelp").textContent = isAdmin
    ? "Create one of up to three admin accounts."
    : "Create a private workspace account.";
  $("submitRegisterBtn").textContent = isAdmin ? "Create admin account" : "Create account";
  $("registerForm").reset();
  $("registerDialog").showModal();
}

async function submitRegister(form) {
  setRegisterError("");
  const body = Object.fromEntries(new FormData(form).entries());
  if (body.password !== body.confirm_password) {
    setRegisterError("Passwords do not match");
    return;
  }
  const action = $("registerDialog").dataset.authAction || "register";
  try {
    const data = await api(`/api/auth/${action}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
    $("registerDialog").close();
    setUser(data.user);
    hideAuth();
    await startAppData();
  } catch (err) {
    setRegisterError(err.message || "Account creation failed");
  }
}

async function logout() {
  await api("/api/auth/logout", { method: "POST" }).catch(() => {});
  for (const run of state.activeRuns.values()) run.controller?.abort();
  state.activeRuns.clear();
  syncRunControls();
  setUser(null);
  newChat();
  state.docs = [];
  state.conversations = [];
  $("manageBtn").hidden = true;
  if ($("adminDialog").open) $("adminDialog").close();
  renderDocs();
  renderHistory();
  showAuth();
}

async function openAdmin() {
  state.adminNotice = null;
  $("adminContent").innerHTML = `<div class="doc-meta">Loading admin assets...</div>`;
  $("adminDialog").showModal();
  await loadAdminOverview();
}

async function loadAdminOverview() {
  const data = await api("/api/admin/overview");
  state.adminData = data;
  renderAdminOverview();
}

function adminNoticeHtml() {
  const notice = state.adminNotice;
  if (!notice) return "";
  const message = typeof notice === "string" ? notice : notice.message;
  const secret = typeof notice === "object" ? notice.secret : "";
  return `
    <div class="admin-notice" role="status">
      <div class="admin-notice-row">
        <div class="admin-notice-main">${escapeHtml(message || "")}</div>
        ${secret ? `<button class="copy-secret-btn" type="button" data-admin-action="copy-secret" data-secret="${escapeHtml(secret)}">Copy password</button>` : ""}
      </div>
    </div>`;
}

function adminFilteredRows(kind, rows, predicate) {
  const query = String(state.adminFilters[kind] || "").trim().toLowerCase();
  const filtered = query ? rows.filter((row) => predicate(row).toLowerCase().includes(query)) : rows;
  const pageSize = 8;
  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  state.adminPages[kind] = Math.min(Math.max(1, Number(state.adminPages[kind] || 1)), pageCount);
  const page = state.adminPages[kind];
  return { filtered, page, pageCount, rows: filtered.slice((page - 1) * pageSize, page * pageSize) };
}

function adminPager(kind, page, pageCount, total) {
  return `
    <div class="admin-pager">
      <span>${total} total · Page ${page} / ${pageCount}</span>
      <button type="button" data-admin-page="${kind}" data-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>Prev</button>
      <button type="button" data-admin-page="${kind}" data-page="${page + 1}" ${page >= pageCount ? "disabled" : ""}>Next</button>
    </div>`;
}

function renderAdminOverview(data = state.adminData || {}) {
  const users = data.users || [];
  const documents = data.documents || [];
  const conversations = data.conversations || [];
  const currentUserId = state.user?.id || "";
  const userPage = adminFilteredRows("users", users, (user) => `${user.username} ${user.is_admin ? "admin" : "user"} ${user.created_time}`);
  const docPage = adminFilteredRows("documents", documents, (doc) => `${doc.name} ${doc.owner_username} ${doc.status} ${doc.upload_time}`);
  const convPage = adminFilteredRows("conversations", conversations, (conversation) => `${conversation.title} ${conversation.owner_username} ${conversation.mode} ${conversation.updated_time}`);
  $("adminContent").innerHTML = `
    ${adminNoticeHtml()}
    <div class="admin-grid">
      <div class="admin-stat"><b>${users.length}</b><span>Users</span></div>
      <div class="admin-stat"><b>${documents.length}</b><span>Documents</span></div>
      <div class="admin-stat"><b>${conversations.length}</b><span>Conversations</span></div>
    </div>
    <section class="admin-section">
      <div class="admin-toolbar">
        <h3>Users</h3>
        <input id="adminFilter_users" type="search" placeholder="Filter users" value="${escapeHtml(state.adminFilters.users)}" data-admin-filter="users" />
      </div>
      <div class="admin-create-row">
        <label>New user<input id="adminCreateUsername" autocomplete="off" placeholder="username" /></label>
        <button class="primary" type="button" data-admin-action="create-user">Create user</button>
      </div>
      <table class="admin-table">
        <thead><tr><th>User</th><th>Role</th><th>Documents</th><th>Conversations</th><th>Created</th><th></th></tr></thead>
        <tbody>${userPage.rows.map((user) => `
          <tr>
            <td>${escapeHtml(user.username)}</td>
            <td><span class="role-pill ${user.is_admin ? "" : "user"}">${user.is_admin ? "Admin" : "User"}</span></td>
            <td>${Number(user.document_count || 0)}</td>
            <td>${Number(user.conversation_count || 0)}</td>
            <td>${escapeHtml(user.created_time || "")}</td>
            <td>${user.is_admin ? "" : `
              <div class="admin-actions">
                <button class="text-link" type="button" data-admin-action="reset-user-password" data-id="${escapeHtml(user.id)}">Reset password</button>
                ${user.id === currentUserId ? "" : `<button class="danger-link" type="button" data-admin-action="delete-user" data-id="${escapeHtml(user.id)}" data-username="${escapeHtml(user.username)}">Delete</button>`}
              </div>`}
            </td>
          </tr>`).join("") || `<tr><td colspan="6">No users</td></tr>`}</tbody>
      </table>
      ${adminPager("users", userPage.page, userPage.pageCount, userPage.filtered.length)}
    </section>
    <section class="admin-section">
      <div class="admin-toolbar">
        <h3>Documents</h3>
        <input id="adminFilter_documents" type="search" placeholder="Filter documents" value="${escapeHtml(state.adminFilters.documents)}" data-admin-filter="documents" />
      </div>
      <table class="admin-table">
        <thead><tr><th>Name</th><th>Owner</th><th>Status</th><th>Pages</th><th>Uploaded</th><th></th></tr></thead>
        <tbody>${docPage.rows.map((doc) => `
          <tr>
            <td>${escapeHtml(doc.name || "")}</td>
            <td>${escapeHtml(doc.owner_username || "")}</td>
            <td>${escapeHtml(statusLabel(doc.status))}</td>
            <td>${Number(doc.page_count || 0)}</td>
            <td>${escapeHtml(doc.upload_time || "")}</td>
            <td>${doc.owner_is_admin ? "" : `<button class="danger-link" type="button" data-admin-action="delete-document" data-id="${escapeHtml(doc.id)}">Delete</button>`}</td>
          </tr>`).join("") || `<tr><td colspan="6">No documents</td></tr>`}</tbody>
      </table>
      ${adminPager("documents", docPage.page, docPage.pageCount, docPage.filtered.length)}
    </section>
    <section class="admin-section">
      <div class="admin-toolbar">
        <h3>Conversations</h3>
        <input id="adminFilter_conversations" type="search" placeholder="Filter conversations" value="${escapeHtml(state.adminFilters.conversations)}" data-admin-filter="conversations" />
      </div>
      <table class="admin-table">
        <thead><tr><th>Title</th><th>Owner</th><th>Mode</th><th>Messages</th><th>Updated</th><th></th></tr></thead>
        <tbody>${convPage.rows.map((conversation) => `
          <tr>
            <td>${escapeHtml(conversation.title || "Untitled conversation")}</td>
            <td>${escapeHtml(conversation.owner_username || "")}</td>
            <td>${escapeHtml(conversation.mode || "")}</td>
            <td>${Number(conversation.message_count || 0)}</td>
            <td>${escapeHtml(conversation.updated_time || "")}</td>
            <td>${conversation.owner_is_admin ? "" : `<button class="danger-link" type="button" data-admin-action="delete-conversation" data-id="${escapeHtml(conversation.id)}">Delete</button>`}</td>
          </tr>`).join("") || `<tr><td colspan="6">No conversations</td></tr>`}</tbody>
      </table>
      ${adminPager("conversations", convPage.page, convPage.pageCount, convPage.filtered.length)}
    </section>
  `;
}

async function createAdminUser() {
  const input = $("adminCreateUsername");
  const username = input?.value.trim();
  if (!username) {
    state.adminNotice = { message: "Enter a username before creating a user." };
    await loadAdminOverview();
    return;
  }
  const data = await api("/api/admin/users", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ username }),
  });
  state.adminNotice = { message: `Created user "${data.user.username}". Temporary password: ${data.temporary_password}`, secret: data.temporary_password };
  await loadAdminOverview();
}

async function resetAdminUserPassword(id) {
  const ok = await appConfirm({
    title: "Reset password",
    message: "Reset this user's password and sign out their existing sessions?",
    okText: "Reset",
    danger: true,
  });
  if (!ok) return;
  const data = await api(`/api/admin/users/${encodeURIComponent(id)}/reset-password`, { method: "POST" });
  state.adminNotice = { message: `Password reset for "${data.user.username}". Temporary password: ${data.temporary_password}`, secret: data.temporary_password };
  await loadAdminOverview();
}

async function deleteAdminUser(id, username) {
  const ok = await appConfirm({
    title: "Delete user",
    message: `Delete user "${username}" and all of their documents and conversations? This cannot be undone.`,
    okText: "Delete",
    danger: true,
  });
  if (!ok) return;
  const data = await api(`/api/admin/users/${encodeURIComponent(id)}`, { method: "DELETE" });
  const docs = Number(data.deleted_assets?.documents || 0);
  const conversations = Number(data.deleted_assets?.conversations || 0);
  state.adminNotice = { message: `Deleted user "${username}" with ${docs} documents and ${conversations} conversations.` };
  await Promise.all([loadAdminOverview(), loadDocs(), loadConversations()]);
  toast("User deleted");
}

async function deleteAdminAsset(kind, id) {
  const ok = await appConfirm({
    title: kind === "document" ? "Delete document" : "Delete conversation",
    message: `Delete this ${kind}? This action affects another user's workspace and cannot be undone.`,
    okText: "Delete",
    danger: true,
  });
  if (!ok) return;
  await api(`/api/admin/${kind === "document" ? "documents" : "conversations"}/${encodeURIComponent(id)}`, { method: "DELETE" });
  if (kind === "document") {
    state.docs = state.docs.filter((doc) => doc.id !== id);
    state.selected.delete(id);
    renderDocs();
    renderAttachedDocs();
  } else if (state.currentConversationId === id) {
    newChat();
  }
  await Promise.all([loadAdminOverview(), loadDocs(), loadConversations()]);
  toast(`${kind === "document" ? "Document" : "Conversation"} deleted`);
}

function docById(id) {
  return state.docs.find((doc) => doc.id === id);
}

function visibleDocs() {
  const query = state.docFilter.trim().toLowerCase();
  const docs = query
    ? state.docs.filter((doc) => `${doc.name || ""} ${doc.status || ""} ${doc.original_name || ""}`.toLowerCase().includes(query))
    : [...state.docs];
  const sort = state.docSort;
  docs.sort((a, b) => {
    if (sort === "name") return String(a.name || "").localeCompare(String(b.name || ""));
    if (sort === "status") return String(a.status || "").localeCompare(String(b.status || "")) || String(a.name || "").localeCompare(String(b.name || ""));
    if (sort === "size") return Number(b.size || 0) - Number(a.size || 0);
    return String(b.upload_time || "").localeCompare(String(a.upload_time || ""));
  });
  return docs;
}

function newConversationId() {
  const bytes = new Uint8Array(8);
  if (globalThis.crypto?.getRandomValues) {
    crypto.getRandomValues(bytes);
    return "conv_" + [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  }
  return "conv_" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

function newAskId() {
  const bytes = new Uint8Array(8);
  if (globalThis.crypto?.getRandomValues) {
    crypto.getRandomValues(bytes);
    return "ask_" + [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  }
  return "ask_" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

function messageTitle(messagesForConversation) {
  const firstUser = (messagesForConversation || []).find((item) => item.role === "user" && item.content);
  return firstUser ? firstUser.content : "Untitled conversation";
}

async function loadInitialSettings() {
  try {
    const cfg = await api("/api/settings");
    setMode(cfg.deep_thinking ? "react" : "standard");
  } catch {}
}

async function loadDocs() {
  const activeId = state.activeOutlineDocId || state.activeDoc?.id || "";
  const data = await api("/api/documents");
  state.docs = data.documents || [];
  if (activeId) state.activeDoc = docById(activeId) || state.activeDoc;
  for (const doc of state.docs) {
    if (doc.status === "indexed") state.documentTasks.delete(doc.id);
  }
  renderDocs();
  renderAttachedDocs();
  updateChatMeta();
  refreshOpenOutline();
}

async function loadConversations() {
  const data = await api("/api/conversations?limit=100");
  state.conversations = data.conversations || [];
  renderHistory();
}

function renderHistory() {
  historyList.innerHTML = "";
  if (!state.conversations.length) {
    historyList.innerHTML = `<div class="history-sub">No saved conversations</div>`;
    return;
  }
  for (const conversation of state.conversations) {
    const item = document.createElement("div");
    const running = state.activeRuns.has(conversation.id);
    item.className = `history-item ${conversation.id === state.currentConversationId ? "active" : ""} ${running ? "running" : ""}`;
    item.innerHTML = `
      <button class="history-load" type="button">
        <div class="history-title">${escapeHtml(messageTitle(conversation.messages))}</div>
        <div class="history-sub">
          <span>${escapeHtml(conversation.mode || "standard")} · ${conversation.document_ids.length} document${conversation.document_ids.length === 1 ? "" : "s"}</span>
          ${running ? `<span class="running-pill"><i class="running-dot"></i>Running</span>` : ""}
        </div>
      </button>
      <button class="history-delete" type="button" title="Delete conversation" aria-label="Delete conversation">
        <svg viewBox="0 0 24 24"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v5M14 11v5"/></svg>
      </button>
    `;
    item.querySelector(".history-load").addEventListener("click", () => loadConversation(conversation.id).catch((err) => toast(err.message)));
    item.querySelector(".history-delete").addEventListener("click", () => deleteConversation(conversation.id).catch((err) => toast(err.message)));
    historyList.appendChild(item);
  }
}

async function deleteConversation(conversationId) {
  if (state.activeRuns.has(conversationId)) {
    toast("This conversation is still running");
    return;
  }
  const conversation = state.conversations.find((item) => item.id === conversationId);
  const title = messageTitle(conversation?.messages || []);
  const ok = await appConfirm({
    title: "Delete conversation",
    message: `Delete "${title}" from history?`,
    okText: "Delete",
    danger: true,
  });
  if (!ok) return;
  await api(`/api/conversations/${conversationId}`, { method: "DELETE" });
  state.conversations = state.conversations.filter((item) => item.id !== conversationId);
  if (state.currentConversationId === conversationId) newChat();
  renderHistory();
  toast("Conversation deleted");
}

async function loadConversation(conversationId) {
  const data = await api(`/api/conversations/${conversationId}`);
  const conversation = data.conversation;
  state.currentConversationId = conversation.id;
  state.selected = new Set(conversation.document_ids || []);
  setMode(conversation.mode === "react" ? "react" : "standard");
  renderConversationMessages(conversation.messages || []);
  renderAttachedDocs();
  renderHistory();
  updateChatMeta();
  const running = state.activeRuns.get(conversation.id);
  if (running?.messageEl && !messages.contains(running.messageEl)) {
    messages.appendChild(running.messageEl);
    messages.scrollTop = messages.scrollHeight;
  }
  syncRunControls();
  const firstDoc = [...state.selected].map(docById).find(Boolean);
  if (firstDoc) {
    state.activeDoc = firstDoc;
    if (appShell.classList.contains("outline-open")) {
      state.activeOutlineDocId = firstDoc.id;
      loadStructure(firstDoc.id, { preserve: true });
    }
  }
}

function newChat() {
  state.currentConversationId = "";
  state.selected.clear();
  state.activeDoc = null;
  state.activeOutlineNode = "";
  messages.innerHTML = `<div class="empty-state">Start a new chat by attaching documents or choosing one from the document list.</div>`;
  renderAttachedDocs();
  renderDocs();
  renderHistory();
  updateChatMeta();
  syncRunControls();
}

function renderConversationMessages(items) {
  messages.innerHTML = "";
  const displayItems = (items || []).filter((item) => item.role === "user" || item.role === "assistant" || item.type === "final" || item.type === "error");
  if (!displayItems.length) {
    messages.innerHTML = `<div class="empty-state">No messages in this conversation yet.</div>`;
    return;
  }
  for (const item of displayItems) {
    if (item.role === "user") addMessage("user", item.content || "", false);
    if (item.role === "assistant") {
      const el = addMessage("assistant", item.content || "", false);
      appendSources(el, item.results || [], item.pages || []);
    }
    if (item.type === "final") {
      const el = addMessage("assistant", item.answer || "", false);
      appendSources(el, item.results || [], item.pages || []);
    }
    if (item.type === "error") addMessage("system", item.error || "The run ended with an error.", false);
  }
  messages.scrollTop = messages.scrollHeight;
}

function updateChatMeta() {
  const count = state.selected.size;
  $("chatMeta").textContent = state.currentConversationId
    ? `${count} attached document${count === 1 ? "" : "s"}`
    : count ? `${count} attached document${count === 1 ? "" : "s"}` : "New conversation";
}

function syncRunControls() {
  const run = state.activeRuns.get(state.currentConversationId);
  state.currentAskId = run?.requestId || "";
  state.currentAskController = run?.controller || null;
  $("cancelAskBtn").hidden = !run;
}

function renderDocs() {
  docList.innerHTML = "";
  const docs = visibleDocs();
  if (!state.docs.length) {
    docList.innerHTML = `<div class="doc-meta">No documents yet</div>`;
    return;
  }
  if (!docs.length) {
    docList.innerHTML = `<div class="doc-meta">No documents match the current filter.</div>`;
    return;
  }
  for (const doc of docs) {
    const card = document.createElement("div");
    card.className = `doc-card ${state.activeDoc?.id === doc.id ? "active" : ""} ${state.activeOutlineDocId === doc.id ? "outline-visible" : ""}`;
    card.dataset.docId = doc.id;
    card.draggable = true;
    const taskId = state.documentTasks.get(doc.id) || "";
    const task = taskId ? state.taskStates.get(taskId) : null;
    const progress = task ? Number(task.progress || 0) : (taskId ? 5 : 0);
    const taskLabel = task ? taskMessageLabel(task.message, task.status) : "Indexing";
    card.innerHTML = `
      <button class="icon-btn outline-doc" title="${state.activeOutlineDocId === doc.id ? "Hide outline" : "Show outline"}" aria-label="${state.activeOutlineDocId === doc.id ? "Hide outline" : "Show outline"}"><svg viewBox="0 0 24 24"><path d="m9 18 6-6-6-6"/></svg></button>
      <div class="doc-main">
        <div class="doc-title" title="${escapeHtml(doc.name)}">${escapeHtml(doc.name)}</div>
        <div class="doc-sub"><span><i class="status ${doc.status}"></i> ${escapeHtml(statusLabel(doc.status))}</span><span>${sizeText(doc.size)}</span><span>${doc.page_count || 0} pages</span></div>
        ${taskId ? `<div class="doc-task-progress" data-doc-task="${escapeHtml(taskId)}"><span>${escapeHtml(taskLabel)}</span><div class="bar"><i style="--p:${progress}%"></i></div></div>` : ""}
      </div>
      <div class="doc-actions">
        <button class="icon-btn use-doc" title="Attach to chat" aria-label="Attach to chat"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg></button>
      </div>
    `;
    card.addEventListener("click", (event) => {
      if (event.target.closest("button")) return;
      state.activeDoc = doc;
      if (appShell.classList.contains("outline-open")) state.activeOutlineDocId = doc.id;
      renderDocs();
      if (appShell.classList.contains("outline-open")) loadStructure(doc.id, { preserve: true });
    });
    card.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      state.activeDoc = doc;
      renderDocs();
      showDocContextMenu(event.clientX, event.clientY, doc.id);
    });
    card.addEventListener("dragstart", (event) => {
      event.dataTransfer.effectAllowed = "copy";
      event.dataTransfer.setData("application/x-lumenindex-doc", doc.id);
      event.dataTransfer.setData("text/plain", doc.name || doc.id);
      card.classList.add("dragging");
    });
    card.addEventListener("dragend", () => card.classList.remove("dragging"));
    card.querySelector(".outline-doc").addEventListener("click", (event) => {
      event.stopPropagation();
      state.activeDoc = doc;
      renderDocs();
      openOutline(doc.id);
    });
    card.querySelector(".use-doc").addEventListener("click", () => {
      attachDocument(doc.id);
      state.activeDoc = doc;
      if (appShell.classList.contains("outline-open")) state.activeOutlineDocId = doc.id;
      renderDocs();
      if (appShell.classList.contains("outline-open")) loadStructure(doc.id, { preserve: true });
    });
    docList.appendChild(card);
  }
}

function showDocContextMenu(x, y, docId) {
  docContextMenu.dataset.docId = docId;
  docContextMenu.style.left = `${Math.min(x, window.innerWidth - 176)}px`;
  docContextMenu.style.top = `${Math.min(y, window.innerHeight - 148)}px`;
  docContextMenu.classList.add("open");
  docContextMenu.setAttribute("aria-hidden", "false");
}

function hideDocContextMenu() {
  docContextMenu.classList.remove("open");
  docContextMenu.setAttribute("aria-hidden", "true");
}

async function renameDocument(docId) {
  const doc = docById(docId);
  if (!doc) return;
  state.renameTargetId = docId;
  $("renameInput").value = doc.name || "";
  $("renameError").hidden = true;
  $("renameDialog").showModal();
}

async function submitRename(event) {
  event.preventDefault();
  const docId = state.renameTargetId;
  const doc = docById(docId);
  const name = $("renameInput").value.trim();
  const error = $("renameError");
  error.hidden = true;
  if (!doc || !docId) return;
  if (!name) {
    error.textContent = "Enter a document name.";
    error.hidden = false;
    return;
  }
  if (name === doc.name) {
    $("renameDialog").close();
    return;
  }
  await api(`/api/documents/${docId}`, { method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ name }) });
  $("renameDialog").close();
  state.renameTargetId = "";
  await loadDocs();
  toast("Document renamed");
}

async function reindexDocument(docId) {
  const doc = docById(docId);
  const data = await api(`/api/documents/${docId}/reindex`, { method: "POST" });
  state.documentTasks.set(docId, data.task_id);
  state.taskStates.set(data.task_id, { status: "pending", progress: 5, message: "Reindexing started" });
  ensureTaskRow(data.task_id, doc?.name || "Document", "Reindexing started", 5);
  renderDocs();
  toast("Reindexing started");
  pollTask(data.task_id, docId);
  await loadDocs();
}

async function deleteDocument(docId) {
  const doc = docById(docId);
  if (!doc) return;
  const ok = await appConfirm({
    title: "Delete document",
    message: `Delete "${doc.name}" and its index cache?`,
    okText: "Delete",
    danger: true,
  });
  if (!ok) return;
  await api(`/api/documents/${docId}`, { method: "DELETE" });
  state.docs = state.docs.filter((item) => item.id !== docId);
  state.selected.delete(docId);
  if (state.activeDoc?.id === docId) {
    state.activeDoc = null;
    closeOutline();
  }
  renderDocs();
  renderAttachedDocs();
  updateChatMeta();
  toast("Document deleted");
}

function attachDocument(docId) {
  if (!docId) return;
  state.selected.add(docId);
  renderAttachedDocs();
  updateChatMeta();
}

function detachDocument(docId) {
  state.selected.delete(docId);
  renderAttachedDocs();
  updateChatMeta();
}

function renderAttachedDocs() {
  const target = $("attachedDocs");
  target.innerHTML = "";
  for (const docId of state.selected) {
    const doc = docById(docId);
    if (!doc) continue;
    const kind = fileKind(doc.name);
    const tag = document.createElement("div");
    tag.className = "file-tag";
    tag.innerHTML = `
      <div class="file-tag-icon">${escapeHtml(kind.slice(0, 4))}</div>
      <div class="file-tag-main">
        <div class="file-tag-name" title="${escapeHtml(doc.name)}">${escapeHtml(doc.name)}</div>
        <div class="file-tag-sub">${escapeHtml(statusLabel(doc.status))}</div>
      </div>
      <button type="button" title="Remove" aria-label="Remove"><svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg></button>
    `;
    tag.querySelector("button").addEventListener("click", () => detachDocument(docId));
    target.appendChild(tag);
  }
}

function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll(".mode-btn").forEach((btn) => btn.classList.toggle("active", btn.dataset.mode === mode));
}

async function loadStructure(docId, options = {}) {
  const token = ++state.outlineLoadToken;
  const preserve = Boolean(options.preserve);
  try {
    const data = await api(`/api/documents/${docId}/structure`);
    if (token !== state.outlineLoadToken || (state.activeOutlineDocId && state.activeOutlineDocId !== docId)) return;
    state.activeDoc = data.document;
    state.activeOutlineDocId = data.document?.id || docId;
    const structure = data.structure || [];
    const cached = state.outlineStructureCache.get(docId);
    if (!structure.length && cached && preserve) {
      renderDocMeta(cached);
      renderTree(cached.structure || []);
      return;
    }
    state.outlineStructureCache.set(docId, data);
    renderDocMeta(data);
    renderTree(structure);
  } catch {
    if (token !== state.outlineLoadToken) return;
    const cached = state.outlineStructureCache.get(docId);
    if (cached && preserve) {
      renderDocMeta(cached);
      renderTree(cached.structure || []);
      return;
    }
    treeView.innerHTML = `<div class="doc-meta">Outline unavailable</div>`;
  }
}

function refreshOpenOutline(force = false) {
  if (!appShell.classList.contains("outline-open") || !state.activeOutlineDocId) return;
  const doc = docById(state.activeOutlineDocId);
  if (!doc) return;
  state.activeDoc = doc;
  const cached = state.outlineStructureCache.get(doc.id);
  if (cached && !force && treeView.querySelector(".tree-node")) return;
  if (cached && doc.status !== "indexed") {
    renderDocMeta(cached);
    renderTree(cached.structure || []);
    return;
  }
  if (doc.status === "indexed") loadStructure(doc.id, { preserve: true });
}

function renderDocMeta(data) {
  const doc = data.document || {};
  const metadata = data.metadata || {};
  const cache = data.cache || {};
  $("docMeta").innerHTML = `
    <div><b>${escapeHtml(doc.name || "Document")}</b></div>
    <div>${escapeHtml(metadata.index_strategy || doc.index_strategy || "indexed")} · ${doc.page_count || metadata.page_count || 0} pages · ${sizeText(doc.size || 0)}</div>
    <div>Fingerprint: ${escapeHtml(cache.fingerprint || doc.fingerprint || "n/a")}</div>
    <div>Index: ${escapeHtml(cache.index_version || metadata.version || "n/a")} · Schema ${escapeHtml(cache.cache_schema_version || metadata.cache_schema_version || "n/a")} · ${cache.is_current ? "Current cache" : "Cache needs refresh"}</div>
    <div>Cache: ${sizeText((cache.metadata_bytes || 0) + (cache.pages_bytes || 0))}</div>
  `;
}

function renderTree(nodes) {
  const filter = $("treeFilter").value.trim().toLowerCase();
  treeView.innerHTML = "";
  const root = document.createElement("div");
  for (const node of nodes) {
    const el = treeNode(node, filter);
    if (el) root.appendChild(el);
  }
  if (!root.children.length) {
    treeView.innerHTML = `<div class="doc-meta">No outline entries match the current filter.</div>`;
    return;
  }
  treeView.appendChild(root);
}

function nodeKey(node) {
  return String(node.node_id || node.section_path || node.title || "");
}

function treeNode(node, filter, depth = 0) {
  const title = node.title || "Untitled";
  const children = node.nodes || [];
  const childEls = children.map((child) => treeNode(child, filter, depth + 1)).filter(Boolean);
  if (filter && !title.toLowerCase().includes(filter) && !childEls.length) return null;
  const key = nodeKey(node);
  const expanded = filter || state.expandedNodes.has(key) || depth === 0;
  const wrap = document.createElement("div");
  wrap.className = "tree-node";
  const pages = node.pages || [node.start_index, node.end_index];
  wrap.innerHTML = `
    <button class="tree-row ${state.activeOutlineNode === key ? "active" : ""}" type="button" data-node-id="${escapeHtml(key)}">
      ${childEls.length ? `<span class="tree-toggle ${expanded ? "expanded" : ""}" role="button" aria-label="${expanded ? "Collapse" : "Expand"}"><svg viewBox="0 0 24 24"><path d="m9 18 6-6-6-6"/></svg></span>` : `<span class="tree-spacer"></span>`}
      <span class="tree-title">${escapeHtml(title)}</span>
      <span class="tree-page">P${pages[0] || 1}-${pages[1] || pages[0] || 1}</span>
    </button>`;
  const childWrap = document.createElement("div");
  childWrap.className = `tree-children ${expanded ? "" : "collapsed"}`;
  childEls.forEach((el) => childWrap.appendChild(el));
  if (childEls.length) wrap.appendChild(childWrap);
  const row = wrap.querySelector(".tree-row");
  row.addEventListener("click", (event) => {
    const clickedToggle = event.target.closest(".tree-toggle");
    if (clickedToggle && childEls.length) {
      if (state.expandedNodes.has(key)) state.expandedNodes.delete(key);
      else state.expandedNodes.add(key);
      renderTreeFromActiveDoc();
      return;
    }
    state.activeOutlineNode = key;
    renderTreeFromActiveDoc();
    loadOutlinePreview(node).catch((err) => toast(err.message));
  });
  return wrap;
}

function renderTreeFromActiveDoc() {
  const docId = state.activeOutlineDocId || state.activeDoc?.id || "";
  if (docId) loadStructure(docId, { preserve: true });
}

async function loadOutlinePreview(node) {
  if (!state.activeDoc) return;
  const pages = node.pages || [node.start_index, node.end_index];
  const data = await api("/api/tools/content", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      document_ids: [state.activeDoc.id],
      document_names: [state.activeDoc.name],
      node_id: node.node_id || "",
      pages: pages.filter(Boolean),
    }),
  });
  const pageRows = data.pages || [];
  $("outlinePreview").innerHTML = `
    <h3>${escapeHtml(node.title || "Selected section")}</h3>
    ${pageRows.length ? pageRows.map((page) => `<p><b>Page ${escapeHtml(page.page)}</b>: ${escapeHtml(page.content || "").slice(0, 700)}</p>`).join("") : `<p>No page content is available for this node.</p>`}
  `;
}

function openOutline(docId = "") {
  const doc = docById(docId) || state.activeDoc || [...state.selected].map(docById).find(Boolean) || state.docs.find((item) => item.status === "indexed");
  if (!doc) return;
  if (appShell.classList.contains("outline-open") && state.activeOutlineDocId === doc.id) {
    closeOutline();
    return;
  }
  state.activeOutlineDocId = doc.id;
  state.activeDoc = doc;
  appShell.classList.add("outline-open");
  renderDocs();
  loadStructure(doc.id, { preserve: true });
}

function closeOutline() {
  state.outlineLoadToken += 1;
  appShell.classList.remove("outline-open");
  state.activeOutlineDocId = "";
  renderDocs();
}

async function uploadFiles(fileList, options = {}) {
  const files = [...(fileList || [])];
  if (!files.length) return [];
  const attach = Boolean(options.attach);
  const controller = new AbortController();
  state.activeUploadController = controller;
  state.uploadCancelled = false;
  const rows = files.map((file) => ({ file, name: file.name, status: "hashing" }));
  const fileForName = new Map(rows.map((row) => [row.name, row.file]));
  $("uploadQueue").innerHTML = rows.map((row) => uploadRow(row.name, "Hashing", 18, "", "upload")).join("");
  const pending = [];
  const accepted = [];
  try {
    await Promise.all(rows.map(async (row, index) => {
      if (controller.signal.aborted || state.uploadCancelled) throw new DOMException("Upload cancelled", "AbortError");
      try {
        row.fingerprint = await sha256File(row.file);
        if (controller.signal.aborted || state.uploadCancelled) throw new DOMException("Upload cancelled", "AbortError");
        if (row.fingerprint) {
          const hit = await api(`/api/cache/${encodeURIComponent(row.fingerprint)}`, { signal: controller.signal });
          if (hit.hit && hit.document) {
            accepted.push({ name: row.name, status: "cached", document: hit.document });
            if (attach) attachDocument(hit.document.id);
            return;
          }
        }
      } catch (err) {
        if (err.name === "AbortError") throw err;
        row.fingerprint = "";
      }
      pending[index] = row.file;
    }));
    const pendingFiles = pending.filter(Boolean);
    if (!pendingFiles.length) {
      $("uploadQueue").innerHTML = accepted.map((item) => uploadRow(item.name, "Cached", 100)).join("");
      await loadDocs();
      clearUploadQueueWhenIdle();
      return accepted;
    }
    const fd = new FormData();
    pendingFiles.forEach((file) => fd.append("files", file));
    $("uploadQueue").innerHTML = [...accepted, ...pendingFiles.map((file) => ({ name: file.name, status: "uploading" }))]
      .map((item) => uploadRow(item.name, statusLabel(item.status), item.status === "cached" ? 100 : 35, "", item.status === "cached" ? "" : "upload"))
      .join("");
    const data = await api("/api/upload", { method: "POST", body: fd, signal: controller.signal });
    const items = [...accepted, ...(data.items || [])];
    $("uploadQueue").innerHTML = items.map((item) => {
      const failed = item.status === "failed";
      const retryId = failed ? registerUploadRetry(fileForName.get(item.name), attach) : "";
      return uploadRow(item.name, statusLabel(item.status), item.task_id ? 45 : 100, item.task_id, item.task_id ? "task" : "", { error: item.error || "", retryId });
    }).join("");
    for (const item of items) {
      if (item.document && attach) attachDocument(item.document.id);
      if (item.document && item.task_id) state.documentTasks.set(item.document.id, item.task_id);
      if (item.task_id) pollTask(item.task_id, item.document?.id || "");
    }
    await loadDocs();
    if (!items.some((item) => item.task_id)) clearUploadQueueWhenIdle();
    return items;
  } catch (err) {
    if (err.name === "AbortError") {
      $("uploadQueue").innerHTML = rows.map((row) => uploadRow(row.name, "Cancelled", 100)).join("");
      toast("Upload cancelled");
      return [];
    }
    $("uploadQueue").innerHTML = rows.map((row) => {
      const retryId = registerUploadRetry(row.file, attach);
      return uploadRow(row.name, "Failed", 100, "", "", { error: err.message || "Upload failed", retryId });
    }).join("");
    toast("Upload failed");
    return [];
  } finally {
    if (state.activeUploadController === controller) state.activeUploadController = null;
    state.uploadCancelled = false;
  }
}

function uploadRow(name, status, progress, taskId = "", cancelKind = "", options = {}) {
  const failed = String(status || "").toLowerCase() === "failed";
  const cancelButton = cancelKind ? `
    <button class="mini-cancel" type="button" data-action="${cancelKind === "task" ? "cancel-task" : "cancel-upload"}" data-task="${escapeHtml(taskId)}" title="Cancel" aria-label="Cancel">
      <svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>
    </button>` : "";
  const retryButton = options.retryId ? `<button class="retry-upload" type="button" data-action="retry-upload" data-retry-id="${escapeHtml(options.retryId)}">Retry</button>` : "";
  return `
    <div class="upload-item ${failed ? "failed" : ""}" data-task="${escapeHtml(taskId)}">
      <div class="upload-top"><span>${escapeHtml(name)} · <b>${escapeHtml(status)}</b></span>${retryButton}${cancelButton}</div>
      ${options.error ? `<div class="upload-error">${escapeHtml(options.error)}</div>` : ""}
      <div class="bar"><i style="--p:${Number(progress) || 0}%"></i></div>
    </div>`;
}

function ensureTaskRow(taskId, name, status = "Indexing", progress = 5) {
  if (!taskId) return;
  const selector = `[data-task="${CSS.escape(taskId)}"]`;
  const existing = document.querySelector(selector);
  if (existing) return;
  const queue = $("uploadQueue");
  queue.insertAdjacentHTML("afterbegin", uploadRow(name, status, progress, taskId, "task"));
}

function cancelUpload() {
  state.uploadCancelled = true;
  state.activeUploadController?.abort();
}

function registerUploadRetry(file, attach) {
  if (!file) return "";
  const retryId = "retry_" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  state.uploadRetries.set(retryId, { file, attach });
  return retryId;
}

function retryUpload(retryId) {
  const retry = state.uploadRetries.get(retryId);
  if (!retry) {
    toast("Retry data is no longer available");
    return;
  }
  state.uploadRetries.delete(retryId);
  uploadFiles([retry.file], { attach: retry.attach }).catch((err) => toast(err.message));
}

function removeUploadRow(taskId, delay = 900) {
  if (!taskId) return;
  const item = document.querySelector(`[data-task="${CSS.escape(taskId)}"]`);
  if (!item) return;
  window.setTimeout(() => {
    item.remove();
    if (!$("uploadQueue").children.length) $("uploadQueue").innerHTML = "";
  }, delay);
}

function clearUploadQueueWhenIdle(delay = 1200) {
  window.setTimeout(() => {
    if (!state.activeUploadController && !state.taskTimers.size) $("uploadQueue").innerHTML = "";
  }, delay);
}

async function cancelTask(taskId) {
  if (!taskId) return;
  await api(`/api/index/cancel/${taskId}`, { method: "POST" });
  toast("Indexing cancellation requested");
}

function pollTask(taskId, docId = "") {
  if (!taskId || state.taskTimers.has(taskId)) return;
  const timer = setInterval(async () => {
    try {
      const task = await api(`/api/index/progress/${taskId}`);
      state.taskStates.set(taskId, task);
      if (docId) {
        const doc = docById(docId);
        ensureTaskRow(taskId, doc?.name || "Document", taskMessageLabel(task.message, task.status), task.progress || 0);
      }
      const item = document.querySelector(`[data-task="${CSS.escape(taskId)}"]`);
      if (item) {
        item.querySelector("b").textContent = taskMessageLabel(task.message, task.status);
        item.querySelector("i").style.setProperty("--p", `${task.progress || 0}%`);
      }
      const cardProgress = document.querySelector(`[data-doc-task="${CSS.escape(taskId)}"]`);
      if (cardProgress) {
        cardProgress.querySelector("span").textContent = taskMessageLabel(task.message, task.status);
        cardProgress.querySelector("i").style.setProperty("--p", `${task.progress || 0}%`);
      }
      if (["completed", "failed", "cancelled"].includes(task.status)) {
        clearInterval(timer);
        state.taskTimers.delete(taskId);
        state.taskStates.delete(taskId);
        if (docId) {
          state.documentTasks.delete(docId);
          renderDocs();
        }
        if (task.status === "completed" || task.status === "cancelled") removeUploadRow(taskId);
        await loadDocs();
        if (task.status === "failed") toast(task.error || "Indexing failed");
      }
    } catch {
      clearInterval(timer);
      state.taskTimers.delete(taskId);
    }
  }, 900);
  state.taskTimers.set(taskId, timer);
}

async function waitForDocuments(documentIds) {
  const deadline = Date.now() + 240000;
  while (Date.now() < deadline) {
    await loadDocs();
    const selectedDocs = [...documentIds].map(docById).filter(Boolean);
    const pending = selectedDocs.filter((doc) => !["indexed", "failed", "cancelled"].includes(doc.status));
    const failed = selectedDocs.find((doc) => ["failed", "cancelled"].includes(doc.status));
    if (failed) throw new Error(`${failed.name}: ${statusLabel(failed.status)}`);
    if (!pending.length) return;
    await new Promise((resolve) => setTimeout(resolve, 1200));
  }
  throw new Error("Document processing timed out");
}

function makeMessageElement(role, text) {
  const el = document.createElement("div");
  el.className = `message ${role}`;
  el.innerHTML = renderMarkdown(text);
  return el;
}

function addMessage(role, text, scroll = true) {
  messages.querySelector(".empty-state")?.remove();
  const el = makeMessageElement(role, text);
  messages.appendChild(el);
  if (scroll) messages.scrollTop = messages.scrollHeight;
  return el;
}

function makeAssistantRunElement(initialText) {
  const el = makeMessageElement("assistant", "");
  el.innerHTML = `
    <details class="message-trace" open>
      <summary>Trace</summary>
      <div class="message-trace-list"></div>
    </details>
    <div class="answer-body">${renderMarkdown(initialText)}</div>
  `;
  return el;
}

function addAssistantRun(initialText) {
  const el = makeAssistantRunElement(initialText);
  messages.querySelector(".empty-state")?.remove();
  messages.appendChild(el);
  messages.scrollTop = messages.scrollHeight;
  return el;
}

function updateRunAnswer(el, text, scroll = true) {
  const body = el.querySelector(".answer-body");
  if (body) body.innerHTML = renderMarkdown(text);
  else el.innerHTML = renderMarkdown(text);
  if (scroll) messages.scrollTop = messages.scrollHeight;
}

function addTrace(event, payload, target) {
  const list = target?.querySelector(".message-trace-list");
  if (!list) return;
  const el = document.createElement("div");
  el.className = "trace-item";
  el.textContent = `${event}: ${traceTitleLabel(payload.title || payload.message || payload.tool?.name || "")}`;
  list.appendChild(el);
  if (messages.contains(target)) messages.scrollTop = messages.scrollHeight;
}

function appendSources(target, results = [], pages = []) {
  if (!target) return;
  target.querySelector(".message-sources")?.remove();
  const sourceRows = [];
  for (const result of results || []) {
    const range = result.pages || [result.page, result.page];
    sourceRows.push({
      title: result.document_name || result.document || "Document",
      location: `${result.section_path || result.title || "Section"} · Page ${range?.[0] || "?"}${range?.[1] && range[1] !== range[0] ? `-${range[1]}` : ""}`,
      snippet: result.snippet || result.summary || "",
    });
  }
  if (!sourceRows.length) {
    for (const page of pages || []) {
      if (!page || typeof page !== "object") continue;
      sourceRows.push({
        title: page.document_name || "Document",
        location: `Page ${page.page || "?"}${page.section ? ` · ${page.section}` : ""}`,
        snippet: page.content || "",
      });
    }
  }
  if (!sourceRows.length) return;
  const limited = sourceRows.slice(0, 8);
  const details = document.createElement("details");
  details.className = "message-sources";
  details.innerHTML = `
    <summary>Sources (${sourceRows.length})</summary>
    <div class="source-list">
      ${limited.map((source) => `
        <div class="source-item">
          <b>${escapeHtml(source.title)}</b>
          <div>${escapeHtml(source.location)}</div>
          ${source.snippet ? `<div class="source-snippet">${escapeHtml(source.snippet).slice(0, 420)}</div>` : ""}
        </div>`).join("")}
    </div>`;
  target.appendChild(details);
}

function isRunVisible(run) {
  return Boolean(run && state.currentConversationId === run.conversationId);
}

function updateRunMessage(run, text, fallbackRole = "assistant") {
  const visible = isRunVisible(run);
  if (run.messageEl) {
    if (visible && !messages.contains(run.messageEl)) messages.appendChild(run.messageEl);
    updateRunAnswer(run.messageEl, text, visible);
    return;
  }
  run.messageEl = visible ? addMessage(fallbackRole, text) : makeMessageElement(fallbackRole, text);
}

async function submitQuestion(question) {
  if (!state.selected.size) {
    toast("Attach at least one document first");
    return;
  }
  const documentIds = [...state.selected];
  const conversationId = state.currentConversationId || newConversationId();
  const runMode = state.mode;
  if (state.activeRuns.has(conversationId)) {
    toast("This conversation is already running");
    return;
  }
  if (!state.currentConversationId) {
    state.currentConversationId = conversationId;
    updateChatMeta();
  }
  const requestId = newAskId();
  const controller = new AbortController();
  const run = { requestId, conversationId, controller, messageEl: null };
  state.activeRuns.set(conversationId, run);
  syncRunControls();
  renderHistory();
  addMessage("user", question);
  const processing = addMessage("system", "Preparing attached documents...");
  try {
    await waitForDocuments(documentIds);
  } catch (err) {
    if (isRunVisible(run) && messages.contains(processing)) processing.innerHTML = renderMarkdown(`Document processing failed: ${err.message}`);
    clearAskState(run);
    return;
  }
  processing.remove();
  const payload = {
    question,
    document_ids: documentIds,
    mode: runMode,
    conversation_id: conversationId,
    request_id: requestId,
  };
  if (runMode === "standard") {
    run.messageEl = isRunVisible(run) ? addMessage("assistant", "Searching...") : makeMessageElement("assistant", "Searching...");
    try {
      const data = await api("/api/ask/standard", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload), signal: controller.signal });
      updateRunMessage(run, data.answer || "", "assistant");
      appendSources(run.messageEl, data.results || [], data.pages || []);
      await loadConversations();
      if (isRunVisible(run)) updateChatMeta();
      return;
    } catch (err) {
      updateRunMessage(run, err.name === "AbortError" ? "Response stopped." : err.message, "assistant");
      return;
    } finally {
      clearAskState(run);
    }
  }
  run.messageEl = isRunVisible(run) ? addAssistantRun("Running deep retrieval...") : makeAssistantRunElement("Running deep retrieval...");
  try {
    const res = await fetch("/api/ask/stream", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload), signal: controller.signal });
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const events = buffer.split("\n\n");
      buffer = events.pop() || "";
      for (const raw of events) {
        const lines = raw.split("\n");
        const event = (lines.find((line) => line.startsWith("event:")) || "").slice(6).trim();
        const dataLine = (lines.find((line) => line.startsWith("data:")) || "").slice(5).trim();
        if (!event || !dataLine) continue;
        const eventData = JSON.parse(dataLine);
        addTrace(event, eventData, run.messageEl);
        if (event === "final") {
          updateRunMessage(run, eventData.answer || "", "assistant");
          appendSources(run.messageEl, eventData.results || [], eventData.pages || []);
        }
        if (event === "error") updateRunMessage(run, eventData.error || "The request failed.", "assistant");
      }
    }
  } catch (err) {
    updateRunMessage(run, err.name === "AbortError" ? "Response stopped." : err.message, "assistant");
  } finally {
    clearAskState(run);
  }
  await loadConversations();
  if (isRunVisible(run)) updateChatMeta();
}

async function cancelCurrentAsk() {
  const run = state.activeRuns.get(state.currentConversationId);
  const requestId = run?.requestId || "";
  run?.controller?.abort();
  clearAskState(run);
  if (requestId) {
    await api("/api/ask/cancel", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ request_id: requestId }) }).catch(() => {});
  }
}

function clearAskState(run = null) {
  if (run) {
    const current = state.activeRuns.get(run.conversationId);
    if (current?.requestId === run.requestId) state.activeRuns.delete(run.conversationId);
  }
  syncRunControls();
  renderHistory();
}

function fileKind(name) {
  const suffix = String(name || "").split(".").pop()?.toUpperCase();
  return suffix || "FILE";
}

function setupDropZone(target, options) {
  const acceptsDrop = (event) => {
    const types = [...(event.dataTransfer?.types || [])];
    return types.includes("Files") || (Boolean(options.attach) && types.includes("application/x-lumenindex-doc"));
  };
  target.addEventListener("dragenter", (event) => {
    if (!acceptsDrop(event)) return;
    event.preventDefault();
    target.classList.add("dragging");
  });
  target.addEventListener("dragover", (event) => {
    if (!acceptsDrop(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    target.classList.add("dragging");
  });
  target.addEventListener("dragleave", (event) => {
    if (!target.contains(event.relatedTarget)) target.classList.remove("dragging");
  });
  target.addEventListener("drop", async (event) => {
    event.preventDefault();
    target.classList.remove("dragging");
    const docId = event.dataTransfer.getData("application/x-lumenindex-doc");
    const pendingQuestion = options.askAfterDrop ? $("questionInput").value.trim() : "";
    if (docId && options.attach) {
      if (pendingQuestion) $("questionInput").value = "";
      attachDocument(docId);
      state.activeDoc = docById(docId) || state.activeDoc;
      renderDocs();
      if (appShell.classList.contains("outline-open")) {
        state.activeOutlineDocId = docId;
        refreshOpenOutline(true);
      }
      if (pendingQuestion) submitQuestion(pendingQuestion).catch((err) => toast(err.message));
      return;
    }
    const files = event.dataTransfer.files;
    if (pendingQuestion) $("questionInput").value = "";
    await uploadFiles(files, { attach: options.attach });
    if (pendingQuestion) submitQuestion(pendingQuestion).catch((err) => toast(err.message));
  });
}

function initEvents() {
  $("newChatBtn").addEventListener("click", newChat);
  $("settingsBtn").addEventListener("click", openSettings);
  $("manageBtn").addEventListener("click", () => openAdmin().catch((err) => toast(err.message)));
  $("logoutBtn").addEventListener("click", () => logout().catch((err) => toast(err.message)));
  $("authForm").addEventListener("submit", (event) => {
    event.preventDefault();
    submitAuth("login", event.currentTarget).catch(() => {});
  });
  document.querySelectorAll("[data-register-action]").forEach((button) => {
    button.addEventListener("click", () => openRegisterDialog(button.dataset.registerAction || "register"));
  });
  $("registerForm").addEventListener("submit", (event) => {
    event.preventDefault();
    submitRegister(event.currentTarget).catch((err) => setRegisterError(err.message));
  });
  $("uploadDocBtn").addEventListener("click", () => $("fileInput").click());
  $("attachChatFileBtn").addEventListener("click", () => $("chatFileInput").click());
  $("fileInput").addEventListener("change", (event) => uploadFiles(event.target.files, { attach: false }).catch((err) => toast(err.message)).finally(() => { event.target.value = ""; }));
  $("chatFileInput").addEventListener("change", (event) => uploadFiles(event.target.files, { attach: true }).catch((err) => toast(err.message)).finally(() => { event.target.value = ""; }));
  $("docSearchInput").addEventListener("input", (event) => {
    state.docFilter = event.target.value;
    renderDocs();
  });
  $("docSortSelect").addEventListener("change", (event) => {
    state.docSort = event.target.value;
    renderDocs();
  });
  $("cancelAskBtn").addEventListener("click", () => cancelCurrentAsk().catch((err) => toast(err.message)));
  $("uploadQueue").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    if (button.dataset.action === "cancel-upload") cancelUpload();
    if (button.dataset.action === "cancel-task") cancelTask(button.dataset.task).catch((err) => toast(err.message));
    if (button.dataset.action === "retry-upload") retryUpload(button.dataset.retryId);
  });
  $("adminContent").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-admin-action]");
    const pageButton = event.target.closest("button[data-admin-page]");
    if (pageButton) {
      const kind = pageButton.dataset.adminPage;
      state.adminPages[kind] = Number(pageButton.dataset.page || 1);
      renderAdminOverview();
      return;
    }
    if (!button) return;
    if (button.dataset.adminAction === "copy-secret") {
      navigator.clipboard?.writeText(button.dataset.secret || "").then(() => toast("Password copied")).catch(() => toast("Copy failed"));
      return;
    }
    if (button.dataset.adminAction === "create-user") createAdminUser().catch((err) => {
      state.adminNotice = { message: err.message };
      loadAdminOverview().catch(() => {});
    });
    if (button.dataset.adminAction === "reset-user-password") resetAdminUserPassword(button.dataset.id).catch((err) => toast(err.message));
    if (button.dataset.adminAction === "delete-user") deleteAdminUser(button.dataset.id, button.dataset.username || "this user").catch((err) => toast(err.message));
    if (button.dataset.adminAction === "delete-document") deleteAdminAsset("document", button.dataset.id).catch((err) => toast(err.message));
    if (button.dataset.adminAction === "delete-conversation") deleteAdminAsset("conversation", button.dataset.id).catch((err) => toast(err.message));
  });
  $("adminContent").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && event.target?.id === "adminCreateUsername") {
      event.preventDefault();
      createAdminUser().catch((err) => {
        state.adminNotice = { message: err.message };
        loadAdminOverview().catch(() => {});
      });
    }
  });
  $("adminContent").addEventListener("input", (event) => {
    const target = event.target.closest("input[data-admin-filter]");
    if (!target) return;
    const kind = target.dataset.adminFilter;
    const cursor = target.selectionStart || target.value.length;
    state.adminFilters[kind] = target.value;
    state.adminPages[kind] = 1;
    renderAdminOverview();
    const restored = $(`adminFilter_${kind}`);
    if (restored) {
      restored.focus();
      restored.setSelectionRange(cursor, cursor);
    }
  });
  $("closeOutlineBtn").addEventListener("click", closeOutline);
  $("treeFilter").addEventListener("input", () => state.activeDoc && loadStructure(state.activeDoc.id));
  document.querySelectorAll(".mode-btn").forEach((btn) => btn.addEventListener("click", () => setMode(btn.dataset.mode)));
  $("chatForm").addEventListener("submit", (event) => {
    event.preventDefault();
    const question = $("questionInput").value.trim();
    if (!question) return;
    $("questionInput").value = "";
    submitQuestion(question).catch((err) => toast(err.message));
  });
  $("questionInput").addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.isComposing) return;
    if (event.shiftKey) return;
    event.preventDefault();
    $("chatForm").requestSubmit();
  });
  setupDropZone(chatPanel, { attach: true, askAfterDrop: true });
  setupDropZone(documentPanel, { attach: false, askAfterDrop: false });
  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
    document.querySelectorAll(".tab,.tab-panel").forEach((el) => el.classList.remove("active"));
    tab.classList.add("active");
    document.querySelector(`[data-panel="${tab.dataset.tab}"]`).classList.add("active");
  }));
  $("settingsForm").addEventListener("submit", saveSettings);
  $("testSettingsBtn").addEventListener("click", () => testSettingsConnection().catch((err) => toast(err.message)));
  $("renameForm").addEventListener("submit", (event) => submitRename(event).catch((err) => {
    $("renameError").textContent = err.message;
    $("renameError").hidden = false;
  }));
  $("renameDialog").addEventListener("close", () => {
    if ($("renameDialog").returnValue === "cancel") state.renameTargetId = "";
  });
  docContextMenu.addEventListener("click", (event) => {
    const action = event.target.closest("button")?.dataset.action;
    const docId = docContextMenu.dataset.docId;
    hideDocContextMenu();
    if (!action || !docId) return;
    if (action === "rename") renameDocument(docId).catch((err) => toast(err.message));
    if (action === "reindex") reindexDocument(docId).catch((err) => toast(err.message));
    if (action === "delete") deleteDocument(docId).catch((err) => toast(err.message));
  });
  document.addEventListener("click", hideDocContextMenu);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") hideDocContextMenu();
  });
  $("confirmOkBtn").addEventListener("click", () => finishConfirm(true));
  $("confirmCancelBtn").addEventListener("click", () => finishConfirm(false));
  confirmDialog.addEventListener("cancel", () => finishConfirm(false));
  confirmDialog.addEventListener("close", () => {
    if (confirmResolve) finishConfirm(confirmDialog.returnValue === "default");
  });
  $("clearCacheBtn").addEventListener("click", async () => {
    const ok = await appConfirm({
      title: "Clear cache",
      message: "Clearing the cache requires reindexing all documents.",
      okText: "Clear cache",
      danger: true,
    });
    if (!ok) return;
    await api("/api/cache/clear", { method: "POST" });
    toast("Cache cleared");
    await loadDocs();
  });
}

async function openSettings() {
  const cfg = await api("/api/settings");
  const form = $("settingsForm");
  for (const [key, value] of Object.entries(cfg)) {
    if (!form.elements[key] || key === "api_key") continue;
    if (form.elements[key].type === "checkbox") form.elements[key].checked = Boolean(value);
    else form.elements[key].value = value ?? "";
  }
  form.elements.api_key.value = "";
  $("settingsTestStatus").textContent = "";
  $("settingsTestStatus").className = "settings-test-status";
  $("settingsDialog").showModal();
}

function settingsPayloadFromForm(form) {
  const body = Object.fromEntries(new FormData(form).entries());
  if (!body.api_key) delete body.api_key;
  body.timeout = Number(body.timeout);
  body.context_window_k = Number(body.context_window_k);
  body.step_budget = Number(body.step_budget);
  body.max_output_tokens = Number(body.max_output_tokens);
  body.deep_thinking = form.elements.deep_thinking.checked;
  body.context_enabled = form.elements.context_enabled.checked;
  return body;
}

async function testSettingsConnection() {
  const form = $("settingsForm");
  const status = $("settingsTestStatus");
  status.className = "settings-test-status";
  status.textContent = "Testing...";
  try {
    const data = await api("/api/settings/test", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(settingsPayloadFromForm(form)) });
    status.className = "settings-test-status ok";
    status.textContent = `Connected in ${data.latency_ms} ms · ${data.model || "model"} · ${data.message || "OK"}`;
  } catch (err) {
    status.className = "settings-test-status fail";
    status.textContent = err.message || "Connection failed";
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const body = settingsPayloadFromForm(form);
  await api("/api/settings", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body) });
  $("settingsDialog").close();
  toast("Settings saved");
}

async function startAppData() {
  newChat();
  await loadInitialSettings();
  await Promise.all([loadDocs(), loadConversations()]);
}

async function bootstrap() {
  initEvents();
  const ok = await checkAuth();
  if (ok) await startAppData();
}

bootstrap().catch((err) => {
  showAuth();
  toast(err.message);
});
