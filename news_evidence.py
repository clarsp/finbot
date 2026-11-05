"""Auditable headline ranking and bounded reuse of unchanged neutral research."""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone

VERSION = "relevance_v1"
RESEARCH_PROMPT_VERSION = "news_market_horizon_v3"
NEUTRAL_TTL_SECONDS = 2 * 3600


def select_headlines(ticker, company_name, articles, limit=8):
    """Rank textual relevance above provider association; keep uncertain evidence.

    This selects inputs, not a trading verdict. The Researcher still establishes
    relevance and direction. Unknown timestamps and indirect stories are retained.
    """
    from market_instruments import SP500
    name = re.sub(r"\b(incorporated|corporation|company|limited|holdings|inc|corp|ltd|plc)\b\.?", "", company_name, flags=re.I).strip(" ,.")
    aliases = [name] if len(name) >= 3 else []
    aliases += {"AMZN": ["Amazon"], "GOOG": ["Google", "Alphabet"],
                "GOOGL": ["Google", "Alphabet"], "META": ["Meta", "Facebook"],
                "MA": ["Mastercard"], "FCEL": ["FuelCell"]}.get(ticker.upper(), [])
    pattern = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(x) for x in aliases + [ticker]) + r")(?!\w)", re.I)
    index_pattern = re.compile(r"S\s*&\s*P\s*500|S&P500|Federal Reserve|\bFed\b|inflation|nonfarm|payrolls|Treasury yields|US stocks|U\.S\. stocks", re.I)
    seen = set()
    ranked = []
    for article in articles:
        title = str(article.get("title") or "")
        summary = str(article.get("summary") or "")
        # Syndicated copies with the same title should not consume multiple slots.
        identity = re.sub(r"\W+", " ", title).strip().casefold() or str(article.get("url") or article.get("article_key"))
        if identity in seen:
            continue
        seen.add(identity)
        if ticker.upper() in SP500:
            score = 3 if index_pattern.search(title) else 2 if index_pattern.search(summary) else 0
        else:
            score = 3 if pattern.search(title) else 2 if pattern.search(summary) else 0
        try:
            stamp = datetime.fromisoformat(str(article.get("published_at") or "").replace("Z", "+00:00"))
            stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
            timestamp = stamp.timestamp()
        except ValueError:
            timestamp = 0
        ranked.append((score, timestamp, {**article, "selection_relevance": "direct_text_match" if score else "provider_association_only"}))
    ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
    selected = [item[2] for item in ranked[:limit]]
    return selected, {"version": VERSION, "collected": len(articles), "unique": len(ranked),
                      "selected": len(selected), "direct_text_matches": sum(score > 0 for score, _, _ in ranked[:limit]),
                      "method": "instrument text relevance, then publication recency; duplicate titles removed"}


def neutral_fingerprint(subject, model):
    """Exact substantive inputs; omit retrieval clocks, not market observations."""
    clocks = {"fetched_at", "retrieved_at", "observed_at", "last_updated", "last_updated_at", "quote_observed_at",
              "etoro_quote_timestamp", "local_timestamp", "session_progress", "minutes_from_open", "minutes_to_close"}
    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k not in clocks}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value
    payload = {"version": VERSION, "research_prompt_version": RESEARCH_PROMPT_VERSION, "model": model, "subject": clean(subject)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def cached_neutral(conn, ticker, fingerprint, now=None):
    now = time.time() if now is None else now
    row = conn.execute("SELECT fingerprint, result_json, analyzed_at, reuse_count FROM neutral_research_cache WHERE symbol=?", (ticker,)).fetchone()
    if row is None or row[0] != fingerprint or not 0 <= now-row[2] < NEUTRAL_TTL_SECONDS:
        return None
    conn.execute("UPDATE neutral_research_cache SET reuse_count=reuse_count+1 WHERE symbol=?", (ticker,))
    conn.commit()
    return {**json.loads(row[1]), "neutral_reuse": {"fingerprint": fingerprint, "analyzed_at": row[2], "reuse_count": row[3]+1, "expires_at": row[2]+NEUTRAL_TTL_SECONDS}}


def remember_neutral(conn, ticker, fingerprint, result, now=None):
    if result.get("status") != "available":
        return  # Provider failures are never cached as genuine neutral research.
    conn.execute("DELETE FROM neutral_research_cache WHERE analyzed_at < ?", ((time.time() if now is None else now)-NEUTRAL_TTL_SECONDS,))
    if result.get("signal") == "neutral":
        conn.execute("INSERT OR REPLACE INTO neutral_research_cache VALUES (?,?,?,?,0)", (ticker, fingerprint, json.dumps(result), time.time() if now is None else now))
    else:
        conn.execute("DELETE FROM neutral_research_cache WHERE symbol=?", (ticker,))
    conn.commit()
