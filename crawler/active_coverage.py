"""Bounded list2 rounds with event overlap and safe page-boundary yielding."""
from __future__ import annotations

import hashlib
import json

from crawler.id_ledger import event_key, safe_int


class ActiveCoverage:
    CHECKPOINT = "lists2_coverage_checkpoint"
    PREVIOUS = "lists2_previous_observations"

    def __init__(self, store, round_id, limit, *, baseline, dry_run=False):
        self.store = store
        self.dry_run = dry_run
        self.round_id = round_id
        self.limit = min(18, max(1, limit))
        checkpoint = self._load(self.CHECKPOINT)
        previous = self._load(self.PREVIOUS)
        self.previous = set(previous.get("events", []))
        self.closed = previous.get("result") if previous.get("round_id") == round_id else None
        self.saved = {"round_id": round_id, "limit": self.limit, "calls": 0,
                      "pages": {}, "quiet": 0, "quiet_matches": [], "restarts": 0,
                      "baseline": baseline}
        self.rechecking = False
        if checkpoint.get("round_id") == round_id:
            self.saved = checkpoint
            self.limit = min(self.limit, checkpoint["limit"])
            self.rechecking = bool(self.saved["pages"])
        elif checkpoint:
            # A different scheduler process abandoned its round.
            self.previous.clear()
        self.page = max(map(int, self.saved["pages"]), default=1) if self.rechecking else 1
        self.reason = "cap"
        self.last_page_stats = {}

    def restart_at_head(self):
        self.saved.update(pages={}, quiet=0, quiet_matches=[])
        self.saved["restarts"] += 1
        self.rechecking = False
        self.page = 1

    def _load(self, name):
        row = self.store.conn.execute("select value from crawl_state where key=?", (name,)).fetchone()
        return json.loads(row[0]) if row and row[0] else {}

    def _save(self, name, value, *, commit):
        if not self.dry_run:
            self.store.set_state(name, json.dumps(value, separators=(",", ":")), commit=commit)

    def pages(self):
        if self.closed:
            return
        while self.saved["calls"] < self.limit:
            self.saved["calls"] += 1
            self._save(self.CHECKPOINT, self.saved, commit=True)
            yield self.page

    def signals(self, articles):
        counts = {str(row["id"]): safe_int(row.get("comment_count", row.get("count_comment", 0)))
                  for row in articles if row.get("id")}
        if not counts:
            return 0, 0
        ids = tuple(counts)
        marks = ",".join("?" for _ in ids)
        existing = self.store.conn.execute(
            f"""select l.post_id, l.source_comment_count,
                (select count(*) from comments c where c.post_id=l.post_id) as saved_rows
                from post_id_ledger l where l.post_id in ({marks})""", ids,
        ).fetchall()
        growth = sum(counts[row["post_id"]] > max(row["source_comment_count"], row["saved_rows"])
                     for row in existing)
        return len(ids) - len(existing), growth

    def observe(self, articles, signals, new_events, *, stop_on_repeat):
        """Called after this page's events and queue candidates are recorded."""
        keys = [hashlib.sha256(event_key(a).encode()).hexdigest()[:24] for a in articles]
        ids = [str(a["id"]) for a in articles]
        matches = {post_id for post_id, key in zip(ids, keys) if key in self.previous}
        matches_in_tail = bool(self.previous.intersection(keys[len(keys) // 2:]))
        pages = self.saved["pages"]
        new_ids, comment_growth = signals
        self.last_page_stats = dict(page=self.page, ids=len(set(ids)),
                                    new_ids=new_ids, comment_growth=comment_growth,
                                    new_events=new_events, previous_matches=len(matches))
        if self.rechecking:
            self.rechecking = False
            old_keys = pages[str(self.page)]
            boundary = set(old_keys)
            retained_ids = {post_id for post_id, key in zip(ids, keys) if key in boundary}
            if (len(retained_ids) < max(2, (len(boundary) + 1) // 2)
                    or not set(old_keys[len(old_keys) // 2:]).intersection(keys)):
                # The active feed moved. Rebuild a contiguous prefix within the
                # original budget. Never silently jump past an unverified page.
                self.restart_at_head()
                return ""
            pages[str(self.page)] = keys
            self.saved.update(quiet=0, quiet_matches=[])
            self.page += 1
            return ""
        repeated = bool(keys and keys in pages.values())
        pages[str(self.page)] = keys
        self.page += 1
        if repeated and stop_on_repeat:
            return "repeat"
        if new_ids or comment_growth or len(matches) < 2 or not matches_in_tail:
            self.saved.update(quiet=0, quiet_matches=[])
        else:
            last_matches = set(self.saved["quiet_matches"])
            self.saved["quiet"] = self.saved["quiet"] + 1 if matches - last_matches else 1
            self.saved["quiet_matches"] = sorted(matches)
        if len(pages) >= 3 and self.saved["quiet"] >= 2:
            return "overlap"
        return ""

    def finish(self, reason, stats):
        if self.closed:
            stats.update(self.closed)
            return
        self.reason = reason
        pages = self.saved["pages"]
        result = dict(stop_reason=reason, active_round_id=self.round_id,
                      coverage_complete=reason in {"empty", "overlap"},
                      previous_overlap=reason == "overlap", round_pages=len(pages),
                      round_page_attempts=self.saved["calls"], boundary_restarts=self.saved["restarts"],
                      last_page=self.last_page_stats, errors=stats["errors"], quota_stop=stats["quota_stop"])
        stats.update(result)
        if reason == "list1_due":
            self._save(self.CHECKPOINT, self.saved, commit=False)
        else:
            # Persist only one compact anchor for the next round. Interrupted
            # and failed runs cannot establish reliable previous observations.
            events = sorted({event for page in pages.values() for event in page})
            if reason in {"error", "quota", "blocked", "invalid_page", "resume_gap"}:
                events = []
            self._save(self.PREVIOUS, {"round_id": self.round_id, "events": events, "result": result}, commit=False)
            self._save(self.CHECKPOINT, {}, commit=False)
