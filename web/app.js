/* Turin by Lime: a tap-through story over MapLibre + deck.gl (UMD globals: maplibregl, deck). */
(() => {
  const S = window.STORY, T = window.TEXT;
  const { MapboxOverlay, TripsLayer, PathLayer, ScatterplotLayer, ColumnLayer } = deck;

  const hex = (h, a = 255) => [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16), a];
  const LIME = [143, 212, 0];
  const KIND = Object.fromEntries(Object.entries(S.kind_colours).map(([k, v]) => [k, hex(v)]));
  Object.assign(KIND, { 'cycle track': hex('#2E7D32'), 'parallel cycleway': hex('#2E7D32'), 'path / track': hex('#B9BCBD'), 'shared lane': hex('#E62E73'), unmatched: [0, 0, 0, 0] });
  const ANGLE = a => (a < 30 ? hex('#E62E73') : a < 60 ? hex('#FF8C42') : hex('#9CA0A1'));
  const FOLLOW_SPEED = 75;   // ride seconds per real second
  const LOOP_SPEED = 220;
  const phone = () => window.matchMedia('(max-width: 760px)').matches;
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  // ── rides on one story clock ─────────────────────────────────────────
  const rides = S.rides.slice().sort((a, b) => a.offset - b.offset);
  const tEnd = r => r.timestamps[r.timestamps.length - 1];
  const T_END = Math.max(...rides.map(tEnd));
  const rideAt = t => rides.find(r => t >= r.timestamps[0] && t <= tEnd(r))
    || rides.reduce((b, r) => (Math.abs(r.timestamps[0] - t) < Math.abs(b.timestamps[0] - t) ? r : b), rides[0]);
  function headAt(t) {
    const r = rideAt(t), ts = r.timestamps;
    if (t <= ts[0]) return r.path[0];
    if (t >= ts[ts.length - 1]) return r.path[r.path.length - 1];
    let lo = 0, hi = ts.length - 1;
    while (hi - lo > 1) { const m = (lo + hi) >> 1; if (ts[m] <= t) lo = m; else hi = m; }
    const f = ts[hi] === ts[lo] ? 0 : (t - ts[lo]) / (ts[hi] - ts[lo]);
    const a = r.path[lo], b = r.path[hi];
    return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f];
  }
  function bearingAt(t) {
    const a = headAt(t - 10), b = headAt(t + 16);
    const dx = (b[0] - a[0]) * Math.cos((a[1] * Math.PI) / 180), dy = b[1] - a[1];
    return dx === 0 && dy === 0 ? null : (Math.atan2(dx, dy) * 180) / Math.PI;
  }
  const clockAt = t => {
    const r = rideAt(t);
    return new Date((r.t0 + (t - r.offset)) * 1000).toLocaleString('en-GB',
      { timeZone: 'Europe/Rome', weekday: 'short', hour: '2-digit', minute: '2-digit' });
  };
  const boundsOf = paths => paths.reduce((b, p) => p.reduce((bb, c) => bb.extend(c), b), new maplibregl.LngLatBounds());

  // ── map ─────────────────────────────────────────────────────────────
  const map = new maplibregl.Map({
    container: 'map', style: 'https://tiles.openfreemap.org/styles/positron',
    bounds: boundsOf(rides.map(r => r.path)), fitBoundsOptions: { padding: 60 },
    pitch: 45, bearing: -15, maxPitch: 72, attributionControl: false,
  });
  map.addControl(new maplibregl.AttributionControl({ compact: true }), 'top-left');
  if (!phone()) map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'bottom-right');
  const overlay = new MapboxOverlay({ interleaved: true, layers: [] });
  map.addControl(overlay);
  map.on('load', () => {
    const firstSymbol = map.getStyle().layers.find(l => l.type === 'symbol');
    if (map.getSource('openmaptiles')) {
      map.addLayer({
        id: 'buildings-3d', type: 'fill-extrusion', source: 'openmaptiles', 'source-layer': 'building', minzoom: 14,
        paint: {
          'fill-extrusion-color': '#dcd8d0', 'fill-extrusion-opacity': 0.8,
          'fill-extrusion-height': ['coalesce', ['get', 'render_height'], 12],
          'fill-extrusion-base': ['coalesce', ['get', 'render_min_height'], 0],
        },
      }, firstSymbol && firstSymbol.id);
    }
    go(0);
    requestAnimationFrame(tick);
  });

  // camera padding: keep the action clear of the card
  const card = document.getElementById('card');
  function pad() {
    const r = card.getBoundingClientRect();
    return phone() ? { top: 70, left: 20, right: 20, bottom: Math.min(window.innerHeight * 0.6, Math.max(40, window.innerHeight - r.top + 10)) }
      : { top: 50, bottom: 50, right: 60, left: r.right + 30 };
  }

  // ── state + layers ──────────────────────────────────────────────────
  const st = { t: 0, target: 0, mode: 'loop', highlight: null, bearing: -15, hold: 0, onArrive: null, focus: null };

  function layers() {
    const whole = ['numbers', 'pedestrian', 'trams', 'outro'].includes(st.highlight);
    const infra = st.highlight === 'pedestrian' ? S.infra.filter(d => d.kind === 'pedestrian street')
      : st.highlight === 'numbers' ? S.infra : [];
    const fixes = rides.flatMap(r => r.fixes || []).filter(f => whole || st.mode === 'loop' || f[2] <= st.t + 1);
    const trams = st.highlight === 'trams';
    return [
      new PathLayer({ id: 'osm-cycle', data: S.osm_cycle, getPath: d => d.path, getColor: [46, 125, 50, 80], getWidth: 2, widthUnits: 'pixels' }),
      new PathLayer({ id: 'trams', data: S.trams, getPath: d => d, getColor: trams ? [18, 19, 21, 230] : [70, 72, 74, 130],
        getWidth: trams ? 2.4 : 1.3, widthUnits: 'pixels', updateTriggers: { getColor: trams, getWidth: trams } }),
      new PathLayer({ id: 'ghost', data: rides, getPath: d => d.path, getColor: [18, 19, 21, 50], getWidth: 3, widthUnits: 'pixels', capRounded: true, jointRounded: true }),
      new PathLayer({ id: 'infra', data: infra, getPath: d => d.path, getColor: d => KIND[d.kind] || [156, 160, 161],
        getWidth: st.highlight === 'pedestrian' ? 9 : 7, widthUnits: 'pixels', capRounded: true, jointRounded: true }),
      new TripsLayer({ id: 'trips', data: rides, getPath: d => d.path, getTimestamps: d => d.timestamps, getColor: LIME,
        getWidth: 6, widthUnits: 'pixels', capRounded: true, jointRounded: true, fadeTrail: !whole,
        trailLength: whole ? T_END + 10 : st.mode === 'loop' ? 500 : 320, currentTime: whole ? T_END : st.t,
        visible: st.highlight !== 'pedestrian' && st.highlight !== 'numbers' }),
      new ScatterplotLayer({ id: 'fixes', data: fixes, getPosition: f => [f[0], f[1]], getRadius: 2.6, radiusUnits: 'pixels',
        getFillColor: [255, 255, 255, 235], stroked: true, getLineColor: [18, 19, 21, 150], getLineWidth: 1, lineWidthUnits: 'pixels' }),
      new ScatterplotLayer({ id: 'tram-x', data: S.tram_crossings.filter(x => trams || (x.t != null && x.t <= st.t && !whole)),
        getPosition: d => [d.lon, d.lat], getFillColor: d => ANGLE(d.angle), getRadius: trams ? 8 : 5, radiusUnits: 'pixels',
        stroked: true, getLineColor: [255, 255, 255], getLineWidth: 1.5, lineWidthUnits: 'pixels', updateTriggers: { getRadius: trams } }),
      new ColumnLayer({ id: 'spots', data: S.spots.filter(s => s.origin !== 'gio'), getPosition: d => [d.lon, d.lat], diskResolution: 12,
        radius: 4, extruded: true, getElevation: d => (st.focus && st.focus.id === d.id ? 40 : 18),
        getFillColor: d => (d.origin === 'stop' ? hex('#068AA1') : hex('#FF8C42', 220)), pickable: true,
        updateTriggers: { getElevation: st.focus && st.focus.id }, onClick: i => i.object && showSpot(i.object) }),
      new ScatterplotLayer({ id: 'moments', data: S.moments || [], getPosition: d => [d.lon, d.lat], getRadius: 9, radiusUnits: 'pixels',
        getFillColor: d => (st.focus && st.focus.id === d.id ? hex('#E62E73') : [255, 255, 255]), stroked: true,
        getLineColor: hex('#E62E73'), getLineWidth: 3, lineWidthUnits: 'pixels', pickable: true,
        updateTriggers: { getFillColor: st.focus && st.focus.id }, onClick: i => i.object && goToMoment(i.object.id) }),
      new ScatterplotLayer({ id: 'radius', data: st.focus && st.mode === 'focus' ? [st.focus] : [], getPosition: d => [d.lon, d.lat],
        getRadius: 30, radiusUnits: 'meters', getFillColor: [230, 46, 115, 16], stroked: true, getLineColor: [230, 46, 115, 170],
        getLineWidth: 1.5, lineWidthUnits: 'pixels' }),
      new ScatterplotLayer({ id: 'head', data: whole || st.mode === 'focus' ? [] : [headAt(st.t)], getPosition: d => d,
        getRadius: 7, radiusUnits: 'pixels', getFillColor: [255, 255, 255], stroked: true, getLineColor: LIME, getLineWidth: 3, lineWidthUnits: 'pixels' }),
    ];
  }

  // ── animation loop ──────────────────────────────────────────────────
  const clockEl = document.getElementById('clock');
  let last = performance.now();
  function tick(now) {
    const dt = Math.min(0.1, (now - last) / 1000);
    last = now;
    if (st.mode === 'loop') st.t = (st.t + dt * LOOP_SPEED) % (T_END + 40);
    else if (st.mode === 'follow' && st.t !== st.target) {
      const step = dt * FOLLOW_SPEED;
      st.t = st.t < st.target ? Math.min(st.target, st.t + step) : Math.max(st.target, st.t - step);
      if (st.t === st.target && st.onArrive) { const f = st.onArrive; st.onArrive = null; f(); }
    }
    if (st.mode === 'follow' && now > st.hold) {
      const b = bearingAt(st.t);
      if (b != null) st.bearing += ((((b - st.bearing) % 360) + 540) % 360 - 180) * Math.min(1, dt * 1.5);
      map.jumpTo({ center: headAt(st.t), bearing: st.bearing, pitch: 58, zoom: phone() ? 15.7 : 16.2, padding: pad() });
    }
    const showClock = st.mode === 'follow' || st.mode === 'focus';
    clockEl.hidden = !showClock;
    if (showClock) clockEl.textContent = clockAt(st.t);
    overlay.setProps({ layers: layers() });
    requestAnimationFrame(tick);
  }

  // ── camera moves ────────────────────────────────────────────────────
  const overview = (paths = rides.map(r => r.path), pitch = 45) =>
    map.fitBounds(boundsOf(paths), { padding: pad(), pitch, bearing: -18, duration: 1600 });
  function focusOn(p, t) {
    st.mode = 'focus';
    const b = t != null ? bearingAt(t) : null;
    map.easeTo({ center: [p.lon, p.lat], zoom: phone() ? 17.2 : 17.6, pitch: 60, bearing: (b ?? st.bearing) + 20, duration: 1800, padding: pad() });
  }
  function followTo(ride, target, then) {
    const fresh = st.t < ride.timestamps[0] - 1 || st.t > target || rideAt(st.t) !== ride || st.mode === 'loop';
    if (fresh) {
      st.t = ride.timestamps[0];
      st.bearing = bearingAt(st.t + 20) ?? st.bearing;
      map.flyTo({ center: headAt(st.t), zoom: phone() ? 15.7 : 16.2, pitch: 58, bearing: st.bearing, duration: 1600, padding: pad() });
      st.hold = performance.now() + 1650;
    }
    st.mode = 'follow';
    st.target = target;
    st.onArrive = then;
    if (st.t === target && then) { st.onArrive = null; then(); }
  }

  // ── steps ───────────────────────────────────────────────────────────
  const stats = S.stats;
  const sum = k => stats.reduce((a, s) => a + (s[k] || 0), 0);
  const limeKm = stats.reduce((a, s) => a + (s.lime_km ?? s.km), 0);
  const limeMin = stats.reduce((a, s) => a + (s.lime_duration ? +s.lime_duration.split(':')[0] + s.lime_duration.split(':')[1] / 60 : s.minutes), 0);
  const media = m => {
    const files = m.media || [];
    const el = f => /\.(mp4|mov)$/i.test(f)
      ? `<video src="photos/${encodeURI(f)}" poster="photos/${encodeURI(f.replace(/\.(mp4|mov)$/i, '.jpg'))}" autoplay muted loop playsinline preload="metadata"></video>`
      : `<img src="photos/${encodeURI(f)}" alt="" loading="lazy">`;
    return files.length ? `<div class="media${files.length > 1 ? ' two' : ''}">${files.map(el).join('')}</div>` : '';
  };

  // ride profile in five groups, ordered from best to worst for a cyclist; 'unmatched' stays out of the bars
  const GROUPS = [
    ['cycleway', ['cycleway', 'cycle track', 'parallel cycleway'], '#2E7D32'],
    ['painted lane', ['painted lane'], '#A5CE39'],
    ['quiet street', ['quiet street', 'path / track'], '#B9BCBD'],
    ['pedestrian street', ['pedestrian street'], '#B1477A'],
    ['mixed traffic', ['mixed traffic', 'shared lane'], '#E62E73'],
  ];
  const grouped = byKindM => {
    const g = GROUPS.map(([name, ks, col]) => ({ name, col, m: ks.reduce((a, k) => a + (byKindM[k] || 0), 0) }));
    const tot = g.reduce((a, x) => a + x.m, 0) || 1;
    return g.map(x => ({ ...x, pct: (100 * x.m) / tot }));
  };
  const barHtml = (g, label = '') => `<div class="row">${label ? `<span class="lbl">${label}</span>` : ''}<div class="bar">${
    g.filter(x => x.pct > 0).map(x => `<span title="${x.name} ${Math.round(x.pct)}%" style="width:${x.pct}%;background:${x.col}">${x.pct >= 12 ? Math.round(x.pct) + '%' : ''}</span>`).join('')}</div></div>`;
  const legendHtml = gs => {
    const present = GROUPS.filter(([name]) => gs.some(g => g.some(x => x.name === name && x.pct >= 0.5)));
    return `<div class="legend">${present.map(([name, , col]) => `<span><i style="background:${col}"></i>${name}</span>`).join('')}</div>`;
  };
  const allKinds = {};
  stats.forEach(s => Object.entries(s.by_kind_m).forEach(([k, v]) => { allKinds[k] = (allKinds[k] || 0) + v; }));
  const gAll = grouped(allKinds);
  const pct = name => Math.round(gAll.find(x => x.name === name).pct);

  const steps = [];
  steps.push({
    html: () => `<div class="kicker">${T.intro.kicker}</div><h1>${T.intro.title}</h1><p>${T.intro.body}</p>
      <div class="chips"><span>${rides.length} rides</span><span>${limeKm.toFixed(1)} km</span><span>${Math.round(limeMin)} min</span></div>
      <p class="hint">${phone() ? 'Swipe or tap Next to ride along.' : 'Press → or Next to ride along.'}</p>`,
    enter: () => { st.mode = 'loop'; st.highlight = null; st.focus = null; overview(); },
  });
  const rideGroups = rides.map(r => grouped((stats.find(x => x.ride_id === r.id) || {}).by_kind_m || {}));
  steps.push({
    html: () => `<div class="kicker">The short version</div><h2>${T.numbers.title}</h2>
      <p class="big"><b style="color:#E62E73">${pct('mixed traffic')}%</b> in mixed traffic, <b style="color:#B1477A">${pct('pedestrian street')}%</b> in pedestrian streets,
      only <b style="color:#2E7D32">${pct('cycleway') + pct('painted lane')}%</b> on a cycleway or painted lane.</p>
      <div class="chart">${rides.map((r, i) => barHtml(rideGroups[i], `Ride ${i + 1}`)).join('')}${barHtml(gAll, '<b>All</b>')}</div>
      ${legendHtml([gAll, ...rideGroups])}
      <div class="nums"><span>distance (Lime)</span><b>${limeKm.toFixed(1)} km</b><span>tram tracks crossed</span><b>${sum('tram_crossings')}, ${sum('tram_crossings_sharp')} under 30°</b>
      <span>stops of half a minute or more</span><b>${sum('stops_20s')}</b></div>
      <p class="src">${T.numbers.note}</p>`,
    enter: () => { st.mode = 'overview'; st.highlight = 'numbers'; st.focus = null; overview(); },
  });
  rides.forEach((r, i) => {
    const s = stats.find(x => x.ride_id === r.id) || {};
    const txt = (T.rides || {})[r.id] || { title: `Ride ${i + 1}`, note: '' };
    const moms = (S.moments || []).filter(m => m.ride_id === r.id && m.t != null).sort((a, b) => a.t - b.t);
    const stop = (S.stops || []).find(x => x.ride_id === r.id && x.dur >= 60);
    steps.push({
      html: () => `<div class="kicker">Ride ${i + 1} · ${esc(clockAt(r.timestamps[0]))}</div><h2>${esc(txt.title)}</h2>
        ${txt.note ? `<p>${esc(txt.note)}</p>` : ''}
        ${stop ? `<p>I stood still for ${Math.round(stop.dur / 60)} minutes${stop.streets ? ' at ' + esc(stop.streets.split(' / ').slice(0, 2).join(' / ')) : ''}.</p>` : ''}
        <p class="osm"><b>${s.lime_km ?? s.km} km</b> in ${s.lime_duration ? esc(s.lime_duration.replace(':', ' min ')) + ' s' : Math.round(s.minutes) + ' min'}</p>
        ${barHtml(grouped(s.by_kind_m))}${legendHtml([grouped(s.by_kind_m)])}`,
      enter: () => { st.highlight = null; st.focus = null; followTo(r, moms.length ? moms[0].t - 15 : tEnd(r), moms.length ? null : () => overview([r.path], 50)); },
    });
    moms.forEach(m => steps.push({
      id: m.id,
      html: () => `<div class="kicker">Ride ${i + 1} · ${esc(m.clock)}</div>${media(m)}<p>${esc(m.caption)}</p>
        ${m.osm ? `<p class="osm"><b>OSM within 30 m:</b> ${esc(m.osm)}</p>` : ''}`,
      enter: () => { st.highlight = null; st.focus = m; followTo(r, m.t, () => focusOn(m, m.t)); },
    }));
  });

  const pedKm = sum('km_pedestrian');
  const byTag = {};
  stats.forEach(s => Object.entries(s.pedestrian_by_bicycle_tag_m || {}).forEach(([k, v]) => { byTag[k] = (byTag[k] || 0) + v; }));
  if (pedKm > 0) steps.push({
    html: () => `<div class="kicker">All three rides</div><h2>${T.pedestrian.title}</h2><p>${T.pedestrian.body}</p>
      <p class="osm"><b>${pedKm.toFixed(1)} km</b> of ${limeKm.toFixed(1)} ran on pedestrian streets (in purple). OSM says bikes are allowed on
      <b>${Math.round((byTag.yes || 0) + (byTag.designated || 0))} m</b> of it and says nothing on <b>${Math.round(byTag.untagged || 0)} m</b>.</p>`,
    enter: () => { st.mode = 'overview'; st.highlight = 'pedestrian'; st.focus = null; overview(); },
  });

  const sharp = S.spots.filter(s => s.origin === 'tram' && s.tram_angle != null).sort((a, b) => a.tram_angle - b.tram_angle).slice(0, 2);
  steps.push({
    html: () => `<div class="kicker">All three rides</div><h2>${T.trams.title}</h2>
      <p>I crossed tram tracks ${sum('tram_crossings')} times${sharp.length ? `, the sharpest at ${sharp.map(s => `${esc(s.streets.split(' / ')[0])} (about ${s.tram_angle}°)`).join(' and ')}` : ''}. ${T.trams.body}</p>
      <div class="legend"><span><i style="background:#E62E73"></i>under 30°</span><span><i style="background:#FF8C42"></i>30–60°</span><span><i style="background:#9CA0A1"></i>60° or more</span></div>`,
    enter: () => { st.mode = 'overview'; st.highlight = 'trams'; st.focus = null; overview(); },
  });

  steps.push({
    html: () => `<h2>${T.outro.title}</h2><p>${T.outro.body}</p>
      <p class="src">${T.outro.credits}</p>${T.outro.repo ? `<p class="src">Code and data: <a href="${T.outro.repo}">${T.outro.repo.replace('https://', '')}</a></p>` : ''}`,
    enter: () => { st.mode = 'overview'; st.highlight = 'outro'; st.focus = null; overview(rides.map(r => r.path), 55); },
  });

  // ── navigation ──────────────────────────────────────────────────────
  const content = document.getElementById('content'), bar = document.getElementById('bar');
  const prev = document.getElementById('prev'), next = document.getElementById('next'), count = document.getElementById('count');
  let cur = -1;
  function render(html) {
    content.classList.remove('enter'); void content.offsetWidth; content.classList.add('enter');
    content.innerHTML = html;
    content.scrollTop = 0;
  }
  function go(i) {
    i = Math.max(0, Math.min(steps.length - 1, i));
    cur = i;
    st.onArrive = null;
    render(steps[i].html());
    bar.style.width = `${(100 * i) / (steps.length - 1)}%`;
    count.textContent = `${i + 1} / ${steps.length}`;
    prev.disabled = i === 0;
    next.textContent = i === steps.length - 1 ? 'Start again' : 'Next →';
    requestAnimationFrame(() => steps[i].enter());
  }
  function goToMoment(id) { const i = steps.findIndex(s => s.id === id); if (i >= 0) go(i); }
  function showSpot(s) {
    st.focus = s;
    render(`<button class="back" type="button">← back to the story</button>
      <div class="kicker">From the data · ${esc(s.streets || '')}</div><h2>${esc(s.title)}</h2>
      <p class="osm"><b>OSM within 30 m:</b> ${esc(s.osm)}</p>`);
    content.querySelector('.back').onclick = () => go(cur);
    focusOn(s, s.t);
  }
  prev.onclick = () => go(cur - 1);
  next.onclick = () => go(cur === steps.length - 1 ? 0 : cur + 1);
  document.addEventListener('keydown', e => {
    if (e.key === 'ArrowRight' || e.key === ' ') { e.preventDefault(); next.click(); }
    if (e.key === 'ArrowLeft') prev.click();
  });
  let x0 = null, y0 = null;
  card.addEventListener('touchstart', e => { x0 = e.touches[0].clientX; y0 = e.touches[0].clientY; }, { passive: true });
  card.addEventListener('touchend', e => {
    if (x0 == null) return;
    const dx = e.changedTouches[0].clientX - x0, dy = e.changedTouches[0].clientY - y0;
    if (Math.abs(dx) > 45 && Math.abs(dx) > 1.5 * Math.abs(dy)) (dx < 0 ? next : prev).click();
    x0 = null;
  }, { passive: true });

  // play: step through on its own, waiting for each ride animation to land
  const playBtn = document.getElementById('play');
  let playing = false, timer = null;
  const busy = () => st.mode === 'follow' && (st.t !== st.target || st.onArrive);
  function schedule() {
    clearTimeout(timer);
    if (!playing) return;
    timer = setTimeout(() => {
      if (!playing) return;
      if (busy()) return schedule();
      if (cur === steps.length - 1) { playing = false; playBtn.classList.remove('on'); playBtn.textContent = '▶'; return; }
      go(cur + 1);
      schedule();
    }, busy() ? 500 : steps[cur].id ? 5500 : 4200);
  }
  playBtn.onclick = () => {
    playing = !playing;
    playBtn.classList.toggle('on', playing);
    playBtn.textContent = playing ? '❚❚' : '▶';
    if (playing) { if (cur === steps.length - 1) go(0); schedule(); } else clearTimeout(timer);
  };
})();
