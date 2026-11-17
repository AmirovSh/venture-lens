import hashlib
import ipaddress
import re
import socket
import time
import ssl
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, urljoin, parse_qs
import httpx
from bs4 import BeautifulSoup
from .settings import setting, SEARCH_TRUST_ENV, ROOT

class EvidenceError(Exception): pass

def public_url(url):
    p = urlparse(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.port not in (None, 80, 443):
        raise EvidenceError('Недопустимый адрес источника')
    try:
        addresses = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    except OSError as e:
        raise EvidenceError('Адрес источника не разрешается') from e
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise EvidenceError('Внутренние адреса заблокированы')
    return url

def normalize(text): return re.sub(r'\s+', ' ', text).strip()

def grounded(claim, sources):
    source = next((x for x in sources if x['id'] == claim.get('source_id')), None)
    quote = normalize(claim.get('quote') or '')
    return bool(source and len(quote) >= 15 and quote.casefold() in normalize(source.get('text', '')).casefold())

def source_record(url, title, text, published=None, origin='page', retrieved=None):
    checksum = hashlib.sha256(text.encode()).hexdigest()
    record = {'id':hashlib.sha256((url + checksum).encode()).hexdigest()[:16], 'url':url, 'title':title,
              'text':text[:24000], 'published':published, 'retrieved':retrieved or time.time(), 'sha256':checksum, 'origin':origin}
    folder = ROOT / 'data/sources'; folder.mkdir(parents=True, exist_ok=True)
    (folder / f'{checksum}.txt').write_text(text, encoding='utf-8')
    return record

def fetch(url):
    with httpx.Client(timeout=15, trust_env=SEARCH_TRUST_ENV, verify=ssl.create_default_context(), headers={'User-Agent':'VentureLensResearch/0.1 (+source-verification)'}) as c:
        for _ in range(5):
            public_url(url)
            with c.stream('GET', url) as r:
                if r.is_redirect:
                    url = urljoin(url, r.headers['location']); continue
                r.raise_for_status()
                if not any(t in r.headers.get('content-type', '') for t in ['text/html','text/plain','application/xhtml']):
                    raise EvidenceError('Поддерживаются HTML и текстовые страницы')
                content = bytearray()
                for chunk in r.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 2_000_000: raise EvidenceError('Страница слишком большая')
                soup = BeautifulSoup(bytes(content), 'html.parser')
                title = soup.title.get_text(' ', strip=True) if soup.title else urlparse(url).hostname
                date = soup.find('meta', property='article:published_time')
                for tag in soup(['script','style','nav','footer','header','noscript']): tag.decompose()
                text = normalize(soup.get_text(' ', strip=True))
                return source_record(url, title, text, date.get('content') if date else None)
    raise EvidenceError('Слишком много перенаправлений')

def search(query, limit=5):
    if setting('EXA_API_KEY'):
        with httpx.Client(timeout=35, trust_env=SEARCH_TRUST_ENV, verify=ssl.create_default_context()) as c:
            r=c.post('https://api.exa.ai/search', headers={'x-api-key':setting('EXA_API_KEY')}, json={'query':query,'numResults':limit,'contents':{'text':{'maxCharacters':16000}}})
            r.raise_for_status()
            return [source_record(x['url'],x.get('title',x['url']),x.get('text',''),x.get('publishedDate'),origin='exa') for x in r.json().get('results',[]) if x.get('text')]
    with httpx.Client(timeout=25, trust_env=SEARCH_TRUST_ENV, verify=ssl.create_default_context(), follow_redirects=True) as c:
        # RSS is an open search result format; it still provides snippets, not full pages.
        r=c.get('https://www.bing.com/search',params={'q':query,'format':'rss','setlang':'en-us','cc':'us'})
        if r.is_success:
            try:
                root=ET.fromstring(r.text)
                items=[]
                for item in root.findall('.//item')[:limit]:
                    url=item.findtext('link','')
                    if url.startswith('http'):
                        items.append(source_record(url,item.findtext('title',url),BeautifulSoup(item.findtext('description',''),'html.parser').get_text(' ',strip=True),origin='search_snippet'))
                if items:return items
            except ET.ParseError:pass
        r=c.get('https://www.google.com/search',params={'q':query,'num':limit},headers={'User-Agent':'Mozilla/5.0'})
        r.raise_for_status()
        soup=BeautifulSoup(r.text,'html.parser')
        results=[]
        for block in soup.select('div.MjjYud, div.Gx5Zad, div.g'):
            heading=block.find(['h3'])
            if not heading: continue
            a=heading.find_parent('a') or block.find('a')
            if not a:continue
            url=a.get('href','')
            if url.startswith('/url?'):url=parse_qs(urlparse(url).query).get('q',parse_qs(urlparse(url).query).get('url',['']))[0]
            if not url.startswith('http') or 'google.com/' in url:continue
            if any(x['url']==url for x in results):continue
            text=normalize(block.get_text(' ',strip=True))
            results.append(source_record(url,heading.get_text(' ',strip=True),text,origin='search_snippet'))
            if len(results)>=limit:break
        if results:return results
        # Public HTML fallback. Search snippets remain explicitly marked as snippets.
        r=c.get('https://html.duckduckgo.com/html/',params={'q':query});r.raise_for_status()
        soup=BeautifulSoup(r.text,'html.parser')
        for block in soup.select('.result')[:limit]:
            a=block.select_one('.result__a'); snippet=block.select_one('.result__snippet')
            if not a:continue
            url=a.get('href','')
            if 'uddg=' in url:url=parse_qs(urlparse(url).query).get('uddg',[''])[0]
            if url.startswith('http'):results.append(source_record(url,a.get_text(' ',strip=True),snippet.get_text(' ',strip=True) if snippet else '',origin='search_snippet'))
        if not results:raise EvidenceError('Поиск не вернул источников: возможно, CAPTCHA. Настройте EXA_API_KEY.')
        return results

def collect(queries, max_sources=6, on_error=None):
    results=[]
    for query in queries:
        try:
            found=search(query, 4)
            per_query=max(1,max_sources//len(queries))
            added=0
            for result in found:
                if any(x['url']==result['url'] for x in results):continue
                if result['origin']=='search_snippet':
                    try:result=fetch(result['url'])
                    except Exception:
                        if on_error:on_error('Страница недоступна; сохранён поисковый фрагмент',result['url'])
                results.append(result)
                added+=1
                if len(results)>=max_sources:return results
                if added>=per_query:break
        except Exception as e:
            if on_error:on_error(type(e).__name__,query)
    if not results:raise EvidenceError('Не удалось получить источники. Повторите этап или настройте поисковый API.')
    return results
