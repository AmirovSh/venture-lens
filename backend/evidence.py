import hashlib
import json
import ipaddress
import re
import socket
import time
import ssl
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, urljoin, parse_qs
import httpx
from bs4 import BeautifulSoup
from botocore.exceptions import ClientError
from .settings import setting, SEARCH_TRUST_ENV, ROOT
from . import storage,providers
from .execution import DependencyUnavailable,Cancelled

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

def cached_search(query,limit=5):
    """Retrieve only previously fetched public documents, never agent conclusions."""
    words=[w.casefold() for w in re.findall(r'[a-zA-Z0-9]{3,}',query) if w.casefold() not in {'pricing','valuation','official','company','product','subscription','terms','the','and','for','with','that','this'}]
    if not words:return []
    found=[]
    for record in storage.cached_pages():
        try:
            if time.time()-record['retrieved']>86400:continue
            haystack=(record['title']+' '+record['url']).casefold()
            score=sum(3 if w in haystack else 1 if w in record['text'].casefold() else 0 for w in words)
            if score>=max(1,len(words)):
                found.append((score,record))
        except (ValueError,KeyError):continue
    return [{**x,'cache_hit':True} for _,x in sorted(found,key=lambda x:x[0],reverse=True)[:limit]]

def grounded(claim, sources):
    source = next((x for x in sources if x['id'] == claim.get('source_id')), None)
    quote = normalize(claim.get('quote') or '')
    return bool(source and len(quote) >= 15 and quote.casefold() in normalize(source.get('text', '')).casefold())

def financial_quote_matches(claim,amount):
    quote=(claim.get('quote') or '').casefold()
    if not any(word in quote for word in ['valued','valuation','market cap','acquisition','acquired','deal','transaction','капитализац','оценк','сделк']):return False
    values=[]
    for match in re.finditer(r'(?:us\$|\$|usd)\s*(\d+(?:[.,]\d+)?)\s*(billion|million|bn|b\b|m\b|млрд|млн)',quote):
        number=float(match.group(1).replace(',','.'))
        values.append(number*(1e9 if match.group(2) in ('billion','bn','b','млрд') else 1e6))
    return any(abs(value-amount)<=max(1,amount*.01) for value in values)

def source_record(url, title, text, published=None, origin='page', retrieved=None):
    checksum,uri=storage.put_source(text)
    record={'id':hashlib.sha256((url+checksum).encode()).hexdigest()[:16],'url':url,'title':title,'text':text[:24000],'published':published,'retrieved':retrieved or time.time(),'sha256':checksum,'origin':origin,'storage_uri':uri}
    if origin=='page':storage.put_cache('pages',hashlib.sha256(url.encode()).hexdigest(),record)
    return record

def fetch(url):
    public_url(url)
    value=storage.get_cache('pages',hashlib.sha256(url.encode()).hexdigest())
    if value and time.time()-value['retrieved']<86400:
        try:
            storage.get_source(value['sha256']);return {**value,'cache_hit':True}
        except (storage.IntegrityError,OSError,ClientError):pass
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
    key=hashlib.sha256(query.encode()).hexdigest()
    cached=storage.get_cache('search',key)
    if cached and time.time()-cached['retrieved']<86400:return cached['results'][:limit]
    errors=[]
    configured=[]
    if setting('SEARXNG_URL'):configured.append(('searxng',setting('SEARXNG_URL').rstrip('/')))
    if setting('EXA_API_KEY'):configured.append(('exa','https://api.exa.ai'))
    for kind,url in configured:
        name=providers.identity('search',url)
        try:
            with providers.lane(name,35,concurrency=2,rpm=int(setting('SEARCH_REQUESTS_PER_MINUTE','20'))):
                with httpx.Client(timeout=30,trust_env=False if kind=='searxng' else SEARCH_TRUST_ENV,verify=ssl.create_default_context()) as c:
                    if kind=='searxng':
                        response=c.get(url+'/search',params={'q':query,'format':'json','language':'en','safesearch':0});response.raise_for_status()
                        results=[source_record(x['url'],x.get('title',x['url']),BeautifulSoup(x.get('content',''),'html.parser').get_text(' ',strip=True),origin='search_snippet') for x in response.json().get('results',[])[:limit] if x.get('url','').startswith('http')]
                    else:
                        response=c.post(url+'/search',headers={'x-api-key':setting('EXA_API_KEY')},json={'query':query,'numResults':limit,'contents':{'text':{'maxCharacters':16000}}});response.raise_for_status()
                        results=[source_record(x['url'],x.get('title',x['url']),x.get('text',''),x.get('publishedDate'),origin='exa') for x in response.json().get('results',[]) if x.get('text')]
                    if not results:raise EvidenceError('Поиск не вернул доказательств: внешние движки могут быть ограничены')
                providers.success(name);storage.put_cache('search',key,{'retrieved':time.time(),'results':results});return results
        except DependencyUnavailable as error:errors.append(error)
        except (httpx.HTTPError,EvidenceError,ValueError) as error:
            providers.failure(name,type(error).__name__,cooldown=60)
            errors.append(DependencyUnavailable('Поиск временно недоступен; ожидаем резервный источник',60))
    local=cached_search(query,limit)
    if local:return local
    if configured and setting('ALLOW_PUBLIC_SEARCH','false').lower()!='true':
        raise errors[-1] if errors else DependencyUnavailable('Поиск недоступен',60)
    return public_search(query,limit)

def public_search(query,limit=5):
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
    deferred=None
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
        except Cancelled:raise
        except DependencyUnavailable as error:
            deferred=error
            if on_error:on_error(str(error),query)
        except Exception as e:
            if on_error:on_error(str(e) if isinstance(e,EvidenceError) else 'Поиск временно недоступен; сохранённые источники будут использованы, если доступны',query)
    if not results:
        if deferred:raise deferred
        raise EvidenceError('Не удалось получить источники. Повторите этап или настройте поисковый API.')
    return results
