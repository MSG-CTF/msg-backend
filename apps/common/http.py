from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler


class SameHostRedirectHandler(HTTPRedirectHandler):
    """리다이렉트를 따라가면 Authorization 헤더도 함께 간다. 다른 호스트로는 토큰을 보내지 않는다.

    같은 호스트로의 이동(http → https, 경로 정리)은 그대로 따른다. 거절은 연결 실패와 같게 다룬다.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).hostname != urlsplit(req.full_url).hostname:
            raise URLError("다른 호스트로의 리다이렉트는 따르지 않습니다")
        return super().redirect_request(req, fp, code, msg, headers, newurl)
