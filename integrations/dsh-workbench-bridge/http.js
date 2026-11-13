import http from 'node:http';

export class BridgeError extends Error {
  constructor(message, payload, status) {
    super(message);
    this.payload = payload;
    this.status = status;
  }
}

export function parseWorkbenchUrl(value) {
  let url;
  try { url = new URL(value); } catch { throw new BridgeError('请提供工作台链接，例如 http://127.0.0.1:8765/?session=<方案ID>。'); }
  if (url.protocol !== 'http:' || !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)
      || url.username || url.password || url.pathname !== '/' || url.hash || url.port === '0'
      || [...url.searchParams.keys()].some(key => key !== 'session') || url.searchParams.getAll('session').length !== 1
      || !/^[a-f0-9]{32}$/.test(url.searchParams.get('session') ?? '')) {
    throw new BridgeError('仅接受无凭据的本机工作台 HTTP 链接，且必须包含一个32位方案 session ID。');
  }
  if (url.hostname === 'localhost') url.hostname = '127.0.0.1';
  return { origin: url.origin, sid: url.searchParams.get('session'), url: url.href };
}

export function requestJSON(origin, path, body, { signal, timeout = 10000 } = {}) {
  const encoded = body === undefined ? undefined : Buffer.from(JSON.stringify(body), 'utf8');
  if (encoded?.length > 2 * 1024 * 1024) throw new BridgeError('工作台请求过大，请缩小本次内容。');
  const deadline = AbortSignal.timeout(timeout);
  const combined = signal ? AbortSignal.any([signal, deadline]) : deadline;
  return new Promise((resolve, reject) => {
    const request = http.request(new URL(path, origin), {
      method: body === undefined ? 'GET' : 'POST', signal: combined,
      headers: { Accept: 'application/json', ...(encoded ? { 'Content-Type': 'application/json', 'Content-Length': encoded.length } : {}) },
    }, response => {
      const chunks = []; let bytes = 0;
      response.on('data', chunk => {
        bytes += chunk.length;
        if (bytes > 8 * 1024 * 1024) { response.destroy(); reject(new BridgeError('工作台响应超过8 MiB，请分页读取。')); }
        else chunks.push(chunk);
      });
      response.on('end', () => {
        let data;
        try { data = JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch { reject(new BridgeError('工作台未返回有效 JSON。')); return; }
        if (!data || typeof data !== 'object' || Array.isArray(data)) { reject(new BridgeError('工作台返回格式无效。')); return; }
        if (response.statusCode < 200 || response.statusCode >= 300) {
          reject(new BridgeError(`工作台请求失败（${response.statusCode}）：${String(data.error ?? '请检查工作台状态。').slice(0, 500)}`, data, response.statusCode));
        } else resolve(data);
      });
      response.on('error', () => reject(new BridgeError('工作台响应中断，请重新读取状态后再提交。')));
    });
    request.on('error', () => reject(new BridgeError('无法连接本机工作台或请求已取消；请检查链接与服务状态。')));
    if (encoded) request.write(encoded);
    request.end();
  });
}
