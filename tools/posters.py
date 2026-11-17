#!/usr/bin/env python3
"""Poster archive data from TMDB: for every film in data/library.json, find it on TMDB and keep its
distinct posters (the same artwork in other languages removed with a colour-layout fingerprint).

  python3 tools/posters.py            # only films not looked up yet
  python3 tools/posters.py --refresh  # look everything up again

Needs a TMDB API key in tools/.tmdb_key (not committed).
Cache:  data/tmdb.json      (douban id -> TMDB match + chosen posters)
Notes:  tools/curation.json        (hand-written, fact-checked notes on famous posters, keyed by douban id)
Fixes:  tools/tmdb_overrides.json  (douban id -> "movie/123" / "tv/456" for a wrong match, or "" to hide a film)
Output: data/posters.js     (window.POSTERS, read by posters.html; images are loaded from TMDB's CDN)
"""
import io, json, os, re, subprocess, sys, time, urllib.parse
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageFilter

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
KEY_FILE = os.path.join(ROOT, 'tools', '.tmdb_key')
CACHE = os.path.join(ROOT, 'data', 'tmdb.json')
OVERRIDES = os.path.join(ROOT, 'tools', 'tmdb_overrides.json')
OUT = os.path.join(ROOT, 'data', 'posters.js')
API, IMG = 'https://api.themoviedb.org/3', 'https://image.tmdb.org/t/p/'
MAX_KEEP, CANDIDATES = 12, 80
CN_NUM = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}


def curl(url, binary=False):
    r = subprocess.run(['curl', '-s', '-f', '-L', '-m', '25', url], capture_output=True)
    if r.returncode != 0: return None
    return r.stdout if binary else r.stdout.decode('utf-8', 'replace')


def api(path, **q):
    q['api_key'] = KEY
    for attempt in range(3):
        body = curl(f'{API}{path}?{urllib.parse.urlencode(q)}')
        if body:
            try: return json.loads(body)
            except ValueError: pass
        time.sleep(1.5 * (attempt + 1))
    return None


def year_of(s):
    m = re.match(r'(\d{4})', s or '')
    return int(m.group(1)) if m else None


def season_of(film):
    text = film['title'] + ' ' + (film.get('orig') or '')
    m = re.search(r'第([一二三四五六七八九十\d]+)季', text) or re.search(r'Season\s*(\d+)', text, re.I)
    if not m: return None
    v = m.group(1)
    return int(v) if v.isdigit() else (10 + CN_NUM.get(v[1:], 0) if v.startswith('十') and len(v) > 1 else CN_NUM.get(v, 0)) or None


def norm(s):
    return re.sub(r'[\s·:：,，!！?？\'"\-—.()（）]', '', s or '').lower()


def clean(name):
    return re.sub(r'\s*(第[一二三四五六七八九十\d]+季|Season\s*\d+)\s*', ' ', name or '', flags=re.I).strip()


def match(film):
    """Returns 'movie/123' / 'tv/456' (+ season) or '' when nothing believable is found."""
    d = film.get('detail') or {}
    y = int(d['year']) if str(d.get('year') or '').isdigit() else None
    tv = bool(d.get('is_tv')) or season_of(film) is not None
    names = [n for n in [clean((film.get('orig') or '').split(' / ')[0]), clean(film['title'])] if n]
    kind, date_key, title_key = ('tv', 'first_air_date', 'name') if tv else ('movie', 'release_date', 'title')
    season = season_of(film)
    best = None
    for name in dict.fromkeys(names):
        for with_year in ([True, False] if y else [False]):
            q = {'query': name, 'language': 'zh-CN'}
            if with_year and not tv: q['year'] = y
            res = (api(f'/search/{kind}', **q) or {}).get('results') or []
            for rank, r in enumerate(res[:8]):
                ry = year_of(r.get(date_key))
                if y and ry:
                    # films: same year (±1); a series: premiere within ±1, or any time before a later season
                    ok = abs(ry - y) <= 1 or (tv and season and season > 1 and ry <= y)
                    if not ok: continue
                elif y and not ry: continue
                titles = {norm(r.get(title_key)), norm(r.get('original_' + title_key))}
                score = (norm(name) in titles) * 10 - rank - (abs(ry - y) if y and ry and not (tv and season and season > 1) else 0)
                if not best or score > best[0]: best = (score, f'{kind}/{r["id"]}', r.get(title_key) or '')
            if best and best[0] >= 8: return best[1], best[2]
    if best: return best[1], best[2]
    return '', ''


