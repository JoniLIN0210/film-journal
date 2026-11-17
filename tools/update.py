#!/usr/bin/env python3
"""Sync the site with Douban. No login needed.

  python3 tools/update.py            # pick up new "看过" marks from RSS, fill details, rebuild
  python3 tools/update.py --refresh  # also re-fetch details for every film

Flow: Douban RSS (latest ~10 marks) -> data/library.json (source of truth)
      -> film details from Douban's mobile JSON endpoint -> posters/<id>.jpg -> data/movies.js
The RSS only carries the most recent ~10 marks (books and music included), so run this
at least once every ~10 marks; anything older has to be re-imported from the browser.
"""
import json, os, re, subprocess, sys, time, html
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

USER_ID = '217771002'
ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
LIB = os.path.join(ROOT, 'data', 'library.json')
RAW = os.path.join(ROOT, 'data', 'raw.json')
OUT = os.path.join(ROOT, 'data', 'movies.js')
POSTERS = os.path.join(ROOT, 'posters')
UA_DESKTOP = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'
UA_MOBILE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'
RATING_WORDS = {'力荐': 5, '推荐': 4, '还行': 3, '较差': 2, '很差': 1}
CST = timezone(timedelta(hours=8))


def curl(url, ua=UA_DESKTOP, referer=None, out=None, keep_errors=False):
    cmd = ['curl', '-s', '-L', '-m', '25', '-A', ua] + ([] if keep_errors else ['-f'])
    if referer: cmd += ['-H', 'Referer: ' + referer]
    if out: cmd += ['-o', out]
    res = subprocess.run(cmd + [url], capture_output=True)
    return res.stdout.decode('utf-8', 'replace') if res.returncode == 0 else None


def load_library():
    if os.path.exists(LIB):
        lib = json.load(open(LIB, encoding='utf-8'))
        if os.path.exists(RAW):  # backfill the list-page intro for entries imported before it was stored
            for r in json.load(open(RAW, encoding='utf-8')):
                f = lib['films'].get(r['id'])
                if f and 'intro' not in f:
                    f['intro'] = r['intro']; f['orig'] = ' / '.join(r['title'].split(' / ')[1:]).strip()
        return lib
    lib = {'profile': {'id': USER_ID, 'name': ''}, 'films': {}}
    if os.path.exists(RAW):  # first run: bootstrap from the browser import
        for r in json.load(open(RAW, encoding='utf-8')):
            lib['films'][r['id']] = {'id': r['id'], 'title': r['title'].split(' / ')[0].strip(), 'date': r['date'],
                                     'rating': r['rating'], 'comment': r['comment'].strip(), 'poster_url': r['poster'],
                                     'orig': ' / '.join(r['title'].split(' / ')[1:]).strip(), 'intro': r['intro']}
    return lib


def read_rss(lib):
    """Upsert the latest 看过 marks. Returns number of new films."""
    xml = curl(f'https://www.douban.com/feed/people/{USER_ID}/interests')
    if not xml:
        print('! RSS 读取失败，跳过新标记检查'); return 0
    m = re.search(r'<channel>\s*<title>(.*?)\s*的收藏</title>', xml, re.S)
    if m: lib['profile']['name'] = html.unescape(m.group(1)).strip()
    new = 0
    for item in re.findall(r'<item>(.*?)</item>', xml, re.S):
        title = html.unescape(re.search(r'<title>(.*?)</title>', item, re.S).group(1)).strip()
        link = re.search(r'<link>(.*?)</link>', item, re.S).group(1).strip()
        mid = re.search(r'movie\.douban\.com/subject/(\d+)', link)
        if not title.startswith('看过') or not mid: continue
        fid = mid.group(1)
        rating = next((v for k, v in RATING_WORDS.items() if re.search(r'推荐:\s*' + k, item)), None)
        note = re.search(r'<p>备注:\s*(.*?)</p>', item, re.S)
        pub = parsedate_to_datetime(re.search(r'<pubDate>(.*?)</pubDate>', item).group(1)).astimezone(CST)
        poster = re.search(r'<img src="([^"]+)"', item)
        f = lib['films'].get(fid)
        if f is None:
            f = lib['films'][fid] = {'id': fid, 'title': title[2:].strip(), 'date': pub.strftime('%Y-%m-%d')}
            new += 1; print('+ 新标记：', f['title'], f['date'])
        f['rating'] = rating if rating else f.get('rating')
        if note: f['comment'] = html.unescape(note.group(1)).strip()
        f.setdefault('comment', '')
        if poster: f.setdefault('poster_url', poster.group(1))
    return new


