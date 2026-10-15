// Saves the share card of a story page (docs/share-card.png). Chrome via the DevTools
// protocol, no dependencies: node tools/brand/share-card.mjs <story page URL> <out.png>
import { spawn } from 'node:child_process';
import { readFileSync, writeFileSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

const [url, out] = process.argv.slice(2);
const profile = mkdtempSync(path.join(tmpdir(), 'ghis-card-'));
const chrome = spawn(process.env.GHIS_CHROME || 'google-chrome', ['--headless=new', '--remote-debugging-port=0',
	'--no-first-run', '--no-sandbox', '--disable-gpu', `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore', detached: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let port;
while (!port) { try { port = readFileSync(`${profile}/DevToolsActivePort`, 'utf8').split('\n')[0]; } catch { await sleep(100); } }
let page;
while (!page) { try { page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((t) => t.type === 'page'); } catch { await sleep(100); } }
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener('open', r));
let id = 0;
const waiting = new Map();
const events = new Map();
ws.addEventListener('message', (e) => {
	const m = JSON.parse(e.data);
	if (waiting.has(m.id)) { waiting.get(m.id)(m.result); waiting.delete(m.id); }
	else if (events.has(m.method)) { events.get(m.method)(); events.delete(m.method); }
});
const send = (method, params = {}) => new Promise((r) => { waiting.set(++id, r); ws.send(JSON.stringify({ id, method, params })); });
await send('Page.enable');
const loaded = new Promise((r) => events.set('Page.loadEventFired', r));
await send('Page.navigate', { url });
await loaded;
await sleep(500);
const { result } = await send('Runtime.evaluate', { returnByValue: true, expression: 'window.ghisShareCard().toDataURL("image/png")' });
writeFileSync(out, Buffer.from(result.value.split(',')[1], 'base64'));
ws.close();
try { process.kill(-chrome.pid, 'SIGTERM'); } catch { chrome.kill(); }
await sleep(500);
try { rmSync(profile, { recursive: true, force: true }); } catch { /* a temp folder */ }
console.log(`saved ${out}`);
