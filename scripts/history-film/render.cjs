// Reproduction of the evolving-node film. All drawing uses a 1920 x 1080 logical canvas.
const fs = require("node:fs"),
  path = require("node:path"),
  cp = require("node:child_process");
const { parseArgs } = require("node:util");
const { Readable } = require("node:stream");
const { pipeline } = require("node:stream/promises");
const { loadData } = require("./data.cjs");
const { values: opts, positionals } = parseArgs({
  allowPositionals: true,
  options: {
    data: { type: "string", default: path.join(__dirname, "snapshot") },
    output: { type: "string", default: path.join(__dirname, "out") },
    scale: { type: "string", default: "2" },
    fps: { type: "string", default: "30" },
    seconds: { type: "string", default: "60" },
    frames: { type: "string", default: "4,6.2,14,23,36,49.5,51.5,52.3,58.5" },
    ffmpeg: { type: "string", default: process.env.GARDEN_FFMPEG || "ffmpeg" },
    fonts: { type: "string" },
    help: { type: "boolean" },
  },
});
if (opts.help) {
  console.log(
    "node render.cjs stills|video [--data DIR] [--output DIR] [--scale 2] [--fps 30] [--seconds 60] [--frames 4,49.5,58.5] [--fonts DIR] [--ffmpeg PATH]",
  );
  process.exit(0);
}
const mode = positionals[0] || "stills";
if (!["stills", "video"].includes(mode) || positionals.length > 1)
  throw new Error("Choose stills or video");
const dir = path.resolve(opts.output),
  { D, G, PH, story, manifest } = loadData(path.resolve(opts.data));
const SCALE = Number(opts.scale),
  W = 1920,
  H = 1080,
  FPS = Number(opts.fps),
  SECONDS = Number(opts.seconds),
  TAU = Math.PI * 2;
if (
  !Number.isFinite(SCALE) ||
  SCALE < 0.1 ||
  SCALE > 4 ||
  ![W * SCALE, H * SCALE].every((n) => Number.isInteger(n) && n % 2 === 0)
)
  throw new Error("Scale must give even dimensions (0.1–4)");
if (
  !Number.isInteger(FPS) ||
  FPS < 1 ||
  FPS > 60 ||
  !Number.isFinite(SECONDS) ||
  SECONDS <= 0 ||
  SECONDS > 600 ||
  !Number.isInteger(FPS * SECONDS)
)
  throw new Error("Invalid fps/seconds");
const frameTimes = opts.frames.split(",").map(Number);
if (frameTimes.some((v) => !Number.isFinite(v) || v < 0 || v > 60))
  throw new Error("Still times must be between 0 and 60");
const { createCanvas, GlobalFonts } = require("@napi-rs/canvas");
const fontDir =
  opts.fonts ||
  process.env.GARDEN_FONT_DIR ||
  (process.platform === "win32"
    ? path.join(process.env.WINDIR || "C:/Windows", "Fonts")
    : process.platform === "darwin"
      ? "/System/Library/Fonts/Supplemental"
      : "/usr/share/fonts/truetype/dejavu");
const fonts = {};
for (const [family, candidates] of [
  ["Title", ["bahnschrift.ttf", "Arial.ttf", "DejaVuSans.ttf"]],
  ["Body", ["segoeui.ttf", "Arial.ttf", "DejaVuSans.ttf"]],
  ["Mono", ["consola.ttf", "Andale Mono.ttf", "DejaVuSansMono.ttf"]],
]) {
  const f = candidates
    .map((n) => path.join(fontDir, n))
    .find((p) => fs.existsSync(p));
  if (!f || !GlobalFonts.registerFromPath(f, family))
    throw new Error(
      "Missing " + family + " font; provide --fonts DIR (see README)",
    );
  fonts[family] = {
    file: path.basename(f),
    sha256: require("node:crypto")
      .createHash("sha256")
      .update(fs.readFileSync(f))
      .digest("hex"),
  };
}
fs.mkdirSync(dir, { recursive: true });
const canvas = createCanvas(W * SCALE, H * SCALE),
  c = canvas.getContext("2d");
c.scale(SCALE, SCALE);
const P = {
  bg: "#0c1428",
  dark: "#080f20",
  fg: "#f2f5ff",
  muted: "#99abc9",
  edge: "#7c8bab",
  cyan: "#63dce9",
  purple: "#b39bff",
  coral: "#ffa18c",
  amber: "#f4cc75",
  mint: "#82e8bd",
  blue: "#82aaff",
  other: "#7995b3",
  red: "#ff5b79",
  green: "#6ef4af",
};
const groups = [
  { name: "Planning & review", color: P.mint, x: 844, y: 336 },
  { name: "Shared core", color: P.amber, x: 1130, y: 488 },
  { name: "Scheduler", color: P.purple, x: 1310, y: 310 },
  { name: "Interface", color: P.cyan, x: 1658, y: 506 },
  { name: "Workers", color: P.coral, x: 1494, y: 780 },
  { name: "Tests", color: P.blue, x: 1055, y: 795 },
  { name: "Docs & other files", color: P.other, x: 768, y: 843 },
];
const clamp = (v, a = 0, b = 1) => Math.max(a, Math.min(b, v)),
  ease = (v) => {
    v = clamp(v);
    return v * v * (3 - 2 * v);
  },
  mix = (a, b, t) => a + (b - a) * t;
