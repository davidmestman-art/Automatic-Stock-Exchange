/**
 * tos_chart.js — ThinkorSwim-style Plotly chart overlays & indicator panes
 *
 * Requires: Plotly 2.x (no other dependencies)
 *
 * Usage:
 *   const chart = new TOSChart('my-div-id');
 *   chart.render(data);           // data = {dates,open,high,low,close,volume}
 *   chart.sync([chart2, chart3]); // optional: keep multiple instances in sync
 */

// ── Shared dark theme ─────────────────────────────────────────────────────────
const TOS_THEME = {
  bg:        '#0a0b0f',
  grid:      'rgba(255,255,255,0.05)',
  text:      '#6b7280',
  candleUp:  '#26a69a',
  candleDown:'#ef5350',
  ma:        '#cc44ff',
  vol:       'rgba(140,140,140,0.28)',
  adx:       '#ef4444',
  diPlus:    '#22c55e',
  diMinus:   '#a855f7',
  rsi:       '#d946ef',
  orb:       'rgba(251,191,36,0.9)',
  res:       'rgba(239,68,68,0.5)',
  sup:       'rgba(34,197,94,0.5)',
};

// Panel y-axis domains (Plotly: 0 = bottom, 1 = top)
const TOS_PANE = {
  price: [0.44, 1.00],
  adx:   [0.30, 0.42],
  dmi:   [0.16, 0.28],
  rsi:   [0.01, 0.13],
};

// ── Indicator math ────────────────────────────────────────────────────────────

function calcEMA(src, period) {
  const k = 2 / (period + 1);
  const out = new Array(src.length).fill(null);
  let val = null, count = 0;
  for (let i = 0; i < src.length; i++) {
    if (src[i] == null) continue;
    val = val == null ? src[i] : src[i] * k + val * (1 - k);
    if (++count >= period) out[i] = val;
  }
  return out;
}

function calcRSI(src, period) {
  const out = new Array(src.length).fill(null);
  let gain = 0, loss = 0;
  for (let i = 1; i <= period && i < src.length; i++) {
    const d = src[i] - src[i - 1];
    d >= 0 ? (gain += d) : (loss -= d);
  }
  gain /= period; loss /= period;
  out[period] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss);
  for (let i = period + 1; i < src.length; i++) {
    if (src[i] == null || src[i - 1] == null) continue;
    const d = src[i] - src[i - 1];
    gain = (gain * (period - 1) + Math.max(d, 0)) / period;
    loss = (loss * (period - 1) + Math.max(-d, 0)) / period;
    out[i] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss);
  }
  return out;
}

// Returns { diP, diM, adx } — all arrays length === C.length
function calcDMI(H, L, C, period) {
  const n = C.length;
  const diP = new Array(n).fill(null);
  const diM = new Array(n).fill(null);
  const adx = new Array(n).fill(null);
  if (n < period * 2 + 2) return { diP, diM, adx };

  let atr = 0, pdm = 0, mdm = 0;
  for (let i = 1; i <= period; i++) {
    const h = H[i] ?? 0, l = L[i] ?? 0, pc = C[i - 1] ?? 0;
    const ph = H[i - 1] ?? 0, pl = L[i - 1] ?? 0;
    atr += Math.max(h - l, Math.abs(h - pc), Math.abs(l - pc));
    const up = h - ph, dn = pl - l;
    pdm += (up > dn && up > 0) ? up : 0;
    mdm += (dn > up && dn > 0) ? dn : 0;
  }

  const dxBuf = [];
  function storeAt(idx, a, p_, m_) {
    const dp = a > 0 ? p_ / a * 100 : 0;
    const dm = a > 0 ? m_ / a * 100 : 0;
    diP[idx] = dp; diM[idx] = dm;
    const s = dp + dm;
    dxBuf.push(s > 0 ? Math.abs(dp - dm) / s * 100 : 0);
  }
  storeAt(period, atr, pdm, mdm);

  for (let i = period + 1; i < n; i++) {
    const h = H[i] ?? 0, l = L[i] ?? 0, pc = C[i - 1] ?? 0;
    const ph = H[i - 1] ?? 0, pl = L[i - 1] ?? 0;
    atr = atr - atr / period + Math.max(h - l, Math.abs(h - pc), Math.abs(l - pc));
    const up = h - ph, dn = pl - l;
    pdm = pdm - pdm / period + ((up > dn && up > 0) ? up : 0);
    mdm = mdm - mdm / period + ((dn > up && dn > 0) ? dn : 0);
    storeAt(i, atr, pdm, mdm);
  }

  // Wilder-smooth ADX from DX buffer
  if (dxBuf.length >= period) {
    let av = dxBuf.slice(0, period).reduce((a, b) => a + b, 0) / period;
    if (period * 2 < n) adx[period * 2] = av;
    for (let i = period; i < dxBuf.length; i++) {
      av = (av * (period - 1) + dxBuf[i]) / period;
      if (period + i + 1 < n) adx[period + i + 1] = av;
    }
  }
  return { diP, diM, adx };
}

// ── Trace builders ────────────────────────────────────────────────────────────

