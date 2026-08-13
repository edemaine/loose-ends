"use strict";

const parameters = new URLSearchParams(location.search);
const path = parameters.get("path") || "";
const title = document.getElementById("viewer-title");
const pathNode = document.getElementById("viewer-path");
const raw = document.getElementById("viewer-raw");
const content = document.getElementById("viewer-content");

function fileUrl(rawMode = false) {
  const query = new URLSearchParams({ path });
  if (rawMode) query.set("raw", "1");
  return `/api/file?${query}`;
}

function showError(message) {
  content.replaceChildren();
  const value = document.createElement("div");
  value.className = "error-box";
  value.textContent = message;
  content.append(value);
}

function appendHighlightedJson(pre, value) {
  const source = JSON.stringify(value, null, 2);
  const tokens = /"(?:\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4})|[^"\\])*"(?=\s*:)|"(?:\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4})|[^"\\])*"|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?|\b(?:true|false|null)\b/g;
  let offset = 0;
  for (const match of source.matchAll(tokens)) {
    pre.append(document.createTextNode(source.slice(offset, match.index)));
    const token = match[0];
    let kind;
    if (token.startsWith('"')) {
      kind = /^\s*:/.test(source.slice(match.index + token.length))
        ? "key"
        : "string";
    } else if (token === "true" || token === "false") {
      kind = "boolean";
    } else if (token === "null") {
      kind = "null";
    } else {
      kind = "number";
    }
    const span = document.createElement("span");
    span.className = `json-${kind}`;
    span.textContent = token;
    pre.append(span);
    offset = match.index + token.length;
  }
  pre.append(document.createTextNode(source.slice(offset)));
}

async function load() {
  if (!path) {
    showError("No artifact path was supplied.");
    return;
  }
  const filename = path.split(/[\\/]/).pop() || path;
  const extension = filename.includes(".") ? filename.split(".").pop().toLowerCase() : "";
  document.title = `${filename} · Loose Ends`;
  title.textContent = filename;
  pathNode.textContent = path;
  raw.href = fileUrl(true);

  if (extension === "pdf") {
    const frame = document.createElement("iframe");
    frame.className = "viewer-frame";
    frame.title = filename;
    frame.src = fileUrl();
    content.replaceChildren(frame);
    return;
  }
  if (["png", "jpg", "jpeg", "gif", "webp", "bmp", "ico"].includes(extension)) {
    const image = document.createElement("img");
    image.className = "viewer-image";
    image.alt = filename;
    image.src = fileUrl();
    content.replaceChildren(image);
    return;
  }

  const response = await fetch(fileUrl(true));
  if (!response.ok) throw new Error(`Could not load artifact (${response.status}).`);
  const source = await response.text();
  if (["md", "markdown"].includes(extension)) {
    const renderer = window.LooseEndsReviewModel?.createMarkdownRenderer(window);
    if (renderer) {
      content.className = "viewer-content markdown";
      content.innerHTML = renderer.render(source);
      return;
    }
  }
  const pre = document.createElement("pre");
  pre.className = "viewer-source";
  if (extension === "json") {
    try {
      pre.classList.add("json-source");
      appendHighlightedJson(pre, JSON.parse(source));
    } catch (error) {
      pre.textContent = source;
    }
  } else {
    pre.textContent = source;
  }
  content.replaceChildren(pre);
}

load().catch(error => showError(error.message));
