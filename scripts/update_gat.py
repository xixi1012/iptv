#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GAT 港澳台直播源每日更新脚本（跑在 GitHub Actions 上）。

流程：
  1. 读取仓库现有 GAT.m3u（存量频道）
  2. 抓取 9 个上游源，解析出候选频道
  3. 并发测流：curl -m 8，接受 200/206/301/302；失败 30 秒后重试一次（-m 15）
  4. 存量频道：原线路能播则保留；播不了则找同名候选线路替换；都没有则移除
  5. 上游新频道：测流通过则加入
  6. 写回 GAT.m3u，输出 gat-diff.json 供 workflow 组装提交信息
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

REPO_FILE = "GAT.m3u"
DIFF_FILE = "gat-diff.json"
GROUP_ORDER = ["香港", "澳门", "台湾", "港澳台"]
OK_CODES = {"200", "206", "301", "302"}

# (上游地址, 固定分组(None=按源文件 group-title 映射), 优先级(越小越优先), 行过滤器)
SOURCES = [
    ("https://iptv-org.github.io/iptv/countries/hk.m3u", "香港", 50, None),
    ("https://iptv-org.github.io/iptv/countries/tw.m3u", "台湾", 50, None),
    ("https://iptv-org.github.io/iptv/countries/mo.m3u", "澳门", 50, None),
    ("https://raw.githubusercontent.com/ChungyuCheung/cy-iptv/main/tv/favorites.m3u", None, 10, "fav"),
    ("https://raw.githubusercontent.com/ineed2underfit/hk-iptv/main/hk_live.m3u", "香港", 20, None),
    ("https://joker-cold.github.io/HK-IPTV/COLD_OK.m3u8", None, 30, "cold"),
    ("https://epg.pw/test_channels_hong_kong.m3u", "香港", 40, None),
    ("https://epg.pw/test_channels_macau.m3u", "澳门", 40, None),
    ("https://epg.pw/test_channels_taiwan.m3u", "台湾", 40, None),
]

FAV_GROUPS = {"香港专区": "香港", "台湾专区": "台湾", "体育精选": "港澳台"}
COLD_KEEP = ("香港", "澳门", "台湾", "港澳台")
COLD_KEEP_EN = {"hong kong": "香港", "macau": "澳门", "taiwan": "台湾"}

# 上游源分组标错、实际不是港澳台频道的，名字归一化后写在这里（小写）
# 例：Pet Club TV 是新西兰 Samsung TV Plus 的宠物频道，被某上游源误标为香港
SKIP_NAMES = {"pet club tv"}


def parse_m3u(text):
    """解析 m3u/m3u8 文本 -> [{'name','group','url'}]"""
    entries = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF"):
            m = re.search(r'group-title="([^"]*)"', line)
            group = m.group(1).strip() if m else ""
            name = line.rsplit(",", 1)[-1].strip() if "," in line else ""
            url = ""
            j = i + 1
            while j < len(lines):
                nxt = lines[j].strip()
                if nxt and not nxt.startswith("#"):
                    url = nxt
                    break
                j += 1
            if name and url:
                entries.append({"name": name, "group": group, "url": url})
            i = j
        i += 1
    return entries


def fav_filter(e):
    """ChungyuCheung 精选源：按中文专区分组，排除大陆台"""
    g = FAV_GROUPS.get(e["group"])
    if not g:
        return None
    if re.match(r"(?i)^cctv\d*\+?$", e["name"]):
        return None
    e["group"] = g
    return e


def cold_filter(e):
    """joker-cold 大文件：只保留 group-title 含港澳台的条目"""
    g = e["group"]
    for k in COLD_KEEP:
        if k in g:
            e["group"] = k
            return e
    gl = g.lower()
    for en, zh in COLD_KEEP_EN.items():
        if en in gl:
            e["group"] = zh
            return e
    return None


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as ex:
        print(f"[抓源失败] {url}: {ex}", file=sys.stderr)
        return None


def test_url(url):
    """测流：首次 -m 8，失败 30 秒后重试一次 -m 15；接受 200/206/301/302"""
    for attempt, maxtime in ((0, 8), (1, 15)):
        if attempt == 1:
            time.sleep(30)
        try:
            p = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 "--connect-timeout", "5", "-m", str(maxtime), url],
                capture_output=True, text=True, timeout=maxtime + 15)
            if p.stdout.strip() in OK_CODES:
                return True
        except Exception:
            pass
    return False


