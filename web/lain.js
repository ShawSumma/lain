"use strict";

const PROJECT = new URLSearchParams(location.search).get("project") ?? "";
const SHOWN = 40;
const ROOTS = 60;
const KNOWN = ["id", "parent", "kind", "name", "project", "pid", "started", "ended", "state", "stream", "children", "running", "depth"];

const nodes = new Map();
const state = { seq: 0, roots: [], source: null, oldest: null, pinned: true };

const byId = (id) => {
  return document.getElementById(id);
};

const make = (tag, ...classes) => {
  const node = document.createElement(tag);
  node.classList.add(...classes);
  return node;
};

const write = (tag, text, ...classes) => {
  const node = make(tag, ...classes);
  node.textContent = text;
  return node;
};

const pad = (n) => {
  return String(n).padStart(2, "0");
};

const now = () => {
  return Date.now() / 1000;
};

const clock = (time) => {
  const date = new Date(time * 1000);
  const day = new Date();
  const shown = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  if (date.toDateString() === day.toDateString()) {
    return shown;
  }
  return `${date.getMonth() + 1}/${date.getDate()} ${shown}`;
};

const lasted = (seconds) => {
  if (seconds < 10) {
    return `${seconds.toFixed(1)}s`;
  } else if (seconds < 60) {
    return `${Math.round(seconds)}s`;
  } else if (seconds < 3600) {
    return `${Math.floor(seconds / 60)}m${pad(Math.floor(seconds % 60))}s`;
  }
  return `${Math.floor(seconds / 3600)}h${pad(Math.floor((seconds % 3600) / 60))}m`;
};

const count = (n) => {
  if (n < 1000) {
    return String(n);
  } else if (n < 1000000) {
    return `${(n / 1000).toFixed(1)}k`;
  }
  return `${(n / 1000000).toFixed(1)}M`;
};

const plural = (n, word) => {
  if (n === 1) {
    return `1 ${word}`;
  }
  return `${n} ${word}s`;
};

const firstLine = (text) => {
  return String(text ?? "").trim().split("\n")[0];
};

const took = (data) => {
  return (data.ended ?? now()) - data.started;
};

const short = (project) => {
  return project.split("/").filter((part) => {
    return part !== "";
  }).at(-1) ?? "";
};

const call = async (method, params) => {
  const response = await fetch("/rpc", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ method: method, project: PROJECT, params: params }),
  });
  if (!response.ok) {
    return null;
  }
  const reply = await response.json();
  if (reply.error !== undefined) {
    return null;
  }
  return reply.result;
};

const parsed = (text) => {
  try {
    return JSON.parse(text);
  } catch (error) {
    if (error instanceof SyntaxError) {
      return null;
    }
    throw error;
  }
};

const inputOf = (data) => {
  const given = parsed(String(data.attrs.input ?? data.attrs.arguments ?? ""));
  if (given === null || typeof given !== "object") {
    return "";
  }
  for (const key of ["file_path", "notebook_path", "path", "command", "pattern", "query", "url", "description", "prompt"]) {
    if (typeof given[key] === "string") {
      let text = given[key];
      if (text.startsWith(`${data.project}/`)) {
        text = text.slice(data.project.length + 1);
      }
      return firstLine(text);
    }
  }
  return "";
};

const facts = (data) => {
  const attrs = data.attrs;
  const found = [];
  switch (data.kind) {
    case "call": {
      found.push(`${attrs.model ?? ""} ${attrs.purpose ?? ""}`.trim());
      if (attrs.prompt !== undefined) {
        found.push(`${count(attrs.prompt)} in`);
      }
      if (attrs.cached !== undefined && attrs.prompt > 0) {
        found.push(`${Math.round((100 * attrs.cached) / attrs.prompt)}% cached`);
      }
      if (attrs.completion !== undefined) {
        found.push(`${count(attrs.completion)} out`);
      }
      if (attrs.completion !== undefined && data.ended !== null) {
        found.push(`${(attrs.completion / Math.max(took(data), 0.1)).toFixed(0)}/s`);
      }
      if (attrs.first !== undefined) {
        found.push(`first token ${attrs.first.toFixed(1)}s`);
      }
      break;
    }
    case "turn": {
      if (attrs.prompt !== undefined) {
        found.push(`${count(attrs.prompt)} in, ${count(attrs.cached ?? 0)} from the cache`);
      }
      if (attrs.completion !== undefined) {
        found.push(`${count(attrs.completion)} out`);
      }
      if (attrs.stop !== undefined && attrs.stop !== "") {
        found.push(attrs.stop);
      }
      break;
    }
    case "tool": {
      found.push(inputOf(data));
      break;
    }
    case "stretch": {
      found.push(firstLine(attrs.asked));
      if (attrs.remembered !== undefined) {
        found.push(`${attrs.remembered} remembered`);
      }
      break;
    }
    case "prompt": {
      break;
    }
    default: {
      if (data.children > 0) {
        found.push(plural(data.children, "part"));
      }
    }
  }
  if (data.state === "failed" && attrs.reason !== undefined) {
    found.push(firstLine(attrs.reason));
  }
  if (data.running > 0 && data.state === "running") {
    found.push(`${data.running} running inside`);
  }
  return found.filter((part) => {
    return part !== "";
  }).join(", ");
};

