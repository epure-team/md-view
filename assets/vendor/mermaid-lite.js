/*
 * md-view offline Mermaid-compatible renderer.
 *
 * This tiny local renderer intentionally avoids CDN/runtime network access. It
 * supports the Mermaid diagram forms most useful in Markdown notes: flowchart /
 * graph edge diagrams and sequenceDiagram message charts. To use the full
 * upstream Mermaid implementation instead, place mermaid.min.js next to this
 * file; server.py will prefer it automatically.
 */
(function () {
  'use strict';

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function linesOf(source) {
    return String(source || '')
      .replace(/\r\n?/g, '\n')
      .split('\n')
      .map(function (line) { return line.trim(); })
      .filter(function (line) { return line && line.slice(0, 2) !== '%%'; });
  }

  function nodeToken(raw) {
    var token = String(raw || '').trim().replace(/;$/, '').trim();
    var match = token.match(/^([A-Za-z0-9_:.\/-]+)\s*(?:\[([^\]]+)\]|\(\(([^)]+)\)\)|\(([^)]+)\)|\{([^}]+)\})?$/);
    if (!match) {
      return { id: token.replace(/\s+/g, '_'), label: token, shape: 'rect' };
    }
    var label = match[2] || match[3] || match[4] || match[5] || match[1];
    var shape = match[5] ? 'diamond' : (match[3] ? 'round' : 'rect');
    return { id: match[1], label: label, shape: shape };
  }

  function parseFlowEdge(line) {
    var m = line.match(/^(.+?)\s*-->\|(.+?)\|\s*(.+)$/);
    if (m) return { from: nodeToken(m[1]), to: nodeToken(m[3]), label: m[2] };
    m = line.match(/^(.+?)\s*--\s*(.+?)\s*-->\s*(.+)$/);
    if (m) return { from: nodeToken(m[1]), to: nodeToken(m[3]), label: m[2] };
    m = line.match(/^(.+?)\s*(-->|==>|-\.->|---)\s*(.+)$/);
    if (m) return { from: nodeToken(m[1]), to: nodeToken(m[3]), label: '' };
    m = line.match(/^([A-Za-z0-9_:.\/-]+)\s*(?:\[([^\]]+)\]|\(([^)]+)\)|\{([^}]+)\})\s*$/);
    if (m) return { node: nodeToken(line) };
    return null;
  }

  function addNode(nodes, order, parsed) {
    if (!parsed || !parsed.id) return;
    if (!nodes[parsed.id]) {
      nodes[parsed.id] = { id: parsed.id, label: parsed.label, shape: parsed.shape };
      order.push(parsed.id);
    } else if (parsed.label && parsed.label !== parsed.id) {
      nodes[parsed.id].label = parsed.label;
      nodes[parsed.id].shape = parsed.shape || nodes[parsed.id].shape;
    }
  }

  function layoutRanks(order, edges) {
    var rank = {};
    order.forEach(function (id) { rank[id] = 0; });
    for (var pass = 0; pass < order.length + 2; pass += 1) {
      edges.forEach(function (edge) {
        var next = (rank[edge.from] || 0) + 1;
        if ((rank[edge.to] || 0) < next) rank[edge.to] = next;
      });
    }
    var groups = [];
    order.forEach(function (id) {
      var r = rank[id] || 0;
      if (!groups[r]) groups[r] = [];
      groups[r].push(id);
    });
    return groups.filter(Boolean);
  }

  function flowSvg(source) {
    var lines = linesOf(source);
    var head = lines.shift() || 'graph TD';
    var direction = (head.match(/\b(TD|TB|BT|LR|RL)\b/i) || [null, 'TD'])[1].toUpperCase();
    var horizontal = direction === 'LR' || direction === 'RL';
    var nodes = {};
    var order = [];
    var edges = [];

    lines.forEach(function (line) {
      var parsed = parseFlowEdge(line);
      if (!parsed) return;
      if (parsed.node) {
        addNode(nodes, order, parsed.node);
      } else {
        addNode(nodes, order, parsed.from);
        addNode(nodes, order, parsed.to);
        edges.push({ from: parsed.from.id, to: parsed.to.id, label: parsed.label || '' });
      }
    });

    if (!order.length) throw new Error('no flowchart nodes found');

    var ranks = layoutRanks(order, edges);
    var nodeW = 172, nodeH = 54, gapX = 56, gapY = 76, pad = 28;
    var pos = {};
    var maxRankSize = ranks.reduce(function (acc, group) { return Math.max(acc, group.length); }, 1);
    ranks.forEach(function (group, r) {
      group.forEach(function (id, i) {
        var x = horizontal ? pad + r * (nodeW + gapX) : pad + i * (nodeW + gapX);
        var y = horizontal ? pad + i * (nodeH + gapY) : pad + r * (nodeH + gapY);
        pos[id] = { x: x, y: y };
      });
    });

    var width = horizontal
      ? pad * 2 + ranks.length * nodeW + Math.max(0, ranks.length - 1) * gapX
      : pad * 2 + maxRankSize * nodeW + Math.max(0, maxRankSize - 1) * gapX;
    var height = horizontal
      ? pad * 2 + maxRankSize * nodeH + Math.max(0, maxRankSize - 1) * gapY
      : pad * 2 + ranks.length * nodeH + Math.max(0, ranks.length - 1) * gapY;

    var body = [];
    body.push('<defs><marker id="mdv-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor"/></marker></defs>');

    edges.forEach(function (edge) {
      var a = pos[edge.from], b = pos[edge.to];
      if (!a || !b) return;
      var x1 = horizontal ? a.x + nodeW : a.x + nodeW / 2;
      var y1 = horizontal ? a.y + nodeH / 2 : a.y + nodeH;
      var x2 = horizontal ? b.x : b.x + nodeW / 2;
      var y2 = horizontal ? b.y + nodeH / 2 : b.y;
      body.push('<line x1="' + x1 + '" y1="' + y1 + '" x2="' + x2 + '" y2="' + y2 + '" stroke="currentColor" stroke-width="1.8" marker-end="url(#mdv-arrow)" opacity="0.72"/>');
      if (edge.label) {
        body.push('<text x="' + ((x1 + x2) / 2) + '" y="' + ((y1 + y2) / 2 - 6) + '" text-anchor="middle" font-size="12" fill="currentColor">' + esc(edge.label) + '</text>');
      }
    });

    order.forEach(function (id) {
      var p = pos[id], n = nodes[id];
      if (!p || !n) return;
      if (n.shape === 'diamond') {
        var cx = p.x + nodeW / 2, cy = p.y + nodeH / 2;
        body.push('<polygon points="' + cx + ',' + p.y + ' ' + (p.x + nodeW) + ',' + cy + ' ' + cx + ',' + (p.y + nodeH) + ' ' + p.x + ',' + cy + '" fill="Canvas" stroke="currentColor" stroke-width="1.5"/>');
      } else {
        var rx = n.shape === 'round' ? 24 : 9;
        body.push('<rect x="' + p.x + '" y="' + p.y + '" width="' + nodeW + '" height="' + nodeH + '" rx="' + rx + '" fill="Canvas" stroke="currentColor" stroke-width="1.5"/>');
      }
      body.push('<text x="' + (p.x + nodeW / 2) + '" y="' + (p.y + nodeH / 2 + 5) + '" text-anchor="middle" font-size="14" font-family="system-ui, sans-serif" fill="currentColor">' + esc(n.label) + '</text>');
    });

    return '<svg xmlns="http://www.w3.org/2000/svg" role="img" viewBox="0 0 ' + width + ' ' + height + '" width="' + width + '" height="' + height + '">' + body.join('') + '</svg>';
  }

  function sequenceSvg(source) {
    var lines = linesOf(source);
    lines.shift();
    var participants = [];
    var labels = {};
    var messages = [];

    function ensure(id) {
      if (participants.indexOf(id) === -1) participants.push(id);
      if (!labels[id]) labels[id] = id;
    }

    lines.forEach(function (line) {
      var p = line.match(/^participant\s+([A-Za-z0-9_:.\/-]+)(?:\s+as\s+(.+))?$/i);
      if (p) {
        ensure(p[1]);
        if (p[2]) labels[p[1]] = p[2];
        return;
      }
      var m = line.match(/^([A-Za-z0-9_:.\/-]+)\s*(?:-+>>?|-->>|->>|->|-->|-x|--x)\s*([A-Za-z0-9_:.\/-]+)\s*:\s*(.*)$/);
      if (m) {
        ensure(m[1]); ensure(m[2]);
        messages.push({ from: m[1], to: m[2], label: m[3] });
      }
    });

    if (!participants.length) throw new Error('no sequence participants found');

    var gap = 170, pad = 42, top = 54, row = 58;
    var width = pad * 2 + Math.max(1, participants.length - 1) * gap;
    var height = top + 44 + Math.max(1, messages.length) * row + 34;
    var x = {};
    participants.forEach(function (id, i) { x[id] = pad + i * gap; });
    var body = [];

    participants.forEach(function (id) {
      body.push('<rect x="' + (x[id] - 64) + '" y="14" width="128" height="34" rx="8" fill="Canvas" stroke="currentColor"/>');
      body.push('<text x="' + x[id] + '" y="36" text-anchor="middle" font-size="14" fill="currentColor">' + esc(labels[id]) + '</text>');
      body.push('<line x1="' + x[id] + '" y1="48" x2="' + x[id] + '" y2="' + (height - 18) + '" stroke="currentColor" stroke-dasharray="5 5" opacity="0.45"/>');
    });

    body.push('<defs><marker id="mdv-seq-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor"/></marker></defs>');
    messages.forEach(function (msg, i) {
      var y = top + 34 + i * row;
      body.push('<line x1="' + x[msg.from] + '" y1="' + y + '" x2="' + x[msg.to] + '" y2="' + y + '" stroke="currentColor" stroke-width="1.7" marker-end="url(#mdv-seq-arrow)"/>');
      body.push('<text x="' + ((x[msg.from] + x[msg.to]) / 2) + '" y="' + (y - 8) + '" text-anchor="middle" font-size="13" fill="currentColor">' + esc(msg.label) + '</text>');
    });

    return '<svg xmlns="http://www.w3.org/2000/svg" role="img" viewBox="0 0 ' + width + ' ' + height + '" width="' + width + '" height="' + height + '">' + body.join('') + '</svg>';
  }

  function renderDiagram(source) {
    var first = (linesOf(source)[0] || '').toLowerCase();
    if (/^(graph|flowchart)\b/.test(first)) return flowSvg(source);
    if (/^sequencediagram\b/.test(first)) return sequenceSvg(source);
    throw new Error('unsupported Mermaid diagram type in offline lite renderer');
  }

  function renderElement(el) {
    if (!el || el.getAttribute('data-md-view-rendered') === '1') return;
    var source = el.textContent || '';
    try {
      el.innerHTML = renderDiagram(source);
      el.setAttribute('data-md-view-rendered', '1');
    } catch (err) {
      el.innerHTML = '<pre class="mermaid-error">' + esc(err && err.message ? err.message : err) + '\n\n' + esc(source) + '</pre>';
      el.setAttribute('data-md-view-rendered', '1');
    }
  }

  window.mermaid = {
    initialize: function () {},
    run: function (options) {
      var selector = options && options.querySelector ? options.querySelector : '.mermaid';
      Array.prototype.forEach.call(document.querySelectorAll(selector), renderElement);
      return Promise.resolve();
    },
    init: function (_config, nodes) {
      Array.prototype.forEach.call(nodes || document.querySelectorAll('.mermaid'), renderElement);
    }
  };
}());
