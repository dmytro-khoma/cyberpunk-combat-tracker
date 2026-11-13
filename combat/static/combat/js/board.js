// Battle map. Only the server rolls dice: this file sends an action
// ({type, actor, target, ...}) and gets the new scene state back.
// Animations are cosmetic: they are drawn from the difference between the old and new state.

import { createFx } from './fx.js';

const root = document.querySelector('[data-board]');

if (root) {
  const $ = (sel) => root.querySelector(sel);
  const ui = {
    map: $('[data-map]'),
    fxLayer: $('[data-fx]'),
    turn: $('[data-turn]'),
    controls: $('[data-controls]'),
    modes: $('[data-modes]'),
    opts: $('[data-opts]'),
    buttons: $('[data-buttons]'),
    status: $('[data-status]'),
    order: $('[data-order]'),
    log: $('[data-log]'),
    start: $('[data-do="start"]'),
    reset: $('[data-do="reset"]'),
    wallToggle: $('[data-wall-toggle]'),
    wallHint: $('[data-wall-hint]'),
    wallClear: $('[data-wall-clear]'),
    round: document.querySelector('[data-round]'),
  };

  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  let state = JSON.parse(document.getElementById('initial-state').textContent);
  let walls = new Set(state.walls.map(([x, y]) => `${x},${y}`));
  let focusKey = '0,0';
  let busy = false;
  let mode = 'fight';
  let modeOwner = null;
  let wallMode = false;
  const opt = { aim: 'body', luck: 0, overclock: false, stealth: false, grenade: '', hack: 'short' };
  const fx = createFx(ui.map, ui.fxLayer);

  const el = (tag, props = {}, ...kids) => {
    const node = Object.assign(document.createElement(tag), props);
    node.append(...kids.filter((k) => k !== null && k !== undefined && k !== false));
    return node;
  };
  const key = (x, y) => `${x},${y}`;
  const dist = (a, b) => Math.max(Math.abs(a.x - b.x), Math.abs(a.y - b.y));
  const me = () => state.combatants.find((c) => c.id === state.activeId) ?? null;
  const at = (x, y) => state.combatants.find((c) => c.x === x && c.y === y && c.condition !== 'dead');
  const up = (c) => c.condition === 'ok';
  const isWall = (x, y) => walls.has(key(x, y));

  const say = (text, error = false) => {
    ui.status.textContent = text;
    ui.status.classList.toggle('status--error', error);
  };

  // --- geometry: the same as rules.line_cells and rules.path_steps ------

  function wallsBetween(a, b) {
    const steps = Math.max(Math.abs(b.x - a.x), Math.abs(b.y - a.y)) * 4;
    const hit = new Set();
    for (let i = 1; i < steps; i += 1) {
      const x = Math.floor(a.x + ((b.x - a.x) * i) / steps + 0.5);
      const y = Math.floor(a.y + ((b.y - a.y) * i) / steps + 0.5);
      if (isWall(x, y) && !(x === a.x && y === a.y) && !(x === b.x && y === b.y)) hit.add(key(x, y));
    }
    return hit.size;
  }

  function reachable(c) {
    const budget = Math.floor(c.moveLeft / 2);
    const seen = new Map([[key(c.x, c.y), 0]]);
    let frontier = [[c.x, c.y]];
    while (frontier.length) {
      const next = [];
      for (const [x, y] of frontier) {
        const d = seen.get(key(x, y));
        if (d >= budget) continue;
        for (let dx = -1; dx <= 1; dx += 1) {
          for (let dy = -1; dy <= 1; dy += 1) {
            if (!dx && !dy) continue;
            const nx = x + dx;
            const ny = y + dy;
            if (nx < 0 || ny < 0 || nx >= state.width || ny >= state.height) continue;
            if (seen.has(key(nx, ny)) || isWall(nx, ny)) continue;
            if (dx && dy && isWall(x + dx, y) && isWall(x, y + dy)) continue;
            seen.set(key(nx, ny), d + 1);
            next.push([nx, ny]);
          }
        }
      }
      frontier = next;
    }
    return seen;
  }

  // --- what the active fighter can do -----------------------------------

  const MODES = [
    ['fight', 'Attack / move', () => true],
    ['grenade', 'Grenade', (c) => Object.values(c.grenades).some((n) => n > 0)],
    ['hack', 'Quickhack', (c) => c.ramMax > 0],
    ['lock', 'Lock on', (c) => !c.melee && c.weaponKind === 'smart'],
    ['medkit', 'Medkit', (c) => c.medkits > 0],
    ['stabilize', 'Stabilise', () => true],
    ['bleed', 'Stop bleeding', () => true],
    ['drain', 'Take RAM', (c) => c.ramMax > 0],
  ];

  function cellRole(c, x, y, f, reach) {
    if (!c || isWall(x, y)) return '';
    const near = (t) => dist(c, t) <= 1;
    switch (mode) {
      case 'fight': {
        if (!f) return reach.has(key(x, y)) && !c.statuses.some((s) => s.startsWith('immobilised')) ? 'reach' : '';
        if (f.side === c.side || c.hasActed || dist(c, f) > c.reachSquares) return '';
        const pierce = !c.melee && c.weaponKind === 'tech' && c.charged ? 1 : 0;
        return wallsBetween(c, f) <= pierce ? 'target' : '';
      }
      case 'grenade':
        return !c.hasActed && dist(c, { x, y }) <= 10 ? 'zone' : '';
      case 'hack':
        return f && f.id !== c.id && !c.hasActed ? 'target' : '';
      case 'lock':
        return f && f.side !== c.side && !c.hasActed ? 'target' : '';
      case 'medkit':
        return f && !c.hasActed && near(f) && (f.hp < f.hpMax || f.bleeding) ? 'target' : '';
      case 'stabilize':
        return f && f.id !== c.id && !c.hasActed && near(f) && f.condition === 'down' ? 'target' : '';
      case 'bleed':
        return f && !c.hasActed && near(f) && f.bleeding ? 'target' : '';
      case 'drain':
        return f && f.id !== c.id && !c.hasActed && near(f) && !up(f) ? 'target' : '';
      default:
        return '';
    }
  }

  // --- server -------------------------------------------------------------

  async function send(type, extra = {}) {
    if (busy) return;
    busy = true;
    root.setAttribute('aria-busy', 'true');
    const before = state;
    try {
      const res = await fetch(root.dataset.actionUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
        body: JSON.stringify({ type, actor: state.activeId, ...extra }),
        credentials: 'same-origin',
      });
      const data = await res.json();
      if (!res.ok) {
        say(data.error ?? 'The server returned an error.', true);
        if (type === 'walls') syncWalls();
        return;
      }
      if (type === 'start' || type === 'reset') {
        window.location.reload();
        return;
      }
      state = data;
      syncWalls();
      say('');
      render(before.log[0]?.id ?? 0);
      fx.play(type, extra, before, state);
    } catch {
      say('No connection to the server.', true);
    } finally {
      busy = false;
      root.removeAttribute('aria-busy');
    }
  }

  function syncWalls() {
    walls = new Set(state.walls.map(([x, y]) => key(x, y)));
    renderMap();
  }

  // --- rendering -------------------------------------------------------------

  function render(lastSeen = Infinity) {
    const c = me();
    if (c?.id !== modeOwner) {
      modeOwner = c?.id ?? null;
      mode = 'fight';
      Object.assign(opt, { aim: 'body', luck: 0, overclock: false, stealth: false });
    }
    renderMap();
    renderTurn();
    renderOrder();
    renderLog(lastSeen);
    ui.round.innerHTML = state.started ? `Round <b>${state.round}</b>` : 'Setup';
  }

  function renderMap() {
    const c = me();
    const reach = c && mode === 'fight' && !wallMode ? reachable(c) : new Map();
    const hadFocus = ui.map.contains(document.activeElement);
    ui.map.style.setProperty('--cols', state.width);
    ui.map.classList.toggle('map--walls', wallMode);
    const cells = [];

    for (let y = 0; y < state.height; y += 1) {
      for (let x = 0; x < state.width; x += 1) {
        const f = at(x, y) ?? state.combatants.find((p) => p.x === x && p.y === y);
        const cell = el('button', { type: 'button', className: 'cell', tabIndex: key(x, y) === focusKey ? 0 : -1 });
        cell.dataset.x = x;
        cell.dataset.y = y;
        let label = `${x}, ${y}`;

        if (isWall(x, y)) {
          cell.classList.add('cell--wall');
          label += ': wall';
        }

        const role = state.started && !wallMode ? cellRole(c, x, y, f && f.condition !== 'dead' ? f : null, reach) : '';
        if (role) cell.classList.add(`cell--${role}`);

        if (f) {
          cell.classList.add('cell--fighter', `cell--${f.side}`, `cell--${f.tier}`, `cell--${f.condition === 'stable' ? 'down' : f.condition}`);
          cell.dataset.id = f.id;
          if (f.cover) cell.append(el('span', { className: `cov cov--${f.cover}` }));
          if (f.id === state.activeId) cell.classList.add('cell--active');
          const bar = el('span', { className: 'hpbar' });
          bar.style.setProperty('--hp', `${Math.max(0, f.hp / f.hpMax) * 100}%`);
          cell.append(el('span', { className: 'token' }, f.name.slice(0, 2).toUpperCase()), bar);
          label += `: ${f.name}, ${f.side === 'crew' ? 'crew' : 'hostile'}, ${f.hp} of ${f.hpMax} HP`;
          if (f.condition === 'dead') label += ', dead';
          else if (f.condition !== 'ok') label += ', unconscious';
        }
        if (wallMode) label += isWall(x, y) ? '. Press Enter to remove the wall' : '. Press Enter to build a wall';
        else if (role === 'reach') label += '. Press Enter to move here';
        else if (role) label += '. Press Enter to act';
        cell.setAttribute('aria-label', label);
        cells.push(cell);
      }
    }
    ui.map.replaceChildren(...cells);
    fx.resize();
    if (hadFocus) ui.map.querySelector('[tabindex="0"]')?.focus();
  }

  function fact(term, value, wide = false) {
    return el('div', { className: wide ? 'wide' : '' }, el('dt', {}, term), el('dd', {}, value));
  }

  function renderTurn() {
    const c = me();
    ui.start.hidden = state.started;
    ui.reset.hidden = !state.started;
    ui.controls.hidden = !c;

    if (state.winner) {
      ui.turn.replaceChildren(el('p', { className: 'winner' }, state.winnerText));
      return;
    }
    if (!c) {
      ui.turn.replaceChildren(el('p', { className: 'muted' },
        state.started ? '' : 'Place the fighters and walls, then start the fight. Initiative goes by REF; a d6 breaks ties.'));
      return;
    }

    const weapon = c.melee ? c.weapon : `${c.weapon} ${c.ammo}/${c.magazine}`;
    const facts = el('dl', { className: 'facts' },
      fact('HP', `${c.hp}/${c.hpMax}`),
      fact('SP', c.spHead !== c.sp ? `${c.sp} · head ${c.spHead}` : `${c.sp}`),
      fact('Move', `${c.moveLeft} m`),
      fact('Action', c.hasActed ? 'used' : 'ready'),
      fact('Weapon', weapon, true),
      c.ramMax ? fact('RAM', `${c.ram}/${c.ramMax}`) : fact('Medkits', `${c.medkits}`),
      fact('Luck', `${c.luck}`),
    );

    const flags = c.statuses.map((s) => [s, true]);
    if (c.jammed) flags.push(['jammed', true]);
    if (c.charged) flags.push(['charged', false]);
    if (c.lockTarget) {
      const t = state.combatants.find((p) => p.id === c.lockTarget);
      flags.push([`locked on: ${t?.name ?? '?'}${c.lockReady ? '' : ' (from next turn)'}`, false]);
    }
    if (c.dodgeUsed) flags.push(['dodge used this round', false]);

    ui.turn.replaceChildren(
      el('div', { className: 'card-head' },
        el('strong', {}, c.name),
        el('span', {}, [c.role, c.tier !== 'regular' ? c.tierLabel : ''].filter(Boolean).join(' · '))),
      facts,
    );
    if (flags.length) {
      ui.turn.append(el('div', { className: 'flags' },
        ...flags.map(([t, bad]) => el('span', { className: bad ? 'flag flag--bad' : 'flag' }, t))));
    }

    renderModes(c);
    renderOpts(c);
    renderButtons(c);
  }

  function renderModes(c) {
    if (!MODES.some(([k, , ok]) => k === mode && ok(c))) mode = 'fight';
    ui.modes.replaceChildren(...MODES.filter(([, , ok]) => ok(c)).map(([k, label]) => {
      const input = el('input', { type: 'radio', name: 'mode', value: k, checked: k === mode });
      input.addEventListener('change', () => { mode = k; renderMap(); renderOpts(c); });
      return el('label', {}, input, el('span', {}, label));
    }));
  }

  function select(label, name, options) {
    const s = el('select', { className: 'inline', id: `opt-${name}` });
    for (const [value, text] of options) s.append(el('option', { value, textContent: text, selected: String(opt[name]) === String(value) }));
    s.addEventListener('change', () => { opt[name] = s.value; });
    return el('div', { className: 'field' }, el('label', { htmlFor: s.id }, label), s);
  }

  function checkbox(label, name) {
    const box = el('input', { type: 'checkbox', checked: opt[name] });
    box.addEventListener('change', () => { opt[name] = box.checked; });
    return el('label', { className: 'check' }, box, label);
  }

  function renderOpts(c) {
    const parts = [];
    const luck = Array.from({ length: Math.min(3, c.luck) + 1 }, (_, i) => [i, i ? `+${i}` : 'don\'t spend']);
    if (opt.luck > c.luck) opt.luck = 0;

    if (mode === 'fight') {
      parts.push(select('Aim at', 'aim', [['body', 'body'], ['head', 'head (−3)'], ['arm', 'arm (−3)'], ['leg', 'leg (−3)']]));
      parts.push(select('Luck', 'luck', luck));
      if (c.overclock && !c.melee) parts.push(checkbox('Overclock: TECH against the model DV, success gives two attacks', 'overclock'));
      if (c.stealthWeapon) parts.push(checkbox('Strike from stealth', 'stealth'));
    } else if (mode === 'grenade') {
      const have = Object.entries(c.grenades).filter(([, n]) => n > 0);
      if (!have.some(([k]) => k === opt.grenade)) opt.grenade = have[0]?.[0] ?? '';
      parts.push(select('Grenade', 'grenade', have.map(([k, n]) => [k, `${state.grenadeNames[k]} ×${n}`])));
      parts.push(select('Luck', 'luck', luck));
    } else if (mode === 'hack') {
      parts.push(select('Quickhack', 'hack', state.hacks.map((h) => [h.key, `${h.name} · ${h.ram} RAM`])));
      parts.push(select('Luck', 'luck', luck));
    }
    ui.opts.replaceChildren(...parts);
    ui.opts.hidden = !parts.length;
  }

  function renderButtons(c) {
    const btn = (text, type, cls = 'btn', disabled = false) => {
      const b = el('button', { type: 'button', className: cls, textContent: text, disabled });
      b.addEventListener('click', () => send(type));
      return b;
    };
    const list = [];
    if (!c.melee) list.push(btn('Reload', 'reload', 'btn', c.hasActed || c.ammo >= c.magazine));
    if (c.jammed) list.push(btn('Clear jam', 'unjam', 'btn btn--red', c.hasActed));
    if (!c.melee && c.weaponKind === 'tech') list.push(btn('Charge', 'charge', 'btn', c.hasActed || c.charged));
    list.push(btn('End turn', 'end_turn', 'btn btn--solid'));
    ui.buttons.replaceChildren(...list);
  }

  function renderOrder() {
    ui.order.replaceChildren(...state.combatants.map((c) => {
      const cover = el('select', { ariaLabel: `${c.name}'s cover` });
      for (const [v, t] of [['', '—'], ['partial', 'partial'], ['full', 'full']]) {
        cover.append(el('option', { value: v, textContent: t, selected: c.cover === v }));
      }
      cover.addEventListener('change', () => send('cover', { target: c.id, cover: cover.value }));

      const name = el('th', { scope: 'row', className: c.condition === 'ok' ? '' : 'gone' },
        el('span', { className: `tag tag--${c.side}` }, c.side === 'crew' ? '■ ' : '□ '), c.name);
      if (c.statuses.length) name.append(el('span', { className: 'st' }, c.statuses.join(', ')));

      const tr = el('tr', {},
        name,
        el('td', { className: 'n' }, c.initiative === null ? '—' : `${c.initiative}${c.initRoll ? ` · ${c.initRoll}` : ''}`),
        el('td', { className: 'n' }, `${c.hp}/${c.hpMax}`),
        el('td', { className: 'n' }, `${c.sp}`),
        el('td', {}, cover),
      );
      if (c.id === state.activeId) tr.setAttribute('aria-current', 'step');
      return tr;
    }));
  }

  function renderLog(lastSeen) {
    ui.log.replaceChildren(...state.log.map((e) => {
      const li = el('li', { className: `k-${e.kind}${e.id > lastSeen ? ' fresh' : ''}`, textContent: e.text });
      li.dataset.round = e.round;
      return li;
    }));
  }

  // --- walls ------------------------------------------------------------------

  function setWallMode(on) {
    wallMode = on;
    ui.wallToggle.setAttribute('aria-pressed', String(on));
    ui.wallToggle.textContent = on ? 'Done' : 'Walls';
    ui.wallHint.hidden = !on;
    ui.wallClear.hidden = !on;
    say(on ? 'Wall mode: click or drag to build walls, drag over a wall to remove it. This does not use the turn.' : '');
    renderMap();
  }

  ui.wallToggle.addEventListener('click', () => setWallMode(!wallMode));
  ui.wallClear.addEventListener('click', () => {
    if (window.confirm('Remove all walls from the map?')) send('clear_walls');
  });

  let paint = null;   // {build, cells: Set, last: [x, y]}

  function paintOne(x, y) {
    const k = key(x, y);
    const cell = ui.map.querySelector(`[data-x="${x}"][data-y="${y}"]`);
    if (!cell || paint.cells.has(k)) return;
    if (paint.build && cell.dataset.id) return;     // no wall on top of a fighter
    paint.cells.add(k);
    cell.classList.toggle('cell--wall', paint.build);
  }

  function paintCell(cell) {
    if (!paint || !cell) return;
    const x = Number(cell.dataset.x);
    const y = Number(cell.dataset.y);
    // A fast drag skips squares, so fill in the line between them.
    const [lx, ly] = paint.last ?? [x, y];
    const steps = Math.max(Math.abs(x - lx), Math.abs(y - ly), 1);
    for (let i = 1; i <= steps; i += 1) {
      paintOne(Math.round(lx + ((x - lx) * i) / steps), Math.round(ly + ((y - ly) * i) / steps));
    }
    paint.last = [x, y];
  }

  ui.map.addEventListener('pointerdown', (event) => {
    if (!wallMode || event.button !== 0) return;
    const cell = event.target.closest('.cell');
    if (!cell) return;
    event.preventDefault();
    paint = { build: !isWall(Number(cell.dataset.x), Number(cell.dataset.y)), cells: new Set(), last: null };
    paintCell(cell);
  });

  window.addEventListener('pointermove', (event) => {
    if (!paint) return;
    paintCell(document.elementFromPoint(event.clientX, event.clientY)?.closest('.cell'));
  });

  window.addEventListener('pointerup', () => {
    if (!paint) return;
    const { build, cells } = paint;
    paint = null;
    for (const k of cells) (build ? walls.add(k) : walls.delete(k));
    if (cells.size) send('walls', { build, cells: [...cells].map((k) => k.split(',').map(Number)) });
  });

  // --- input ------------------------------------------------------------------

  function useCell(x, y) {
    const c = me();
    if (!c) {
      say(state.started ? 'The fight is over.' : 'Start the fight first. You can place walls with the Walls button.', true);
      return;
    }
    const f = at(x, y);

    if (mode === 'grenade') {
      send('grenade', { x, y, grenade: opt.grenade, luck: Number(opt.luck) });
      return;
    }
    if (mode === 'fight') {
      if (!f) send('move', { x, y });
      else if (f.id === c.id) say('That is the active fighter.');
      else if (f.side === c.side) say(`${f.name} is on your side. To help them, pick Medkit or Stabilise.`, true);
      else {
        send('attack', { target: f.id, aim: opt.aim, luck: Number(opt.luck), overclock: opt.overclock, stealth: opt.stealth });
        opt.overclock = false;
      }
      return;
    }
    if (!f) {
      say('Nobody is here.', true);
      return;
    }
    const type = { hack: 'hack', lock: 'lock', medkit: 'medkit', stabilize: 'stabilize', bleed: 'stop_bleeding', drain: 'drain' }[mode];
    send(type, { target: f.id, hack: opt.hack, luck: Number(opt.luck) });
  }

  ui.map.addEventListener('click', (event) => {
    const cell = event.target.closest('.cell');
    if (!cell) return;
    const x = Number(cell.dataset.x);
    const y = Number(cell.dataset.y);
    focusKey = key(x, y);
    if (wallMode) {
      // The mouse paints through pointer events; only keyboard Enter ends up here.
      if (event.detail === 0) send('walls', { build: !isWall(x, y), cells: [[x, y]] });
      return;
    }
    useCell(x, y);
  });

  ui.map.addEventListener('keydown', (event) => {
    const step = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[event.key];
    if (!step) return;
    event.preventDefault();
    const [x, y] = focusKey.split(',').map(Number);
    const nx = Math.min(Math.max(x + step[0], 0), state.width - 1);
    const ny = Math.min(Math.max(y + step[1], 0), state.height - 1);
    ui.map.querySelector(`[data-x="${x}"][data-y="${y}"]`).tabIndex = -1;
    const next = ui.map.querySelector(`[data-x="${nx}"][data-y="${ny}"]`);
    next.tabIndex = 0;
    next.focus();
    focusKey = key(nx, ny);
  });

  ui.start.addEventListener('click', () => send('start'));
  ui.reset.addEventListener('click', () => {
    if (window.confirm('Reset the fight? HP, ammo and RAM are restored, half of the lost SP comes back and the log is cleared. Walls stay.')) send('reset');
  });

  const first = me();
  if (first) focusKey = key(first.x, first.y);
  render();
}
