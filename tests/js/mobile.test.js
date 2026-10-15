// Every page at phone size, under real mobile emulation (what DevTools' device mode does):
// nothing may make the page wider than the screen, or phones zoom the whole page out.
// Needs Chrome or Chromium and Python; skipped when either is missing.
//   node --test tests/js/*.test.js
const test = require('node:test');
const assert = require('node:assert/strict');
const { spawn, execFileSync, spawnSync } = require('node:child_process');
const { mkdtempSync, rmSync, readFileSync, existsSync } = require('node:fs');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const ROOT = path.resolve(__dirname, '..', '..');
// Chrome or Chromium on Linux, macOS or Windows; GHIS_CHROME wins.
function findChrome() {
	if (process.env.GHIS_CHROME) return process.env.GHIS_CHROME;
	const dirs = (process.env.PATH || '').split(path.delimiter);
	for (const name of ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser']) {
		for (const dir of dirs) if (existsSync(path.join(dir, name))) return path.join(dir, name);
	}
	return [
		'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
		'/Applications/Chromium.app/Contents/MacOS/Chromium',
		...[process.env.PROGRAMFILES, process.env['PROGRAMFILES(X86)'], process.env.LOCALAPPDATA]
			.filter(Boolean).map((base) => path.join(base, 'Google', 'Chrome', 'Application', 'chrome.exe')),
	].find((candidate) => existsSync(candidate));
}
const CHROME = findChrome();
const PYTHON = ['python3', 'python', 'py'].find((name) => spawnSync(name, ['--version']).status === 0);
const skip = !CHROME ? 'no Chrome or Chromium found'
	: !PYTHON ? 'no Python found'
		: typeof WebSocket === 'undefined' ? 'needs Node 22+ (global WebSocket)' : false;

const PHONE = { width: 393, height: 852, deviceScaleFactor: 2, mobile: true }; // iPhone 16
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Waits on real signals rather than fixed sleeps, so a slow CI machine is only slower.
async function openChrome() {
	const profile = mkdtempSync(path.join(tmpdir(), 'ghis-chrome-'));
	// Port 0: Chrome picks a free port and writes it to DevToolsActivePort, so no collisions.
	const proc = spawn(CHROME, ['--headless=new', '--remote-debugging-port=0', '--no-first-run',
		'--no-default-browser-check', '--no-sandbox', '--disable-gpu', `--user-data-dir=${profile}`,
		'about:blank'], { stdio: 'ignore', detached: process.platform !== 'win32' });
	// `google-chrome` is a launcher script: the browser and its helpers are its children.
	// Their own process group lets one signal stop them all.
	let port;
	for (let i = 0; i < 300 && !port; i++) {
		try { port = readFileSync(path.join(profile, 'DevToolsActivePort'), 'utf8').split('\n')[0]; } catch { await sleep(100); }
	}
	assert.ok(port, 'Chrome did not start within 30 s');
	let page;
	for (let i = 0; i < 100 && !page; i++) {
		try {
			page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((t) => t.type === 'page');
		} catch { /* not listening yet */ }
		if (!page) await sleep(100);
	}
	assert.ok(page, 'Chrome has no page to drive');

	const ws = new WebSocket(page.webSocketDebuggerUrl);
	await new Promise((resolve, reject) => { ws.addEventListener('open', resolve); ws.addEventListener('error', reject); });
	let id = 0;
	const replies = new Map();
	const listeners = new Map();
	ws.addEventListener('message', (event) => {
		const message = JSON.parse(event.data);
		if (message.id && replies.has(message.id)) {
			const { resolve, reject } = replies.get(message.id);
			replies.delete(message.id);
			if (message.error) reject(new Error(`${message.error.message} (${message.error.code})`));
			else resolve(message.result);
		} else if (message.method && listeners.has(message.method)) {
			listeners.get(message.method)();
			listeners.delete(message.method);
		}
	});
	const send = (method, params = {}) => new Promise((resolve, reject) => {
		replies.set(++id, { resolve, reject });
		ws.send(JSON.stringify({ id, method, params }));
	});
	const next = (event, ms) => new Promise((resolve, reject) => {
		const timer = setTimeout(() => reject(new Error(`no ${event} within ${ms} ms`)), ms);
		listeners.set(event, () => { clearTimeout(timer); resolve(); });
	});
	const close = async () => {
		ws.close();
		const exited = new Promise((resolve) => proc.once('exit', resolve));
		try { process.kill(-proc.pid, 'SIGTERM'); } catch { proc.kill(); }
		await Promise.race([exited, sleep(5000)]);
		// Helpers can still be flushing the profile for a moment after the launcher exits.
		// Cleaning a temporary folder must never fail a test, so retry, then let it go.
		for (let attempt = 0; attempt < 20; attempt++) {
			try {
				rmSync(profile, { recursive: true, force: true });
				return;
			} catch {
				await sleep(250);
			}
		}
	};
	await send('Page.enable');
	return { send, next, close };
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
// Fonts loaded and two frames painted: the layout the visitor actually sees.
const SETTLED = 'document.fonts.ready.then(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))))';

test('every page fits a phone without zooming out', { skip, timeout: 180000 }, async (t) => {
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
		const loaded = chrome.next('Page.loadEventFired', 30000);
		await chrome.send('Page.navigate', { url });
		await loaded;
		await chrome.send('Runtime.evaluate', { expression: SETTLED, awaitPromise: true });
		const { result } = await chrome.send('Runtime.evaluate', { expression: MEASURE, returnByValue: true });
		const { page, phone, culprits } = result.value;
		assert.ok(page <= phone, `${name} is ${page}px wide on a ${phone}px phone; sticking out: ${culprits.join(', ') || 'a pseudo-element'}`);
	}
});

test('the share card draws a 1200 x 630 image with content on it', { skip, timeout: 120000 }, async (t) => {
	const out = mkdtempSync(path.join(tmpdir(), 'ghis-card-'));
	t.after(() => rmSync(out, { recursive: true, force: true }));
	execFileSync(PYTHON, [path.join(ROOT, 'ghis.py'), 'demo', '--out', out], { stdio: 'ignore' });
	const chrome = await openChrome();
	t.after(chrome.close);
	const loaded = chrome.next('Page.loadEventFired', 30000);
	await chrome.send('Page.navigate', { url: pathToFileURL(path.join(out, 'index.html')).href });
	await loaded;
	await chrome.send('Runtime.evaluate', { expression: SETTLED, awaitPromise: true });
	const { result } = await chrome.send('Runtime.evaluate', {
		returnByValue: true,
		expression: `(() => {
			const card = window.ghisShareCard();
			const pixels = card.getContext('2d').getImageData(0, 0, card.width, card.height).data;
			let bright = 0;
			for (let i = 0; i < pixels.length; i += 4 * 31) if (pixels[i] + pixels[i + 1] + pixels[i + 2] > 450) bright++;
			const preview = document.querySelector('#share-preview');
			return { width: card.width, height: card.height, bright, preview: preview && preview.src.startsWith('data:image/png') };
		})()`,
	});
	const card = result.value;
	assert.deepEqual([card.width, card.height], [1200, 630]);
	assert.ok(card.bright > 200, `the card looks empty (${card.bright} bright samples)`); // text and tiles drawn
	assert.ok(card.preview, 'the page shows a preview of the card');
});
