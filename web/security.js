(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.HerdrSecurity = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  function sanitizeRelayUrl(rawUrl) {
    if (!rawUrl) return { changed: false, url: rawUrl };

    let url;
    try {
      url = new URL(rawUrl);
    } catch {
      return { changed: false, url: rawUrl };
    }

    let changed = false;
    if (url.searchParams.has('token')) {
      url.searchParams.delete('token');
      changed = true;
    }

    const hash = url.hash.slice(1);
    const hashQueryIndex = hash.indexOf('?');
    const hashParameters = new URLSearchParams(
      hashQueryIndex === -1 ? hash : hash.slice(hashQueryIndex + 1)
    );
    if (hashParameters.has('token')) {
      hashParameters.delete('token');
      const cleanParameters = hashParameters.toString();
      const hashPrefix = hashQueryIndex === -1 ? '' : hash.slice(0, hashQueryIndex);
      url.hash = cleanParameters
        ? `${hashPrefix ? `${hashPrefix}?` : ''}${cleanParameters}`
        : hashPrefix;
      changed = true;
    }

    return { changed, url: url.toString() };
  }

  function stripTokenFromUrl(rawUrl) {
    const sanitized = sanitizeRelayUrl(rawUrl);
    if (!sanitized.url) return { changed: false, relativeUrl: '' };

    const url = new URL(sanitized.url);
    return {
      changed: sanitized.changed,
      relativeUrl: `${url.pathname}${url.search}${url.hash}`,
    };
  }

  function isSuccessfulAuthResult(message) {
    return Boolean(
      message
      && message.type === 'auth_result'
      && message.protocol === 1
      && message.ok === true
    );
  }

  function createConnectionController({
    cancelReconnect,
    onAuthenticated,
    onMessage,
    onSocketChange,
    onStatus,
    openSocket,
    scheduleReconnect,
  }) {
    let socket = null;
    let reconnectHandle = null;

    function isCurrent(candidate) {
      return candidate === socket;
    }

    function cancelPendingReconnect() {
      if (reconnectHandle === null) return;
      cancelReconnect(reconnectHandle);
      reconnectHandle = null;
    }

    function clearCurrentSocket(candidate) {
      if (!isCurrent(candidate)) return false;
      socket = null;
      onSocketChange(null);
      return true;
    }

    function rejectAuthentication(candidate) {
      if (!clearCurrentSocket(candidate)) return;
      cancelPendingReconnect();
      onStatus('disconnected');
      candidate.close(1008, 'Unauthorized');
    }

    function disconnect() {
      cancelPendingReconnect();
      const previousSocket = socket;
      socket = null;
      onSocketChange(null);
      if (previousSocket) previousSocket.close();
    }

    function connect(connection, reconnect) {
      disconnect();
      onStatus('connecting');
      const currentSocket = openSocket(connection.url);
      socket = currentSocket;
      onSocketChange(currentSocket);
      let awaitingAuthentication = Boolean(connection.authMessage.token);

      currentSocket.onopen = () => {
        if (!isCurrent(currentSocket)) return;
        if (!awaitingAuthentication) {
          onStatus('connected');
          onAuthenticated();
          return;
        }
        onStatus('authenticating');
        currentSocket.send(JSON.stringify(connection.authMessage));
      };
      currentSocket.onclose = () => {
        if (!clearCurrentSocket(currentSocket)) return;
        onStatus('disconnected');
        reconnectHandle = scheduleReconnect(() => {
          reconnectHandle = null;
          reconnect();
        });
      };
      currentSocket.onerror = () => {
        if (isCurrent(currentSocket)) onStatus('disconnected');
      };
      currentSocket.onmessage = event => {
        if (!isCurrent(currentSocket)) return;

        let message;
        try {
          message = JSON.parse(event.data);
        } catch {
          if (awaitingAuthentication) rejectAuthentication(currentSocket);
          return;
        }

        if (awaitingAuthentication) {
          if (!isSuccessfulAuthResult(message)) {
            rejectAuthentication(currentSocket);
            return;
          }
          awaitingAuthentication = false;
          onStatus('connected');
          onAuthenticated();
          return;
        }

        if (message.type === 'auth_result') return;
        onMessage(message);
      };
      return currentSocket;
    }

    return { connect, disconnect };
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
      if (!session || typeof session !== 'object') return session;

      let safeSession = session;
      if (Object.hasOwn(session, 'token')) {
        const { token: _discardedToken, ...withoutToken } = session;
        safeSession = withoutToken;
        changed = true;
      }

      const cleanUrl = sanitizeRelayUrl(safeSession.url);
      if (!cleanUrl.changed) return safeSession;
      changed = true;
      return { ...safeSession, url: cleanUrl.url };
    });
    return { changed, sessions: sanitized };
  }

  function createAuthenticatedConnection(url, token) {
    return {
      url: sanitizeRelayUrl(url).url,
      authMessage: { type: 'auth', protocol: 1, token },
    };
  }

  function clearLegacyRelayToken(storage) {
    storage.removeItem('herdr_relay_token');
  }

  return {
    clearLegacyRelayToken,
    createConnectionController,
    createAuthenticatedConnection,
    sanitizeSavedSessions,
    sanitizeRelayUrl,
    stripTokenFromUrl,
  };
}));
