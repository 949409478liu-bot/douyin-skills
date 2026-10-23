"""Shared, fail-closed Douyin page selection; probes must be read-only."""
import re
from urllib.parse import urlsplit

SNAPSHOT_EXPRESSION = "({url:location.href,title:document.title,readyState:document.readyState,bodyText:(document.body?.innerText || '').slice(0,5000)})"


class DouyinTargetError(RuntimeError):
    pass


def page_health(snapshot):
    """Detect error documents without interpreting ordinary video text as HTTP status."""
    title = str(snapshot.get("title") or "").strip()
    body = str(snapshot.get("bodyText") or "").strip()
    url = str(snapshot.get("url") or snapshot.get("href") or "")
    error = re.compile(r"bad gateway|service unavailable|gateway timeout|internal server error|(?:http\s*(?:error)?\s*)?5\d\d\s+(?:error|bad|internal|service|gateway)|nginx\s+error", re.I)
    status_only = re.fullmatch(r"(?:HTTP\s*)?5\d\d", title, re.I)
    if error.search(title) or status_only or error.match(body) or re.search(r"/(?:error/)?5\d\d(?:[/?#]|$)", url):
        return "UPSTREAM_BAD_GATEWAY"
    if re.search(r"\bERR_[A-Z_]+", title + "\n" + body[:500]) or url.startswith("chrome-error://"):
        return "RUNTIME_FAIL"
    return "HEALTHY"


def classify_login_result(state, snapshot):
    health = page_health(snapshot)
    evidence = state.get("page") or {}
    auth = bool(evidence.get("hasLoginPanel") or evidence.get("hasLoginKeyword"))
    result = dict(state, page_health=health,
                  auth_evidence="CONFIRMED" if health == "HEALTHY" and auth else "UNCONFIRMED")
    if health != "HEALTHY":
        result.update(success=False, logged_in=None, risk_page=False,
                      preflight_status=health, reason="DOUYIN_UPSTREAM_BAD_GATEWAY" if health == "UPSTREAM_BAD_GATEWAY" else "DOUYIN_TARGET_UNHEALTHY")
    elif state.get("risk_page"):
        result["preflight_status"] = "RISK_BLOCKED"
    elif state.get("success") is True and state.get("logged_in") is True:
        result["preflight_status"] = "PASS"
    elif state.get("success") is True and state.get("logged_in") is False and auth:
        result["preflight_status"] = "AUTH_REQUIRED"
    else:
        result.update(success=False, logged_in=None, preflight_status="RUNTIME_FAIL", reason="LOGIN_STATE_UNCONFIRMED")
    return result


def target_id(target):
    return str(target.get("targetId") or target.get("id") or "")


def is_douyin_url(url):
    parts = urlsplit(str(url or ""))
    return parts.scheme == "https" and parts.hostname in {"douyin.com", "www.douyin.com"}


def page_rank(target):
    path = urlsplit(target.get("url", "")).path
    if path == "/search" or path.startswith("/search/"):
        return 0
    if path.startswith("/video/"):
        return 1
    if path in {"", "/"}:
        return 2
    return 3


def select_target(targets, probe, *, pinned_id="", saved_id=""):
    pages = [t for t in targets if isinstance(t, dict) and t.get("type") == "page"]
    candidates = [t for t in pages if is_douyin_url(t.get("url")) and target_id(t)]
    if not candidates:
        raise DouyinTargetError("DOUYIN_TARGET_MISSING")
    fallback_candidates = list(candidates)
    if pinned_id:
        candidates = [t for t in candidates if target_id(t) == pinned_id]
        if not candidates:
            raise DouyinTargetError("DOUYIN_TARGET_INVALID: pinned target missing or outside Douyin")
    candidates.sort(key=lambda t: (page_rank(t), target_id(t) != saved_id, target_id(t)))
    failures = []
    unhealthy = []
    for target in candidates:
        try:
            live = probe(target)
            if not isinstance(live, dict) or not is_douyin_url(live.get("url")):
                raise DouyinTargetError("DOUYIN_TARGET_INVALID: live URL outside Douyin")
            if live.get("readyState") not in {"loading", "interactive", "complete"}:
                raise DouyinTargetError("DOUYIN_TARGET_UNRESPONSIVE")
        except Exception as exc:
            failures.append(str(exc))
            continue
        health = page_health(live)
        if health != "HEALTHY":
            unhealthy.append(health)
            if pinned_id and len(candidates) == 1:
                candidates.extend(sorted((t for t in fallback_candidates if target_id(t) != pinned_id), key=lambda t: (page_rank(t), target_id(t) != saved_id, target_id(t))))
            failures.append("DOUYIN_TARGET_UNHEALTHY:" + health)
            continue
        selected = dict(target, url=live["url"], title=live.get("title", ""))
        metadata = {
            "selected_target_id": target_id(target),
            "selected_url": selected["url"],
            "selected_title": selected["title"],
            "target_domain": urlsplit(selected["url"]).hostname,
            "page_health": "HEALTHY",
            "unhealthy_targets_rejected": len(unhealthy),
            "target_reused": True,
            "target_created": False,
            "cdp_page_count": len(pages),
            "douyin_page_count": sum(is_douyin_url(t.get("url")) for t in pages),
            "target_selection_reason": "pinned_responsive_douyin" if pinned_id and target_id(target) == pinned_id else "ranked_responsive_douyin",
        }
        return selected, metadata
    if unhealthy and len(unhealthy) == len(candidates):
        raise DouyinTargetError("DOUYIN_UPSTREAM_BAD_GATEWAY" if all(h == "UPSTREAM_BAD_GATEWAY" for h in unhealthy) else "DOUYIN_TARGET_UNHEALTHY")
    reason = "DOUYIN_TARGET_INVALID" if any("DOUYIN_TARGET_INVALID" in f for f in failures) else "DOUYIN_TARGET_UNRESPONSIVE"
    raise DouyinTargetError(reason)
