"""Read-only Douyin profile identity and post-listing workflows.

The public contract deliberately treats ``sec_uid`` as the account identity.
Display names are evidence for a human, never a durable key.
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote, urlencode

from platform_adapter import PlatformAdapter, resolve_adapter

from .login import settle_login_state


_SEC_UID_RE = re.compile(r"^[A-Za-z0-9_-]{8,160}$")


def _guard_account_read(page, adapter: PlatformAdapter) -> dict | None:
    state = settle_login_state(page, adapter=adapter)
    if state.get("risk_page"):
        return {
            "success": False,
            "state": "RISK_PAGE",
            "risk_page": True,
            "needs_user_verification": True,
            "error": "账号主页读取遇到验证码/风控页",
            "login_state": state,
        }
    if not state.get("logged_in"):
        return {
            "success": False,
            "state": "AUTH_REQUIRED",
            "logged_in": False,
            "error": "账号主页读取需要有效登录状态",
            "login_state": state,
        }
    return None


def _valid_sec_uid(value: object) -> str:
    sec_uid = str(value or "").strip()
    if not _SEC_UID_RE.fullmatch(sec_uid):
        raise ValueError("sec_uid 格式无效")
    return sec_uid


def profile_url(sec_uid: str) -> str:
    return f"https://www.douyin.com/user/{quote(_valid_sec_uid(sec_uid), safe='_-')}"


def resolve_user(
    page,
    nickname: str,
    adapter: PlatformAdapter | None = None,
) -> dict:
    """Resolve exactly one visible, exact nickname match to a ``sec_uid``.

    Fuzzy matches and multiple exact matches fail closed. The returned evidence
    is intentionally small and contains no cookies or page dumps.
    """

    adapter = resolve_adapter(adapter)
    nickname = str(nickname or "").strip()
    if not nickname:
        raise ValueError("昵称不能为空")
    search_url = (
        f"https://www.douyin.com/search/{quote(nickname, safe='')}?type=user"
    )
    page.navigate(search_url)
    page.wait_for_load(20)
    blocked = _guard_account_read(page, adapter)
    if blocked:
        return blocked

    wanted = json.dumps(nickname, ensure_ascii=False)
    snapshot = page.evaluate(
        f"""
        (() => {{
          const wanted = {wanted};
          const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          const links = Array.from(document.querySelectorAll('a[href*="/user/"]')).filter(visible);
          const candidates = [];
          for (const link of links) {{
            const href = link.href || '';
            const match = href.match(/\/user\/([^/?#]+)/);
            if (!match || match[1] === 'self') continue;
            const container = link.closest('[data-e2e*="user"], li, article') || link.parentElement || link;
            const lines = ((container.innerText || link.innerText || ''))
              .split(/\\n+/).map(value => value.trim()).filter(Boolean).slice(0, 12);
            const exact = lines.includes(wanted) || (link.innerText || '').trim() === wanted;
            candidates.push({{
              sec_uid: decodeURIComponent(match[1]),
              display_name: exact ? wanted : ((link.innerText || '').trim() || lines[0] || ''),
              profile_url: href.split(/[?#]/, 1)[0],
              exact_match: exact,
            }});
          }}
          return {{title: document.title || '', href: location.href || '', candidates}};
        }})()
        """
    ) or {}
    if adapter.is_risk_page(snapshot.get("title", ""), ""):
        return {
            "success": False,
            "state": "RISK_PAGE",
            "risk_page": True,
            "needs_user_verification": True,
            "error": "账号搜索结果为验证码/风控页",
        }

    exact_by_uid: dict[str, dict] = {}
    for candidate in snapshot.get("candidates") or []:
        if not isinstance(candidate, dict) or not candidate.get("exact_match"):
            continue
        try:
            sec_uid = _valid_sec_uid(candidate.get("sec_uid"))
        except ValueError:
            continue
        exact_by_uid[sec_uid] = {
            "sec_uid": sec_uid,
            "display_name": nickname,
            "profile_url": profile_url(sec_uid),
        }
    matches = list(exact_by_uid.values())
    if len(matches) > 1:
        return {
            "success": False,
            "state": "AMBIGUOUS",
            "query": nickname,
            "exact_match_count": len(matches),
            "candidates": matches,
            "error": "存在多个同名账号，不能唯一确认 sec_uid",
        }
    if not matches:
        return {
            "success": False,
            "state": "NOT_FOUND",
            "query": nickname,
            "exact_match_count": 0,
            "error": "未找到可唯一确认的精确昵称账号",
        }
    return {
        "success": True,
        "state": "RESOLVED",
        "query": nickname,
        "identity_key": "sec_uid",
        "account": matches[0],
        "evidence": {
            "source": "douyin_user_search",
            "match": "exact_visible_nickname",
            "search_url": search_url,
        },
    }


def _normalize_aweme(item: object, expected_sec_uid: str) -> dict:
    if not isinstance(item, dict):
        raise ValueError("aweme_list 包含非对象元素")
    video_id = str(item.get("aweme_id") or "").strip()
    if not video_id.isdigit():
        raise ValueError("作品缺少有效 aweme_id")
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    author_sec_uid = str(author.get("sec_uid") or "").strip()
    if author_sec_uid and author_sec_uid != expected_sec_uid:
        raise ValueError("作品作者 sec_uid 与请求账号不一致")
    duration_ms = None
    video = item.get("video") if isinstance(item.get("video"), dict) else {}
    if isinstance(video.get("duration"), (int, float)):
        duration_ms = int(video["duration"])
    return {
        "video_id": video_id,
        "title": str(item.get("desc") or "").strip(),
        "detail_url": f"https://www.douyin.com/video/{video_id}",
        "author_sec_uid": author_sec_uid or expected_sec_uid,
        "author_nickname": str(author.get("nickname") or "").strip(),
        "publish_time": item.get("create_time"),
        "duration_ms": duration_ms,
        "is_top": bool(item.get("is_top")),
    }


def list_user_posts(
    page,
    sec_uid: str,
    cursor: str = "0",
    count: int = 18,
    adapter: PlatformAdapter | None = None,
) -> dict:
    """Read one profile-post API page and return its server-issued cursor."""

    adapter = resolve_adapter(adapter)
    sec_uid = _valid_sec_uid(sec_uid)
    cursor = str(cursor if cursor is not None else "0").strip()
    if not cursor.isdigit():
        raise ValueError("cursor 必须是非负整数字符串")
    if not isinstance(count, int) or not 1 <= count <= 20:
        raise ValueError("count 必须在 1 到 20 之间")

    page.navigate(profile_url(sec_uid))
    page.wait_for_load(20)
    blocked = _guard_account_read(page, adapter)
    if blocked:
        return {**blocked, "sec_uid": sec_uid, "cursor": cursor}

    query = urlencode(
        {
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "sec_user_id": sec_uid,
            "max_cursor": cursor,
            "count": str(count),
        }
    )
    endpoint = f"https://www.douyin.com/aweme/v1/web/aweme/post/?{query}"
    fetched = page.evaluate(
        f"""
        (async () => {{
          try {{
            const response = await fetch({json.dumps(endpoint)}, {{
              credentials: 'include',
              headers: {{'accept': 'application/json, text/plain, */*'}},
            }});
            const text = await response.text();
            let payload = null;
            try {{ payload = JSON.parse(text); }} catch (_error) {{}}
            return {{http_status: response.status, payload}};
          }} catch (error) {{
            return {{http_status: 0, payload: null, fetch_error: String(error)}};
          }}
        }})()
        """
    ) or {}
    http_status = fetched.get("http_status")
    payload = fetched.get("payload")
    if http_status in (401, 403):
        return {
            "success": False,
            "state": "RISK_OR_AUTH_GATE",
            "risk_page": http_status == 403,
            "logged_in": False if http_status == 401 else True,
            "sec_uid": sec_uid,
            "cursor": cursor,
            "http_status": http_status,
            "error": "主页作品接口拒绝请求；停止分页，不提交 cursor",
        }
    if http_status != 200 or not isinstance(payload, dict):
        return {
            "success": False,
            "state": "FETCH_FAILED",
            "sec_uid": sec_uid,
            "cursor": cursor,
            "http_status": http_status,
            "error": str(fetched.get("fetch_error") or "主页作品接口未返回有效 JSON")[:300],
        }
    if payload.get("status_code") not in (None, 0):
        return {
            "success": False,
            "state": "API_REJECTED",
            "sec_uid": sec_uid,
            "cursor": cursor,
            "api_status_code": payload.get("status_code"),
            "error": str(payload.get("status_msg") or "主页作品接口返回失败状态")[:300],
        }
    aweme_list = payload.get("aweme_list")
    if not isinstance(aweme_list, list):
        return {
            "success": False,
            "state": "SCHEMA_INVALID",
            "sec_uid": sec_uid,
            "cursor": cursor,
            "error": "主页作品响应缺少 aweme_list",
        }
    try:
        videos = [_normalize_aweme(item, sec_uid) for item in aweme_list]
    except ValueError as exc:
        return {
            "success": False,
            "state": "SCHEMA_INVALID",
            "sec_uid": sec_uid,
            "cursor": cursor,
            "error": str(exc),
        }
    has_more = bool(payload.get("has_more"))
    next_cursor = str(payload.get("max_cursor") or "").strip() if has_more else None
    if has_more and (not next_cursor.isdigit() or next_cursor == cursor):
        return {
            "success": False,
            "state": "CURSOR_INVALID",
            "sec_uid": sec_uid,
            "cursor": cursor,
            "error": "响应声明 has_more，但未提供可推进的真实 max_cursor",
        }
    return {
        "success": True,
        "state": "PAGE_READY",
        "identity_key": "sec_uid",
        "sec_uid": sec_uid,
        "profile_url": profile_url(sec_uid),
        "cursor": cursor,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "count": len(videos),
        "videos": videos,
        "cursor_source": "douyin_aweme_post_response.max_cursor",
    }