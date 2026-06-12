const TRAILING_LINK = /\s*\[[^\]]+\]\([^)]+\)\s*$/;
const WRAPPED_LINK = /^\[([^\]]+)\]\([^)]+\)$/;

function showTab(name) {
  const tab = name === "team" ? "team" : "report";
  document.getElementById("panel-report").hidden = tab !== "report";
  document.getElementById("panel-team").hidden = tab !== "team";
  document.querySelectorAll(".tab").forEach((el) => {
    el.classList.toggle("active", el.dataset.tab === tab);
  });
}

window.addEventListener("hashchange", () => {
  showTab(location.hash.slice(1) || "report");
});

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text || "";
  return div.innerHTML;
}

function stripTitleLink(text) {
  let cleaned = (text || "").trim();
  cleaned = cleaned.replace(TRAILING_LINK, "").trim();
  const wrapped = cleaned.match(WRAPPED_LINK);
  return wrapped ? wrapped[1] : cleaned;
}

function sourceFromUrl(url) {
  if (!url) return "Morning AI";
  try {
    const host = new URL(url).hostname.replace(/^www\./, "");
    if (host.includes("ycombinator")) return "Hacker News";
    if (host.includes("arxiv")) return "arXiv";
    if (host.includes("github")) return "GitHub";
    return host;
  } catch {
    return "Morning AI";
  }
}

function formatMastheadDate(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  const day = date.toLocaleDateString("en-US", {
    weekday: "long",
    month: "long",
    day: "numeric",
    year: "numeric",
  });
  return `${day} · ${editionTime(iso)}`;
}

function issueNumber(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  const start = new Date(date.getFullYear(), 0, 0);
  const day = Math.floor((date - start) / 86400000);
  return String(day).padStart(3, "0");
}

/** 24-hour local time when the edition was written (HH:mm). */
function editionTime(iso) {
  if (!iso) return "—";
  try {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "—";
    const h = String(date.getHours()).padStart(2, "0");
    const m = String(date.getMinutes()).padStart(2, "0");
    return `${h}:${m}`;
  } catch {
    return "—";
  }
}

function updateMasthead(iso) {
  const dateText = formatMastheadDate(iso);
  const num = issueNumber(iso);
  document.getElementById("masthead-date").textContent = dateText;
  document.getElementById("masthead-issue").textContent = iso
    ? `No. ${num} · Single Copy`
    : "No. — · Single Copy";
  document.getElementById("footer-issue").textContent = iso
    ? `vol. 1 · iss. ${num}`
    : "vol. 1";
}

function formatReport(data) {
  let body = (data.report || "").trim();
  const urls = data.section_urls || [];
  const lines = [];
  let urlIdx = 0;

  for (const line of body.split("\n")) {
    const isStoryHeading = line.startsWith("## ") && !line.startsWith("###");
    if (isStoryHeading && urlIdx < urls.length) {
      const url = (urls[urlIdx] || "").trim();
      urlIdx += 1;
      const heading = stripTitleLink(line.slice(3));
      lines.push(url ? `## [${heading}](${url})` : `## ${heading}`);
    } else {
      lines.push(line);
    }
  }
  return lines.join("\n");
}

function extractSection(block, heading) {
  const re = new RegExp(
    `### ${heading}\\s*\\n+([\\s\\S]*?)(?=\\n### |\\n## |$)`,
    "i"
  );
  const match = block.match(re);
  return match ? match[1].trim() : "";
}

function stripGlobalSection(body, heading) {
  return body
    .replace(
      new RegExp(`### ${heading}\\s*\\n+[\\s\\S]*?(?=\\n### |\\n## |$)`),
      ""
    )
    .trim();
}

function extractGlobalCallouts(markdown) {
  const whatToWatch = extractSection(markdown, "What to watch");
  const priority = extractSection(markdown, "Priority");
  const action = extractSection(markdown, "One action");
  let body = markdown;
  for (const heading of ["What to watch", "Priority", "One action"]) {
    body = stripGlobalSection(body, heading);
  }
  return { body: body.trim(), whatToWatch, priority, action };
}

