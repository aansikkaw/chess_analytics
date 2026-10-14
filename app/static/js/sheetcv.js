// Scoresheet photo cleanup, in the browser (no libraries): find the sheet, flatten it, remove shadows,
// boost contrast, and cut zoomed crops of its two halves. Vision models read flat, even, high-contrast pages
// far better than a phone photo taken at an angle under a lamp.
//
//   detectCorners(img)          the sheet's four corners: Otsu threshold, largest bright region, its extreme points
//   warp(img, corners, maxSide) perspective correction (a homography, solved from the four corners) to a flat page
//   enhance(canvas)             illumination correction (divide by a blurred background), then a contrast stretch
//   halves(canvas)              the left and right halves (the sheet's two move columns), slightly overlapping

export async function loadImage(blob) {
  try { return await createImageBitmap(blob, { imageOrientation: "from-image" }); }
  catch {
    const url = URL.createObjectURL(blob);
    try {
      const img = new Image();
      img.src = url;
      await img.decode();
      return img;
    } finally { URL.revokeObjectURL(url); }
  }
}

const dims = (img) => [img.width || img.naturalWidth, img.height || img.naturalHeight];

function drawScaled(img, maxSide) {
  const [w, h] = dims(img);
  const s = Math.min(1, maxSide / Math.max(w, h));
  const c = document.createElement("canvas");
  c.width = Math.max(1, Math.round(w * s));
  c.height = Math.max(1, Math.round(h * s));
  const ctx = c.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(img, 0, 0, c.width, c.height);
  return { canvas: c, ctx, scale: s };
}

function grayOf(data, n) {
  const g = new Float32Array(n);
  for (let i = 0, j = 0; i < n; i++, j += 4) g[i] = 0.299 * data[j] + 0.587 * data[j + 1] + 0.114 * data[j + 2];
  return g;
}

/** Box blur with radius r, via a summed-area table (cost independent of r). */
function boxBlur(src, w, h, r) {
  const W = w + 1;
  const sat = new Float64Array(W * (h + 1));
  for (let y = 0; y < h; y++) {
    let row = 0;
    for (let x = 0; x < w; x++) {
      row += src[y * w + x];
      sat[(y + 1) * W + x + 1] = sat[y * W + x + 1] + row;
    }
  }
  const out = new Float32Array(w * h);
  for (let y = 0; y < h; y++) {
    const y0 = Math.max(0, y - r), y1 = Math.min(h, y + r + 1);
    for (let x = 0; x < w; x++) {
      const x0 = Math.max(0, x - r), x1 = Math.min(w, x + r + 1);
      const sum = sat[y1 * W + x1] - sat[y0 * W + x1] - sat[y1 * W + x0] + sat[y0 * W + x0];
      out[y * w + x] = sum / ((y1 - y0) * (x1 - x0));
    }
  }
  return out;
}

function otsu(g) {
  const hist = new Float64Array(256);
  for (const v of g) hist[Math.max(0, Math.min(255, v | 0))]++;
  const total = g.length;
  let sum = 0;
  for (let t = 0; t < 256; t++) sum += t * hist[t];
  let sumB = 0, wB = 0, best = 0, thr = 127;
  for (let t = 0; t < 256; t++) {
    wB += hist[t];
    if (!wB) continue;
    const wF = total - wB;
    if (!wF) break;
    sumB += t * hist[t];
    const mB = sumB / wB, mF = (sum - sumB) / wF;
    const between = wB * wF * (mB - mF) * (mB - mF);
    if (between > best) { best = between; thr = t; }
  }
  return thr;
}

function quadArea(q) {
  let a = 0;
  for (let i = 0; i < 4; i++) { const [x1, y1] = q[i], [x2, y2] = q[(i + 1) % 4]; a += x1 * y2 - x2 * y1; }
  return Math.abs(a) / 2;
}

function convex(q) {
  let sign = 0;
  for (let i = 0; i < 4; i++) {
    const [ax, ay] = q[i], [bx, by] = q[(i + 1) % 4], [cx, cy] = q[(i + 2) % 4];
    const z = (bx - ax) * (cy - by) - (by - ay) * (cx - bx);
    if (z !== 0) { if (sign && Math.sign(z) !== sign) return false; sign = Math.sign(z); }
  }
  return true;
}

