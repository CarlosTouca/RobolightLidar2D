// Cliente da API REST. Erros viram `ApiError` com a mensagem do servidor (em português).

export class ApiError extends Error {
  constructor(message, code = 'network') {
    super(message);
    this.code = code;
  }
}

async function request(method, path, body) {
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError('servidor indisponível', 'network');
  }
  let data = null;
  try { data = await res.json(); } catch { /* corpo vazio */ }
  if (!res.ok) {
    const e = data && data.error;
    throw new ApiError(e ? e.message : `erro HTTP ${res.status}`, e ? e.code : 'http');
  }
  return data;
}

export const api = {
  config: () => request('GET', '/api/config'),
  ports: () => request('GET', '/api/ports'),
  status: () => request('GET', '/api/status'),
  connect: (port) => request('POST', '/api/connect', { port }),
  disconnect: () => request('POST', '/api/disconnect'),
  start: () => request('POST', '/api/scan/start'),
  stop: () => request('POST', '/api/scan/stop'),
  resetPose: () => request('POST', '/api/pose/reset'),
  rotation: (hz) => request('POST', '/api/rotation', { hz }),
  steer: (x, y, speed) => request('POST', '/api/steer', { x, y, speed }),
};
