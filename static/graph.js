/* 知识图谱：自包含力导向布局，不依赖任何 CDN。
   规模：~100 创新点 + ~70 机制 + ~30 论文 = 200 节点，
   直接在前端算布局，后端只给数据。 */

(() => {
  const SVG_NS = "http://www.w3.org/2000/svg";
  const state = {
    nodes: [],
    links: [],
    alpha: 1,
    running: false,
    scale: 1,
    tx: 0,
    ty: 0,
    highlight: null,
    width: 0,
    height: 0,
  };

  const el = {
    svg: document.getElementById("graph-svg"),
    viewport: document.getElementById("graph-viewport"),
    gLinks: document.getElementById("graph-links"),
    gNodes: document.getElementById("graph-nodes"),
    stats: document.getElementById("graph-stats"),
    tooltip: document.getElementById("graph-tip"),
    search: document.getElementById("graph-search"),
    minRefs: document.getElementById("graph-min-refs"),
    withPapers: document.getElementById("graph-with-papers"),
    relayout: document.getElementById("graph-relayout"),
  };

  function sizeOf() {
    const rect = el.svg.getBoundingClientRect();
    state.width = rect.width || 900;
    state.height = rect.height || 600;
    el.svg.setAttribute("viewBox", `0 0 ${state.width} ${state.height}`);
  }

  async function load() {
    const params = new URLSearchParams({
      min_refs: el.minRefs.value,
      include_papers: el.withPapers.checked ? "1" : "0",
    });
    const response = await fetch(`/graph/data?${params}`);
    const data = await response.json();
    build(data);
  }

  function build(data) {
    sizeOf();
    state.nodes = data.nodes.map((node, index) => {
      const angle = (index / Math.max(1, data.nodes.length)) * Math.PI * 2;
      const radius = node.type === "mechanism" ? 140 : node.type === "paper" ? 260 : 200;
      return {
        ...node,
        x: state.width / 2 + Math.cos(angle) * radius * (0.6 + Math.random() * 0.6),
        y: state.height / 2 + Math.sin(angle) * radius * (0.6 + Math.random() * 0.6),
        vx: 0,
        vy: 0,
      };
    });
    const index = new Map(state.nodes.map((node) => [node.id, node]));
    state.links = data.links
      .map((link) => ({
        source: index.get(link.source),
        target: index.get(link.target),
        kind: link.kind,
      }))
      .filter((link) => link.source && link.target);

    el.stats.textContent =
      `${data.stats.mechanisms} 个机制 · ${data.stats.insights} 条创新点 · ${data.stats.papers} 篇论文`;

    render();
    state.alpha = 1;
    start();
  }

  function render() {
    el.gLinks.innerHTML = "";
    el.gNodes.innerHTML = "";
    state.nodes.forEach((node) => {
      node.dom = null;
      node.labelDom = null;
    });

    state.links.forEach((link) => {
      const line = document.createElementNS(SVG_NS, "line");
      line.setAttribute("class", `graph-link ${link.kind}`);
      el.gLinks.appendChild(line);
      link.dom = line;
    });

    state.nodes.forEach((node) => {
      const group = document.createElementNS(SVG_NS, "g");
      group.setAttribute("class", `graph-node ${node.type}`);

      const circle = document.createElementNS(SVG_NS, "circle");
      circle.setAttribute("r", node.size);
      group.appendChild(circle);

      const text = document.createElementNS(SVG_NS, "text");
      text.setAttribute("dy", node.size + 11);
      text.textContent = trim(node.label, node.type === "insight" ? 12 : 16);
      // 只常显「被多篇引用」的机制名与论文名，其余悬停才显示，避免 68 个标签糊成一团
      const minor =
        node.type === "insight" || (node.type === "mechanism" && (node.refs || 0) < 2);
      if (minor) text.setAttribute("class", "graph-label-minor");
      group.appendChild(text);

      group.addEventListener("mouseenter", () => focus(node));
      group.addEventListener("mouseleave", () => focus(null));
      group.addEventListener("click", (event) => {
        if (Math.abs(node.dragMoved || 0) < 4 && node.url) window.location.href = node.url;
        event.stopPropagation();
      });
      enableDrag(node, group);

      el.gNodes.appendChild(group);
      node.dom = group;
      node.labelDom = text;
    });
    draw();
  }

  function trim(text, max) {
    const value = String(text || "");
    return value.length > max ? `${value.slice(0, max)}…` : value;
  }

  function neighbors(node) {
    const set = new Set();
    if (!node) return set;
    set.add(node.id);
    state.links.forEach((link) => {
      if (link.source.id === node.id) set.add(link.target.id);
      if (link.target.id === node.id) set.add(link.source.id);
    });
    return set;
  }

  function focus(node) {
    state.highlight = node ? neighbors(node) : null;
    if (node) {
      el.tooltip.textContent = node.type === "insight"
        ? node.label
        : `${node.label}${node.refs ? `（${node.refs} 条创新点）` : ""}${node.count ? `（${node.count} 条）` : ""}`;
      el.tooltip.classList.add("on");
    } else {
      el.tooltip.classList.remove("on");
    }
    draw();
  }

  function draw() {
    state.links.forEach((link) => {
      const dim = state.highlight && !(state.highlight.has(link.source.id) && state.highlight.has(link.target.id));
      link.dom.setAttribute("x1", link.source.x);
      link.dom.setAttribute("y1", link.source.y);
      link.dom.setAttribute("x2", link.target.x);
      link.dom.setAttribute("y2", link.target.y);
      link.dom.setAttribute("opacity", dim ? 0.05 : link.kind === "tag" ? 0.35 : 0.18);
    });
    state.nodes.forEach((node) => {
      const dim = state.highlight && !state.highlight.has(node.id);
      node.dom.setAttribute("transform", `translate(${node.x},${node.y})`);
      node.dom.setAttribute("opacity", dim ? 0.15 : 1);
    });
  }

  function simulate() {
    const nodes = state.nodes;
    const alpha = state.alpha;

    // 斥力（O(n²)，200 节点足够）
    for (let i = 0; i < nodes.length; i += 1) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j += 1) {
        const b = nodes[j];
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let dist2 = dx * dx + dy * dy;
        if (dist2 < 1) {
          dx = (Math.random() - 0.5) * 2;
          dy = (Math.random() - 0.5) * 2;
          dist2 = 1;
        }
        const dist = Math.sqrt(dist2);
        const force = (900 * alpha) / dist2;
        const fx = (dx / dist) * force;
        const fy = (dy / dist) * force;
        a.vx += fx;
        a.vy += fy;
        b.vx -= fx;
        b.vy -= fy;
      }
    }

    // 弹簧
    state.links.forEach((link) => {
      const dx = link.target.x - link.source.x;
      const dy = link.target.y - link.source.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 1;
      const rest = link.kind === "tag" ? 70 : 55;
      const k = (link.kind === "tag" ? 0.035 : 0.05) * alpha;
      const force = (dist - rest) * k;
      const fx = (dx / dist) * force;
      const fy = (dy / dist) * force;
      link.source.vx += fx;
      link.source.vy += fy;
      link.target.vx -= fx;
      link.target.vy -= fy;
    });

    // 向心 + 阻尼
    const cx = state.width / 2;
    const cy = state.height / 2;
    nodes.forEach((node) => {
      if (node.fixed) {
        node.vx = 0;
        node.vy = 0;
        return;
      }
      node.vx += (cx - node.x) * 0.0025 * alpha;
      node.vy += (cy - node.y) * 0.0025 * alpha;
      node.vx *= 0.82;
      node.vy *= 0.82;
      node.x += node.vx;
      node.y += node.vy;
      const pad = 20;
      node.x = Math.max(pad, Math.min(state.width - pad, node.x));
      node.y = Math.max(pad, Math.min(state.height - pad, node.y));
    });
  }

  function start() {
    if (state.running) return;
    state.running = true;
    const step = () => {
      simulate();
      draw();
      state.alpha *= 0.985;
      if (state.alpha < 0.03) {
        state.running = false;
        return;
      }
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  function enableDrag(node, group) {
    let dragged = false;
    group.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      dragged = true;
      node.dragMoved = 0;
      node.fixed = true;
      group.setPointerCapture(event.pointerId);
      group.classList.add("dragging");
    });
    group.addEventListener("pointermove", (event) => {
      if (!node.fixed) return;
      const point = toLocal(event);
      node.dragMoved = (node.dragMoved || 0) + Math.abs(point.x - node.x) + Math.abs(point.y - node.y);
      node.x = point.x;
      node.y = point.y;
      draw();
      if (!state.running) start();
    });
    group.addEventListener("pointerup", () => {
      node.fixed = false;
      group.classList.remove("dragging");
      setTimeout(() => {
        node.dragMoved = 0;
      }, 200);
      if (dragged) state.alpha = Math.max(state.alpha, 0.25);
    });
  }

  function toLocal(event) {
    const rect = el.svg.getBoundingClientRect();
    return {
      x: (event.clientX - rect.left - state.tx) / state.scale,
      y: (event.clientY - rect.top - state.ty) / state.scale,
    };
  }

  function applyTransform() {
    el.viewport.setAttribute(
      "transform",
      `translate(${state.tx},${state.ty}) scale(${state.scale})`,
    );
  }

  el.svg.addEventListener("wheel", (event) => {
    event.preventDefault();
    const rect = el.svg.getBoundingClientRect();
    const mx = event.clientX - rect.left;
    const my = event.clientY - rect.top;
    const delta = event.deltaY > 0 ? 0.9 : 1.1;
    const next = Math.max(0.3, Math.min(3, state.scale * delta));
    const ratio = next / state.scale;
    state.tx = mx - (mx - state.tx) * ratio;
    state.ty = my - (my - state.ty) * ratio;
    state.scale = next;
    applyTransform();
  }, { passive: false });

  let panning = null;
  el.svg.addEventListener("pointerdown", (event) => {
    if (event.target.closest(".graph-node")) return;
    panning = { x: event.clientX - state.tx, y: event.clientY - state.ty };
  });
  el.svg.addEventListener("pointermove", (event) => {
    if (!panning) return;
    state.tx = event.clientX - panning.x;
    state.ty = event.clientY - panning.y;
    applyTransform();
  });
  el.svg.addEventListener("pointerup", () => {
    panning = null;
  });

  el.search.addEventListener("input", () => {
    const term = el.search.value.trim();
    if (!term) {
      state.highlight = null;
      draw();
      return;
    }
    state.highlight = new Set(
      state.nodes.filter((node) => node.label.includes(term)).map((node) => node.id),
    );
    draw();
  });

  el.minRefs.addEventListener("change", load);
  el.withPapers.addEventListener("change", load);
  el.relayout.addEventListener("click", () => {
    state.scale = 1;
    state.tx = 0;
    state.ty = 0;
    applyTransform();
    load();
  });
  window.addEventListener("resize", () => {
    sizeOf();
    state.alpha = Math.max(state.alpha, 0.3);
    if (!state.running) start();
  });

  load();
})();
