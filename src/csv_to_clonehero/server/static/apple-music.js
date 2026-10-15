(() => {
  "use strict";
  function mount() {
  const form = document.querySelector("#appleMusicForm");
  if (!form || form.dataset.riffAppleMusicBound === "true") return;
  form.dataset.riffAppleMusicBound = "true";
  const badge = document.querySelector("#appleMusicCount");
  const message = document.querySelector("#appleMusicMessage");
  let busy = false;
  function renderCount(count) {
    count = Math.max(0, Number(count) || 0);
    badge.textContent = count ? `${count.toLocaleString()} unique ${count === 1 ? "song" : "songs"} imported` : "Not added";
    badge.classList.toggle("status-pill--quiet", !count);
  }
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (busy) return;
    const file = document.querySelector("#appleMusicFile").files?.[0];
    if (!file) return;
    if (!/\.(txt|xml)$/i.test(file.name) || file.size > 10 * 1024 * 1024) {
      message.textContent = "Choose a Music/iTunes TXT or XML export up to 10 MB.";
      message.classList.add("is-error");
      return;
    }
    const button = form.querySelector("button[type=submit]");
    const body = new FormData(form);
    busy = true;
    button.disabled = true;
    button.textContent = "Importing...";
    message.textContent = "Reading the selected export metadata...";
    message.classList.remove("is-error");
    let outcomeKnown = false;
    try {
      const response = await fetch("/api/sources/apple-music/import", {
        method: "POST", credentials: "same-origin", headers: {"X-Riff-Request": "1"}, body,
      });
      const result = await response.json();
      outcomeKnown = true;
      if (!response.ok || !result.ok) throw new Error(result.error || "The export could not be imported.");
      renderCount(result.source?.tracks);
      const report = result.source?.report || {};
      const count = Math.max(0, Number(result.source?.tracks) || 0);
      const skipped = Math.max(0, Number(report.skipped_rows) || 0);
      const duplicates = Math.max(0, Number(report.duplicate_rows) || 0);
      message.textContent = `${count.toLocaleString()} unique ${count === 1 ? "song" : "songs"} imported. ${duplicates.toLocaleString()} duplicate ${duplicates === 1 ? "row" : "rows"} combined; ${skipped.toLocaleString()} ${skipped === 1 ? "row" : "rows"} without song metadata skipped. Choose Find charts when ready.`;
      form.querySelector("input[type=file]").value = "";
      document.dispatchEvent(new CustomEvent("riff:library-imported", {detail: result}));
    } catch (error) {
      message.textContent = outcomeKnown
        ? error.message || "The export could not be imported."
        : "The import outcome could not be confirmed. Reload to check saved imports before retrying.";
      message.classList.add("is-error");
    } finally {
      busy = false;
      button.disabled = false;
      button.textContent = "Import export";
    }
  });
  }
  window.RiffAppleMusic = Object.freeze({mount});
  mount();
})();
