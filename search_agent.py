"""
Pipeline Command — Daily Search Agent
Runs via GitHub Actions at 8:00 AM EST daily.
Searches Indeed, ZipRecruiter, Dice (MCP) + Ladders/LinkedIn/Levels (web).
Scores results against rubric via Anthropic API.
Writes results.json to repo for dashboard to consume.
"""

import os
import json
import re
import hashlib
from datetime import datetime, timezone, timedelta
import urllib.request
import urllib.error

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL   = "claude-sonnet-4-6"
RESULTS_FILE = "results.json"
HISTORY_FILE = "history.json"   # seen dedup keys — never re-surface a dismissed role

EST = timezone(timedelta(hours=-5))

RUBRIC = """
Senior product leadership in fintech / financial-services / trading / payments / travel-tech / insurance.
Score HIGH (80-100) when role matches most of:
- Title: Senior PM, Group PM, Director of Product, VP Product, or Head of Product
- Domain: payments, trading platforms, wealth/brokerage, data & analytics, digital transformation,
  platform modernization, product-led growth
- Comp: base >= $180K (bonus/equity a plus)
- Location: Charlotte / Lake Norman MSA OR remote / hybrid friendly
- Mandate: commercial outcomes, platform scaling, 0->1 or turnaround, PLG, digital transformation
- Culture: technical depth valued, not pure people-management abstraction

Score LOW / flag (below 60) when:
- Junior or associate-level, or clear step down in scope
- Hard on-site relocation outside Charlotte metro
- Pure scrum master / delivery manager, no product ownership
- Domain mismatch with no transferable system (e.g. pure hardware, CPG)
- Comp clearly below $160K base
"""

CANDIDATE_CONTEXT = """
Candidate: Travis Witsch — VP/Director PM
Location: Charlotte / Lake Norman, NC
Background: 20+ years across Charles Schwab (~15 yrs), Spirit Airlines, Truist Insurance Holdings, Lynchval Systems
Deep expertise: payments, data/BI/EDWH/semantic layer, trading/brokerage, digital transformation,
  mobile apps, API platforms, supply operations, GDS/NDC/travel tech
Certifications: SAFe 6 Agilist, CSPO, CSM
Education: BS Actuarial Science + CS Minor, University of Pittsburgh
Comp floor: $180K base; target $200-230K+; open to hybrid or remote
Key proof points: 2K->400K engagement scale, 30% trial-to-funded conversion lift,
  $40M revenue lift, mobile app shipped in <90 days during M&A, semantic layer product ownership
"""

QUERIES = [
    {"id": "q1",  "label": "Wealth management Director",         "text": 'Director Product "wealth management" OR "financial advisor" remote'},
    {"id": "q2",  "label": "Data/BI/Analytics VP",              "text": 'VP "product manager" "data" OR "analytics" OR "BI" OR "digital transformation" fintech'},
    {"id": "q3",  "label": "Trading / brokerage Head of Product","text": '"Head of Product" OR "Director of Product" "trading" OR "brokerage" OR "capital markets" OR "platform transformation" OR "digital transformation"'},
    {"id": "q4",  "label": "Payments strategy VP/Director",      "text": '"product strategy" VP Director payments fintech remote'},
    {"id": "q5",  "label": "Platform strategy / product",        "text": '"platform strategy" OR "platform product" Director VP Charlotte OR remote'},
    {"id": "q6",  "label": "Product / revenue operations",       "text": '"product operations" OR "revenue operations" VP Director fintech'},
    {"id": "q7",  "label": "Trader / member / advisor engagement","text": '"trader engagement" OR "member experience" OR "advisor experience" product director'},
    {"id": "q8",  "label": "GTM payments VP/Director",           "text": '"go-to-market" "product manager" VP Director payments OR fintech'},
    {"id": "q9",  "label": "Platform transformation / PLG",      "text": '"platform transformation" OR "product-led growth" Director VP "product manager" fintech OR "financial services" OR payments remote'},
    {"id": "q10", "label": "Digital transformation fin services", "text": '"digital transformation" "product" Director VP "financial services" OR banking OR fintech OR insurance Charlotte OR remote'},
]

