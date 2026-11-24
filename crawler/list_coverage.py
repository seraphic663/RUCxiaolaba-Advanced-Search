"""Track joins between consecutive list1 scans using source IDs and creation time."""

from __future__ import annotations

import json

from crawler.id_ledger import ledger_state, now_iso


class List1Coverage:
    """A new head can advance while older, unverified intervals stay open."""

    key = "list1_monitor_coverage_v1"

    def __init__(self, conn):
        self.conn = conn
        self.state = json.loads(ledger_state(conn, self.key, default="{}"))
        self.previous_head = self.state.get("head")
        self.gaps = self.state.setdefault("gaps", [])
        self.seen = set()
        self.oldest = ""
        self.newest = ""
        self.overlap = False

    def crosses(self, anchor):
        return (
            self.oldest <= anchor["time"] <= self.newest
            and len(self.seen.intersection(anchor["witnesses"].items())) >= 2
        )

    def observe(self, page, articles, max_pages):
        # A pinned item preceding the chronological feed can reappear every
        # hour. Only the descending suffix can supply overlap witnesses.
        points = []
        for article in articles:
            identity = str(article.get("id") or "")
            date = str(article.get("create_time") or article.get("show_create_time") or "")
            if not identity or not date:
                return False
            if points and date > points[-1][0]:
                points = []
            points.append((date, identity))
        if not points:
            return False

        # A few adjacent IDs tolerate the removal of a single head post.
        head = {"time": points[0][0], "witnesses": {pid: date for date, pid in points[:3]}}
        tail = {"time": points[-1][0], "witnesses": {pid: date for date, pid in points[-3:]}}
        self.seen.update((pid, date) for date, pid in points)
        self.oldest = min(self.oldest or tail["time"], tail["time"])
        self.newest = max(self.newest, head["time"])
        if self.previous_head:
            self.overlap = self.crosses(self.previous_head)
        if page == 1:
            if (self.previous_head and head != self.previous_head
                    and not self.overlap and head["time"] >= self.previous_head["time"]):
                self.gaps.append({"older": self.previous_head, "newer": head, "status": "open"})
            if not self.previous_head or head["time"] >= self.previous_head["time"]:
                self.state["head"] = head

        retained = []
        for gap in self.gaps:
            older, newer = gap["older"], gap["newer"]
            if self.crosses(newer):
                if self.crosses(older):
                    continue
                if older["time"] < tail["time"] <= newer["time"]:
                    gap["newer"] = tail
                    gap["status"] = "open"
                elif head["time"] < older["time"]:
                    # The original witnesses disappeared or the feed reordered.
                    # Keep the interval visible without repeatedly paging for it.
                    gap["status"] = "anchor_missing"
            elif page == max_pages and tail["time"] > newer["time"]:
                gap["status"] = "beyond_page_limit"
            retained.append(gap)
        self.gaps[:] = retained
        # The caller commits this checkpoint with the page's stored data.
        self.conn.execute(
            "insert into ledger_state(key,value,updated_at) values (?,?,?) "
            "on conflict(key) do update set value=excluded.value, updated_at=excluded.updated_at",
            (self.key, json.dumps(self.state, separators=(",", ":")), now_iso()),
        )
        return True

    def needs_another_page(self):
        return any(gap["status"] == "open" for gap in self.gaps)

    def report(self):
        return {
            "list1_previous_head": (self.previous_head or {}).get("time", ""),
            "list1_current_head": (self.state.get("head") or {}).get("time", ""),
            "list1_overlap": self.overlap,
            "list1_open_gaps": len(self.gaps),
            "list1_gaps_beyond_page_limit": sum(g["status"] == "beyond_page_limit" for g in self.gaps),
            "list1_gaps_missing_anchor": sum(g["status"] == "anchor_missing" for g in self.gaps),
        }