const hash = (n) => {
  const v = Math.sin(n * 127.1 + 311.7) * 43758.5453123;
  return v - Math.floor(v);
};
const knots = story.knots;
function realAt(v) {
  let k = 0;
  while (k < knots.length - 2 && knots[k + 1][0] < v) k++;
  const [a, x] = knots[k],
    [b, y] = knots[k + 1];
  return mix(x, y, clamp((v - a) / (b - a)));
}
function filmAt(t) {
  if (t <= D.start) return 2;
  if (t >= D.end) return 57;
  let k = 1;
  while (k < knots.length - 2 && knots[k + 1][1] < t) k++;
  const [a, x] = knots[k],
    [b, y] = knots[k + 1];
  return mix(a, b, clamp((t - x) / (y - x)));
}
const versions = new Map(G.versions.map((v) => [v.sha, v]));
const commits = D.commits.map((m, i) => ({ ...m, index: i, f: filmAt(m.at) }));
const tasks = new Map(
  D.tasks.map((t, i) => {
    t.index = i;
    t.events = t.events.map((e, j) => ({
      at: e[0],
      s: e[1],
      label: e[2],
      detail: e[3],
      f: filmAt(e[0]),
      previous: j ? t.events[j - 1][1] : e[1],
    }));
    return [t.id, t];
  }),
);
// Keep a motion's origin across same-stage bookkeeping events so a rejection can
// finish its return arc instead of being cut off by the next one-second tick.
for (const t of D.tasks) {
  let prior = null;
  for (const e of t.events) {
    if (!prior || e.s !== prior.s) {
      e.moveFrom = prior?.s ?? e.s;
      e.moveF = e.f;
    } else {
      e.moveFrom = prior.moveFrom;
      e.moveF = prior.moveF;
    }
    prior = e;
  }
}
const history = D.files.map(() => []),
  maxSizes = D.files.map(() => 0);
commits.forEach((m) => {
  const sizes = new Map(versions.get(m.sha)?.sizes || []);
  m.changes.forEach(([i, op]) => {
    const bytes = sizes.get(i) || 0;
    history[i].push({ f: m.f, at: m.at, op, bytes, commit: m });
    maxSizes[i] = Math.max(maxSizes[i], bytes);
  });
});
const GROW_SECONDS = 0.8;
for (const hs of history) {
  let prior = null;
  for (const e of hs) {
    e.previousBytes = prior?.targetBytes ?? 0;
    e.fromBytes = prior
      ? mix(
          prior.fromBytes,
          prior.targetBytes,
          ease((e.f - prior.f) / GROW_SECONDS),
        )
      : 0;
    e.targetBytes = e.op === "D" ? 0 : e.bytes;
    prior = e;
  }
}
function displayedBytes(e, v) {
  return e
    ? mix(e.fromBytes, e.targetBytes, ease((v - e.f) / GROW_SECONDS))
    : 0;
}
function groupFor(p) {
  if (p.startsWith("tests/")) return 5;
  if (p.startsWith("src/garden/scheduler")) return 2;
  if (
    p.startsWith("src/garden/web") ||
    p.startsWith("src/garden/tui") ||
    p.startsWith("src/garden/static")
  )
    return 3;
  if (
    p.startsWith("src/garden/runner") ||
    p.startsWith("src/garden/worker") ||
    p.startsWith("src/garden/provision") ||
    p.startsWith("src/garden/pool")
  )
    return 4;
  if (
    p.startsWith("src/") &&
    /\/(brief|graph|planner|review|personas|retro|principles|goals|triage|coordination)\b/.test(
      p,
    )
  )
    return 0;
  if (p.startsWith("src/") || p.startsWith("scripts/")) return 1;
  return 6;
}
const nodes = D.files.map((p, i) => ({
  id: i,
  path: p,
  g: groupFor(p),
  x: 0,
  y: 0,
  r: 0,
  degree: 0,
  fx: 0,
  fy: 0,
}));
const codeNodes = nodes.filter((n) => n.g !== 6),
  codeIds = new Set(codeNodes.map((n) => n.id));
const layoutEdges = G.edges.filter(
  ([a, b]) => codeIds.has(a) && codeIds.has(b),
);
layoutEdges.forEach(([a, b]) => {
  nodes[a].degree++;
  nodes[b].degree++;
});
function radius(bytes, other = false) {
  if (bytes <= 0) return 0;
  return other
    ? clamp(Math.sqrt(bytes / 22000), 1.2, 3)
    : clamp(Math.sqrt(bytes / 145), 2.8, 38);
}
for (const n of nodes) {
  const g = groups[n.g],
    a = hash(n.id + 21) * TAU,
    r =
      n.g === 6
        ? Math.sqrt(hash(n.id + 89)) * 70
        : Math.sqrt(hash(n.id + 89)) * 100;
  n.x = g.x + Math.cos(a) * r;
  n.y = g.y + Math.sin(a) * r * (n.g === 6 ? 0.48 : 0.78);
  n.r = radius(maxSizes[n.id], n.g === 6);
}
// Deterministic spring layout: dependency edges attract; file-sized nodes repel.
for (let iter = 0; iter < 460; iter++) {
  for (const n of codeNodes) {
    const g = groups[n.g];
    n.fx = (g.x - n.x) * 0.042;
    n.fy = (g.y - n.y) * 0.042;
  }
  for (let i = 0; i < codeNodes.length; i++)
    for (let j = i + 1; j < codeNodes.length; j++) {
      const a = codeNodes[i],
        b = codeNodes[j],
        dx = a.x - b.x,
        dy = a.y - b.y,
        d2 = dx * dx + dy * dy + 0.1,
        d = Math.sqrt(d2),
        min = a.r + b.r + 10;
      let force = 600 / d2;
      if (d < min) force += (min - d) * 0.38;
      if (d < 170) {
        a.fx += (dx / d) * force;
        a.fy += (dy / d) * force;
        b.fx -= (dx / d) * force;
        b.fy -= (dy / d) * force;
      }
    }
  for (const [ai, bi] of layoutEdges) {
    const a = nodes[ai],
      b = nodes[bi],
      dx = b.x - a.x,
      dy = b.y - a.y,
      d = Math.hypot(dx, dy) || 1,
      ideal = a.g === b.g ? 60 : 265;
    const f =
      ((d - ideal) * 0.008) / (1 + Math.sqrt(a.degree + b.degree) * 0.05);
    a.fx += (dx / d) * f;
    a.fy += (dy / d) * f;
    b.fx -= (dx / d) * f;
    b.fy -= (dy / d) * f;
  }
  const step = 0.35 * (1 - iter / 600);
  for (const n of codeNodes) {
    n.x = clamp(n.x + n.fx * step, 693, 1805);
    n.y = clamp(n.y + n.fy * step, 230, 893);
  }
}
const centroids = groups.map((g, i) => {
  const a = nodes.filter((n) => n.g === i);
  return {
    x: a.length ? a.reduce((s, n) => s + n.x, 0) / a.length : g.x,
    y: a.length ? a.reduce((s, n) => s + n.y, 0) / a.length : g.y,
  };
});
for (const n of nodes) {
  n.baseX = n.x;
  n.baseY = n.y;
}
const groupPeaks = groups.map(() => 0),
  groupMass = groups.map(() => 0),
  liveSizes = nodes.map(() => 0);
