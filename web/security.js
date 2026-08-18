(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.HerdrSecurity = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  function stripTokenFromUrl(rawUrl) {
    const url = new URL(rawUrl);
    const changed = url.searchParams.has('token');
    if (changed) url.searchParams.delete('token');
    return {
      changed,
      relativeUrl: `${url.pathname}${url.search}${url.hash}`,
    };
  }

  function sanitizeSavedSessions(rawSessions) {
    if (!rawSessions) return { changed: false, sessions: [] };

    let sessions;
    try {
      sessions = JSON.parse(rawSessions);
    } catch {
      return { changed: false, sessions: [] };
    }
    if (!Array.isArray(sessions)) return { changed: false, sessions: [] };

    let changed = false;
    const sanitized = sessions.map(session => {
      if (!session || typeof session !== 'object' || !Object.hasOwn(session, 'token')) {
        return session;
      }
      const { token: _discardedToken, ...safeSession } = session;
      changed = true;
      return safeSession;
    });
    return { changed, sessions: sanitized };
  }

  function createAuthenticatedConnection(url, token) {
    return {
      url,
      authMessage: { type: 'auth', protocol: 1, token },
    };
  }

  function clearLegacyRelayToken(storage) {
    storage.removeItem('herdr_relay_token');
  }

  return {
    clearLegacyRelayToken,
    createAuthenticatedConnection,
    sanitizeSavedSessions,
    stripTokenFromUrl,
  };
}));
