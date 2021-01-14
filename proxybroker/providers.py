import asyncio
import os
import re
import warnings
import time
import json
from base64 import b64decode
from html import unescape
from math import sqrt
from urllib.parse import unquote, urlparse

import aiohttp

from .errors import BadStatusError
from .utils import IPPattern, IPPortPatternGlobal, get_headers, log


class Provider:
    """Proxy provider.

    Provider - a website that publish free public proxy lists.

    :param str url: Url of page where to find proxies
    :param tuple proto:
        (optional) List of the types (protocols) that may be supported
        by proxies returned by the provider. Then used as :attr:`Proxy.types`
    :param int max_conn:
        (optional) The maximum number of concurrent connections on the provider
    :param int max_tries:
        (optional) The maximum number of attempts to receive response
    :param int timeout:
        (optional) Timeout of a request in seconds
    """

    _pattern = IPPortPatternGlobal

    def __init__(
        self, url=None, proto=(), max_conn=4, max_tries=3, timeout=20, loop=None
    ):
        if url:
            self.domain = urlparse(url).netloc
        self.url = url
        self.proto = proto
        self._max_tries = max_tries
        self._timeout = timeout
        self._session = None
        self._cookies = {}
        self._proxies = set()
        # concurrent connections on the current provider
        self._sem_provider = asyncio.Semaphore(max_conn)
        self._loop = loop or asyncio.get_event_loop()

    @property
    def proxies(self):
        """Return all found proxies.

        :return:
            Set of tuples with proxy hosts, ports and types (protocols)
            that may be supported (from :attr:`.proto`).

            For example:
                {('192.168.0.1', '80', ('HTTP', 'HTTPS'), ...)}

        :rtype: set
        """
        return self._proxies

    @proxies.setter
    def proxies(self, new):
        new = [(host, port, self.proto) for host, port in new if port]
        self._proxies.update(new)

    async def get_proxies(self):
        """Receive proxies from the provider and return them.

        :return: :attr:`.proxies`
        """
        log.debug('Try to get proxies from %s' % self.domain)

        async with aiohttp.ClientSession(
            headers=get_headers(), cookies=self._cookies, loop=self._loop
        ) as self._session:
            await self._pipe()

        log.debug(
            '%d proxies received from %s: %s'
            % (len(self.proxies), self.domain, self.proxies)
        )
        return self.proxies

    async def _pipe(self):
        await self._find_on_page(self.url)

    async def _find_on_pages(self, urls):
        if not urls:
            return
        tasks = []
        if not isinstance(urls[0], dict):
            urls = set(urls)
        for url in urls:
            if isinstance(url, dict):
                tasks.append(self._find_on_page(**url))
            else:
                tasks.append(self._find_on_page(url))
        await asyncio.gather(*tasks)

    async def _find_on_page(self, url, data=None, headers=None, method='GET'):
        page = await self.get(url, data=data, headers=headers, method=method)
        if not page:
            return
        oldcount = len(self.proxies)
        try:
            received = self.find_proxies(page)
        except Exception as e:
            received = []
            log.error(
                'Error when executing find_proxies.'
                'Domain: %s; Error: %r' % (self.domain, e)
            )
        if not received:
            log.error(f'Got 0 proxies from {url}')
            return
        self.proxies = received
        added = len(self.proxies) - oldcount
        log.debug(
            '%d(%d) proxies added(received) from %s' % (added, len(received), url)
        )

    async def get(self, url, data=None, headers=None, method='GET'):
        for _ in range(self._max_tries):
            page = await self._get(url, data=data, headers=headers, method=method)
            if page:
                break
        return page

    async def _get(self, url, data=None, headers=None, method='GET'):
        page = ''
        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with self._sem_provider, self._session.request(
                method, url, data=data, headers=headers, timeout=timeout
            ) as resp:
                page = await resp.text()
                if resp.status != 200:
                    log.debug(
                        'url: %s\nheaders: %s\ncookies: %s\npage:\n%s'
                        % (url, resp.headers, resp.cookies, page)
                    )
                    raise BadStatusError('Status: %s' % resp.status)
        except (
            UnicodeDecodeError,
            BadStatusError,
            asyncio.TimeoutError,
            aiohttp.ClientOSError,
            aiohttp.ClientResponseError,
            aiohttp.ServerDisconnectedError,
        ) as e:
            page = ''
            log.debug('%s is failed. Error: %r;' % (url, e))
        return page

    def find_proxies(self, page):
        return self._find_proxies(page)

    def _find_proxies(self, page):
        proxies = self._pattern.findall(page)
        return proxies