def signature(img_bytes):
    """Colour layout of a heavily blurred thumbnail: text and small details vanish, the artwork's composition stays.
    Same artwork in another language / with another tagline ends up nearly identical."""
    im = Image.open(io.BytesIO(img_bytes)).convert('RGB').resize((60, 90)).filter(ImageFilter.GaussianBlur(4)).resize((6, 9))
    px = im.get_flattened_data() if hasattr(im, 'get_flattened_data') else im.getdata()
    return [c for p in px for c in (p if isinstance(p, tuple) else (p,))]


SAME_ART = 36  # mean per-channel difference below this = same artwork (tuned on 花样年华 / 天使爱美丽 / 寄生虫 / 龙猫)


def pick_posters(tm, season):
    """Fetch posters, best first, and drop near-duplicates (same artwork in another language, re-crops...)."""
    kind, tid = tm.split('/')
    lists = []
    if kind == 'tv' and season:
        lists.append((api(f'/tv/{tid}/season/{season}/images') or {}).get('posters') or [])
    lists.append((api(f'/{kind}/{tid}/images') or {}).get('posters') or [])
    seen, cands = set(), []
    for lst in lists:
        for p in sorted(lst, key=lambda p: (-(p.get('vote_count') or 0), -(p.get('vote_average') or 0))):
            if p['file_path'] not in seen and (p.get('width') or 0) >= 300:
                seen.add(p['file_path']); cands.append(p)
    cands = cands[:CANDIDATES]
    with ThreadPoolExecutor(8) as ex:
        thumbs = list(ex.map(lambda p: curl(IMG + 'w92' + p['file_path'], binary=True), cands))
    kept, sigs = [], []
    for p, t in zip(cands, thumbs):
        if not t: continue
        try: g = signature(t)
        except Exception: continue
        if any(sum(abs(x - y) for x, y in zip(g, k)) / len(g) < SAME_ART for k in sigs): continue
        sigs.append(g)
        kept.append([p['file_path'], p.get('iso_639_1') or '', round(p['width'] / p['height'], 3), round(p.get('vote_average') or 0, 1)])
        if len(kept) >= MAX_KEEP: break
    return kept


def build(lib, cache):
    out = {}
    for fid, c in cache.items():
        if fid in lib['films'] and c.get('tm') and c.get('p'):
            out[fid] = {'tm': c['tm'], 'n': c.get('name', ''), 'p': c['p']}
    notes_file = os.path.join(ROOT, 'tools', 'curation.json')
    notes = json.load(open(notes_file, encoding='utf-8')) if os.path.exists(notes_file) else {}
    notes = {k: v for k, v in notes.items() if k in out}
    with open(OUT, 'w', encoding='utf-8') as fh:
        fh.write('window.POSTERS = ' + json.dumps(out, ensure_ascii=False, separators=(',', ':')) + ';\n')
        fh.write('window.POSTER_NOTES = ' + json.dumps(notes, ensure_ascii=False, separators=(',', ':')) + ';\n')
    return out


def run(refresh=False, log=print):
    global KEY
    if not os.path.exists(KEY_FILE):
        log('! 没有 TMDB 密钥（tools/.tmdb_key），跳过海报馆。'); return None
    KEY = open(KEY_FILE).read().strip()
    lib = json.load(open(os.path.join(ROOT, 'data', 'library.json'), encoding='utf-8'))
    cache = json.load(open(CACHE, encoding='utf-8')) if os.path.exists(CACHE) else {}
    over = {k: v for k, v in (json.load(open(OVERRIDES, encoding='utf-8')) if os.path.exists(OVERRIDES) else {}).items() if not k.startswith('_')}
    todo = [f for fid, f in lib['films'].items() if refresh or fid not in cache or (fid in over and cache[fid].get('tm') != over[fid])]
    log(f'海报馆：{len(todo)} 部需要查找（共 {len(lib["films"])} 部）')

    def one(f):
        tm, name = (over[f['id']], '') if f['id'] in over else match(f)
        season = season_of(f) if tm.startswith('tv/') else None
        return f['id'], {'tm': tm, 'name': name, 'season': season,
                         'p': pick_posters(tm, season) if tm else []}

    done = 0
    with ThreadPoolExecutor(4) as ex:
        for fid, entry in ex.map(one, todo):
            cache[fid] = entry; done += 1
            if done % 25 == 0:
                log(f'  {done}/{len(todo)}'); json.dump(cache, open(CACHE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    json.dump(cache, open(CACHE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    out = build(lib, cache)
    missing = [lib['films'][fid]['title'] for fid in lib['films'] if not (cache.get(fid) or {}).get('p')]
    log(f'海报馆：{len(out)} 部共 {sum(len(v["p"]) for v in out.values())} 张海报；{len(missing)} 部没找到' + (f'：{"、".join(missing[:12])}{"…" if len(missing) > 12 else ""}' if missing else ''))
    return out


if __name__ == '__main__':
    run(refresh='--refresh' in sys.argv)