MCP_SOURCES = [
    {"id": "indeed",       "name": "Indeed",       "url": "https://mcp.indeed.com/claude/mcp"},
    {"id": "ziprecruiter", "name": "ZipRecruiter", "url": "https://api.ziprecruiter.com/mcp"},
    {"id": "dice",         "name": "Dice",         "url": "https://mcp.dice.com/mcp"},
]

FETCH_SOURCES = [
    {"id": "ladders",  "name": "The Ladders",   "site": "theladders.com"},
    {"id": "linkedin", "name": "LinkedIn Jobs", "site": "linkedin.com/jobs"},
    {"id": "levels",   "name": "Levels.fyi",    "site": "levels.fyi/jobs"},
]

ALERT_THRESHOLD = 80


# ─────────────────────────────────────────────
#  UTILITIES
# ─────────────────────────────────────────────
def log(msg):
    ts = datetime.now(EST).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def dedup_key(title: str, company: str) -> str:
    raw = (title + company).lower()
    raw = re.sub(r"[\s\-.,/]", "", raw)
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def anthropic_post(payload: dict) -> dict:
    """POST to Anthropic /v1/messages. Returns parsed JSON or raises."""
    body = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type":      "application/json",
            "x-api-key":         API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def extract_text(data: dict) -> str:
    return " ".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    ).strip()


def parse_json_array(text: str) -> list:
    """Extract first JSON array found in text."""
    text = re.sub(r"```json|```", "", text).strip()
    match = re.search(r"\[[\s\S]*\]", text)
    if not match:
        return []
    try:
        result = json.loads(match.group())
        return result if isinstance(result, list) else []
    except json.JSONDecodeError:
        return []


def parse_json_object(text: str) -> dict:
    """Extract first JSON object found in text."""
    text = re.sub(r"```json|```", "", text).strip()
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return {}
    try:
        result = json.loads(match.group())
        return result if isinstance(result, dict) else {}
    except json.JSONDecodeError:
        return {}


# ─────────────────────────────────────────────
#  SEARCH
# ─────────────────────────────────────────────
def search_mcp(source: dict, query_text: str) -> list:
    """Search via MCP connector (Indeed / ZipRecruiter / Dice)."""
    prompt = (
        f'Search for jobs matching: "{query_text}"\n'
        f"Focus: Director, VP, Head of Product in fintech, financial services, payments, "
        f"trading, wealth management, or digital transformation. Charlotte area or remote preferred.\n"
        f"Return a JSON array of up to 5 real listings found, no markdown:\n"
        f'[{{"title":"","company":"","comp":"","loc":"","remote":true,"url":"","jd":"2-3 sentence description"}}]\n'
        f"Return [] if no strong matches."
    )
    try:
        data = anthropic_post({
            "model":      MODEL,
            "max_tokens": 1200,
            "messages":   [{"role": "user", "content": prompt}],
            "mcp_servers": [{"type": "url", "url": source["url"], "name": source["id"] + "-mcp"}],
        })
        return parse_json_array(extract_text(data))[:5]
    except Exception as e:
        log(f"    MCP error ({source['name']}): {e}")
        return []


def search_web(source: dict, query_text: str) -> list:
    """Search via web_search tool for Ladders / LinkedIn / Levels."""
    prompt = (
        f'Search {source["site"]} for job listings matching: "{query_text}"\n'
        f"Find Director, VP, or Head of Product roles in fintech, financial services, payments, "
        f"trading, wealth management, or digital transformation. Charlotte or remote.\n"
        f"Return a JSON array of up to 4 real listings, no markdown:\n"
        f'[{{"title":"","company":"","comp":"salary or unknown","loc":"city, state or remote",'
        f'"remote":true,"url":"direct apply url","jd":"2-3 sentence description"}}]\n'
        f"Return [] if nothing relevant."
    )
    try:
        data = anthropic_post({
            "model":      MODEL,
            "max_tokens": 1200,
            "messages":   [{"role": "user", "content": prompt}],
            "tools":      [{"type": "web_search_20250305", "name": "web_search"}],
        })
        return parse_json_array(extract_text(data))[:4]
    except Exception as e:
        log(f"    Web fetch error ({source['name']}): {e}")
        return []


