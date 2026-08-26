(() => {
  'use strict';
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const weekdays = ['', '星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日'];
  let courses = [];
  let homeworks = [];
  let previewRows = [];

  async function api(url, options = {}) {
    const config = { ...options, headers: { ...(options.headers || {}) } };
    const method = String(config.method || 'GET').toUpperCase();
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      const token = $('meta[name="deskguardian-csrf-token"]')?.content;
      if (!token) throw new Error('页面安全令牌缺失，请刷新后重试');
      config.headers['X-DeskGuardian-CSRF'] = token;
    }
    if (config.body && !(config.body instanceof FormData) && typeof config.body !== 'string') {
      config.headers['Content-Type'] = 'application/json';
      config.body = JSON.stringify(config.body);
    }
    const response = await fetch(url, config);
    const type = response.headers.get('content-type') || '';
    const payload = type.includes('json') ? await response.json() : { ok: response.ok, data: await response.text() };
    if (!response.ok || payload.ok === false) {
      const error = payload.error;
      const message = typeof error === 'string' ? error : (error && error.message) || `请求失败（${response.status}）`;
      throw new Error(message);
    }
    return Object.prototype.hasOwnProperty.call(payload, 'data') ? payload.data : payload;
  }

  function toast(message, kind = '') {
    const item = document.createElement('div');
    item.className = `toast ${kind}`;
    item.textContent = message;
    $('#toast-region')?.append(item);
    window.setTimeout(() => item.remove(), 3500);
  }

  function busy(button, active, label = '处理中…') {
    if (!button) return;
    if (active) { button.dataset.label = button.textContent; button.textContent = label; button.disabled = true; }
    else { button.textContent = button.dataset.label || button.textContent; button.disabled = false; }
  }

  function value(obj, ...keys) {
    for (const key of keys) if (obj && obj[key] !== undefined && obj[key] !== null) return obj[key];
    return '';
  }

  function dateText(raw) {
    if (!raw) return '未设置';
    const text = String(raw);
    const calendarDate = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
    if (calendarDate) {
      return `${calendarDate[1]}年${Number(calendarDate[2])}月${Number(calendarDate[3])}日`;
    }
    if (!/^\d{4}-\d{2}-\d{2}[T\s]/.test(text)) return text;
    const date = new Date(text);
    return Number.isNaN(date.getTime()) ? text : new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric', weekday: 'short', hour: '2-digit', minute: '2-digit' }).format(date);
  }

  function create(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function initNavigation() {
    const page = document.body.dataset.page;
    $(`[data-nav="${page}"]`)?.classList.add('active');
    $('.nav-toggle')?.addEventListener('click', (event) => {
      const open = $('.main-nav').classList.toggle('open');
      event.currentTarget.setAttribute('aria-expanded', String(open));
    });
  }

  async function sendTestNotification(button) {
    busy(button, true, '正在发送…');
    try { await api('/api/notifications/test', { method: 'POST' }); toast('测试提醒已发送'); }
    catch (error) { toast(error.message, 'error'); }
    finally { busy(button, false); }
  }

  async function initToday() {
    $('#test-notification')?.addEventListener('click', e => sendTestNotification(e.currentTarget));
    try {
      const data = await api('/api/today');
      const dayCourses = value(data, 'courses', 'today_courses') || [];
      const dueHomeworks = value(data, 'homeworks', 'due_homeworks') || [];
      const heading = $('#today-heading');
      heading.textContent = value(data, 'title', 'date_label') || new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' }).format(new Date());
      $('#today-summary').textContent = `今天有 ${dayCourses.length} 节课程，未来 3 天有 ${dueHomeworks.length} 项作业截止。`;
      $('#course-count').textContent = dayCourses.length;
      $('#homework-count').textContent = dueHomeworks.length;
      renderTodayCourses(dayCourses);
      renderTodayHomeworks(dueHomeworks);
    } catch (error) {
      $('#today-error').textContent = `无法加载今日数据：${error.message}`;
      $('#today-error').classList.remove('hidden');
      renderTodayCourses([]); renderTodayHomeworks([]);
    }
  }

  function renderTodayCourses(items) {
    const root = $('#today-courses'); root.replaceChildren();
    if (!items.length) { root.append(create('p', 'empty-state', '今天没有课程，享受空闲时间吧。')); return; }
    items.forEach(item => {
      const row = create('div', 'timeline-item');
      row.append(create('div', 'timeline-time', value(item, 'time', 'start_time') || `第${value(item, 'start_section')}节`), create('span', 'timeline-marker'));
      const content = create('div', 'timeline-content');
      content.append(create('h3', '', value(item, 'name', 'course_name') || '未命名课程'), create('p', 'muted', `${value(item, 'room') || '教室未定'} · 第${value(item, 'start_section')}–${value(item, 'end_section')}节`));
      row.append(content); root.append(row);
    });
  }

  function renderTodayHomeworks(items) {
    const root = $('#today-homeworks'); root.replaceChildren();
    if (!items.length) { root.append(create('p', 'empty-state', '未来 3 天没有作业截止。')); return; }
    items.forEach(item => {
      const card = create('div', 'homework-card');
      card.append(create('h3', '', value(item, 'subject') || '未分类'), create('p', 'muted', value(item, 'content', 'name') || '未填写内容'), create('span', 'due-label', `截止：${dateText(value(item, 'deadline'))}`));
      root.append(card);
    });
  }

  function normalizeCourse(item = {}) {
    return { id: item.id, name: value(item, 'name', 'course_name'), weekday: Number(value(item, 'weekday', 'day')) || 1, start_section: Number(value(item, 'start_section', 'start')) || 1, end_section: Number(value(item, 'end_section', 'end')) || Number(value(item, 'start_section', 'start')) || 1, room: value(item, 'room', 'classroom'), is_active: value(item, 'is_active', 'active') === '' ? true : Boolean(value(item, 'is_active', 'active')) };
  }

  function initImport() {
    $('#parse-form')?.addEventListener('submit', parseSchedule);
    $('#clear-import')?.addEventListener('click', () => {
      $('#parse-form').reset(); previewRows = []; renderPreview(); $('#preview-section').classList.add('hidden'); $('#parse-status').classList.add('hidden');
    });
    $('#add-preview-row')?.addEventListener('click', () => { previewRows.push(normalizeCourse()); renderPreview(); });
    $('#select-all-preview')?.addEventListener('change', e => $$('.preview-select').forEach(box => { box.checked = e.target.checked; }));
    $('#confirm-import')?.addEventListener('click', confirmImport);
  }

  async function parseSchedule(event) {
    event.preventDefault();
    const button = $('#parse-button'); const status = $('#parse-status');
    busy(button, true, '解析中…'); status.className = 'alert info'; status.textContent = '正在解析，AI 不可用时将自动切换规则引擎。';
    try {
      const data = await api('/api/parse', { method: 'POST', body: { text: $('#schedule-text').value, mode: $('#parse-mode').value } });
      const list = Array.isArray(data) ? data : (value(data, 'courses', 'results', 'items') || []);
      const candidates = Array.isArray(data?.candidates) ? data.candidates : [];
      previewRows = list.length ? list.map(normalizeCourse) : candidates.map(line => normalizeCourse({ name: line }));
      const source = value(data, 'source', 'parser') || '未知解析器';
      const fallback = value(data, 'fallback', 'fallback_reason', 'message');
      $('#parse-meta').textContent = `解析来源：${source}${fallback ? ` · ${fallback}` : ''}`;
      status.className = 'alert success'; status.textContent = list.length ? `解析完成，共得到 ${previewRows.length} 条课程。请检查后确认。` : `自动解析未得到完整课程，已载入 ${previewRows.length} 条候选行。请逐格修正后确认。`;
      $('#preview-section').classList.remove('hidden'); renderPreview();
    } catch (error) { status.className = 'alert error'; status.textContent = `解析失败：${error.message}。你仍可点击“新增一行”手工录入。`; $('#preview-section').classList.remove('hidden'); renderPreview(); }
    finally { busy(button, false); }
  }

  function renderPreview() {
    const body = $('#preview-body'); if (!body) return; body.replaceChildren();
    previewRows.forEach((item, index) => {
      const row = document.createElement('tr');
      const selectedCell = document.createElement('td'); const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = true; checkbox.className = 'preview-select'; checkbox.setAttribute('aria-label', `选择${item.name || '第' + (index + 1) + '行'}`); selectedCell.append(checkbox); row.append(selectedCell);
      ['name', 'weekday', 'start_section', 'end_section', 'room'].forEach(key => { const cell = create('td', '', item[key]); cell.title = '双击编辑'; cell.addEventListener('dblclick', () => editPreviewCell(cell, index, key)); row.append(cell); });
      const action = create('td', 'actions'); const remove = create('button', 'button danger small-button', '删除'); remove.type = 'button'; remove.addEventListener('click', () => { previewRows.splice(index, 1); renderPreview(); }); action.append(remove); row.append(action); body.append(row);
    });
    $('#preview-empty').classList.toggle('hidden', previewRows.length > 0);
    $('#select-all-preview').checked = true;
  }

  function editPreviewCell(cell, index, key) {
    if (cell.isContentEditable) return;
    cell.contentEditable = 'true'; cell.focus();
    const range = document.createRange(); range.selectNodeContents(cell); const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
    const finish = () => { cell.contentEditable = 'false'; let val = cell.textContent.trim(); if (['weekday', 'start_section', 'end_section'].includes(key)) val = Math.max(1, Math.min(key === 'weekday' ? 7 : 8, Number(val) || 1)); previewRows[index][key] = val; cell.textContent = val; };
    cell.addEventListener('blur', finish, { once: true });
    cell.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); cell.blur(); } if (event.key === 'Escape') { cell.textContent = previewRows[index][key]; cell.blur(); } }, { once: true });
  }

  async function confirmImport() {
    const selected = $$('.preview-select').map((box, i) => box.checked ? previewRows[i] : null).filter(Boolean);
    if (!selected.length) { toast('请至少选择一条课程', 'error'); return; }
    const invalid = selected.find(item => !item.name || item.start_section > item.end_section);
    if (invalid) { toast('请填写课程名，并确保结束节次不早于开始节次', 'error'); return; }
    const button = $('#confirm-import'); busy(button, true, '导入中…');
    try { const result = await api('/api/import/confirm', { method: 'POST', body: { courses: selected } }); toast(`成功导入 ${value(result, 'count', 'imported') || selected.length} 条课程`); previewRows = []; renderPreview(); $('#preview-section').classList.add('hidden'); }
    catch (error) { toast(error.message, 'error'); }
    finally { busy(button, false); }
  }

  function initManage() {
    $$('.tab').forEach(tab => tab.addEventListener('click', () => switchTab(tab)));
    $$('[data-open-dialog]').forEach(button => button.addEventListener('click', () => openCreateDialog(button.dataset.openDialog)));
    $$('[data-close-dialog]').forEach(button => button.addEventListener('click', () => button.closest('dialog').close()));
    $('#course-weekday-filter')?.addEventListener('change', renderCourses);
    $('#course-search')?.addEventListener('input', renderCourses);
    $('#course-form')?.addEventListener('submit', saveCourse);
    $('#homework-form')?.addEventListener('submit', saveHomework);
    $('#settings-form')?.addEventListener('submit', saveSettings);
    $('#settings-test-notification')?.addEventListener('click', e => sendTestNotification(e.currentTarget));
    $('#import-json')?.addEventListener('click', importJson);
    Promise.allSettled([loadCourses(), loadHomeworks(), loadSettings()]);
  }

  function switchTab(tab) {
    $$('.tab').forEach(item => { const active = item === tab; item.classList.toggle('active', active); item.setAttribute('aria-selected', String(active)); });
    $$('.tab-panel').forEach(panel => panel.classList.toggle('hidden', panel.id !== tab.dataset.tab));
  }

  async function loadCourses() { try { const data = await api('/api/courses'); courses = (Array.isArray(data) ? data : value(data, 'courses', 'items') || []).map(normalizeCourse); renderCourses(); } catch (error) { toast(`课程加载失败：${error.message}`, 'error'); } }
  async function loadHomeworks() { try { const data = await api('/api/homeworks'); homeworks = Array.isArray(data) ? data : value(data, 'homeworks', 'items') || []; renderHomeworks(); } catch (error) { toast(`作业加载失败：${error.message}`, 'error'); } }

  function renderCourses() {
    const body = $('#courses-body'); if (!body) return; body.replaceChildren();
    const day = $('#course-weekday-filter').value; const query = $('#course-search').value.trim().toLowerCase();
    const filtered = courses.filter(item => (!day || String(item.weekday) === day) && (!query || `${item.name} ${item.room}`.toLowerCase().includes(query)));
    filtered.forEach(item => {
      const row = document.createElement('tr');
      [item.name, weekdays[item.weekday] || item.weekday, `第 ${item.start_section}–${item.end_section} 节`, item.room || '—'].forEach(text => row.append(create('td', '', text)));
      const statusCell = document.createElement('td'); statusCell.append(create('span', `status-pill ${item.is_active ? 'on' : ''}`, item.is_active ? '已启用' : '已停用')); row.append(statusCell);
      row.append(actionCell(() => editCourse(item), () => removeItem('courses', item.id, item.name))); body.append(row);
    });
    $('#courses-empty').classList.toggle('hidden', filtered.length > 0);
  }

  function renderHomeworks() {
    const body = $('#homeworks-body'); if (!body) return; body.replaceChildren();
    homeworks.forEach(item => {
      const row = document.createElement('tr');
      [value(item, 'subject') || '—', value(item, 'content', 'name') || '—', dateText(item.deadline)].forEach(text => row.append(create('td', '', text)));
      const notified = item.notified_day0 ? '当天已提醒' : item.notified_day1 ? '提前一天已提醒' : '待提醒'; const state = document.createElement('td'); state.append(create('span', `status-pill ${item.notified_day0 || item.notified_day1 ? 'on' : ''}`, notified)); row.append(state);
      row.append(actionCell(() => editHomework(item), () => removeItem('homeworks', item.id, value(item, 'content', 'subject')))); body.append(row);
    });
    $('#homeworks-empty').classList.toggle('hidden', homeworks.length > 0);
  }

  function actionCell(editAction, deleteAction) {
    const cell = create('td', 'actions'); const edit = create('button', 'button secondary small-button', '编辑'); const remove = create('button', 'button danger small-button', '删除'); edit.type = remove.type = 'button'; edit.addEventListener('click', editAction); remove.addEventListener('click', deleteAction); cell.append(edit, remove); return cell;
  }

  function openCreateDialog(id) {
    if (id === 'course-dialog') { $('#course-form').reset(); $('#course-id').value = ''; $('#course-active').checked = true; $('#course-dialog-title').textContent = '新增课程'; }
    else { $('#homework-form').reset(); $('#homework-id').value = ''; $('#homework-dialog-title').textContent = '新增作业'; }
    document.getElementById(id).showModal();
  }

  function editCourse(item) { $('#course-id').value = item.id; $('#course-name').value = item.name; $('#course-weekday').value = item.weekday; $('#course-start').value = item.start_section; $('#course-end').value = item.end_section; $('#course-room').value = item.room; $('#course-active').checked = item.is_active; $('#course-dialog-title').textContent = '编辑课程'; $('#course-dialog').showModal(); }
  function editHomework(item) { $('#homework-id').value = item.id; $('#homework-subject').value = value(item, 'subject'); $('#homework-content').value = value(item, 'content', 'name'); $('#homework-deadline').value = String(item.deadline || '').slice(0, 10); $('#homework-dialog-title').textContent = '编辑作业'; $('#homework-dialog').showModal(); }

  async function saveCourse(event) {
    event.preventDefault(); const id = $('#course-id').value; const payload = { name: $('#course-name').value.trim(), weekday: Number($('#course-weekday').value), start_section: Number($('#course-start').value), end_section: Number($('#course-end').value), room: $('#course-room').value.trim(), is_active: $('#course-active').checked };
    if (payload.start_section > payload.end_section) { toast('结束节次不能早于开始节次', 'error'); return; }
    const button = $('button[type=submit]', event.currentTarget); busy(button, true);
    try { await api(id ? `/api/courses/${id}` : '/api/courses', { method: id ? 'PUT' : 'POST', body: payload }); $('#course-dialog').close(); await loadCourses(); toast('课程已保存'); } catch (error) { toast(error.message, 'error'); } finally { busy(button, false); }
  }

  async function saveHomework(event) {
    event.preventDefault(); const id = $('#homework-id').value; const payload = { subject: $('#homework-subject').value.trim(), content: $('#homework-content').value.trim(), deadline: $('#homework-deadline').value };
    const button = $('button[type=submit]', event.currentTarget); busy(button, true);
    try { await api(id ? `/api/homeworks/${id}` : '/api/homeworks', { method: id ? 'PUT' : 'POST', body: payload }); $('#homework-dialog').close(); await loadHomeworks(); toast('作业已保存'); } catch (error) { toast(error.message, 'error'); } finally { busy(button, false); }
  }

  async function removeItem(type, id, label) {
    if (!window.confirm(`确定删除“${label || '此项'}”吗？此操作不可撤销。`)) return;
    try { await api(`/api/${type}/${id}`, { method: 'DELETE' }); type === 'courses' ? await loadCourses() : await loadHomeworks(); toast('已删除'); } catch (error) { toast(error.message, 'error'); }
  }

  function renderSectionTimes(sectionTimes = {}) {
    const root = $('#section-times-grid'); if (!root) return; root.replaceChildren();
    for (let section = 1; section <= 8; section += 1) {
      const item = sectionTimes[String(section)] || {};
      const row = create('div', 'section-time-row');
      row.append(create('strong', '', `第 ${section} 节`));
      const start = document.createElement('input'); start.type = 'time'; start.required = true; start.value = item.start || ''; start.dataset.section = section; start.dataset.kind = 'start'; start.setAttribute('aria-label', `第 ${section} 节开始时间`);
      const separator = create('span', 'muted', '至');
      const end = document.createElement('input'); end.type = 'time'; end.required = true; end.value = item.end || ''; end.dataset.section = section; end.dataset.kind = 'end'; end.setAttribute('aria-label', `第 ${section} 节结束时间`);
      row.append(start, separator, end); root.append(row);
    }
  }

  async function loadSettings() {
    try { const data = await api('/api/settings'); const settings = value(data, 'settings') || data || {}; $('#reminder-minutes').value = value(settings, 'reminder_minutes', 'course_reminder_minutes', 'advance_minutes') || 5; $('#autostart').checked = ['true', '1', true, 1].includes(value(settings, 'autostart', 'auto_start')); renderSectionTimes(settings.section_times || {}); }
    catch (error) { toast(`设置加载失败：${error.message}`, 'error'); }
  }

  async function saveSettings(event) {
    event.preventDefault(); const button = $('button[type=submit]', event.currentTarget); busy(button, true);
    const sectionTimes = {};
    $$('#section-times-grid input').forEach(input => { const section = input.dataset.section; sectionTimes[section] ||= {}; sectionTimes[section][input.dataset.kind] = input.value; });
    try { await api('/api/settings', { method: 'PUT', body: { reminder_minutes: Number($('#reminder-minutes').value), autostart: $('#autostart').checked, section_times: sectionTimes } }); toast('设置已保存'); }
    catch (error) { toast(error.message, 'error'); } finally { busy(button, false); }
  }

  async function importJson() {
    const file = $('#json-file').files[0]; if (!file) { toast('请先选择 JSON 文件', 'error'); return; }
    const button = $('#import-json'); busy(button, true, '导入中…');
    try { const data = JSON.parse(await file.text()); await api('/api/import/json', { method: 'POST', body: data }); await Promise.all([loadCourses(), loadHomeworks(), loadSettings()]); toast('JSON 数据导入成功'); }
    catch (error) { toast(error instanceof SyntaxError ? '文件不是有效的 JSON' : error.message, 'error'); } finally { busy(button, false); }
  }

  document.addEventListener('DOMContentLoaded', () => {
    initNavigation();
    const page = document.body.dataset.page;
    if (page === 'today') initToday();
    if (page === 'import') initImport();
    if (page === 'manage') initManage();
  });
})();