// 1. EMA overlay on price panel
function buildEMATrace(dates, closes, period = 20) {
  return {
    type: 'scatter', mode: 'lines',
    x: dates, y: calcEMA(closes, period),
    name: `EMA${period}`,
    line: { color: TOS_THEME.ma, width: 1.5 },
    yaxis: 'y', xaxis: 'x',
    hovertemplate: `EMA${period}: $%{y:.2f}<extra></extra>`,
  };
}

// 2. Volume bars overlaid at the base of the price panel (via y2)
function buildVolumeTrace(dates, volumes) {
  return {
    type: 'bar',
    x: dates, y: volumes,
    name: 'Vol',
    yaxis: 'y2', xaxis: 'x',
    marker: { color: TOS_THEME.vol },
    hovertemplate: 'Vol: %{y:,.0f}<extra></extra>',
  };
}

// 3. ADX pane — single strength line
function buildADXTraces(dates, highs, lows, closes, period = 14) {
  const { adx } = calcDMI(highs, lows, closes, period);
  return [{
    type: 'scatter', mode: 'lines',
    x: dates, y: adx, name: 'ADX',
    line: { color: TOS_THEME.adx, width: 1.5 },
    yaxis: 'y3', xaxis: 'x',
    hovertemplate: 'ADX: %{y:.1f}<extra></extra>',
  }];
}

// 4. DMI pane — DI+, DI-, ADX dotted reference
function buildDMITraces(dates, highs, lows, closes, period = 14) {
  const { diP, diM, adx } = calcDMI(highs, lows, closes, period);
  return [
    { type:'scatter', mode:'lines', x:dates, y:diP, name:'DI+',
      line:{color:TOS_THEME.diPlus,  width:1.5}, yaxis:'y4', xaxis:'x',
      hovertemplate:'DI+: %{y:.1f}<extra></extra>' },
    { type:'scatter', mode:'lines', x:dates, y:diM, name:'DI-',
      line:{color:TOS_THEME.diMinus, width:1.5}, yaxis:'y4', xaxis:'x',
      hovertemplate:'DI-: %{y:.1f}<extra></extra>' },
    { type:'scatter', mode:'lines', x:dates, y:adx, name:'ADX',
      line:{color:TOS_THEME.adx, width:1, dash:'dot'}, yaxis:'y4', xaxis:'x',
      hoverinfo:'skip' },
  ];
}

// 5. RSI pane
function buildRSITraces(dates, closes, period = 14) {
  return [{
    type: 'scatter', mode: 'lines',
    x: dates, y: calcRSI(closes, period),
    name: 'RSI',
    line: { color: TOS_THEME.rsi, width: 1.5 },
    yaxis: 'y5', xaxis: 'x',
    hovertemplate: 'RSI: %{y:.1f}<extra></extra>',
  }];
}

function buildRSIShapes() {
  return [
    [70, 'rgba(239,68,68,0.35)'],
    [50, 'rgba(255,255,255,0.08)'],
    [30, 'rgba(34,197,94,0.35)'],
  ].map(([v, col]) => ({
    type:'line', xref:'paper', x0:0, x1:1,
    yref:'y5', y0:v, y1:v,
    line:{ color:col, width:1, dash:'dot' },
  }));
}

// ── Layout factory ────────────────────────────────────────────────────────────

function buildLayout(maxVol, extraShapes = [], extraAnnotations = []) {
  const C = TOS_THEME, D = TOS_PANE;
  const ax = {
    gridcolor: C.grid, showgrid: true,
    tickfont: { size:9, color:C.text },
    zeroline: false, showline: false, tickcolor: C.text,
  };
  return {
    paper_bgcolor: C.bg, plot_bgcolor: C.bg,
    margin: { l:0, r:54, t:4, b:20, pad:0 },
    font: { color:C.text, size:9 },
    showlegend: false,
    hovermode: 'x',
    dragmode: 'pan',
    xaxis:  { ...ax, domain:[0,1], rangeslider:{visible:false} },
    yaxis:  { ...ax, domain:D.price, side:'right', tickprefix:'$' },
    yaxis2: { overlaying:'y', side:'right', showgrid:false, fixedrange:true,
               range:[0, maxVol * 5], showticklabels:false, zeroline:false },
    yaxis3: { ...ax, domain:D.adx, side:'right', tickfont:{size:8, color:C.text} },
    yaxis4: { ...ax, domain:D.dmi, side:'right', tickfont:{size:8, color:C.text} },
    yaxis5: { ...ax, domain:D.rsi, side:'right', range:[0,100],
               tickvals:[30,70], tickfont:{size:8, color:C.text} },
    shapes:      [...buildRSIShapes(), ...extraShapes],
    annotations: extraAnnotations,
  };
}

// ── Plotly interaction config (TOS model) ─────────────────────────────────────
// - Desktop: drag pans, scroll/trackpad zooms, zoom in/out buttons
// - Mobile:  one-finger drag pans, pinch zooms (Plotly native touch handling)
const TOS_CONFIG = {
  responsive:     true,
  scrollZoom:     true,   // desktop scroll / trackpad
  displayModeBar: false,  // no toolbar — pinch handles zoom, drag handles pan
};