/** The sheet's corners [tl, tr, br, bl] in image pixels. Falls back to the whole photo (slightly inset). */
export function detectCorners(img) {
  const [W, H] = dims(img);
  const fallback = [[W * 0.02, H * 0.02], [W * 0.98, H * 0.02], [W * 0.98, H * 0.98], [W * 0.02, H * 0.98]];
  const { canvas, ctx, scale } = drawScaled(img, 360);
  const w = canvas.width, h = canvas.height, n = w * h;
  const g = boxBlur(grayOf(ctx.getImageData(0, 0, w, h).data, n), w, h, 2);
  const t = otsu(g);
  const mask = new Uint8Array(n);
  for (let i = 0; i < n; i++) mask[i] = g[i] > t ? 1 : 0;
  // largest bright connected region (the paper)
  const label = new Int32Array(n);
  const stack = new Int32Array(n);
  let bestLabel = 0, bestSize = 0, next = 0;
  for (let s = 0; s < n; s++) {
    if (!mask[s] || label[s]) continue;
    next++;
    let top = 0, size = 0;
    stack[top++] = s;
    label[s] = next;
    while (top) {
      const p = stack[--top];
      size++;
      const x = p % w, y = (p / w) | 0;
      if (x > 0 && mask[p - 1] && !label[p - 1]) { label[p - 1] = next; stack[top++] = p - 1; }
      if (x < w - 1 && mask[p + 1] && !label[p + 1]) { label[p + 1] = next; stack[top++] = p + 1; }
      if (y > 0 && mask[p - w] && !label[p - w]) { label[p - w] = next; stack[top++] = p - w; }
      if (y < h - 1 && mask[p + w] && !label[p + w]) { label[p + w] = next; stack[top++] = p + w; }
    }
    if (size > bestSize) { bestSize = size; bestLabel = next; }
  }
  if (bestSize < n * 0.15) return fallback;
  let tl = [0, 0], tr = [0, 0], br = [0, 0], bl = [0, 0];
  let mnS = Infinity, mxS = -Infinity, mnD = Infinity, mxD = -Infinity;
  for (let p = 0; p < n; p++) {
    if (label[p] !== bestLabel) continue;
    const x = p % w, y = (p / w) | 0, s = x + y, d = x - y;
    if (s < mnS) { mnS = s; tl = [x, y]; }
    if (s > mxS) { mxS = s; br = [x, y]; }
    if (d > mxD) { mxD = d; tr = [x, y]; }
    if (d < mnD) { mnD = d; bl = [x, y]; }
  }
  const q = [tl, tr, br, bl].map(([x, y]) => [(x + 0.5) / scale, (y + 0.5) / scale]);
  if (quadArea(q) < W * H * 0.2 || !convex(q)) return fallback;
  return q;
}

/** Solve the homography that maps the flat page (u, v) onto the photo (x, y). */
function homography(dst, src) {
  const A = [], b = [];
  for (let i = 0; i < 4; i++) {
    const [u, v] = dst[i], [x, y] = src[i];
    A.push([u, v, 1, 0, 0, 0, -u * x, -v * x]); b.push(x);
    A.push([0, 0, 0, u, v, 1, -u * y, -v * y]); b.push(y);
  }
  for (let c = 0; c < 8; c++) { // Gaussian elimination with partial pivoting
    let piv = c;
    for (let r = c + 1; r < 8; r++) if (Math.abs(A[r][c]) > Math.abs(A[piv][c])) piv = r;
    [A[c], A[piv]] = [A[piv], A[c]]; [b[c], b[piv]] = [b[piv], b[c]];
    for (let r = c + 1; r < 8; r++) {
      const f = A[r][c] / A[c][c];
      for (let k = c; k < 8; k++) A[r][k] -= f * A[c][k];
      b[r] -= f * b[c];
    }
  }
  const hv = new Array(8).fill(0);
  for (let r = 7; r >= 0; r--) {
    let s = b[r];
    for (let k = r + 1; k < 8; k++) s -= A[r][k] * hv[k];
    hv[r] = s / A[r][r];
  }
  return hv;
}

const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);

