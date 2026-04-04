// ─── State ────────────────────────────────────────────────────────
let allClips       = [];
let activeTag      = null;
let sortable       = null;     // main grid drag-to-reorder
let queueSortable  = null;     // queue panel drag-to-reorder
let ingestPollTimer = null;
let lastDoneCount  = 0;
let modalBusy      = false;
let queue          = [];       // ordered array of clip IDs (the "selected" working set)
let pendingQueueSuggestion = null; // AI-suggested order for queue, waiting for apply
let showQueuedOnly = false;   // filter grid to queued clips only
let isAuthenticated = false;  // set after /api/auth/status check

// ─── Init ─────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
    initAuth();
    loadClips();
    pollIngestStatus();
    initDrawer();
    initQueuePanel();
    initBottomNav();
    initSearchToggle();
    initIngestModal();

    document.getElementById("btn-show-queued").addEventListener("click", toggleShowQueued);
    document.getElementById("btn-fab-montage").addEventListener("click", () => requireAuth(openMontageModal));
    document.getElementById("btn-toolbar-montage").addEventListener("click", () => requireAuth(openMontageModal));

    // Sort (desktop header + sidebar + mobile)
    document.getElementById("sort-select")?.addEventListener("change", onSortChange);
    document.getElementById("sort-select-sidebar")?.addEventListener("change", (e) => {
        const h = document.getElementById("sort-select");
        if (h) h.value = e.target.value;
        filterAndRender();
    });
    document.getElementById("sort-select-mobile")?.addEventListener("change", (e) => {
        const h = document.getElementById("sort-select");
        if (h) h.value = e.target.value;
        filterAndRender();
    });

    // Search
    document.getElementById("search").addEventListener("input", debounce(onSearch, 250));
    document.getElementById("search-desktop")?.addEventListener("input", debounce(() => {
        document.getElementById("search").value = document.getElementById("search-desktop").value;
        filterAndRender();
    }, 250));

    // Library AI order (sidebar)
    document.getElementById("btn-ai-order").addEventListener("click", () => requireAuth(onLibraryAIOrder));
    document.getElementById("btn-apply-order").addEventListener("click", onApplyLibraryOrder);
    document.getElementById("btn-dismiss-order").addEventListener("click", () => {
        document.getElementById("ai-suggestion").style.display = "none";
    });

    // Export
    document.getElementById("btn-outputs").addEventListener("click", () => { closeDrawer(); openOutputsPanel(); });
    document.getElementById("outputs-close").addEventListener("click", () => {
        document.getElementById("outputs-overlay").style.display = "none";
    });
    document.getElementById("outputs-overlay").addEventListener("click", (e) => {
        if (e.target === e.currentTarget) document.getElementById("outputs-overlay").style.display = "none";
    });

    // Montage modal
    document.getElementById("montage-close").addEventListener("click", () => {
        document.getElementById("montage-overlay").style.display = "none";
    });
    document.getElementById("btn-montage-render").addEventListener("click", onRenderMontage);

    // Clip detail modal
    document.getElementById("modal-close").addEventListener("click", closeModal);
    document.getElementById("modal-overlay").addEventListener("click", (e) => {
        if (e.target === e.currentTarget && !modalBusy) closeModal();
    });
});

// ─── Load & Render ────────────────────────────────────────────────
async function loadClips() {
    const loading = document.getElementById("loading");
    loading.style.display = "block";
    try {
        const resp = await fetch("/api/clips");
        allClips = await resp.json();
        renderClips(allClips);
        renderTags();
        renderStats();
    } catch (err) {
        loading.textContent = "Error loading clips: " + err.message;
    }
}

