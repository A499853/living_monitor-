"""Persistent local Douyin authorization; never exposes cookie values in status.

The saved authorization has no application-imposed expiry. Douyin still owns
the validity of its server-side session; a transport error or anonymous probe
restriction must never silently delete the user's saved authorization.
"""

def cookie_is_authorized(value):
    if not isinstance(value,str) or not value or len(value)>32768 or any(c in value for c in '\r\n\x00'):
        return False
    # Douyin's signed-in browser session, not an anonymous bootstrap cookie.
    parts={item.split('=',1)[0].strip():item.split('=',1)[1].strip() for item in value.split(';') if '=' in item}
    return any(parts.get(key) for key in ('sessionid','sessionid_ss','sid_tt'))


def authorization_status(value):
    """Describe local persistence, without claiming to validate a remote session."""
    authorized = cookie_is_authorized(value)
    return {
        'douyin_authorized': authorized,
        'local_authorization_permanent': authorized,
        'expires_at': None,
    }


def authorized_monitor_cookie(value):
    """Validate locally captured credentials without exposing or decoding them."""
    if not cookie_is_authorized(value):
        raise ValueError('授权监控需要先在抖音官方窗口完成扫码或手机验证码登录。')
    try:
        value.encode('latin-1')
    except UnicodeError:
        raise ValueError('登录资料格式无效，请在抖音官方窗口重新登录。') from None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('登录资料格式无效，请在抖音官方窗口重新登录。')
    return value.strip()
