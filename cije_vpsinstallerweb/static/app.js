const Cije = {
  async api(path, { method = "GET", body } = {}) {
    const opts = {
      method,
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    let data = {};
    try {
      data = await res.json();
    } catch {
      data = {};
    }
    return { ok: res.ok, status: res.status, body: data };
  },

  flash(message) {
    document.querySelector(".toast")?.remove();
    const el = document.createElement("div");
    el.className = "toast";
    el.textContent = message;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 5000);
  },

  streamJob(jobId, logEl, statusEl) {
    if (!jobId || !logEl) return;
    const url = "/api/jobs/" + encodeURIComponent(jobId) + "/stream";
    const source = new EventSource(url);
    const append = (line) => {
      if (logEl.textContent === "Waiting for a job…") logEl.textContent = "";
      logEl.textContent += (logEl.textContent && !logEl.textContent.endsWith("\n") ? "\n" : "") + line;
      logEl.scrollTop = logEl.scrollHeight;
    };
    source.onmessage = (ev) => {
      let data;
      try {
        data = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (data.line !== undefined) append(data.line);
      if (data.status && statusEl) statusEl.textContent = data.status;
      if (data.done) source.close();
    };
    source.onerror = () => {
      source.close();
      Cije.pollJob(jobId, logEl, statusEl);
    };
  },

  async pollJob(jobId, logEl, statusEl) {
    let seen = logEl.textContent.split("\n").filter(Boolean).length;
    const tick = async () => {
      const res = await Cije.api("/api/jobs/" + encodeURIComponent(jobId));
      if (!res.ok) return;
      const job = res.body;
      if (statusEl && job.status) statusEl.textContent = job.status;
      const lines = job.lines || [];
      if (lines.length > seen) {
        logEl.textContent = lines.join("\n");
        logEl.scrollTop = logEl.scrollHeight;
        seen = lines.length;
      }
      if (job.status === "queued" || job.status === "running") {
        setTimeout(tick, 400);
      }
    };
    tick();
  },
};

window.Cije = Cije;