function parseStory(block, fallbackUrl) {
  const lines = block.trim().split("\n");
  let title = "";
  let url = fallbackUrl || "";

  const h2 = lines.find((l) => l.startsWith("## ") && !l.startsWith("###"));
  if (h2) {
    const linkMatch = h2.match(/^## \[([^\]]+)\]\(([^)]+)\)/);
    if (linkMatch) {
      title = linkMatch[1];
      url = linkMatch[2];
    } else {
      title = stripTitleLink(h2.slice(3));
    }
  }

  const breakthrough =
    extractSection(block, "The Breakthrough") ||
    extractSection(block, "What happened");
  const caveats = extractSection(block, "The Caveats");

  return {
    title,
    url,
    source: sourceFromUrl(url),
    summary: breakthrough,
    caveats,
  };
}

function splitStories(markdown) {
  const { body } = extractGlobalCallouts(markdown);
  return body.split(/(?=^## )/m).filter((p) => p.trim());
}

function renderSourceLine(story) {
  return `
    <div class="source-line mono">
      <span class="source-name">${escapeHtml(story.source)}</span>
    </div>`;
}

function renderLeadTagRow() {
  return `
    <div class="tag-row">
      <span class="tag-priority mono">★ Priority</span>
    </div>`;
}

function renderLeadStory(story) {
  return `
    <article class="lead-story">
      ${renderSourceLine(story)}
      <h2 class="display">
        <a href="${escapeHtml(story.url)}" target="_blank" rel="noreferrer">${escapeHtml(story.title)}</a>
      </h2>
      <p class="lead-body">${escapeHtml(story.summary)}</p>
      ${story.caveats ? `<p class="lead-caveats">${escapeHtml(story.caveats)}</p>` : ""}
      ${renderLeadTagRow()}
    </article>`;
}

function renderStoryCard(story) {
  return `
    <article class="story-card">
      ${renderSourceLine(story)}
      <h3 class="display">
        <a href="${escapeHtml(story.url)}" target="_blank" rel="noreferrer">${escapeHtml(story.title)}</a>
      </h3>
      <div class="story-rule"></div>
      <p class="story-body">${escapeHtml(story.summary)}</p>
      ${story.caveats ? `<p class="story-caveats">${escapeHtml(story.caveats)}</p>` : ""}
    </article>`;
}

function renderCallouts(whatToWatch, priority, action) {
  if (!whatToWatch && !priority && !action) return "";
  const parts = [];
  if (whatToWatch) {
    parts.push(`
      <div class="callout callout-watch">
        <div class="callout-label mono">▶ What to Watch</div>
        <p>${escapeHtml(whatToWatch)}</p>
      </div>`);
  }
  if (priority) {
    parts.push(`
      <div class="callout callout-priority">
        <div class="callout-label mono">▶ Priority Read</div>
        <p>${escapeHtml(priority)}</p>
      </div>`);
  }
  if (action) {
    parts.push(`
      <div class="callout callout-action">
        <div class="callout-label mono">▶ One Action</div>
        <p>${escapeHtml(action)}</p>
      </div>`);
  }
  return `<div class="callouts">${parts.join("")}</div>`;
}

function renderEmptyReport() {
  updateMasthead("");
  document.getElementById("panel-report").innerHTML = `
    <div class="empty-state">
      <h2>No report yet</h2>
      <p>Run <code>python agent.py</code>, then <code>python scripts/export_site.py</code>.</p>
    </div>`;
}

function buildReportHTML(data, formattedMarkdown) {
  const blocks = splitStories(formattedMarkdown);
  const urls = data.section_urls || [];
  const stories = blocks.map((block, i) =>
    parseStory(block, urls[i] || "")
  );

  const { whatToWatch, priority, action } = extractGlobalCallouts(
    (data.report || "").trim()
  );

  const themes = (data.themes || [])
    .map((t) => `<span class="theme-tag mono">${escapeHtml(t)}</span>`)
    .join("");

  const lead = stories[0];
  const rest = stories.slice(1);

  return `
    <div class="report-view">
      ${
        themes
          ? `<div class="theme-strip">
          <span class="theme-strip-label mono">Today's threads —</span>
          ${themes}
        </div>`
          : ""
      }
      ${lead ? renderLeadStory(lead) : ""}
      ${
        rest.length
          ? `<div class="story-grid">${rest.map((s) => renderStoryCard(s)).join("")}</div>`
          : ""
      }
      ${renderCallouts(whatToWatch, priority, action)}
    </div>`;
}

async function loadReport() {
  const panel = document.getElementById("panel-report");
  try {
    const res = await fetch(`report.json?t=${Date.now()}`, { cache: "no-store" });
    if (!res.ok) {
      renderEmptyReport();
      return;
    }
    const data = await res.json();
    if (!data.report) {
      renderEmptyReport();
      return;
    }
    updateMasthead(data.generated_at);
    const formatted = formatReport(data);
    panel.innerHTML = buildReportHTML(data, formatted);
  } catch {
    renderEmptyReport();
  }
}

function portraitBgStyle(agent) {
  const bg = agent.portrait_bg;
  if (!bg) return "";
  return ` style="--portrait-bg: ${escapeHtml(bg)}"`;
}

function renderCardSide(agent) {
  const bgStyle = portraitBgStyle(agent);
  if (agent.image) {
    return `<div class="card-side"${bgStyle}><img src="${escapeHtml(agent.image)}" alt=""></div>`;
  }
  return `<div class="card-side pixelated"${bgStyle}>${agent.emoji || "🤖"}</div>`;
}

function renderPersonCard(person) {
  return `
    <div class="person-card">
      ${renderCardSide(person)}
      <div class="card-body">
        <div class="card-head">
          <h3 class="card-name display">${escapeHtml(person.name)}</h3>
          <span class="card-badge mono">Human</span>
        </div>
        <div class="card-role mono">${escapeHtml(person.title)}</div>
        <p class="card-bio">${escapeHtml(person.bio)}</p>
        ${person.note ? `
        <dl class="card-dl">
          <div>
            <dt>Note</dt>
            <dd>${escapeHtml(person.note)}</dd>
          </div>
        </dl>` : ""}
      </div>
    </div>`;
}

function renderAgentCard(agent, isBoss) {
  const bossBadge = isBoss
    ? `<span class="card-badge card-badge--boss mono">★ Boss</span>`
    : "";

  const subAgents = agent.sub_agents
    ? `<div class="sub-roles">
        <div class="sub-roles-title mono">Sub-roles</div>
        <ul>${agent.sub_agents
          .map(
            (sub) => `
          <li>
            <span>${escapeHtml(sub.name)}</span>
            <span class="sub-model mono">${escapeHtml(sub.model)}</span>
          </li>`
          )
          .join("")}</ul>
      </div>`
    : "";

  return `
    <div class="agent-card${isBoss ? " agent-card--boss" : ""}">
      ${renderCardSide(agent)}
      <div class="card-body">
        <div class="card-head">
          <h3 class="card-name display">${escapeHtml(agent.name)}</h3>
          ${bossBadge}
        </div>
        <div class="card-role mono">${escapeHtml(agent.title || "")}</div>
        <p class="card-bio">${escapeHtml(agent.bio || "")}</p>
        <dl class="card-dl">
          <div>
            <dt>Model</dt>
            <dd>${escapeHtml(agent.model || "")}</dd>
          </div>
          <div>
            <dt>Runs</dt>
            <dd>${escapeHtml(agent.schedule || "")}</dd>
          </div>
          ${agent.note || agent.badge === "local only" ? `<div><dt>Note</dt><dd>${escapeHtml(agent.note || agent.badge)}</dd></div>` : ""}
        </dl>
        ${subAgents}
      </div>
    </div>`;
}

async function loadTeam() {
  const panel = document.getElementById("panel-team");
  try {
    const res = await fetch(`team.json?t=${Date.now()}`, { cache: "no-store" });
    if (!res.ok) {
      panel.innerHTML = `<div class="empty-state"><p>Could not load team.json</p></div>`;
      return;
    }
    const team = await res.json();
    panel.innerHTML = `
      <div class="team-view">
        <div class="team-intro">
          <div class="team-section-label mono">Section B · The Newsroom</div>
          <h2 class="display">Who Made This Paper</h2>
          <p>One human, one boss agent, four reporters. They wake up at 8 AM, file the briefing, then go back to sleep.</p>
        </div>
        <div class="team-center">${renderPersonCard(team.human)}</div>
        <div class="org-line"><div class="org-line-v"></div></div>
        <div class="team-center">${renderAgentCard(team.leader, true)}</div>
        <div class="org-line">
          <div class="org-line-v"></div>
          <div class="org-line-h"></div>
        </div>
        <div class="team-grid">
          ${team.reports.map((a) => renderAgentCard(a, false)).join("")}
        </div>
      </div>`;
  } catch {
    panel.innerHTML = `<div class="empty-state"><p>Could not load team roster</p></div>`;
  }
}

showTab(location.hash.slice(1) || "report");
loadReport();
loadTeam();