for (const m of commits) {
  const sizes = new Map(versions.get(m.sha)?.sizes || []);
  for (const [i, op] of m.changes) {
    const g = nodes[i].g,
      b = op === "D" ? 0 : sizes.get(i) || 0;
    groupMass[g] += b - liveSizes[i];
    liveSizes[i] = b;
    groupPeaks[g] = Math.max(groupPeaks[g], groupMass[g]);
  }
}
fs.writeFileSync(
  path.join(dir, "layout.json"),
  JSON.stringify(
    nodes.map((n) => ({
      id: n.id,
      path: n.path,
      group: groups[n.g].name,
      x: n.x,
      y: n.y,
      maxBytes: maxSizes[n.id],
    })),
  ),
);
const phaseNumber = (s) => Number(String(s).match(/phase-(\d+)/)?.[1]) || 1;
function phaseAt(task, actual) {
  let p = phaseNumber(task.phase);
  const moves = PH.moves
    .filter((m) => m.id === task.id)
    .sort((a, b) => a.at - b.at);
  if (moves.length) {
    p = phaseNumber(moves[0].from);
    for (const m of moves) if (m.at <= actual) p = phaseNumber(m.to);
  }
  return p;
}
const shotSpecs = story.shots;
function shotAt(v, actual) {
  const s = shotSpecs.find((x) => v >= x[2] && v < x[3]);
  if (!s) return null;
  let t = tasks.get(s[0]);
  if (t?.created > actual) {
    const m = commits.filter((m) => m.task && m.at <= actual).at(-1);
    t = m ? tasks.get(m.task) : null;
  }
  return { task: t, title: s[1], from: s[2], to: s[3] };
}
function stateAt(task, t) {
  let lo = 0,
    hi = task.events.length;
  while (lo < hi) {
    const m = (lo + hi) >> 1;
    if (task.events[m].at <= t) lo = m + 1;
    else hi = m;
  }
  return lo ? task.events[lo - 1] : null;
}
function fileAt(i, v) {
  const a = history[i];
  let lo = 0,
    hi = a.length;
  while (lo < hi) {
    const m = (lo + hi) >> 1;
    if (a[m].f <= v) lo = m + 1;
    else hi = m;
  }
  return lo ? a[lo - 1] : null;
}
function text(
  s,
  x,
  y,
  size = 25,
  color = P.fg,
  font = "Body",
  align = "left",
  alpha = 1,
) {
  c.save();
  c.globalAlpha = alpha;
  c.font = `${size}px "${font}"`;
  c.fillStyle = color;
  c.textAlign = align;
  c.textBaseline = "alphabetic";
  c.fillText(s, x, y);
  c.restore();
}
function dot(p, r, col, alpha = 1, outline = false) {
  c.save();
  c.globalAlpha = alpha;
  c.beginPath();
  c.arc(p.x, p.y, r, 0, TAU);
  if (outline) {
    c.strokeStyle = col;
    c.lineWidth = 1.5;
    c.stroke();
  } else {
    c.fillStyle = col;
    c.fill();
  }
  c.restore();
}
function glow(p, r, col, alpha = 0.3) {
  if (alpha <= 0 || r <= 0) return;
  c.save();
  c.globalAlpha = alpha;
  const g = c.createRadialGradient(p.x, p.y, 0, p.x, p.y, r);
  g.addColorStop(0, col);
  g.addColorStop(1, "transparent");
  c.fillStyle = g;
  c.fillRect(p.x - r, p.y - r, r * 2, r * 2);
  c.restore();
}
function line(a, b, col, width = 1, alpha = 1) {
  c.save();
  c.globalAlpha = alpha;
  c.strokeStyle = col;
  c.lineWidth = width;
  c.lineCap = "round";
  c.beginPath();
  c.moveTo(a.x, a.y);
  c.lineTo(b.x, b.y);
  c.stroke();
  c.restore();
}
function pathCurve(a, b, bend = 0) {
  const dx = b.x - a.x,
    dy = b.y - a.y,
    d = Math.hypot(dx, dy) || 1;
  return [
    a,
    {
      x: a.x + dx * 0.34 - (dy / d) * bend,
      y: a.y + dy * 0.34 + (dx / d) * bend,
    },
    {
      x: a.x + dx * 0.7 - (dy / d) * bend,
      y: a.y + dy * 0.7 + (dx / d) * bend,
    },
    b,
  ];
}
function onCurve(p, t) {
  t = clamp(t);
  const u = 1 - t;
  return {
    x:
      u * u * u * p[0].x +
      3 * u * u * t * p[1].x +
      3 * u * t * t * p[2].x +
      t * t * t * p[3].x,
    y:
      u * u * u * p[0].y +
      3 * u * u * t * p[1].y +
      3 * u * t * t * p[2].y +
      t * t * t * p[3].y,
  };
}
function drawCurve(p, col, width = 1, alpha = 1) {
  c.save();
  c.globalAlpha = alpha;
  c.strokeStyle = col;
  c.lineWidth = width;
  c.lineCap = "round";
  c.beginPath();
  c.moveTo(p[0].x, p[0].y);
  c.bezierCurveTo(p[1].x, p[1].y, p[2].x, p[2].y, p[3].x, p[3].y);
  c.stroke();
  c.restore();
}
function arrow(a, b, col, alpha = 1, size = 6) {
  const angle = Math.atan2(b.y - a.y, b.x - a.x);
  line(
    b,
    {
      x: b.x - Math.cos(angle - 0.5) * size,
      y: b.y - Math.sin(angle - 0.5) * size,
    },
    col,
    1.4,
    alpha,
  );
  line(
    b,
    {
      x: b.x - Math.cos(angle + 0.5) * size,
      y: b.y - Math.sin(angle + 0.5) * size,
    },
    col,
    1.4,
    alpha,
  );
}
function wrap(s, width, size, max = 3) {
  c.font = `${size}px "Body"`;
  let row = "",
    out = [];
  for (const word of s.split(/\s+/)) {
    if (row && c.measureText(row + " " + word).width > width) {
      out.push(row);
      row = word;
    } else row += (row ? " " : "") + word;
  }
  if (row) out.push(row);
  if (out.length > max) {
    out = out.slice(0, max);
    let last = out.at(-1);
    while (c.measureText(last + "…").width > width) last = last.slice(0, -1);
    out[max - 1] = last + "…";
  }
  return out;
}
const depSnapshots = [];
let importState = new Map(),
  sizeState = new Map(),
  aliveState = new Set();