class Blogspot_com_base(Provider):
    _cookies = {'NCR': 1}

    async def _pipe(self):
        exp = r'''<a href\s*=\s*['"]([^'"]*\.\w+/\d{4}/\d{2}/[^'"#]*)['"]>'''
        pages = await asyncio.gather(
            *[self.get('http://%s/' % d) for d in self.domains]
        )
        urls = re.findall(exp, ''.join(pages))
        await self._find_on_pages(urls)


class Webanetlabs_net(Provider):
    domain = 'webanetlabs.net'

    async def _pipe(self):
        exp = r'''href\s*=\s*['"]([^'"]*proxylist_at_[^'"]*)['"]'''
        page = await self.get('https://webanetlabs.net/publ/24')
        if not page:
            return
        urls = [
            'https://webanetlabs.net%s' % path
            for path in re.findall(exp, page)
        ]
        await self._find_on_pages(urls)


class Checkerproxy_net(Provider):
    domain = 'checkerproxy.net'

    async def _pipe(self):
        exp = r'''href\s*=\s*['"](/archive/\d{4}-\d{2}-\d{2})['"]'''
        page = await self.get('https://checkerproxy.net/')
        if not page:
            return
        urls = [
            'https://checkerproxy.net/api%s' % path
            for path in re.findall(exp, page)
        ]
        await self._find_on_pages(urls)


class Proxy_list_org(Provider):
    domain = 'proxy-list.org'
    _pattern = re.compile(r'''Proxy\('([\w=]+)'\)''')

    def find_proxies(self, page):
        return [b64decode(hp).decode().split(':') for hp in self._find_proxies(page)]

    async def _pipe(self):
        exp = r'''href\s*=\s*['"]\./([^'"]?index\.php\?p=\d+[^'"]*)['"]'''
        url = 'http://proxy-list.org/english/index.php?p=1'
        page = await self.get(url)
        if not page:
            return
        urls = [
            'http://proxy-list.org/english/%s' % path
            for path in re.findall(exp, page)
        ]
        urls.append(url)
        await self._find_on_pages(urls)


class Aliveproxy_com(Provider):
    # more: http://www.aliveproxy.com/socks-list/socks5.aspx/United_States-us
    domain = 'aliveproxy.com'

    async def _pipe(self):
        paths = [
            'socks5-list',
            'high-anonymity-proxy-list',
            'anonymous-proxy-list',
            'fastest-proxies',
            'us-proxy-list',
            'gb-proxy-list',
            'fr-proxy-list',
            'de-proxy-list',
            'jp-proxy-list',
            'ca-proxy-list',
            'ru-proxy-list',
            'proxy-list-port-80',
            'proxy-list-port-81',
            'proxy-list-port-3128',
            'proxy-list-port-8000',
            'proxy-list-port-8080',
        ]
        urls = ['http://www.aliveproxy.com/%s/' % path for path in paths]
        await self._find_on_pages(urls)


class Proxylist_me(Provider):
    domain = 'proxylist.me'

    async def _pipe(self):
        exp = r'''href\s*=\s*['"][^'"]*/?page=(\d+)['"]'''
        page = await self.get('https://proxylist.me/')
        if not page:
            return
        lastId = max([int(n) for n in re.findall(exp, page)])
        urls = [
            'https://proxylist.me/?page=%d' % n
            for n in range(lastId)
        ]
        await self._find_on_pages(urls)


class Foxtools_ru(Provider):
    domain = 'foxtools.ru'

    async def _pipe(self):
        urls = [
            'http://api.foxtools.ru/v2/Proxy.txt?page=%d' % n
            for n in range(1, 3)
        ]
        await self._find_on_pages(urls)


class Gatherproxy_com(Provider):
    domain = 'gatherproxy.com'
    _pattern_h = re.compile(
        r'''(?P<ip>(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?))'''  # noqa
        r'''(?=.*?(?:(?:(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?))|'(?P<port>[\d\w]+)'))''',  # noqa
        flags=re.DOTALL,
    )

    def find_proxies(self, page):
        # if 'gp.dep' in page:
        #     proxies = self._pattern_h.findall(page)  # for http(s)
        #     proxies = [(host, str(int(port, 16)))
        #                for host, port in proxies if port]
        # else:
        #     proxies = self._find_proxies(page)  # for socks
        return [
            (host, str(int(port, 16)))
            for host, port in self._pattern_h.findall(page)
            if port
        ]

    async def _pipe(self):
        url = 'http://www.gatherproxy.com/proxylist/anonymity/'
        expNumPages = r'href="#(\d+)"'
        method = 'POST'
        # hdrs = {'Content-Type': 'application/x-www-form-urlencoded'}
        urls = []
        for t in ['anonymous', 'elite']:
            data = {'Type': t, 'PageIdx': 1}
            page = await self.get(url, data=data, method=method)
            if not page:
                continue
            lastPageId = max([int(n) for n in re.findall(expNumPages, page)])
            urls = [
                {
                    'url': url,
                    'data': {'Type': t, 'PageIdx': pid},
                    'method': method,
                }
                for pid in range(1, lastPageId + 1)
            ]
        # urls.append({'url': 'http://www.gatherproxy.com/sockslist/',
        #              'method': method})
        await self._find_on_pages(urls)


