"""Contracts for actual-time history, daily accounting and read-only refresh."""
import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.crawler_report_service import CrawlerReportService
from storage.crawler_metrics import HISTORY_COLUMNS


class CrawlerReportsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "posts.db"
        with sqlite3.connect(self.db) as c:
            c.execute("CREATE TABLE crawler_run_history(command TEXT,finished_at TEXT,source_calls INTEGER,stats_json TEXT)")
        c.close()
        self.metrics = self.root / "crawler_history.csv"
        self.patch = patch.dict('os.environ', {'CRAWLER_METRICS_PATH': str(self.metrics)})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.legacy = patch('app.services.crawler_report_service.LEGACY_HISTORY', self.root / 'legacy.json')
        self.legacy.start()
        self.addCleanup(self.legacy.stop)
        self.service = CrawlerReportService(self.db)

    def append_point(self, record_id, at, value, method='exact'):
        exists = self.metrics.exists()
        with self.metrics.open('a', encoding='utf-8', newline='') as f:
            w = csv.DictWriter(f, fieldnames=HISTORY_COLUMNS)
            if not exists: w.writeheader()
            w.writerow(dict.fromkeys(HISTORY_COLUMNS, '') | {'record_id': record_id, 'observed_at': at,
                'metric_name': 'id_pending', 'value': value, 'method': method, 'quality': method,
                'source_id': 'runtime:test:' + record_id, 'source_run_id': record_id})

    def run_row(self, command, calls, stats):
        with sqlite3.connect(self.db) as c:
            c.execute('INSERT INTO crawler_run_history VALUES (?,?,?,?)', (command, '2026-11-02T04:00:00+08:00', calls, json.dumps(stats)))
        c.close()

    def test_irregular_observations_are_not_rounded_resampled_or_capped(self):
        for i in range(125): self.append_point(str(i), f'2026-11-02T02:{i//60:02d}:{i%60:02d}+08:00', 15000-i)
        data = self.service.report('2026-11-01T23:59:00+08:00', '2026-11-02T03:00:00+08:00')
        self.assertEqual(len(data['points']), 125)
        self.assertEqual(data['points'][1]['at'], '2026-11-02T02:00:01+08:00')
        self.assertEqual(data['points'][-1]['value'], 14876)
        equivalent = self.service.report('2026-11-01T15:59:00Z', '2026-11-01T19:00:00Z')
        self.assertEqual(data['points'], equivalent['points'])

    def test_refresh_appends_and_preserves_estimate_provenance_without_writes(self):
        self.append_point('one', '2026-11-02T02:01:00+08:00', 10)
        self.append_point('estimate', '2026-11-02T02:02:00+08:00', 11, 'backcast')
        first = self.service.report('2026-11-02', '2026-11-03')
        self.append_point('two', '2026-11-02T02:03:00+08:00', 9)
        before = self.metrics.read_bytes()
        second = self.service.report('2026-11-02', '2026-11-03')
        self.assertEqual(len(first['points']), 2)
        self.assertEqual(len(second['points']), 3)
        self.assertEqual(second['points'][1]['method'], 'backcast')
        self.assertEqual(self.metrics.read_bytes(), before)

    def test_daily_flow_separates_inserts_reopens_retries_and_history(self):
        self.run_row('discover-latest', 2, {'queue_inserted': 8,'queue_reopened':0,'queue_delta_total':8})
        self.run_row('discover-active', 2, {'queue_inserted':2,'queue_reopened':3,'queue_delta_total':5})
        self.run_row('trickle-fill', 10, {'task_type':'id_followup','completed_details':6,'refreshed_details':2,
            'partial_completed':1,'partial_saved':2,'retry_scheduled':1,'queue_delta_total':-9})
        self.run_row('trickle-fill', 25, {'task_type':'history_detail','queue_delta_total':-25})
        d = self.service.report('2026-11-02','2026-11-03')['daily'][0]
        self.assertEqual((d['inflow'],d['removed_pending'],d['net'],d['detail_calls']), (13,9,4,10))
        self.assertEqual(d['normal_success'],7)
        self.assertEqual(d['list2_queue_reopened'],3)

    def test_failed_requests_are_counted_but_missing_outcomes_are_flagged(self):
        self.run_row('trickle-fill', 19, {'scheduler_failed':True,'scheduler_job':'trickle_fill'})
        d = self.service.report('2026-11-02','2026-11-03')['daily'][0]
        self.assertEqual(d['detail_calls'],19)
        self.assertEqual(d['incomplete_runs'],1)

    def test_legacy_unrouted_detail_work_is_not_assumed_to_be_id_work(self):
        self.run_row('trickle-fill', 20, {'queue_delta_total':-20})
        d = self.service.report('2026-11-02','2026-11-03')['daily'][0]
        self.assertEqual(d['unrouted_detail_calls'],20)
        self.assertEqual(d.get('detail_calls',0),0)
        self.assertEqual(d['incomplete_runs'],1)

    def test_omission_recovery_is_local_inflow_without_detail_requests(self):
        self.run_row('repair-omissions',0,{'queue_inserted':4,'queue_reopened':558,'queue_delta_total':562})
        d=self.service.report('2026-11-02','2026-11-03')['daily'][0]
        self.assertEqual((d['inflow'],d['net'],d['omission_restored']),(562,562,562))
        self.assertEqual(d.get('detail_calls',0),0)

    def test_quota_is_grouped_by_request_date_not_rollover_timestamp(self):
        history = {'date':'2026-11-02','recorded_at':'2026-11-03T00:00:00+08:00','reason':'day_rollover',
            'cookie_lanes':{'old':{'detail_calls':500,'new_list_calls':54,'active_list_calls':36,'rate_limited':0}}}
        self.db.with_name('.crawler_quota_history.jsonl').write_text(json.dumps(history)+'\n',encoding='utf-8')
        original = self.db.with_name('.crawler_quota_history.jsonl').read_bytes()
        d = self.service.report('2026-11-02','2026-11-02T23:59:59')['quota_days'][0]
        self.assertEqual((d['date'],d['lane'],d['total']), ('2026-11-02','old',590))
        self.assertEqual(original,self.db.with_name('.crawler_quota_history.jsonl').read_bytes())

    def test_invalid_and_reversed_intervals_are_rejected(self):
        for a,b in [('bad','2026-11-02'),('2026-11-03','2026-11-02')]:
            with self.assertRaises(ValueError): self.service.report(a,b)

    def test_custom_end_includes_the_complete_selected_day_or_minute(self):
        self.append_point('late', '2026-11-02T23:59:50+08:00', 8)
        for end in ['2026-11-02','2026-11-02T23:59']:
            self.assertEqual(len(self.service.report('2026-11-02',end)['points']),1)
