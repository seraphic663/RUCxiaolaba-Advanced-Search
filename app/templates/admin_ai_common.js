function aiCostText(cost) {
  if (!cost || !cost.available) return '金额待定（当前接口暂无价格表）';
  if (!cost.usage_complete && !cost.prompt_tokens && !cost.completion_tokens) return '费用未知（接口未返回用量）';
  return '¥' + cost.low_cny.toFixed(4) + '–' + cost.high_cny.toFixed(4) + (cost.usage_complete === false ? '（仅已返回用量）' : '');
}
function aiEstimateText(estimate) {
  return '预计 ' + aiCostText(estimate.cost) + ' · ' + estimate.seconds_low + '–' + estimate.seconds_high + ' 秒';
}
