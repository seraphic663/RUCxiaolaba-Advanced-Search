function updateThemeButton() {
  var dark = document.documentElement.getAttribute('data-theme') === 'dark';
  document.querySelectorAll('.theme-icon').forEach(function(icon) {
    icon.textContent = dark ? '☀' : '☾';
  });
  document.querySelectorAll('.theme-toggle').forEach(function(button) {
    var label = dark ? '切换浅色模式' : '切换深色模式';
    button.setAttribute('aria-label', label);
    button.setAttribute('title', label);
  });
}

function toggleTheme() {
  var dark = document.documentElement.getAttribute('data-theme') === 'dark';
  var next = dark ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('rucxlb-theme', next);
  updateThemeButton();
}

var SETTINGS_KEY = 'rucxlb-ui-settings';
var DEFAULT_SETTINGS = {
  pageSize: 50,
  truncateLen: 140,
  commentLimit: 50,
  showStats: true,
  compactMode: false
};

function loadSettings() {
  try {
    var raw = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}');
    return Object.assign({}, DEFAULT_SETTINGS, raw);
  } catch (e) {
    return Object.assign({}, DEFAULT_SETTINGS);
  }
}

var uiSettings = loadSettings();

function normalizeSettings(settings) {
  settings.pageSize = [20, 50, 100].indexOf(Number(settings.pageSize)) >= 0 ? Number(settings.pageSize) : 50;
  settings.truncateLen = [0, 100, 140, 200].indexOf(Number(settings.truncateLen)) >= 0 ? Number(settings.truncateLen) : 140;
  settings.commentLimit = [0, 20, 50].indexOf(Number(settings.commentLimit)) >= 0 ? Number(settings.commentLimit) : 50;
  settings.showStats = settings.showStats !== false;
  settings.compactMode = settings.compactMode === true;
  return settings;
}

function applySettings() {
  uiSettings = normalizeSettings(uiSettings);
  PAGE_SIZE = uiSettings.pageSize;
  TRUNCATE_LEN = uiSettings.truncateLen;
  document.documentElement.classList.toggle('compact-mode', uiSettings.compactMode);
  document.documentElement.classList.toggle('hide-stats', !uiSettings.showStats);
}

function openSettings() {
  uiSettings = normalizeSettings(loadSettings());
  document.getElementById('set-page-size').value = String(uiSettings.pageSize);
  document.getElementById('set-truncate-len').value = String(uiSettings.truncateLen);
  document.getElementById('set-comment-limit').value = String(uiSettings.commentLimit);
  document.getElementById('set-show-stats').checked = uiSettings.showStats;
  document.getElementById('set-compact-mode').checked = uiSettings.compactMode;
  document.getElementById('settings-backdrop').classList.add('open');
}

function closeSettings() {
  document.getElementById('settings-backdrop').classList.remove('open');
}

function saveSettings() {
  uiSettings = normalizeSettings({
    pageSize: Number(document.getElementById('set-page-size').value),
    truncateLen: Number(document.getElementById('set-truncate-len').value),
    commentLimit: Number(document.getElementById('set-comment-limit').value),
    showStats: document.getElementById('set-show-stats').checked,
    compactMode: document.getElementById('set-compact-mode').checked
  });
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(uiSettings));
  applySettings();
  closeSettings();
  if (typeof onUiSettingsSaved === 'function') onUiSettingsSaved();
}

function getDateFilterParams() {
  var root = document.getElementById('date-filter');
  if (!root) return [];
  var from = root.dataset.from || '';
  var to = root.dataset.to || '';
  if (from || to) {
    var range = [];
    if (from) range.push('from=' + encodeURIComponent(from));
    if (to) range.push('to=' + encodeURIComponent(to));
    return range;
  }
  var preset = root.dataset.preset || '';
  return preset ? ['date=' + encodeURIComponent(preset)] : [];
}

function setupDateFilter(onChange) {
  var root = document.getElementById('date-filter');
  var panel = document.getElementById('date-range-panel');
  if (!root || !panel) return;

  var trigger = document.getElementById('date-filter-trigger');
  var summary = document.getElementById('date-filter-summary');
  var fromInput = document.getElementById('date-from');
  var toInput = document.getElementById('date-to');
  var error = document.getElementById('date-range-error');

  function closePanel(restoreFocus) {
    panel.hidden = true;
    trigger.setAttribute('aria-expanded', 'false');
    if (restoreFocus) trigger.focus();
  }

  function updateView() {
    var from = root.dataset.from || '';
    var to = root.dataset.to || '';
    var preset = root.dataset.preset || '';
    if (from && to) summary.textContent = from + ' – ' + to;
    else if (from) summary.textContent = from + ' 起';
    else if (to) summary.textContent = '截至 ' + to;
    else {
      var active = panel.querySelector('[data-date-preset="' + preset + '"]');
      summary.textContent = active ? active.textContent.trim() : '不限时间';
    }

    panel.querySelectorAll('[data-date-preset]').forEach(function(button) {
      var activePreset = button.getAttribute('data-date-preset') || '';
      var active = !from && !to && activePreset === preset;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
  }

  trigger.addEventListener('click', function() {
    var opening = panel.hidden;
    if (opening) {
      fromInput.value = root.dataset.from || '';
      toInput.value = root.dataset.to || '';
      error.textContent = '';
    }
    panel.hidden = !opening;
    trigger.setAttribute('aria-expanded', opening ? 'true' : 'false');
  });

  panel.querySelectorAll('[data-date-preset]').forEach(function(button) {
    button.addEventListener('click', function() {
      root.dataset.from = '';
      root.dataset.to = '';
      root.dataset.preset = button.getAttribute('data-date-preset') || '';
      fromInput.value = '';
      toInput.value = '';
      error.textContent = '';
      updateView();
      closePanel(false);
      if (typeof onChange === 'function') onChange();
    });
  });

  document.getElementById('date-range-apply').addEventListener('click', function() {
    var from = fromInput.value;
    var to = toInput.value;
    error.textContent = '';
    if (!from && !to) {
      error.textContent = '请至少选择一个日期';
      return;
    }
    if (from && to && from > to) {
      error.textContent = '开始日期不能晚于结束日期';
      return;
    }
    root.dataset.from = from;
    root.dataset.to = to;
    root.dataset.preset = '';
    updateView();
    closePanel(false);
    if (typeof onChange === 'function') onChange();
  });

  document.getElementById('date-range-clear').addEventListener('click', function() {
    root.dataset.from = '';
    root.dataset.to = '';
    root.dataset.preset = '';
    fromInput.value = '';
    toInput.value = '';
    error.textContent = '';
    updateView();
    closePanel(false);
    if (typeof onChange === 'function') onChange();
  });

  document.getElementById('date-range-close').addEventListener('click', function() {
    closePanel(true);
  });

  document.addEventListener('click', function(event) {
    if (!panel.hidden && !root.contains(event.target) && !panel.contains(event.target)) {
      closePanel(false);
    }
  });
  document.addEventListener('keydown', function(event) {
    if (event.key === 'Escape' && !panel.hidden) closePanel(true);
  });
  updateView();
}