const label = (data) => {
  if (data.kind === "call" && data.attrs.subject) {
    return data.attrs.subject;
  } else if (data.kind === "session" || data.kind === "agent") {
    return data.name.split("#").at(-1);
  }
  return data.name;
};

const entryOf = (data) => {
  if (!nodes.has(data.id)) {
    const el = make("div", "node");
    const row = make("div", "row");
    const body = make("div", "body");
    body.hidden = true;
    el.append(row, body);
    const entry = { data: data, el: el, row: row, body: body, meta: null, items: null, open: false, by: null, loaded: false, seen: new Set(), last: null, tick: null };
    row.addEventListener("click", () => {
      if (entry.open && entry.by === "auto") {
        entry.by = "user";
        drawMeta(entry);
      } else if (entry.open) {
        closeEntry(entry, "user");
      } else {
        openEntry(entry, "user");
      }
    });
    nodes.set(data.id, entry);
  }
  const entry = nodes.get(data.id);
  entry.data = data;
  return entry;
};

const drawRow = (entry) => {
  const data = entry.data;
  entry.row.className = `row ${data.state}`;
  let mark = "+";
  if (entry.open) {
    mark = "-";
  }
  const time = write("span", lasted(took(data)), "took");
  entry.tick = time;
  const shown = [write("span", mark, "turn"), write("span", data.kind, "kind"), write("span", label(data), "label"), time, write("span", facts(data), "facts")];
  let when = clock(data.started);
  if (data.depth === 0 && PROJECT === "" && data.project !== "") {
    when = `${short(data.project)}  ${when}`;
  }
  shown.push(write("span", when, "when"));
  entry.row.replaceChildren(...shown);
};

const valueText = (value) => {
  if (typeof value === "string") {
    return value;
  }
  return JSON.stringify(value, null, 2);
};

const drawMeta = (entry) => {
  const data = entry.data;
  const grid = make("div", "attrs");
  const rows = [
    ["state", data.state],
    ["started", new Date(data.started * 1000).toLocaleString()],
    ["took", lasted(took(data))],
    ["id", data.id],
    ["process", String(data.pid)],
  ];
  if (data.project !== "") {
    rows.push(["project", data.project]);
  }
  for (const [key, value] of Object.entries(data.attrs)) {
    if (!KNOWN.includes(key) && value !== "" && value !== null && !(Array.isArray(value) && value.length === 0)) {
      rows.push([key, valueText(value)]);
    }
  }
  for (const [key, value] of rows) {
    grid.append(write("span", key), write("span", value, "value"));
  }
  grid.hidden = entry.by !== "user";
  if (entry.meta === null) {
    entry.body.prepend(grid);
  } else {
    entry.meta.replaceWith(grid);
  }
  entry.meta = grid;
};

const timeOf = (item) => {
  if (item.child !== undefined) {
    return item.child.data.started;
  }
  return item.time;
};

const placeItem = (entry, item, node) => {
  const items = entry.items;
  let index = items.length;
  while (index > 0 && timeOf(items[index - 1]) > timeOf(item)) {
    index -= 1;
  }
  if (index === items.length) {
    entry.box.append(node);
  } else {
    entry.box.insertBefore(node, items[index].node);
  }
  items.splice(index, 0, item);
};

const addChunk = (entry, piece) => {
  const key = `${piece.time}|${piece.kind}|${piece.text.length}`;
  if (entry.seen.has(key)) {
    return;
  }
  entry.seen.add(key);
  const last = entry.items.at(-1);
  if (last !== undefined && last.kind === piece.kind && last.child === undefined) {
    last.node.textContent += piece.text;
    return;
  }
  const node = write("div", piece.text, "said", piece.kind);
  placeItem(entry, { kind: piece.kind, time: piece.time, node: node }, node);
  markLive(entry);
};

const markLive = (entry) => {
  for (const item of entry.items) {
    item.node.classList.remove("live");
  }
  const last = entry.items.at(-1);
  if (entry.data.state === "running" && last !== undefined && last.child === undefined) {
    last.node.classList.add("live");
  }
};

const addChild = (entry, child) => {
  if (entry.items.some((item) => {
    return item.child === child;
  })) {
    return;
  }
  placeItem(entry, { child: child, node: child.el }, child.el);
  markLive(entry);
  trimChildren(entry);
  if (child.data.state === "running" && !child.open && child.by !== "user") {
    openEntry(child, "auto");
  }
};

const trimChildren = (entry) => {
  const children = entry.items.filter((item) => {
    return item.child !== undefined;
  });
  const hidden = children.length - SHOWN;
  children.forEach((item, index) => {
    item.node.hidden = index < hidden && !entry.all && item.child.data.state !== "running";
  });
  if (entry.earlier === undefined) {
    entry.earlier = make("div", "more");
    entry.earlier.addEventListener("click", () => {
      entry.all = true;
      trimChildren(entry);
    });
    entry.box.before(entry.earlier);
  }
  entry.earlier.hidden = hidden <= 0 || entry.all === true;
  entry.earlier.textContent = `${hidden} earlier`;
};

