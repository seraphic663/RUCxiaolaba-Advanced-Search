import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crawler.id_ledger import mark_detail_finished, record_list_page
from crawler.service import CrawlerService
from storage.crawler_omissions import crawler_omission_snapshot, find_crawler_omissions, requeue_crawler_omissions
from storage.post_writer import SQLitePostStore
from tests.test_crawler_service import FakeClient, detail


class CrawlerOmissionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'posts.db'
        self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('Upstream blocked'))
        self.network.start()
        self.addCleanup(self.network.stop)
        with SQLitePostStore(self.db) as store:
            store.init_schema()

    def article(self, pid, count):
        return {'id':pid,'create_time':'2026-06-25 10:00:00','update_time':'2026-06-25 11:00:00','count_comment':count}

    def service(self, client):
        service = CrawlerService(db_path=self.db, cookie='test', lock_timeout=2)
        service.client = lambda: client
        return service

    def seed_complete(self, pid):
        client = FakeClient({}, {pid:detail(pid,1)})
        parsed = self.service(client).fetch_detail_with_error(client,pid)[0]
        with SQLitePostStore(self.db) as store:
            store.upsert_post(*parsed)
            store.enqueue_crawler_candidate(post_id=pid,source='lists',priority=10,
                list_create_time='2026-06-25 10:00:00',list_update_time='2026-06-25 10:00:00',
                list_comment_count=1,db_comment_count=1,reason='new_post')
            store.finish_crawler_queue_detail(pid,detail_comment_count=1,retry_delay_seconds=3600,max_same_observation_attempts=2)
            mark_detail_finished(store.conn,pid,status='succeeded',comment_count=1)
            store.conn.execute("INSERT OR REPLACE INTO ledger_state VALUES ('lists2_baseline_ready','1','2026-06-25')")
            store.conn.commit()

    def observe(self, pid, endpoint='lists'):
        return self.service(FakeClient({1:[self.article(pid,2)]},{})).discover_queue(
            command='discover-latest' if endpoint=='lists' else 'discover-active',endpoint=endpoint,
            since='2026-06-25 00:00:00',max_pages=1,old_page_threshold=1,min_pages=1,
            no_action_page_threshold=1,dry_run=False,write_stubs=True,min_delay=0,max_delay=0)

    def test_both_feeds_reopen_a_real_completed_detail_observation(self):
        for pid,endpoint in [('205','lists'),('207','lists2')]:
            with self.subTest(endpoint=endpoint):
                self.seed_complete(pid)
                stats = self.observe(pid,endpoint)
                self.assertEqual(stats['queue_reopened'],1)
                with SQLitePostStore(self.db) as store:
                    q=store.conn.execute('SELECT status,last_attempt_list_comment_count FROM crawler_queue WHERE post_id=?',(pid,)).fetchone()
                    self.assertEqual(tuple(q),('pending',1))
                    self.assertEqual(store.get_post_crawl_snapshot(pid)['crawl_status'],'partial')

    def test_list1_recovers_counter_already_overwritten_by_a_stub(self):
        self.seed_complete('205')
        with SQLitePostStore(self.db) as store:
            store.upsert_list_stub(self.article('205',2),source='lists')
        self.assertEqual(self.observe('205')['queue_reopened'],1)

    def test_same_observation_deferred_work_keeps_its_retry_limit(self):
        self.seed_complete('205')
        with SQLitePostStore(self.db) as store:
            store.enqueue_crawler_candidate(post_id='205',source='lists',priority=0,
                list_create_time='2026-06-25 10:00:00',list_update_time='2026-06-25 11:00:00',
                list_comment_count=2,db_comment_count=1,reason='comment_changed')
            store.upsert_list_stub(self.article('205',2),source='lists')
            self.assertEqual(store.finish_crawler_queue_detail('205',detail_comment_count=1,
                retry_delay_seconds=3600,max_same_observation_attempts=1),'deferred')
        self.observe('205')
        with SQLitePostStore(self.db) as store:
            q=store.conn.execute("SELECT status,same_observation_attempts FROM crawler_queue WHERE post_id='205'").fetchone()
            self.assertEqual(tuple(q),('deferred',1))

    def seed_lost(self, store):
        record_list_page(store.conn,run_id='orphan',endpoint='lists',page=1,
            articles=[self.article('90',0)],observed_at='2026-06-25T11:00:00+08:00')
        store.upsert_list_stub(self.article('205',2),source='lists')
        record_list_page(store.conn,run_id='growth',endpoint='lists',page=1,
            articles=[self.article('205',2)],observed_at='2026-06-25T11:00:00+08:00')
        store.conn.commit()

    def test_explicit_repair_is_rechecked_idempotent_and_uses_actual_comments(self):
        self.seed_complete('205')
        with SQLitePostStore(self.db) as store:
            self.seed_lost(store)
            before=find_crawler_omissions(store.conn,post_ids=['90','205'])
            self.assertEqual({r['kind'] for r in before},{'missing_post','missing_refresh'})
            result=requeue_crawler_omissions(store,['90','205'])
            self.assertEqual({r['action'] for r in result},{'inserted','reopened'})
            self.assertEqual(requeue_crawler_omissions(store,['90','205']),[])
            q=store.conn.execute("SELECT status,last_attempt_list_comment_count,db_comment_count,task_type FROM crawler_queue WHERE post_id='205'").fetchone()
            self.assertEqual(tuple(q),('pending',1,1,'id_followup'))

    def test_same_observation_and_full_posts_are_not_repaired(self):
        self.seed_complete('205')
        with SQLitePostStore(self.db) as store:
            self.seed_lost(store)
            store.conn.execute("UPDATE crawler_queue SET last_attempt_list_comment_count=2 WHERE post_id='205'")
            store.conn.commit()
            self.assertEqual(requeue_crawler_omissions(store,['205']),[])
            store.conn.execute("UPDATE posts SET crawl_status='full' WHERE id='205'")
            store.conn.execute("DELETE FROM crawler_queue WHERE post_id='205'")
            store.conn.commit()
            self.assertEqual(find_crawler_omissions(store.conn,post_ids=['205']),[])

    def test_report_is_read_only_and_excludes_an_unfinished_list_page(self):
        self.seed_complete('205')
        with SQLitePostStore(self.db) as store:
            self.seed_lost(store)
            record_list_page(store.conn,run_id='live',endpoint='lists',page=1,
                articles=[self.article('91',0)],observed_at='2999-01-01T00:00:00+08:00')
            store.record_crawler_run(command='discover-latest',stats={},started_at='2026-06-25T11:00:00+08:00')
            store.conn.execute('PRAGMA query_only=ON')
            changes=store.conn.total_changes
            data=crawler_omission_snapshot(store.conn)
            self.assertEqual((data['missing_post'],data['missing_refresh'],data['total']),(1,1,2))
            self.assertEqual(store.conn.total_changes,changes)
