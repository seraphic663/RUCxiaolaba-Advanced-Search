/* Render persisted observations only; opening/refreshing this page never crawls. */
(function () {
  'use strict';
  var preset = '72', report = null, controller = null, focusIndex = 0, shown = [];
  var fromInput = document.getElementById('report-from');
  var toInput = document.getElementById('report-to');
  var svg = document.getElementById('pending-chart');
  var tooltip = document.getElementById('chart-tooltip');
  var initial = new URLSearchParams(location.search);
  if (initial.has('from') || initial.has('to')) {
    preset = 'custom';
    fromInput.value = initial.get('from') || '';
    toInput.value = initial.get('to') || '';
  } else if (['24','48','72','168','720','all'].indexOf(initial.get('hours')) >= 0) {
    preset = initial.get('hours');
  }
  document.querySelectorAll('[data-hours]').forEach(function(b){b.classList.toggle('active',b.dataset.hours===preset);});
  var number = function (n) { return Number(n || 0).toLocaleString('zh-CN'); };
  var sign = function (n) { return (n > 0 ? '+' : '') + number(n); };
  function escape(value) { return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) { return {'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]; }); }
  function beijing(value) { return new Date(new Date(value).getTime() + 8 * 3600000).toISOString().slice(0, 19); }
  function stamp(value) { return beijing(value).replace('T', ' '); }
  function cell(value) { return '<td>' + escape(value) + '</td>'; }
  function rows(id, data, columns, missing) {
    document.getElementById(id).innerHTML = data.length ? data.map(function (r) {
      return '<tr>' + columns.map(function (fn) { return cell(fn(r)); }).join('') + '</tr>';
    }).join('') : '<tr><td colspan="' + columns.length + '" class="empty">' + escape(missing || '这个范围没有记录') + '</td></tr>';
  }
  function range() {
    if (preset === 'custom') return {from: fromInput.value || '1970-01-01T00:00:00+08:00', to: toInput.value};
    var end = new Date();
    return {from: preset === 'all' ? '1970-01-01T00:00:00+08:00' : new Date(end.getTime() - Number(preset) * 3600000).toISOString(), to: end.toISOString()};
  }
  function load() {
    var span = range();
    if (controller) controller.abort();
    controller = new AbortController();
    fetch('/api/admin/crawler-report?' + new URLSearchParams(span), {signal: controller.signal, cache:'no-store'})
      .then(function (response) { return response.json().then(function (data) { if (!response.ok || !data.ok) throw new Error(data.error || ('HTTP ' + response.status)); return data; }); })
      .then(function (data) {
        report = data;
        var view = preset === 'custom' ? {from:fromInput.value,to:toInput.value} : {hours:preset};
        history.replaceState(null,'','/admin/reports?'+new URLSearchParams(view));
        document.getElementById('report-error').hidden = true;
        if (preset !== 'custom') {
          fromInput.value = beijing(preset === 'all' && data.earliest ? data.earliest : data.from).slice(0,16);
          toInput.value = beijing(data.to).slice(0,16);
        }
        document.getElementById('freshness').textContent = '最近观测 ' + (data.latest ? stamp(data.latest) : '暂无') + ' · 页面更新 ' + stamp(data.generated_at) + '（北京时间）';
        render();
        renderDaily();
        document.getElementById('source-note').textContent = '来源：爬虫指标 CSV、运行历史与配额日结。已有 ' + number(data.total_observations) + ' 条 ID 数量记录；精确观测与旧报告按性质区分。每轮结束及空闲时定期记录，页面每分钟读取新记录。';
      }).catch(function (error) {
        if (error.name === 'AbortError') return;
        var box = document.getElementById('report-error');
        box.hidden = false; box.textContent = '更新失败：' + error.message + (report ? '。当前保留上次成功读取的数据。' : '');
      });
  }
  function renderDaily() {
    function date(r) { return r.date + (r.incomplete_runs ? '（' + r.incomplete_runs + ' 轮结果不完整）' : ''); }
    function measured(r, key, signed) { return (r.incomplete_runs ? '已记录 ' : '') + (signed ? sign(r[key]) : number(r[key])); }
    rows('flow-rows', report.daily, [date,
      function(r){return number(r.list1_queue_inserted);}, function(r){return number(r.list2_queue_inserted);},
      function(r){return number(r.list1_queue_reopened)+' / '+number(r.list2_queue_reopened);},
      function(r){return measured(r,'inflow');}, function(r){return number(r.detail_calls)+(r.unrouted_detail_calls?'（另 '+number(r.unrouted_detail_calls)+' 次未分路由）':'');},
      function(r){return measured(r,'removed_pending');}, function(r){return r.incomplete_runs ? '无法完整对账' : sign(r.net);}
    ]);
    rows('detail-rows', report.daily, [date,
      function(r){return measured(r,'completed_details');}, function(r){return measured(r,'refreshed_details');},
      function(r){return measured(r,'normal_success');}, function(r){return measured(r,'misses');},
      function(r){return measured(r,'partial_saved');}, function(r){return measured(r,'retry_scheduled');},
      function(r){return measured(r,'deferred_observations');},
      function(r){return (r.incomplete_runs?'已记录 ':'')+number((r.new_comment_rows||0)+(r.partial_comment_rows_added||0));},
      function(r){return measured(r,'claim_conflicts');}
    ]);
    rows('quota-rows', report.quota_days, [function(r){return r.date+(r.partial?'（今日累计）':'');},
      function(r){return r.lane==='new'?'新 cookie':r.lane==='old'?'旧 cookie':'未分 lane';},
      function(r){return number(r.detail_calls);}, function(r){return number(r.new_list_calls);},
      function(r){return number(r.active_list_calls);}, function(r){return number(r.total);},
      function(r){return number(r.rate_limited);}
    ], '这个范围没有配额日结记录');
  }
  var chart = null;
  function draw() {
    if (!report) return;
    tooltip.hidden = true;
    var width = Math.max(280, svg.clientWidth), height = svg.clientHeight || 390;
    svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
    if (!shown.length) { svg.innerHTML = '<text x="50%" y="50%" text-anchor="middle" fill="currentColor">所选范围没有观测记录</text>'; chart = null; return; }
    var values = shown.map(function(p){return p.value;});
    var low = Math.max(0,Math.floor((Math.min.apply(null,values)-50)/100)*100);
    var high = Math.ceil((Math.max.apply(null,values)+50)/100)*100;
    var times = shown.map(function(p){return new Date(p.at).getTime();});
    var start = new Date(report.from).getTime(), end = new Date(report.to).getTime();
    if (preset === 'all') start = times[0];
    if (end <= start) end = start + 3600000;
    var left = width < 600 ? 56 : 70, right = 24, top = 28, bottom = 46;
    var pw = width-left-right, ph = height-top-bottom;
    function x(t){return left+(t-start)/(end-start)*pw;}
    function y(v){return top+(high-v)/(high-low)*ph;}
    var pieces=[];
    for (var i=0;i<5;i++) {
      var val=low+(high-low)*i/4, yy=y(val);
      pieces.push('<line x1="'+left+'" x2="'+(width-right)+'" y1="'+yy+'" y2="'+yy+'" stroke="var(--ui-border,#e4e8ec)"/>');
      pieces.push('<text x="'+(left-10)+'" y="'+(yy+4)+'" text-anchor="end" fill="var(--ui-muted,#778795)" font-size="11">'+number(Math.round(val))+'</text>');
    }
    var ticks=width<600?3:5;
    for (var j=0;j<ticks;j++) {
      var t=start+(end-start)*j/(ticks-1), label=stamp(t).slice(5,16);
      var xx=x(t), anchor=j===0?'start':j===ticks-1?'end':'middle';
      pieces.push('<text x="'+xx+'" y="'+(height-12)+'" text-anchor="'+anchor+'" fill="var(--ui-muted,#778795)" font-size="11">'+escape(label)+'</text>');
    }
    [true,false].forEach(function(exact){
      var path='', previous=null;
      shown.forEach(function(p,index){
        if ((p.method==='exact')!==exact) {previous=null;return;}
        var time=times[index], separated=previous===null || time-previous>6*3600000;
        path+=(separated?'M':'L')+x(time).toFixed(2)+','+y(p.value).toFixed(2)+' ';
        previous=time;
      });
      pieces.push('<path d="'+path+'" fill="none" stroke="var(--chart-'+(exact?'blue':'orange')+')" stroke-width="2" '+(exact?'':'stroke-dasharray="5 4"')+'/>');
    });
    // All observations are drawn; sparse markers keep dense ranges readable.
    var stride=Math.max(1,Math.floor(shown.length/24));
    shown.forEach(function(p,index){
      var before=shown[index-1],after=shown[index+1];
      var isolated=(!before || (before.method==='exact')!==(p.method==='exact') || times[index]-times[index-1]>6*3600000) &&
        (!after || (after.method==='exact')!==(p.method==='exact') || times[index+1]-times[index]>6*3600000);
      if (isolated || index%stride===0 || index===shown.length-1) pieces.push('<circle cx="'+x(times[index])+'" cy="'+y(p.value)+'" r="3.5" fill="var(--ui-surface,#fff)" stroke="var(--chart-'+(p.method==='exact'?'blue':'orange')+')" stroke-width="1.7"/>');
    });
    [0,shown.length-1].forEach(function(index){
      var xx=x(times[index]), anchor=index===0?'start':'end';
      pieces.push('<text x="'+xx+'" y="'+(y(shown[index].value)-13)+'" text-anchor="'+anchor+'" font-size="13" font-weight="600" fill="currentColor">'+number(shown[index].value)+'</text>');
    });
    pieces.push('<circle id="chart-focus" r="5" fill="var(--ui-surface,#fff)" stroke="var(--chart-blue)" stroke-width="2" visibility="hidden"/>');
    svg.innerHTML=pieces.join(''); chart={x:x,y:y,times:times,left:left,pw:pw,start:start,end:end,width:width,height:height};
  }
  function render() {
    if (!report) return;
    var include=document.getElementById('include-legacy').checked;
    shown=report.points.filter(function(p){return include || p.method==='exact';});
    var a=shown[0], b=shown[shown.length-1], delta=a?b.value-a.value:0;
    var exactCount=shown.filter(function(p){return p.method==='exact';}).length;
    var cards=[['起点积存',a?number(a.value):'—',a?stamp(a.at).slice(5,16)+(a.method==='exact'?'':' · 旧报告'):'暂无记录'],
      ['终点积存',b?number(b.value):'—',b?stamp(b.at).slice(5,16)+(b.method==='exact'?'':' · 旧报告'):'暂无记录'],
      ['区间净变化',a?sign(delta):'—',a&&a.value?(delta/a.value*100).toFixed(2)+'%'+(a.method==='exact'&&b.method==='exact'?'':' · 含旧报告口径'):''],
      ['展示记录数',number(shown.length),number(exactCount)+' 次精确观测'+(exactCount<shown.length?' / '+number(shown.length-exactCount)+' 条旧报告':'')]];
    document.getElementById('report-cards').innerHTML=cards.map(function(c,i){return '<div class="card '+(i===2?'delta':'')+'"><small>'+escape(c[0])+'</small><strong>'+escape(c[1])+'</strong><span>'+escape(c[2])+'</span></div>';}).join('');
    document.getElementById('chart-caption').textContent= a ? '实际首末观测：'+stamp(a.at)+' — '+stamp(b.at)+'。'+(shown.some(function(p){return p.method!=='exact';})?'橙色记录来自旧报告，保留其估算或历史导入口径。':'当前显示均为精确观测。') : '可扩大时间范围查看已有记录。';
    rows('observation-rows',shown,[function(p){return stamp(p.at);},function(p){return number(p.value);},function(p){return p.method==='exact'?'精确观测':p.quality==='estimated'?'旧报告 · 估算':'旧报告 · 历史口径';},function(p){return p.source+(p.run_id?' / run '+p.run_id:'');}]);
    draw();
  }
  function focusPoint(index) {
    if (!chart || !shown.length) return;
    focusIndex=Math.max(0,Math.min(shown.length-1,index));
    var p=shown[focusIndex], xx=chart.x(chart.times[focusIndex]), yy=chart.y(p.value);
    var circle=document.getElementById('chart-focus');circle.setAttribute('cx',xx);circle.setAttribute('cy',yy);circle.setAttribute('visibility','visible');
    tooltip.textContent=stamp(p.at)+'（北京时间）\n待查 '+number(p.value)+' 条 · '+(p.method==='exact'?'精确观测':p.quality==='estimated'?'旧报告估算':'旧报告历史口径')+'\n'+p.source+(p.run_id?' / run '+p.run_id:'');
    tooltip.hidden=false;
    tooltip.style.left=Math.max(0,Math.min(chart.width-tooltip.offsetWidth,xx+14))+'px';
    tooltip.style.top=Math.max(0,Math.min(chart.height-tooltip.offsetHeight,yy+16))+'px';
  }
  svg.addEventListener('pointermove',function(event){
    if (!chart) return;
    var rect=svg.getBoundingClientRect(), time=chart.start+(event.clientX-rect.left-chart.left)/chart.pw*(chart.end-chart.start);
    var lo=0,hi=chart.times.length-1;
    while(lo<hi){var mid=Math.floor((lo+hi)/2);if(chart.times[mid]<time)lo=mid+1;else hi=mid;}
    if(lo>0 && Math.abs(chart.times[lo-1]-time)<Math.abs(chart.times[lo]-time))lo--;
    focusPoint(lo);
  });
  svg.addEventListener('pointerleave',function(){tooltip.hidden=true;var dot=document.getElementById('chart-focus');if(dot)dot.setAttribute('visibility','hidden');});
  svg.addEventListener('keydown',function(event){if(event.key==='ArrowRight'||event.key==='ArrowLeft'){event.preventDefault();focusPoint(focusIndex+(event.key==='ArrowRight'?1:-1));}});
  document.querySelectorAll('[data-hours]').forEach(function(button){button.addEventListener('click',function(){preset=button.dataset.hours;document.querySelectorAll('[data-hours]').forEach(function(b){b.classList.toggle('active',b===button);});load();});});
  [fromInput,toInput].forEach(function(input){input.addEventListener('change',function(){preset='custom';document.querySelectorAll('[data-hours]').forEach(function(b){b.classList.remove('active');});load();});});
  document.getElementById('refresh-now').addEventListener('click',load);
  document.getElementById('include-legacy').addEventListener('change',render);
  new ResizeObserver(draw).observe(svg);
  setInterval(function(){if(!document.hidden && document.getElementById('auto-refresh').checked)load();},60000);
  document.addEventListener('visibilitychange',function(){if(!document.hidden && document.getElementById('auto-refresh').checked)load();});
  load();
})();
