"""
Search for the new spider data (data/articles.csv).
Simple grep-like search with dedup. No Flask/DuckDB dependency needed.
"""
import csv
import os

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CSV_PATH = os.path.join(DATA_DIR, "articles.csv")


def load_deduped():
    """Load articles, dedup by id, return list sorted by id desc."""
    if not os.path.exists(CSV_PATH):
        print(f"[!] {CSV_PATH} not found. Run spider_new.py first.")
        return []

    seen = {}
    with open(CSV_PATH, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            aid = row.get("id", "")
            if aid and aid not in seen:
                seen[aid] = {
                    "id": aid,
                    "content": row.get("content", ""),
                    "category": row.get("category_name", ""),
                    "user": row.get("user_name", ""),
                }
    articles = sorted(seen.values(), key=lambda x: int(x["id"]), reverse=True)
    return articles


def search(keywords):
    """Case-insensitive keyword search. All keywords must match (AND)."""
    articles = load_deduped()
    results = []
    for a in articles:
        text = a["content"].lower()
        if all(kw.lower() in text for kw in keywords if kw):
            results.append(a)
    return results


if __name__ == "__main__":
    import sys

    articles = load_deduped()
    print(f"Loaded {len(articles)} unique articles.\n")

    if len(sys.argv) > 1:
        keywords = sys.argv[1:]
        results = search(keywords)
        print(f"Search '{' '.join(keywords)}': {len(results)} results\n")
        for a in results[:20]:
            content = a["content"][:150]
            print(f"  [#{a['id']}] [{a['category']}] {a['user']}")
            print(f"    {content}")
            print()
    else:
        print("Usage: python search_new.py <keyword1> [keyword2 ...]")
        print("\nLatest 10 posts:")
        for a in articles[:10]:
            content = a["content"][:120]
            print(f"  [#{a['id']}] [{a['category']}] {a['user']}")
            print(f"    {content}")
            print()
