import http from 'node:http';
import { createDatabase } from './db.js';
import { runAgent, searchCatalog } from './agent.js';

const db = createDatabase();
const jobs = new Map();
const sessions = new Map();
const send = (res, status, data) => { res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'access-control-allow-origin': '*' }); res.end(JSON.stringify(data)); };
const readBody = (req) => new Promise((resolve) => { let data = ''; req.on('data', (part) => data += part); req.on('end', () => resolve(data ? JSON.parse(data) : {})); });

http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (req.method === 'OPTIONS') return send(res, 204, {});
  if (req.method === 'POST' && url.pathname === '/api/chat') {
    const body = await readBody(req);
    const sessionId = typeof body.sessionId === 'string' && body.sessionId.length <= 100 ? body.sessionId : 'anonymous';
    const session = sessions.get(sessionId) ?? { history: [] };
    sessions.set(sessionId, session);
    return send(res, 200, await runAgent(body.message ?? '', db, jobs, session));
  }
  if (req.method === 'GET' && url.pathname === '/api/catalog/search') return send(res, 200, searchCatalog(url.searchParams.get('q') ?? ''));
  if (req.method === 'GET' && url.pathname.startsWith('/api/jobs/')) return send(res, 200, jobs.get(url.pathname.split('/').pop()) ?? { error: 'Job not found' });
  if (req.method === 'GET' && url.pathname === '/api/openapi.json') return send(res, 200, { openapi: '3.0.0', info: { title: 'Local AWS Data Agent API', version: '1.0.0' }, paths: { '/api/chat': {}, '/api/catalog/search': {}, '/api/jobs/{run_id}': {} } });
  return send(res, 404, { error: 'Not found' });
}).listen(3001, () => console.log('API listening at http://localhost:3001'));