function renderClips(clips) {
    const grid    = document.getElementById("clip-grid");
    const loading = document.getElementById("loading");
    loading.style.display = "none";

    grid.innerHTML = clips.map(c => clipCardHTML(c)).join("");
    document.getElementById("clip-count").textContent = `${clips.length} clip${clips.length !== 1 ? "s" : ""}`;

    // Card clicks: open modal. Queue badge clicks: toggle queue.
    grid.querySelectorAll(".clip-card").forEach(card => {
        const id = parseInt(card.dataset.id);
        card.addEventListener("click", () => openModal(id));
    });

    grid.querySelectorAll(".queue-badge-btn").forEach(btn => {
        btn.addEventListener("click", (e) => {
            e.stopPropagation();
            toggleQueue(parseInt(btn.dataset.clipId));
        });
    });

    grid.querySelectorAll(".share-badge-btn").forEach(btn => {
        btn.addEventListener("click", (e) => {
            e.stopPropagation();
            const id  = parseInt(btn.dataset.clipId);
            const url = window.location.origin + "/share/clip/" + id;
            copyToClipboard(url, btn, null, () => {
                btn.classList.add("copied");
                btn.innerHTML = "✓";
                setTimeout(() => {
                    btn.classList.remove("copied");
                    btn.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>`;
                }, 2000);
            });
        });
    });

    // Main grid drag-to-reorder (disabled when not authenticated)
    if (sortable) sortable.destroy();
    sortable = new Sortable(grid, {
        animation: 150,
        ghostClass: "sortable-ghost",
        chosenClass: "sortable-chosen",
        disabled: !isAuthenticated,
        onEnd: onDragEnd,
    });
}

function clipCardHTML(c) {
    const dur     = c.duration ? formatDuration(c.duration) : "?";
    const res     = c.width && c.height ? `${c.width}×${c.height}` : "";
    const posHTML = c.position != null ? `<span class="clip-position">${c.position}</span>` : "";
    const tagsHTML = (c.tags || "").split(",").filter(Boolean)
        .map(t => `<span class="tag-pill">${esc(t.trim())}</span>`).join("");
    const rationaleHTML = c.ai_rationale
        ? `<div class="clip-rationale">${esc(c.ai_rationale)}</div>` : "";

    const queuePos = queue.indexOf(c.id);  // -1 if not in queue
    const inQ = queuePos >= 0;

    const thumbFrame = c.thumbnail_frame || "frame_0001.jpg";
    const thumbHTML = c.file_hash
        ? `<img class="clip-thumb" src="/frames/${c.file_hash}/${thumbFrame}"
               onerror="this.style.display='none'" loading="lazy" alt="">`
        : "";

    return `
        <div class="clip-card${inQ ? " in-queue" : ""}" data-id="${c.id}">
            <div class="clip-thumb-wrap">
                ${thumbHTML}
                <button class="queue-badge-btn${inQ ? " in-queue" : ""}"
                        data-clip-id="${c.id}"
                        title="${inQ ? `Remove from queue (pos ${queuePos + 1})` : "Add to queue"}">
                    ${inQ ? queuePos + 1 : "+"}
                </button>
                <button class="share-badge-btn" data-clip-id="${c.id}" title="Copy share link">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
                </button>
            </div>
            <div class="clip-card-body">
                <div class="clip-card-header">
                    <span class="clip-filename">${esc(c.filename)}</span>
                    ${posHTML}
                </div>
                <div class="clip-meta">
                    <span>${dur}</span>
                    ${res ? `<span>${res}</span>` : ""}
                    <span class="clip-status ${c.status}">${c.status}</span>
                </div>
                <div class="clip-synopsis">${esc(c.synopsis || "—")}</div>
                ${tagsHTML ? `<div class="clip-tags">${tagsHTML}</div>` : ""}
                ${rationaleHTML}
            </div>
        </div>`;
}

function renderTags() {
    const tagSet = new Set();
    allClips.forEach(c => (c.tags || "").split(",").filter(Boolean).forEach(t => tagSet.add(t.trim())));
    const container = document.getElementById("tag-list");
    if (tagSet.size === 0) {
        container.innerHTML = '<span style="color:var(--text2);font-size:0.8rem">No tags yet</span>';
        return;
    }
    container.innerHTML = [...tagSet].sort().map(t =>
        `<span class="tag-pill${activeTag === t ? " active" : ""}" data-tag="${esc(t)}">${esc(t)}</span>`
    ).join("");
    container.querySelectorAll(".tag-pill").forEach(pill => {
        pill.addEventListener("click", () => {
            activeTag = activeTag === pill.dataset.tag ? null : pill.dataset.tag;
            filterAndRender();
            renderTags();
        });
    });
}

function renderStats() {
    const stats = {};
    allClips.forEach(c => { stats[c.status] = (stats[c.status] || 0) + 1; });
    document.getElementById("stats").innerHTML = Object.entries(stats)
        .filter(([, v]) => v > 0)
        .map(([k, v]) => `<div style="font-size:0.83rem"><span class="clip-status ${k}">${k}</span> ${v}</div>`)
        .join("");
}

// ─── Filter & Sort ────────────────────────────────────────────────
function filterAndRender() {
    const searchTerm = document.getElementById("search").value.toLowerCase();
    const sortBy     = document.getElementById("sort-select")?.value || "filename";

    let filtered = allClips;

    if (searchTerm) {
        filtered = filtered.filter(c =>
            (c.filename  || "").toLowerCase().includes(searchTerm) ||
            (c.synopsis  || "").toLowerCase().includes(searchTerm) ||
            (c.transcript|| "").toLowerCase().includes(searchTerm) ||
            (c.tags      || "").toLowerCase().includes(searchTerm) ||
            (c.notes     || "").toLowerCase().includes(searchTerm)
        );
    }

    if (activeTag) {
        filtered = filtered.filter(c =>
            (c.tags || "").split(",").map(t => t.trim()).includes(activeTag)
        );
    }

    if (showQueuedOnly) {
        filtered = filtered.filter(c => queue.includes(c.id));
        // Sort by queue position when showing queued only
        filtered.sort((a, b) => queue.indexOf(a.id) - queue.indexOf(b.id));
        renderClips(filtered);
        return;
    }

    filtered.sort((a, b) => {
        if (sortBy === "position") {
            const pa = a.position ?? 99999, pb = b.position ?? 99999;
            return pa - pb || a.filename.localeCompare(b.filename);
        }
        if (sortBy === "duration") return (b.duration || 0) - (a.duration || 0);
        return a.filename.localeCompare(b.filename);
    });

    renderClips(filtered);
}

function onSearch()    { filterAndRender(); }
function onSortChange(){ filterAndRender(); }

function toggleShowQueued() {
    showQueuedOnly = !showQueuedOnly;
    const btn = document.getElementById("btn-show-queued");
    btn.textContent = showQueuedOnly ? "✕ Show All" : "☰ Show Queued";
    btn.classList.toggle("btn-primary", showQueuedOnly);
    filterAndRender();
}

// ─── Drag & Drop (main grid) ──────────────────────────────────────
async function onDragEnd() {
    const cards = document.getElementById("clip-grid").querySelectorAll(".clip-card");
    const items = [...cards].map((card, idx) => ({ id: parseInt(card.dataset.id), position: idx + 1 }));
    try {
        await fetch("/api/clips/reorder", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ items }),
        });
        items.forEach(({ id, position }) => {
            const clip = allClips.find(c => c.id === id);
            if (clip) clip.position = position;
        });
        cards.forEach((card, idx) => {
            let posEl = card.querySelector(".clip-position");
            if (posEl) {
                posEl.textContent = idx + 1;
            } else {
                card.querySelector(".clip-card-header").insertAdjacentHTML(
                    "beforeend", `<span class="clip-position">${idx + 1}</span>`
                );
            }
        });
    } catch (err) {
        alert("Failed to save order: " + err.message);
    }
}

// ─── Queue ────────────────────────────────────────────────────────
function toggleQueue(clipId) {
    const idx = queue.indexOf(clipId);
    if (idx >= 0) queue.splice(idx, 1);
    else queue.push(clipId);
    updateQueueBadgesInGrid();
    renderQueuePanel();
    updateQueueCountBadges();
}

function removeFromQueue(clipId) {
    const idx = queue.indexOf(clipId);
    if (idx < 0) return;
    queue.splice(idx, 1);
    updateQueueBadgesInGrid();
    renderQueuePanel();
    updateQueueCountBadges();
}

function clearQueue() {
    queue = [];
    pendingQueueSuggestion = null;
    document.getElementById("queue-share-url")?.classList.remove("visible");
    updateQueueBadgesInGrid();
    renderQueuePanel();
    updateQueueCountBadges();
}

function updateQueueBadgesInGrid() {
    // Update badge appearance on all currently-visible cards without full re-render
    document.querySelectorAll(".queue-badge-btn").forEach(btn => {
        const id  = parseInt(btn.dataset.clipId);
        const pos = queue.indexOf(id);
        const inQ = pos >= 0;
        btn.textContent = inQ ? pos + 1 : "+";
        btn.classList.toggle("in-queue", inQ);
        btn.title = inQ ? `Remove from queue (pos ${pos + 1})` : "Add to queue";
        btn.closest(".clip-card").classList.toggle("in-queue", inQ);
    });
}

function updateQueueCountBadges() {
    const n = queue.length;
    const show = n > 0;
    const hb = document.getElementById("queue-header-badge");
    const nb = document.getElementById("queue-nav-badge");
    if (hb) { hb.textContent = n; hb.style.display = show ? "flex" : "none"; }
    if (nb) { nb.textContent = n; nb.style.display = show ? "flex" : "none"; }

    // Show/hide the "Show Queued" filter button in toolbar
    const qBtn = document.getElementById("btn-show-queued");
    if (qBtn) {
        qBtn.style.display = show ? "inline-flex" : "none";
        if (!show && showQueuedOnly) {
            showQueuedOnly = false;
            qBtn.textContent = "☰ Show Queued";
            qBtn.classList.remove("btn-primary");
            filterAndRender();
        }
    }

    // Floating montage button (mobile only — CSS hides on desktop)
    const fab = document.getElementById("btn-fab-montage");
    if (fab) {
        fab.style.display = show ? "flex" : "none";
        const fabCount = document.getElementById("fab-montage-count");
        if (fabCount) fabCount.textContent = n;
    }

    // Toolbar montage button (desktop only — CSS hides on mobile)
    const tbm = document.getElementById("btn-toolbar-montage");
    if (tbm) {
        tbm.style.display = show ? "inline-flex" : "none";
        const tmc = document.getElementById("toolbar-montage-count");
        if (tmc) tmc.textContent = n;
    }
}

function initQueuePanel() {
    document.getElementById("btn-queue-toggle").addEventListener("click", toggleQueuePanel);
    document.getElementById("btn-queue-close").addEventListener("click", closeQueuePanel);
    document.getElementById("queue-backdrop").addEventListener("click", closeQueuePanel);
    document.getElementById("btn-queue-clear").addEventListener("click", clearQueue);
    document.getElementById("btn-queue-ai-order").addEventListener("click", () => requireAuth(onQueueAIOrder));
    document.getElementById("btn-queue-montage").addEventListener("click", () => requireAuth(openMontageModal));
    document.getElementById("btn-queue-share").addEventListener("click", shareQueuePlaylist);
    document.getElementById("btn-queue-regen").addEventListener("click", () => requireAuth(onQueueBatchRegen));
    document.getElementById("btn-queue-retag").addEventListener("click", () => requireAuth(onQueueBatchRetag));
    document.getElementById("btn-queue-ai-apply").addEventListener("click", applyQueueAIOrder);
    document.getElementById("btn-queue-ai-dismiss").addEventListener("click", dismissQueueAIOrder);
}

function toggleQueuePanel() {
    const panel = document.getElementById("queue-panel");
    if (panel.classList.contains("queue-open")) closeQueuePanel();
    else openQueuePanel();
}

function openQueuePanel() {
    document.getElementById("queue-panel").classList.add("queue-open");
    document.getElementById("queue-backdrop").classList.add("open");
    setNavActive("queue");
}

function closeQueuePanel() {
    document.getElementById("queue-panel").classList.remove("queue-open");
    document.getElementById("queue-backdrop").classList.remove("open");
    setNavActive("grid");
}

function renderQueuePanel() {
    const list  = document.getElementById("queue-list");
    const count = document.getElementById("queue-panel-count");
    const empty = queue.length === 0;

    count.textContent = queue.length;
    document.getElementById("btn-queue-ai-order").disabled = empty;
    document.getElementById("btn-queue-montage").disabled  = empty;
    document.getElementById("btn-queue-share").disabled    = empty;
    document.getElementById("btn-queue-regen").disabled    = empty;
    document.getElementById("btn-queue-retag").disabled    = empty;
    document.getElementById("btn-queue-clear").disabled    = empty;

    if (empty) {
        list.innerHTML = `<div class="queue-empty">Tap <strong>+</strong> on any clip thumbnail to add it to your queue.</div>`;
        if (queueSortable) { queueSortable.destroy(); queueSortable = null; }
        return;
    }

    list.innerHTML = queue.map((id, idx) => {
        const clip = allClips.find(c => c.id === id);
        if (!clip) return "";
        const thumb = clip.file_hash
            ? `<img src="/frames/${clip.file_hash}/frame_0001.jpg" onerror="this.style.display='none'" alt="">`
            : "";
        return `
            <div class="queue-item" data-id="${id}">
                <span class="queue-item-num">${idx + 1}</span>
                <div class="queue-item-thumb">${thumb}</div>
                <div class="queue-item-info">
                    <div class="queue-item-name">${esc(clip.filename)}</div>
                    ${clip.duration ? `<div class="queue-item-dur">${formatDuration(clip.duration)}</div>` : ""}
                </div>
                <button class="queue-item-remove" data-clip-id="${id}" title="Remove">×</button>
            </div>`;
    }).join("");

    list.querySelectorAll(".queue-item-remove").forEach(btn => {
        btn.addEventListener("click", () => removeFromQueue(parseInt(btn.dataset.clipId)));
    });

    // Drag-to-reorder within queue
    if (queueSortable) queueSortable.destroy();
    queueSortable = new Sortable(list, {
        animation: 150,
        ghostClass: "sortable-ghost",
        onEnd: () => {
            queue = [...list.querySelectorAll(".queue-item")].map(el => parseInt(el.dataset.id));
            updateQueueBadgesInGrid();
            renderQueuePanel();
        },
    });
}

// ─── Queue AI Order ───────────────────────────────────────────────
async function onQueueAIOrder() {
    if (!queue.length) return;
    const btn = document.getElementById("btn-queue-ai-order");
    btn.disabled = true;
    const orig = btn.innerHTML;
    let dots = 0;
    const ticker = setInterval(() => {
        dots = (dots + 1) % 4;
        btn.textContent = "Thinking" + ".".repeat(dots);
    }, 500);

    try {
        const resp = await fetch("/api/order/ai-suggest", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ clip_ids: queue }),
        });
        const data = await resp.json();
        if (data.error) { alert(data.error); return; }

        // Store suggestion — user reviews, then applies
        pendingQueueSuggestion = data.suggestion;
        showQueueAIResult(`AI suggested order for ${data.clip_count} clips.`);
    } catch (err) {
        alert("AI ordering failed: " + err.message);
    } finally {
        clearInterval(ticker);
        btn.disabled = false;
        btn.innerHTML = orig;
    }
}

function showQueueAIResult(msg) {
    const box = document.getElementById("queue-ai-result");
    document.getElementById("queue-ai-label").textContent = msg;
    box.style.display = "flex";
}

function applyQueueAIOrder() {
    if (!pendingQueueSuggestion) return;
    // Reorder the queue array to match the AI suggestion
    const suggestedIds = pendingQueueSuggestion.map(item => item.id);
    // Only include IDs that are currently in the queue
    const newOrder = suggestedIds.filter(id => queue.includes(id));
    // Append any that weren't in suggestion (shouldn't happen, but safety)
    queue.forEach(id => { if (!newOrder.includes(id)) newOrder.push(id); });
    queue = newOrder;
    pendingQueueSuggestion = null;
    document.getElementById("queue-ai-result").style.display = "none";
    updateQueueBadgesInGrid();
    renderQueuePanel();
}

function dismissQueueAIOrder() {
    pendingQueueSuggestion = null;
    document.getElementById("queue-ai-result").style.display = "none";
}

// ─── Queue Playlist Share ─────────────────────────────────────────
function shareQueuePlaylist() {
    if (!queue.length) return;
    const url     = window.location.origin + "/share/playlist?ids=" + queue.join(",");
    const urlBox  = document.getElementById("queue-share-url");
    const shareBtn = document.getElementById("btn-queue-share");

    // Always show the URL so user can see/copy it manually
    urlBox.textContent = url;
    urlBox.classList.add("visible");

    copyToClipboard(url, null, null, () => {
        const orig = shareBtn.textContent;
        shareBtn.textContent = "✓ Link Copied!";
        setTimeout(() => { shareBtn.textContent = orig; }, 2500);
    });
}

// ─── Clipboard helper ─────────────────────────────────────────────
// onSuccess callback is called if copy succeeds; falls back to showing the URL box.
function copyToClipboard(text, _unused, _unused2, onSuccess) {
    if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(() => {
            if (onSuccess) onSuccess();
        }).catch(() => {
            // Clipboard blocked — user can still manually copy from URL box
            if (onSuccess) onSuccess(); // still call so UI feedback shows
        });
    } else {
        // Non-secure context fallback
        try {
            const ta = document.createElement("textarea");
            ta.value = text;
            ta.style.position = "fixed"; ta.style.opacity = "0";
            document.body.appendChild(ta);
            ta.focus(); ta.select();
            document.execCommand("copy");
            document.body.removeChild(ta);
            if (onSuccess) onSuccess();
        } catch {
            if (onSuccess) onSuccess();
        }
    }
}

// ─── Queue Batch Regen ────────────────────────────────────────────
async function onQueueBatchRegen() {
    if (!queue.length) return;
    const ids   = [...queue];
    const btn   = document.getElementById("btn-queue-regen");
    const prog  = document.getElementById("queue-batch-progress");
    const bar   = document.getElementById("queue-batch-bar");
    const label = document.getElementById("queue-batch-label");

    btn.disabled = true;
    prog.style.display = "block";
    let done = 0;

    for (const id of ids) {
        const clip = allClips.find(c => c.id === id);
        label.textContent = `Regenerating ${done + 1}/${ids.length}: ${clip ? clip.filename : id}`;
        bar.style.width = `${Math.round(done / ids.length * 100)}%`;
        try {
            const resp = await fetch(`/api/clips/${id}/regenerate-synopsis`, { method: "POST" });
            const data = await resp.json();
            if (data.synopsis && clip) clip.synopsis = data.synopsis;
        } catch (e) {
            console.warn(`Regen failed for clip ${id}:`, e);
        }
        done++;
    }

    bar.style.width = "100%";
    label.textContent = `Done — regenerated ${done} synopsis${done !== 1 ? "es" : ""}`;
    setTimeout(() => { prog.style.display = "none"; }, 3000);
    btn.disabled = false;
    filterAndRender();
}

// ─── Queue Batch Re-tag ───────────────────────────────────────────
async function onQueueBatchRetag() {
    if (!queue.length) return;
    const ids   = [...queue];
    const btn   = document.getElementById("btn-queue-retag");
    const prog  = document.getElementById("queue-batch-progress");
    const bar   = document.getElementById("queue-batch-bar");
    const label = document.getElementById("queue-batch-label");

    btn.disabled = true;
    prog.style.display = "block";
    let done = 0;

    for (const id of ids) {
        const clip = allClips.find(c => c.id === id);
        label.textContent = `Re-tagging ${done + 1}/${ids.length}: ${clip ? clip.filename : id}`;
        bar.style.width = `${Math.round(done / ids.length * 100)}%`;
        try {
            const resp = await fetch(`/api/clips/${id}/regenerate-tags`, { method: "POST" });
            const data = await resp.json();
            if (data.tags && clip) clip.tags = data.tags;
        } catch (e) {
            console.warn(`Re-tag failed for clip ${id}:`, e);
        }
        done++;
    }

    bar.style.width = "100%";
    label.textContent = `Done — re-tagged ${done} clip${done !== 1 ? "s" : ""}`;
    setTimeout(() => { prog.style.display = "none"; }, 3000);
    btn.disabled = false;
    renderTags();
    filterAndRender();
}

// ─── Library-level AI Order (sidebar) ────────────────────────────
async function onLibraryAIOrder() {
    const btn = document.getElementById("btn-ai-order");
    btn.disabled = true;
    document.getElementById("btn-ai-order-label").textContent = "Thinking...";

    try {
        const resp = await fetch("/api/order/ai-suggest", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({}),
        });
        const data = await resp.json();
        if (data.error) { alert(data.error); return; }

        document.getElementById("ai-suggestion-label").textContent =
            `AI ordering suggestion ready (${data.clip_count} clips).`;
        document.getElementById("ai-suggestion").style.display = "flex";
    } catch (err) {
        alert("AI ordering failed: " + err.message);
    } finally {
        btn.disabled = false;
        document.getElementById("btn-ai-order-label").textContent = "AI Suggest Order";
    }
}

async function onApplyLibraryOrder() {
    try {
        const resp = await fetch("/api/order/apply", { method: "POST" });
        const data = await resp.json();
        if (data.error) { alert(data.error); return; }
        document.getElementById("ai-suggestion").style.display = "none";
        await loadClips();
        document.getElementById("sort-select").value = "position";
        filterAndRender();
    } catch (err) {
        alert("Failed to apply order: " + err.message);
    }
}

// ─── Drawer (left sidebar on mobile) ─────────────────────────────
function initDrawer() {
    document.getElementById("btn-drawer-open").addEventListener("click", openDrawer);
    document.getElementById("btn-drawer-close").addEventListener("click", closeDrawer);
    document.getElementById("sidebar-backdrop").addEventListener("click", closeDrawer);
}

function openDrawer() {
    document.getElementById("sidebar").classList.add("drawer-open");
    document.getElementById("sidebar-backdrop").classList.add("open");
    document.body.style.overflow = "hidden";
}

function closeDrawer() {
    document.getElementById("sidebar").classList.remove("drawer-open");
    document.getElementById("sidebar-backdrop").classList.remove("open");
    document.body.style.overflow = "";
}

// ─── Bottom Nav ───────────────────────────────────────────────────
function initBottomNav() {
    document.querySelectorAll(".nav-item").forEach(item => {
        item.addEventListener("click", () => {
            const tab = item.dataset.tab;
            switch (tab) {
                case "grid":    closeQueuePanel(); window.scrollTo({ top: 0, behavior: "smooth" }); break;
                case "search":  setNavActive("search"); toggleSearchBar(true); break;
                case "queue":   toggleQueuePanel(); break;
                case "outputs": openOutputsPanel(); break;
                case "menu":    openDrawer(); break;
            }
        });
    });
}

function setNavActive(tab) {
    document.querySelectorAll(".nav-item").forEach(i => i.classList.remove("active"));
    document.querySelector(`.nav-item[data-tab="${tab}"]`)?.classList.add("active");
}

// ─── Search Toggle (mobile) ───────────────────────────────────────
function initSearchToggle() {
    document.getElementById("btn-header-search")?.addEventListener("click", () => toggleSearchBar(true));
    document.getElementById("btn-search-close")?.addEventListener("click", () => {
        toggleSearchBar(false);
        setNavActive("grid");
    });
}

function toggleSearchBar(open) {
    const bar = document.getElementById("search-bar");
    if (open) {
        bar.classList.add("search-open");
        setTimeout(() => document.getElementById("search").focus(), 50);
    } else {
        bar.classList.remove("search-open");
        document.getElementById("search").value = "";
        filterAndRender();
    }
}

// ─── Montage Builder ──────────────────────────────────────────────
function openMontageModal() {
    const ids = [...queue];
    if (!ids.length) return;

    const names = ids.map(id => {
        const c = allClips.find(x => x.id === id);
        return c ? c.filename : `clip ${id}`;
    });

    document.getElementById("montage-clip-summary").innerHTML =
        `<strong>${ids.length} clip${ids.length !== 1 ? "s" : ""} in queue order:</strong><br>` +
        names.map((n, i) => `${i + 1}. ${esc(n)}`).join("<br>");

    document.getElementById("montage-name").value = "";
    document.getElementById("montage-reencode").checked = false;
    document.getElementById("montage-status").textContent = "";
    document.getElementById("btn-montage-render").disabled = false;
    document.getElementById("btn-montage-render").textContent = "Render";
    document.getElementById("montage-overlay").style.display = "flex";
}

async function onRenderMontage() {
    const ids = [...queue];
    if (!ids.length) return;

    const btn      = document.getElementById("btn-montage-render");
    const status   = document.getElementById("montage-status");
    const closeBtn = document.getElementById("montage-close");
    const filename = document.getElementById("montage-name").value.trim();
    const reencode = document.getElementById("montage-reencode").checked;

    btn.disabled = closeBtn.disabled = true;
    closeBtn.style.opacity = "0.3";

    let dots = 0;
    const ticker = setInterval(() => {
        dots = (dots + 1) % 4;
        btn.textContent = (reencode ? "Encoding" : "Rendering") + ".".repeat(dots);
    }, 600);
    status.textContent = reencode ? "Re-encoding — this may take a while..." : "Concatenating clips...";

    try {
        const resp = await fetch("/api/export/render", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ clip_ids: ids, filename, reencode }),
        });
        const data = await resp.json();
        clearInterval(ticker);

        if (data.error) {
            btn.textContent = "Render";
            btn.disabled = false;
            status.textContent = "";
            alert("Render failed: " + data.error + (data.details ? "\n\n" + data.details : ""));
        } else {
            btn.textContent = "Done ✓";
            status.innerHTML = `Saved <strong>${esc(data.filename)}</strong> (${data.size_mb} MB, ${data.clip_count} clips)` +
                (data.skipped?.length ? ` — ${data.skipped.length} skipped` : "");
        }
    } catch (err) {
        clearInterval(ticker);
        btn.textContent = "Render";
        btn.disabled = false;
        status.textContent = "";
        alert("Render failed: " + err.message);
    } finally {
        closeBtn.disabled = false;
        closeBtn.style.opacity = "1";
    }
}

// ─── Outputs Panel ────────────────────────────────────────────────
async function openOutputsPanel() {
    document.getElementById("outputs-overlay").style.display = "flex";
    await refreshOutputsList();
}

async function refreshOutputsList() {
    const container = document.getElementById("outputs-list");
    container.innerHTML = '<div class="loading">Loading...</div>';
    try {
        const resp  = await fetch("/api/outputs");
        const files = await resp.json();

        if (!files.length) {
            container.innerHTML = '<p style="color:var(--text2);font-size:0.9rem;padding:20px 0">No rendered outputs yet. Add clips to the Queue and tap Make Montage.</p>';
            return;
        }

        container.innerHTML = `
            <table class="outputs-table">
                <thead><tr><th>Filename</th><th>Size</th><th>Created</th><th></th></tr></thead>
                <tbody>
                    ${files.map(f => `
                        <tr data-filename="${esc(f.filename)}">
                            <td class="output-name">${esc(f.filename)}</td>
                            <td style="white-space:nowrap">${f.size_mb} MB</td>
                            <td style="white-space:nowrap">${esc(f.created_at)}</td>
                            <td class="output-actions">
                                <button class="btn btn-sm btn-primary btn-play-output">▶ Play</button>
                                <a class="btn btn-sm" href="/api/outputs/${encodeURIComponent(f.filename)}/video" download="${esc(f.filename)}">⬇ Download</a>
                                <button class="btn btn-sm btn-delete-output" style="color:var(--red)">✕</button>
                            </td>
                        </tr>`).join("")}
                </tbody>
            </table>
            <div id="output-player-container" style="display:none;margin-top:16px">
                <div style="font-size:0.8rem;color:var(--text2);margin-bottom:6px" id="output-player-label"></div>
                <video id="output-player" class="modal-video" controls style="max-height:360px"></video>
            </div>`;

        container.querySelectorAll(".btn-play-output").forEach(btn => {
            btn.addEventListener("click", () => {
                const filename = btn.closest("tr").dataset.filename;
                const player = document.getElementById("output-player");
                document.getElementById("output-player-label").textContent = filename;
                player.src = `/api/outputs/${encodeURIComponent(filename)}/video`;
                document.getElementById("output-player-container").style.display = "block";
                player.play();
                document.getElementById("output-player-container").scrollIntoView({ behavior: "smooth" });
            });
        });

        container.querySelectorAll(".btn-delete-output").forEach(btn => {
            btn.addEventListener("click", async () => {
                const filename = btn.closest("tr").dataset.filename;
                if (!confirm(`Delete ${filename}?`)) return;
                await fetch(`/api/outputs/${encodeURIComponent(filename)}`, { method: "DELETE" });
                await refreshOutputsList();
            });
        });
    } catch (err) {
        container.innerHTML = `<p style="color:var(--red)">Error: ${esc(err.message)}</p>`;
    }
}

// ─── Clip Detail Modal ────────────────────────────────────────────
function openModal(clipId) {
    const clip = allClips.find(c => c.id === clipId);
    if (!clip) return;

    const framesHTML = (clip.raw_frames || []).map((f, i) =>
        `<li><span class="frame-idx">#${i + 1}</span>${esc(f.description || "")}</li>`
    ).join("");

    document.getElementById("modal-content").innerHTML = `
        <h2>${esc(clip.filename)}</h2>
        <div class="clip-meta" style="margin-bottom:12px">
            <span>${clip.duration ? formatDuration(clip.duration) : "?"}</span>
            ${clip.width ? `<span>${clip.width}×${clip.height}</span>` : ""}
            ${clip.codec ? `<span>${clip.codec}</span>` : ""}
            <span class="clip-status ${clip.status}">${clip.status}</span>
        </div>

        <video class="modal-video" controls preload="metadata" src="/api/clips/${clip.id}/video">
            Your browser does not support video playback.
        </video>

        <div class="field">
            <label>Thumbnail
                <span style="font-size:0.72rem;color:var(--text2);font-weight:normal;margin-left:6px">click a frame to set</span>
            </label>
            <div id="thumb-picker" class="thumb-picker">
                <span class="thumb-picker-empty">Loading frames…</span>
            </div>
        </div>

        <div class="field">
            <label>Synopsis
                <button id="btn-regen" class="btn btn-sm" style="margin-left:8px">↻ Regenerate</button>
            </label>
            <div id="modal-synopsis" style="font-size:0.9rem">${esc(clip.synopsis || "—")}</div>
        </div>

        <div class="field">
            <label>Transcript
                <button id="btn-transcribe" class="btn btn-sm" style="margin-left:8px">${clip.transcript ? "↻ Re-transcribe" : "▶ Transcribe"}</button>
                <span style="color:var(--text2);font-weight:normal;font-size:0.72rem;margin-left:6px">(editable)</span>
            </label>
            <textarea id="modal-transcript" placeholder="No transcript yet — click Transcribe to run Whisper...">${esc(clip.transcript || "")}</textarea>
        </div>

        ${framesHTML ? `<div class="field"><label>Frame Descriptions</label><ul class="frame-list">${framesHTML}</ul></div>` : ""}

        <div class="field">
            <label>Tags
                <button id="btn-retag" class="btn btn-sm" style="margin-left:8px" title="Auto-generate tags from video content">↻ Auto-tag</button>
            </label>
            <input type="text" id="modal-tags" value="${esc(clip.tags || "")}" placeholder="tag1, tag2, ...">
        </div>

        <div class="field">
            <label>Notes</label>
            <textarea id="modal-notes" placeholder="Your notes...">${esc(clip.notes || "")}</textarea>
        </div>

        <div style="display:flex;align-items:center;gap:10px;margin-top:8px;flex-wrap:wrap">
            <button class="btn btn-primary" id="modal-save">Save</button>
            <button class="btn" id="modal-share" title="Copy shareable link for this clip">🔗 Share</button>
            <span id="modal-saved" style="color:var(--green);font-size:0.85rem;display:none">Saved</span>
        </div>
    `;

    document.getElementById("modal-share").addEventListener("click", () => {
        const url = window.location.origin + "/share/clip/" + clipId;
        const btn = document.getElementById("modal-share");
        copyToClipboard(url, null, null, () => {
            const orig = btn.textContent;
            btn.textContent = "✓ Copied!";
            setTimeout(() => { btn.textContent = orig; }, 2000);
        });
    });

    document.getElementById("modal-save").addEventListener("click", () => {
        requireAuth(async () => {
            const tags       = document.getElementById("modal-tags").value;
            const notes      = document.getElementById("modal-notes").value;
            const transcript = document.getElementById("modal-transcript").value;
            await fetch(`/api/clips/${clipId}`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ tags, notes, transcript }),
            });
            clip.tags = tags; clip.notes = notes; clip.transcript = transcript;
            renderTags();
            filterAndRender();
            closeModal();
        });
    });

    document.getElementById("btn-transcribe").addEventListener("click", () => requireAuth(async () => {
        const btn         = document.getElementById("btn-transcribe");
        const transcriptEl = document.getElementById("modal-transcript");
        const closeBtn    = document.getElementById("modal-close");
        modalBusy = true;
        btn.disabled = closeBtn.disabled = true;
        closeBtn.style.opacity = "0.3";
        transcriptEl.style.opacity = "0.4";
        let dots = 0;
        const ticker = setInterval(() => {
            dots = (dots + 1) % 4;
            btn.textContent = "Transcribing" + ".".repeat(dots);
        }, 500);
        try {
            const resp = await fetch(`/api/clips/${clipId}/transcribe`, { method: "POST" });
            const data = await resp.json();
            if (data.error) {
                alert(data.error);
            } else if (!data.transcript) {
                transcriptEl.value = "";
                transcriptEl.placeholder = "No speech detected in this clip.";
                clip.transcript = "";
            } else {
                transcriptEl.value = data.transcript;
                clip.transcript = data.transcript;
                btn.textContent = "↻ Re-transcribe";
            }
        } catch (err) {
            alert("Transcription failed: " + err.message);
        } finally {
            clearInterval(ticker);
            modalBusy = false;
            btn.disabled = closeBtn.disabled = false;
            closeBtn.style.opacity = "1";
            transcriptEl.style.opacity = "1";
            if (btn.textContent.startsWith("Transcribing"))
                btn.textContent = clip.transcript ? "↻ Re-transcribe" : "▶ Transcribe";
        }
    }));

    document.getElementById("btn-regen").addEventListener("click", () => requireAuth(async () => {
        const btn       = document.getElementById("btn-regen");
        const synopsisEl = document.getElementById("modal-synopsis");
        const closeBtn  = document.getElementById("modal-close");

        const tags       = document.getElementById("modal-tags").value;
        const notes      = document.getElementById("modal-notes").value;
        const transcript = document.getElementById("modal-transcript").value;
        await fetch(`/api/clips/${clipId}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ tags, notes, transcript }),
        });
        clip.tags = tags; clip.notes = notes; clip.transcript = transcript;

        modalBusy = true;
        btn.disabled = closeBtn.disabled = true;
        closeBtn.style.opacity = "0.3";
        synopsisEl.innerHTML = '<span class="regen-spinner">Generating synopsis</span>';
        let dots = 0;
        const ticker = setInterval(() => {
            dots = (dots + 1) % 4;
            const el = document.querySelector(".regen-spinner");
            if (el) el.textContent = "Generating synopsis" + ".".repeat(dots);
        }, 500);
        try {
            const resp = await fetch(`/api/clips/${clipId}/regenerate-synopsis`, { method: "POST" });
            const data = await resp.json();
            if (data.error) {
                synopsisEl.textContent = clip.synopsis || "—";
                alert(data.error);
            } else {
                synopsisEl.textContent = data.synopsis;
                clip.synopsis = data.synopsis;
                filterAndRender();
            }
        } catch (err) {
            synopsisEl.textContent = clip.synopsis || "—";
            alert("Regeneration failed: " + err.message);
        } finally {
            clearInterval(ticker);
            modalBusy = false;
            btn.disabled = closeBtn.disabled = false;
            closeBtn.style.opacity = "1";
            btn.textContent = "↻ Regenerate";
        }
    }));

    document.getElementById("btn-retag").addEventListener("click", () => requireAuth(async () => {
        const btn     = document.getElementById("btn-retag");
        const tagsEl  = document.getElementById("modal-tags");
        const closeBtn = document.getElementById("modal-close");
        modalBusy = true;
        btn.disabled = closeBtn.disabled = true;
        closeBtn.style.opacity = "0.3";
        const orig = btn.textContent;
        btn.textContent = "Tagging…";
        try {
            const resp = await fetch(`/api/clips/${clipId}/regenerate-tags`, { method: "POST" });
            const data = await resp.json();
            if (data.error) {
                alert(data.error);
            } else {
                tagsEl.value = data.tags;
                clip.tags = data.tags;
                renderTags();
                filterAndRender();
            }
        } catch (err) {
            alert("Re-tagging failed: " + err.message);
        } finally {
            modalBusy = false;
            btn.disabled = closeBtn.disabled = false;
            closeBtn.style.opacity = "1";
            btn.textContent = orig;
        }
    }));

    document.getElementById("modal-overlay").style.display = "flex";

    // Load thumbnail picker asynchronously (non-blocking)
    loadThumbPicker(clip);
}

async function loadThumbPicker(clip) {
    const picker = document.getElementById("thumb-picker");
    if (!picker) return;

    try {
        const resp = await fetch(`/api/clips/${clip.id}/frames`);
        const data = await resp.json();

        if (!data.frames || data.frames.length === 0) {
            picker.innerHTML = `
                <span class="thumb-picker-empty">No frames on disk.&nbsp;</span>
                <button id="btn-extract-frames" class="btn btn-sm btn-primary">Extract Frames</button>`;
            document.getElementById("btn-extract-frames")?.addEventListener("click", () => requireAuth(async () => {
                const btn = document.getElementById("btn-extract-frames");
                btn.disabled = true;
                btn.textContent = "Extracting…";
                try {
                    const r = await fetch(`/api/clips/${clip.id}/extract-frames`, { method: "POST" });
                    const d = await r.json();
                    if (d.error) { alert(d.error); btn.disabled = false; btn.textContent = "Extract Frames"; }
                    else { await loadThumbPicker(clip); }
                } catch (err) {
                    alert("Failed: " + err.message);
                    btn.disabled = false; btn.textContent = "Extract Frames";
                }
            }));
            return;
        }

        const current = data.thumbnail_frame || "frame_0001.jpg";
        picker.innerHTML = data.frames.map(fname => `
            <div class="thumb-picker-item${fname === current ? " selected" : ""}"
                 data-frame="${fname}" title="${fname}">
                <img src="/frames/${clip.file_hash}/${fname}"
                     onerror="this.parentElement.style.display='none'" alt="${fname}">
            </div>`).join("");

        picker.querySelectorAll(".thumb-picker-item").forEach(item => {
            item.addEventListener("click", () => requireAuth(async () => {
                const frame = item.dataset.frame;
                try {
                    await fetch(`/api/clips/${clip.id}/thumbnail`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ frame }),
                    });
                    // Update selection highlight
                    picker.querySelectorAll(".thumb-picker-item").forEach(i =>
                        i.classList.toggle("selected", i === item)
                    );
                    // Update in-memory clip and refresh card
                    clip.thumbnail_frame = frame;
                    filterAndRender();
                } catch (err) {
                    alert("Failed to set thumbnail: " + err.message);
                }
            }));
        });
    } catch (err) {
        picker.innerHTML = `<span class="thumb-picker-empty">Could not load frames.</span>`;
    }
}

function closeModal() {
    if (modalBusy) return;
    document.getElementById("modal-overlay").style.display = "none";
}

// ─── Ingest Polling ───────────────────────────────────────────────
async function pollIngestStatus() {
    try {
        const resp = await fetch("/api/ingest/status");
        const s    = await resp.json();
        const banner   = document.getElementById("ingest-banner");
        const isActive = s.processing > 0 || (s.pending > 0 && s.total > 0);

        if (s.total === 0) {
            banner.style.display = "none";
            ingestPollTimer = setTimeout(pollIngestStatus, 5000);
            return;
        }

        banner.style.display = "block";
        document.getElementById("progress-bar").style.width = s.pct + "%";
        document.getElementById("ingest-pct").textContent   = s.pct + "%";

        const parts = [`${s.done} done`];
        if (s.error    > 0) parts.push(`${s.error} errors`);
        if (s.processing>0) parts.push(`${s.processing} processing`);
        if (s.pending  > 0) parts.push(`${s.pending} pending`);
        document.getElementById("ingest-label").textContent = parts.join(" · ");

        if (s.current) {
            document.getElementById("ingest-current").textContent = `Processing: ${s.current.filename}`;
        } else if (!isActive && s.done > 0) {
            document.getElementById("ingest-current").textContent =
                s.error > 0 ? `Completed with ${s.error} error(s)` : "All clips processed!";
        } else {
            document.getElementById("ingest-current").textContent = "";
        }

        if (s.done > lastDoneCount) {
            lastDoneCount = s.done;
            await loadClips();
        }

        ingestPollTimer = setTimeout(pollIngestStatus, isActive ? 4000 : 15000);
    } catch {
        ingestPollTimer = setTimeout(pollIngestStatus, 10000);
    }
}

// ─── Auth ─────────────────────────────────────────────────────────
async function initAuth() {
    const resp = await fetch("/api/auth/status");
    const data = await resp.json();
    isAuthenticated = data.authenticated;
    applyAuthState(data.required);

    document.getElementById("btn-login-submit").addEventListener("click", submitLogin);
    document.getElementById("login-password").addEventListener("keydown", (e) => {
        if (e.key === "Enter") submitLogin();
    });
    document.getElementById("btn-logout")?.addEventListener("click", async () => {
        await fetch("/api/auth/logout", { method: "POST" });
        isAuthenticated = false;
        applyAuthState(true);
    });
}

function applyAuthState(authRequired) {
    // Show logout button only when logged in and auth is enabled
    const logoutBtn = document.getElementById("btn-logout");
    if (logoutBtn) logoutBtn.style.display = isAuthenticated && authRequired ? "block" : "none";

    // Show a "Sign in" hint in the header queue button when auth required and not logged in
    const queueToggle = document.getElementById("btn-queue-toggle");
    if (queueToggle) queueToggle.title = (!authRequired || isAuthenticated) ? "Queue" : "Queue (sign in to make changes)";
}

function showLoginModal(onSuccess) {
    const overlay = document.getElementById("login-overlay");
    document.getElementById("login-password").value = "";
    document.getElementById("login-error").style.display = "none";
    overlay.style.display = "flex";
    setTimeout(() => document.getElementById("login-password").focus(), 50);
    overlay._onSuccess = onSuccess;
}

function closeLoginModal() {
    document.getElementById("login-overlay").style.display = "none";
}

async function submitLogin() {
    const password = document.getElementById("login-password").value;
    const errEl = document.getElementById("login-error");
    const btn = document.getElementById("btn-login-submit");
    btn.disabled = true;
    btn.textContent = "Signing in...";
    errEl.style.display = "none";

    try {
        const resp = await fetch("/api/auth/login", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ password }),
        });
        if (resp.ok) {
            isAuthenticated = true;
            closeLoginModal();
            applyAuthState(true);
            const cb = document.getElementById("login-overlay")._onSuccess;
            if (cb) cb();
        } else {
            errEl.style.display = "inline";
        }
    } finally {
        btn.disabled = false;
        btn.textContent = "Sign In";
    }
}

// Wrap any action that requires auth
function requireAuth(action) {
    if (isAuthenticated) {
        action();
    } else {
        showLoginModal(action);
    }
}

// ─── Ingest Modal ─────────────────────────────────────────────────
function initIngestModal() {
    document.getElementById("btn-ingest").addEventListener("click", () => {
        requireAuth(() => { closeDrawer(); openIngestModal(); });
    });
    document.getElementById("btn-fix-thumbnails").addEventListener("click", () => onExtractFrames(false));
    document.getElementById("btn-reextract-all-thumbnails").addEventListener("click", () => onExtractFrames(true));
    document.getElementById("ingest-close").addEventListener("click", closeIngestModal);
    document.getElementById("btn-ingest-modal-cancel").addEventListener("click", closeIngestModal);
    document.getElementById("ingest-overlay").addEventListener("click", (e) => {
        if (e.target === e.currentTarget) closeIngestModal();
    });
    document.getElementById("btn-ingest-preview").addEventListener("click", onIngestPreview);
    document.getElementById("ingest-reprocess-all").addEventListener("change", onReprocessAllToggle);
    document.getElementById("btn-ingest-start").addEventListener("click", onIngestStart);

    // Folder picker
    document.getElementById("btn-folder-browse").addEventListener("click", () => openFolderPicker());
    document.getElementById("folder-picker-close").addEventListener("click", closeFolderPicker);
    document.getElementById("btn-folder-picker-cancel").addEventListener("click", closeFolderPicker);
    document.getElementById("folder-picker-overlay").addEventListener("click", (e) => {
        if (e.target === e.currentTarget) closeFolderPicker();
    });
    document.getElementById("btn-folder-picker-select").addEventListener("click", onFolderPickerSelect);
}

// ─── Folder Picker ────────────────────────────────────────────────
let _folderPickerCurrent = "";

async function openFolderPicker(path) {
    document.getElementById("folder-picker-overlay").style.display = "flex";
    await navigateFolderPicker(path || document.getElementById("ingest-dir-input").value.trim() || "");
}

function closeFolderPicker() {
    document.getElementById("folder-picker-overlay").style.display = "none";
}

function onFolderPickerSelect() {
    document.getElementById("ingest-dir-input").value = _folderPickerCurrent;
    closeFolderPicker();
}

async function navigateFolderPicker(path) {
    const list = document.getElementById("folder-picker-list");
    const pathEl = document.getElementById("folder-picker-path");
    list.innerHTML = `<div class="folder-picker-item" style="color:var(--text2);cursor:default">Loading…</div>`;

    try {
        const params = path ? `?path=${encodeURIComponent(path)}` : "";
        const resp = await fetch(`/api/fs/browse${params}`);
        const data = await resp.json();
        if (!resp.ok) {
            list.innerHTML = `<div class="folder-picker-item" style="color:var(--red);cursor:default">${esc(data.detail || "Error")}</div>`;
            return;
        }

        _folderPickerCurrent = data.path;
        pathEl.textContent = data.path;

        const folderSvg = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>`;
        const upSvg = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"/></svg>`;

        let html = "";
        if (data.parent) {
            html += `<div class="folder-picker-item up" data-path="${esc(data.parent)}">${upSvg} .. (up one level)</div>`;
        }
        if (data.dirs.length === 0 && !data.parent) {
            html += `<div class="folder-picker-item" style="color:var(--text2);cursor:default">No subdirectories</div>`;
        }
        html += data.dirs.map(d => {
            const full = data.path.endsWith("/") ? data.path + d : data.path + "/" + d;
            return `<div class="folder-picker-item" data-path="${esc(full)}">${folderSvg} ${esc(d)}</div>`;
        }).join("");
        list.innerHTML = html;

        list.querySelectorAll(".folder-picker-item[data-path]").forEach(item => {
            item.addEventListener("click", () => navigateFolderPicker(item.dataset.path));
        });
    } catch (err) {
        list.innerHTML = `<div class="folder-picker-item" style="color:var(--red);cursor:default">Error: ${esc(err.message)}</div>`;
    }
}

let _framesProgressTimer = null;

function openIngestModal() {
    // Reset state
    document.getElementById("ingest-dir-input").value = "";
    document.getElementById("ingest-preview-area").style.display = "none";
    document.getElementById("ingest-preview-empty").style.display = "none";
    document.getElementById("ingest-reprocess-all").checked = false;
    document.getElementById("ingest-reprocess-warning").style.display = "none";
    document.getElementById("ingest-start-error").style.display = "none";
    document.getElementById("btn-ingest-start").disabled = true;
    document.getElementById("ingest-overlay").style.display = "flex";
    document.getElementById("ingest-dir-input").focus();
    loadMissingFramesInfo();
}

function closeIngestModal() {
    document.getElementById("ingest-overlay").style.display = "none";
    if (_framesProgressTimer) { clearTimeout(_framesProgressTimer); _framesProgressTimer = null; }
}

async function loadMissingFramesInfo() {
    try {
        const resp = await fetch("/api/ingest/missing-frames");
        if (!resp.ok) return;
        const data = await resp.json();
        const status = document.getElementById("ingest-missing-status");
        const fixBtn = document.getElementById("btn-fix-thumbnails");
        const reextractBtn = document.getElementById("btn-reextract-all-thumbnails");

        if (data.running) {
            const pct = data.total > 0 ? Math.round((data.done / data.total) * 100) : 0;
            const modeLabel = data.mode === "all" ? "Regenerating all thumbnails" : "Extracting missing thumbnails";
            status.textContent = `${modeLabel}… ${data.done} / ${data.total} clips`;
            fixBtn.disabled = true;
            reextractBtn.disabled = true;
            document.getElementById("ingest-frames-progress").style.display = "block";
            document.getElementById("ingest-frames-bar").style.width = pct + "%";
            document.getElementById("ingest-frames-label").textContent = `${data.done} of ${data.total} done`;
            _framesProgressTimer = setTimeout(loadMissingFramesInfo, 2000);
        } else {
            document.getElementById("ingest-frames-progress").style.display = "none";
            reextractBtn.disabled = false;
            reextractBtn.textContent = `Regenerate all (${data.total})`;
            if (data.count > 0) {
                status.textContent = `${data.count} of ${data.total} clip${data.total !== 1 ? "s" : ""} have no thumbnails`;
                fixBtn.disabled = false;
                fixBtn.textContent = `Extract missing (${data.count})`;
            } else {
                status.textContent = `All ${data.total} clips have thumbnails`;
                fixBtn.disabled = true;
                fixBtn.textContent = `Extract missing (0)`;
            }
        }
    } catch (_) {}
}

async function onExtractFrames(force) {
    document.getElementById("ingest-start-error").style.display = "none";
    document.getElementById("btn-fix-thumbnails").disabled = true;
    document.getElementById("btn-reextract-all-thumbnails").disabled = true;
    try {
        const resp = await fetch("/api/ingest/extract-missing-frames", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ force }),
        });
        const data = await resp.json();
        if (!resp.ok) {
            document.getElementById("ingest-start-error").textContent = data.detail || "Failed to start";
            document.getElementById("ingest-start-error").style.display = "block";
            loadMissingFramesInfo();
            return;
        }
        loadMissingFramesInfo();
    } catch (err) {
        document.getElementById("ingest-start-error").textContent = "Could not reach server: " + err.message;
        document.getElementById("ingest-start-error").style.display = "block";
        loadMissingFramesInfo();
    }
}

async function onIngestPreview() {
    const dir = document.getElementById("ingest-dir-input").value.trim();
    if (!dir) return;

    const btn = document.getElementById("btn-ingest-preview");
    btn.disabled = true;
    btn.textContent = "Loading…";
    document.getElementById("ingest-start-error").style.display = "none";

    try {
        const resp = await fetch("/api/ingest/list-videos", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ directory: dir }),
        });
        const data = await resp.json();
        if (!resp.ok) {
            showIngestError(data.detail || "Failed to list videos");
            document.getElementById("ingest-preview-area").style.display = "none";
            document.getElementById("ingest-preview-empty").style.display = "none";
            document.getElementById("btn-ingest-start").disabled = true;
            return;
        }
        if (data.count === 0) {
            document.getElementById("ingest-preview-area").style.display = "none";
            document.getElementById("ingest-preview-empty").style.display = "block";
            document.getElementById("btn-ingest-start").disabled = true;
        } else {
            document.getElementById("ingest-preview-count").textContent =
                `${data.count} video file${data.count !== 1 ? "s" : ""} found`;
            document.getElementById("ingest-preview-list").textContent = data.files.join("\n");
            document.getElementById("ingest-preview-area").style.display = "block";
            document.getElementById("ingest-preview-empty").style.display = "none";
            document.getElementById("btn-ingest-start").disabled = false;
        }
    } catch (err) {
        showIngestError("Could not reach server: " + err.message);
    } finally {
        btn.disabled = false;
        btn.textContent = "Preview";
    }
}

function onReprocessAllToggle() {
    const checked = document.getElementById("ingest-reprocess-all").checked;
    const warning = document.getElementById("ingest-reprocess-warning");
    if (checked) {
        const count = allClips.length;
        document.getElementById("ingest-reprocess-count").textContent =
            `all ${count} clip${count !== 1 ? "s" : ""}`;
        document.getElementById("ingest-reprocess-list").textContent =
            allClips.map(c => c.filename).join("\n");
        warning.style.display = "block";
    } else {
        warning.style.display = "none";
    }
}

async function onIngestStart() {
    const dir = document.getElementById("ingest-dir-input").value.trim();
    const reprocessAll = document.getElementById("ingest-reprocess-all").checked;
    const btn = document.getElementById("btn-ingest-start");

    btn.disabled = true;
    btn.textContent = "Starting…";
    document.getElementById("ingest-start-error").style.display = "none";

    try {
        const resp = await fetch("/api/ingest/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ directory: dir, reprocess_all: reprocessAll }),
        });
        const data = await resp.json();
        if (!resp.ok) {
            showIngestError(data.detail || "Failed to start ingest");
            btn.disabled = false;
            btn.textContent = "Start Ingest";
            return;
        }
        closeIngestModal();
    } catch (err) {
        showIngestError("Could not reach server: " + err.message);
        btn.disabled = false;
        btn.textContent = "Start Ingest";
    }
}

function showIngestError(msg) {
    const el = document.getElementById("ingest-start-error");
    el.textContent = msg;
    el.style.display = "block";
}

// ─── Utilities ────────────────────────────────────────────────────
function formatDuration(secs) {
    const m = Math.floor(secs / 60);
    const s = Math.floor(secs % 60);
    return m > 0 ? `${m}:${s.toString().padStart(2, "0")}` : `${s}s`;
}

function esc(str) {
    const el = document.createElement("span");
    el.textContent = str;
    return el.innerHTML;
}

function debounce(fn, ms) {
    let timer;
    return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}