for (const m of commits) {
  for (const [i, op] of m.changes)
    if (op === "D") {
      aliveState.delete(i);
      importState.delete(i);
    } else aliveState.add(i);
  const ver = versions.get(m.sha);
  for (const [i, targets] of ver?.updates || []) importState.set(i, targets);
  for (const [i, bytes] of ver?.sizes || []) sizeState.set(i, bytes);
  const edges = [];
  for (const [a, targets] of importState)
    if (aliveState.has(a))
      for (const [b, kind] of targets)
        if (aliveState.has(b)) edges.push([a, b, kind]);
  depSnapshots.push({ f: m.f, edges });
}
function edgesAt(v) {
  let lo = 0,
    hi = depSnapshots.length;
  while (lo < hi) {
    const m = (lo + hi) >> 1;
    if (depSnapshots[m].f <= v) lo = m + 1;
    else hi = m;
  }
  return lo ? depSnapshots[lo - 1].edges : [];
}
const background = createCanvas(W * SCALE, H * SCALE),
  bc = background.getContext("2d");
bc.scale(SCALE, SCALE);
let bg = bc.createLinearGradient(0, 0, W, H);
bg.addColorStop(0, P.dark);
bg.addColorStop(0.65, P.bg);
bg.addColorStop(1, "#171d36");
bc.fillStyle = bg;
bc.fillRect(0, 0, W, H);
groups.slice(0, 6).forEach((g) => {
  const grad = bc.createRadialGradient(g.x, g.y, 0, g.x, g.y, 270);
  grad.addColorStop(0, g.color + "09");
  grad.addColorStop(1, g.color + "00");
  bc.fillStyle = grad;
  bc.fillRect(g.x - 270, g.y - 270, 540, 540);
});
const rowY = [282, 382, 482, 582, 682],
  mergeOrigin = { x: 557, y: 682 };