# ─────────────────────────────────────────────
#  SCORING
# ─────────────────────────────────────────────
def score_result(result: dict) -> dict:
    """Score a single result against the rubric. Returns result with score fields added."""
    prompt = (
        f"Score this job listing against the rubric for the candidate below.\n\n"
        f"RUBRIC:\n{RUBRIC}\n\n"
        f"CANDIDATE CONTEXT:\n{CANDIDATE_CONTEXT}\n\n"
        f"ROLE:\n"
        f"Title: {result.get('title','')}\n"
        f"Company: {result.get('company','')}\n"
        f"Comp: {result.get('comp','unknown')}\n"
        f"Location: {result.get('loc','unknown')}\n"
        f"Remote: {result.get('remote',False)}\n"
        f"JD: {result.get('jd','')}\n\n"
        f"Return ONLY a JSON object, no markdown:\n"
        f'{{"score":<0-100>,"tier":"<Strong match|Good match|Marginal|Skip>",'
        f'"rationale":"<max 22 words>","flags":["<short concern>"]}}'
    )
    try:
        data  = anthropic_post({"model": MODEL, "max_tokens": 400, "messages": [{"role": "user", "content": prompt}]})
        out   = parse_json_object(extract_text(data))
        result["score"]     = max(0, min(100, int(out.get("score", 0))))
        result["tier"]      = out.get("tier", "Unscored")
        result["rationale"] = out.get("rationale", "")
        result["flags"]     = out.get("flags", [])[:4]
    except Exception as e:
        log(f"    Scoring error: {e}")
        result["score"]     = 0
        result["tier"]      = "Unscored"
        result["rationale"] = "Scoring failed"
        result["flags"]     = []
    return result