/** The sheet, flattened: a canvas of the page seen straight on (grey levels in all three channels). */
export function warp(img, corners, maxSide = 2000) {
  const [W, H] = dims(img);
  const srcScale = Math.min(1, 2600 / Math.max(W, H)); // keep memory sane on phones with huge photos
  const { ctx, canvas } = drawScaled(img, 2600);
  const sw = canvas.width, sh = canvas.height;
  const src = ctx.getImageData(0, 0, sw, sh).data;
  const q = corners.map(([x, y]) => [x * srcScale, y * srcScale]);
  let ow = Math.max(dist(q[0], q[1]), dist(q[3], q[2]));
  let oh = Math.max(dist(q[0], q[3]), dist(q[1], q[2]));
  const s = Math.min(1, maxSide / Math.max(ow, oh));
  ow = Math.max(8, Math.round(ow * s));
  oh = Math.max(8, Math.round(oh * s));
  const hm = homography([[0, 0], [ow, 0], [ow, oh], [0, oh]], q);
  const out = document.createElement("canvas");
  out.width = ow; out.height = oh;
  const octx = out.getContext("2d", { willReadFrequently: true });
  const img2 = octx.createImageData(ow, oh);
  const d = img2.data;
  for (let v = 0; v < oh; v++) {
    for (let u = 0; u < ow; u++) {
      const den = hm[6] * u + hm[7] * v + 1;
      const x = (hm[0] * u + hm[1] * v + hm[2]) / den, y = (hm[3] * u + hm[4] * v + hm[5]) / den;
      const o = (v * ow + u) * 4;
      if (x < 0 || y < 0 || x >= sw - 1 || y >= sh - 1) { d[o] = d[o + 1] = d[o + 2] = 255; d[o + 3] = 255; continue; }
      const x0 = x | 0, y0 = y | 0, fx = x - x0, fy = y - y0;
      const i00 = (y0 * sw + x0) * 4, i10 = i00 + 4, i01 = i00 + sw * 4, i11 = i01 + 4;
      for (let ch = 0; ch < 3; ch++) {
        const top = src[i00 + ch] * (1 - fx) + src[i10 + ch] * fx;
        const bot = src[i01 + ch] * (1 - fx) + src[i11 + ch] * fx;
        d[o + ch] = top * (1 - fy) + bot * fy;
      }
      d[o + 3] = 255;
    }
  }
  octx.putImageData(img2, 0, 0);
  return out;
}

/** Shadows out, contrast up: divide by a blurred background (the paper's own brightness), then stretch so the
 *  ink is near black and the paper white. Returns a new canvas. */
export function enhance(canvas) {
  const w = canvas.width, h = canvas.height, n = w * h;
  const data = canvas.getContext("2d", { willReadFrequently: true }).getImageData(0, 0, w, h).data;
  const g = grayOf(data, n);
  const bg = boxBlur(g, w, h, Math.max(8, Math.round(Math.max(w, h) / 30)));
  const flat = new Float32Array(n);
  for (let i = 0; i < n; i++) flat[i] = Math.min(255, (g[i] / Math.max(1, bg[i])) * 235);
  const hist = new Uint32Array(256);
  for (const v of flat) hist[v | 0]++;
  let lo = 0, hi = 255, acc = 0;
  for (let t = 0; t < 256; t++) { acc += hist[t]; if (acc >= n * 0.01) { lo = t; break; } }
  acc = 0;
  for (let t = 255; t >= 0; t--) { acc += hist[t]; if (acc >= n * 0.02) { hi = t; break; } }
  const span = Math.max(20, hi - lo);
  const out = document.createElement("canvas");
  out.width = w; out.height = h;
  const octx = out.getContext("2d");
  const im = octx.createImageData(w, h);
  for (let i = 0, j = 0; i < n; i++, j += 4) {
    const t = Math.max(0, Math.min(1, (flat[i] - lo) / span));
    const v = 255 * Math.pow(t, 1.25); // a little gamma darkens faint pencil
    im.data[j] = im.data[j + 1] = im.data[j + 2] = v;
    im.data[j + 3] = 255;
  }
  octx.putImageData(im, 0, 0);
  return out;
}

/** The left and right halves (6% overlap), each at most `maxSide` pixels. */
export function halves(canvas, maxSide = 1600) {
  const w = canvas.width, h = canvas.height;
  const cut = (x0, x1) => {
    const cw = Math.round(x1 - x0);
    const s = Math.min(1, maxSide / Math.max(cw, h));
    const c = document.createElement("canvas");
    c.width = Math.round(cw * s); c.height = Math.round(h * s);
    c.getContext("2d").drawImage(canvas, x0, 0, cw, h, 0, 0, c.width, c.height);
    return c;
  };
  return [cut(0, w * 0.53), cut(w * 0.47, w)];
}

export const toBlob = (canvas, q = 0.88) => new Promise((res) => canvas.toBlob(res, "image/jpeg", q));
