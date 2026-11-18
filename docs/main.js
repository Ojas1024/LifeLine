// Washes that crossfade per section, reveal-on-scroll, section progress, gentle parallax, ink on click,
// a live disk map in the hero, cycling search queries, and copy buttons.
(() => {
  "use strict";
  const motion = document.documentElement.classList.contains("motion");
  document.documentElement.classList.add("ready");
  const washes = Object.fromEntries([...document.querySelectorAll(".wash i")].map((i) => [i.dataset.w, i]));
  const inks = { hero: "#c3d170", rescue: "#e3b34c", scans: "#86a1e6", review: "#7fd6a6", safety: "#e3b34c", get: "#c3d170" };
  let current = "hero";

  function setWash(name) {
    if (!washes[name] || name === current && washes[name].classList.contains("on")) return;
    Object.values(washes).forEach((w) => w.classList.toggle("on", w.dataset.w === name));
    current = name;
    document.querySelectorAll(".chips a").forEach((a) => a.classList.toggle("on", a.getAttribute("href") === `#${name}`));
  }
  setWash("hero");

  const io = new IntersectionObserver((entries) => {
    entries.forEach((e) => { if (e.isIntersecting) setWash(e.target.dataset.wash); });
  }, { rootMargin: "-45% 0px -45% 0px" });
  document.querySelectorAll("[data-wash]").forEach((s) => io.observe(s));

  // stagger the format chips
  document.querySelectorAll(".fchips span").forEach((s, i) => s.style.setProperty("--i", i));

  const rev = new IntersectionObserver((entries) => {
    entries.forEach((e) => { if (e.isIntersecting) { e.target.classList.add("in"); rev.unobserve(e.target); } });
  }, { rootMargin: "0px 0px -8% 0px" });
  document.querySelectorAll(".reveal, .collage").forEach((el) => (motion ? rev.observe(el) : el.classList.add("in")));

  // nav progress: how far through each chapter we are
  const bars = [...document.querySelectorAll(".progress a")].map((a) => [a, document.getElementById(a.dataset.s)]);
  let ticking = false;
  function progress() {
    ticking = false;
    const mid = innerHeight / 2;
    bars.forEach(([a, sec]) => {
      const r = sec.getBoundingClientRect();
      a.style.setProperty("--p", Math.min(1, Math.max(0, (mid - r.top) / r.height)).toFixed(3));
    });
  }
  addEventListener("scroll", () => { if (!ticking) { ticking = true; requestAnimationFrame(progress); } }, { passive: true });
  progress();

  // copy buttons
  document.querySelectorAll("button[data-copy]").forEach((b) => b.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(b.dataset.copy); } catch { return; }
    const t = b.textContent;
    b.textContent = "Copied"; b.classList.add("done");
    setTimeout(() => { b.textContent = t; b.classList.remove("done"); }, 1400);
  }));

  // the hero disk map: a scan sweeping a drive, lighting up where files are found
  const cellsEl = document.querySelector(".cells");
  const countEl = document.querySelector(".mapcount");
  if (cellsEl) {
    const n = +cellsEl.dataset.cells;
    const cells = Array.from({ length: n }, () => cellsEl.appendChild(document.createElement("i")));
    const hits = new Set([5, 9, 10, 23, 31, 38, 39, 47, 58, 61, 70, 77, 84, 85, 91]);
    if (!motion) {
      cells.forEach((c, i) => c.classList.add(hits.has(i) ? "f" : "s"));
      countEl.textContent = hits.size;
    } else {
      let i = 0, found = 0;
      const tick = () => {
        if (i >= n) {
          setTimeout(() => { cells.forEach((c) => c.className = ""); i = 0; found = 0; countEl.textContent = 0; tick(); }, 2600);
          return;
        }
        cells[i].classList.add(hits.has(i) ? "f" : "s");
        if (hits.has(i)) countEl.textContent = ++found;
        i++;
        setTimeout(tick, 70);
      };
      setTimeout(tick, 900);
    }
  }

  // cycling queries in the search terminal
  document.querySelectorAll(".typed").forEach((el) => {
    const texts = JSON.parse(el.dataset.texts || "[]");
    if (!motion) { el.textContent = texts[0] || ""; return; }
    let k = 0;
    const type = (text, i = 0) => {
      el.textContent = text.slice(0, i);
      if (i < text.length) return setTimeout(() => type(text, i + 1), 50 + Math.random() * 60);
      setTimeout(() => erase(text, text.length), 1800);
    };
    const erase = (text, i) => {
      el.textContent = text.slice(0, i);
      if (i > 0) return setTimeout(() => erase(text, i - 1), 22);
      k = (k + 1) % texts.length;
      setTimeout(() => type(texts[k]), 300);
    };
    const t = new IntersectionObserver(([e]) => { if (e.isIntersecting) { t.disconnect(); type(texts[0]); } }, { threshold: 0.6 });
    t.observe(el);
  });

  if (!motion) return;

  // parallax on the hero collage
  const collage = document.querySelector(".collage");
  const cards = [...collage.querySelectorAll("[data-depth]")];
  collage.closest(".hero").addEventListener("pointermove", (e) => {
    const r = collage.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width - 0.5, y = (e.clientY - r.top) / r.height - 0.5;
    cards.forEach((c) => { const d = +c.dataset.depth; c.style.transform = `translate(${(-x * d).toFixed(1)}px, ${(-y * d).toFixed(1)}px)`; });
  });

  // a drop of the chapter's ink where empty space is clicked
  const washEl = document.querySelector(".wash");
  addEventListener("pointerdown", (e) => {
    if (e.target.closest("a, button, .card, .tile, pre, code, input")) return;
    const ink = document.createElement("i");
    ink.className = "ink";
    ink.style.left = `${e.clientX + innerWidth * 0.1}px`;
    ink.style.top = `${e.clientY + innerHeight * 0.1}px`;
    ink.style.setProperty("--ink-c", inks[current] || inks.hero);
    washEl.appendChild(ink);
    setTimeout(() => ink.remove(), 2500);
  });
})();
