import requests

from src.exceptions import RequestError, TwitchAPIError, TwitchAPIErrorNotFound, TwitchAPIErrorForbidden, \
    TwitchAPIErrorBadRequest


class Api:
    """
    Sends requests to a specified API endpoint.
    """
    def __init__(self, pushbullet_key):

        self.pushbullet_key = pushbullet_key

    def get_request(self, url, p=None, h=None):
        """
        Wrapper for get requests for catching exceptions and status code issues.\n

            :param url: http/s endpoint to send request to
            :param p: parameter(s) to pass with request
            :param h: header(s) to pass with request
            :return: entire requests response
            :raises requestError: on requests module error
            :raises twitchAPIErrorBadRequest: on http code 400
            :raises twitchAPIErrorForbidden: on http code 403
            :raises twitchAPIErrorNotFound: on http code 404
            :raises twitchAPIError: on any http code other than 400, 403, 404 or 200
        """
        try:
            if p is None:
                _r = requests.get(url, headers=h)

            else:
                _r = requests.get(url, params=p)

        except requests.exceptions.RequestException as e:
            raise RequestError(self.pushbullet_key, url, e)

        if _r.status_code == 400:
            raise TwitchAPIErrorBadRequest(self.pushbullet_key, url, _r.status_code, _r.text)

        if _r.status_code == 403:
            raise TwitchAPIErrorForbidden(self.pushbullet_key, url, _r.status_code, _r.text)

        if _r.status_code == 404:
            raise TwitchAPIErrorNotFound(self.pushbullet_key, url, _r.status_code, _r.text)

        if _r.status_code != 200:
            raise TwitchAPIError(self.pushbullet_key, url, _r.status_code, _r.text)

        return _r

    def get_request_with_session(self, url, session):
        """Wrapper for get requests using a session for catching exceptions and status code issues.

        :param url: http/s endpoint to send request to
        :param session: a requests session for sending request
        :return: entire requests response
        """
        try:
            _r = session.get(url)

        except requests.exceptions.RequestException as e:
            raise RequestError(self.pushbullet_key, url, e)

        if _r.status_code == 400:
            raise TwitchAPIErrorBadRequest(self.pushbullet_key, url, _r.status_code, _r.text)

        if _r.status_code == 403:
            raise TwitchAPIErrorForbidden(self.pushbullet_key, url, _r.status_code, _r.text)

        if _r.status_code == 404:
            raise TwitchAPIErrorNotFound(self.pushbullet_key, url, _r.status_code, _r.text)

        if _r.status_code != 200:
            raise TwitchAPIError(self.pushbullet_key, url, _r.status_code, _r.text)

        return _r

    def post_request(self, url, d=None, j=None, h=None):
        """Wrapper for post requests for catching exceptions and status code issues.

        :param url: http/s endpoint to send request to
        :param d: data to send with request
        :param j: data to send with request as json
        :param h: headers to send with request
        :return: entire requests response
        """
        try:
            if j is None:
                _r = requests.post(url, data=d, headers=h)

            elif d is None:
                _r = requests.post(url, json=j, headers=h)

        except requests.exceptions.RequestException as e:
            raise RequestError(self.pushbullet_key, url, e)

        if _r.status_code != 200:
            raise TwitchAPIError(self.pushbullet_key, url, _r.status_code, _r.text)

        return _r