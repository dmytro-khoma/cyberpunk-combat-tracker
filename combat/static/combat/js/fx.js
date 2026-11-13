// Эффекты поверх карты: трассеры, удары, взрывы, взлом, всплывающий урон.
// Ничего не решают — только показывают то, что уже посчитал сервер.

const NS = 'http://www.w3.org/2000/svg';
const wait = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

export function createFx(map, svg) {
  const calm = window.matchMedia('(prefers-reduced-motion: reduce)');

  const cellAt = (x, y) => map.querySelector(`[data-x="${x}"][data-y="${y}"]`);
  const center = (p) => {
    const c = p && cellAt(p.x, p.y);
    return c ? { x: c.offsetLeft + c.offsetWidth / 2, y: c.offsetTop + c.offsetHeight / 2, size: c.offsetWidth } : null;
  };

  function resize() {
    const w = map.offsetWidth;
    const h = map.offsetHeight;
    svg.setAttribute('width', w);
    svg.setAttribute('height', h);
    svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  }

  function make(tag, attrs, text) {
    const node = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (text) node.textContent = text;
    svg.append(node);
    return node;
  }

  function run(node, frames, options) {
    const anim = node.animate(frames, { fill: 'forwards', ...options });
    anim.finished.then(() => node.remove(), () => node.remove());
    return anim;
  }

  function label(p, text, cls = 'fx-text', delay = 0) {
    const t = make('text', { x: p.x, y: p.y - p.size * 0.55, class: cls, 'text-anchor': 'middle' }, text);
    run(t, [
      { opacity: 0, transform: 'translateY(4px)' },
      { opacity: 1, transform: 'translateY(-6px)', offset: 0.2 },
      { opacity: 1, transform: 'translateY(-16px)', offset: 0.7 },
      { opacity: 0, transform: 'translateY(-26px)' },
    ], { duration: 1100, delay, easing: 'ease-out' });
  }

  function shake(id, delay = 0) {
    const token = map.querySelector(`[data-id="${id}"] .token`);
    token?.animate([
      { transform: 'translateX(0)' }, { transform: 'translateX(-4px)' }, { transform: 'translateX(4px)' },
      { transform: 'translateX(-2px)' }, { transform: 'translateX(0)' },
    ], { duration: 260, delay });
    const cell = map.querySelector(`[data-id="${id}"]`);
    cell?.animate([{ backgroundColor: 'rgb(227 38 45 / 0.55)' }, { backgroundColor: 'transparent' }], { duration: 420, delay });
  }

  function impact(p) {
    const g = make('g', { class: 'fx-impact' });
    for (let i = 0; i < 6; i += 1) {
      const a = (Math.PI * 2 * i) / 6 + Math.random() * 0.5;
      const r1 = p.size * 0.15;
      const r2 = p.size * 0.5;
      const line = document.createElementNS(NS, 'line');
      line.setAttribute('x1', p.x + Math.cos(a) * r1);
      line.setAttribute('y1', p.y + Math.sin(a) * r1);
      line.setAttribute('x2', p.x + Math.cos(a) * r2);
      line.setAttribute('y2', p.y + Math.sin(a) * r2);
      g.append(line);
    }
    run(g, [{ opacity: 1 }, { opacity: 0 }], { duration: 260, easing: 'ease-out' });
  }

  async function tracer(from, to, hit) {
    let end = to;
    if (!hit) {
      const dx = to.x - from.x;
      const dy = to.y - from.y;
      const len = Math.hypot(dx, dy) || 1;
      const side = (Math.random() < 0.5 ? -1 : 1) * to.size * 0.55;
      end = { x: to.x + (dx / len) * to.size * 1.6 - (dy / len) * side, y: to.y + (dy / len) * to.size * 1.6 + (dx / len) * side };
    }
    const len = Math.hypot(end.x - from.x, end.y - from.y);
    const dash = Math.min(46, len * 0.5);
    const duration = Math.min(320, Math.max(130, len * 0.7));

    const flash = make('circle', { cx: from.x, cy: from.y, r: from.size * 0.32, class: 'fx-flash' });
    run(flash, [{ opacity: 1, transform: 'scale(0.4)' }, { opacity: 0, transform: 'scale(1.5)' }], { duration: 160, easing: 'ease-out' });

    const line = make('line', { x1: from.x, y1: from.y, x2: end.x, y2: end.y, class: 'fx-tracer' });
    line.style.strokeDasharray = `${dash} ${len + dash}`;
    run(line, [{ strokeDashoffset: dash }, { strokeDashoffset: -len }], { duration, easing: 'linear' });

    await wait(duration * 0.85);
    if (hit) impact(to);
  }

  async function slash(to, hit) {
    const s = to.size * 0.42;
    const strokes = [
      [to.x - s, to.y - s, to.x + s, to.y + s],
      [to.x + s, to.y - s * 0.7, to.x - s * 0.8, to.y + s],
    ];
    for (const [x1, y1, x2, y2] of strokes) {
      const len = Math.hypot(x2 - x1, y2 - y1);
      const path = make('path', { d: `M${x1} ${y1} Q${(x1 + x2) / 2 + s * 0.3} ${(y1 + y2) / 2 - s * 0.3} ${x2} ${y2}`, class: hit ? 'fx-slash' : 'fx-slash fx-slash--miss' });
      path.style.strokeDasharray = `${len * 1.2}`;
      run(path, [
        { strokeDashoffset: len * 1.2, opacity: 1 },
        { strokeDashoffset: 0, opacity: 1, offset: 0.45 },
        { strokeDashoffset: 0, opacity: 0 },
      ], { duration: 380, easing: 'ease-out' });
      await wait(90);
    }
  }

  async function lob(from, to) {
    const mx = (from.x + to.x) / 2;
    const my = Math.min(from.y, to.y) - Math.max(30, Math.hypot(to.x - from.x, to.y - from.y) * 0.35);
    const d = `M${from.x} ${from.y} Q${mx} ${my} ${to.x} ${to.y}`;
    const path = make('path', { d, class: 'fx-lob' });
    const len = path.getTotalLength();
    path.style.strokeDasharray = `6 ${len}`;
    run(path, [{ strokeDashoffset: 6 }, { strokeDashoffset: -len }], { duration: 420, easing: 'ease-in' });
    await wait(400);
  }

  function blast(p, radius, kind) {
    const r = (radius + 0.5) * p.size;
    const cls = { flash: 'fx-blast fx-blast--flash', emp: 'fx-blast fx-blast--emp' }[kind] ?? 'fx-blast';
    const core = make('circle', { cx: p.x, cy: p.y, r, class: cls });
    run(core, [
      { opacity: 0.95, transform: 'scale(0.15)' },
      { opacity: 0.6, transform: 'scale(1)', offset: 0.35 },
      { opacity: 0, transform: 'scale(1.08)' },
    ], { duration: 700, easing: 'cubic-bezier(.1,.7,.3,1)' });
    const ring = make('circle', { cx: p.x, cy: p.y, r, class: 'fx-ring' });
    run(ring, [{ opacity: 1, transform: 'scale(0.2)' }, { opacity: 0, transform: 'scale(1.25)' }], { duration: 600, easing: 'ease-out' });
    map.parentElement.animate([
      { transform: 'translate(0,0)' }, { transform: 'translate(-3px,2px)' }, { transform: 'translate(3px,-2px)' },
      { transform: 'translate(-2px,-1px)' }, { transform: 'translate(0,0)' },
    ], { duration: 280 });
  }

  async function hackLine(from, to, ok) {
    const path = make('path', { d: zigzag(from, to), class: 'fx-hack' });
    run(path, [{ strokeDashoffset: 0, opacity: 1 }, { strokeDashoffset: -60, opacity: 1, offset: 0.7 }, { strokeDashoffset: -80, opacity: 0 }], { duration: 650 });
    await wait(300);
    if (ok) {
      const box = make('rect', { x: to.x - to.size / 2, y: to.y - to.size / 2, width: to.size, height: to.size, class: 'fx-glitch' });
      run(box, [
        { opacity: 0.9, transform: 'translateX(0)' }, { opacity: 0.3, transform: 'translateX(3px)' },
        { opacity: 0.8, transform: 'translateX(-3px)' }, { opacity: 0, transform: 'translateX(0)' },
      ], { duration: 420, easing: 'steps(4)' });
    }
  }

  function zigzag(a, b) {
    const n = 7;
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const len = Math.hypot(dx, dy) || 1;
    const nx = -dy / len;
    const ny = dx / len;
    let d = `M${a.x} ${a.y}`;
    for (let i = 1; i < n; i += 1) {
      const k = (i % 2 ? 1 : -1) * Math.min(7, a.size * 0.2);
      d += ` L${a.x + (dx * i) / n + nx * k} ${a.y + (dy * i) / n + ny * k}`;
    }
    return `${d} L${b.x} ${b.y}`;
  }

  function slide(id, fromCell) {
    const cell = map.querySelector(`[data-id="${id}"]`);
    const was = center(fromCell);
    if (!cell || !was) return;
    const dx = was.x - (cell.offsetLeft + cell.offsetWidth / 2);
    const dy = was.y - (cell.offsetTop + cell.offsetHeight / 2);
    cell.querySelector('.token')?.animate(
      [{ transform: `translate(${dx}px, ${dy}px)` }, { transform: 'translate(0, 0)' }],
      { duration: Math.min(420, 120 + Math.hypot(dx, dy) * 1.2), easing: 'cubic-bezier(.2,.8,.2,1)' },
    );
  }

  // Что произошло — по новым строкам журнала и разнице HP.
  async function play(type, extra, before, after) {
    if (calm.matches) return;
    const prev = new Map(before.combatants.map((c) => [c.id, c]));
    const actor = prev.get(before.activeId);
    const fresh = after.log.filter((e) => e.id > (before.log[0]?.id ?? 0)).reverse();
    const has = (start, part) => fresh.some((e) => e.text.startsWith(start) && (!part || e.text.includes(part)));

    if (type === 'move' && actor) slide(actor.id, actor);

    if (type === 'attack' && actor) {
      const target = prev.get(extra.target);
      const a = center(actor);
      const b = center(target);
      if (a && b) {
        let swings = fresh.filter((e) => e.text.startsWith(`${actor.name} →`) && /— (попадание|промах)\.$/.test(e.text));
        if (!swings.length) swings = [{ text: 'промах.' }];
        for (const swing of swings) {
          const hit = swing.text.endsWith('попадание.');
          if (actor.melee && target && Math.max(Math.abs(actor.x - target.x), Math.abs(actor.y - target.y)) <= 1) await slash(b, hit);
          else await tracer(a, b, hit);
          if (!hit) label(b, 'МИМО', 'fx-text fx-text--miss');
          await wait(140);
        }
        if (has(`${target.name} уклоняется`)) label(b, 'УКЛОН', 'fx-text fx-text--miss', 150);
      }
    }

    if (type === 'grenade' && actor && after.fx?.blast) {
      const [x, y, radius, kind] = after.fx.blast;
      const a = center(actor);
      const b = center({ x, y });
      if (a && b) {
        await lob(a, b);
        blast(b, radius, kind);
        await wait(160);
      }
    }

    if (type === 'hack' && actor) {
      const a = center(actor);
      const b = center(prev.get(extra.target));
      if (a && b) await hackLine(a, b, has(`${actor.name}: «`, 'успех'));
    }

    // Урон, лечение, падения.
    for (const now of after.combatants) {
      const was = prev.get(now.id);
      if (!was) continue;
      const p = center(now);
      if (!p) continue;
      const diff = now.hp - was.hp;
      if (diff < 0) {
        label(p, `−${-diff}`, 'fx-text fx-text--dmg');
        shake(now.id);
      } else if (diff > 0) {
        label(p, `+${diff}`, 'fx-text fx-text--heal');
      } else if (type === 'attack' && now.id === extra.target && fresh.some((e) => e.kind === 'hit' && e.text.startsWith('Урон'))) {
        label(p, '0', 'fx-text fx-text--miss');
      }
      if (was.condition === 'ok' && now.condition !== 'ok') {
        label(p, now.condition === 'dead' ? 'УБИТ' : 'НА ЗЕМЛЕ', 'fx-text fx-text--down', 350);
      }
    }
  }

  return { resize, play };
}