// ── TOSChart class ────────────────────────────────────────────────────────────

class TOSChart {
  /**
   * @param {string} divId  — id of the container <div>
   */
  constructor(divId) {
    this.divId  = divId;
    this._peers = [];
  }

  /**
   * Render the full chart.
   * @param {object} data  — { dates, open, high, low, close, volume,
   *                           orb_high?, orb_low? }
   */
  render(data) {
    const { dates, open:opens, high:highs, low:lows, close:closes, volume:volumes } = data;
    const maxVol = Math.max(...(volumes || []).filter(v => v != null), 1);

    const traces = [
      { type:'candlestick', x:dates, open:opens, high:highs, low:lows, close:closes,
        increasing:{ line:{color:TOS_THEME.candleUp,   width:1}, fillcolor:TOS_THEME.candleUp   },
        decreasing:{ line:{color:TOS_THEME.candleDown,  width:1}, fillcolor:TOS_THEME.candleDown  },
        name:'Price', yaxis:'y', xaxis:'x', whiskerwidth:0.25, hoverinfo:'x+y' },
      buildEMATrace(dates, closes, 20),
      buildVolumeTrace(dates, volumes),
      ...buildADXTraces(dates, highs, lows, closes),
      ...buildDMITraces(dates, highs, lows, closes),
      ...buildRSITraces(dates, closes),
    ];

    const shapes = [], annotations = [];
    this._addORBLines(data, shapes, annotations);
    this._addReadouts(highs, lows, closes, annotations);

    Plotly.newPlot(this.divId, traces, buildLayout(maxVol, shapes, annotations), TOS_CONFIG);

    document.getElementById(this.divId)
      .on('plotly_relayout', e => this._onRelayout(e));
  }

  /**
   * Link this chart's x-axis to one or more peer TOSChart instances.
   * @param {TOSChart[]} peers
   */
  sync(peers) {
    this._peers = peers.filter(p => p !== this);
  }

  // ── Private ────────────────────────────────────────────────────────────────

  _addORBLines(data, shapes, annotations) {
    const C = TOS_THEME;
    if (data.orb_high) {
      shapes.push({ type:'line', xref:'paper', x0:0, x1:1,
        yref:'y', y0:data.orb_high, y1:data.orb_high,
        line:{ color:C.orb, width:2, dash:'dot' } });
      annotations.push({ xref:'paper', x:1.01, yref:'y', y:data.orb_high,
        text:'ORB H', showarrow:false, font:{size:8, color:'#fbbf24'}, xanchor:'left' });
    }
    if (data.orb_low) {
      shapes.push({ type:'line', xref:'paper', x0:0, x1:1,
        yref:'y', y0:data.orb_low, y1:data.orb_low,
        line:{ color:'rgba(251,191,36,0.6)', width:2, dash:'dot' } });
      annotations.push({ xref:'paper', x:1.01, yref:'y', y:data.orb_low,
        text:'ORB L', showarrow:false, font:{size:8, color:'#fbbf24'}, xanchor:'left' });
    }
  }

  _addReadouts(highs, lows, closes, annotations) {
    const C = TOS_THEME, D = TOS_PANE;
    const { diP, diM, adx } = calcDMI(highs, lows, closes, 14);
    const rsi = calcRSI(closes, 14);
    const last = arr => [...arr].reverse().find(v => v != null);
    const fmt  = v => v == null ? '—' : v.toFixed(1);
    const lAdx = last(adx), lDiP = last(diP), lDiM = last(diM), lRsi = last(rsi);
    annotations.push(
      { xref:'paper', yref:'paper', x:0.01, y:D.adx[1]-0.002,
        text:`<span style="color:${C.adx}">ADX ${fmt(lAdx)}</span>`,
        showarrow:false, font:{size:9}, xanchor:'left', yanchor:'top' },
      { xref:'paper', yref:'paper', x:0.01, y:D.dmi[1]-0.002,
        text:`<span style="color:${C.diPlus}">DI+ ${fmt(lDiP)}</span>  <span style="color:${C.diMinus}">DI- ${fmt(lDiM)}</span>  <span style="color:${C.adx}">ADX ${fmt(lAdx)}</span>`,
        showarrow:false, font:{size:9}, xanchor:'left', yanchor:'top' },
      { xref:'paper', yref:'paper', x:0.01, y:D.rsi[1]-0.002,
        text:`<span style="color:${C.rsi}">RSI ${fmt(lRsi)}</span>  <span style="color:#6b7280;font-size:8px">OB:70  OS:30</span>`,
        showarrow:false, font:{size:9}, xanchor:'left', yanchor:'top' },
    );
  }

  _onRelayout(event) {
    const xUpdate = {};
    if (event['xaxis.range[0]'] !== undefined) {
      xUpdate['xaxis.range[0]'] = event['xaxis.range[0]'];
      xUpdate['xaxis.range[1]'] = event['xaxis.range[1]'];
    } else if (event['xaxis.autorange']) {
      xUpdate['xaxis.autorange'] = true;
    } else {
      return;
    }
    this._peers.forEach(peer => Plotly.relayout(peer.divId, xUpdate));
  }
}
