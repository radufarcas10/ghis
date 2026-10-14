// Every page at phone size, under real mobile emulation (what DevTools' device mode does):
// nothing may make the page wider than the screen, or phones zoom the whole page out.
// Needs Chrome or Chromium and Python; skipped when either is missing.
//   node --test tests/js/*.test.js
const test = require('node:test');
const assert = require('node:assert/strict');
const { spawn, execFileSync, spawnSync } = require('node:child_process');
const { mkdtempSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const ROOT = path.resolve(__dirname, '..', '..');
const CHROME = process.env.GHIS_CHROME
	|| ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser']
		.find((name) => spawnSync('which', [name]).status === 0);
const PYTHON = ['python3', 'python'].find((name) => spawnSync(name, ['--version']).status === 0);
const skip = !CHROME ? 'no Chrome or Chromium found'
	: !PYTHON ? 'no Python found'
		: typeof WebSocket === 'undefined' ? 'needs Node 22+ (global WebSocket)' : false;

const PHONE = { width: 393, height: 852, deviceScaleFactor: 2, mobile: true }; // iPhone 16
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function openChrome() {
	const port = 9400 + Math.floor(Math.random() * 400);
	const profile = mkdtempSync(path.join(tmpdir(), 'ghis-chrome-'));
	const proc = spawn(CHROME, ['--headless=new', `--remote-debugging-port=${port}`, '--no-first-run',
		'--no-sandbox', '--disable-gpu', `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore' });
	let page;
	for (let i = 0; i < 100 && !page; i++) {
		try {
			page = (await (await fetch(`http://127.0.0.1:${port}/json`)).json()).find((t) => t.type === 'page');
		} catch { await sleep(100); }
	}
	const ws = new WebSocket(page.webSocketDebuggerUrl);
	await new Promise((resolve) => ws.addEventListener('open', resolve));
	let id = 0;
	const waiting = new Map();
	ws.addEventListener('message', (event) => {
		const message = JSON.parse(event.data);
		if (waiting.has(message.id)) { waiting.get(message.id)(message.result); waiting.delete(message.id); }
	});
	const send = (method, params = {}) => new Promise((resolve) => {
		waiting.set(++id, resolve);
		ws.send(JSON.stringify({ id, method, params }));
	});
	const close = async () => {
		ws.close();
		const exited = new Promise((resolve) => proc.once('exit', resolve));
		proc.kill();
		await exited; // Chrome writes to its profile until it is gone
		rmSync(profile, { recursive: true, force: true, maxRetries: 5 });
	};
	return { send, close };
}

// The page's width against the phone's, and which elements stick out. (A wide ::before or
// ::after shows up only in the page width: pseudo-elements cannot be measured directly.)
const MEASURE = `(() => {
	const vw = document.documentElement.clientWidth;
	const culprits = [...document.querySelectorAll('body *')]
		.filter((el) => !el.closest('.frame, .chart-scroll, .table-wrap, .years')) // scroll or clip by design
		.filter((el) => el.getBoundingClientRect().right > vw + 1)
		.map((el) => el.tagName.toLowerCase() + (typeof el.className === 'string' && el.className ? '.' + el.className : ''));
	return { page: document.documentElement.scrollWidth, phone: vw, culprits: culprits.slice(0, 5) };
})()`;

test('every page fits a phone without zooming out', { skip, timeout: 120000 }, async (t) => {
	const out = mkdtempSync(path.join(tmpdir(), 'ghis-mobile-'));
	t.after(() => rmSync(out, { recursive: true, force: true }));
	execFileSync(PYTHON, [path.join(ROOT, 'ghis.py'), 'demo', '--provider', 'gitlab', '--out', out], { stdio: 'ignore' });

	const pages = {
		'story': pathToFileURL(path.join(out, 'index.html')).href,
		'website': pathToFileURL(path.join(ROOT, 'site', 'index.html')).href,
		'app landing page': pathToFileURL(path.join(ROOT, 'src', 'ghis', 'web', 'connect.html')).href,
	};
	const chrome = await openChrome();
	t.after(chrome.close);
	await chrome.send('Emulation.setDeviceMetricsOverride', PHONE);
	await chrome.send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'reduce' }] });

	for (const [name, url] of Object.entries(pages)) {
		await chrome.send('Page.navigate', { url });
		await sleep(2500);
		const { result } = await chrome.send('Runtime.evaluate', { expression: MEASURE, returnByValue: true });
		const { page, phone, culprits } = result.value;
		assert.ok(page <= phone, `${name} is ${page}px wide on a ${phone}px phone; sticking out: ${culprits.join(', ') || 'a pseudo-element'}`);
	}
});
