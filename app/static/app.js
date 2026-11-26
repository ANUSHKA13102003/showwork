(function () {
  "use strict";

  let activeChart = null;
  let briefingChart = null;
  let briefingBooted = false;
  let scorecardLoaded = false;
  let lastWorkItems = [];
  let defaultWork = null;

  const el = (id) => document.getElementById(id);

  function escapeHtml(val) {
    return String(val ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtInt(n) {
    if (n === null || n === undefined || Number.isNaN(Number(n))) return "n/a";
    return Math.round(Number(n)).toLocaleString("en-GB");
  }

  function fmtMoney(v) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "n/a";
    const num = Number(v);
    const dec = Math.abs(num) < 1000 ? 2 : 0;
    const sign = num < 0 ? "-" : "";
    return (
      sign +
      "£" +
      Math.abs(num).toLocaleString("en-GB", {
        minimumFractionDigits: dec,
        maximumFractionDigits: dec,
      })
    );
  }

  function fmtSignedMoney(v) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "n/a";
    const num = Number(v);
    return (num >= 0 ? "+" : "-") + fmtMoney(Math.abs(num));
  }

  function fmtPct(p) {
    if (p === null || p === undefined || Number.isNaN(Number(p))) return "n/a";
    return (Number(p) * 100).toFixed(1) + "%";
  }

  function fmtWilson(metricObj) {
    if (!metricObj) return "n/a";
    const p = fmtPct(metricObj.rate);
    const lo = (metricObj.ci_low * 100).toFixed(1);
    const hi = (metricObj.ci_high * 100).toFixed(1);
    return `${p} (${metricObj.passed}/${metricObj.total}, 95% CI ${lo}–${hi}%)`;
  }

  function updateModeUI(modeObj) {
    if (!modeObj) return;
    const dot = el("mode-dot");
    const lbl = el("mode-label");
    const banner = el("mode-banner");
    dot.className = "mode-dot " + (modeObj.mode === "llm" ? "llm" : "nokey");
    lbl.textContent = modeObj.mode === "llm" ? "Mode: LLM + compiler" : "Mode: Rule-based (no key)";
    banner.textContent = modeObj.reason || "";
  }

  async function bootStatus() {
    const wake = el("wakeup-screen");
    const elapsedEl = el("wakeup-elapsed");
    let finished = false;
    const started = Date.now();

    const slowTimer = setTimeout(() => {
      if (!finished) wake.classList.remove("hidden");
    }, 900);

    const tick = setInterval(() => {
      const s = Math.floor((Date.now() - started) / 1000);
      elapsedEl.textContent = `Elapsed: ${s}s`;
    }, 1000);

    while (!finished) {
      try {
        const res = await fetch("/api/status", { headers: { Accept: "application/json" } });
        if (res.ok) {
          const data = await res.json();
          finished = true;
          clearTimeout(slowTimer);
          clearInterval(tick);
          wake.classList.add("hidden");
          updateModeUI(data.mode);
          if (data.as_of) {
            el("footer-asof").textContent = `Data through ${data.as_of} · ${fmtInt(data.rows)} clean lines`;
          }
          renderSuggested(data.suggested_questions || []);
          return;
        }
      } catch (_) {
        wake.classList.remove("hidden");
      }
      await new Promise((r) => setTimeout(r, 2000));
    }
  }

  function formatFilters(work) {
    const parts = [];
    if (work.time_range) {
      parts.push(`date in [${work.time_range.start}, ${work.time_range.end})`);
    } else {
      parts.push("all dates (2009-12-01 to 2011-12-09)");
    }
    if (work.filters && work.filters.length) {
      work.filters.forEach((f) => {
        parts.push(`${f.dimension} in (${f.values.join(", ")})`);
      });
    }
    if (work.compare_to) {
      parts.push(`compared to ${work.compare_to.replace("_", " ")}`);
    }
    return parts.join(" · ");
  }

  function openDrawerWork(work, selectedLabel) {
    if (!work) return;
    el("dw-metric").textContent = work.metric;
    el("dw-version").textContent = `v${work.definition_version}`;
    el("dw-definition").textContent = work.definition;
    el("dw-grain").textContent = work.grain;
    el("dw-owner").textContent = work.owner;
    el("dw-scanned").textContent = fmtInt(work.rows_scanned);
    el("dw-asof").textContent = work.as_of;
    el("dw-filters").textContent = formatFilters(work);
    el("dw-caveats").textContent = work.caveats;
    el("dw-sql").textContent = work.sql;

    const sel = el("dw-selected-val");
    if (selectedLabel) {
      sel.textContent = `Selected figure: ${selectedLabel}`;
      sel.classList.remove("hidden");
    } else {
      sel.classList.add("hidden");
    }
    el("drawer-backdrop").classList.remove("hidden");
    el("work-drawer").classList.remove("hidden");
  }

  function closeDrawerWork() {
    el("drawer-backdrop").classList.add("hidden");
    el("work-drawer").classList.add("hidden");
  }

  /* --- BRIEFING VIEW --- */
  async function bootBriefing() {
    if (briefingBooted) return;
    const loading = el("briefing-loading");
    const errBox = el("briefing-error");
    loading.classList.remove("hidden");
    errBox.classList.add("hidden");

    try {
      const res = await fetch("/api/briefings", { headers: { Accept: "application/json" } });
      if (!res.ok) throw new Error(`Failed to list briefing weeks (${res.status})`);
      const data = await res.json();
      const weeks = data.weeks || [];
      const sel = el("briefing-week");
      sel.innerHTML = "";
      weeks.forEach((w) => {
        const opt = document.createElement("option");
        opt.value = w.week;
        opt.textContent = w.saved ? `${w.week} (backfilled)` : w.week;
        sel.appendChild(opt);
      });
      briefingBooted = true;
      if (weeks.length) {
        await loadBriefingWeek(weeks[0].week);
      }
    } catch (err) {
      errBox.textContent = err.message || "Could not load briefing weeks.";
      errBox.classList.remove("hidden");
      loading.classList.add("hidden");
    }
  }

  async function loadBriefingWeek(weekStr) {
    const loading = el("briefing-loading");
    const errBox = el("briefing-error");
    const content = el("briefing-content");
    loading.classList.remove("hidden");
    errBox.classList.add("hidden");

    try {
      const res = await fetch(`/api/briefing/${encodeURIComponent(weekStr)}`, {
        headers: { Accept: "application/json" },
      });
      if (!res.ok) throw new Error(`Could not load week ${weekStr} (${res.status})`);
      const doc = await res.json();
      renderBriefing(doc);
      content.classList.remove("hidden");
    } catch (err) {
      errBox.textContent = err.message || "Could not load briefing.";
      errBox.classList.remove("hidden");
    } finally {
      loading.classList.add("hidden");
    }
  }

  function renderBriefingSummaryText(text, doc) {
    const p = el("briefing-summary");
    p.innerHTML = "";
    const defaultKpiWork = doc.kpis && doc.kpis[0] ? doc.kpis[0].show_work : null;
    const kpiLookup = {};
    (doc.kpis || []).forEach((k) => {
      [k.current_fmt, k.wow_change_fmt, k.wow_pct_fmt, k.avg4_fmt, k.vs_avg4_pct_fmt, k.wow_prev_fmt].forEach((s) => {
        if (s && s !== "n/a") kpiLookup[s] = k.show_work;
      });
    });
    const rx = /([+\-]?£[\d,]+(?:\.\d+)?|[+\-]?\d+(?:\.\d+)?%)/g;
    let last = 0;
    let m;
    while ((m = rx.exec(text)) !== null) {
      if (m.index > last) {
        p.appendChild(document.createTextNode(text.slice(last, m.index)));
      }
      const tok = m[0];
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "num-chip";
      btn.textContent = tok;
      btn.title = "Click to inspect Show work";
      const w = kpiLookup[tok] || defaultKpiWork;
      btn.addEventListener("click", () => openDrawerWork(w, tok));
      p.appendChild(btn);
      last = m.index + tok.length;
    }
    if (last < text.length) {
      p.appendChild(document.createTextNode(text.slice(last)));
    }
  }

  function renderBriefing(doc) {
    const seasonal = el("briefing-seasonal");
    if (doc.seasonal_note) {
      seasonal.textContent = doc.seasonal_note;
      seasonal.classList.remove("hidden");
    } else {
      seasonal.classList.add("hidden");
    }

    const sumObj = doc.summary || {};
    const ver = sumObj.verification || {};
    const srcLabels = {
      llm_verified: "Gemini summary · 100% numeric tokens verified",
      llm_verified_retry: "Gemini summary (retry) · 100% numeric tokens verified",
      template_fallback: "Deterministic template fallback · 100% verified",
      template: "Deterministic template · 100% verified",
    };
    el("briefing-badge").textContent = ver.ok ? "Verified summary" : "Summary";
    el("briefing-meta").textContent =
      `${srcLabels[sumObj.source] || sumObj.source || ""} · ${ver.word_count || 0} words · Week ${doc.week} to ${doc.week_end_inclusive}`;
    renderBriefingSummaryText(sumObj.text || "", doc);

    // KPI cards
    const kpiBox = el("briefing-kpis");
    kpiBox.innerHTML = "";
    (doc.kpis || []).forEach((k) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "kpi-btn";
      const up = Number(k.wow_change) >= 0;
      const deltaCls = k.key === "return_rate" ? (up ? "neg" : "pos") : up ? "pos" : "neg";
      btn.innerHTML = `
        <div class="kpi-label">${escapeHtml(k.label)}</div>
        <div class="kpi-val">${escapeHtml(k.current_fmt)}</div>
        <div class="kpi-delta ${deltaCls}">${escapeHtml(k.wow_change_fmt)} (${escapeHtml(k.wow_pct_fmt)}) WoW</div>
        <div class="kpi-sub">${escapeHtml(k.vs_avg4_pct_fmt)} vs 4-wk avg (${escapeHtml(k.avg4_fmt)})</div>
      `;
      btn.addEventListener("click", () => {
        openDrawerWork(k.show_work, `${k.label}: ${k.current_fmt} (week of ${doc.week})`);
      });
      kpiBox.appendChild(btn);
    });

    // Driver chart + 3 driver tables
    const drv = doc.drivers || {};
    const catMembers = drv.dimensions?.category?.members || [];
    el("briefing-chart-title").textContent =
      `Category drivers of net revenue change (${fmtSignedMoney(drv.change)} WoW vs ${doc.previous_week})`;

    if (briefingChart) {
      briefingChart.destroy();
      briefingChart = null;
    }
    if (window.Chart && catMembers.length) {
      const ctx = el("briefing-chart").getContext("2d");
      briefingChart = new window.Chart(ctx, {
        type: "bar",
        data: {
          labels: catMembers.map((m) => m.member),
          datasets: [
            {
              data: catMembers.map((m) => m.change),
              backgroundColor: catMembers.map((m) => (m.change < 0 ? "#b84339" : "#0f5e4c")),
              borderRadius: 4,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { grid: { display: false }, ticks: { font: { size: 11 } } },
            y: {
              grid: { color: "#eceae2" },
              ticks: {
                font: { size: 11 },
                callback: (v) => "£" + Number(v).toLocaleString("en-GB"),
              },
            },
          },
        },
      });
    }

    const drvBox = el("briefing-drivers");
    drvBox.innerHTML = "";
    const dimTitles = [
      ["category", "By category"],
      ["country", "By country"],
      ["customer_type", "By customer type"],
    ];
    dimTitles.forEach(([dKey, dTitle]) => {
      const info = drv.dimensions?.[dKey];
      if (!info) return;
      const wrap = document.createElement("div");
      wrap.className = "driver-block";
      const h3 = document.createElement("h3");
      h3.textContent = dTitle;
      wrap.appendChild(h3);

      const tbl = document.createElement("table");
      tbl.className = "data-table";
      tbl.innerHTML = `<thead><tr><th>Segment</th><th class="num-col">Current</th><th class="num-col">WoW Δ</th></tr></thead>`;
      const tb = document.createElement("tbody");
      info.members.forEach((m) => {
        const tr = document.createElement("tr");
        const tdName = document.createElement("td");
        tdName.textContent = m.member;
        const tdCur = document.createElement("td");
        tdCur.className = "num-col";
        const bCur = document.createElement("button");
        bCur.type = "button";
        bCur.className = "table-num-btn";
        bCur.textContent = fmtMoney(m.current);
        bCur.addEventListener("click", () =>
          openDrawerWork(info.show_work, `${m.member} current: ${fmtMoney(m.current)}`)
        );
        tdCur.appendChild(bCur);

        const tdChg = document.createElement("td");
        tdChg.className = "num-col";
        const bChg = document.createElement("button");
        bChg.type = "button";
        bChg.className = "table-num-btn";
        bChg.textContent = fmtSignedMoney(m.change);
        bChg.addEventListener("click", () =>
          openDrawerWork(info.show_work, `${m.member} WoW change: ${fmtSignedMoney(m.change)}`)
        );
        tdChg.appendChild(bChg);

        tr.append(tdName, tdCur, tdChg);
        tb.appendChild(tr);
      });
      const trSum = document.createElement("tr");
      trSum.innerHTML = `<td><strong>Total Δ</strong></td><td class="num-col">${fmtMoney(drv.current)}</td><td class="num-col"><strong>${fmtSignedMoney(info.sum_of_changes)}</strong></td>`;
      tb.appendChild(trSum);
      tbl.appendChild(tb);
      wrap.appendChild(tbl);
      drvBox.appendChild(wrap);
    });

    // Anomalies
    const anomBox = el("briefing-anomalies");
    anomBox.innerHTML = "";
    const anoms = doc.anomalies || [];
    if (!anoms.length) {
      anomBox.innerHTML = `<p class="muted">No metrics exceeded 3x scaled MAD over the prior 8 complete weeks.</p>`;
    } else {
      const list = document.createElement("div");
      list.className = "list-items";
      anoms.forEach((a) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "list-item-btn";
        btn.innerHTML = `
          <div class="list-item-title">${escapeHtml(a.metric)}: ${escapeHtml(a.current_fmt)} (${a.mad_score > 0 ? "+" : ""}${a.mad_score} MAD vs ${escapeHtml(a.median_8w_fmt)} median)</div>
          <div class="list-item-detail">${escapeHtml(a.note)}</div>
        `;
        btn.addEventListener("click", () =>
          openDrawerWork(a.show_work, `${a.metric}: ${a.current_fmt}`)
        );
        list.appendChild(btn);
      });
      anomBox.appendChild(list);
    }

    // Watchlist
    const watchBox = el("briefing-watchlist");
    watchBox.innerHTML = "";
    const wList = document.createElement("div");
    wList.className = "list-items";
    (doc.watchlist || []).forEach((w) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "list-item-btn";
      btn.innerHTML = `
        <div class="list-item-title">${escapeHtml(w.title)}</div>
        <div class="list-item-detail">${escapeHtml(w.detail)}</div>
      `;
      btn.addEventListener("click", () => openDrawerWork(w.show_work, w.title));
      wList.appendChild(btn);
    });
    watchBox.appendChild(wList);
  }

  /* --- ASK VIEW --- */
  function renderSuggested(items) {
    const box = el("suggested-list");
    box.innerHTML = "";
    items.forEach((q) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "chip";
      btn.textContent = q;
      btn.addEventListener("click", () => {
        el("ask-input").value = q;
        updateCharCount();
        submitQuestion(q);
      });
      box.appendChild(btn);
    });
  }

  function updateCharCount() {
    const len = el("ask-input").value.length;
    el("char-count").textContent = `${len} / 400`;
  }

  function openWorkPanel(work, selectedLabel) {
    if (!work) return;
    const panel = el("work-panel");
    panel.classList.remove("hidden");
    el("ask-result").classList.remove("single-col");
    el("work-metric").textContent = work.metric;
    el("work-version").textContent = `v${work.definition_version}`;
    el("work-definition").textContent = work.definition;
    el("work-grain").textContent = work.grain;
    el("work-owner").textContent = work.owner;
    el("work-scanned").textContent = fmtInt(work.rows_scanned);
    el("work-asof").textContent = work.as_of;
    el("work-filters").textContent = formatFilters(work);
    el("work-caveats").textContent = work.caveats;
    el("work-sql").textContent = work.sql;

    const selBox = el("work-selected-val");
    if (selectedLabel) {
      selBox.textContent = `Selected figure: ${selectedLabel}`;
      selBox.classList.remove("hidden");
    } else {
      selBox.classList.add("hidden");
    }

    panel.classList.remove("flash");
    void panel.offsetWidth;
    panel.classList.add("flash");
    if (window.innerWidth <= 900) {
      panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }

  function renderAnswerSentence(template, numbers) {
    const p = el("answer-text");
    p.innerHTML = "";
    const parts = String(template || "").split(/(\{\{\d+\}\})/g);
    parts.forEach((chunk) => {
      const m = chunk.match(/^\{\{(\d+)\}\}$/);
      if (m) {
        const idx = Number(m[1]);
        const item = numbers[idx];
        if (item) {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "num-chip";
          btn.textContent = item.text;
          btn.title = "Click to inspect Show work";
          btn.addEventListener("click", () => {
            document.querySelectorAll(".num-chip").forEach((b) => b.classList.remove("selected"));
            btn.classList.add("selected");
            openWorkPanel(item.show_work, `${item.text} (${item.show_work.metric})`);
          });
          p.appendChild(btn);
          return;
        }
      }
      p.appendChild(document.createTextNode(chunk));
    });
  }

  function renderChart(chartSpec) {
    const card = el("chart-card");
    if (activeChart) {
      activeChart.destroy();
      activeChart = null;
    }
    if (!chartSpec || !window.Chart || !chartSpec.labels || !chartSpec.labels.length) {
      card.classList.add("hidden");
      return;
    }
    card.classList.remove("hidden");
    el("chart-title").textContent = chartSpec.title;
    const ctx = el("ask-chart").getContext("2d");
    const isLine = chartSpec.type === "line";
    const colors = chartSpec.values.map((v) => (Number(v) < 0 ? "#b84339" : "#0f5e4c"));
    activeChart = new window.Chart(ctx, {
      type: isLine ? "line" : "bar",
      data: {
        labels: chartSpec.labels,
        datasets: [
          {
            label: chartSpec.title,
            data: chartSpec.values,
            borderColor: "#0f5e4c",
            backgroundColor: isLine ? "rgba(15, 94, 76, 0.10)" : colors,
            borderWidth: isLine ? 2 : 0,
            borderRadius: isLine ? 0 : 4,
            fill: isLine,
            tension: 0.2,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { grid: { display: false }, ticks: { maxRotation: 35, font: { size: 11 } } },
          y: {
            grid: { color: "#eceae2" },
            ticks: {
              font: { size: 11 },
              callback: function (val) {
                if (chartSpec.unit === "gbp") return "£" + Number(val).toLocaleString("en-GB");
                if (chartSpec.unit === "ratio") return (Number(val) * 100).toFixed(0) + "%";
                return Number(val).toLocaleString("en-GB");
              },
            },
          },
        },
      },
    });
  }

  function renderTable(tableSpec, work, rawKind) {
    const card = el("table-card");
    const tbl = el("result-table");
    tbl.innerHTML = "";
    if (!tableSpec || !tableSpec.rows || !tableSpec.rows.length) {
      card.classList.add("hidden");
      return;
    }
    card.classList.remove("hidden");
    const thead = document.createElement("thead");
    const hr = document.createElement("tr");
    tableSpec.columns.forEach((c, idx) => {
      const th = document.createElement("th");
      th.textContent = c;
      const isNum =
        idx >= tableSpec.columns.length - 1 ||
        (rawKind === "why" && idx >= 2) ||
        (tableSpec.columns.length === 2 && idx === 1 && c.startsWith("Prior"));
      if (isNum) th.className = "num-col";
      hr.appendChild(th);
    });
    thead.appendChild(hr);
    tbl.appendChild(thead);

    const tbody = document.createElement("tbody");
    tableSpec.rows.forEach((row, rIdx) => {
      const tr = document.createElement("tr");
      row.forEach((cell, cIdx) => {
        const td = document.createElement("td");
        const looksNumeric = /^[+\-]?£?[\d,]+(\.\d+)?%?$/.test(String(cell));
        if (looksNumeric) {
          td.className = "num-col";
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "table-num-btn";
          btn.textContent = cell;
          btn.addEventListener("click", () => {
            let rowWork = work;
            if (rawKind === "why" && tableSpec.raw_rows && tableSpec.raw_rows[rIdx]) {
              const sqlOverride = tableSpec.raw_rows[rIdx][5];
              if (sqlOverride) rowWork = Object.assign({}, work, { sql: sqlOverride });
            }
            const labelPrefix = cIdx > 0 ? `${row[0]}: ${cell}` : String(cell);
            openWorkPanel(rowWork, labelPrefix);
          });
          td.appendChild(btn);
        } else {
          td.textContent = cell;
        }
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    tbl.appendChild(tbody);
  }

  async function submitQuestion(q) {
    const errBox = el("ask-error");
    const loading = el("ask-loading");
    const empty = el("ask-empty");
    const result = el("ask-result");
    const submitBtn = el("ask-submit");

    errBox.classList.add("hidden");
    empty.classList.add("hidden");
    loading.classList.remove("hidden");
    submitBtn.disabled = true;

    try {
      const res = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ question: q }),
      });
      const payload = await res.json();
      if (!res.ok) {
        throw new Error(payload.detail || `Request failed (${res.status})`);
      }

      if (payload.mode) updateModeUI(payload.mode);
      result.classList.remove("hidden");
      lastWorkItems = payload.numbers || [];
      defaultWork = payload.show_work || null;

      const badge = el("answer-badge");
      if (payload.status === "answered") {
        badge.className = "badge";
        badge.textContent = payload.kind === "why" ? "Verified driver breakdown" : "Verified computation";
      } else if (payload.status === "clarify") {
        badge.className = "badge warn";
        badge.textContent = "Clarification needed";
      } else {
        badge.className = "badge refused";
        badge.textContent = "Refused — out of scope";
      }

      const plannerMap = {
        llm: "Structured by Gemini · Computed by DuckDB",
        rule: "Structured by deterministic parser · Computed by DuckDB",
        guard: "Blocked by deterministic prompt-injection guard",
      };
      el("answer-planner").textContent = plannerMap[payload.planner] || "";

      renderAnswerSentence(payload.answer_template || payload.answer, lastWorkItems);

      if (payload.show_work) {
        el("answer-hint").classList.remove("hidden");
        renderChart(payload.chart);
        renderTable(payload.table, payload.show_work, payload.kind);
        openWorkPanel(payload.show_work, lastWorkItems[0] ? lastWorkItems[0].text : null);
      } else {
        el("answer-hint").classList.add("hidden");
        el("chart-card").classList.add("hidden");
        el("table-card").classList.add("hidden");
        el("work-panel").classList.add("hidden");
        result.classList.add("single-col");
      }
    } catch (err) {
      errBox.textContent = err.message || "Could not complete request.";
      errBox.classList.remove("hidden");
    } finally {
      loading.classList.add("hidden");
      submitBtn.disabled = false;
    }
  }

  /* --- SCORECARD VIEW --- */
  async function loadScorecard() {
    if (scorecardLoaded) return;
    const loading = el("scorecard-loading");
    const errBox = el("scorecard-error");
    const content = el("scorecard-content");
    loading.classList.remove("hidden");
    errBox.classList.add("hidden");

    try {
      const res = await fetch("/api/scorecard", { headers: { Accept: "application/json" } });
      if (!res.ok) throw new Error(`Failed to load scorecard (${res.status})`);
      const data = await res.json();
      renderScorecard(data);
      scorecardLoaded = true;
      content.classList.remove("hidden");
    } catch (err) {
      errBox.textContent = err.message || "Could not load scorecard.";
      errBox.classList.remove("hidden");
    } finally {
      loading.classList.add("hidden");
    }
  }

  function renderScorecard(data) {
    const headlines = el("eval-headlines");
    headlines.innerHTML = "";
    const ev = data.evals;

    if (ev && ev.summary) {
      const testLlm = ev.summary.llm?.test;
      const testBase = ev.summary.baseline?.test;
      const bv = ev.briefing_verifier;
      const cards = [
        {
          label: "Held-out test value accuracy (LLM mode)",
          val: testLlm ? fmtPct(testLlm.answer_value_accuracy.rate) : "n/a",
          sub: testLlm
            ? `${testLlm.answer_value_accuracy.passed}/${testLlm.answer_value_accuracy.total} · 95% CI ${(testLlm.answer_value_accuracy.ci_low * 100).toFixed(1)}–${(testLlm.answer_value_accuracy.ci_high * 100).toFixed(1)}%`
            : "",
        },
        {
          label: "Plain-LLM baseline (held-out test)",
          val: testBase ? fmtPct(testBase.answer_value_accuracy.rate) : "n/a",
          sub: testBase
            ? `${testBase.answer_value_accuracy.passed}/${testBase.answer_value_accuracy.total} · Schema + 50 sample rows`
            : "",
        },
        {
          label: "Refusal & injection accuracy (test)",
          val: testLlm ? fmtPct(testLlm.refusal_accuracy.rate) : "n/a",
          sub: testLlm
            ? `${testLlm.refusal_accuracy.passed}/${testLlm.refusal_accuracy.total} boundary cases handled`
            : "",
        },
        {
          label: "Briefing verifier failure rate (1st pass)",
          val: bv ? fmtPct(bv.first_pass_failure_rate) : "0.0%",
          sub: bv
            ? `${bv.first_pass_failures}/${bv.weeks_evaluated} failed 1st pass (${bv.template_fallbacks} fallbacks, 100% final verified)`
            : "",
        },
      ];
      cards.forEach((c) => {
        const div = document.createElement("div");
        div.className = "kpi-card";
        div.innerHTML = `<div class="kpi-label">${escapeHtml(c.label)}</div><div class="kpi-val">${escapeHtml(c.val)}</div><div class="kpi-sub">${escapeHtml(c.sub)}</div>`;
        headlines.appendChild(div);
      });

      const evalTbl = el("eval-table");
      const rowsHtml = [];
      const modes = [
        ["llm", "Showwork (LLM + compiler)"],
        ["no_key", "Showwork (No-key rule parser)"],
        ["baseline", "Plain LLM (schema + 50 rows)"],
      ];
      modes.forEach(([mKey, mLabel]) => {
        ["dev", "test"].forEach((split) => {
          const s = ev.summary[mKey]?.[split];
          if (!s) return;
          rowsHtml.push(`
            <tr>
              <td><strong>${escapeHtml(mLabel)}</strong></td>
              <td>${escapeHtml(split.toUpperCase())}</td>
              <td class="num-col">${s.request_match_accuracy ? escapeHtml(fmtWilson(s.request_match_accuracy)) : "n/a (direct text)"}</td>
              <td class="num-col">${escapeHtml(fmtWilson(s.answer_value_accuracy))}</td>
              <td class="num-col">${escapeHtml(fmtWilson(s.refusal_accuracy))}</td>
            </tr>
          `);
        });
      });
      evalTbl.innerHTML = `
        <thead>
          <tr>
            <th>System / Mode</th>
            <th>Split</th>
            <th class="num-col">Request-match accuracy</th>
            <th class="num-col">Answer-value accuracy</th>
            <th class="num-col">Refusal / clarify accuracy</th>
          </tr>
        </thead>
        <tbody>${rowsHtml.join("")}</tbody>
      `;

      const failBox = el("failures-body");
      const fails = ev.failures || [];
      if (!fails.length) {
        failBox.innerHTML = `<p class="muted">No failures recorded in Showwork modes.</p>`;
      } else {
        const fRows = fails
          .map(
            (f) => `
          <tr>
            <td>${escapeHtml(f.system)}</td>
            <td>${escapeHtml(f.split)}</td>
            <td>${escapeHtml(f.id)}: ${escapeHtml(f.question)}</td>
            <td>${escapeHtml(f.detail)}</td>
          </tr>`
          )
          .join("");
        failBox.innerHTML = `
          <div class="table-scroll">
            <table class="data-table">
              <thead><tr><th>System</th><th>Split</th><th>Question</th><th>What happened</th></tr></thead>
              <tbody>${fRows}</tbody>
            </table>
          </div>`;
      }

      const injTbl = el("injection-table");
      const injRows = (ev.injections || [])
        .map(
          (item) => `
        <tr>
          <td><span class="code-inline">${escapeHtml(item.id)}</span></td>
          <td>${escapeHtml(item.question)}</td>
          <td>${escapeHtml(item.outcome_llm)}</td>
          <td><span class="badge">${item.passed ? "Blocked" : "Failed"}</span></td>
        </tr>`
        )
        .join("");
      injTbl.innerHTML = `
        <thead><tr><th>ID</th><th>Adversarial prompt</th><th>Showwork response</th><th>Status</th></tr></thead>
        <tbody>${injRows}</tbody>
      `;
    }

    const q = data.quality || {};
    el("quality-summary-line").textContent =
      `Input rows: ${fmtInt(q.input_rows)} · Clean rows: ${fmtInt(q.clean_rows)} (${fmtInt(q.sale_rows)} sales, ${fmtInt(q.return_rows)} returns) · Quarantined: ${fmtInt(q.quarantined_rows)} · Guest share: ${fmtPct(q.flags?.guest_share_of_rows)} · Canonicalised descriptions: ${fmtInt(q.descriptions?.rows_relabelled_to_canonical_description)} rows across ${fmtInt(q.descriptions?.stock_codes_with_several_descriptions)} multi-description stock codes.`;

    const qRows = (q.reasons || [])
      .filter((r) => r.count > 0)
      .map(
        (r) => `
      <tr>
        <td><span class="code-inline">${escapeHtml(r.code)}</span></td>
        <td class="num-col">${fmtInt(r.count)}</td>
        <td class="num-col">${(r.share_of_input * 100).toFixed(2)}%</td>
        <td>${escapeHtml(r.description)}</td>
      </tr>`
      )
      .join("");
    el("quarantine-table").innerHTML = `
      <thead><tr><th>Reason code</th><th class="num-col">Rows</th><th class="num-col">% of raw</th><th>Rule</th></tr></thead>
      <tbody>${qRows}</tbody>
    `;

    const c = data.categories || {};
    const cc = c.llm_consistency_check;
    el("category-summary-line").textContent =
      `${fmtInt(c.products)} distinct product stock codes normalised into 18 categories (${fmtInt(c.review_queue)} low-confidence items queued in review_queue.csv). ` +
      (cc
        ? `Consistency check (LLM vs ${cc.sampled} hidden rule labels — not ground truth): ${(cc.agreement * 100).toFixed(1)}% exact match.`
        : "");
    const cRows = (c.stages || [])
      .map(
        (s) => `
      <tr>
        <td>${escapeHtml(s.stage)}</td>
        <td class="num-col">${fmtInt(s.products)}</td>
        <td class="num-col">${(s.share * 100).toFixed(2)}%</td>
        <td class="num-col">${(s.cumulative * 100).toFixed(2)}%</td>
      </tr>`
      )
      .join("");
    el("category-table").innerHTML = `
      <thead><tr><th>Stage</th><th class="num-col">Products</th><th class="num-col">Stage share</th><th class="num-col">Cumulative</th></tr></thead>
      <tbody>${cRows}</tbody>
    `;

    const mRows = (data.metrics || [])
      .map(
        (m) => `
      <tr>
        <td><strong>${escapeHtml(m.label)}</strong><br><span class="code-inline">${escapeHtml(m.key)}</span> (v${escapeHtml(m.definition_version)})</td>
        <td>${escapeHtml(m.definition)}</td>
        <td><span class="code-inline">${escapeHtml(m.sql)}</span></td>
        <td>${escapeHtml(m.grain)} · ${escapeHtml(m.owner)}</td>
        <td class="muted">${escapeHtml(m.caveats)}</td>
      </tr>`
      )
      .join("");
    el("metrics-table").innerHTML = `
      <thead><tr><th>Metric</th><th>Definition</th><th>SQL expression</th><th>Grain &amp; Owner</th><th>Caveats</th></tr></thead>
      <tbody>${mRows}</tbody>
    `;
  }

  function showView(viewName, push) {
    const valid = ["briefing", "ask", "scorecard"];
    const target = valid.includes(viewName) ? viewName : "briefing";
    valid.forEach((v) => {
      el("view-" + v).classList.toggle("hidden", v !== target);
    });
    document.querySelectorAll(".nav-link").forEach((a) => {
      a.classList.toggle("active", a.dataset.nav === target);
    });
    if (push) {
      window.history.pushState({ view: target }, "", "/" + target);
    }
    if (target === "briefing") bootBriefing();
    if (target === "scorecard") loadScorecard();
  }

  function routeFromLocation() {
    const p = window.location.pathname.replace(/\/+$/, "");
    if (p === "/ask") return "ask";
    if (p === "/scorecard") return "scorecard";
    return "briefing";
  }

  document.addEventListener("DOMContentLoaded", () => {
    const input = el("ask-input");
    input.addEventListener("input", updateCharCount);

    el("ask-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const q = input.value.trim();
      if (q) submitQuestion(q);
    });

    el("briefing-week").addEventListener("change", (e) => {
      if (e.target.value) loadBriefingWeek(e.target.value);
    });

    el("drawer-close").addEventListener("click", closeDrawerWork);
    el("drawer-backdrop").addEventListener("click", closeDrawerWork);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeDrawerWork();
    });

    el("mode-pill").addEventListener("click", () => {
      el("mode-banner").classList.toggle("hidden");
    });

    el("inspect-query-btn").addEventListener("click", () => {
      if (defaultWork) openWorkPanel(defaultWork, null);
    });

    document.querySelectorAll("[data-nav]").forEach((a) => {
      a.addEventListener("click", (e) => {
        e.preventDefault();
        showView(a.dataset.nav, true);
      });
    });

    window.addEventListener("popstate", () => {
      showView(routeFromLocation(), false);
    });

    showView(routeFromLocation(), false);
    bootStatus();
  });
})();
