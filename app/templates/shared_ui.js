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
