"use strict";

const STAGES = ["queued", "downloading", "transcribing", "filtering", "done"];
const STAGE_RU = {
  queued: "Ожидает",
  downloading: "Скачивание",
  transcribing: "Расшифровка",
  filtering: "Отбор цитат",
  done: "Готово",
  error: "Ошибка",
};

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) =>
  String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function tc(sec) {
  if (sec == null) return "--:--";
  sec = Math.floor(sec);
  const h = Math.floor(sec / 3600),
    m = Math.floor((sec % 3600) / 60),
    s = sec % 60;
  const p = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${p(m)}:${p(s)}` : `${p(m)}:${p(s)}`;
}

function stampUrl(url, sec) {
  if (sec == null || !url) return url || "#";
  const s = Math.floor(sec);
  if (url.includes("youtube.com") || url.includes("youtu.be"))
    return url + (url.includes("?") ? "&" : "?") + "t=" + s + "s";
  if (url.includes("twitch.tv")) {
    const h = Math.floor(s / 3600),
      m = Math.floor((s % 3600) / 60),
      sc = s % 60;
    return url + (url.includes("?") ? "&" : "?") + `t=${h}h${m}m${sc}s`;
  }
  return url;
}

// ------------------------------------------------------------------- здоровье
async function loadHealth() {
  try {
    const h = await (await fetch("/api/health")).json();
    const el = $("#health"),
      dot = $("#health-dot");
    $("#topn").textContent = h.top_n;
    if (h.ollama_ok) {
      el.className = "status ok";
      $("#health-text").textContent = "Сервис готов";
    } else {
      el.className = "status bad";
      $("#health-text").textContent = "Ollama не запущен";
    }
  } catch {
    $("#health").className = "status bad";
    $("#health-text").textContent = "Сервис недоступен";
  }
}

// ------------------------------------------------------------------- отправка
async function submitLinks() {
  const btn = $("#submit"),
    msg = $("#submit-msg"),
    ta = $("#links");
  const text = ta.value.trim();
  if (!text) {
    msg.className = "submit-msg err";
    msg.textContent = "Добавьте хотя бы одну ссылку.";
    return;
  }
  btn.disabled = true;
  msg.className = "submit-msg";
  msg.textContent = "Добавляем…";
  try {
    const r = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "ошибка");
    msg.className = "submit-msg ok";
    const watched = d.watched && d.watched.length ? ` Наблюдаем за Twitch: ${d.watched.join(", ")}.` : "";
    msg.textContent = `Добавлено задач: ${d.added}.${watched}`;
    ta.value = "";
    refresh();
  } catch (e) {
    msg.className = "submit-msg err";
    msg.textContent = String(e.message || e);
  } finally {
    btn.disabled = false;
  }
}

// ------------------------------------------------------------------- конвейер
function renderQueue(streams, watches = []) {
  const active = streams.filter((s) => s.status !== "done");
  const box = $("#queue");
  if (!active.length) {
    if (watches.length) {
      box.innerHTML = `<div class="queue-empty">Очередь пуста. Слежу за Twitch: ${watches
        .map((w) => esc(w.login || w.url))
        .join(", ")}</div>`;
    } else {
      box.innerHTML = '<div class="queue-empty">Активных задач нет.</div>';
    }
    return;
  }
  box.innerHTML = active
    .map((s) => {
      const cur = STAGES.indexOf(s.status);
      const steps = STAGES.slice(0, 4)
        .map((st, i) => {
          let cls = "";
          if (s.status === "error") cls = i === 0 ? "done" : "";
          else if (i < cur) cls = "done";
          else if (i === cur) cls = "active";
          return `<i class="${cls}"></i>`;
        })
        .join("");
      const isErr = s.status === "error";
      const liveActions =
        !isErr && s.source === "Twitch live"
          ? `<div class="job-actions">
              <button data-convert="${s.id}">Собрать цитаты</button>
              <button data-stop-recording="${s.id}">Остановить запись</button>
            </div>`
          : "";
      return `<div class="job ${isErr ? "err" : ""}">
        <div class="job-top">
          <span class="job-title">${esc(s.title || s.url)}</span>
          <span class="job-src">${esc(STAGE_RU[s.status] || s.status)}</span>
        </div>
        <div class="steps">${steps}</div>
        <div class="job-msg">${esc(s.stage_msg || "")}</div>
        ${
          isErr
            ? `<div class="job-actions"><button data-retry="${s.id}">Повторить</button><button data-del="${s.id}">Убрать</button></div>`
            : ""
        }
        ${liveActions}
      </div>`;
    })
    .join("");
}

// ------------------------------------------------------------------- витрина
function quoteCard(q, url) {
  const t = $("#tpl-quote").content.cloneNode(true);
  $(".q-text", t).textContent = q.text;
  const a = $(".tc", t);
  a.href = stampUrl(url, q.t_start);
  $(".tc-t", t).textContent = tc(q.t_start);
  const emo = $(".emo", t);
  if (q.emotion) {
    emo.textContent = q.emotion;
    emo.dataset.e = q.emotion;
  } else emo.remove();
  $(".tags", t).innerHTML = (q.tags || []).map((x) => `<span class="tag">${esc(x)}</span>`).join("");
  $(".score-bar i", t).style.width = Math.round(q.score || 0) + "%";
  $(".score b", t).textContent = Math.round(q.score || 0);
  $(".reason", t).textContent = q.reason || "";
  return t;
}

function renderCollections(streams, filter) {
  const done = streams.filter((s) => s.status !== "error" && (s.quotes || []).length);
  const box = $("#collections");
  const empty = $("#empty");
  if (!done.length) {
    box.innerHTML = "";
    empty.classList.add("show");
    return;
  }
  empty.classList.remove("show");
  box.innerHTML = "";
  const f = (filter || "").trim().toLowerCase();

  done.forEach((s) => {
    let quotes = s.quotes;
    if (f) quotes = quotes.filter((q) => q.text.toLowerCase().includes(f));
    if (f && !quotes.length) return;

    const sec = document.createElement("div");
    sec.className = "collection";
    const dur = s.duration ? " · " + tc(s.duration) : "";
    sec.innerHTML = `
      <div class="col-head">
        <div class="col-id">
          <div class="col-badge">${(s.title || "?").trim().charAt(0).toUpperCase()}</div>
          <div class="col-meta">
            <div class="col-title" title="${esc(s.title || s.url)}">${esc(s.title || s.url)}</div>
            <div class="col-sub">${esc(s.source || "стрим")}${dur} · ${quotes.length} цитат · <a href="${esc(
      s.url
    )}" target="_blank">источник</a></div>
          </div>
        </div>
        <div class="col-actions">
          <a href="/api/export/${s.id}.html" target="_blank">Открыть</a>
          <a href="/api/export/${s.id}.json" download>JSON</a>
          <button data-copy="${s.id}">Копировать</button>
          <button data-del="${s.id}">Удалить</button>
        </div>
      </div>
      <div class="quotes"></div>`;
    const grid = $(".quotes", sec);
    quotes.forEach((q) => grid.appendChild(quoteCard(q, s.url)));
    box.appendChild(sec);
  });
}

function copyStream(s) {
  const lines = [s.title || s.url, s.url, ""];
  (s.quotes || []).forEach((q) => {
    lines.push(`«${q.text}»  [${tc(q.t_start)}]  — ${q.emotion || ""} ${Math.round(q.score)}`);
  });
  navigator.clipboard.writeText(lines.join("\n"));
}

// ------------------------------------------------------------------- цикл
let LAST = [];
let WATCHES = [];
async function refresh() {
  try {
    const [streams, watches] = await Promise.all([
      (await fetch("/api/streams")).json(),
      (await fetch("/api/live-watches")).json(),
    ]);
    LAST = streams;
    WATCHES = watches.channels || [];
    renderQueue(streams, WATCHES);
    renderCollections(streams, $("#search").value);
  } catch {}
}

// делегирование кликов (retry/delete/copy)
document.addEventListener("click", async (e) => {
  const retry = e.target.closest("[data-retry]");
  const del = e.target.closest("[data-del]");
  const copy = e.target.closest("[data-copy]");
  const stopRecording = e.target.closest("[data-stop-recording]");
  const convert = e.target.closest("[data-convert]");
  if (retry) {
    await fetch(`/api/streams/${retry.dataset.retry}/retry`, { method: "POST" });
    refresh();
  } else if (del) {
    await fetch(`/api/streams/${del.dataset.del}`, { method: "DELETE" });
    refresh();
  } else if (stopRecording) {
    stopRecording.disabled = true;
    stopRecording.textContent = "Останавливаем…";
    await fetch(`/api/streams/${stopRecording.dataset.stopRecording}/stop-recording`, { method: "POST" });
    setTimeout(refresh, 800);
  } else if (convert) {
    convert.disabled = true;
    convert.textContent = "Собираем…";
    await fetch(`/api/streams/${convert.dataset.convert}/convert-now`, { method: "POST" });
    convert.textContent = "Готово";
    setTimeout(refresh, 800);
  } else if (copy) {
    const s = LAST.find((x) => x.id == copy.dataset.copy);
    if (s) {
      copyStream(s);
      copy.textContent = "Скопировано";
      setTimeout(() => (copy.textContent = "Копировать"), 1500);
    }
  }
});

// ---------------------------------------------------------------- профиль/публикация
async function loadChannel() {
  try {
    const c = await (await fetch("/api/channel")).json();
    $("#ch-name").value = c.name || "";
    $("#ch-handle").value = c.handle || "";
    $("#ch-tagline").value = c.tagline || "";
  } catch {}
}

async function saveChannel() {
  const body = {
    name: $("#ch-name").value.trim() || "Итоги стрима",
    handle: $("#ch-handle").value.trim(),
    tagline: $("#ch-tagline").value.trim(),
    accent: "#7c3742",
    accent2: "#56604b",
  };
  const btn = $("#ch-save");
  btn.textContent = "…";
  await fetch("/api/channel", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  btn.textContent = "Сохранено";
  setTimeout(() => (btn.textContent = "Сохранить"), 1400);
}

async function publishSite() {
  const btn = $("#publish"),
    msg = $("#publish-msg");
  btn.disabled = true;
  msg.className = "submit-msg";
  msg.textContent = "Обновляем…";
  try {
    await saveChannel();
    const r = await fetch("/api/publish", { method: "POST" });
    const d = await r.json();
    if (!r.ok) throw new Error("Не удалось обновить страницу");
    msg.className = d.ok === false ? "submit-msg err" : "submit-msg ok";
    const pushed = d.pushed ? " GitHub Pages обновлён." : "";
    msg.textContent = `Опубликовано цитат: ${d.stats.phrases}. Эфиров: ${d.stats.streams}.${pushed}`;
  } catch (e) {
    msg.className = "submit-msg err";
    msg.textContent = String(e.message || e);
  } finally {
    btn.disabled = false;
  }
}

$("#submit").addEventListener("click", submitLinks);
$("#links").addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") submitLinks();
});
$("#search").addEventListener("input", () => renderCollections(LAST, $("#search").value));
$("#ch-save").addEventListener("click", saveChannel);
$("#publish").addEventListener("click", publishSite);

loadHealth();
loadChannel();
refresh();
setInterval(refresh, 2500);
setInterval(loadHealth, 15000);
