// Небольшие удобства интерфейса: автосабмит фильтров, подтверждение отклонения, спарклайны.
document.addEventListener('DOMContentLoaded', function () {
  // светлая/тёмная тема: атрибут на <html>, выбор в localStorage
  function applyTheme(dark) {
    if (dark) document.documentElement.setAttribute('data-theme', 'dark'); else document.documentElement.removeAttribute('data-theme');
    document.querySelectorAll('.js-theme-icon').forEach(function (el) { el.textContent = dark ? '☀️' : '🌙'; });
    document.querySelectorAll('.js-theme-label').forEach(function (el) { el.textContent = dark ? 'Светлая тема' : 'Тёмная тема'; });
    try { localStorage.setItem('radar_theme', dark ? 'dark' : 'light'); } catch (e) {}
  }
  var isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  document.querySelectorAll('.js-theme-icon').forEach(function (el) { el.textContent = isDark ? '☀️' : '🌙'; });
  document.querySelectorAll('.js-theme-label').forEach(function (el) { el.textContent = isDark ? 'Светлая тема' : 'Тёмная тема'; });
  document.querySelectorAll('.js-theme-toggle').forEach(function (b) {
    b.addEventListener('click', function () { applyTheme(document.documentElement.getAttribute('data-theme') !== 'dark'); });
  });
  // перетаскивание границы заголовка меняет ширину колонки
  document.querySelectorAll('table').forEach(function (table) {
    table.querySelectorAll('thead th').forEach(function (th) {
      var grip = document.createElement('span'); grip.className = 'col-grip'; th.appendChild(grip);
      var startX, startW;
      grip.addEventListener('mousedown', function (e) {
        startX = e.pageX; startW = th.offsetWidth; e.preventDefault();
        function move(ev) { th.style.minWidth = th.style.width = Math.max(40, startW + ev.pageX - startX) + 'px'; }
        function up() { document.removeEventListener('mousemove', move); document.removeEventListener('mouseup', up); }
        document.addEventListener('mousemove', move); document.addEventListener('mouseup', up);
      });
    });
  });
  // сортировка по клику на заголовок (все таблицы, кроме class="nosort"); строка-«хвост» (tr.usp-row) едет вместе с основной
  function cellValue(td) {
    if (!td) return { n: null, s: '' };
    var t = (td.dataset.sort !== undefined ? td.dataset.sort : td.textContent).replace(/\s+/g, ' ').trim();
    var num = t.replace(/[ \s]/g, '').replace(',', '.').replace(/[₽%]/g, '').replace(/(млн|тыс|г\.|шт).*$/, '');
    var m = num.match(/^[+\-]?\d+(\.\d+)?/);
    var n = m ? parseFloat(m[0]) : null;
    if (n !== null && /млн/.test(t)) n *= 1e6; else if (n !== null && /тыс/.test(t)) n *= 1e3;
    var d = t.match(/^(\d{2})\.(\d{2})\.(\d{4})/); if (d) n = +(d[3] + d[2] + d[1]);
    var iso = t.match(/^(\d{4})-(\d{2})-(\d{2})/); if (iso) n = +(iso[1] + iso[2] + iso[3]);
    if (/^(нет данных|не найдено|не проверялось|не указано|нет информации|—|-)$/i.test(t)) return { n: null, s: '' };
    return { n: n, s: t.toLowerCase() };
  }
  document.querySelectorAll('table:not(.nosort)').forEach(function (table) {
    var tbody = table.tBodies[0]; if (!tbody || !table.tHead) return;
    var ths = table.tHead.rows[table.tHead.rows.length - 1].cells;
    Array.prototype.forEach.call(ths, function (th, idx) {
      if (th.classList.contains('nosort') || !th.textContent.trim()) return;
      th.classList.add('sortable'); th.title = (th.title ? th.title + ' · ' : '') + 'сортировать';
      th.addEventListener('click', function (e) {
        if (e.target.classList.contains('col-grip')) return;
        var dir = th.dataset.dir === 'asc' ? 'desc' : 'asc';
        Array.prototype.forEach.call(ths, function (o) { o.dataset.dir = ''; o.classList.remove('sort-asc', 'sort-desc'); });
        th.dataset.dir = dir; th.classList.add(dir === 'asc' ? 'sort-asc' : 'sort-desc');
        var groups = [], cur = null;
        Array.prototype.forEach.call(tbody.rows, function (tr) {
          if (tr.classList.contains('usp-row') || tr.classList.contains('sub-row')) { if (cur) cur.push(tr); return; }
          cur = [tr]; groups.push(cur);
        });
        groups.sort(function (a, b) {
          var va = cellValue(a[0].cells[idx]), vb = cellValue(b[0].cells[idx]);
          var aEmpty = va.n === null && !va.s, bEmpty = vb.n === null && !vb.s;
          if (aEmpty && bEmpty) return 0; if (aEmpty) return 1; if (bEmpty) return -1;
          var r;
          if (va.n !== null && vb.n !== null) r = va.n - vb.n; else r = va.s.localeCompare(vb.s, 'ru');
          return dir === 'asc' ? r : -r;
        });
        groups.forEach(function (g) { g.forEach(function (tr) { tbody.appendChild(tr); }); });
      });
    });
  });
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
