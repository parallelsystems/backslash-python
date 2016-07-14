import gzip
import json
import random
import time

import requests
from requests.exceptions import ConnectionError, ReadTimeout

from munch import munchify
from sentinels import NOTHING
from urlobject import URLObject as URL

from ._compat import BytesIO, TextIOWrapper, iteritems
from .comment import Comment
from .error import Error
from .exceptions import BackslashClientException, ParamsTooLarge
from .session import Session
from .suite import Suite
from .test import Test
from .user import User
from .utils import raise_for_status
from .warning import Warning

_RETRY_STATUS_CODES = frozenset([
    requests.codes.bad_gateway,
    requests.codes.gateway_timeout,
])

_TYPES_BY_TYPENAME = {
    'session': Session,
    'test': Test,
    'error': Error,
    'warning': Warning,
    'comment': Comment,
    'suite': Suite,
    'user': User,
}


_COMPRESS_THRESHOLD = 4 * 1024
_MAX_PARAMS_SIZE = 5 * 1024 * 1024  # 5Mb


class API(object):

    def __init__(self, client, url, runtoken):
        super(API, self).__init__()
        self.client = client
        self.url = URL(url)
        self.runtoken = runtoken
        self.session = requests.Session()
        self.session.headers.update({
            'X-Backslash-run-token': self.runtoken})
        self.call = CallProxy(self)

    def info(self):
        """Inspects the remote API and returns information about its capabilities
        """
        resp = self.session.options(self.url.add_path('api'))
        resp.raise_for_status()
        info = resp.json()
        return munchify(info)

    def call_function(self, name, params=None):
        is_compressed, data = self._serialize_params(params)
        headers = {'Content-type': 'application/json'}
        if is_compressed:
            headers['Content-encoding'] = 'gzip'

        for _ in self._iter_retries():

            try:
                resp = self.session.post(
                    self.url.add_path('api').add_path(name), data=data, headers=headers)
            except (ConnectionError, ReadTimeout, ):
                continue

            if resp.status_code not in _RETRY_STATUS_CODES:
                break
        else:
            raise BackslashClientException(
                'Maximum number of retries exceeded for calling Backslash API')

        raise_for_status(resp)

        return self._normalize_return_value(resp)

    def _iter_retries(self, timeout=30, sleep_range=(3, 10)):
        start_time = time.time()
        end_time = start_time + timeout
        while True:
            yield
            if time.time() < end_time:
                time.sleep(random.randrange(*sleep_range))

    def get(self, path, raw=False, params=None):
        resp = self.session.get(self.url.add_path(path), params=params)
        raise_for_status(resp)
        if raw:
            return resp.json()
        else:
            return self._normalize_return_value(resp)

    def _normalize_return_value(self, response):
        json = response.json()
        if json is None:
            return None
        result = json.get('result')
        if result is None:
            if isinstance(json, dict):
                for key, value in json.items():
                    if isinstance(value, dict) and value.get('type') == key:
                        return self.build_api_object(value)
            return json
        elif isinstance(result, dict) and 'type' in result:
            return self.build_api_object(result)
        return result

    def build_api_object(self, result):
        objtype = self._get_objtype(result)
        if objtype is None:
            return result
        return objtype(self.client, result)

    def _get_objtype(self, json_object):
        typename = json_object['type']
        return _TYPES_BY_TYPENAME.get(typename)

    def _serialize_params(self, params):
        if params is None:
            params = {}

        returned = {}
        for param_name, param_value in iteritems(params):
            if param_value is NOTHING:
                continue
            returned[param_name] = param_value
        compressed = False
        returned = json.dumps(returned)
        if len(returned) > _COMPRESS_THRESHOLD:
            compressed = True
            returned = self._compress(returned)
        if len(returned) > _MAX_PARAMS_SIZE:
            raise ParamsTooLarge()
        return compressed, returned

    def _compress(self, data):
        s = BytesIO()

        with gzip.GzipFile(fileobj=s, mode='wb') as f:
            with TextIOWrapper(f) as w:
                w.write(data)

        return s.getvalue()


class CallProxy(object):

    def __init__(self, api):
        super(CallProxy, self).__init__()
        self._api = api

    def __getattr__(self, attr):
        if attr.startswith('_'):
            raise AttributeError(attr)

        def new_func(**params):
            return self._api.call_function(attr, params)

        new_func.__name__ = attr
        return new_func
