"use strict";
/* Публичная страница читает готовые цитаты из data.json. */

const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const STATE = { phrases: [], emo: "все", dayLabel: "" };

function shade(hex, f) {
  hex = String(hex || "").replace("#", "");
  if (hex.length === 3) hex = hex.split("").map((c) => c + c).join("");
  const n = parseInt(hex, 16);
  if (isNaN(n)) return null;
  const r = Math.round(((n >> 16) & 255) * f), g = Math.round(((n >> 8) & 255) * f), b = Math.round((n & 255) * f);
  return `rgb(${r},${g},${b})`;
}

async function boot() {
  let data = window.__DATA__;
  if (!data) { try { data = await (await fetch("data.json", { cache: "no-store" })).json(); } catch { data = null; } }
  if (!data || !data.phrases) data = { channel: {}, stats: { phrases: 0, streams: 0 }, phrases: [] };

  const stats = data.current_stats || data.stats || {};
  STATE.dayLabel = data.current_day_label || "";
  applyChannel(data.channel || {}, stats, STATE.dayLabel);
  STATE.phrases = data.phrases;
  buildFilters(data.phrases);
  render();
  // подвал — только данные: хэндл стримера и дата сборки
  const parts = [];
  if (data.channel && data.channel.handle) parts.push(esc(data.channel.handle));
  if (data.generated_at) parts.push(esc(new Date(data.generated_at).toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: "numeric" })));
  $("#colophon").innerHTML = parts.join(' <span class="c-dot">·</span> ');
}

function applyChannel(ch, stats, dayLabel) {
  const root = document.documentElement.style;
  if (ch.accent) { root.setProperty("--accent", ch.accent); const ink = shade(ch.accent, 0.62); if (ink) root.setProperty("--accent-ink", ink); }
  // всё из данных стримера; пусто — ничего не показываем, никаких моих подстановок
  $("#name").textContent = ch.name || "";
  $("#handle").textContent = ch.handle || "";
  const sub = $("#sub"); sub.textContent = ch.tagline || ""; sub.hidden = !ch.tagline;
  const day = dayLabel ? ` · ${dayLabel}` : "";
  $("#count").textContent = `${stats.phrases || 0} цитат · ${stats.streams || 0} эфиров${day}`;
  document.title = ch.name || "Итоги стрима";
}

function buildFilters(phrases) {
  const counts = {};
  phrases.forEach((p) => { if (p.emotion) counts[p.emotion] = (counts[p.emotion] || 0) + 1; });
  const emos = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  const items = [`<button class="f on" data-emo="все">все<span class="n">${phrases.length}</span></button>`]
    .concat(emos.map(([e, n]) => `<button class="f" data-emo="${esc(e)}">${esc(e)}<span class="n">${n}</span></button>`));
  $("#filters").innerHTML = items.join("");
}

function render() {
  const list = STATE.emo === "все" ? STATE.phrases : STATE.phrases.filter((p) => p.emotion === STATE.emo);
  $("#none").hidden = list.length !== 0;
  $("#index").innerHTML = list.map(entry).join("");

}

function entry(p, i) {
  const meta = [p.emotion ? `<span class="em">${esc(p.emotion)}</span>` : ""]
    .filter(Boolean).join('<span class="sep"></span>');
  return `<article class="entry${i === 0 ? " lead" : ""}">
    <span class="num">${String(i + 1).padStart(2, "0")}</span>
    <div class="q-main">
      <a class="entry-q" href="${esc(p.link)}" target="_blank" rel="noopener">${esc(p.text)}</a>
      <div class="entry-meta">${meta}</div>
    </div>
    <a class="entry-tc" href="${esc(p.link)}" target="_blank" rel="noopener">${esc(p.timecode)}</a>
  </article>`;
}

document.addEventListener("click", (e) => {
  const f = e.target.closest(".f");
  if (f) {
    STATE.emo = f.dataset.emo;
    $("#filters").querySelectorAll(".f").forEach((b) => b.classList.toggle("on", b === f));
    render();
  }
});

boot();
