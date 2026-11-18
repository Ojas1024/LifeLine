// Download page: offer the visitor's own system first, and show the live version and file sizes.
(() => {
  "use strict";
  const base = "https://github.com/Ojas1024/LifeLine/releases/latest/download/";
  const choices = {
    windows: { file: "Lifeline-Windows.exe", label: "Download for Windows", note: "Lifeline-Windows.exe · Windows 10 and 11, 64-bit" },
    linux: { file: "Lifeline-Linux-x86_64", label: "Download for Linux", note: "Lifeline-Linux-x86_64 · x86-64, glibc 2.35 or newer" },
    mac: { file: "Lifeline-macOS-arm64.zip", label: "Download for Mac", note: "Lifeline-macOS-arm64.zip · Apple silicon. On an Intel Mac use pip, below." },
  };

  function detect() {
    const p = ((navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || "").toLowerCase();
    const ua = navigator.userAgent.toLowerCase();
    if (/android|iphone|ipad/.test(ua)) return null;          // phones can't run it
    if (p.includes("win") || ua.includes("windows")) return "windows";
    if (p.includes("mac") || ua.includes("mac os")) return "mac";
    if (p.includes("linux") || ua.includes("linux")) return "linux";
    return null;
  }

  const os = detect();
  const primary = document.getElementById("primary");
  const note = document.getElementById("primary-note");
  if (os && primary) {
    const c = choices[os];
    primary.href = base + c.file;
    primary.firstChild.textContent = c.label + " ";
    note.textContent = c.note;
    const card = document.querySelector(`.dl-card[data-os="${os}"]`);
    if (card) { card.classList.add("yours"); card.parentNode.prepend(card); }
  } else if (note) {
    note.textContent = "Lifeline runs on Windows, Linux and macOS computers. Pick yours below.";
    primary.href = "#all";
  }

  const fmt = (n) => n >= 1048576 ? `${(n / 1048576).toFixed(0)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;
  fetch("https://api.github.com/repos/Ojas1024/LifeLine/releases/latest", { headers: { Accept: "application/vnd.github+json" } })
    .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
    .then((rel) => {
      const v = (rel.tag_name || "").replace(/^v/, "");
      if (v) document.querySelectorAll(".ver").forEach((el) => (el.textContent = v));
      const sizes = Object.fromEntries((rel.assets || []).map((a) => [a.name, a.size]));
      document.querySelectorAll("[data-asset]").forEach((a) => {
        const s = sizes[a.dataset.asset];
        const slot = a.querySelector(".size");
        if (s && slot) slot.textContent = `· ${fmt(s)}`;
      });
    })
    .catch(() => {});   // offline or rate-limited: the static page still works
})();
