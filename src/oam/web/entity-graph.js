/* Interactive rendering of source-supported entity co-occurrences. */
window.drawEntityGraph = (container, data, { element, button, msg, setAttribute, setText, onSelect, onError }) => {
  const namespace = "http://www.w3.org/2000/svg";
  const svgNode = (tag, attrs = {}) => {
    const node = document.createElementNS(namespace, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    return node;
  };
  const nodes = data.nodes.map((item, index) => {
    const angle = index * 2.3999632297;
    const radius = 34 * Math.sqrt(index);
    return { ...item, x: 600 + Math.cos(angle) * radius, y: 330 + Math.sin(angle) * radius, weight: 0 };
  });
  const byId = new Map(nodes.map(node => [node.entity_id, node]));
  const edges = data.edges.filter(edge => byId.has(edge.source) && byId.has(edge.target));
  const neighbors = new Map(nodes.map(node => [node.entity_id, new Set([node.entity_id])]));
  for (const edge of edges) {
    byId.get(edge.source).weight += edge.weight;
    byId.get(edge.target).weight += edge.weight;
    neighbors.get(edge.source).add(edge.target);
    neighbors.get(edge.target).add(edge.source);
  }
  const maxWeight = Math.max(1, ...nodes.map(node => node.weight));
  const times = nodes.map(node => Date.parse(node.last_mentioned_at)).filter(Number.isFinite);
  const oldest = times.length ? Math.min(...times) : 0;
  const newest = times.length ? Math.max(...times) : 0;
  const legend = element("div", undefined, "entity-graph-legend");
  legend.append(element("p", msg("Scroll to zoom · drag to pan · hover to explore · click to select")), element("strong", msg("Recency · last seen")), element("div", undefined, "recency-ramp"));
  if (times.length) legend.append(element("small", `${new Date(oldest).toISOString().slice(0, 10)} — ${new Date(newest).toISOString().slice(0, 10)}`));
  legend.append(element("strong", msg("Co-occurrences")), element("span", "· ● ⬤", "size-legend"));
  const tools = element("div", undefined, "entity-graph-tools");
  const svg = svgNode("svg", { viewBox: "0 0 1200 660", "aria-label": "", role: "group" });
  setAttribute(svg, "aria-label", msg("Entity graph"));
  const links = svgNode("g", { class: "entity-graph-links" });
  const dots = svgNode("g");
  svg.append(links, dots);
  const edgeElements = edges.map(edge => {
    const line = svgNode("line", { "data-weight": edge.weight, "stroke-width": Math.min(2.5, 0.4 + Math.log1p(edge.weight) * 0.4) });
    links.append(line);
    return { edge, line };
  });
  let dragged = false;
  for (const node of nodes) {
    const radius = 4 + Math.sqrt(node.weight / maxWeight) * 11;
    const heat = newest === oldest ? 1 : (Date.parse(node.last_mentioned_at) - oldest) / (newest - oldest);
    const hue = Number.isFinite(heat) ? 215 - heat * 201 : 215;
    const group = svgNode("g", { class: "entity-graph-node", role: "button", tabindex: "0", "aria-label": node.name, "data-entity-id": node.entity_id });
    group.append(svgNode("circle", { r: radius + 6, fill: `hsl(${hue} 80% 70% / 0.14)` }), svgNode("circle", { r: radius, fill: `hsl(${hue} 78% 68%)` }));
    const text = svgNode("text", { x: radius + 9, y: 4 });
    text.textContent = node.name;
    const title = svgNode("title");
    title.textContent = `${node.name} · ${node.memory_count}`;
    group.append(text, title);
    const highlight = active => {
      for (const other of nodes) other.group.classList.toggle("dimmed", active && !neighbors.get(node.entity_id).has(other.entity_id));
      for (const { edge, line } of edgeElements) line.classList.toggle("highlighted", active && [edge.source, edge.target].includes(node.entity_id));
    };
    group.addEventListener("mouseenter", () => highlight(true));
    group.addEventListener("mouseleave", () => highlight(false));
    group.addEventListener("focus", () => highlight(true));
    group.addEventListener("blur", () => highlight(false));
    group.addEventListener("click", () => { if (!dragged) onSelect(node); });
    group.addEventListener("keydown", event => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); onSelect(node); } });
    node.group = group;
    dots.append(group);
  }
  const paint = () => {
    for (const node of nodes) node.group.setAttribute("transform", `translate(${node.x},${node.y})`);
    for (const { edge, line } of edgeElements) {
      const a = byId.get(edge.source), b = byId.get(edge.target);
      for (const [key, value] of Object.entries({ x1: a.x, y1: a.y, x2: b.x, y2: b.y })) line.setAttribute(key, value);
    }
  };
  // Settle the bounded graph once; animation below adds only subtle visual drift.
  for (let tick = 0; tick < 65; tick++) {
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j], dx = a.x - b.x, dy = a.y - b.y;
        const squared = Math.max(64, dx * dx + dy * dy);
        const force = Math.min(2, 160 / squared);
        a.x += dx * force; a.y += dy * force;
        b.x -= dx * force; b.y -= dy * force;
      }
      a.x += (600 - a.x) * 0.012; a.y += (330 - a.y) * 0.012;
    }
    for (const edge of edges) {
      const a = byId.get(edge.source), b = byId.get(edge.target);
      const dx = b.x - a.x, dy = b.y - a.y, length = Math.hypot(dx, dy) || 1;
      const force = Math.max(-0.6, Math.min(0.6, (length - 90) * 0.003));
      a.x += dx / length * force; a.y += dy / length * force;
      b.x -= dx / length * force; b.y -= dy / length * force;
    }
  }
  for (const node of nodes) { node.baseX = node.x; node.baseY = node.y; }
  let bounds = { x: 0, y: 0, width: 1200, height: 660 };
  if (nodes.length) {
    const minX = Math.min(...nodes.map(node => node.x)) - 120;
    const minY = Math.min(...nodes.map(node => node.y)) - 80;
    const width = Math.max(600, Math.max(...nodes.map(node => node.x)) + 180 - minX);
    const height = Math.max(400, Math.max(...nodes.map(node => node.y)) + 100 - minY);
    bounds = { x: minX, y: minY, width, height };
  }
  const originalBounds = { ...bounds };
  const applyBounds = () => svg.setAttribute("viewBox", `${bounds.x} ${bounds.y} ${bounds.width} ${bounds.height}`);
  const zoom = factor => {
    const width = Math.max(originalBounds.width / 8, Math.min(originalBounds.width * 4, bounds.width * factor));
    const height = width * originalBounds.height / originalBounds.width;
    bounds = { x: bounds.x + (bounds.width - width) / 2, y: bounds.y + (bounds.height - height) / 2, width, height };
    applyBounds();
  };
  svg.addEventListener("wheel", event => { event.preventDefault(); zoom(event.deltaY > 0 ? 1.12 : 1 / 1.12); }, { passive: false });
  let drag = null;
  svg.addEventListener("pointerdown", event => {
    dragged = false;
    if (event.target.closest('[role="button"]')) return;
    drag = { x: event.clientX, y: event.clientY, left: bounds.x, top: bounds.y };
    svg.setPointerCapture(event.pointerId);
  });
  svg.addEventListener("pointermove", event => {
    if (!drag) return;
    const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
    if (Math.hypot(dx, dy) > 3) dragged = true;
    bounds.x = drag.left - dx * bounds.width / svg.clientWidth;
    bounds.y = drag.top - dy * bounds.height / svg.clientHeight;
    applyBounds();
  });
  const endDrag = event => {
    if (svg.hasPointerCapture(event.pointerId)) svg.releasePointerCapture(event.pointerId);
    drag = null;
  };
  svg.addEventListener("pointerup", endDrag);
  svg.addEventListener("pointercancel", endDrag);
  let frame = null, disposed = false, previous = 0;
  let paused = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const animate = time => {
    if (disposed || paused) return;
    if (time - previous > 100) {
      for (let i = 0; i < nodes.length; i++) {
        nodes[i].x = nodes[i].baseX + Math.sin(time / 4500 + i) * 2;
        nodes[i].y = nodes[i].baseY + Math.cos(time / 6000 + i) * 2;
      }
      paint(); previous = time;
    }
    frame = requestAnimationFrame(animate);
  };
  const pause = button(() => msg(paused ? "Resume" : "Pause"), () => {
    paused = !paused;
    setText(pause, msg(paused ? "Resume" : "Pause"));
    if (paused) cancelAnimationFrame(frame); else frame = requestAnimationFrame(animate);
  });
  const download = button(msg("Share"), () => {
    const copy = svg.cloneNode(true);
    copy.setAttribute("xmlns", namespace);
    const style = svgNode("style");
    style.textContent = "text{font:12px system-ui;fill:#52616b}line{stroke:#9aaab5;stroke-opacity:.25}";
    copy.prepend(style);
    const url = URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(copy)], { type: "image/svg+xml" }));
    const link = document.createElement("a"); link.href = url; link.download = "entity-relationships.svg"; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  setAttribute(download, "title", msg("Download as SVG"));
  tools.append(download, pause, button(msg("Full screen"), async () => {
    try { if (document.fullscreenElement === container) await document.exitFullscreen(); else await container.requestFullscreen(); }
    catch (error) { onError(error); }
  }));
  const zoomTools = element("div", undefined, "entity-graph-zoom");
  const plus = button("+", () => zoom(0.8)), minus = button("−", () => zoom(1.25));
  setAttribute(plus, "aria-label", msg("Zoom in")); setAttribute(minus, "aria-label", msg("Zoom out"));
  zoomTools.append(plus, minus, button(msg("Reset"), () => { bounds = { ...originalBounds }; applyBounds(); }));
  container.replaceChildren(legend, tools, svg, zoomTools);
  applyBounds(); paint();
  if (!paused) frame = requestAnimationFrame(animate);
  return () => { disposed = true; cancelAnimationFrame(frame); if (document.fullscreenElement === container) document.exitFullscreen().catch(() => {}); };
};
