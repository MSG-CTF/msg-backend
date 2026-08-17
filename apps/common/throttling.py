from rest_framework.throttling import SimpleRateThrottle

class LoginRateThrottle(SimpleRateThrottle):
    scope = "login"

    def get_cache_key(self,request, view):
        login_id = str(request.data.get("login_id") or "").strip().lower()
        return f"throttle_login_{self.get_ident(request)}_{login_id}"