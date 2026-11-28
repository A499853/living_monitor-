/* Optional wx_channels_download global user script. Identity capture only. */
(() => {
  'use strict';
  const endpoint = __LIVEWATCH_CAPTURE_ENDPOINT__;
  if (typeof WXU === 'undefined' || typeof WXU.check_live_existing !== 'function') return;
  let button;
  function identifier(value) {
    // Never round uint64 identifiers through JavaScript Number.
    if (typeof value === 'string') return value;
    if (Number.isSafeInteger(value)) return String(value);
    return '';
  }
  function current() {
    const [error, profile, live] = WXU.check_live_existing({silence: true});
    if (error || !profile || !live || !live.liveInfo) return null;
    if (profile.objectDesc?.mediaType !== 9) return null;
    const contact = profile.contact || profile.anchorContact || {};
    const data = {
      object_id: identifier(profile.id), nonce_id: profile.objectNonceId || '',
      live_id: identifier(live.liveInfo.liveId), username: contact.username || '',
      nickname: (contact.nickname || '').slice(0, 160)
    };
    if (profile.liveInfo?.liveId && identifier(profile.liveInfo.liveId) !== data.live_id) return null;
    return data.object_id && data.nonce_id && data.live_id ? data : null;
  }
  function mount() {
    if (!document.body || !current()) return;
    if (button) return;
    button = document.createElement('button');
    button.textContent = '捕获到直播守望';
    button.type = 'button';
    Object.assign(button.style, {position: 'fixed', right: '18px', top: '80px',
      zIndex: '2147483647', background: '#168653', color: '#fff', border: '0',
      borderRadius: '6px', padding: '10px 14px', cursor: 'pointer', fontSize: '14px'});
    button.onclick = async () => {
      const payload = current();
      if (!payload) { button.textContent = '请重新打开当前直播'; return; }
      button.disabled = true;
      try {
        const [err] = await WXU.request({method: 'POST', url: endpoint, body: payload});
        button.textContent = err ? '连接失败，点击重试' : '已捕获，请回直播守望加入监控';
      } catch (_) {
        button.textContent = '连接失败，点击重试';
      } finally { button.disabled = false; }
    };
    document.body.append(button);
  }
  // Upstream stores the event data in its own listener; run after all handlers.
  for (const name of ['onJoinLive', 'onFetchFeedProfile', 'onDOMContentLoaded']) {
    if (typeof WXU[name] === 'function') WXU[name](() => setTimeout(mount, 0));
  }
  setTimeout(mount, 0);
})();