# ─────────────────────────────────────────────
#  PERSISTENCE
# ─────────────────────────────────────────────
def load_history() -> set:
    """Load set of dedup keys already seen (found or dismissed)."""
    if not os.path.exists(HISTORY_FILE):
        return set()
    try:
        with open(HISTORY_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_history(seen: set):
    with open(HISTORY_FILE, "w") as f:
        json.dump(list(seen), f)


def load_existing_results() -> list:
    """Load any un-actioned results from previous run."""
    if not os.path.exists(RESULTS_FILE):
        return []
    try:
        with open(RESULTS_FILE) as f:
            data = json.load(f)
            return data.get("pending", [])
    except Exception:
        return []


def save_results(new_results: list, run_meta: dict):
    """
    Merge new results with any pending from prior runs, save to results.json.
    Dashboard reads this file via 'Import results'.
    """
    existing = load_existing_results()
    # Dedup against existing pending
    existing_keys = {dedup_key(r["title"], r["company"]) for r in existing}
    merged = existing[:]
    added = 0
    for r in new_results:
        k = dedup_key(r["title"], r["company"])
        if k not in existing_keys:
            merged.append(r)
            existing_keys.add(k)
            added += 1
    # Sort by score desc
    merged.sort(key=lambda x: x.get("score", 0), reverse=True)
    output = {
        "generated":   datetime.now(EST).isoformat(),
        "run_meta":    run_meta,
        "new_count":   added,
        "total_count": len(merged),
        "pending":     merged,
    }
    with open(RESULTS_FILE, "w") as f:
        json.dump(output, f, indent=2)
    log(f"Saved {len(merged)} total results ({added} new) → {RESULTS_FILE}")
    return added


# ─────────────────────────────────────────────
#  WINDOWS TOAST NOTIFICATION
# ─────────────────────────────────────────────
def send_toast(title: str, message: str):
    """
    Send a Windows 10/11 toast notification.
    Uses win10toast if installed, falls back to a PowerShell call.
    GitHub Actions runners are Linux so this is a no-op there —
    it fires when you run the script locally on Windows.
    """
    import platform
    if platform.system() != "Windows":
        log(f"Toast (skipped on non-Windows): {title} — {message}")
        return
    try:
        import subprocess
        ps_script = (
            f'[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;'
            f'$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);'
            f'$template.SelectSingleNode("//text[@id=\'1\']").InnerText = "{title}";'
            f'$template.SelectSingleNode("//text[@id=\'2\']").InnerText = "{message}";'
            f'$toast = [Windows.UI.Notifications.ToastNotification]::new($template);'
            f'[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Pipeline Command").Show($toast);'
        )
        subprocess.run(["powershell", "-Command", ps_script], capture_output=True, timeout=10)
        log(f"Toast sent: {title}")
    except Exception as e:
        log(f"Toast failed (non-critical): {e}")


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    if not API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable not set.")

    run_start = datetime.now(EST)
    log(f"=== Pipeline Command Search Agent ===")
    log(f"Run started: {run_start.strftime('%Y-%m-%d %H:%M EST')}")
    log(f"Queries: {len(QUERIES)} | MCP sources: {len(MCP_SOURCES)} | Web sources: {len(FETCH_SOURCES)}")

    seen      = load_history()
    found     = []
    dedup_ct  = 0
    error_ct  = 0

    # ── MCP sources ──
    for src in MCP_SOURCES:
        for q in QUERIES:
            log(f"  [{src['name']}] {q['label']}…")
            results = search_mcp(src, q["text"])
            for r in results:
                if not r.get("title") or not r.get("company"):
                    continue
                k = dedup_key(r["title"], r["company"])
                if k in seen:
                    dedup_ct += 1
                    continue
                r["source"]     = src["name"]
                r["queryLabel"] = q["label"]
                r["queryId"]    = q["id"]
                found.append(r)
                seen.add(k)
            log(f"    → {len(results)} results")

    # ── Web fetch sources ──
    for src in FETCH_SOURCES:
        for q in QUERIES:
            log(f"  [{src['name']}] {q['label']}…")
            results = search_web(src, q["text"])
            for r in results:
                if not r.get("title") or not r.get("company"):
                    continue
                k = dedup_key(r["title"], r["company"])
                if k in seen:
                    dedup_ct += 1
                    continue
                r["source"]     = src["name"]
                r["queryLabel"] = q["label"]
                r["queryId"]    = q["id"]
                found.append(r)
                seen.add(k)
            log(f"    → {len(results)} results")

    log(f"Raw found: {len(found)} | Dupes skipped: {dedup_ct}")

    # ── Score all found ──
    if found:
        log(f"Scoring {len(found)} new matches…")
        for i, r in enumerate(found):
            log(f"  Scoring {i+1}/{len(found)}: {r['title']} @ {r['company']}")
            score_result(r)
            r["foundAt"] = run_start.isoformat()

    # ── Save results ──
    run_meta = {
        "runAt":        run_start.isoformat(),
        "queriesRun":   len(QUERIES),
        "sourcesRun":   len(MCP_SOURCES) + len(FETCH_SOURCES),
        "rawFound":     len(found),
        "dupesSkipped": dedup_ct,
        "errors":       error_ct,
    }
    added = save_results(found, run_meta)
    save_history(seen)

    # ── Summary ──
    strong = [r for r in found if r.get("score", 0) >= ALERT_THRESHOLD]
    run_end = datetime.now(EST)
    elapsed = (run_end - run_start).seconds

    log(f"")
    log(f"=== Run complete in {elapsed}s ===")
    log(f"New matches:    {added}")
    log(f"Strong (≥{ALERT_THRESHOLD}): {len(strong)}")
    if strong:
        log(f"Top matches:")
        for r in sorted(strong, key=lambda x: x.get("score",0), reverse=True)[:5]:
            log(f"  {r.get('score')} — {r['title']} @ {r['company']}")

    # ── Windows toast (fires locally, skipped on GitHub Actions Linux runner) ──
    if added > 0:
        top = strong[0] if strong else found[0]
        toast_title = f"Pipeline Command — {added} new match{'es' if added>1 else ''}"
        toast_msg   = (
            f"{len(strong)} score ≥ {ALERT_THRESHOLD}. "
            f"Top: {top['title']} @ {top['company']} ({top.get('score','?')})"
            if strong else
            f"Open dashboard to review."
        )
        send_toast(toast_title, toast_msg)

    # Exit with error code if nothing found (helps GitHub Actions flag in summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