class Gatherproxy_com_socks(Provider):
    domain = 'gatherproxy.com^socks'

    async def _pipe(self):
        urls = [
            {
                'url': 'http://www.gatherproxy.com/sockslist/',
                'method': 'POST'
            }
        ]
        await self._find_on_pages(urls)


class Xseo_in(Provider):
    domain = 'xseo.in'
    charEqNum = {}

    def char_js_port_to_num(self, matchobj):
        chars = matchobj.groups()[0]
        num = ''.join([self.charEqNum[ch] for ch in chars if ch != '+'])
        return num

    def find_proxies(self, page):
        expPortOnJS = r'\(""\+(?P<chars>[a-z+]+)\)'
        expCharNum = r'\b(?P<char>[a-z])=(?P<num>\d);'
        self.charEqNum = {char: i for char, i in re.findall(expCharNum, page)}
        page = re.sub(expPortOnJS, self.char_js_port_to_num, page)
        return self._find_proxies(page)

    async def _pipe(self):
        await self._find_on_page(
            url='https://xseo.in/proxylist', data={'submit': 1}, method='POST'
        )


class Nntime_com(Provider):
    domain = 'nntime.com'
    charEqNum = {}
    _pattern = re.compile(
        r'''\b(?P<ip>(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}'''
        r'''(?:25[0-5]|2[0-4]\d|[01]?\d\d?))(?=.*?(?:(?:(?:(?:25'''
        r'''[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)'''
        r''')|(?P<port>\d{2,5})))''',
        flags=re.DOTALL,
    )

    def char_js_port_to_num(self, matchobj):
        chars = matchobj.groups()[0]
        num = ''.join([self.charEqNum[ch] for ch in chars if ch != '+'])
        return num

    def find_proxies(self, page):
        expPortOnJS = r'\(":"\+(?P<chars>[a-z+]+)\)'
        expCharNum = r'\b(?P<char>[a-z])=(?P<num>\d);'
        self.charEqNum = {char: i for char, i in re.findall(expCharNum, page)}
        page = re.sub(expPortOnJS, self.char_js_port_to_num, page)
        return self._find_proxies(page)

    async def _pipe(self):
        tpl = 'http://www.nntime.com/proxy-updated-{:02}.htm'
        urls = [tpl.format(n) for n in range(1, 31)]
        await self._find_on_pages(urls)


class Proxynova_com(Provider):
    domain = 'proxynova.com'

    async def _pipe(self):
        expCountries = r'"([a-z]{2})"'
        page = await self.get('https://www.proxynova.com/proxy-server-list/')
        if not page:
            return
        tpl = 'https://www.proxynova.com/proxy-server-list/country-%s/'
        urls = [
            tpl % isoCode
            for isoCode in re.findall(expCountries, page)
            if isoCode != 'en'
        ]
        await self._find_on_pages(urls)


