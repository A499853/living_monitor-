"""Extract mobile share text without interpreting its prose as room IDs."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

_URL=re.compile(r'''https?://[^\s<>"'`\u200b\u200c\u200d\ufeff]+''',re.I)
_SHARE=re.compile(r'长按复制|复制下方链接|正在直播|查看\s*(?:TA|Ta|ta|他|她)\s*的更多作品|在抖音[，,]|打开[【「]?抖音|直接观看直播')
_TRAILER=re.compile(r'(?:(?:\d+[-－、.])|(?:[0-9]+@[0-9]+\.com)|(?::?\d+(?:am|pm))|[\s:：;；,，。!！#-])+',re.I)
_HOSTS={'live.douyin.com','v.douyin.com','www.douyin.com','douyin.com','www.iesdouyin.com','iesdouyin.com','m.iesdouyin.com','webcast.amemv.com'}


def unescape_share_text(value):
    # Markdown/clipboard exports sometimes escape just the scheme's colon.
    return re.sub(r'(?i)\b(https?)\\+:(?=//)',r'\1:',value).replace('\u200b','').replace('\ufeff','')


def urls_in_text(value):
    value=unescape_share_text(value)
    return [match.group(0).rstrip('，。；！）》】」』,.!;)]}') for match in _URL.finditer(value)]


def canonical_share_url(value):
    parsed=urlsplit(value)
    host=(parsed.hostname or '').lower()
    if host not in _HOSTS or parsed.scheme.lower() not in ('http','https'):
        raise ValueError('请使用抖音官方主页、直播间或手机分享链接')
    try: port=parsed.port
    except ValueError: raise ValueError('分享链接端口无效') from None
    if parsed.username is not None or parsed.password is not None or port not in (None,443 if parsed.scheme.lower()=='https' else 80):
        raise ValueError('分享链接地址无效')
    if host=='v.douyin.com':
        if not re.fullmatch(r'/[A-Za-z0-9_-]{1,64}/?',parsed.path):
            raise ValueError('抖音短链接缺少有效分享标识')
        # Only the token identifies a short link; sharing/tracking parameters
        # are neither needed for resolution nor part of duplicate detection.
        return 'https://v.douyin.com/'+parsed.path.strip('/')+'/'
    try: params=parse_qs(parsed.query,keep_blank_values=True,max_num_fields=64)
    except ValueError: raise ValueError('分享链接参数过多，请重新复制短链接') from None
    profile=re.fullmatch(r'/(?:share/)?user/(MS4[A-Za-z0-9._-]{8,200})/?',parsed.path)
    if profile and host in ('www.douyin.com','douyin.com','www.iesdouyin.com','iesdouyin.com','m.iesdouyin.com'):
        sec_uid=profile[1]
        for field in ('sec_uid','sec_user_id'):
            if field in params and params[field]!=[sec_uid]:
                raise ValueError('主页链接中的账号标识不一致，请重新分享')
        return 'https://www.douyin.com/user/'+sec_uid
    if host=='webcast.amemv.com' and re.fullmatch(r'/(?:douyin/)?webcast/reflow/[0-9]{1,32}/?',parsed.path):
        # sec_user_id is target identity, not tracking. Keep it in exchanges.
        owner=params.get('sec_user_id',params.get('sec_uid',[]))
        if len(owner)!=1 or not re.fullmatch(r'MS4[A-Za-z0-9._-]{8,200}',owner[0]):
            raise ValueError('手机直播分享缺少唯一的主播标识，请重新复制完整分享链接')
        if int(parsed.path.rstrip('/').rsplit('/',1)[1])<=0:
            raise ValueError('手机直播分享缺少有效场次，请重新分享')
        for field in ('sec_uid','sec_user_id'):
            if field in params and params[field]!=owner:
                raise ValueError('手机直播分享中的主播标识不一致，请重新分享')
        return urlunsplit(('https',host,parsed.path.rstrip('/'),urlencode({'sec_user_id':owner[0]}),''))
    return urlunsplit((parsed.scheme.lower(),host,parsed.path,parsed.query,''))


def normalize_douyin_text(value):
    value=unescape_share_text(value).strip()
    urls=urls_in_text(value)
    if not urls: return value
    if len(urls)!=1:
        raise ValueError('一条直播间只能填写一个链接，多条分享请使用批量添加')
    return canonical_share_url(urls[0])


def split_douyin_batch(value):
    """Keep legacy one-ID-per-line input; collapse identifiable share blocks.

    Only blocks containing recognizable Douyin share prose absorb numeric
    prefixes/trailing codes. Arbitrary standalone IDs keep their original order.
    No network request is needed during adding/importing.
    """
    value=unescape_share_text(value)
    result=[]
    for paragraph in re.split(r'\n\s*\n',value.replace('\r\n','\n').replace('\r','\n')):
        lines=[line.strip() for line in paragraph.splitlines() if line.strip()]
        if not lines: continue
        shared=any(_SHARE.search(line) for line in lines)
        if not shared:
            # One pasted line can contain multiple complete share messages.
            for line in lines:
                links=urls_in_text(line)
                result.extend(links if len(links)>1 else [line])
            continue
        found=any(urls_in_text(line) for line in lines)
        if not found:
            result.append('\n'.join(lines))
            continue
        for line in lines:
            links=urls_in_text(line)
            if links:
                result.extend(links)
            elif re.fullmatch(r'@?[A-Za-z0-9._-]{2,64}',line) and not _TRAILER.fullmatch(line):
                # Explicit standalone IDs remain entries in a mixed batch;
                # surrounding share prose never becomes a phantom room.
                result.append(line)
    return result
