// ghis story logic: formatting, the per-range statistics and chart scales. No DOM access,
// so it runs both as a classic browser script (window.GhisLib, which keeps the offline page
// working from file://, where ES modules are blocked) and under `node --test`.
(function (root, factory) {
	const lib = factory();
	if (typeof module === 'object' && module.exports) module.exports = lib;
	else root.GhisLib = lib;
})(typeof self !== 'undefined' ? self : this, () => {
	'use strict';

	// ------------------------------------------------------------ formatting

	const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({
		'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
	}[c]));
	// Links that come from data (an uploaded history.json included) may only be http(s).
	const safeUrl = (url) => (/^https?:\/\//i.test(url || '') ? url : null);
	const plain = new Intl.NumberFormat('en');
	const compact = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 });
	const num = (n) => (Math.abs(n) >= 10000 ? compact.format(n) : plain.format(Math.round(n)));
	const pct = (part, whole) => (whole ? Math.round((part / whole) * 100) : 0);
	const plural = (n, one, many = `${one}s`) => `${num(n)} ${n === 1 ? one : many}`;
	const utc = (day) => new Date(`${day}T00:00:00Z`);
	const dayFmt = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
	const monthFmt = new Intl.DateTimeFormat('en-GB', { month: 'short', year: 'numeric', timeZone: 'UTC' });
	const monthShort = new Intl.DateTimeFormat('en-GB', { month: 'short', timeZone: 'UTC' });
	const fmtDay = (day) => dayFmt.format(utc(day));
	const DAY_MS = 86400000;
	const addDays = (day, n) => new Date(utc(day).getTime() + n * DAY_MS).toISOString().slice(0, 10);
	const daysBetween = (a, b) => Math.round((utc(b) - utc(a)) / DAY_MS);
	const weekdayOf = (day) => (utc(day).getUTCDay() + 6) % 7; // Monday = 0
	const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
	const WEEKDAYS_LONG = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
	const hourLabel = (h) => `${String(h).padStart(2, '0')}:00`;
	const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

	function duration(ms) {
		const hours = ms / 3600000;
		if (hours < 1) return `${Math.max(1, Math.round(ms / 60000))} min`;
		if (hours < 48) return `${hours < 10 ? hours.toFixed(1) : Math.round(hours)} h`;
		const days = hours / 24;
		return `${days < 10 ? days.toFixed(1) : Math.round(days)} days`;
	}

	function median(values) {
		if (!values.length) return null;
		const sorted = [...values].sort((a, b) => a - b);
		const mid = sorted.length >> 1;
		return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
	}

	function initials(name) {
		const parts = String(name || '?').trim().split(/[\s._-]+/).filter(Boolean);
		return ((parts[0]?.[0] || '?') + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase();
	}

	// ------------------------------------------------------------ vocabulary

	function vocabulary(provider, host) {
		if (provider === 'gitlab') return { req: 'merge request', reqs: 'merge requests', REQ: 'MR', REQS: 'MRs', host: 'GitLab' };
		const name = provider === 'github' ? 'GitHub' : provider === 'gitea' ? (host || 'Forgejo') : 'git';
		return { req: 'pull request', reqs: 'pull requests', REQ: 'PR', REQS: 'PRs', host: name };
	}

	// Activity actions that count as a contribution, with their [singular, plural] labels.
	function actionLabels(V) {
		return {
			request_opened: [`${V.req} opened`, `${V.reqs} opened`],
			merged: [`${V.req} merged`, `${V.reqs} merged`],
			approved: ['approval', 'approvals'],
			reviewed: ['review', 'reviews'],
			commented: ['comment', 'comments'],
			issue_opened: ['issue opened', 'issues opened'],
		};
	}
	const CONTRIBUTION_ACTIONS = new Set(Object.keys(actionLabels(vocabulary())));

	function contributionCount(actions) {
		let n = 0;
		for (const [action, count] of Object.entries(actions || {})) if (CONTRIBUTION_ACTIONS.has(action)) n += count;
		return n;
	}

	const STOPWORDS = new Set(('the and for with from into onto that this was were are not but use when then than add adds added fix fixes fixed update updates updated remove removes removed merge merged branch '
		+ 'master main develop dev feature wip test tests make makes made more less new old its it\'s also via per all any can will now only out off own just some what which who why how get set '
		+ 'has have had our your their them they you see one two three back should would could after before over under same other each does did done doing pull request requests revert reverts '
		+ 'version bump chore refactor feat docs style').split(/\s+/));
	const TICKET_RE = /\b[A-Z][A-Z0-9]{1,9}-\d+\b/g;

	// ------------------------------------------------------------ model

	function createModel(data) {
		const commits = (data.commits || [])
			.map((c) => ({ ...c, day: c.t.slice(0, 10), hour: +c.t.slice(11, 13), ms: Date.parse(c.t) }))
			.sort((a, b) => a.ms - b.ms);
		const requests = (data.requests || data.mergeRequests || []).filter((r) => r.createdAt);
		const reviews = (data.reviews || []).filter((r) => r.createdAt);
		const activity = data.activity || {};
		const generatedDay = (data.generatedAt || new Date().toISOString()).slice(0, 10);

		// A language keeps its colour whatever year is shown: slots go by all-time rank.
		const totals = {};
		for (const c of commits) for (const [lang, n] of Object.entries(c.l || {})) totals[lang] = (totals[lang] || 0) + n;
		const ranked = Object.entries(totals).filter(([l]) => l !== 'Other').sort((a, b) => b[1] - a[1]);
		const langColors = new Map(ranked.slice(0, 6).map(([lang], i) => [lang, `var(--s${i + 1})`]));

		const yearSet = new Set();
		for (const c of commits) yearSet.add(c.day.slice(0, 4));
		for (const r of requests) yearSet.add(r.createdAt.slice(0, 4));
		for (const day of Object.keys(activity)) if (contributionCount(activity[day])) yearSet.add(day.slice(0, 4));
		const years = [...yearSet].sort().reverse();

		function compute(range) {
			const inRange = range === 'all' ? (d) => !!d : (d) => !!d && d.startsWith(range);
			const cs = commits.filter((c) => inRange(c.day));
			const reqs = requests.filter((r) => inRange(r.createdAt));
			const revs = reviews.filter((r) => inRange(r.createdAt));

			const events = {};
			const eventDays = {};
			for (const [day, actions] of Object.entries(activity)) {
				if (!inRange(day)) continue;
				for (const [action, n] of Object.entries(actions)) events[action] = (events[action] || 0) + n;
				if (contributionCount(actions)) eventDays[day] = actions;
			}

			let adds = 0, dels = 0;
			const byDay = new Map();
			const hours = new Array(24).fill(0);
			const weekdays = new Array(7).fill(0);
			const punch = Array.from({ length: 7 }, () => new Array(24).fill(0));
			const langs = {};
			const repos = new Map();
			let biggest = null;
			for (const c of cs) {
				adds += c.a; dels += c.d;
				const day = byDay.get(c.day) || { n: 0, a: 0, d: 0 };
				day.n++; day.a += c.a; day.d += c.d;
				byDay.set(c.day, day);
				const wd = weekdayOf(c.day);
				hours[c.hour]++; weekdays[wd]++; punch[wd][c.hour]++;
				for (const [lang, n] of Object.entries(c.l || {})) langs[lang] = (langs[lang] || 0) + n;
				const repo = repos.get(c.r) || { r: c.r, n: 0, a: 0, d: 0, first: c.day, last: c.day };
				repo.n++; repo.a += c.a; repo.d += c.d; repo.last = c.day;
				repos.set(c.r, repo);
				if (!c.m && (!biggest || c.a + c.d > biggest.a + biggest.d)) biggest = c;
			}

			const contrib = new Map();
			for (const [day, v] of byDay) contrib.set(day, v.n);
			for (const [day, actions] of Object.entries(eventDays)) contrib.set(day, (contrib.get(day) || 0) + contributionCount(actions));
			const activeDays = [...contrib.keys()].sort();

			let streak = { len: 0 };
			let run = null;
			for (const day of activeDays) {
				run = run && addDays(run.end, 1) === day ? { ...run, end: day, len: run.len + 1 } : { start: day, end: day, len: 1 };
				if (run.len > streak.len) streak = run;
			}

			let busiest = null;
			for (const [day, v] of byDay) if (!busiest || v.n > busiest.n) busiest = { day, ...v };

			const tickets = new Set();
			for (const text of [...cs.map((c) => c.s), ...reqs.map((r) => r.title)]) {
				for (const match of String(text).match(TICKET_RE) || []) tickets.add(match);
			}
			const words = {};
			for (const c of cs) {
				for (const raw of String(c.s).replace(TICKET_RE, ' ').toLowerCase().match(/[a-z][a-z'-]{2,}/g) || []) {
					const word = raw.replace(/['-]+$/, '');
					if (word.length > 2 && !STOPWORDS.has(word)) words[word] = (words[word] || 0) + 1;
				}
			}

			const merged = reqs.filter((r) => r.state === 'merged' && r.mergedAt);
			const ttm = merged.map((r) => ({ r, ms: Date.parse(r.mergedAt) - Date.parse(r.createdAt) })).filter((x) => x.ms >= 0);

			const share = (pred) => pct(cs.filter(pred).length, cs.length);
			return {
				range, cs, reqs, revs, events, adds, dels, byDay, contrib, activeDays, streak, busiest, biggest,
				hours, weekdays, punch, langs, tickets, words, merged, ttm,
				repos: [...repos.values()].sort((a, b) => b.n - a.n),
				nightPct: share((c) => c.hour >= 22 || c.hour < 5),
				morningPct: share((c) => c.hour >= 5 && c.hour < 9),
				weekendPct: share((c) => weekdayOf(c.day) >= 5),
				mergesPct: share((c) => c.m),
			};
		}

		// The months a range covers: first to last activity for all time, the calendar year
		// (up to the generation month for the current one) otherwise.
		function monthList(s) {
			let from, to;
			if (s.range === 'all') {
				const days = [...s.cs.map((c) => c.day), ...s.reqs.map((r) => r.createdAt.slice(0, 10))].sort();
				if (!days.length) return [];
				from = days[0].slice(0, 7); to = days[days.length - 1].slice(0, 7);
			} else {
				from = `${s.range}-01`;
				to = s.range === generatedDay.slice(0, 4) ? generatedDay.slice(0, 7) : `${s.range}-12`;
			}
			const months = [];
			for (let m = from; m <= to;) {
				months.push(m);
				const [y, mm] = m.split('-').map(Number);
				m = mm === 12 ? `${y + 1}-01` : `${y}-${String(mm + 1).padStart(2, '0')}`;
			}
			return months;
		}

		return { commits, requests, reviews, activity, generatedDay, langColors, years, compute, monthList };
	}

	function persona(s) {
		if (s.cs.length < 10) return null;
		const peak = s.hours.indexOf(Math.max(...s.hours));
		if (s.nightPct >= 20) return ['🦉', 'Night owl', `${s.nightPct}% of your commits land between 22:00 and 05:00.`];
		if (s.morningPct >= 25) return ['🌅', 'Early bird', `${s.morningPct}% of your commits land before 09:00.`];
		if (s.weekendPct >= 20) return ['🏕️', 'Weekend warrior', `${s.weekendPct}% of your commits happen on Saturday or Sunday.`];
		if (peak >= 9 && peak <= 17) return ['☕', 'Office-hours coder', `You keep it civilised — your peak hour is ${hourLabel(peak)}.`];
		return ['🌙', 'Evening coder', `Your peak hour is ${hourLabel(peak)}.`];
	}

	// ------------------------------------------------------------ chart scales

	// Axis ticks for counts: whole-number steps of 1, 2 or 5 × 10ⁿ (2.5 only from 25 up).
	function niceTicks(max, count = 4) {
		const raw = max / count;
		const power = 10 ** Math.floor(Math.log10(raw || 1));
		const step = Math.max(1, [1, 2, 2.5, 5, 10].map((m) => m * power)
			.filter(Number.isInteger).find((s) => s >= raw) || power);
		const ticks = [];
		for (let v = 0; v <= max + step * 0.001 || ticks.length < 2; v += step) ticks.push(v);
		if (ticks[ticks.length - 1] < max) ticks.push(ticks[ticks.length - 1] + step);
		return ticks;
	}

	// A column rounded at its data end and square at the baseline.
	function columnPath(x, y, w, h, r) {
		r = Math.min(r, w / 2, h);
		return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
	}

	return {
		esc, safeUrl, num, pct, plural, utc, fmtDay, monthFmt, monthShort, addDays, daysBetween,
		weekdayOf, WEEKDAYS, WEEKDAYS_LONG, hourLabel, clamp, duration, median, initials,
		vocabulary, actionLabels, contributionCount, STOPWORDS, createModel, persona, niceTicks, columnPath,
	};
});