class Spys_ru(Provider):
    domain = 'spys.ru'
    charEqNum = {}

    def char_js_port_to_num(self, matchobj):
        chars = matchobj.groups()[0].split('+')
        # ex: '+(i9w3m3^k1y5)+(g7g7g7^v2e5)+(d4r8o5^i9u1)+(y5c3e5^t0z6)'
        # => ['', '(i9w3m3^k1y5)', '(g7g7g7^v2e5)',
        #     '(d4r8o5^i9u1)', '(y5c3e5^t0z6)']
        # => ['i9w3m3', 'k1y5'] => int^int
        num = ''
        for numOfChars in chars[1:]:  # first - is ''
            var1, var2 = numOfChars.strip('()').split('^')
            digit = self.charEqNum[var1] ^ self.charEqNum[var2]
            num += str(digit)
        return num

    def find_proxies(self, page):
        expPortOnJS = r'(?P<js_port_code>(?:\+\([a-z0-9^+]+\))+)'
        # expCharNum = r'\b(?P<char>[a-z\d]+)=(?P<num>[a-z\d\^]+);'
        expCharNum = r'[>;]{1}(?P<char>[a-z\d]{4,})=(?P<num>[a-z\d\^]+)'
        # self.charEqNum = {
        #     char: i for char, i in re.findall(expCharNum, page)}
        res = re.findall(expCharNum, page)
        for char, num in res:
            if '^' in num:
                digit, tochar = num.split('^')
                num = int(digit) ^ self.charEqNum[tochar]
            self.charEqNum[char] = int(num)
        page = re.sub(expPortOnJS, self.char_js_port_to_num, page)
        return self._find_proxies(page)

    async def _pipe(self):
        expSession = r"'([a-z0-9]{32})'"
        url = 'http://spys.one/proxies/'
        page = await self.get(url)
        if not page:
            return
        sessionId = re.findall(expSession, page)[0]
        data = {
            'xf0': sessionId,  # session id
            'xpp': 3,  # 3 - 200 proxies on page
            'xf1': None,
        }  # 1 = ANM & HIA; 3 = ANM; 4 = HIA
        method = 'POST'
        urls = [
            {'url': url, 'data': {**data, 'xf1': lvl}, 'method': method}
            for lvl in [3, 4]
        ]
        await self._find_on_pages(urls)
        # expCountries = r'>([A-Z]{2})<'
        # url = 'http://spys.ru/proxys/'
        # page = await self.get(url)
        # links = ['http://spys.ru/proxys/%s/' %
        #          isoCode for isoCode in re.findall(expCountries, page)]


class My_proxy_com(Provider):
    domain = 'my-proxy.com'

    async def _pipe(self):
        exp = r'''href\s*=\s*['"]([^'"]?free-[^'"]*)['"]'''
        url = 'https://www.my-proxy.com/free-proxy-list.html'
        page = await self.get(url)
        if not page:
            return
        urls = [
            'https://www.my-proxy.com/%s' % path
            for path in re.findall(exp, page)
        ]
        urls.append(url)
        await self._find_on_pages(urls)


class Free_proxy_cz(Provider):
    domain = 'free-proxy.cz'
    _pattern = re.compile(r'''decode\("([\w=]+)".*?\("([\d=]+)"\)''', flags=re.DOTALL)

    def find_proxies(self, page):
        return [
            (b64decode(h).decode(), p)
            for h, p in self._find_proxies(page)
        ]

    async def _pipe(self):
        tpl = 'http://free-proxy.cz/en/proxylist/country/all/http/uptime/all/%d'
        urls = [tpl % n for n in range(1, 15)]
        await self._find_on_pages(urls)


class Proxylistplus_com(Provider):
    domain = 'list.proxylistplus.com'

    async def _pipe(self):
        names = ['Fresh-HTTP-Proxy', 'SSL', 'Socks']
        urls = [
            'http://list.proxylistplus.com/%s-List-%d' % (i, n)
            for i in names
            for n in range(1, 7)
        ]
        await self._find_on_pages(urls)


class Proxylist_download(Provider):
    domain = 'www.proxy-list.download'

    async def _pipe(self):
        urls = [
            'https://www.proxy-list.download/api/v1/get?type=http',
            'https://www.proxy-list.download/api/v1/get?type=https',
        ]
        await self._find_on_pages(urls)


class DidsoftHttp(Provider):
    domain = 'free-proxy-list.net'

    async def _pipe(self):
        urls = [
            'https://free-proxy-list.net/',
            'https://us-proxy.org/',
            'https://free-proxy-list.net/uk-proxy.html',
            'https://www.sslproxies.org/',
            'https://free-proxy-list.net/anonymous-proxy.html',
        ]
        await self._find_on_pages(urls)


