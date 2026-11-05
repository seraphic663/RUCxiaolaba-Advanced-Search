'use strict';
updateThemeButton();
var aiBusy = false;
var aiConfigured = false;
var aiRequestQueue = Promise.resolve();
function aiElement(id) { return document.getElementById(id); }
function aiNode(tag, text, className) {
  var node = document.createElement(tag);
  if (text !== undefined) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}
function aiRequest(path, payload) {
  if (payload === undefined) return aiFetch(path, payload);
  var task = aiRequestQueue.then(function() { return aiFetch(path, payload); });
  aiRequestQueue = task.catch(function() {});
  return task;
}
async function aiFetch(path, payload) {
  var options = {cache: 'no-store', credentials: 'same-origin'};
  if (payload !== undefined) {
    options.method = 'POST';
    options.headers = {'Content-Type': 'application/json', 'X-CSRF-Token': adminCsrfToken};
    options.body = JSON.stringify(payload);
  }
  var response = await fetch('/api/admin/ai-search' + path, options);
  var data;
  try { data = await response.json(); }
  catch (_) { throw new Error('服务返回了无效响应，请稍后重试'); }
  if (data.csrf_token) adminCsrfToken = data.csrf_token;
  if (!response.ok || !data.ok) throw new Error(data.message || '请求失败，请稍后重试');
  return data;
}
function setAiBusy(value) {
  aiBusy = value;
  document.querySelectorAll('#settings-form button, #query-form button').forEach(function(button) { button.disabled = value; });
}
function showAiSettings(settings) {
  aiConfigured = settings.api_key_set;
  aiElement('ai-base-url').value = settings.base_url;
  aiElement('ai-model').value = settings.model;
  aiElement('search-url').value = settings.search_url;
  aiElement('model-label').textContent = settings.model;
  aiElement('key-status').textContent = settings.api_key_set ? '已配置 Key；填写新值可替换' : '尚未配置 Key';
  aiElement('search-api-key').placeholder = settings.search_api_key_set ? '已配置；留空保留' : '可选，接口无需鉴权时留空';
  aiElement('connection-status').textContent = settings.api_key_set ? 'AI 已配置 · ' + settings.model : '等待配置 AI Key';
  aiElement('connection-status').classList.toggle('ready', settings.api_key_set);
  aiElement('ai-settings').open = !settings.api_key_set;
  refreshAiEstimate();
}
async function refreshAiEstimate() {
  try {
    var result = await aiRequest('/estimate', {query: aiElement('ai-query').value.trim() || '检索费用估算', mode: aiElement('ai-mode').value});
    aiElement('query-estimate').textContent = aiEstimateText(result.estimate) + ' · 按单条 query 预估，最多 4 次 AI 调用';
  } catch (error) { aiElement('query-estimate').textContent = '暂时无法估算：' + error.message; }
}
aiElement('ai-mode').addEventListener('change', refreshAiEstimate);
aiElement('settings-form').addEventListener('submit', async function(event) {
  event.preventDefault();
  if (aiBusy) return;
  setAiBusy(true);
  aiElement('settings-status').textContent = '正在保存…';
  try {
    var result = await aiRequest('/settings', {
      base_url: aiElement('ai-base-url').value.trim(), model: aiElement('ai-model').value.trim(),
      api_key: aiElement('ai-api-key').value.trim(), clear_api_key: aiElement('clear-api-key').checked,
      search_url: aiElement('search-url').value.trim(), search_api_key: aiElement('search-api-key').value.trim(),
      clear_search_api_key: aiElement('clear-search-api-key').checked
    });
    showAiSettings(result.settings);
    ['ai-api-key', 'search-api-key'].forEach(function(id) { aiElement(id).value = ''; });
    ['clear-api-key', 'clear-search-api-key'].forEach(function(id) { aiElement(id).checked = false; });
    aiElement('settings-status').textContent = '设置已保存';
  } catch (error) { aiElement('settings-status').textContent = error.message; }
  finally { setAiBusy(false); }
});
aiElement('test-connection').addEventListener('click', async function() {
  if (aiBusy) return;
  setAiBusy(true);
  aiElement('settings-status').textContent = '正在测试已保存的接口…';
  try {
    var result = await aiRequest('/test', {});
    aiElement('settings-status').textContent = result.message + ' · ' + result.model;
  } catch (error) { aiElement('settings-status').textContent = error.message; }
  finally { setAiBusy(false); }
});
function renderAiAnswer(answer, sources, status) {
  var target = aiElement('ai-answer');
  target.replaceChildren();
  if (!answer) {
    var messages = {
      retrieval_only: '已返回关键词检索结果，可在右侧查看来源。',
      not_configured: '配置 AI Key 后，即可使用查询理解与引用回答。',
      failed: 'AI 暂时无法完成回答，检索来源仍可查看。',
      unverified: 'AI 回答未通过引用检查，请直接核对检索来源。',
      no_answer: '当前没有足够证据回答这个具体问题。不会用其他场所、其他年份或常识代答。',
      no_results: '没有找到可用资料。可以缩短关键词、扩大日期范围或切换到正文 + 评论。'
    };
    target.textContent = messages[status] || '请查看检索来源。';
    return;
  }
  answer.split(/(\[\d+\])/g).forEach(function(part) {
    var match = /^\[(\d+)\]$/.exec(part);
    if (match && Number(match[1]) >= 1 && Number(match[1]) <= sources.length) {
      var link = aiNode('a', part, 'citation');
      link.href = '#source-' + Number(match[1]);
      link.setAttribute('aria-label', '查看来源 ' + Number(match[1]));
      target.append(link);
    } else { target.append(document.createTextNode(part)); }
  });
}
function renderAiSources(sources) {
  var target = aiElement('ai-sources');
  target.replaceChildren();
  aiElement('source-count').textContent = sources.length + ' 篇';
  sources.forEach(function(source) {
    var card = aiNode('article', undefined, 'source-card');
    card.id = 'source-' + source.ref;
    var title = aiNode('div', undefined, 'source-title');
    title.append(aiNode('span', '[' + source.ref + '] 帖子 #' + source.post_id));
    if (source.url && source.url.startsWith('/admin?post=')) {
      var link = aiNode('a', '查看原帖 ↗');
      link.href = source.url; link.target = '_blank'; link.rel = 'noopener';
      title.append(link);
    }
    var states = {full: '已保存详情', list_only: '仅列表，详情待补', partial: '详情不完整', unknown: '完整度未知'};
    card.append(title, aiNode('p', [source.time, source.category, states[source.crawl_status] || '详情不完整'].filter(Boolean).join(' · '), 'source-meta'));
    card.append(aiNode('span', '正文节选', 'source-label'), aiNode('p', source.content || '暂无正文', 'source-body'));
    if (source.comments.length) {
      var details = aiNode('details');
      details.open = true;
      details.append(aiNode('summary', '评论证据与上下文 · ' + source.comments.length + ' 条'));
      source.comments.forEach(function(comment) {
        var item = aiNode('blockquote', undefined, 'source-comment');
        item.append(aiNode('small', [comment.time, '评论 #' + comment.id, comment.parent_id ? '回复评论 #' + comment.parent_id : ''].filter(Boolean).join(' · ')), document.createTextNode(comment.content));
        if (comment.parent_content) item.append(aiNode('small', '所回复的原评论：' + comment.parent_content));
        details.append(item);
      });
      card.append(details);
    }
    if (source.quoted_evidence && source.quoted_evidence.length) {
      var proof = aiNode('details');
      proof.append(aiNode('summary', '回答引用的原文 · ' + source.quoted_evidence.length + ' 处'));
      source.quoted_evidence.forEach(function(citation) {
        var quote = aiNode('blockquote', undefined, 'source-comment');
        quote.append(aiNode('small', (citation.kind === 'comment' ? '评论' : '正文') + ' · ' + citation.evidence_id), document.createTextNode(citation.quote));
        proof.append(quote);
      });
      card.append(proof);
    }
    target.append(card);
  });
}
function renderAiResult(data) {
  aiElement('ai-empty').hidden = true;
  aiElement('ai-results').hidden = false;
  aiElement('result-summary').textContent = '本轮召回 ' + data.stats.candidate_count + ' 篇候选 · 展示 ' + data.sources.length + ' 篇来源 · ' + (data.stats.elapsed_ms / 1000).toFixed(1) + ' 秒 · AI 调用 ' + data.stats.ai_calls + ' 次';
  aiElement('result-summary').append(aiNode('p', '按已返回 token 折算：' + aiCostText(data.stats.cost) + ' · 输入 ' + (data.stats.usage.prompt_tokens || 0) + ' / 输出 ' + (data.stats.usage.completion_tokens || 0) + ' tokens'));
  aiElement('search-warnings').replaceChildren();
  data.warnings.forEach(function(message) { aiElement('search-warnings').append(aiNode('p', message, 'ai-warning')); });
  renderAiAnswer(data.answer, data.sources, data.answer_status);
  renderAiSources(data.sources);
  aiElement('query-chips').replaceChildren();
  data.queries.forEach(function(query) { aiElement('query-chips').append(aiNode('span', query, 'query-chip')); });
  aiElement('search-stages').textContent = data.stats.stages.map(function(stage) { return stage.name + ' ' + (stage.ms / 1000).toFixed(1) + 's'; }).join(' · ');
  if (data.dataset && data.dataset.latest_post_time) {
    aiElement('search-stages').append(aiNode('p', '检索库最新帖子：' + data.dataset.latest_post_time + '；历史用户陈述不等于当前官方信息。'));
  }
}
aiElement('query-form').addEventListener('submit', async function(event) {
  event.preventDefault();
  if (aiBusy) return;
  var query = aiElement('ai-query').value.trim();
  if (!query) { aiElement('ai-query').focus(); return; }
  setAiBusy(true);
  aiElement('search-error').hidden = true;
  aiElement('ai-results').hidden = true;
  aiElement('search-warnings').replaceChildren();
  var usesAi = aiElement('ai-mode').value === 'answer' && aiConfigured;
  aiElement('search-progress').textContent = usesAi ? '正在理解问题、检索资料与生成回答…' : '正在检索资料…';
  aiElement('query-form').setAttribute('aria-busy', 'true');
  try {
    var data = await aiRequest('', {
      query: query, scope: aiElement('ai-scope').value, top_k: Number(aiElement('ai-top-k').value),
      mode: aiElement('ai-mode').value, date_from: aiElement('ai-date-from').value, date_to: aiElement('ai-date-to').value
    });
    renderAiResult(data);
  } catch (error) {
    aiElement('search-error').textContent = error.message;
    aiElement('search-error').hidden = false;
  } finally {
    setAiBusy(false);
    aiElement('query-form').removeAttribute('aria-busy');
    aiElement('search-progress').textContent = '';
  }
});
aiElement('ai-query').addEventListener('keydown', function(event) {
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') aiElement('query-form').requestSubmit();
});
setAiBusy(true);
aiRequest('/settings').then(function(data) { showAiSettings(data.settings); }).catch(function(error) {
  aiElement('connection-status').textContent = error.message;
  aiElement('ai-settings').open = true;
}).finally(function() { setAiBusy(false); });