function lane(s) {
  return s === 4 ? 1 : s === 7 ? 4 : s === 5 ? 4 : s === 6 ? 5 : s;
}
function tokenPosition(t, s) {
  const l = lane(s),
    x = 305 + hash(t.index + 31) * 215;
  return {
    x,
    y:
      l === 5
        ? 743 + hash(t.index) * 18
        : rowY[l] + (hash(t.index + 93) - 0.5) * 32,
  };
}
const taskColor = new Map(
  D.tasks.map((t) => {
    const id = Number(t.id.slice(3));
    return [
      t.id,
      `hsl(${(id * 137.507764) % 360}, ${72 + (id % 13)}%, ${66 + (id % 9)}%)`,
    ];
  }),
);
function positionAt(t, v) {
  const e = stateAt(t, realAt(v));
  if (!e || e.s === 8) return null;
  let p = tokenPosition(t, e.s);
  const age = v - e.moveF,
    prev = tokenPosition(t, e.moveFrom);
  if (e.s === 4 && e.moveFrom === 3 && age < 0.7)
    return onCurve(
      [
        { x: prev.x, y: 582 },
        { x: 74, y: 582 },
        { x: 74, y: 382 },
        { x: p.x, y: 382 },
      ],
      ease(age / 0.7),
    );
  if (lane(e.moveFrom) !== lane(e.s) && age < 0.5)
    return onCurve(
      pathCurve(prev, p, (t.index % 2 ? 1 : -1) * 12),
      ease(age / 0.5),
    );
  return p;
}
function reviewPulse(e, v) {
  const age = v - e.f;
  return age >= 0 && age < 1.2
    ? Math.pow(Math.sin((age / 1.2) * Math.PI * 2), 2) *
        (0.8 + 0.2 * (1 - age / 1.2))
    : 0;
}
function drawProcess(v, actual, shot) {
  text("The development loop", 76, 199, 29, P.fg, "Title");
  const labels = ["Ready", "Build", "Checks", "Review", "Merge"],
    laneTitles = [];
  line({ x: 163, y: rowY[0] }, { x: 163, y: rowY[4] }, P.edge, 1.6, 0.23);
  for (let i = 0; i < 5; i++) {
    const y = rowY[i];
    dot({ x: 163, y }, 7, P.muted, 0.42, true);
    text(labels[i], 190, y + 8, 27, P.fg, "Title");
    line({ x: 302, y: y + 29 }, { x: 548, y: y + 29 }, P.edge, 1, 0.13);
    if (i < 4)
      arrow({ x: 163, y: y + 38 }, { x: 163, y: y + 60 }, P.muted, 0.48, 7);
    const at = realAt(Math.floor(v / 1.15) * 1.15);
    const active = D.tasks
      .map((t) => ({ t, e: stateAt(t, at) }))
      .filter((o) => o.e && o.e.s !== 8 && o.e.s !== 7 && lane(o.e.s) === i)
      .sort((a, b) => b.e.at - a.e.at)[0];
    if (active) {
      const s = wrap(active.t.id + "  " + active.t.title, 411, 19, 1)[0];
      laneTitles.push({ s, y: y + 60, col: taskColor.get(active.t.id) });
    }
  }
  text("Queued or held", 76, 765, 20, P.muted);
  line({ x: 274, y: 752 }, { x: 548, y: 752 }, P.edge, 1, 0.12);
  const returnPath = [
    { x: 163, y: 582 },
    { x: 53, y: 582 },
    { x: 53, y: 382 },
    { x: 163, y: 382 },
  ];
  drawCurve(returnPath, P.red, 1.4, 0.25);
  arrow({ x: 120, y: 382 }, { x: 155, y: 382 }, P.red, 0.55, 9);
  text("Revise", 57, 471, 22, P.red);
  const featured = shot?.task?.id;
  for (const t of D.tasks) {
    const e = stateAt(t, actual);
    if (!e || e.s === 8) continue;
    const merged = t.merges.length
      ? commits.find((m) => m.sha === t.merges.at(-1))
      : null;
    if (merged && v > merged.f + 0.45) continue;
    let p = positionAt(t, v);
    const age = v - e.f;
    let color = taskColor.get(t.id),
      pulse = 0;
    const rev = t.events
      .filter(
        (x) =>
          x.at <= actual &&
          (x.label === "Review approved" ||
            (x.label === "Changes requested" && x.detail)),
      )
      .at(-1);
    if (rev) {
      pulse = reviewPulse(rev, v);
      if (pulse > 0) {
        color = rev.label === "Review approved" ? P.green : P.red;
        glow(p, 18 + 12 * pulse, color, 0.48 * pulse);
      }
    }
    const selected = t.id === featured;
    let prior = null;
    for (let j = 10; j >= 0; j--) {
      const pv = Math.max(0, v - j * 0.05),
        pt = positionAt(t, pv);
      if (pt && prior)
        line(
          prior,
          pt,
          taskColor.get(t.id),
          selected ? 3.2 : 2.0,
          (1 - j / 11) * 0.65,
        );
      prior = pt;
    }
    dot(
      p,
      selected ? 5.7 : 2.4 + hash(t.index) * 1.2,
      color,
      selected ? 1 : 0.82,
    );
    if (pulse > 0) dot(p, 7 + 4 * pulse, color, 0.65 * pulse, true);
    if (selected) {
      dot(p, 12, color, 0.7, true);
      text(t.id, p.x, p.y - 21, 23, P.fg, "Mono", "center");
    }
  }
  if (shot?.task) {
    const t = shot.task,
      e = stateAt(t, actual);
    const rev = t.events
      .filter(
        (x) =>
          x.at <= actual &&
          (x.label === "Review approved" ||
            (x.label === "Changes requested" && x.detail)),
      )
      .at(-1);
    if (rev && v - rev.f < 1.2) {
      const p = { x: 163, y: 582 },
        a = reviewPulse(rev, v),
        color = rev.label === "Review approved" ? P.green : P.red;
      glow(p, 62, color, 0.33 * a);
      dot(p, 10 + 11 * a, color, 0.85 * a, true);
      text(
        rev.label === "Review approved" ? "Approved" : "Changes requested",
        190,
        619,
        22,
        color,
        "Body",
        "left",
        Math.max(0.35, a),
      );
    }
  }
  for (const { s, y, col } of laneTitles) {
    c.save();
    c.font = '19px "Body"';
    c.strokeStyle = P.dark;
    c.lineWidth = 5;
    c.lineJoin = "round";
    c.strokeText(s, 190, y);
    c.restore();
    text(s, 190, y, 19, col, "Body", "left", 0.9);
  }
  dot(mergeOrigin, 5, P.green, 0.8);
}
function drawNetwork(v, actual) {
  const states = nodes.map((n) => fileAt(n.id, v)),
    alive = states.map((e) => e && e.op !== "D");
  const masses = groups.map(() => 0);
  for (const n of nodes) masses[n.g] += displayedBytes(states[n.id], v);
  for (const n of nodes) {
    const center = centroids[n.g],
      ratio = clamp(masses[n.g] / Math.max(1, groupPeaks[n.g]));
    const spread =
      n.g === 6 ? 0.7 + 0.3 * Math.sqrt(ratio) : 0.34 + 0.66 * Math.sqrt(ratio);
    n.x = center.x + (n.baseX - center.x) * spread;
    n.y = center.y + (n.baseY - center.y) * spread;
    n.drawR = radius(displayedBytes(states[n.id], v), n.g === 6);
  }
  const edges = edgesAt(v);
  // Dependency edges stay quiet. They never flash, brighten or carry update pulses.
  for (const [a, b, kind] of edges) {
    const na = nodes[a],
      nb = nodes[b];
    if (!na || !nb) continue;
    const same = na.g === nb.g;
    line(
      na,
      nb,
      same ? groups[na.g].color : P.edge,
      kind === "template" ? 0.9 : 1.05,
      same ? 0.07 : 0.03,
    );
  }
  for (const n of nodes) {
    const e = states[n.id];
    if (!e) continue;
    const age = v - e.f,
      col = groups[n.g].color,
      r = n.drawR;
    if (e.op === "D") {
      if (age < GROW_SECONDS) {
        dot(n, r, P.red, 0.55 * (1 - age / GROW_SECONDS));
        glow(n, r * 2 + 10, P.red, 0.25 * (1 - age / GROW_SECONDS));
      }
      continue;
    }
    const fresh = Math.exp(-Math.max(0, age) / 1.7),
      alpha = n.g === 6 ? 0.13 + fresh * 0.65 : 0.2 + fresh * 0.76;
    if (age < 5) {
      glow(
        n,
        r * 3.25 + 15,
        col,
        (n.g === 6 ? 0.27 : 0.49) * Math.exp(-age / 1.45),
      );
      glow(n, r * 1.5 + 6, col, 0.3 * Math.exp(-age / 0.8));
    }
    dot(n, r, col, alpha);
    if (n.g !== 6) {
      dot(n, r, col, 0.2, true);
      dot(n, Math.max(0.9, r * 0.31), P.fg, 0.08 + fresh * 0.79);
    }
    if (age < 2.7)
      dot(n, r + 5 + age * 5, col, 0.4 * Math.exp(-age / 0.8), true);
  }
  // Direct flights have one origin (Merge) and the actual file as their destination.
  for (const m of commits) {
    if (!m.task) continue;
    const age = v - m.f;
    if (age < 0 || age > 1.05) continue;
    for (const [i, op] of m.changes) {
      const n = nodes[i],
        col = taskColor.get(m.task) || groups[n.g].color;
      const duration = 0.6 + hash(i + 50) * 0.35,
        t = clamp(age / duration),
        bend = (hash(i + 95) - 0.5) * 90;
      const route = pathCurve(mergeOrigin, n, bend),
        p = onCurve(route, ease(t));
      if (t < 1) {
        for (let j = 8; j > 0; j--) {
          const a = onCurve(route, ease(clamp(t - j * 0.02))),
            b = onCurve(route, ease(clamp(t - (j - 1) * 0.02)));
          line(a, b, col, n.g === 6 ? 1.6 : 3, (1 - j / 9) * 0.8);
        }
        glow(p, 11, col, 0.3);
        dot(p, n.g === 6 ? 1.8 : 3.6, col, 0.95);
      }
    }
  }
  const boxes = [];
  const protect = (x, y, width, height) =>
    boxes.push({ x, y, w: width, h: height });
  // Stable subsystem labels serve as the graph's spatial legend.
  const anchors = [
    { x: 837, y: 343 },
    { x: 1037, y: 623 },
    { x: 1240, y: 220 },
    { x: 1635, y: 355 },
    { x: 1420, y: 824 },
    { x: 1005, y: 922 },
    { x: 681, y: 913 },
  ];
  groups.forEach((g, i) => {
    if (!nodes.some((n) => n.g === i && alive[n.id])) return;
    const p = anchors[i];
    text(g.name, p.x, p.y, 23, g.color, "Title");
    c.font = '23px "Title"';
    protect(p.x - 3, p.y - 26, c.measureText(g.name).width + 6, 33);
  });
  const priority = nodes
    .filter((n) => n.g !== 6 && alive[n.id] && v - states[n.id].f < 1.55)
    .sort(
      (a, b) =>
        states[b.id].f - states[a.id].f ||
        (a.g === 5 ? 1 : 0) - (b.g === 5 ? 1 : 0) ||
        b.degree - a.degree,
    );
  const labels = [];
  for (const n of priority) {
    if (labels.length >= 6) break;
    let s = n.path.replace(/^src\/garden\//, "");
    if (s.length > 35) s = "…/" + s.split("/").slice(-2).join("/");
    if (s.length > 40) s = s.slice(0, 37) + "…";
    const kb = states[n.id].bytes / 1024;
    s += "  " + (kb >= 10 ? Math.round(kb) : kb.toFixed(1)) + " KB";
    c.font = '25px "Mono"';
    const width = c.measureText(s).width;
    const candidates = [
      { x: n.x + n.drawR + 11, y: n.y - 11 },
      { x: n.x - width - n.drawR - 8, y: n.y - 11 },
      { x: n.x - width / 2, y: n.y + n.drawR + 25 },
      { x: n.x - width / 2, y: n.y - n.drawR - 16 },
    ];
    let chosen;
    for (const p of candidates) {
      p.x = clamp(p.x, 667, W - 48 - width);
      p.y = clamp(p.y, 219, 898);
      const box = { x: p.x - 5, y: p.y - 25, w: width + 10, h: 33 };
      if (
        !boxes.some(
          (b) =>
            box.x < b.x + b.w + 8 &&
            box.x + box.w > b.x - 8 &&
            box.y < b.y + b.h + 8 &&
            box.y + box.h > b.y - 8,
        )
      ) {
        chosen = { ...p, box };
        break;
      }
    }
    if (!chosen) continue;
    boxes.push(chosen.box);
    labels.push({ n, s, p: chosen });
  }
  for (const { n, s, p } of labels) {
    const age = v - states[n.id].f,
      a = clamp(age / 0.08) * clamp((1.55 - age) / 0.32);
    c.save();
    c.globalAlpha = 0.95 * a;
    c.font = '25px "Mono"';
    c.lineWidth = 6;
    c.lineJoin = "round";
    c.strokeStyle = P.bg;
    c.strokeText(s, p.x, p.y);
    c.restore();
    line(
      n,
      { x: p.x + (p.x < n.x ? c.measureText(s).width : 0), y: p.y - 8 },
      groups[n.g].color,
      1,
      0.22 * a,
    );
    text(s, p.x, p.y, 25, P.fg, "Mono", "left", a);
  }
  return {
    files: alive.filter(Boolean).length,
    edges: edges.length,
    codeFiles: nodes.filter((n) => n.g !== 6 && alive[n.id]).length,
    codeBytes: nodes.reduce(
      (s, n) => s + (n.g !== 6 && alive[n.id] ? states[n.id].bytes : 0),
      0,
    ),
  };
}
function drawPhases(v, actual, shot) {
  text("Phases", 76, 1031, 20, P.muted, "Title");
  const focus = shot?.task ? phaseAt(shot.task, actual) : null;
  for (let i = 1; i <= 8; i++) {
    const x = 220 + (i - 1) * 165,
      closed = PH.closed.some(
        (p) => phaseNumber(p.phase) === i && p.at <= actual,
      );
    const current = focus === i;
    text(
      String(i).padStart(2, "0"),
      x,
      1032,
      24,
      current ? P.fg : closed ? P.green : P.muted,
      "Mono",
    );
    if (current)
      line({ x: x - 2, y: 1043 }, { x: x + 94, y: 1043 }, P.fg, 2.7, 0.9);
    else
      line(
        { x: x - 2, y: 1043 },
        { x: x + 94, y: 1043 },
        closed ? P.green : P.edge,
        1.8,
        closed ? 0.7 : 0.22,
      );
    if (closed) {
      dot({ x: x + 70, y: 1023 }, 3, P.green, 0.8);
    } else if (current) text("in focus", x + 39, 1031, 16, P.muted);
  }
  text(
    "Green mark = recorded phase closure",
    1845,
    1032,
    18,
    P.muted,
    "Body",
    "right",
  );
}
function drawLens(v, actual, shot) {
  if (!shot?.task) return;
  const t = shot.task,
    e = stateAt(t, actual),
    a = ease((v - shot.from) / 0.45) * ease((shot.to - v) / 0.4);
  text(
    t.id + "  /  Phase " + String(phaseAt(t, actual)).padStart(2, "0"),
    76,
    820,
    24,
    taskColor.get(t.id),
    "Mono",
    "left",
    a,
  );
  wrap(t.title, 515, 29, 3).forEach((s, i) =>
    text(s, 76, 861 + i * 36, 29, P.fg, "Body", "left", a),
  );
  if (e) {
    const color =
      e.label === "Review approved"
        ? P.green
        : e.s === 4
          ? P.red
          : taskColor.get(t.id);
    text(e.label, 76, 977, 23, color, "Body", "left", a);
  }
}
function drawArrivals(v) {
  const at = Math.floor(v / 1.45) * 1.45;
  const recent = commits
    .filter((m) => m.task && m.f <= at)
    .slice(-2)
    .reverse();
  recent.forEach((m, i) => {
    const t = tasks.get(m.task);
    if (!t) return;
    const y = 961 + i * 32,
      col = taskColor.get(t.id),
      a = i ? 0.54 : 0.95;
    dot({ x: 704, y: y - 7 }, 3.5, col, a);
    text(t.id, 721, y, 22, col, "Mono", "left", a);
    text(wrap(t.title, 1030, 22, 1)[0], 816, y, 22, P.fg, "Body", "left", a);
  });
}
function render(v) {
  const actual = realAt(v),
    shot = shotAt(v, actual);
  c.drawImage(background, 0, 0, W, H);
  const stats = drawNetwork(v, actual);
  drawProcess(v, actual, shot);
  drawLens(v, actual, shot);
  drawArrivals(v);
  text("Context Garden", 76, 73, 46, P.fg, "Title");
  text("A system building itself", 76, 113, 25, P.muted);
  const date = new Date(actual);
  text(
    date.toLocaleDateString("en-GB", {
      day: "2-digit",
      month: "short",
      year: "numeric",
      timeZone: "UTC",
    }),
    1845,
    65,
    29,
    P.fg,
    "Title",
    "right",
  );
  text(
    date.toISOString().slice(11, 16) + " UTC",
    1845,
    105,
    22,
    P.muted,
    "Mono",
    "right",
  );
  text("Code dependencies", 700, 69, 29, P.fg, "Title");
  text("Imports and template references", 700, 109, 21, P.muted);
  text(
    stats.codeFiles +
      " files  /  " +
      (stats.codeBytes / 1048576).toFixed(2) +
      " MB",
    1154,
    109,
    22,
    P.cyan,
    "Mono",
  );
  text("Node area grows with file size (capped)", 700, 150, 21, P.muted);
  text(
    "Modified nodes glow. Older code recedes.",
    1845,
    150,
    21,
    P.muted,
    "Body",
    "right",
  );
  drawPhases(v, actual, shot);
  line({ x: 76, y: 1064 }, { x: 1845, y: 1064 }, P.edge, 2, 0.18);
  line(
    { x: 76, y: 1064 },
    { x: mix(76, 1845, (actual - D.start) / (D.end - D.start)), y: 1064 },
    P.cyan,
    2.5,
    0.8,
  );
  if (v < 3) {
    const a = ease(v / 0.45) * ease((3 - v) / 0.6);
    c.save();
    c.globalAlpha = a;
    c.fillStyle = P.dark + "ed";
    c.fillRect(48, 180, 562, 813);
    text(story.intro, 76, 334, 70, P.fg, "Title");
    text("One evolving", 76, 423, 57, P.fg, "Title");
    text("system.", 76, 493, 57, P.fg, "Title");
    text("Every node is a real file.", 76, 590, 27, P.muted);
    text("Every link is a code dependency.", 76, 632, 25, P.muted);
    text("Watch the work arrive.", 76, 718, 30, P.cyan);
    c.restore();
  }
  if (v >= 57) {
    const a = ease((v - 57) / 0.7);
    c.save();
    c.globalAlpha = a;
    c.fillStyle = P.dark + "f5";
    c.fillRect(49, 172, 555, 823);
    text("Built in the loop.", 76, 278, 49, P.fg, "Title");
    text(D.tasks.length + " tasks", 76, 382, 39, P.cyan, "Title");
    text(
      D.stats.matchedMerges + " task-linked merges",
      76,
      442,
      33,
      P.green,
      "Title",
    );
    text(stats.edges + " code dependencies", 76, 502, 33, P.purple, "Title");
    text(story.dateLabel, 76, 609, 27, P.muted);
    text(
      SECONDS === 60
        ? "Actual history, compressed to a minute."
        : "Actual history, compressed to " + SECONDS + " seconds.",
      76,
      656,
      23,
      P.muted,
    );
    text(
      (SCALE === 2 ? "4K" : W * SCALE + " × " + H * SCALE) +
        "  /  " +
        FPS +
        " fps",
      76,
      950,
      21,
      P.muted,
      "Mono",
    );
    c.restore();
  }
  return stats;
}

function receipt(extra) {
  const stats = render(60);
  return {
    width: W * SCALE,
    height: H * SCALE,
    fps: FPS,
    seconds: SECONDS,
    sourceHead: D.sourceHead,
    sourceStart: new Date(D.start).toISOString(),
    sourceEnd: new Date(D.end).toISOString(),
    inputs: manifest.sha256,
    fonts,
    canvasVersion: require("@napi-rs/canvas/package.json").version,
    nodeVersion: process.version,
    platform: process.platform,
    dependencyExtraction: G.stats,
    final: stats,
    taskRecords: D.tasks.length,
    matchedMerges: D.stats.matchedMerges,
    ...extra,
  };
}
async function main() {
  if (mode === "stills") {
    for (const v of frameTimes) {
      render(v);
      fs.writeFileSync(
        path.join(dir, "frame-" + String(v).replace(".", "-") + ".png"),
        canvas.toBuffer("image/png"),
      );
    }
    fs.writeFileSync(
      path.join(dir, "stills.json"),
      JSON.stringify(receipt({ frames: frameTimes }), null, 2) + "\n",
    );
    console.log("Rendered " + frameTimes.length + " stills to " + dir);
    return;
  }
  const probe = cp.spawnSync(opts.ffmpeg, ["-version"], {
    encoding: "utf8",
    windowsHide: true,
  });
  if (probe.error || probe.status !== 0)
    throw new Error("FFmpeg unavailable; provide --ffmpeg PATH");
  const output = path.join(dir, "context-garden-evolving-nodes.mp4");
  const ff = cp.spawn(
    opts.ffmpeg,
    [
      "-y",
      "-hide_banner",
      "-loglevel",
      "warning",
      "-f",
      "rawvideo",
      "-pix_fmt",
      "rgba",
      "-s",
      W * SCALE + "x" + H * SCALE,
      "-r",
      String(FPS),
      "-i",
      "pipe:0",
      "-an",
      "-c:v",
      "libx264",
      "-preset",
      "fast",
      "-crf",
      "13",
      "-pix_fmt",
      "yuv420p",
      "-threads",
      "2",
      "-movflags",
      "+faststart",
      output,
    ],
    { windowsHide: true, stdio: ["pipe", "ignore", "pipe"] },
  );
  let errors = "";
  ff.stderr.on("data", (b) => {
    errors = (errors + b.toString()).slice(-8000);
  });
  const done = new Promise((resolve, reject) => {
    ff.on("error", reject);
    ff.on("close", (code) =>
      code === 0
        ? resolve()
        : reject(new Error("Encoder " + code + ": " + errors)),
    );
  });
  async function* frames() {
    for (let i = 0; i < FPS * SECONDS; i++) {
      render((i / (FPS * SECONDS)) * 60);
      yield Buffer.from(canvas.data());
      if (i % (FPS * 5) === 0)
        console.log("Rendered " + i / FPS + " / " + SECONDS + " seconds");
    }
  }
  const sent = pipeline(Readable.from(frames()), ff.stdin);
  try {
    await Promise.all([sent, done]);
  } catch (error) {
    ff.kill();
    await Promise.allSettled([sent, done]);
    throw error;
  }
  fs.writeFileSync(
    path.join(dir, "video.json"),
    JSON.stringify(
      receipt({
        video: path.basename(output),
        bytes: fs.statSync(output).size,
        encoder: "H.264, CRF 13, yuv420p",
        ffmpeg: probe.stdout.split("\n")[0],
      }),
      null,
      2,
    ) + "\n",
  );
  console.log("Rendered " + output);
}
main().catch((e) => {
  console.error(e.message);
  process.exitCode = 1;
});