class Openproxy_space(Provider):
    domain = 'openproxy.space'
    _pattern = re.compile(r'\b(?P<ip>(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?))'
                          r':(?:(?![7-9]\d{4})(?!6[6-9]\d{3})(?!65[6-9]\d{2})(?!655[4-9]\d)(?!6553[6-9])(?!0+)(?P<port>\d{1,5}))\b', flags=re.DOTALL)

    async def _pipe(self):
        headers = dict()
        headers["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.88 Safari/537.36"
        headers["Accept"] = "application/json, text/plain, */*"
        headers["Accept-Encoding"] = "gzip, deflate"
        headers["Accept-Language"] = "en-US,en;q=0.9"
        headers["Origin"] = "https://openproxy.space"
        headers["Referer"] = "https://openproxy.space/"

        timestamp = int(round(time.time() * 1000))
        page = await self.get('https://api.openproxy.space/list?skip=0&ts=%s' % timestamp, headers=headers)
        if not page:
            return

        jsonData = json.loads(page)
        codes = list(map(lambda x: x['code'], jsonData))

        if 'SOCKS5' in self.proto:
            index = 0
        elif 'SOCKS4' in self.proto:
            index = 1
        else:
            index = 2

        # urls = [
        #     'https://openproxy.space/list/%s' % path
        #     for path in codes[index::3]
        # ]
        # await self._find_on_pages(urls)

        await self._find_on_page('https://openproxy.space/list/%s' % codes[index])


class XroxyHttp(Provider):
    domain = 'www.xroxy.com^http'

    async def _pipe(self):
        urls = [
            'https://www.xroxy.com/proxylist.php?type=All_http&pnum=%d' % (i)
            for i in range(0, 9)
        ]
        await self._find_on_pages(urls)


class XroxySocks4(Provider):
    domain = 'www.xroxy.com^socks4'

    async def _pipe(self):
        urls = [
            'https://www.xroxy.com/proxylist.php?type=Socks4&pnum=%d' % (i)
            for i in range(0, 4)
        ]
        await self._find_on_pages(urls)


class XroxySocks5(Provider):
    domain = 'www.xroxy.com^socks5'

    async def _pipe(self):
        urls = [
            'https://www.xroxy.com/proxylist.php?type=Socks5&pnum=%d' % (i)
            for i in range(0, 4)
        ]
        await self._find_on_pages(urls)


class ProxyProvider(Provider):
    def __init__(self, *args, **kwargs):
        warnings.warn(
            '`ProxyProvider` is deprecated, use `Provider` instead.',
            DeprecationWarning,
        )
        super().__init__(*args, **kwargs)


PROVIDERS = [
    Provider(
        url='https://api.proxyscrape.com/?request=getproxies&proxytype=http',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25'),
    ),  # added by ZerGo0
    Provider(
        url='https://api.proxyscrape.com/?request=getproxies&proxytype=socks4',
        proto=('SOCKS4'),
    ),  # added by ZerGo0
    Provider(
        url='https://api.proxyscrape.com/?request=getproxies&proxytype=socks5',
        proto=('SOCKS5'),
    ),  # added by ZerGo0
    DidsoftHttp(
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25'),
    ),  # 800
    Provider(
        url='https://www.socks-proxy.net/',
        proto=('SOCKS4'),
    ),  # 300   by Didsoft Ltd.
    Openproxy_space(
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25'),
    ),
    Openproxy_space(
        proto=('SOCKS4'),
    ),
    Openproxy_space(
        proto=('SOCKS5'),
    ),
    XroxyHttp(
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25'),
    ),
    XroxySocks4(
        proto=('SOCKS4'),
    ),
    XroxySocks5(
        proto=('SOCKS5'),
    ),
    Provider(
        url='https://t.me/s/proxiesfine',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25'),
    ),  # 4200
    Provider(
        url='http://cn-proxy.com/archives/218',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25'),
    ),  # 70
    Provider(
        url='https://www.ipaddress.com/proxy-list/',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25'),
    ),  # 53
    Provider(
        url='http://pubproxy.com/api/proxy?limit=20&format=txt',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25'),
        max_conn=1,
    ),  # 20
    Proxy_list_org(proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),  # noqa; 140
    Xseo_in(proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),  # noqa; 240
    Spys_ru(proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),  # noqa; 660
    Proxylistplus_com(proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),  # noqa; 450
    Proxylist_me(proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),  # noqa; 2872
    Foxtools_ru(
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25'),
        max_conn=1
    ),  # noqa; 500
    Gatherproxy_com(proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),  # noqa; 3212
    Nntime_com(proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')),  # noqa; 1050
    Gatherproxy_com_socks(proto=('SOCKS4', 'SOCKS5')),  # noqa; 30
    My_proxy_com(max_conn=2),  # noqa; 1000
    Checkerproxy_net(),  # noqa; 60000
    Aliveproxy_com(),  # noqa; 210
    Webanetlabs_net(),  # noqa; 5000
    Proxylist_download(
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25'),
    ),  # noqa; 35590
    Provider(
        url='https://www.proxy-list.download/api/v1/get?type=socks4',
        proto=('SOCKS4'),
    ),  # 53
    Provider(
        url='https://www.proxy-list.download/api/v1/get?type=socks5',
        proto=('SOCKS5'),
    ),  # 53
    Provider(
        url='http://www.proxylists.net/',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25'),
    ),  # 49
    Provider(
        url='https://www.marcosbl.com/lab/proxies/',
        proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')
    ),  # 25+
    Proxynova_com(proto=('HTTP', 'CONNECT:80', 'HTTPS', 'CONNECT:25')), # 818
    Free_proxy_cz(),  # 420
]
