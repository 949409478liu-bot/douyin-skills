"""
搜索并下载扩展 - 集成 CDP 媒体拦截
在 douyin_skills 搜索结果基础上，直接下载视频
"""
from __future__ import annotations

import json
import time
import urllib.request
import websocket
import os
import tempfile

def capture_and_download_from_search(page, keyword: str, limit: int, output_dir: str, cdp_port: int = 9223) -> dict:
    """
    在搜索页直接捕获并下载视频

    Args:
        page: Playwright page 对象（douyin_skills 的搜索页）
        keyword: 搜索关键词
        limit: 下载数量
        output_dir: 输出目录
        cdp_port: Chrome CDP 端口

    Returns:
        {
            "success": bool,
            "keyword": str,
            "downloaded": int,
            "videos": [{"video_id": str, "size_mb": float, "path": str}, ...],
            "failed": [{"video_id": str, "error": str}, ...]
        }
    """
    print(f"\n[集成下载] 关键词: {keyword}, 限制: {limit}")

    # 获取 CDP WebSocket URL
    cdp_endpoint = f"http://127.0.0.1:{cdp_port}"
    try:
        with urllib.request.urlopen(f"{cdp_endpoint}/json", timeout=5) as resp:
            pages = json.loads(resp.read().decode('utf-8'))
            cdp_page = None
            for p in pages:
                if p.get('type') == 'page' and 'douyin.com' in p.get('url', ''):
                    cdp_page = p
                    break

            if not cdp_page:
                return {
                    "success": False,
                    "keyword": keyword,
                    "downloaded": 0,
                    "videos": [],
                    "failed": [],
                    "error": "CDP_PAGE_NOT_FOUND"
                }

            ws_url = cdp_page['webSocketDebuggerUrl']
    except Exception as e:
        return {
            "success": False,
            "keyword": keyword,
            "downloaded": 0,
            "videos": [],
            "failed": [],
            "error": f"CDP_CONNECTION_FAILED: {e}"
        }

    # 等待搜索结果稳定
    print("  [1] 等待搜索结果稳定...")
    time.sleep(3)

    # 提取页面上的所有 video_id
    print("  [2] 提取页面视频 ID...")
    video_ids_script = """
    (() => {
        const seen = new Set();
        const selectors = [
            'a[href*="/video/"]',
            '[data-e2e*="video"]',
            '.search-result-card'
        ];

        for (const sel of selectors) {
            const elements = document.querySelectorAll(sel);
            for (const el of elements) {
                const href = el.href || el.querySelector('a')?.href || '';
                const match = href.match(/\\/video\\/(\\d{16,})/);
                if (match) seen.add(match[1]);
                if (seen.size >= 20) break;
            }
            if (seen.size >= 20) break;
        }

        return Array.from(seen);
    })()
    """

    try:
        video_ids_raw = page.evaluate(video_ids_script) or []
        video_ids = video_ids_raw[:limit]
        print(f"    找到 {len(video_ids)} 个视频 ID")

        if not video_ids:
            return {
                "success": False,
                "keyword": keyword,
                "downloaded": 0,
                "videos": [],
                "failed": [],
                "error": "NO_VIDEO_IDS_FOUND"
            }
    except Exception as e:
        return {
            "success": False,
            "keyword": keyword,
            "downloaded": 0,
            "videos": [],
            "failed": [],
            "error": f"EXTRACT_IDS_FAILED: {e}"
        }

    # 启动 CDP Network 监听
    print("  [3] 启动 CDP Network 监听...")
    try:
        ws = websocket.create_connection(ws_url, timeout=10, suppress_origin=True)
        ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
        ws.recv()
        print("    ✅ Network 已启用")
    except Exception as e:
        return {
            "success": False,
            "keyword": keyword,
            "downloaded": 0,
            "videos": [],
            "failed": [],
            "error": f"CDP_NETWORK_ENABLE_FAILED: {e}"
        }

    # 滚动页面触发视频加载
    print("  [4] 滚动页面触发所有视频加载...")
    try:
        page.evaluate("""
        (() => {
            window.scrollTo(0, document.body.scrollHeight / 3);
            setTimeout(() => window.scrollTo(0, document.body.scrollHeight * 2 / 3), 1000);
            setTimeout(() => window.scrollTo(0, document.body.scrollHeight), 2000);
        })()
        """)
        time.sleep(4)
    except:
        pass

    # 捕获 CDN 请求（20 秒）
    print("  [5] 捕获 CDN 请求（20秒）...")
    candidates = {}
    start = time.time()
    msg_id = 2

    try:
        ws.settimeout(0.1)
        while time.time() - start < 20:
            try:
                msg = ws.recv()
                data = json.loads(msg)

                if data.get('method') == 'Network.responseReceived':
                    params = data.get('params', {})
                    response = params.get('response', {})
                    url = response.get('url', '')
                    status = response.get('status', 0)
                    mime = response.get('mimeType', '')

                    # 抖音视频 CDN 特征
                    if status in (200, 206) and 'video' in mime and any(domain in url for domain in ['douyinvod.com', 'ixigua.com', 'bytecdn.cn']):
                        # 尝试提取 video_id
                        import re
                        vid_match = re.search(r'[&?]__vid=(\d{16,})', url) or re.search(r'/(\d{16,})/', url)
                        if vid_match:
                            video_id = vid_match.group(1)
                            if video_id in video_ids:
                                if video_id not in candidates:
                                    candidates[video_id] = []
                                candidates[video_id].append(url)
                                print(f"    [捕获] video_id={video_id}, 候选+1")
            except:
                pass

        ws.close()
        print(f"    捕获结束，共 {len(candidates)} 个视频有候选")

    except Exception as e:
        ws.close()
        print(f"    ⚠️  捕获异常: {e}")

    # 下载每个视频
    print(f"\n  [6] 下载视频...")
    downloaded = []
    failed = []

    for video_id in video_ids:
        if video_id not in candidates:
            print(f"    ❌ {video_id}: 无CDN候选")
            failed.append({"video_id": video_id, "error": "NO_CDN_CANDIDATES"})
            continue

        urls = candidates[video_id]
        print(f"    [下载] {video_id} ({len(urls)} 个候选)...")

        # 选择最大的文件
        best_url = None
        best_size = 0

        for url in urls:
            try:
                req = urllib.request.Request(url, method='HEAD')
                with urllib.request.urlopen(req, timeout=5) as resp:
                    size = int(resp.headers.get('Content-Length', 0))
                    if size > best_size:
                        best_size = size
                        best_url = url
            except:
                continue

        if not best_url or best_size == 0:
            print(f"      ❌ 无法探测文件大小")
            failed.append({"video_id": video_id, "error": "PROBE_FAILED"})
            continue

        # 下载
        output_path = os.path.join(output_dir, f"{video_id}.mp4")
        try:
            with urllib.request.urlopen(best_url, timeout=60) as resp:
                with open(output_path, 'wb') as f:
                    f.write(resp.read())

            actual_size = os.path.getsize(output_path)
            print(f"      ✅ {actual_size/1048576:.1f} MB")
            downloaded.append({
                "video_id": video_id,
                "size_mb": actual_size / 1048576,
                "path": output_path
            })
        except Exception as e:
            print(f"      ❌ 下载失败: {e}")
            failed.append({"video_id": video_id, "error": str(e)})

    return {
        "success": len(downloaded) > 0,
        "keyword": keyword,
        "downloaded": len(downloaded),
        "videos": downloaded,
        "failed": failed
    }
