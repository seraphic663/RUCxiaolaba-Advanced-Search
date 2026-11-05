'use strict';
updateThemeButton();
var benchCatalog = null, benchDetail = null, benchSelected = '', benchPoll = null, benchStarting = false, benchLoadEpoch = 0;
var benchPostQueue = Promise.resolve();
function benchEl(id) { return document.getElementById(id); }
function benchNode(tag, text, className) {
  var node = document.createElement(tag);
  if (text !== undefined) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}
function benchRequest(path, payload) {
  if (payload === undefined) return benchFetch(path, payload);
  var task = benchPostQueue.then(function() { return benchFetch(path, payload); });
  benchPostQueue = task.catch(function() {});
  return task;
}
async function benchFetch(path, payload) {
  var options = {cache: 'no-store', credentials: 'same-origin'};
  if (payload !== undefined) {
    options.method = 'POST';
    options.headers = {'Content-Type': 'application/json', 'X-CSRF-Token': adminCsrfToken};
    options.body = JSON.stringify(payload);
  }
  var response = await fetch('/api/admin/ai-search/bench' + path, options);
  var data;
  try { data = await response.json(); } catch (_) { throw new Error('服务返回无效响应'); }
  if (data.csrf_token) adminCsrfToken = data.csrf_token;
  if (!response.ok || !data.ok) throw new Error(data.message || '请求失败');
  return data;
}
function benchError(error) {
  benchEl('bench-error').textContent = error.message;
  benchEl('bench-error').hidden = false;
}
function benchState(state) { return {running:'运行中', queued:'排队中', complete:'已完成', failed:'运行失败', interrupted:'服务重启中断，需手动重测'}[state] || state; }
function benchCanRun() { return benchCatalog && benchCatalog.configured && benchCatalog.local_retrieval && !benchCatalog.active_id && !benchStarting; }
function benchFailedIds() {
  if (!benchDetail || !benchCatalog) return [];
  var current = new Set(benchCatalog.manifest.cases.map(function(c) { return c.id; }));
  return Array.from(new Set(benchDetail.runs.filter(function(row) { return !row.check.passed && current.has(row.case.id); }).map(function(row) { return row.case.id; })));
}
function benchButtons() {
  var enabled = benchCanRun();
  benchEl('bench-run-all').disabled = !enabled;
  benchEl('bench-run-failed').disabled = !enabled || !benchFailedIds().length;
  benchEl('bench-concurrency').disabled = !!(benchCatalog && benchCatalog.active_id) || benchStarting;
  document.querySelectorAll('[data-retest]').forEach(function(button) { button.disabled = !enabled; });
}
function benchShowEstimate(estimate) {
  benchEl('bench-estimate').textContent = aiEstimateText(estimate) + ' · ' + estimate.query_count + ' 条 query / 并发 ' + estimate.concurrency + (estimate.basis === 'previous_run' ? ' · 根据历史用量与逐条耗时估算' : ' · 暂无可比历史，使用保守估算');
  if (estimate.concurrency > estimate.baseline_concurrency) benchEl('bench-estimate').append('（已放宽高并发耗时上限）');
}
async function benchEstimate() {
  try {
    var result = await benchRequest('/estimate', {concurrency: Number(benchEl('bench-concurrency').value)});
    benchShowEstimate(result.estimate);
  } catch (error) { benchError(error); }
}
function benchRenderSummary(summary) {
  var target = benchEl('bench-run-summary'); target.replaceChildren();
  if (!summary) { target.append(benchNode('p', '尚无评测结果。重测前会完成离线源记录核查。')); return; }
  target.append(benchNode('h2', benchState(summary.status) + ' · ' + summary.passed + ' / ' + summary.completed + ' 项通过自动门槛'));
  if (summary.status === 'running') {
    var progress = benchNode('progress', undefined, 'bench-progress'); progress.max = summary.total; progress.value = summary.completed;
    progress.setAttribute('aria-label', '已完成 ' + summary.completed + ' / ' + summary.total); target.append(progress);
  }
  target.append(benchNode('p', '进度 ' + summary.completed + ' / ' + summary.total + ' · 失败 ' + summary.failed + ' · 并发 ' + summary.concurrency + ' · AI 请求 ' + summary.ai_calls + ' 次'));
  var duration = summary.wall_elapsed_ms !== null ? '整轮已用 ' + (summary.wall_elapsed_ms / 1000).toFixed(1) + ' 秒' : '逐条耗时累计 ' + (summary.query_elapsed_ms / 1000).toFixed(1) + ' 秒（旧轮次未记录整轮墙钟）';
  target.append(benchNode('p', duration + ' · 按已返回 token 折算 ' + aiCostText(summary.cost)));
  target.append(benchNode('p', '输入 ' + (summary.usage.prompt_tokens || 0) + ' / 输出 ' + (summary.usage.completion_tokens || 0) + ' tokens · 本轮检索库最新帖子 ' + (summary.source.latest_post_time || '未知'), 'note'));
  if (!summary.manifest_matches) target.append(benchNode('p', '该轮使用其他版本的用例清单，请独立解读分数。', 'ai-warning'));
  if (summary.estimate) target.append(benchNode('p', '启动前预估：' + aiEstimateText(summary.estimate), 'note'));
  if (summary.error_code) target.append(benchNode('p', '运行器异常：' + summary.error_code + '；已完成的结果已保留。', 'ai-warning'));
}
var benchFailures = {
  pipeline_failure_or_unsupported_generation_blocked: '接口/流程失败，或生成内容未通过证据检查',
  answerable_query_abstained: '有证据的问题未答出', reference_evidence_not_retrieved: '未召回必须命中的历史记录',
  reference_evidence_not_cited: '未引用必须使用的历史记录', expected_answer_points_missing: '缺少必要答案要点',
  citation_verification_unavailable: '缺少引用核验结果', missing_or_wrongly_owned_citation: '引用缺失或原文归属不符',
  cited_evidence_outside_expected_question_scope: '引用证据不在预期问题范围内',
  unanswerable_query_received_factual_answer: '无足够证据的问题生成了事实答案', not_a_valid_abstention: '未形成有效拒答',
  known_off_topic_source_shown: '展示了已知无关来源', request_failed: '本次请求失败'
};
function benchRenderCases() {
  var target = benchEl('bench-cases');
  var expanded = new Set(Array.from(target.querySelectorAll('details[open][data-case-row]')).map(function(node) { return node.dataset.caseRow; }));
  target.replaceChildren();
  var rows = benchDetail ? benchDetail.runs.slice() : [];
  if (benchDetail && benchDetail.summary.status === 'running') {
    var completed = new Set(rows.map(function(row) { return row.case.id; }));
    benchCatalog.manifest.cases.forEach(function(c) {
      if (!completed.has(c.id) && (!benchDetail.case_ids || benchDetail.case_ids.indexOf(c.id) >= 0)) rows.push({case:c, pending:true});
    });
  } else if (!benchDetail) rows = benchCatalog.manifest.cases.map(function(c) { return {case:c, pending:true}; });
  rows.sort(function(a,b) { return a.case.id.localeCompare(b.case.id) || (a.repeat || 1) - (b.repeat || 1); });
  rows.forEach(function(row) {
    var c = row.case, result = row.result || {}, stats = result.stats || {}, check = row.check || {};
    var key = c.id + '-' + (row.repeat || 1);
    var card = benchNode('details', undefined, 'bench-case'); card.dataset.caseRow = key; card.open = expanded.has(key);
    var summary = benchNode('summary');
    summary.append(benchNode('span', c.id, 'bench-case-number'), benchNode('span', c.query, 'bench-case-query'));
    summary.append(benchNode('span', c.answerable ? '有答案' : '应拒答', 'bench-badge'));
    var running = benchDetail && benchDetail.summary.running_case_ids.indexOf(c.id) >= 0;
    summary.append(benchNode('span', row.pending ? (running ? '处理中' : '待运行') : (check.passed ? '通过' : '失败'), 'bench-badge ' + (row.pending ? '' : check.passed ? 'pass' : 'fail')));
    var body = benchNode('div', undefined, 'bench-case-body');
    body.append(benchNode('p', (c.domain || '') + ' · ' + (c.intent || '') + (row.repeat ? ' · 第 ' + row.repeat + ' 次' : ''), 'bench-meta'));
    if (c.date_from || c.date_to) body.append(benchNode('p', '限定帖子发布时间：' + (c.date_from || '不限') + ' 至 ' + (c.date_to || '不限'), 'bench-meta'));
    body.append(benchNode('h3', c.answerable ? '参考答案' : '拒答理由'), benchNode('p', c.reference_answer));
    if (!row.pending) {
      body.append(benchNode('h3', '实际回答 · ' + (result.answer_status || '未知')));
      var answer = benchNode('div', undefined, 'bench-answer');
      (result.answer || '未给出事实答案').split(/(\[\d+\])/g).forEach(function(part) {
        var match = /^\[(\d+)\]$/.exec(part);
        if (match && (result.sources || []).some(function(source) { return source.ref === Number(match[1]); })) {
          var ref = benchNode('a', part, 'citation'); ref.href = '#bench-source-' + key + '-' + match[1];
          ref.addEventListener('click', function() { proof.open = true; }); answer.append(ref);
        } else answer.append(document.createTextNode(part));
      });
      body.append(answer);
      body.append(benchNode('p', ((stats.elapsed_ms || 0) / 1000).toFixed(1) + ' 秒 · AI ' + (stats.ai_calls || 0) + ' 次 · ' + aiCostText(stats.cost), 'bench-meta'));
      if (!check.passed) body.append(benchNode('p', (check.failures || []).map(function(code) { return benchFailures[code] || code; }).join('；'), 'ai-warning'));
      (result.warnings || []).forEach(function(warning) { body.append(benchNode('p', warning, 'note')); });
      var proof = benchNode('details'); proof.append(benchNode('summary', '引用原文与检索来源 · ' + (result.sources || []).length + ' 篇'));
      (result.sources || []).forEach(function(source) {
        var evidence = benchNode('div', undefined, 'bench-evidence');
        evidence.id = 'bench-source-' + key + '-' + source.ref;
        var link = benchNode('a', '[' + source.ref + '] 帖子 #' + source.post_id + ' ↗');
        link.href = '/admin?post=' + encodeURIComponent(source.post_id); link.target = '_blank'; link.rel = 'noopener';
        evidence.append(link, benchNode('small', source.time || ''), benchNode('p', source.content || '暂无正文'));
        (source.quoted_evidence || []).forEach(function(citation) {
          evidence.append(benchNode('small', citation.evidence_id), benchNode('p', '“' + citation.quote + '”'));
        });
        proof.append(evidence);
      });
      body.append(proof);
    }
    if (benchCatalog.manifest.cases.some(function(current) { return current.id === c.id; })) {
      var actions = benchNode('div', undefined, 'settings-actions');
      var button = benchNode('button', '单条重测 · 真实 API'); button.type = 'button'; button.dataset.retest = c.id;
      button.addEventListener('click', function() { benchStart([c.id]); });
      actions.append(button); body.append(actions);
    }
    card.append(summary, body); target.append(card);
  });
  benchButtons();
}
async function benchLoad(runId, options) {
  clearTimeout(benchPoll);
  var epoch = ++benchLoadEpoch;
  var data = await benchRequest('');
  if (epoch !== benchLoadEpoch) return;
  benchCatalog = data;
  benchEl('bench-description').textContent = '固定 v' + data.manifest.version + ' · ' + data.manifest.cases.length + ' 条 query · ' + data.manifest.cases.filter(function(c) { return c.answerable; }).length + ' 条有答案 / ' + data.manifest.cases.filter(function(c) { return !c.answerable; }).length + ' 条应拒答';
  benchEl('bench-model').textContent = data.model;
  benchEl('bench-history').replaceChildren();
  var latestFull = data.history.find(function(summary) { return summary.manifest_matches && summary.total >= data.manifest.cases.length; });
  var chosen = runId || benchSelected || data.active_id || (latestFull && latestFull.id) || (data.history[0] && data.history[0].id) || '';
  data.history.forEach(function(summary) {
    var option = benchNode('option', summary.created_at_beijing + ' · ' + summary.label + ' · ' + summary.passed + '/' + summary.completed + ' · ' + benchState(summary.status));
    option.value = summary.id; benchEl('bench-history').append(option);
  });
  if (!data.history.length) benchEl('bench-history').append(benchNode('option', '尚无历史结果'));
  benchSelected = chosen; benchEl('bench-history').value = chosen;
  var detail = chosen ? await benchRequest('?run_id=' + encodeURIComponent(chosen)) : null;
  if (epoch !== benchLoadEpoch) return;
  benchDetail = detail;
  if (chosen) history.replaceState(null, '', '?run=' + encodeURIComponent(chosen));
  benchRenderSummary(benchDetail && benchDetail.summary); benchRenderCases();
  if (!options || !options.poll) {
    if (benchEl('bench-concurrency').value === '18') benchShowEstimate(data.estimate);
    else await benchEstimate();
  }
  benchEl('bench-operation').textContent = !data.configured ? '请先在自然语言检索页配置 AI Key。' : !data.local_retrieval ? '固定 Bench 仅支持本站本地检索，请先切回本地检索。' : data.active_id ? '后台评测运行中，每 2.5 秒更新；刷新页面可继续查看。' : '每次重测均保留为独立轮次。';
  if (data.active_id) benchPoll = setTimeout(function() { benchLoad(benchSelected, {poll:true}).catch(benchError); }, 2500);
}
async function benchStart(caseIds) {
  if (!benchCanRun()) return;
  benchStarting = true; benchButtons(); benchEl('bench-error').hidden = true;
  benchEl('bench-operation').textContent = '正在核查本地源记录并创建新轮次…';
  try {
    var requestId = window.crypto && window.crypto.randomUUID ? window.crypto.randomUUID() : Date.now() + '-' + Math.random().toString(36).slice(2);
    var result = await benchRequest('/run', {case_ids:caseIds || [], concurrency:Number(benchEl('bench-concurrency').value), request_id:requestId});
    benchSelected = result.run.id; await benchLoad(result.run.id);
  } catch (error) { benchError(error); }
  finally { benchStarting = false; benchButtons(); }
}
benchEl('bench-run-all').addEventListener('click', function() { benchStart([]); });
benchEl('bench-run-failed').addEventListener('click', function() { var ids = benchFailedIds(); if (ids.length) benchStart(ids); });
benchEl('bench-history').addEventListener('change', function() { benchSelected = this.value; benchLoad(this.value).catch(benchError); });
benchEl('bench-concurrency').addEventListener('change', benchEstimate);
benchEl('bench-refresh').addEventListener('click', function() { benchLoad(benchSelected).catch(benchError); });
benchLoad(new URLSearchParams(location.search).get('run') || '').catch(benchError);
