"use strict";

// Work hub board. Every card is built with textContent, never innerHTML: titles come from Jira/GitLab.

const SOURCE = { jira: "Jira", gitlab: "GitLab", local: "Daily" };
const WRITE_HEADERS = { "Content-Type": "application/json", "X-Workhub": "1" };
const POLL_MS = 30000;

let state = null;
let byId = new Map();
let dragEl = null; // the card being dragged
let dropped = false;
let syncing = false;
let requestSeq = 0; // answers that arrive out of order must not overwrite newer ones
let shownSeq = 0;
let flashTimer = null;
let todoEditing = null; // id of the card whose "new to-do" field is open
let todoEditId = null; // id of the to-do whose text is being edited

const $ = (selector) => document.querySelector(selector);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

async function call(path, body) {
  const init = body === undefined ? {} : { method: "POST", headers: WRITE_HEADERS, body: JSON.stringify(body) };
  let res;
  try {
    res = await fetch(path, init);
  } catch (_) {
    throw new Error("The board server is not running. Start it with `workhub board`, then reload this tab.");
  }
  let data = {};
  try {
    data = await res.json();
  } catch (_) {
    // empty or non-JSON body
  }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

// Call an endpoint that answers with the whole board, and show it.
async function load(path, body) {
  const seq = ++requestSeq;
  try {
    const data = await call(path, body);
    if (seq >= shownSeq && data.items) {
      shownSeq = seq;
      show(data);
    }
    return data;
  } catch (err) {
    flash(err.message);
    if (path !== "/api/state") load("/api/state");
    return null;
  }
}

function show(data) {
  state = data;
  byId = new Map(data.items.map((item) => [item.id, item]));
  const laneSelect = $("#add").elements.lane;
  if (!laneSelect.options.length) {
    for (const lane of data.lanes) {
      const isToday = lane.key === "today";
      laneSelect.append(new Option(lane.name, lane.key, isToday, isToday));
    }
  }
  render();
}

// --- rendering -------------------------------------------------------------------

function render() {
  if (todoEditing && !byId.has(todoEditing)) todoEditing = null; // the card left the board
  if (todoEditId && !state.items.some((item) => (item.todos || []).some((todo) => todo.id === todoEditId))) {
    todoEditId = null; // the to-do was removed meanwhile
  }
  const perLane = new Map(state.lanes.map((lane) => [lane.key, []]));
  for (const item of state.items) (perLane.get(item.lane) || perLane.get("inbox")).push(item);
  const columns = state.lanes.map((lane) => {
    const items = perLane.get(lane.key);
    const column = el("section", `lane lane-${lane.key}`);
    const head = el("header", "lane-head");
    head.append(el("h2", null, lane.name), el("span", "count", String(items.length)));
    const list = el("div", "cards");
    list.dataset.lane = lane.key;
    for (const item of items) list.append(card(item));
    if (!items.length) list.append(el("div", "empty", lane.key === "today" ? "Drag today's work here" : "Empty"));
    makeDropZone(list);
    column.append(head, list);
    return column;
  });
  $("#board").replaceChildren(...columns);
  renderTop();
  applyFilter();
  const editor = document.querySelector(".todo-input");
  if (editor) {
    editor.focus();
    editor.setSelectionRange(editor.value.length, editor.value.length);
  }
}

function card(item) {
  const info = item.info || {};
  const node = el("article", `card src-${item.source}`);
  node.draggable = true;
  node.dataset.id = item.id;
  if (item.id === state.next) node.classList.add("is-next");

  const top = el("div", "card-top");
  top.append(el("span", "src", SOURCE[item.source] || item.source));
  if (item.ref) top.append(link(item, item.ref, "ref"));
  if (item.kind && item.source !== "local") top.append(el("span", "kind", item.kind));
  const right = el("span", "right");
  if (info.priority) right.append(el("span", "prio", info.priority));
  right.append(tool("✎", "Edit note", () => editNote(item)));
  right.append(tool("+☐", "Add a to-do", () => openTodoInput(item.id)));
  if (item.source === "local") right.append(tool("🗑", "Delete this daily task", () => deleteTask(item)));
  top.append(right);
  node.append(top);

  node.append(!item.ref && isHttp(item.url) ? link(item, item.title, "title") : el("div", "title", item.title));
  if (info.parent) {
    node.append(el("div", "parent", `↳ ${info.parent}${info.parent_title ? " " + info.parent_title : ""}`));
  }

  const meta = el("div", "meta");
  if (item.status) {
    meta.append(el("span", `status cat-${info.status_cat || "none"} st-${item.status_key || "none"}`, item.status));
  }
  for (const role of item.roles || []) meta.append(el("span", "role", role));
  if (info.author) meta.append(el("span", "role", `by ${info.author}`));
  for (const tag of info.tags || []) meta.append(el("span", "role", tag));
  if (info.due) {
    const due = el("span", "due", `due ${info.due}`);
    if (info.due < localDate()) due.classList.add("overdue");
    meta.append(due);
  }
  if (meta.childElementCount) node.append(meta);
  if (item.note) node.append(el("div", "note", item.note));
  const todos = todoList(item, node);
  if (todos) node.append(todos);

  node.addEventListener("dragstart", (event) => {
    dragEl = node;
    dropped = false;
    event.dataTransfer.effectAllowed = "move";
    // A private type: Firefox/Zen would try to open a text/plain id like "gitlab:issue:1" as an address.
    event.dataTransfer.setData("application/x-workhub-card", item.id);
    requestAnimationFrame(() => node.classList.add("dragging"));
  });
  node.addEventListener("dragend", () => {
    node.classList.remove("dragging");
    document.querySelectorAll(".cards.over").forEach((list) => list.classList.remove("over"));
    const wasDropped = dropped;
    dragEl = null;
    if (!wasDropped) render(); // cancelled: put the card back where it came from
  });
  return node;
}

// --- to-dos on a card ----------------------------------------------------------------

// The card's checklist: tick a to-do when it's done, or delete it when it doesn't apply. Both remove it.
function todoList(item, node) {
  const todos = item.todos || [];
  const editing = todoEditing === item.id;
  if (!todos.length && !editing) return null;
  const list = el("ul", "todos");
  for (const todo of todos) {
    const row = el("li", "todo");
    const box = el("input", "todo-box");
    box.type = "checkbox";
    box.title = "Done: remove it";
    box.addEventListener("change", () => {
      row.classList.add("done");
      load("/api/todos/remove", { todo: todo.id });
    });
    row.append(box, todoText(todo, node), tool("✕", "Doesn't apply: delete it", () => {
      row.classList.add("done");
      load("/api/todos/remove", { todo: todo.id });
    }));
    list.append(row);
  }
  if (editing) {
    node.draggable = false; // Firefox/Zen can't place the cursor in a field inside a draggable card
    const row = el("li", "todo-new");
    const input = el("input", "todo-input");
    input.type = "text";
    input.maxLength = 300;
    input.placeholder = "New to-do, Enter to add, Esc to close";
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        const text = input.value.trim();
        input.value = "";
        if (text) load("/api/todos", { id: item.id, text }); // stays open for the next one
      } else if (event.key === "Escape") {
        input.value = "";
        input.blur();
      }
    });
    input.addEventListener("blur", () => {
      if (!input.isConnected) return; // replaced by a re-render, which reopens it
      const text = input.value.trim();
      todoEditing = null;
      node.draggable = true;
      row.remove();
      if (!list.childElementCount) list.remove();
      if (text) load("/api/todos", { id: item.id, text }); // keep what was typed
    });
    row.append(input);
    list.append(row);
  }
  return list;
}

