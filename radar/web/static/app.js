// Небольшие удобства интерфейса: автосабмит фильтров, подтверждение отклонения, спарклайны.
document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('form.filters select').forEach(function (el) {
    el.addEventListener('change', function () { el.form.submit(); });
  });
  document.querySelectorAll('form[data-confirm]').forEach(function (f) {
    f.addEventListener('submit', function (e) { if (!confirm(f.dataset.confirm)) e.preventDefault(); });
  });
  document.querySelectorAll('svg.spark[data-series]').forEach(function (svg) {
    var vals; try { vals = JSON.parse(svg.dataset.series); } catch (e) { return; }
    if (!vals || vals.length < 2) return;
    var w = +svg.getAttribute('width') || 160, h = +svg.getAttribute('height') || 36, max = Math.max.apply(null, vals) || 1;
    var pts = vals.map(function (v, i) { return (i / (vals.length - 1) * (w - 2) + 1).toFixed(1) + ',' + (h - 1 - (v / max) * (h - 4)).toFixed(1); });
    var p = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
    p.setAttribute('points', pts.join(' ')); p.setAttribute('fill', 'none'); p.setAttribute('stroke', '#1f5fbf'); p.setAttribute('stroke-width', '1.5');
    svg.appendChild(p);
  });
});