def norm(name):
    return re.sub(r"\s+", " ", name.strip().lower())


def best_candidate(cands, ok, used_urls):
    """同名候选中选最优：测流通过、未被占用、优先级高者胜"""
    viable = [c for c in cands if ok(c["url"]) and c["url"] not in used_urls]
    if not viable:
        return None
    viable.sort(key=lambda c: (c["prio"], c["url"]))
    return viable[0]


def main():
    t0 = time.time()

    # 1. 存量
    existing = []
    if os.path.exists(REPO_FILE):
        with open(REPO_FILE, encoding="utf-8") as f:
            existing = parse_m3u(f.read())
    print(f"[存量] {len(existing)} 个频道")

    # 2. 抓源
    with ThreadPoolExecutor(max_workers=8) as ex:
        texts = list(ex.map(fetch, [s[0] for s in SOURCES]))
    candidates = {}
    for (url, fixed_group, prio, filt), text in zip(SOURCES, texts):
        if not text:
            continue
        n = 0
        for e in parse_m3u(text):
            if filt == "fav":
                e = fav_filter(e)
            elif filt == "cold":
                e = cold_filter(e)
            elif fixed_group:
                e["group"] = fixed_group
            else:
                continue
            if not e or not e["url"].startswith(("http://", "https://")):
                continue
            e["prio"] = prio
            candidates.setdefault(norm(e["name"]), []).append(e)
            n += 1
        print(f"[候选] {url.split('/')[2]}: {n} 条")
    print(f"[候选] 去重后 {len(candidates)} 个频道名")

    # 3. 测流
    urls = {e["url"] for e in existing}
    for lst in candidates.values():
        urls.update(e["url"] for e in lst)
    urls = sorted(urls)
    print(f"[测流] 共 {len(urls)} 个 URL，并发测试中…")
    with ThreadPoolExecutor(max_workers=24) as ex:
        results = dict(zip(urls, ex.map(test_url, urls)))
    passed = sum(1 for v in results.values() if v)
    print(f"[测流] 通过 {passed}/{len(urls)}")

    def ok(u):
        return results.get(u, False)

    # 4. 存量更新
    used_urls = set()
    new_entries, added, removed, kept_new_url = [], [], [], []
    existing_names = set()
    for e in existing:
        n = norm(e["name"])
        existing_names.add(n)
        if ok(e["url"]):
            new_entries.append(e)
            used_urls.add(e["url"])
            continue
        alt = best_candidate(candidates.get(n, []), ok, used_urls)
        if alt:
            new_entries.append({"name": e["name"], "group": e["group"], "url": alt["url"]})
            used_urls.add(alt["url"])
            kept_new_url.append([e["name"], alt["url"]])
        else:
            removed.append(e["name"])

    # 5. 新增（按分组追加，保持版面整齐）
    new_by_group = {g: [] for g in GROUP_ORDER}
    for n, lst in sorted(candidates.items()):
        if n in existing_names or n in SKIP_NAMES:
            continue
        alt = best_candidate(lst, ok, used_urls)
        if not alt:
            continue
        grp = alt["group"] if alt["group"] in GROUP_ORDER else "港澳台"
        new_by_group[grp].append({"name": alt["name"], "group": grp, "url": alt["url"]})
        used_urls.add(alt["url"])
        added.append([alt["name"], grp])
    for g in GROUP_ORDER:
        new_entries.extend(new_by_group[g])

    # 6. 写回
    with open(REPO_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for e in new_entries:
            f.write(f'#EXTINF:-1 group-title="{e["group"]}",{e["name"]}\n{e["url"]}\n')

    diff = {
        "total": len(new_entries),
        "added": added,
        "removed": removed,
        "kept_new_url": kept_new_url,
    }
    with open(DIFF_FILE, "w", encoding="utf-8") as f:
        json.dump(diff, f, ensure_ascii=False, indent=1)

    print(f"[完成] 共 {len(new_entries)} 个频道，新增 {len(added)}，移除 {len(removed)}，"
          f"换线路 {len(kept_new_url)}，用时 {time.time()-t0:.0f}s")
    for name, grp in added:
        print(f"  + {name}（{grp}）")
    for name in removed:
        print(f"  - {name}")


if __name__ == "__main__":
    main()