// A to-do's text: click it to edit. Enter or clicking elsewhere saves, Esc cancels.
function todoText(todo, node) {
  if (todoEditId !== todo.id) {
    const span = el("span", "todo-text", todo.text);
    span.title = "Click to edit";
    span.addEventListener("click", () => {
      todoEditing = null;
      todoEditId = todo.id;
      render();
    });
    return span;
  }
  node.draggable = false; // Firefox/Zen can't place the cursor in a field inside a draggable card
  const input = el("input", "todo-input todo-edit");
  input.type = "text";
  input.maxLength = 300;
  input.value = todo.text;
  let finished = false;
  const finish = (save) => {
    if (finished) return;
    finished = true;
    todoEditId = null;
    const text = input.value.trim();
    if (save && text && text !== todo.text) load("/api/todos/edit", { todo: todo.id, text });
    else render(); // unchanged, emptied or cancelled: keep the old text
  };
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === "Escape") {
      event.preventDefault();
      finish(event.key === "Enter");
    }
  });
  input.addEventListener("blur", () => {
    if (input.isConnected) finish(true);
  });
  return input;
}

function openTodoInput(itemId) {
  todoEditId = null;
  todoEditing = itemId;
  render();
}

function link(item, text, className) {
  if (!isHttp(item.url)) return el("span", className, text);
  const a = el("a", className, text);
  a.href = item.url;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  a.draggable = false;
  a.addEventListener("click", (event) => {
    if (state.links !== "server") return; // a normal browser tab opens the link itself
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey) return;
    event.preventDefault();
    openItem(item);
  });
  return a;
}

// In an app window, links go through the server, which opens them in your default browser.
function openItem(item) {
  if (state.links !== "server") {
    window.open(item.url, "_blank", "noopener");
    return;
  }
  call("/api/open", { id: item.id }).catch(() => window.open(item.url, "_blank", "noopener"));
}

function tool(text, title, onClick) {
  const button = el("button", "tool", text);
  button.type = "button";
  button.title = title;
  button.addEventListener("click", onClick);
  return button;
}