def fetch_detail(fid):
    """Returns a detail dict, None on failure, or 'RATE' when Douban is rate-limiting this network."""
    body = curl(f'https://m.douban.com/rexxar/api/v2/movie/{fid}?for_mobile=1', UA_MOBILE, f'https://m.douban.com/movie/subject/{fid}/', keep_errors=True)
    try: d = json.loads(body)
    except Exception: return None
    if 'rate_limit' in str(d.get('msg')): return 'RATE'
    if not d.get('title'): return None
    return {'title': d.get('title'), 'original_title': d.get('original_title') or '', 'year': d.get('year'),
            'is_tv': bool(d.get('is_tv')), 'episodes': d.get('episodes_count') or 0, 'durations': d.get('durations') or [],
            'genres': d.get('genres') or [], 'countries': d.get('countries') or [], 'languages': d.get('languages') or [],
            'directors': [x['name'] for x in d.get('directors') or []], 'actors': [x['name'] for x in (d.get('actors') or [])[:6]],
            'pic': (d.get('pic') or {}).get('normal') or ''}


def fill_details(lib, refresh=False):
    """Fetch details for films that lack them. Stops early if Douban starts rate-limiting. Returns (done, left)."""
    todo = [f for f in lib['films'].values() if refresh or not f.get('detail')]
    todo.sort(key=lambda f: f.get('date') or '', reverse=True)  # newest marks first
    done = strikes = 0
    for f in todo:
        d = fetch_detail(f['id'])
        if d == 'RATE':
            strikes += 1
            if strikes >= 3:
                print('! 豆瓣暂时限流了，先停在这里；过几个小时再运行一次会接着补。'); break
            time.sleep(8); continue
        strikes = 0
        if d: f['detail'] = d; done += 1
        else: print('! 详情获取失败：', f['title'], f['id'], flush=True)
        if done and done % 25 == 0:
            print(f'  已补全 {done} 部详情…', flush=True); save(lib)
        time.sleep(1.5)
    return done, sum(1 for f in lib['films'].values() if not f.get('detail'))


COUNTRIES = set('''中国大陆 中国香港 中国台湾 中国澳门 中国 美国 日本 韩国 英国 法国 德国 意大利 西班牙 印度 泰国 俄罗斯 苏联 加拿大 澳大利亚
爱尔兰 瑞典 巴西 丹麦 新加坡 墨西哥 阿根廷 比利时 荷兰 瑞士 奥地利 波兰 伊朗 土耳其 新西兰 挪威 芬兰 匈牙利 捷克 希腊 葡萄牙 以色列 黎巴嫩
冰岛 智利 哥伦比亚 南非 越南 菲律宾 马来西亚 印度尼西亚 卡塔尔 罗马尼亚 保加利亚 塞尔维亚 乌克兰 格鲁吉亚 西德 东德 捷克斯洛伐克 南斯拉夫
卢森堡 埃及 摩洛哥 突尼斯 沙特阿拉伯 阿联酋 约旦 巴勒斯坦 不丹 尼泊尔 蒙古 哈萨克斯坦 古巴 秘鲁 乌拉圭 爱沙尼亚 拉脱维亚 立陶宛 克罗地亚
斯洛文尼亚 斯洛伐克 波黑 北马其顿 马其顿 阿尔巴尼亚 塞浦路斯 马耳他 摩纳哥 柬埔寨 老挝 缅甸 孟加拉国 巴基斯坦 斯里兰卡 阿富汗 伊拉克 叙利亚
肯尼亚 尼日利亚 塞内加尔 乍得 毛里塔尼亚 阿尔及利亚 委内瑞拉 玻利维亚 巴拉圭 厄瓜多尔 多米尼加 波多黎各 朝鲜 列支敦士登 科索沃 黑山'''.split())
GENRES = set('剧情 喜剧 动作 爱情 科幻 动画 悬疑 惊悚 恐怖 纪录片 短片 情色 同性 音乐 歌舞 家庭 儿童 传记 历史 战争 犯罪 西部 奇幻 冒险 灾难 武侠 古装 运动 黑色电影 真人秀 脱口秀 戏曲 荒诞 鬼怪'.split())


