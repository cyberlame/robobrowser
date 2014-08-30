"""
Robotic browser.
"""

import re
from bs4 import BeautifulSoup
from bs4.element import Tag

from robobrowser import helpers
from robobrowser import exceptions
from robobrowser.compat import urlparse
from robobrowser.forms.form import Form
from robobrowser.cache import RoboHTTPAdapter
from robobrowser.helpers import retry

import pycurl, curl, io
from urllib.parse import urlencode
import zlib

_link_ptn = re.compile(r'^(a|button)$', re.I)
_form_ptn = re.compile(r'^form$', re.I)


class DeflateDecoder(object):

    def __init__(self):
        self._first_try = True
        self._data = binary_type()
        self._obj = zlib.decompressobj()

    def __getattr__(self, name):
        return getattr(self._obj, name)

    def decompress(self, data):
        if not self._first_try:
            return self._obj.decompress(data)

        self._data += data
        try:
            return self._obj.decompress(data)
        except zlib.error:
            self._first_try = False
            self._obj = zlib.decompressobj(-zlib.MAX_WBITS)
            try:
                return self.decompress(self._data)
            finally:
                self._data = None


def _get_decoder(mode):
    if mode == 'gzip':
        return zlib.decompressobj(16 + zlib.MAX_WBITS)

    return DeflateDecoder()


class RoboCurl(object):
    def __init__(self):
        self.c = None
        self.reset()

    def reset(self):
        if self.c is not None:
            self.c.close()

        self.hdr = ""
        self.headers = {}
        self.payload = ""

        self.c = pycurl.Curl()
        self.c.setopt(pycurl.TIMEOUT, 40)
        self.c.setopt(pycurl.FOLLOWLOCATION, 1)
        self.c.setopt(pycurl.MAXREDIRS, 5)
        self.c.setopt(pycurl.COOKIEFILE, "/dev/null")
        self.c.setopt(pycurl.ENCODING, 'gzip, deflate')

        def header_callback(x):
            self.hdr += x.decode('ascii')
        self.set_option(pycurl.HEADERFUNCTION, header_callback)

    def set_socks(self, addr, port):
        self.c.setopt(pycurl.PROXY, addr)
        self.c.setopt(pycurl.PROXYPORT, port)
        self.c.setopt(pycurl.PROXYTYPE, pycurl.PROXYTYPE_SOCKS5)

    def _do_request(self, url, headers=None):
        self.hdr = ""
        self.headers = {}

        buf = io.BytesIO()
        self.c.setopt(pycurl.WRITEDATA, buf)
        self.c.setopt(pycurl.URL, url)
        if headers:
            self.c.setopt(pycurl.HTTPHEADER, headers)

        self.c.perform()
        self.payload = buf.getvalue()

        if headers:
            empty_headers = []
            for header in headers:
                if ":" not in header:
                    continue
                name, _ = header.split(":", 1)
                empty_headers.append(name+":")
            self.c.setopt(pycurl.HTTPHEADER, empty_headers)

        self.headers = self.parse_headers(self.hdr)
        encoding = self.headers.get('content-encoding', '')
        if encoding in ('gzip', 'deflate'):
            decoder = _get_decoder(encoding)
            self.payload = decoder.decompress(self.payload)
        return self.payload

    def parse_headers(self, headers):
        d = {}
        first = True
        for line in headers.split("\r\n"):
            if first:
                first = False
                self.status_line = line
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            d[name.lower()] = value.strip()
        return d

    def get(self, url, headers=None):
        self.c.setopt(pycurl.HTTPGET, 1)
        return self._do_request(url, headers)

    def post(self, url, params, headers=None):
        self.c.setopt(pycurl.POST, 1)
        self.c.setopt(pycurl.POSTFIELDS, urlencode(params))
        return self._do_request(url, headers)

    def set_timeout(self, timeout):
        self.c.setopt(pycurl.TIMEOUT, timeout)

    def set_option(self, *args):
        self.c.setopt(*args)
    
    def set_verbosity(self, level):
        self.set_option(pycurl.VERBOSE, level)

    @property
    def url(self):
        return self.c.getinfo(pycurl.EFFECTIVE_URL)


