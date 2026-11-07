/* nas-admin 前端（无框架、零 CDN）。
   ⚠️ 三条铁律：
   1) 命令输出 / 日志 / 文件内容一律 textContent（模型或 shell 吐什么都不会变 HTML）
   2) 任何 401 → 弹口令遮罩，解锁后自动重试原请求（与作文批改同一套语义）
   3) 高危动作的确认**在服务端也校验**，前端只负责把用户敲的字传过去 */
(function () {
  'use strict';
  var $ = function (s) { return document.querySelector(s); };
  var $$ = function (s) { return Array.prototype.slice.call(document.querySelectorAll(s)); };
  var CURRENT = 'overview';
  var unlocked = false;          // 这个会话里是否已经解锁过（决定 401 是否弹框）

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function toast(msg) {
    var t = $('#toast');
    t.textContent = msg;
    t.classList.remove('hidden');
    clearTimeout(t._h);
    t._h = setTimeout(function () { t.classList.add('hidden'); }, 2600);
  }
  function hms(ms) {
    if (ms < 1000) return ms + ' ms';
    if (ms < 60000) return (ms / 1000).toFixed(1) + ' s';
    return Math.floor(ms / 60000) + ' 分 ' + Math.round((ms % 60000) / 1000) + ' 秒';
  }
  function ts(t) {
    if (!t) return '';
    var d = new Date(t * 1000);
    function p(n) { return (n < 10 ? '0' : '') + n; }
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' +
      p(d.getHours()) + ':' + p(d.getMinutes());
  }
  function sz(n) {
    if (n == null || n < 0) return '—';
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' K';
    if (n < 1073741824) return (n / 1048576).toFixed(1) + ' M';
    return (n / 1073741824).toFixed(1) + ' G';
  }

  /* ---------------- 口令门 ---------------- */
  var pending = null;            // 被 401 拦下的待重试请求
  function openPw(msg) {
    $('#pwErr').textContent = msg || '口令不正确，重试';
    if (msg) $('#pwErr').classList.remove('hidden'); else $('#pwErr').classList.add('hidden');
    $('#pwOverlay').classList.remove('hidden');
    setTimeout(function () { $('#pwInput').focus(); }, 60);
  }
  function closePw() {
    $('#pwOverlay').classList.add('hidden');
    $('#pwInput').value = '';
    $('#pwErr').classList.add('hidden');
  }
  function doUnlock() {
    api('/api/login', { method: 'POST', body: JSON.stringify({ password: $('#pwInput').value }) },
      function (j, ok, st) {
        if (ok) {
          unlocked = true;
          closePw();
          toast('已解锁');
          var p = pending; pending = null;
          if (p) api(p.path, p.opts, p.cb); else load(CURRENT);
        } else {
          openPw(st === 429 ? (j.msg || '太频繁了') : (j.msg || '口令不正确，重试'));
        }
      });
  }

  /* ---------------- 请求封装 ---------------- */
  function api(path, opts, cb) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    if (opts.body && typeof opts.body === 'string') {
      opts.headers['Content-Type'] = 'application/json';
    }
    fetch(path, opts).then(function (r) {
      var ct = r.headers.get('content-type') || '';
      var pr = ct.indexOf('application/json') >= 0 ? r.json() : r.text().then(function (t) { return { raw: t }; });
      return pr.then(function (j) {
        // ⚠️ 登录接口自己的 401 = "口令错"，必须交回 doUnlock 处理；
        //    只有**其它接口**的 401 才当成会话过期去弹遮罩（踩过：错口令提示不显示、
        //    解锁成功后重放失败登录请求 → 遮罩关不掉、总览不加载）
        if (r.status === 401 && path !== '/api/login') {
          pending = { path: path, opts: opts, cb: cb };
          openPw(unlocked ? '会话过期，重新解锁' : '');
          return;
        }
        if (cb) cb(j || {}, r.ok, r.status);
      });
    }).catch(function (e) {
      if (cb) cb({ error: 'net', msg: String(e) }, false, 0);
      else toast('网络错误：' + e);
    });
  }

  /* ---------------- 页签 ---------------- */
  function show(v) {
    CURRENT = v;
    $$('.view').forEach(function (s) { s.classList.toggle('hidden', s.id !== 'view-' + v); });
    $$('.tab').forEach(function (t) { t.classList.toggle('on', t.dataset.v === v); });
    // 只让当前页签的自动刷新跑，切走就停（见「自动刷新」节规矩 ①）
    if (v !== 'overview') stopAuto('overview');
    if (v !== 'containers') stopAuto('containers');
    load(v);
    if (v === 'overview') { syncAuto('overview'); stamp('overview'); }
    if (v === 'containers') { syncAuto('containers'); stamp('containers'); }
  }
  function load(v) {
    if (v === 'overview') loadOverview();
    else if (v === 'containers') loadContainers(false);
    else if (v === 'files') loadFs($('#fsPath').value || '/');
    else if (v === 'cmd') loadAudit('#cmdHistory', 30);
    else if (v === 'danger') loadAudit('#auditList', 50);
  }

  /* ---------------- 自动刷新 ---------------- */
  // 规矩（都踩过）：
  //   1) 只刷「当前可见页签」，切走就停 —— 否则在文件页/命令页也一直打接口，白烧 NAS；
  //   2) 上一次还没回来就跳过（`inflight`），别让慢 docker 接口排队堆起来；
  //   3) 页面在后台（切了浏览器标签/手机息屏）跳过，回来立刻补一次；
  //   4) 口令遮罩开着、或用户正在输入（焦点在输入框/文本域）时跳过 —— 别把人的输入洗掉；
  //   5) 「资源占用」模式（docker stats 要 2 秒）不自动刷，只在列表模式刷。
  // ⚠️ 2026-10-18 用户要求「强制启用自动刷新」：**没有开关**，进页面就刷；
  //    只保留「间隔」可选（记忆在本机 localStorage）。下面这些暂停条件一个都不能去：
  //    切走页签 / 页面在后台 / 口令遮罩开着 / 正在文本框里打字 / 上一发还没回来 /
  //    容器页处于「资源占用」模式（docker stats 一次约 2 秒，自动刷会把 NAS 打满）。
  var AUTO = { overview: null, containers: null };   // {timer, inflight, mode}
  var AUTO_KEY = 'nasadmin_auto_interval';

  function autoCfg() {
    try { return JSON.parse(localStorage.getItem(AUTO_KEY) || '{}') || {}; } catch (e) { return {}; }
  }
  function saveAutoSec(k, sec) {
    var c = autoCfg(); c[k] = sec;
    try { localStorage.setItem(AUTO_KEY, JSON.stringify(c)); } catch (e) { }
  }
  function pwOpen() { return !$('#pwOverlay').classList.contains('hidden'); }
  function typing() {
    var a = document.activeElement;
    if (!a) return false;
    var tag = (a.tagName || '').toLowerCase();
    // 只有「会打字」的控件才让路；select/checkbox 获得焦点不该停刷新
    // （踩过：select_option 之后焦点留在 select，自动刷新一直是 0 次）
    if (tag === 'input') {
      var t = (a.type || 'text').toLowerCase();
      return ['text', 'password', 'search', 'number', 'email', 'url', 'tel', 'date', 'time'].indexOf(t) >= 0;
    }
    return tag === 'textarea' || !!a.isContentEditable;
  }
  function stamp(k) {
    var el = k === 'overview' ? $('#ovAge') : $('#ctAge');
    if (el) el.textContent = '更新于 ' + new Date().toLocaleTimeString('zh-CN', { hour12: false });
  }
  function stopAuto(k) {
    if (AUTO[k] && AUTO[k].timer) clearInterval(AUTO[k].timer);
    // ⚠️ 只清定时器，**不许重置整个对象**——`mode` 是页面状态（列表/资源占用），
    //    跟定时器生命周期无关。旧写法 `AUTO[k]={...mode:false}` 会把刚设的 mode 冲掉，
    //    导致「资源占用模式禁用自动刷新」永远失效（UI 测试抓到的）。
    if (!AUTO[k]) AUTO[k] = { timer: null, inflight: false, mode: false };
    AUTO[k].timer = null;
    AUTO[k].inflight = false;
  }
  function startAuto(k) {
    stopAuto(k);
    var sel = k === 'overview' ? $('#ovAutoSec') : $('#ctAutoSec');
    var def = k === 'overview' ? 5 : 10;
    var sec = Math.max(2, parseInt(sel && sel.value, 10) || def);
    AUTO[k].timer = setInterval(function () {
      if (CURRENT !== k) return;                       // ① 只刷当前页签
      if (document.hidden) return;                     // ③ 后台不刷
      if (pwOpen() || typing()) return;                // ④ 人在操作就先让路
      if (AUTO[k].inflight) return;                    // ② 上一发还没回
      if (k === 'containers' && AUTO[k].mode) return;  // ⑤ 资源占用模式不自动刷
      AUTO[k].inflight = true;
      var done = function () { AUTO[k].inflight = false; stamp(k); };
      if (k === 'overview') loadOverview(done);
      else loadContainers(false, done);
    }, sec * 1000);
  }
  function syncAuto(k) {
    var sel = k === 'overview' ? $('#ovAutoSec') : $('#ctAutoSec');
    var mode = k === 'containers' ? AUTO[k].mode : false;
    if (sel) sel.disabled = !!(k === 'containers' && mode);
    if (sel) saveAutoSec(k, parseInt(sel.value, 10));
    startAuto(k);
  }
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) return;
    // 从后台回来立刻补一次（不等下一个周期）
    if (CURRENT === 'overview' || CURRENT === 'containers') {
      var k = CURRENT;
      if (AUTO[k].timer && !AUTO[k].inflight && !pwOpen()) {
        AUTO[k].inflight = true;
        var done = function () { AUTO[k].inflight = false; stamp(k); };
        if (k === 'overview') loadOverview(done); else if (!AUTO[k].mode) loadContainers(false, done);
        else AUTO[k].inflight = false;
      }
    }
  });

  /* ---------------- 1. 总览 ---------------- */
  function stat(k, v, cls) {
    return '<div class="stat ' + (cls || '') + '"><div class="k">' + esc(k) +
      '</div><div class="v">' + v + '</div></div>';
  }
  function bar(usedPct) {
    var cls = usedPct >= 90 ? 'bad' : (usedPct >= 75 ? 'warn' : '');
    return '<div class="bar ' + cls + '"><i style="width:' + Math.min(100, usedPct) + '%"></i></div>';
  }
  function loadOverview(done) {
    api('/api/overview', {}, function (j, ok) {
      if (done) done();
      if (!ok) { $('#ovStats').innerHTML = '<div class="muted small">' + esc(j.msg || j.error || '读取失败') + '</div>'; return; }
      var memPct = j.mem.total_mb ? Math.round(100 * j.mem.used_mb / j.mem.total_mb) : 0;
      var cpuCls = j.cpu.total >= 90 ? 'bad' : (j.cpu.total >= 70 ? 'warn' : 'good');
      // 排列顺序（2026-10-18 用户要求重排）：动态健康指标在前（CPU/内存/负载/温度），
      // 然后是「有没有事」（容器），最后才是身份信息（运行时间/IP）——一眼扫过去先看要紧的
      var h = '';
      h += stat('CPU 占用', (j.cpu.total == null ? '–' : j.cpu.total + '%'), cpuCls);
      h += stat('内存', j.mem.used_mb + '<small> / ' + j.mem.total_mb + ' MB</small>',
        memPct >= 90 ? 'bad' : (memPct >= 75 ? 'warn' : ''));
      h += stat('负载', j.load.load1 + ' <small>' + j.load.load5 + ' ' + j.load.load15 + '</small>');
      if (j.temps.length) {
        var tmax = Math.max.apply(null, j.temps.map(function (t) { return t.c; }));
        h += stat('温度', tmax + ' <small>°C</small>', tmax >= 70 ? 'bad' : (tmax >= 60 ? 'warn' : ''));
      }
      h += stat('容器', j.containers.running + '<small> 运行 / ' + j.containers.total + ' 总数</small>',
        (!j.containers.error && j.containers.total > 0 && j.containers.running < j.containers.total) ? 'warn' : '');
      // swap 单独占一格：NAS 曾经 swap 吃满、内存假象紧张，藏在「系统信息」卡里根本看不见
      if (j.mem.swap_total_mb > 0) {
        var swPct = Math.round(100 * j.mem.swap_used_mb / j.mem.swap_total_mb);
        h += stat('Swap', j.mem.swap_used_mb + '<small> / ' + j.mem.swap_total_mb + ' MB</small>',
          swPct >= 85 ? 'bad' : (swPct >= 60 ? 'warn' : ''));
      }
      // AI 余额：值单独走 /api/balance（服务端有缓存，不会 5 秒一打）；卡片带 ↻ 手动强刷
      h += '<div class="stat" id="balStat"><div class="k">DeepSeek 余额' +
        ' <button class="btn ghost tiny" data-bal-refresh title="强制重新查询">↻</button></div>' +
        '<div class="v"><span class="muted small">查询中…</span></div></div>';
      h += stat('运行时间', '<span style="font-size:16px">' + esc(j.uptime.text) + '</span>');
      // IPv6 地址又长又多，全塞进卡片会把后面内容顶下去两屏（手机实测）；只显示 IPv4，IPv6 记个数
      var ips = j.host.ips || [];
      var ips4 = ips.filter(function (x) { return String(x).indexOf(':') < 0; });
      var ip6n = ips.length - ips4.length;
      h += stat('本机 IP', '<span style="font-size:15px">' + esc(ips4.join(' ') || (ip6n ? '（仅 IPv6）' : '–')) +
        '</span>' + (ip6n ? '<div class="muted small">IPv6 ×' + ip6n + '</div>' : ''));
      $('#ovStats').innerHTML = h;
      loadBalance(false);               // 余额单独取（服务端缓存 5 分钟，刷新页面不打外面 API）

      $('#ovUptime').textContent = '现在 ' + j.host.now;
      var dh = '<table class="tbl"><thead><tr><th>挂载点</th><th>设备</th><th>已用</th><th style="width:34%">占用</th></tr></thead><tbody>';
      j.disks.forEach(function (d) {
        dh += '<tr><td class="mono" data-label="挂载点">' + esc(d.mount) + '</td>' +
          '<td class="small muted mono" data-label="设备">' + esc(d.dev) +
          '</td><td class="nowrap" data-label="已用">' + d.used_gb + 'G / ' + d.total_gb + 'G<small>（剩 ' +
          d.free_gb + 'G）</small></td><td data-label="占用"><b>' + d.used_pct + '%</b>' + bar(d.used_pct) + '</td></tr>';
      });
      dh += '</tbody></table>';
      $('#ovDisks').innerHTML = dh || '<div class="muted small">没读到磁盘</div>';

      var ch = '';
      if (j.containers.error) ch = '⚠️ docker 不可用：' + esc(j.containers.error);
      else ch = '<b>' + j.containers.running + '</b> 个在跑，共 <b>' + j.containers.total +
        '</b> 个容器。<button class="btn ghost tiny" data-go="containers">去管理 ›</button>';
      $('#ovContainers').innerHTML = ch;

      var mh = '';
      if (j.temps.length) {
        mh += '🌡️ ' + j.temps.map(function (t) { return esc(t.zone) + ' ' + t.c + '°C'; }).join('　');
      }
      mh += '<div class="mt">🧩 内核 <span class="mono">' + esc(j.host.kernel) + '</span></div>';
      mh += '<div>💻 <span class="mono">' + esc(j.host.name) + '</span></div>';
      // swap 不再重复：已升格成顶部统计格（高占比会标黄/标红），这里去掉
      $('#ovMisc').innerHTML = mh;
      loadCharts();                 // 走势图跟着总览一起刷新
    });
  }

  /* ---------------- 1a2. DeepSeek 余额（服务端缓存 5 分钟；↻ 强制刷新） ---------------- */
  function loadBalance(force) {
    var box = $('#balStat');
    if (!box) return;
    api('/api/balance' + (force ? '?refresh=1' : ''), {}, function (j, ok) {
      if (!box.isConnected) return;         // 页面已重渲/切走，别写孤儿节点
      var v = box.querySelector('.v');
      if (!v) return;
      if (!ok || !j.ok) {
        box.className = 'stat bad';
        v.innerHTML = '<span style="font-size:14px">查不到</span>' +
          '<div class="muted small">' + esc((j && (j.error || j.err)) || '查询失败') + '</div>';
        return;
      }
      var stale = j.stale ? ' <span class="muted" title="' + esc(j.err || '') + '">（旧值）</span>' : '';
      box.className = 'stat ' + (j.total <= 0 ? 'bad' : (j.total < 1 ? 'warn' : 'good'));
      v.innerHTML = '¥' + j.total.toFixed(2) + stale +
        (j.is_available ? '' : '<div class="muted small">已欠费：接口会拒</div>') +
        '<div class="muted small">更新 ' + esc(new Date(j.ts * 1000).toTimeString().slice(0, 5)) + '</div>';
    });
  }

  /* ---------------- 1b. CPU / 内存走势图（canvas 手绘，零 CDN） ---------------- */
  var CH = { points: [], interval: 5 };
  function hexToRgba(h, a) {
    h = (h || '').trim();
    if (h.charAt(0) !== '#') return 'rgba(47,109,246,' + a + ')';
    if (h.length === 4) h = '#' + h[1] + h[1] + h[2] + h[2] + h[3] + h[3];
    var n = parseInt(h.slice(1), 16);
    return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')';
  }
  function fmtHM(ts) {
    var d = new Date(ts * 1000), p = function (n) { return (n < 10 ? '0' : '') + n; };
    return p(d.getHours()) + ':' + p(d.getMinutes());
  }
  function drawChart(cv, pts, key, opt) {
    var dpr = Math.min(3, window.devicePixelRatio || 1);
    var w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) return;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
      cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    }
    var ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    var cs = getComputedStyle(document.documentElement);
    var cLine = cs.getPropertyValue('--line').trim() || '#e6e6e2';
    var cMuted = cs.getPropertyValue('--muted').trim() || '#8a8a86';
    var cMain = cs.getPropertyValue(opt.colorVar || '--brand').trim() || '#2f6df6';
    var padL = 42, padR = 6, padT = 8, padB = 20;
    var iw = Math.max(10, w - padL - padR), ih = Math.max(10, h - padT - padB);
    var yMax = opt.max > 0 ? opt.max : 100;
    var grid = opt.grid || [0, yMax / 2, yMax];
    ctx.font = '11px -apple-system,"PingFang SC","Microsoft YaHei",sans-serif';
    ctx.textBaseline = 'middle';
    grid.forEach(function (g) {
      var y = padT + ih - (g / yMax) * ih;
      ctx.strokeStyle = cLine; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(padL, y + 0.5); ctx.lineTo(padL + iw, y + 0.5); ctx.stroke();
      ctx.fillStyle = cMuted; ctx.textAlign = 'right';
      ctx.fillText(opt.axisFmt ? opt.axisFmt(g) : String(Math.round(g)), padL - 6, y);
    });
    if (!pts || pts.length < 2) {
      ctx.fillStyle = cMuted; ctx.textAlign = 'center';
      ctx.fillText('正在采集数据…', padL + iw / 2, padT + ih / 2);
      return;
    }
    var t0 = pts[0].t, t1 = pts[pts.length - 1].t;
    if (t1 <= t0) t1 = t0 + 1;
    var X = function (t) { return padL + (t - t0) / (t1 - t0) * iw; };
    var Y = function (v) { return padT + ih - Math.max(0, Math.min(yMax, v)) / yMax * ih; };
    var g = ctx.createLinearGradient(0, padT, 0, padT + ih);
    g.addColorStop(0, hexToRgba(cMain, 0.22));
    g.addColorStop(1, hexToRgba(cMain, 0.02));
    ctx.beginPath();
    ctx.moveTo(X(pts[0].t), padT + ih);
    pts.forEach(function (p) { ctx.lineTo(X(p.t), Y(p[key])); });
    ctx.lineTo(X(pts[pts.length - 1].t), padT + ih);
    ctx.closePath(); ctx.fillStyle = g; ctx.fill();
    ctx.beginPath();
    pts.forEach(function (p, i) { i ? ctx.lineTo(X(p.t), Y(p[key])) : ctx.moveTo(X(p.t), Y(p[key])); });
    ctx.strokeStyle = cMain; ctx.lineWidth = 2; ctx.lineJoin = 'round'; ctx.lineCap = 'round';
    ctx.stroke();
    var last = pts[pts.length - 1];
    ctx.beginPath(); ctx.arc(X(last.t), Y(last[key]), 2.6, 0, Math.PI * 2);
    ctx.fillStyle = cMain; ctx.fill();
    ctx.fillStyle = cMuted; ctx.textBaseline = 'alphabetic';
    ctx.textAlign = 'left'; ctx.fillText(fmtHM(t0), padL, h - 5);
    ctx.textAlign = 'right'; ctx.fillText(fmtHM(t1), padL + iw, h - 5);
  }
  function loadCharts() {
    var mins = parseInt($('#chRange') && $('#chRange').value, 10) || 15;
    api('/api/charts?minutes=' + mins, {}, function (j, ok) {
      if (!ok) { $('#chHint').textContent = '图表读取失败'; return; }
      CH.points = j.points || [];
      CH.interval = j.interval || 5;
      renderCharts();
    });
  }
  function renderCharts() {
    var pts = CH.points || [];
    var last = pts[pts.length - 1];
    var total = (last && last.mem_total) || 0;
    if ($('#chCpuNow')) $('#chCpuNow').textContent = last ? last.cpu + '%' : '–';
    if ($('#chMemNow')) $('#chMemNow').textContent = last ? Math.round(last.mem_used) + ' MB' : '–';
    if ($('#chCpuMeta')) {
      if (pts.length) {
        var mx = Math.max.apply(null, pts.map(function (p) { return p.cpu; }));
        $('#chCpuMeta').textContent = '峰值 ' + mx + '%';
      } else $('#chCpuMeta').textContent = '';
    }
    if ($('#chMemMeta')) {
      if (last) {
        $('#chMemMeta').textContent = '/ ' + Math.round(last.mem_total) + ' MB' +
          (last.swap_used > 0 ? '　swap ' + Math.round(last.swap_used) + ' MB' : '');
      } else $('#chMemMeta').textContent = '';
    }
    if ($('#chHint')) {
      $('#chHint').textContent = pts.length >= 2 ? ('每 ' + CH.interval + ' 秒采样') : '采样中…约 10 秒出图';
    }
    var c1 = $('#chCpu'), c2 = $('#chMem');
    if (c1) drawChart(c1, pts, 'cpu', { max: 100, grid: [0, 50, 100], colorVar: '--brand' });
    if (c2) drawChart(c2, pts, 'mem_used', {
      max: total || 1, grid: [0, (total || 1) / 2, total || 1],
      colorVar: '--ok', axisFmt: function (v) { return Math.round(v) + 'M'; }
    });
  }
  window.addEventListener('resize', function () {
    clearTimeout(CH._rt); CH._rt = setTimeout(renderCharts, 150);
  });
  (function () {
    try {
      var mq = window.matchMedia('(prefers-color-scheme: dark)');
      var h = function () { renderCharts(); };
      if (mq.addEventListener) mq.addEventListener('change', h);
      else if (mq.addListener) mq.addListener(h);
    } catch (e) { }
  })();

  /* ---------------- 2. 容器 ---------------- */
  function loadContainers(withStats, done) {
    if (!AUTO.containers) AUTO.containers = { timer: null, inflight: false, mode: false };
    AUTO.containers.mode = !!withStats;
    // 资源占用模式下必须禁掉自动刷新（docker stats 一次约 2 秒，自动刷会把 NAS 打满）
    syncAuto('containers');
    $('#ctList').innerHTML = '<div class="muted small">读取中…' + (withStats ? '（含资源占用，约 2 秒）' : '') + '</div>';
    api('/api/containers' + (withStats ? '?stats=1' : ''), {}, function (j, ok) {
      if (done) done();
      if (!ok) { $('#ctList').innerHTML = '<div class="muted small">⚠️ ' + esc(j.msg || 'docker 出错') + '</div>'; return; }
      var list = j.containers || [];
      $('#ctCount').textContent = list.length;
      if (!list.length) { $('#ctList').innerHTML = '<div class="muted">没有容器</div>'; return; }
      var h = '<table class="tbl"><thead><tr><th>状态</th><th>名字</th><th>镜像</th><th>端口</th>' +
        (withStats ? '<th>CPU / 内存</th>' : '') + '<th>操作</th></tr></thead><tbody>';
      list.forEach(function (c) {
        h += '<tr><td class="nowrap" data-label="状态">' + (c.up ? '<span style="color:var(--ok)">● 运行中</span>'
          : '<span class="muted">○ 停了</span>') + '<div class="muted small">' + esc(c.status) + '</div></td>' +
          '<td class="mono" data-label="名字">' + esc(c.name) + '</td>' +
          '<td class="small mono" style="word-break:break-all" data-label="镜像">' + esc(c.image) + '</td>' +
          '<td class="small mono" style="word-break:break-all" data-label="端口">' + esc(c.ports || '—') + '</td>' +
          (withStats ? '<td class="small nowrap" data-label="资源">' + esc(c.cpu || '—') + '<br>' + esc(c.mem || '—') + '</td>' : '') +
          '<td class="nowrap" data-label="操作">' +
          (c.up
            ? '<button class="btn tiny" data-act="restart" data-n="' + esc(c.name) + '">重启</button> ' +
              '<button class="btn tiny" data-act="stop" data-n="' + esc(c.name) + '">停</button>'
            : '<button class="btn tiny" data-act="start" data-n="' + esc(c.name) + '">启动</button>') +
          ' <button class="btn tiny ghost" data-act="logs" data-n="' + esc(c.name) + '">日志</button>' +
          ' <button class="btn tiny danger" data-act="rm" data-n="' + esc(c.name) + '">删除</button>' +
          '</td></tr>';
      });
      h += '</tbody></table>';
      $('#ctList').innerHTML = h;
    });
  }
  function containerAct(action, name) {
    var body = { name: name, action: action };
    if (action === 'rm') {
      var ans = window.prompt('删除容器要输入容器名确认：\n\n' + name, '');
      if (ans == null) return;
      body.confirm = ans.trim();
    }
    toast(action + ' ' + name + ' …');
    api('/api/container', { method: 'POST', body: JSON.stringify(body) }, function (j, ok) {
      toast(ok ? ('✔ ' + name + (j.msg ? '：' + j.msg : '')) : ('✘ ' + (j.msg || j.error)));
      loadContainers(false);
    });
  }
  function showLogs(name) {
    $('#ctLogCard').classList.remove('hidden');
    $('#ctLogName').textContent = name;
    $('#ctLogBox').textContent = '读取中…';
    api('/api/container/logs?name=' + encodeURIComponent(name) + '&tail=300', {}, function (j, ok) {
      $('#ctLogBox').textContent = ok ? (j.text || '（没有输出）') : ('⚠️ ' + (j.msg || j.error));
    });
  }

  /* ---------------- 3. 文件 ---------------- */
  var CUR = '/', EDIT = '';
  function loadFs(path) {
    CUR = path || '/';
    $('#fsPath').value = CUR;
    $('#fsList').innerHTML = '<div class="muted small">读取中…</div>';
    api('/api/fs?path=' + encodeURIComponent(CUR), {}, function (j, ok) {
      if (!ok) {
        $('#fsList').innerHTML = '<div class="muted small">⚠️ ' + esc(j.msg || '读不了') + '</div>';
        $('#fsCrumbs').innerHTML = '';
        return;
      }
      CUR = j.path;
      $('#fsPath').value = CUR;
      var ch = '<a href="#" data-p="/">/</a>';
      (j.crumbs || []).forEach(function (c) {
        ch += ' <span class="muted">›</span> <a href="#" data-p="' + esc(c.path) + '">' + esc(c.name) + '</a>';
      });
      $('#fsCrumbs').innerHTML = ch;
      var h = '';
      if (j.parent && j.parent !== CUR) {
        h += '<div class="fs-row" data-p="' + esc(j.parent) + '"><span class="fs-ic">↩</span>' +
          '<span class="nm">.. 上一级</span></div>';
      }
      (j.entries || []).forEach(function (e) {
        var ic = e.dir ? '📁' : (e.symlink ? '🔗' : '📄');
        var full = (CUR === '/' ? '' : CUR) + '/' + e.name;
        h += '<div class="fs-row ' + (e.dir ? 'dir' : '') + '" data-p="' + esc(full) + '">' +
          '<span class="fs-ic">' + ic + '</span>' +
          '<span class="nm">' + esc(e.name) + (e.symlink ? ' <span class="muted small">→ ' + esc(e.symlink) + '</span>' : '') + '</span>' +
          '<span class="mt2">' + (e.dir ? '' : sz(e.size)) + '</span>' +
          '<span class="mt2">' + (e.mtime ? ts(e.mtime).slice(5) : '') + '</span>' +
          '<span class="mt2 mono">' + esc(e.mode || '') + '</span>' +
          // 下载按钮：文件=直接下；目录=打包成 zip 下（⚠️ stopPropagation，别顺带钻进目录）
          (e.error ? '<span class="mt2" style="color:var(--danger)">锁</span>'
            : '<button class="btn ghost tiny dl" data-dl="' + esc(full) + '" data-isdir="' + (e.dir ? '1' : '0') + '"' +
              ' title="' + (e.dir ? '打包成 zip 下载整个文件夹' : '下载文件') + '">⬇️' + (e.dir ? '打包' : '') + '</button>') +
          '</div>';
      });
      $('#fsList').innerHTML = h || '<div class="muted small">空目录</div>';
      if (j.truncated) $('#fsList').innerHTML += '<div class="muted small">（超 1000 项只显示前 1000）</div>';
    });
  }
  function openFile(path) {
    api('/api/fs/read?path=' + encodeURIComponent(path), {}, function (j, ok) {
      if (!ok) { toast('⚠️ ' + (j.msg || j.error)); return; }
      EDIT = j.path;
      $('#fsEditCard').classList.remove('hidden');
      $('#fsEditName').textContent = EDIT;
      $('#fsEdit').value = j.content;
      $('#fsEditCard').scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  }
  function deletePath(path, isDir) {
    var need = isDir ? path : path.split('/').pop();
    var ans = window.prompt((isDir ? '删除目录要输入**完整路径**' : '删除文件要输入**文件名**') +
      '确认：\n\n' + need, '');
    if (ans == null) return;
    api('/api/fs/delete', { method: 'POST', body: JSON.stringify({ path: path, confirm: ans.trim() }) },
      function (j, ok) {
        toast(ok ? '✔ 已删除' : ('✘ ' + (j.msg || j.error)));
        if (ok) {
          if (EDIT === path) $('#fsEditCard').classList.add('hidden');
          loadFs(CUR);
        } else if (j.error === 'need_confirm') {
          window.alert(j.msg);
        }
      });
  }

  /* ---------------- 4. 命令 ---------------- */
  function runCmd() {
    var cmd = $('#cmdIn').value.trim();
    if (!cmd) { toast('先输入命令'); return; }
    var out = $('#cmdOut');
    out.classList.remove('hidden');
    out.textContent = '▶ ' + cmd + '\n（执行中，最多 30 秒…）';
    $('#btnCmdRun').disabled = true;
    api('/api/exec', { method: 'POST', body: JSON.stringify({ cmd: cmd }) }, function (j, ok) {
      $('#btnCmdRun').disabled = false;
      if (!j || j.error === 'net') { out.textContent = '⚠️ ' + (j && j.msg ? j.msg : '请求失败'); return; }
      var head = '▶ ' + j.cmd + '\n── exit=' + j.rc + '（' + hms(j.ms) + '）──\n';
      out.textContent = head + (j.out || '') + (j.err ? (j.out ? '\n' : '') + '── stderr ──\n' + j.err : '') +
        (j.truncated ? '\n…（输出超长已截断）' : '');
      out.scrollTop = out.scrollHeight;
      loadAudit('#cmdHistory', 30);
    });
  }
  function loadAudit(sel, n) {
    var box = $(sel);
    if (!box) return;
    box.innerHTML = '<div class="muted small">读取中…</div>';
    api('/api/audit?tail=' + n, {}, function (j, ok) {
      if (!ok) { box.innerHTML = '<div class="muted small">读不到审计日志</div>'; return; }
      var rows = (j.rows || []).slice().reverse();
      if (!rows.length) { box.innerHTML = '<div class="muted small">还没有记录</div>'; return; }
      var h = '<table class="tbl"><thead><tr><th>时间</th><th>IP</th><th>类型</th><th>动作</th><th>详情</th></tr></thead><tbody>';
      rows.forEach(function (r) {
        var dcolor = r.rc === 0 ? 'var(--ok)' : (r.rc ? 'var(--danger)' : 'var(--muted)');
        h += '<tr><td class="nowrap small" data-label="时间">' + esc(r.ts) + '</td>' +
          '<td class="small" data-label="IP">' + esc(r.ip) + '</td>' +
          '<td class="small" data-label="类型">' + esc(r.kind) + '</td>' +
          '<td class="small" data-label="动作">' + esc(r.action) +
          (r.rc != null ? ' <span class="mono" style="color:' + dcolor + '">rc=' + esc(r.rc) + '</span>' : '') +
          '</td><td class="small mono" style="word-break:break-all" data-label="详情">' + esc(r.detail) + '</td></tr>';
      });
      h += '</tbody></table>';
      box.innerHTML = h;
    });
  }

  /* ---------------- 5. 危险区 ---------------- */
  function systemAct(action, confirmVal) {
    api('/api/system', { method: 'POST', body: JSON.stringify({ action: action, confirm: confirmVal }) },
      function (j, ok) {
        if (ok) {
          if (action === 'docker_prune') {
            $('#pruneOut').classList.remove('hidden');
            $('#pruneOut').textContent = j.msg + '\n\n' + (j.out || '');
            toast('✔ 清理完成');
          } else {
            toast('✔ ' + j.msg);
          }
        } else {
          toast('✘ ' + (j.msg || j.error));
          if (j.error === 'need_confirm') window.alert(j.msg);
        }
      });
  }

  /* ---------------- 事件绑定 ---------------- */
  document.addEventListener('click', function (e) {
    var br = e.target.closest ? e.target.closest('[data-bal-refresh]') : null;
    if (br) { e.preventDefault(); loadBalance(true); return; }
    var t = e.target.closest ? e.target.closest('[data-v],[data-p],[data-act],[data-go]') : null;
    if (!t) return;
    if (t.dataset.v) { e.preventDefault(); show(t.dataset.v); return; }
    if (t.dataset.go) { e.preventDefault(); show(t.dataset.go); return; }
    if (t.dataset.p != null) { e.preventDefault(); loadFs(t.dataset.p); return; }
    if (t.dataset.act) {
      e.preventDefault();
      var a = t.dataset.act, n = t.dataset.n;
      if (a === 'logs') showLogs(n);
      else if (a === 'rm') {
        var isDir = t.dataset.dir === '1';
        deletePath(n, isDir);
      } else containerAct(a, n);
    }
  });
  // 文件行：目录=进入，文件=打开编辑
  $('#fsList').addEventListener('click', function (e) {
    var dl = e.target.closest ? e.target.closest('[data-dl]') : null;
    if (dl) {                                  // 点下载：别冒泡去打开/进目录
      e.preventDefault();
      e.stopPropagation();
      downloadPath(dl.dataset.dl, dl.dataset.isdir === '1', dl);
      return;
    }
    var row = e.target.closest('.fs-row');
    if (!row || !row.dataset.p) return;
    e.stopPropagation();
    var name = row.querySelector('.nm').textContent.trim();
    if (row.classList.contains('dir') || name === '.. 上一级') loadFs(row.dataset.p);
    else openFile(row.dataset.p);
  });

  // 下载：文件 → /api/fs/download；目录 → /api/fs/download_folder（打包 zip）
  function downloadPath(p, isDir, btn) {
    var old = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = isDir ? '打包中…' : '下载中…'; }
    // 用 fetch 探一次（带 cookie、能读错误体），再由浏览器另起下载 —— 这样打包失败会给出人话提示
    var url = (isDir ? '/api/fs/download_folder?path=' : '/api/fs/download?path=') + encodeURIComponent(p);
    // ⚠️ 不能拿 HEAD 探：Flask 的 GET 路由默认不接 HEAD 之外的方法时会给 405，
    //    而且打包接口本身只认 GET。用 `Range: bytes=0-0` 探——只拿 1 字节，
    //    Flask 会回 206 且**不会**把整个 zip 传过来（探失败也能提前给人话提示）。
    fetch(url, { headers: { Range: 'bytes=0-0' }, cache: 'no-store' }).then(function (r) {
      if (r.ok || r.status === 206) {
        window.location.href = url;           // 走浏览器自带下载管理器（大文件更稳）
      } else {
        return r.json().then(function (j) { toast('✘ ' + (j.msg || j.error || ('HTTP ' + r.status))); });
      }
    }).catch(function (e) {
      toast('✘ 网络错误：' + e);
    }).then(function () {
      if (btn) { btn.disabled = false; btn.textContent = old; }
    });
  }
  $('#fsCrumbs').addEventListener('click', function (e) {
    if (e.target.tagName === 'A') { e.preventDefault(); loadFs(e.target.dataset.p); }
  });

  $('#btnPwOk').addEventListener('click', doUnlock);
  $('#pwInput').addEventListener('keydown', function (e) { if (e.key === 'Enter') doUnlock(); });
  $('#btnPwCancel').addEventListener('click', closePw);

  $('#btnRefresh').addEventListener('click', function () { load(CURRENT); toast('已刷新'); });
  $('#btnLogout').addEventListener('click', function () {
    api('/api/logout', { method: 'POST' }, function () { unlocked = false; openPw('已退出，重新输入口令'); });
  });

  $('#chRange').addEventListener('change', loadCharts);
  $('#btnCtReload').addEventListener('click', function () { loadContainers(false, function () { stamp('containers'); }); });
  $('#btnCtStats').addEventListener('click', function () {
    loadContainers(true, function () { stamp('containers'); syncAuto('containers'); });
    syncAuto('containers');
  });
  [['#ovAutoSec', 'overview'], ['#ctAutoSec', 'containers']].forEach(function (t) {
    var s = $(t[0]);
    if (!s) return;
    s.addEventListener('change', function () { syncAuto(t[1]); });
  });
  $('#btnCtLogClose').addEventListener('click', function () { $('#ctLogCard').classList.add('hidden'); });

  $('#btnFsGo').addEventListener('click', function () { loadFs($('#fsPath').value.trim() || '/'); });
  $('#fsPath').addEventListener('keydown', function (e) { if (e.key === 'Enter') loadFs($('#fsPath').value.trim() || '/'); });
  $('#btnFsReload').addEventListener('click', function () { loadFs(CUR); });
  $('#btnFsMkdir').addEventListener('click', function () {
    var nm = window.prompt('新目录名（会建在 ' + CUR + ' 下）', '');
    if (!nm) return;
    api('/api/fs/mkdir', { method: 'POST', body: JSON.stringify({ path: (CUR === '/' ? '' : CUR) + '/' + nm }) },
      function (j, ok) { toast(ok ? '✔ 已创建' : ('✘ ' + (j.msg || j.error))); loadFs(CUR); });
  });
  $('#btnFsUp').addEventListener('click', function () { $('#fsFile').click(); });
  $('#fsFile').addEventListener('change', function () {
    if (!this.files.length) return;
    var fd = new FormData();
    Array.prototype.forEach.call(this.files, function (f) { fd.append('files', f); });
    fd.append('path', CUR);
    toast('上传中…');
    fetch('/api/fs/upload', { method: 'POST', body: fd }).then(function (r) { return r.json(); })
      .then(function (j) {
        toast(j.ok ? ('✔ 上传 ' + (j.saved || []).length + ' 个' + ((j.errors || []).length ? '，失败 ' + j.errors.length : ''))
          : ('✘ ' + (j.msg || j.error)));
        this.value = '';
        loadFs(CUR);
      }.bind(this)).catch(function (e) { toast('上传失败：' + e); });
  });
  $('#btnFsEditClose').addEventListener('click', function () { $('#fsEditCard').classList.add('hidden'); });
  $('#btnFsSave').addEventListener('click', function () {
    if (!EDIT) return;
    api('/api/fs/save', { method: 'POST', body: JSON.stringify({ path: EDIT, content: $('#fsEdit').value }) },
      function (j, ok) { toast(ok ? ('✔ 已保存（' + j.bytes + ' 字节）') : ('✘ ' + (j.msg || j.error))); });
  });
  $('#btnFsDl').addEventListener('click', function () {
    if (EDIT) window.open('/api/fs/download?path=' + encodeURIComponent(EDIT), '_blank');
  });
  $('#btnFsDel').addEventListener('click', function () {
    if (EDIT) deletePath(EDIT, false);
  });

  $('#btnCmdRun').addEventListener('click', runCmd);
  $('#cmdIn').addEventListener('keydown', function (e) { if (e.key === 'Enter') runCmd(); });
  $('#btnAuditReload').addEventListener('click', function () { loadAudit('#cmdHistory', 30); });
  $('#btnAudit2').addEventListener('click', function () { loadAudit('#auditList', 50); });

  $('#btnReboot').addEventListener('click', function () {
    if (window.confirm('真的要重启 NAS 吗？所有服务会中断。')) {
      systemAct('reboot', $('#cfReboot').value.trim());
    }
  });
  $('#btnShutdown').addEventListener('click', function () {
    if (window.confirm('真的要关机吗？')) systemAct('shutdown', $('#cfShutdown').value.trim());
  });
  $('#btnPrune').addEventListener('click', function () {
    systemAct('docker_prune', $('#cfPrune').value.trim());
  });

  /* ---------------- 启动 ---------------- */
  // 恢复上次选的刷新间隔（开关已取消：自动刷新是强制的）
  (function restoreAuto() {
    var c = autoCfg();
    [['overview', '#ovAutoSec'], ['containers', '#ctAutoSec']].forEach(function (t) {
      var sel = $(t[1]), saved = c[t[0]];
      if (!sel || !saved) return;
      var has = Array.prototype.some.call(sel.options, function (o) { return o.value === String(saved); });
      if (has) sel.value = String(saved);
    });
  })();
  fetch('/api/health').then(function (r) { return r.json(); }).then(function (j) {
    if (!j.configured) $('#cfgBanner').classList.remove('hidden');
  }).catch(function () { });
  show('overview');
})();
