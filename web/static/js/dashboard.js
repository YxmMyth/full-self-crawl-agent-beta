/* Helmsman live dashboard controller.
 *
 * One Alpine component, one native EventSource onto /api/stream. Each event
 * type updates a slice of reactive state; Alpine re-renders the panels. Heavy
 * markdown (models, strategy report) is pulled on demand via REST, never
 * streamed. Connection status lives in an Alpine store so the topbar pill can
 * read it from outside the component.
 */

document.addEventListener("alpine:init", () => {
  Alpine.store("d", { connected: false });

  Alpine.data("dashboard", () => ({
    // identity
    domain: "", runId: null, mode: "auto",
    // live status
    phase: "idle", step: null, sessionId: null, currentUrl: null, heartbeatAge: null,
    counts: { locations: 0, observations: 0, sessions: 0, samples: 0, catalog: 0, data_files: 0, data_bytes: 0 },
    // flags
    gatePending: false, assistPending: false, assist: {}, done: null,
    askPending: false, ask: {}, askMessage: "",
    // collections
    locations: [], observations: [], sessions: [], logs: [],
    artifacts: {}, audit: null,
    // panels
    modelTab: null, modelHtml: "", strategyHtml: "", strategyLoaded: false,
    steps: [],
    // VNC mini-screen
    vncConnected: false, vncExpanded: false, vncUrl: "",
    // Full noVNC client (vnc.html), not vnc_lite: it honors resize=scale
    // (local downscale so the whole 1280x900 desktop fits the mini-screen) and
    // shows connection status instead of a silent black canvas. autoconnect
    // skips the Connect dialog; path points at our same-origin WS proxy.
    _es: null,
    _vncBase() {
      const r = encodeURIComponent(this.runId || "");
      return `/vnc/${r}/vnc.html?autoconnect=true&resize=scale&reconnect=true&path=vnc/${r}/websockify&view_only=false`;
    },

    boot(state) {
      state = state || {};
      this.domain = state.domain || "";
      this.runId = state.run_id || null;
      this.mode = state.mode || "auto";
      this.phase = state.phase || "idle";
      this.gatePending = !!state.gate_pending;
      this.assistPending = !!state.assist_pending;
      this.askPending = !!state.ask_pending;
      this.steps = this._stepsFor(this.mode);
      // ESC exits VNC fullscreen.
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && this.vncExpanded) this.vncExpanded = false;
      });
      this.connect();
    },

    _stepsFor(mode) {
      if (mode === "explore")
        return [{ key: "recon", label: "RECON" }, { key: "done", label: "DONE" }];
      if (mode === "harvest")
        return [{ key: "harvest", label: "HARVEST" }, { key: "done", label: "DONE" }];
      return [
        { key: "recon", label: "RECON" },
        { key: "gate", label: "GATE" },
        { key: "harvest", label: "HARVEST" },
        { key: "done", label: "DONE" },
      ];
    },

    connect() {
      const es = new EventSource(
        this.runId ? "/api/stream/" + encodeURIComponent(this.runId) : "/api/stream"
      );
      this._es = es;
      es.onopen = () => (Alpine.store("d").connected = true);
      es.onerror = () => (Alpine.store("d").connected = false);
      const on = (name, fn) =>
        es.addEventListener(name, (e) => {
          let d = {};
          try { d = JSON.parse(e.data); } catch (_) {}
          fn.call(this, d);
        });

      on("run_started", (d) => {
        this.runId = d.run_id;
        this.domain = d.domain || this.domain;
        if (d.mode) { this.mode = d.mode; this.steps = this._stepsFor(d.mode); }
      });
      on("status", (d) => {
        this.phase = d.phase || this.phase;
        this.sessionId = d.session_id || this.sessionId;
        if (d.step != null) this.step = d.step;
        if (d.current_url) this.currentUrl = d.current_url;
        if (d.counts) this.counts = d.counts;
        this.heartbeatAge = (d.heartbeat_age == null ? null : d.heartbeat_age);
        this.gatePending = !!d.gate_pending;
        this.assistPending = !!d.assist_pending;
        this.askPending = !!d.ask_pending;
      });
      on("location", (d) => {
        if (!this.locations.some((l) => l.pattern === d.pattern))
          this.locations.push({ pattern: d.pattern, how_to_reach: d.how_to_reach });
      });
      on("observation", (d) => {
        this.observations.unshift(d);
        if (this.observations.length > 200) this.observations.pop();
      });
      on("session", (d) => {
        const i = this.sessions.findIndex((s) => s.session_id === d.session_id);
        if (i >= 0) this.sessions[i] = d; else this.sessions.push(d);
      });
      on("artifact", (d) => {
        if (!this.artifacts[d.dir]) this.artifacts[d.dir] = [];
        const arr = this.artifacts[d.dir];
        const i = arr.findIndex((a) => a.filename === d.filename);
        if (i >= 0) arr[i] = d; else arr.push(d);
      });
      on("gate_pending", () => { this.gatePending = true; this.phase = "gate"; this.loadStrategy(); });
      on("assist_pending", (d) => {
        this.assistPending = !!d.pending;
        this.assist = d || {};
        // Surface the live browser so the operator can act on the gate.
        if (d.pending) { if (!this.vncConnected) this.toggleVnc(); this.vncExpanded = true; }
      });
      on("ask_pending", (d) => {
        // Text question (ask_human / intake / gate calibration). Unlike assist,
        // it's not a browser op, so don't auto-open the VNC — just show the box.
        this.askPending = !!d.pending;
        this.ask = d || {};
        if (d.pending) this.askMessage = "";
      });
      on("audit", (d) => { this.audit = d; });
      on("log", (d) => {
        this.logs.push(d);
        if (this.logs.length > 400) this.logs.shift();
        this.$nextTick(() => { const p = this.$refs.logpanel; if (p) p.scrollTop = p.scrollHeight; });
      });
      on("done", (d) => {
        this.done = d.outcome;
        this.phase = d.outcome === "completed" ? "done" : d.outcome;
        Alpine.store("d").connected = false;
      });
    },

    // ── phase stepper ──
    stepClass(key) {
      const order = this.steps.map((s) => s.key);
      const cur = this._phaseToStep();
      const ci = order.indexOf(cur);
      const ki = order.indexOf(key);
      if (this.done) return key === "done" ? "active" : "done";
      if (ki < ci) return "done";
      if (ki === ci) return "active";
      return "";
    },
    _phaseToStep() {
      const p = this.phase;
      if (p === "launching" || p === "recon" || p === "auto") return "recon";
      if (p === "gate") return "gate";
      if (p === "harvest") return "harvest";
      if (["done", "completed", "error", "killed", "blocked"].includes(p)) return "done";
      return "recon";
    },

    // ── actions ──
    _ctl(action) {
      // Per-run endpoint when we know our run_id; else the newest-run alias.
      return this.runId
        ? "/api/runs/" + encodeURIComponent(this.runId) + "/" + action
        : "/api/runs/active/" + action;
    },
    async gate(decision) {
      const fd = new FormData();
      fd.append("decision", decision);
      fd.append("domain", this.domain || ""); // durable: answer needs no live handle
      await fetch(this._ctl("gate"), { method: "POST", body: fd });
      this.gatePending = false;
    },
    async answerAssist(status) {
      const fd = new FormData();
      fd.append("uuid", this.assist.uuid || "");
      fd.append("status", status);
      fd.append("domain", this.domain || ""); // durable: answer needs no live handle
      await fetch(this._ctl("assist"), { method: "POST", body: fd });
      this.assistPending = false; // optimistic; status.json reconciles
    },
    async answerAsk(status) {
      const fd = new FormData();
      fd.append("uuid", this.ask.uuid || "");
      fd.append("message", this.askMessage || "");
      fd.append("status", status);
      fd.append("domain", this.domain || ""); // durable: answer needs no live handle
      await fetch(this._ctl("ask"), { method: "POST", body: fd });
      this.askPending = false; // optimistic; status.json reconciles
      this.askMessage = "";
    },
    async stop() {
      if (!confirm("确定停止当前任务?")) return;
      await fetch(this._ctl("stop"), { method: "POST" });
    },
    async loadModel(type) {
      this.modelTab = type;
      if (!this.runId) { this.modelHtml = "<div class='empty'>run_id 未就绪</div>"; return; }
      this.modelHtml = "<div class='empty'>加载中…</div>";
      try {
        const r = await fetch(
          `/api/runs/model?domain=${encodeURIComponent(this.domain)}&run_id=${encodeURIComponent(this.runId)}&type=${type}`
        );
        const j = await r.json();
        this.modelHtml = j.markdown ? marked.parse(j.markdown) : "<div class='empty'>(空)</div>";
      } catch (e) {
        this.modelHtml = "<div class='empty'>加载失败</div>";
      }
    },
    async loadStrategy() {
      if (this.strategyLoaded || !this.runId) return;
      try {
        const r = await fetch(
          `/api/runs/file?domain=${encodeURIComponent(this.domain)}&run_id=${encodeURIComponent(this.runId)}&path=strategy_report.md`
        );
        if (r.ok) {
          this.strategyHtml = marked.parse(await r.text());
          this.strategyLoaded = true;
        }
      } catch (e) {}
    },

    // ── VNC mini-screen ──
    toggleVnc() {
      if (this.vncConnected) {
        this.vncConnected = false;
        this.vncUrl = "";          // unload iframe → VNC live only while viewing
      } else {
        this.vncUrl = this._vncBase();
        this.vncConnected = true;
      }
    },
    toggleExpand() {
      if (!this.vncConnected) this.toggleVnc();
      this.vncExpanded = !this.vncExpanded;
    },

    // ── format ──
    hbText() {
      const a = this.heartbeatAge;
      if (a == null) return "";
      if (a < 2) return "活跃";
      if (a < 60) return Math.round(a) + "s 前";
      return Math.round(a / 60) + "m 前";
    },
    fmtBytes(n) {
      n = n || 0;
      if (n < 1024) return n + " B";
      if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
      return (n / 1048576).toFixed(2) + " MB";
    },
  }));
});