class RoboResponse(object):
    def __init__(self, session):
        self.content = session.payload
        self.url = session.url
        self.status_line = ""
        self.headers = session.headers


class RoboState(object):
    """Representation of a browser state. Wraps the browser and response, and
    lazily parses the response content.

    """

    def __init__(self, browser, response):
        self.browser = browser
        self.response = response
        self.url = self.response.url
        self._parsed = None

    @property
    def parsed(self):
        """Lazily parse response content, using HTML parser specified by the
        browser.

        """
        if self._parsed is None:
            self._parsed = BeautifulSoup(
                self.response.content,
                features=self.browser.parser,
            )
        return self._parsed


class RoboBrowser(object):
    """Robotic web browser. Represents HTTP requests and responses using the
    requests library and parsed HTML using BeautifulSoup.

    :param str parser: HTML parser; used by BeautifulSoup
    :param str user_agent: Default user-agent
    :param history: History length; infinite if True, 1 if falsy, else
        takes integer value

    :param int timeout: Default timeout, in seconds
    :param bool allow_redirects: Allow redirects on POST/PUT/DELETE

    :param int tries: Number of retries
    :param Exception errors: Exception or tuple of exceptions to catch
    :param int delay: Delay between retries
    :param int multiplier: Delay multiplier between retries

    """
    def __init__(self, session=None, parser=None, user_agent=None,
                 history=True, timeout=None, allow_redirects=True, tries=None,
                 errors=pycurl.error, delay=None, multiplier=None):

        self.session = session or RoboCurl()

        # Add default user agent string
        if user_agent is not None:
            self.session.set_option(pycurl.USERAGENT, user_agent)

        self.parser = parser
        if timeout is not None:
            self.session.set_timeout(timeout)
        
        self.session.set_option(pycurl.FOLLOWLOCATION, 
            1 if allow_redirects else 0)

        # Configure history
        self.history = history
        if history is True:
            self._maxlen = None
        elif not history:
            self._maxlen = 1
        else:
            self._maxlen = history
        self._states = []
        self._cursor = -1

        # Set up retries
        if tries:
            decorator = retry(tries, errors, delay, multiplier)
            self._open, self.open = self.open, decorator(self.open)
            self._submit_form, self.submit_form = \
                self.submit_form, decorator(self.submit_form)

    def __repr__(self):
        try:
            return '<RoboBrowser url={0}>'.format(self.url)
        except exceptions.RoboError:
            return '<RoboBrowser>'

    @property
    def state(self):
        if self._cursor == -1:
            raise exceptions.RoboError('No state')
        try:
            return self._states[self._cursor]
        except IndexError:
            raise exceptions.RoboError('Index out of range')

    @property
    def response(self):
        return self.state.response

    @property
    def url(self):
        return self.state.url

    @property
    def parsed(self):
        return self.state.parsed

    @property
    def find(self):
        """See ``BeautifulSoup::find``."""
        try:
            return self.parsed.find
        except AttributeError:
            raise exceptions.RoboError

    @property
    def find_all(self):
        """See ``BeautifulSoup::find_all``."""
        try:
            return self.parsed.find_all
        except AttributeError:
            raise exceptions.RoboError

    @property
    def select(self):
        """See ``BeautifulSoup::select``."""
        try:
            return self.parsed.select
        except AttributeError:
            raise exceptions.RoboError

    def _build_url(self, url):
        """Build absolute URL.

        :param url: Full or partial URL
        :return: Full URL

        """
        return urlparse.urljoin(
            self.url,
            url
        )

    @property
    def _default_send_args(self):
        """

        """
        return {
            # 'timeout': self.timeout,
            # 'allow_redirects': self.allow_redirects,
        }

    def _build_send_args(self, **kwargs):
        """Merge optional arguments with defaults.

        :param kwargs: Keyword arguments to `Session::send`

        """
        out = self._default_send_args
        out.update(kwargs)
        return out

    def open(self, url, **kwargs):
        """Open a URL.

        :param str url: URL to open
        :param kwargs: Keyword arguments to `Session::send`

        """
        response = self.session.get(url, **self._build_send_args(**kwargs))
        self._update_state(response)

    def _update_state(self, content):
        """Update the state of the browser. Create a new state object, and
        append to or overwrite the browser's state history.

        :param requests.MockResponse: New response object

        """
        # Clear trailing states
        self._states = self._states[:self._cursor + 1]

        # Append new state
        response = RoboResponse(self.session)

        state = RoboState(self, response)
        self._states.append(state)
        self._cursor += 1

        # Clear leading states
        if self._maxlen:
            decrement = len(self._states) - self._maxlen
            if decrement > 0:
                self._states = self._states[decrement:]
                self._cursor -= decrement

    def _traverse(self, n=1):
        """Traverse state history. Used by `back` and `forward` methods.

        :param int n: Cursor increment. Positive values move forward in the
            browser history; negative values move backward.

        """
        if not self.history:
            raise exceptions.RoboError('Not tracking history')
        cursor = self._cursor + n
        if cursor >= len(self._states) or cursor < 0:
            raise exceptions.RoboError('Index out of range')
        self._cursor = cursor

    def back(self, n=1):
        """Go back in browser history.

        :param int n: Number of pages to go back

        """
        self._traverse(-1 * n)

    def forward(self, n=1):
        """Go forward in browser history.

        :param int n: Number of pages to go forward

        """
        self._traverse(n)

    def get_link(self, text=None, *args, **kwargs):
        """Find an anchor or button by containing text, as well as standard
        BeautifulSoup arguments.

        :param text: String or regex to be matched in link text
        :return: BeautifulSoup tag if found, else None

        """
        return helpers.find(
            self.parsed, _link_ptn, text=text, *args, **kwargs
        )

    def get_links(self, text=None, *args, **kwargs):
        """Find anchors or buttons by containing text, as well as standard
        BeautifulSoup arguments.

        :param text: String or regex to be matched in link text
        :return: List of BeautifulSoup tags

        """
        return helpers.find_all(
            self.parsed, _link_ptn, text=text, *args, **kwargs
        )

    def get_form(self, id=None, *args, **kwargs):
        """Find form by ID, as well as standard BeautifulSoup arguments.

        :param str id: Form ID
        :return: BeautifulSoup tag if found, else None

        """
        if id:
            kwargs['id'] = id
        form = self.find(_form_ptn, *args, **kwargs)
        if form is not None:
            return Form(form)

    def get_forms(self, *args, **kwargs):
        """Find forms by standard BeautifulSoup arguments.
        :args: Positional arguments to `BeautifulSoup::find_all`
        :args: Keyword arguments to `BeautifulSoup::find_all`

        :return: List of BeautifulSoup tags

        """
        forms = self.find_all(_form_ptn, *args, **kwargs)
        return [
            Form(form)
            for form in forms
        ]

    def follow_link(self, link, **kwargs):
        """Click a link.

        :param Tag link: Link to click
        :param kwargs: Keyword arguments to `Session::send`

        """
        try:
            href = link['href']
        except KeyError:
            raise exceptions.RoboError('Link element must have "href" '
                                       'attribute')
        self.open(self._build_url(href), **kwargs)

    def submit_form(self, form, submit=None, **kwargs):
        """Submit a form.

        :param Form form: Filled-out form object
        :param Submit submit: Optional `Submit` to click, if form includes
            multiple submits
        :param kwargs: Keyword arguments to `Session::send`

        """
        # Get HTTP verb
        method = form.method.upper()

        # Send request
        url = self._build_url(form.action) or self.url
        payload = form.serialize(submit=submit)
        
        serialized = payload.to_requests(method)
        params = {k:v for k,v in serialized['data']}

        # send_args = self._build_send_args(**kwargs)
        # send_args.update(serialized)
        
        meth = self.session.get
        if method == "POST":
            meth = self.session.post
        
        self.session.payload = ""
        self.session.hdr = ""

        response = meth(url, params)

        # Update history
        self._update_state(response)