const openEntry = async (entry, by) => {
  entry.open = true;
  entry.by = by;
  entry.body.hidden = false;
  drawRow(entry);
  if (entry.loaded) {
    return;
  }
  entry.loaded = true;
  entry.items = [];
  entry.box = make("div");
  entry.body.append(entry.box);
  drawMeta(entry);
  const found = await call("show_span", { id: entry.data.id });
  if (found === null) {
    entry.box.append(write("div", "lain no longer has this span", "empty"));
    return;
  }
  entry.data = found.span;
  drawRow(entry);
  drawMeta(entry);
  for (const piece of found.chunks) {
    addChunk(entry, piece);
  }
  for (const data of found.children) {
    addChild(entry, entryOf(data));
    drawRow(nodes.get(data.id));
  }
  markLive(entry);
};

const closeEntry = (entry, by) => {
  entry.open = false;
  entry.by = by;
  entry.body.hidden = true;
  drawRow(entry);
};

const changed = (data) => {
  const entry = nodes.get(data.id);
  if (entry !== undefined) {
    entry.data = data;
    drawRow(entry);
    if (entry.meta !== null) {
      drawMeta(entry);
    }
    if (entry.items !== null) {
      markLive(entry);
    }
    if (data.state !== "running" && entry.open && entry.by === "auto") {
      closeEntry(entry, null);
    }
    return;
  }
  const parent = nodes.get(data.parent ?? "");
  if (data.parent === null || data.depth === 0) {
    addRoot(entryOf(data), true);
  } else if (parent !== undefined && parent.loaded) {
    const child = entryOf(data);
    drawRow(child);
    addChild(parent, child);
  } else if (parent !== undefined) {
    parent.data.children += 1;
    drawRow(parent);
  }
};

const atBottom = () => {
  return window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 40;
};

const toBottom = () => {
  if (state.pinned) {
    window.scrollTo(0, document.documentElement.scrollHeight);
  }
};

const addRoot = (entry, fresh) => {
  drawRow(entry);
  const tree = byId("tree");
  for (const empty of tree.querySelectorAll(":scope > .empty")) {
    empty.remove();
  }
  if (fresh) {
    tree.append(entry.el);
  } else {
    const before = document.documentElement.scrollHeight;
    tree.prepend(entry.el);
    if (!state.pinned) {
      window.scrollBy(0, document.documentElement.scrollHeight - before);
    }
  }
  if (entry.data.state === "running" && entry.by !== "user") {
    openEntry(entry, "auto");
  }
};

const spoken = (piece) => {
  const entry = nodes.get(piece.span);
  if (entry !== undefined && entry.items !== null) {
    addChunk(entry, piece);
  }
};

const header = () => {
  let running = 0;
  for (const entry of nodes.values()) {
    if (entry.data.state === "running" && entry.data.kind === "call") {
      running += 1;
    }
  }
  const parts = [];
  if (PROJECT !== "") {
    parts.push(PROJECT);
  } else {
    parts.push("every project");
  }
  if (running > 0) {
    parts.push(`${plural(running, "model call")} running`);
  }
  byId("what").textContent = parts.join(", ");
};

const tick = () => {
  for (const entry of nodes.values()) {
    if (entry.data.state === "running" && entry.tick !== null) {
      entry.tick.textContent = lasted(took(entry.data));
    }
  }
  header();
};

const roots = async (before) => {
  return call("list_spans", { project: PROJECT, before: before ?? 0, limit: ROOTS });
};

const older = async () => {
  const found = await roots(state.oldest);
  if (found === null) {
    return;
  }
  for (const data of found.spans) {
    if (!nodes.has(data.id)) {
      addRoot(entryOf(data), false);
    }
    state.oldest = data.started;
  }
  byId("older").hidden = found.spans.length === 0;
};

const listen = () => {
  const source = new EventSource(`/live?project=${encodeURIComponent(PROJECT)}&since=${state.seq}`);
  state.source = source;
  source.addEventListener("message", (message) => {
    const event = JSON.parse(message.data);
    state.seq = event.seq;
    if (event.type === "span") {
      changed(event.span);
    } else {
      spoken(event.chunk);
    }
  });
  source.addEventListener("error", () => {
    source.close();
    setTimeout(start, 2000);
  });
};

const start = async () => {
  const found = await roots(null);
  if (found === null) {
    setTimeout(start, 2000);
    return;
  }
  nodes.clear();
  const tree = byId("tree");
  tree.replaceChildren();
  state.seq = found.seq;
  state.oldest = null;
  for (const data of found.spans) {
    addRoot(entryOf(data), false);
    state.oldest = data.started;
  }
  if (found.spans.length === 0) {
    tree.append(write("div", "nothing has run yet; jobs, sessions and harness runs show here as they start", "empty"));
  }
  byId("older").hidden = found.spans.length === 0;
  listen();
};

byId("older").addEventListener("click", older);
window.addEventListener("scroll", () => {
  state.pinned = atBottom();
});
new ResizeObserver(toBottom).observe(byId("tree"));
setInterval(tick, 1000);
start();
