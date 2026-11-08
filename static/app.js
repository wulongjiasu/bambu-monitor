(() => {
  'use strict';

  const FILTERS = {
    all: () => true,
    running: (p) => p.status === 'RUNNING' || p.status === 'PREPARE',
    paused: (p) => p.status === 'PAUSE',
    finished: (p) => p.status === 'FINISH',
    error: (p) => p.status !== 'OFFLINE' && (p.status === 'FAILED' || p.error > 0),
    offline: (p) => !p.online,
  };

  const FAN_LABELS = {
    cooling: '部件风扇',
    heatbreak: '热端风扇',
    aux: '辅助风扇',
    chamber: '腔体风扇',
  };

  const grid = document.getElementById('grid');
  const template = document.getElementById('card-template');
  const empty = document.getElementById('empty');
  const emptyTitle = document.getElementById('empty-title');
  const emptySub = document.getElementById('empty-sub');
  const connection = document.getElementById('connection');
  const connectionText = document.getElementById('connection-text');
  const searchInput = document.getElementById('search');
  const toastEl = document.getElementById('toast');
  const addOpen = document.getElementById('add-open');
  const dialog = document.getElementById('printer-dialog');
  const form = document.getElementById('printer-form');
  const dialogClose = document.getElementById('dialog-close');
  const dialogCancel = document.getElementById('dialog-cancel');
  const dialogSubmit = document.getElementById('dialog-submit');
  const scanBtn = document.getElementById('scan-btn');
  const scanBtnText = document.getElementById('scan-btn-text');
  const scanStatus = document.getElementById('scan-status');
  const scanResults = document.getElementById('scan-results');

  const state = new Map();
  const refs = new Map();

  // 展开的卡片 ID 会记住，SSE 更新和刷新页面后保持不变
  const expanded = new Set();
  const EXPAND_KEY = 'bambu-expanded';

  try {
    const saved = JSON.parse(localStorage.getItem(EXPAND_KEY) || '[]');
    if (Array.isArray(saved)) saved.forEach((id) => expanded.add(String(id)));
  } catch (_) {
    /* 本地记录损坏时忽略 */
  }

  let appTitle = '打印工坊';
  let allowControl = false;
  let allowManage = false;
  let activeFilter = 'all';
  let query = '';
  let toastTimer = null;
  let scanning = false;

  /* ------------------------------------------------------------- formatting */
  const round = (value) => (value == null ? null : Math.round(Number(value)));

  function formatMinutes(minutes) {
    const value = Number(minutes);
    if (!Number.isFinite(value) || value <= 0) return '—';
    if (value < 60) return `${Math.round(value)}m`;
    const hours = Math.floor(value / 60);
    const mins = Math.round(value % 60);
    return `${hours}h${String(mins).padStart(2, '0')}m`;
  }

  function formatHours(hours) {
    const value = Number(hours);
    if (!Number.isFinite(value)) return '—';
    if (value < 100) return `${value.toFixed(1)} h`;
    return `${Math.round(value).toLocaleString('zh-CN')} h`;
  }

  function formatTracked(hours) {
    const value = Number(hours);
    if (!Number.isFinite(value) || value <= 0) return '0 分钟';
    const minutes = Math.round(value * 60);
    if (minutes < 60) return `${minutes} 分钟`;
    return formatHours(value);
  }

  function fanPercent(raw) {
    if (raw == null) return null;
    const value = Number(raw);
    if (!Number.isFinite(value)) return null;
    // Bambu MQTT reports fan speed on a 0-15 scale (15 = 100%).
    // Keep fallbacks for firmware that reports 0-100 or 0-255.
    let percent;
    if (value <= 15) percent = (value / 15) * 100;
    else if (value <= 100) percent = value;
    else percent = (value / 255) * 100;
    return Math.max(0, Math.min(100, Math.round(percent)));
  }

  function wifiLevel(dbm) {
    if (dbm == null) return -1;
    if (dbm >= -50) return 4;
    if (dbm >= -60) return 3;
    if (dbm >= -70) return 2;
    if (dbm >= -80) return 1;
    return 0;
  }

  function ageOf(printer) {
    if (printer.last_seen == null) return null;
    const elapsed = (Date.now() - (printer._received || Date.now())) / 1000;
    return Math.max(0, Number(printer.last_seen) + elapsed);
  }

  function compactAge(seconds) {
    if (seconds == null) return '—';
    const value = Math.floor(seconds);
    if (value < 60) return `${value} 秒`;
    if (value < 3600) return `${Math.floor(value / 60)} 分钟`;
    const hours = Math.floor(value / 3600);
    const mins = Math.floor((value % 3600) / 60);
    return mins ? `${hours} 小时 ${mins} 分` : `${hours} 小时`;
  }

  function setText(el, value) {
    if (el && el.textContent !== value) el.textContent = value;
  }

  function saveExpanded() {
    try {
      localStorage.setItem(EXPAND_KEY, JSON.stringify([...expanded]));
    } catch (_) {
      /* 隐私模式下忽略 */
    }
  }

  function applyExpanded(el, id) {
    const on = expanded.has(String(id));
    el.root.classList.toggle('is-expanded', on);
    if (el.toggle) {
      el.toggle.setAttribute('aria-expanded', String(on));
      el.toggle.setAttribute('aria-label', on ? '收起详情' : '展开详情');
      el.toggle.title = on ? '收起详情' : '展开详情';
    }
  }

  function toggleCard(card) {
    const id = card?.dataset.id;
    if (!id) return;
    if (expanded.has(id)) expanded.delete(id);
    else expanded.add(id);
    saveExpanded();
    const el = refs.get(id);
    if (el) applyExpanded(el, id);
  }

  /* -------------------------------------------------------------- rendering */
  function createCard(printer) {
    const node = template.content.firstElementChild.cloneNode(true);
    node.dataset.id = printer.id;

    const q = (selector) => node.querySelector(selector);
    const fans = {};
    node.querySelectorAll('[data-fan]').forEach((wrap) => {
      fans[wrap.dataset.fan] = {
        wrap,
        value: wrap.querySelector('b'),
        fill: wrap.querySelector('.meter i'),
      };
    });

    const el = {
      root: node,
      toggle: q('.card__toggle'),
      status: q('[data-f="status"]'),
      model: q('[data-f="model"]'),
      name: q('[data-f="name"]'),
      file: q('[data-f="file"]'),
      ring: q('.ring'),
      ringValue: q('.ring__value'),
      progress: q('[data-f="progress"]'),
      remaining: q('[data-f="remaining"]'),
      layer: q('[data-f="layer"]'),
      speed: q('[data-f="speed"]'),
      usage: q('[data-f="usage"]'),
      fans,
      nozzle: q('[data-f="nozzle"]'),
      nozzleTarget: q('[data-f="nozzle-target"]'),
      bed: q('[data-f="bed"]'),
      bedTarget: q('[data-f="bed-target"]'),
      chamber: q('[data-f="chamber"]'),
      chamberTarget: q('[data-f="chamber-target"]'),
      chamberChip: q('[data-temp="chamber"]'),
      error: q('.card__error'),
      manage: q('.card__manage'),
      wifiBars: [...node.querySelectorAll('.wifi__bars i')],
      wifiText: q('[data-f="wifi-text"]'),
      ip: q('[data-f="ip"]'),
      updated: q('[data-f="updated"]'),
      actions: q('.actions'),
      pause: q('[data-action="pause"]'),
      resume: q('[data-action="resume"]'),
      stop: q('[data-action="stop"]'),
    };

    refs.set(printer.id, el);
    grid.appendChild(node);
    updateCard(printer);
  }

  function updateCard(printer) {
    const el = refs.get(printer.id);
    if (!el) return;

    const status = String(printer.status || 'IDLE').toLowerCase().replace(/[^a-z]/g, '');
    const hasData = printer.last_seen != null;
    const progress = printer.online ? Math.max(0, Math.min(100, Number(printer.progress) || 0)) : Number(printer.progress) || 0;

    el.root.className = `card status-${status || 'idle'}${printer.online ? '' : ' is-offline'}`;
    el.root.dataset.id = printer.id;
    applyExpanded(el, printer.id);

    setText(el.status, printer.status_label || printer.status || '—');
    setText(el.model, printer.model || '拓竹');
    setText(el.name, printer.name || printer.id);
    setText(el.file, printer.file || '无打印任务');
    el.file.title = printer.file || '';

    el.ringValue.style.strokeDashoffset = String(100 - progress);
    el.ring.setAttribute('aria-label', `打印进度 ${progress}%`);
    setText(el.progress, String(progress));

    const showJob = hasData && (
      ['RUNNING', 'PREPARE', 'PAUSE', 'FINISH', 'FAILED', 'OFFLINE'].includes(printer.status)
    );
    setText(el.remaining, showJob ? formatMinutes(printer.remaining_min) : '—');
    setText(
      el.layer,
      showJob && printer.total_layers ? `${printer.layer} / ${printer.total_layers}` : '—'
    );
    setText(el.speed, hasData && printer.status !== 'IDLE' ? printer.speed_label || '—' : '—');

    const usage = Number(printer.usage_hours);
    const tracked = Number(printer.usage_tracked_hours) || 0;
    const baseline = Number(printer.usage_baseline_hours) || 0;
    setText(el.usage, formatHours(usage));
    el.usage.title = [
      `累计工作时间 ${formatHours(usage)}`,
      `起始 ${formatHours(baseline)} + 本机记录 ${formatTracked(tracked)}`,
      '统计打印、暂停和准备时间',
    ].join(' · ');

    for (const [key, fan] of Object.entries(el.fans)) {
      const raw = printer.fans ? printer.fans[key] : null;
      const percent = fanPercent(raw);
      if (percent == null) {
        setText(fan.value, '—');
        fan.fill.style.width = '0%';
        fan.wrap.title = `${FAN_LABELS[key]} · 无数据`;
      } else {
        setText(fan.value, `${percent}%`);
        fan.fill.style.width = `${percent}%`;
        fan.wrap.title = `${FAN_LABELS[key]} ${percent}%（原始值 ${raw}）`;
      }
    }

    const temps = printer.temps || {};
    const nozzle = hasData ? round(temps.nozzle) : null;
    const bed = hasData ? round(temps.bed) : null;
    const chamber = hasData ? round(temps.chamber) : null;

    setText(el.nozzle, nozzle == null ? '—' : `${nozzle}°`);
    setText(el.nozzleTarget, round(temps.nozzle_target) ? `/${round(temps.nozzle_target)}°` : '');
    setText(el.bed, bed == null ? '—' : `${bed}°`);
    setText(el.bedTarget, round(temps.bed_target) ? `/${round(temps.bed_target)}°` : '');

    const hasChamber = temps.chamber != null;
    el.chamberChip.hidden = !hasChamber;
    if (hasChamber) {
      setText(el.chamber, chamber == null ? '—' : `${chamber}°`);
      setText(el.chamberTarget, '');
    }

    const errorCode = Number(printer.error) || 0;
    const showError = printer.status === 'FAILED' || errorCode > 0;
    el.error.hidden = !showError;
    if (showError) {
      setText(el.error, errorCode ? `设备报告错误 · #${errorCode}` : '打印任务失败，请检查打印机');
    }

    const level = printer.online ? wifiLevel(printer.wifi) : -1;
    el.wifiBars.forEach((bar, index) => bar.classList.toggle('on', index < level));
    setText(el.wifiText, printer.wifi == null ? '—' : `${printer.wifi} dBm`);
    el.wifiText.parentElement.title = printer.wifi == null ? 'WiFi 信号未知' : `WiFi ${printer.wifi} dBm`;
    setText(el.ip, printer.ip || '—');
    el.ip.title = printer.ip ? `内网 IP：${printer.ip}` : 'IP 未知';

    updateFooter(printer);
    updateActions(el, printer);
  }

  function updateFooter(printer) {
    const el = refs.get(printer.id);
    if (!el) return;
    const age = ageOf(printer);
    if (age == null) {
      setText(el.updated, '暂无数据');
      el.updated.classList.remove('stale');
      return;
    }
    setText(el.updated, age < 5 ? '刚刚更新' : `${compactAge(age)}前更新`);
    el.updated.classList.toggle('stale', age > 180 && printer.online);
  }

  function updateActions(el, printer) {
    if (el.manage) el.manage.hidden = !allowManage;
    const actionable = allowControl && printer.online;
    if (!actionable) {
      el.actions.hidden = true;
      return;
    }
    const running = printer.status === 'RUNNING' || printer.status === 'PREPARE';
    const paused = printer.status === 'PAUSE';
    el.pause.hidden = !running;
    el.resume.hidden = !paused;
    el.stop.hidden = !(running || paused);
    el.actions.hidden = !(running || paused);
  }

  /* ---------------------------------------------------------------- summary */
  function countStats() {
    const list = [...state.values()];
    return {
      total: list.length,
      online: list.filter((p) => p.online).length,
      printing: list.filter((p) => p.status === 'RUNNING' || p.status === 'PREPARE').length,
      paused: list.filter((p) => p.status === 'PAUSE').length,
      finished: list.filter((p) => p.status === 'FINISH').length,
      error: list.filter((p) => p.status !== 'OFFLINE' && (p.status === 'FAILED' || p.error > 0)).length,
      offline: list.filter((p) => !p.online).length,
    };
  }

  function renderSummary() {
    const stats = countStats();
    setText(document.getElementById('stat-online'), `${stats.online}/${stats.total}`);
    setText(document.getElementById('stat-printing'), String(stats.printing));
    setText(document.getElementById('stat-paused'), String(stats.paused));
    setText(document.getElementById('stat-error'), String(stats.error));
    document.querySelector('.stat--alert')?.classList.toggle('has-alert', stats.error > 0);

    const counts = {
      all: stats.total,
      running: stats.printing,
      paused: stats.paused,
      finished: stats.finished,
      error: stats.error,
      offline: stats.offline,
    };
    for (const [name, value] of Object.entries(counts)) {
      const node = document.querySelector(`[data-count="${name}"]`);
      if (node) setText(node, String(value));
    }

    setText(document.getElementById('footer-count'), String(stats.total));
    document.title = `${appTitle} · ${stats.online}/${stats.total} 在线`;
  }

  function matchesSearch(printer) {
    if (!query) return true;
    const haystack = [printer.name, printer.id, printer.model, printer.file, printer.subtask_name, printer.ip]
      .filter(Boolean)
      .join(' ')
      .toLowerCase();
    return haystack.includes(query);
  }

  function applyFilters() {
    let visible = 0;
    for (const [id, printer] of state) {
      const el = refs.get(id);
      if (!el) continue;
      const match = (FILTERS[activeFilter] || FILTERS.all)(printer) && matchesSearch(printer);
      el.root.hidden = !match;
      if (match) visible += 1;
    }

    const total = state.size;
    empty.hidden = visible > 0;
    if (total === 0) {
      emptyTitle.textContent = '还没有打印机';
      emptySub.textContent = allowManage
        ? '点击「添加打印机」扫描局域网，或手动填写设备信息。'
        : '在 config.json 中添加设备后刷新页面。';
    } else {
      emptyTitle.textContent = '没有符合条件的打印机';
      emptySub.textContent = '试试切换状态筛选，或清空搜索关键字。';
    }
  }

  /* ------------------------------------------------------------------ state */
  function decorate(printer) {
    printer._baseAge = printer.last_seen;
    printer._received = Date.now();
    return printer;
  }

  function applySnapshot(payload) {
    appTitle = payload.config?.title || '打印工坊';
    allowControl = Boolean(payload.config?.allow_control);
    allowManage = Boolean(payload.config?.allow_manage);
    addOpen.hidden = !allowManage;
    setText(document.getElementById('app-title'), appTitle);

    grid.replaceChildren();
    state.clear();
    refs.clear();

    for (const printer of payload.printers || []) {
      decorate(printer);
      state.set(printer.id, printer);
      createCard(printer);
    }

    renderSummary();
    applyFilters();
  }

  function applyUpdate(payload) {
    const printer = payload.printer;
    if (!printer) return;
    decorate(printer);
    const isNew = !state.has(printer.id);
    state.set(printer.id, printer);
    if (isNew) createCard(printer);
    else updateCard(printer);
    renderSummary();
    applyFilters();
  }

  function removePrinter(id) {
    const key = String(id || '');
    if (!key || !state.has(key)) return;
    state.delete(key);
    refs.get(key)?.root.remove();
    refs.delete(key);
    if (expanded.delete(key)) saveExpanded();
    renderSummary();
    applyFilters();
  }

  /* -------------------------------------------------------------------- SSE */
  function setLive(mode) {
    connection.classList.remove('is-connecting', 'is-offline');
    if (mode === 'on') {
      connectionText.textContent = '实时连接';
      connection.title = '已连接监控服务';
    } else if (mode === 'off') {
      connection.classList.add('is-offline');
      connectionText.textContent = '连接中断，重试中';
      connection.title = '与监控服务的连接已断开';
    } else {
      connection.classList.add('is-connecting');
      connectionText.textContent = '连接中';
      connection.title = '正在连接监控服务';
    }
  }

  function connect() {
    setLive('connecting');
    const source = new EventSource('/api/events');

    source.addEventListener('open', () => setLive('on'));
    source.addEventListener('snapshot', (event) => {
      try {
        applySnapshot(JSON.parse(event.data));
        setLive('on');
      } catch (error) {
        console.warn('快照解析失败', error);
      }
    });
    source.addEventListener('update', (event) => {
      try {
        applyUpdate(JSON.parse(event.data));
      } catch (error) {
        console.warn('状态解析失败', error);
      }
    });
    source.addEventListener('remove', (event) => {
      try {
        removePrinter(JSON.parse(event.data).id);
      } catch (error) {
        console.warn('移除事件解析失败', error);
      }
    });
    source.addEventListener('error', () => setLive('off'));
  }

  /* ------------------------------------------------------------------- misc */
  function toast(message, kind = 'ok') {
    toastEl.textContent = message;
    toastEl.className = `toast show${kind === 'error' ? ' error' : ''}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove('show'), 2800);
  }

  function tickClock() {
    const now = new Date();
    const clock = document.getElementById('clock');
    clock.textContent = now.toLocaleTimeString('zh-CN', { hour12: false });
    clock.dateTime = now.toISOString();
  }

  /* ------------------------------------------------------------- add dialog */
  function openDialog() {
    if (!allowManage) {
      toast('网页管理已禁用（server.allow_manage）', 'error');
      return;
    }
    form.reset();
    scanResults.replaceChildren();
    scanResults.hidden = true;
    scanStatus.textContent = '自动发现同网段的拓竹打印机（约 10 秒）';
    setScanning(false);
    dialog.showModal();
    setTimeout(() => form.elements.serial?.focus(), 60);
  }

  function closeDialog() {
    if (dialog.open) dialog.close();
  }

  function setScanning(active) {
    scanning = active;
    scanBtn.disabled = active;
    scanBtn.classList.toggle('is-busy', active);
    scanBtnText.textContent = active ? '扫描中…' : '扫描局域网';
  }

  function fillForm(device) {
    form.elements.model.value = device.model || '';
    form.elements.serial.value = device.serial || '';
    form.elements.ip.value = device.ip || '';
    const named = device.name && device.name !== device.model ? device.name : '';
    form.elements.name.value = named || (device.model ? `${device.model}-${(device.serial || '').slice(-4)}` : '');
    form.elements.access_code.focus();
    toast('已填入设备信息，请补充访问码');
  }

  function renderScanResults(devices, seconds) {
    scanResults.replaceChildren();
    if (!devices.length) {
      scanStatus.textContent = `未发现打印机（用时 ${seconds ?? '—'} 秒）。请确认服务器与打印机在同一网段，或手动填写。`;
      scanResults.hidden = true;
      return;
    }
    scanStatus.textContent = `发现 ${devices.length} 台设备（用时 ${seconds ?? '—'} 秒），点击即可填入`;
    for (const device of devices) {
      const item = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `found${device.known ? ' is-known' : ''}`;
      button.disabled = Boolean(device.known);

      const main = document.createElement('span');
      main.className = 'found__main';
      const title = document.createElement('b');
      title.textContent = device.model || '拓竹打印机';
      const meta = document.createElement('span');
      meta.textContent = [
        device.ip,
        device.serial || '序列号未识别',
        device.name && device.name !== device.model ? device.name : '',
        device.signal == null ? '' : `${device.signal} dBm`,
      ].filter(Boolean).join(' · ');
      main.append(title, meta);

      const tag = document.createElement('span');
      tag.className = 'found__tag';
      tag.textContent = device.known ? '已添加' : '填入';

      button.append(main, tag);
      button.addEventListener('click', () => fillForm(device));
      item.append(button);
      scanResults.append(item);
    }
    scanResults.hidden = false;
  }

  async function scanLan() {
    if (scanning) return;
    setScanning(true);
    scanResults.hidden = true;
    scanResults.replaceChildren();
    scanStatus.textContent = '正在监听局域网广播（约 10 秒）…';
    try {
      const response = await fetch('/api/scan', { method: 'POST' });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `扫描失败 (${response.status})`);
      renderScanResults(data.devices || [], data.scanned_in);
    } catch (error) {
      scanStatus.textContent = error.message || '扫描失败';
      toast(error.message || '扫描失败', 'error');
    } finally {
      setScanning(false);
    }
  }

  async function submitPrinter(event) {
    event.preventDefault();
    if (!allowManage) {
      toast('网页管理已禁用', 'error');
      return;
    }
    const payload = Object.fromEntries(new FormData(form).entries());
    dialogSubmit.disabled = true;
    dialogSubmit.textContent = '添加中…';
    try {
      const response = await fetch('/api/printers', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `添加失败 (${response.status})`);
      closeDialog();
      toast(data.message || '已添加');
    } catch (error) {
      toast(error.message || '添加失败', 'error');
    } finally {
      dialogSubmit.disabled = false;
      dialogSubmit.textContent = '添加';
    }
  }

  function bindEvents() {
    addOpen.addEventListener('click', openDialog);
    dialogClose.addEventListener('click', closeDialog);
    dialogCancel.addEventListener('click', closeDialog);
    scanBtn.addEventListener('click', scanLan);
    form.addEventListener('submit', submitPrinter);
    dialog.addEventListener('click', (event) => {
      // 点击遮罩层（dialog 自身）关闭弹窗
      if (event.target === dialog) closeDialog();
    });

    document.getElementById('tabs').addEventListener('click', (event) => {
      const button = event.target.closest('[data-filter]');
      if (!button) return;
      activeFilter = button.dataset.filter;
      document.querySelectorAll('.tab').forEach((tab) => tab.classList.toggle('is-active', tab === button));
      applyFilters();
    });

    let searchTimer;
    searchInput.addEventListener('input', () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        query = searchInput.value.trim().toLowerCase();
        applyFilters();
      }, 120);
    });

    searchInput.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        searchInput.value = '';
        query = '';
        applyFilters();
      }
    });

    grid.addEventListener('click', async (event) => {
      const button = event.target.closest('button[data-action]');

      if (!button) {
        const card = event.target.closest('.card');
        if (!card) return;
        // 选中文字或点击表单元素时不触发折叠
        if (event.target.closest('a, input, textarea, select')) return;
        if (window.getSelection()?.toString()) return;
        toggleCard(card);
        return;
      }

      const card = button.closest('.card');
      const id = card?.dataset.id;
      const printer = state.get(id);
      if (!printer) return;

      const action = button.dataset.action;

      if (action === 'remove') {
        if (!allowManage) return;
        if (!window.confirm(`确定从监控中移除「${printer.name}」吗？\n配置文件中的该设备也会被删除。`)) {
          return;
        }
        button.disabled = true;
        try {
          const response = await fetch(`/api/printers/${encodeURIComponent(id)}`, { method: 'DELETE' });
          const data = await response.json().catch(() => ({}));
          if (!response.ok) throw new Error(data.error || `移除失败 (${response.status})`);
          toast(data.message || '已移除');
        } catch (error) {
          toast(error.message || '移除失败', 'error');
          button.disabled = false;
        }
        return;
      }

      if (action === 'stop' && !window.confirm(`确定停止「${printer.name}」当前的打印任务吗？`)) {
        return;
      }

      const buttons = [...card.querySelectorAll('.actions button')];
      buttons.forEach((item) => { item.disabled = true; });

      try {
        const response = await fetch(`/api/printers/${encodeURIComponent(id)}/command`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
        toast(data.message || '指令已发送');
      } catch (error) {
        toast(error.message || '指令发送失败', 'error');
      } finally {
        setTimeout(() => buttons.forEach((item) => { item.disabled = false; }), 1200);
      }
    });
  }

  function start() {
    bindEvents();
    tickClock();
    setInterval(tickClock, 1000);
    setInterval(() => {
      for (const printer of state.values()) updateFooter(printer);
      applyFilters();
    }, 5000);
    connect();
  }

  start();
})();