def parse_intro(f):
    """Fallback when the detail endpoint is unavailable: best-effort parse of the list-page intro line
    (dates / cast / countries / directors / runtime / title / genres / writers / languages)."""
    segs = [x.strip().rstrip('/').strip() for x in (f.get('intro') or '').split(' / ')]
    segs = [x for x in segs if x]
    dates = [x for x in segs if re.match(r'^\d{4}(-\d{2}){0,2}(\(|$)', x)]
    body = [x for x in segs if x not in dates]
    rt_re, is_c = re.compile(r'^(\d+)\s*分钟'), (lambda x: x.split(' ')[0] in COUNTRIES)
    cut = len(body)
    for i, x in enumerate(body):
        if i and (rt_re.match(x) or x == f['title'] or re.search(r'(www\.|https?:|\.com|\.co\.)', x) or x in GENRES):
            cut = i; break
    pre, post = body[:cut], body[cut:]
    mins = next((int(rt_re.match(x).group(1)) for x in post if rt_re.match(x)), None)
    directors = []
    while pre and not is_c(pre[-1]) and len(directors) < 3: directors.insert(0, pre.pop())
    countries = []
    while pre and is_c(pre[-1]): countries.insert(0, pre.pop().split(' ')[0])
    if not countries: pre, directors = pre + directors, []
    return {'title': f['title'], 'original_title': f.get('orig') or '', 'year': dates[0][:4] if dates else None,
            'is_tv': bool(re.search(r'第.{1,3}季|Season \d', f['title'] + (f.get('orig') or ''))), 'episodes': 0,
            'durations': [f'{mins}分钟'] if mins else [], 'genres': [x for x in post if x in GENRES], 'countries': countries,
            'languages': [x for x in post if x not in GENRES and re.search(r'(语|话|方言)$', x) and len(x) < 12],
            'directors': directors, 'actors': pre[:6], 'pic': ''}


def fetch_posters(lib):
    os.makedirs(POSTERS, exist_ok=True)
    n = 0
    for f in lib['films'].values():
        out = os.path.join(POSTERS, f['id'] + '.jpg')
        if os.path.exists(out) and os.path.getsize(out) > 1000: continue
        url = (f.get('poster_url') or (f.get('detail') or {}).get('pic') or '').replace('.webp', '.jpg')
        if 'doubanio.com/view/' not in url: continue  # Douban has no poster for this entry
        curl(url, referer='https://movie.douban.com/', out=out)
        if os.path.exists(out) and os.path.getsize(out) > 1000: n += 1
        elif os.path.exists(out): os.remove(out)
        time.sleep(0.2)
    return n


def save(lib):
    json.dump(lib, open(LIB, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


def build(lib):
    rows = []
    for f in lib['films'].values():
        d = f.get('detail') or parse_intro(f)
        mins = next((int(m.group(1)) for s in d.get('durations', []) for m in [re.search(r'(\d+)', s)] if m), None)
        tv, ep = bool(d.get('is_tv')), d.get('episodes') or 0
        year = int(d['year']) if str(d.get('year') or '').isdigit() else None
        has_poster = os.path.exists(os.path.join(POSTERS, f['id'] + '.jpg'))
        rows.append(dict(id=f['id'], t=d.get('title') or f['title'], o=d.get('original_title') or '', y=year,
                         tv=tv, ep=ep, rt1=mins, rt=(mins * ep if tv and ep and mins else mins),
                         c=d.get('countries', []), d=d.get('directors', []), a=d.get('actors', []), g=d.get('genres', []),
                         l=d.get('languages', []), r=f.get('rating'), date=f.get('date') or '', cm=f.get('comment') or '',
                         p=f'posters/{f["id"]}.jpg' if has_poster else ''))
    rows.sort(key=lambda r: r['date'], reverse=True)
    profile = dict(lib['profile'], updated=datetime.now(CST).strftime('%Y-%m-%d'), url=f'https://www.douban.com/people/{USER_ID}/')
    with open(OUT, 'w', encoding='utf-8') as fh:
        fh.write('window.PROFILE = ' + json.dumps(profile, ensure_ascii=False) + ';\n')
        fh.write('window.MOVIES = ' + json.dumps(rows, ensure_ascii=False, separators=(',', ':')) + ';\n')
    return rows


if __name__ == '__main__':
    lib = load_library()
    new = read_rss(lib)
    done, left = fill_details(lib, refresh='--refresh' in sys.argv)
    posters = fetch_posters(lib)
    save(lib)
    rows = build(lib)
    print(f'完成：共 {len(rows)} 部（剧集 {sum(r["tv"] for r in rows)} 部），新增 {new} 部，本次补全详情 {done} 部，新下载海报 {posters} 张。'
          + (f' 还有 {left} 部暂用列表页信息，下次运行会继续补。' if left else ''))
