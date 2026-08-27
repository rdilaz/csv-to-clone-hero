// ==UserScript==
// @name         CSV to Clone Hero SoundCloud exporter
// @namespace    csv-to-clonehero
// @match        https://soundcloud.com/you/likes*
// @match        https://soundcloud.com/*
// @grant        none
// ==/UserScript==

/*
 * This exporter reads only rendered SoundCloud track cards. It never accesses
 * cookies, storage, credentials, private endpoints, audio URLs, or CAPTCHA
 * pages. SoundCloud UI changes may require updates to these selectors.
 */
(function () {
  "use strict";
  const button = document.createElement("button");
  button.textContent = "Export visible SoundCloud tracks";
  Object.assign(button.style, { position: "fixed", zIndex: 99999, right: "16px", bottom: "16px", padding: "10px", cursor: "pointer" });
  document.body.appendChild(button);

  const text = (node) => (node && node.textContent || "").trim();
  const collect = () => {
    const result = [];
    const seen = new Set();
    document.querySelectorAll("article, .soundList__item, [data-testid='trackListItem']").forEach((card) => {
      const link = card.querySelector("a[href*='soundcloud.com/']");
      const title = text(card.querySelector(".sc-link-primary, [data-testid='trackTitle'], a[ title]"));
      const artist = text(card.querySelector(".sc-link-secondary, [data-testid='trackUser'], .soundTitle__username"));
      if (!link || !title || !artist) return;
      const id = link.href.split("?")[0];
      if (seen.has(id)) return;
      seen.add(id);
      result.push({ title, artist, permalink_url: id, url: id, source: "soundcloud-browser-export" });
    });
    return result;
  };
  const download = (name, content, type) => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([content], { type }));
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };
  button.addEventListener("click", async () => {
    button.disabled = true;
    const original = button.textContent;
    for (let i = 0; i < 12; i++) {
      window.scrollBy(0, Math.max(500, window.innerHeight * 0.8));
      button.textContent = `Collecting tracks (${i + 1}/12)`;
      await new Promise((resolve) => setTimeout(resolve, 350));
    }
    const tracks = collect();
    if (!tracks.length) alert("No rendered tracks were found. Open a public Likes or playlist page and try again.");
    else {
      download("soundcloud-tracks.json", JSON.stringify({ tracks }, null, 2), "application/json");
      const csv = ["Track name,Artist name,Soundcloud - id", ...tracks.map((t) => [t.title, t.artist, t.permalink_url].map((v) => `"${String(v).replaceAll('"', '""')}"`).join(","))].join("\r\n");
      download("soundcloud-tracks.csv", csv, "text/csv");
    }
    button.textContent = original;
    button.disabled = false;
  });
})();