function renderTop() {
  const next = state.next ? byId.get(state.next) : null;
  const nextButton = $("#next");
  nextButton.hidden = !next;
  if (next) {
    nextButton.textContent = `Next: ${next.ref ? next.ref + " " : ""}${next.title}`;
    nextButton.onclick = () => (isHttp(next.url) ? openItem(next) : null);
  }
  const alerts = (state.sources || [])
    .filter((source) => source.error)
    .map((source) => el("div", "alert", `${SOURCE[source.name] || source.name}: ${source.error}`));
  $("#alerts").replaceChildren(...alerts);
  const times = (state.sources || []).map((source) => source.last_success).filter(Boolean).sort();
  $("#synced").textContent = times.length ? `Synced ${ago(times[times.length - 1])}` : "Not synced yet";
}

function applyFilter() {
  const query = $("#filter").value.trim().toLowerCase();
  for (const node of document.querySelectorAll(".card")) {
    node.hidden = query !== "" && !searchText(byId.get(node.dataset.id)).includes(query);
  }
}

function searchText(item) {
  if (!item) return "";
  const info = item.info || {};
  return [SOURCE[item.source], item.ref, item.kind, item.title, item.status, item.note, info.parent, info.author,
    ...(item.roles || []), ...(info.tags || []), ...(item.todos || []).map((todo) => todo.text)]
    .filter(Boolean).join(" ").toLowerCase();
}

// --- drag and drop -------------------------------------------------------------------

function makeDropZone(list) {
  list.addEventListener("dragover", (event) => {
    if (!dragEl) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    list.classList.add("over");
    const before = cardBelowPointer(list, event.clientY);
    if (before === null) {
      if (list.lastElementChild !== dragEl) list.append(dragEl);
    } else if (before !== dragEl && before.previousElementSibling !== dragEl) {
      list.insertBefore(dragEl, before);
    }
  });
  list.addEventListener("dragleave", (event) => {
    if (!list.contains(event.relatedTarget)) list.classList.remove("over");
  });
  list.addEventListener("drop", (event) => {
    if (!dragEl) return;
    event.preventDefault();
    dropped = true;
    list.classList.remove("over");
    const order = [...list.querySelectorAll(".card")].map((node) => node.dataset.id);
    load("/api/move", { id: dragEl.dataset.id, lane: list.dataset.lane, order });
  });
}

// The first visible card whose middle is below the pointer: the dragged card goes in front of it.
function cardBelowPointer(list, y) {
  for (const node of list.querySelectorAll(".card")) {
    if (node === dragEl || node.hidden) continue;
    const box = node.getBoundingClientRect();
    if (y < box.top + box.height / 2) return node;
  }
  return null;
}

// --- actions ---------------------------------------------------------------------------

// Daily tasks are yours, so they can be deleted. Ask first only when a note or to-dos would go with it.
function deleteTask(item) {
  const extras = [item.note ? "its note" : "", (item.todos || []).length ? `${item.todos.length} to-do(s)` : ""]
    .filter(Boolean);
  if (extras.length && !window.confirm(`Delete "${item.title}" with ${extras.join(" and ")}?`)) return;
  load("/api/items/delete", { id: item.id });
}

function editNote(item) {
  const note = window.prompt(`Note for ${item.ref || item.title}`, item.note || "");
  if (note !== null) load("/api/note", { id: item.id, note: note.trim() });
}

async function syncNow() {
  if (syncing) return;
  syncing = true;
  const button = $("#sync");
  button.disabled = true;
  button.textContent = "Syncing…";
  try {
    await load("/api/sync", {});
  } finally {
    syncing = false;
    button.disabled = false;
    button.textContent = "Sync";
  }
}

function flash(message) {
  const box = $("#flash");
  box.textContent = message;
  box.hidden = false;
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => (box.hidden = true), 6000);
}

// --- helpers ---------------------------------------------------------------------------

function isHttp(url) {
  return /^https?:\/\//i.test(url || "");
}

function localDate() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function ago(iso) {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
}

// --- start -------------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  $("#sync").addEventListener("click", syncNow);
  $("#filter").addEventListener("input", applyFilter);
  $("#add").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = event.target.elements.text;
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    load("/api/items", { title: text, lane: event.target.elements.lane.value || "today" });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "/" && !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) {
      event.preventDefault();
      $("#filter").focus();
    }
  });
  // A card dropped outside a lane must do nothing (Firefox/Zen would otherwise run its own drop action).
  // The card then snaps back on dragend.
  for (const type of ["dragover", "drop"]) {
    document.addEventListener(type, (event) => {
      if (dragEl) event.preventDefault();
    });
  }
  const refresh = () => {
    if (!dragEl && !syncing && !todoEditing && !todoEditId && !document.hidden) load("/api/state");
  };
  setInterval(refresh, POLL_MS);
  document.addEventListener("visibilitychange", refresh);
  load("/api/state");
});
