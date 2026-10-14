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

	// ------------------------------------------------------------ time zones

	// A commit's UTC offset in minutes, from its ISO time ("+03:00", "-0500" or "Z").
	function offsetOf(time) {
		const m = /([+-])(\d\d):?(\d\d)$/.exec(time || '');
		return m ? (m[1] === '-' ? -1 : 1) * (Number(m[2]) * 60 + Number(m[3])) : 0;
	}

	// The author's home offset for a month: the one most of that month's commits were made in
	// (so daylight saving follows the calendar), else the most common overall, else UTC.
	function homeOffsets(commits) {
		const byMonth = {};
		const overall = {};
		for (const c of commits) {
			const off = offsetOf(c.t);
			const month = c.t.slice(0, 7);
			(byMonth[month] ||= {})[off] = (byMonth[month][off] || 0) + 1;
			overall[off] = (overall[off] || 0) + 1;
		}
		const top = (counts) => Number(Object.entries(counts).sort((a, b) => b[1] - a[1])[0]?.[0] ?? 0);
		const months = Object.fromEntries(Object.entries(byMonth).map(([m, counts]) => [m, top(counts)]));
		const fallback = top(overall);
		return (month) => months[month] ?? fallback;
	}

	// Activity arrives per UTC hour ("2024-03-01T23"); move each hour into the author's day so
	// a late review sits beside that evening's commits. Day keys (older files) pass through.
	function localizeActivity(activity, offsetFor) {
		const out = {};
		for (const [key, actions] of Object.entries(activity || {})) {
			let day = key.slice(0, 10);
			if (key.length >= 13) {
				const ms = Date.parse(`${key.slice(0, 13)}:00:00Z`);
				day = new Date(ms + offsetFor(key.slice(0, 7)) * 60000).toISOString().slice(0, 10);
			}
			const bucket = (out[day] ||= {});
			for (const [action, n] of Object.entries(actions)) bucket[action] = (bucket[action] || 0) + n;
		}
		return out;
	}

	// ------------------------------------------------------------ model

	const MILESTONES = [1, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000];
	const REQUEST_MILESTONES = [1, 10, 50, 100, 250, 500, 1000];
	const countBy = (values) => {
		const counts = {};
		for (const v of values) if (v) counts[v] = (counts[v] || 0) + 1;
		return Object.entries(counts).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
	};

	function createModel(data) {
		const projects = data.projects || [];
		const commits = (data.commits || [])
			.map((c) => ({ ...c, day: c.t.slice(0, 10), hour: +c.t.slice(11, 13), ms: Date.parse(c.t) }))
			.sort((a, b) => a.ms - b.ms);
		const requests = (data.requests || data.mergeRequests || []).filter((r) => r.createdAt);
		const reviews = (data.reviews || []).filter((r) => r.createdAt);
		const activity = localizeActivity(data.activity, homeOffsets(commits));
		const generatedDay = (data.generatedAt || new Date().toISOString()).slice(0, 10);
		const createdDay = (data.user?.createdAt || '').slice(0, 10) || null;

		// Bulk changes (an import, a vendored library, a formatter run) are commits far larger
		// than your usual. They still count as commits, but their lines would outweigh months
		// of real work, so line totals, languages and "biggest commit" leave them out.
		const sizes = commits.filter((c) => !c.m).map((c) => c.a + c.d).filter((n) => n > 0);
		const bulkThreshold = Math.max(2000, 40 * (median(sizes) || 0));
		for (const c of commits) c.bulk = !c.m && c.a + c.d > bulkThreshold;

		// A language keeps its color whatever year is shown: slots go by all-time rank.
		const totals = {};
		for (const c of commits) {
			if (c.bulk) continue;
			for (const [lang, n] of Object.entries(c.l || {})) totals[lang] = (totals[lang] || 0) + n;
		}
		const ranked = Object.entries(totals).filter(([l]) => l !== 'Other').sort((a, b) => b[1] - a[1]);
		const langColors = new Map(ranked.slice(0, 6).map(([lang], i) => [lang, `var(--s${i + 1})`]));

		const yearSet = new Set();
		for (const c of commits) yearSet.add(c.day.slice(0, 4));
		for (const r of requests) yearSet.add(r.createdAt.slice(0, 4));
		for (const day of Object.keys(activity)) if (contributionCount(activity[day])) yearSet.add(day.slice(0, 4));
		const years = [...yearSet].sort().reverse();

		// Repositories with commits, most first: what the repository filter offers.
		const repoCounts = {};
		for (const c of commits) repoCounts[c.r] = (repoCounts[c.r] || 0) + 1;
		const repoOptions = Object.entries(repoCounts)
			.map(([r, n]) => ({ r: Number(r), n, path: projects[r]?.path || `#${r}` }))
			.sort((a, b) => b.n - a.n);

		const pathOf = (r) => (projects[r]?.path || '').toLowerCase();

		// `until` ("MM-DD") cuts a year short, for comparing like with like against a year
		// still in progress.
		function compute(range, { repo = null, until = null } = {}) {
			const inRange = (d) => !!d && (range === 'all' || d.startsWith(range)) && (!until || d.slice(5, 10) <= until);
			const repoPath = repo == null ? null : pathOf(repo);
			const inRepo = (r) => repo == null || r.project.toLowerCase() === repoPath;
			const cs = commits.filter((c) => inRange(c.day) && (repo == null || c.r === repo));
			const reqs = requests.filter((r) => inRange(r.createdAt) && inRepo(r));
			const revs = reviews.filter((r) => inRange(r.createdAt) && inRepo(r));

			// Activity events carry no repository, so a repository view is commits only.
			const events = {};
			const eventDays = {};
			if (repo == null) {
				for (const [day, actions] of Object.entries(activity)) {
					if (!inRange(day)) continue;
					for (const [action, n] of Object.entries(actions)) events[action] = (events[action] || 0) + n;
					if (contributionCount(actions)) eventDays[day] = actions;
				}
			}

			let adds = 0, dels = 0, coAuthored = 0;
			const bulk = { n: 0, a: 0, d: 0, commits: [] };
			const byDay = new Map();
			const hours = new Array(24).fill(0);
			const weekdays = new Array(7).fill(0);
			const punch = Array.from({ length: 7 }, () => new Array(24).fill(0));
			const langs = {};
			const repos = new Map();
			let biggest = null;
			for (const c of cs) {
				const day = byDay.get(c.day) || { n: 0, a: 0, d: 0 };
				day.n++;
				const repoStat = repos.get(c.r) || { r: c.r, n: 0, a: 0, d: 0, first: c.day, last: c.day };
				repoStat.n++; repoStat.last = c.day;
				if (c.co) coAuthored++;
				if (c.bulk) {
					bulk.n++; bulk.a += c.a; bulk.d += c.d; bulk.commits.push(c);
				} else {
					adds += c.a; dels += c.d;
					day.a += c.a; day.d += c.d;
					repoStat.a += c.a; repoStat.d += c.d;
					for (const [lang, n] of Object.entries(c.l || {})) langs[lang] = (langs[lang] || 0) + n;
					if (!c.m && (!biggest || c.a + c.d > biggest.a + biggest.d)) biggest = c;
				}
				byDay.set(c.day, day);
				repos.set(c.r, repoStat);
				const wd = weekdayOf(c.day);
				hours[c.hour]++; weekdays[wd]++; punch[wd][c.hour]++;
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
			// The streak still running when the story was made (today may not have started yet).
			const active = new Set(activeDays);
			let current = { len: 0 };
			if (!until && (range === 'all' || generatedDay.startsWith(range))) {
				let day = active.has(generatedDay) ? generatedDay : addDays(generatedDay, -1);
				const end = day;
				while (active.has(day)) { current = { start: day, end, len: current.len + 1 }; day = addDays(day, -1); }
			}

			let busiest = null;
			for (const [day, v] of byDay) if (!busiest || v.n > busiest.n) busiest = { day, ...v };

			const tickets = new Set();
			for (const text of [...cs.map((c) => c.s), ...reqs.map((r) => r.title)]) {
				for (const match of String(text || '').match(TICKET_RE) || []) tickets.add(match);
			}
			const words = {};
			for (const c of cs) {
				for (const raw of String(c.s || '').replace(TICKET_RE, ' ').toLowerCase().match(/[a-z][a-z'-]{2,}/g) || []) {
					const word = raw.replace(/['-]+$/, '');
					if (word.length > 2 && !STOPWORDS.has(word)) words[word] = (words[word] || 0) + 1;
				}
			}

			const merged = reqs.filter((r) => r.state === 'merged' && r.mergedAt);
			const ttm = merged.map((r) => ({ r, ms: Date.parse(r.mergedAt) - Date.parse(r.createdAt) })).filter((x) => x.ms >= 0);

			const share = (pred) => pct(cs.filter(pred).length, cs.length);
			return {
				range, repo, until, cs, reqs, revs, events, adds, dels, byDay, contrib, activeDays, streak,
				currentStreak: current, busiest, biggest, bulk, coAuthored,
				hours, weekdays, punch, langs, tickets, words, merged, ttm,
				repos: [...repos.values()].sort((a, b) => b.n - a.n),
				people: {
					reviewed: countBy(revs.map((r) => r.author)),
					reviewers: countBy(reqs.flatMap((r) => r.reviewers || [])),
				},
				nightPct: share((c) => c.hour >= 22 || c.hour < 5),
				morningPct: share((c) => c.hour >= 5 && c.hour < 9),
				weekendPct: share((c) => weekdayOf(c.day) >= 5),
				mergesPct: share((c) => c.m),
			};
		}

		// The same stretch of the year before: the whole year, or up to today's date while the
		// current year is still running. Null when there is nothing to compare with.
		function previous(s) {
			if (s.range === 'all') return null;
			const prior = String(Number(s.range) - 1);
			if (!years.includes(prior)) return null;
			const until = s.range === generatedDay.slice(0, 4) ? generatedDay.slice(5, 10) : null;
			const p = compute(prior, { repo: s.repo, until });
			return p.cs.length || p.reqs.length ? p : null;
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

		// Round-number moments of all time (your 1,000th commit stays your 1,000th whatever
		// year is shown), kept when they fall in the range.
		function milestones(s) {
			const inRange = (d) => !!d && (s.range === 'all' || d.startsWith(s.range)) && d <= generatedDay;
			const own = s.repo == null ? commits : commits.filter((c) => c.r === s.repo);
			const out = [];
			for (const n of MILESTONES) {
				const c = own[n - 1];
				if (c) out.push({ day: c.day, kind: n === 1 ? 'first' : 'count', n, commit: c });
			}
			if (s.repo == null) {
				const firstIn = new Map();
				for (const c of commits) if (!firstIn.has(c.r)) firstIn.set(c.r, c);
				for (const c of firstIn.values()) if (c !== commits[0]) out.push({ day: c.day, kind: 'repo', commit: c });
			}
			const repoPath = s.repo == null ? null : pathOf(s.repo);
			const mine = requests.filter((r) => repoPath == null || r.project.toLowerCase() === repoPath)
				.slice().sort((a, b) => a.createdAt.localeCompare(b.createdAt));
			if (mine[0]) out.push({ day: mine[0].createdAt.slice(0, 10), kind: 'request', request: mine[0] });
			const mergedSorted = mine.filter((r) => r.mergedAt).sort((a, b) => a.mergedAt.localeCompare(b.mergedAt));
			for (const n of REQUEST_MILESTONES.slice(1)) {
				const r = mergedSorted[n - 1];
				if (r) out.push({ day: r.mergedAt.slice(0, 10), kind: 'merged', n, request: r });
			}
			if (createdDay && s.repo == null) {
				for (let y = 1; ; y++) {
					const day = `${Number(createdDay.slice(0, 4)) + y}${createdDay.slice(4)}`;
					if (day > generatedDay) break;
					out.push({ day, kind: 'anniversary', n: y });
				}
			}
			return out.filter((m) => inRange(m.day)).sort((a, b) => a.day.localeCompare(b.day) || (a.n || 0) - (b.n || 0));
		}

		return {
			commits, requests, reviews, activity, generatedDay, langColors, years, repoOptions,
			bulkThreshold, compute, previous, monthList, milestones,
		};
	}

	// Signed whole-percent change, or null when there is no base to compare with.
	function delta(current, before) {
		return before ? Math.round(((current - before) / before) * 100) : null;
	}

	function persona(s) {
		if (s.cs.length < 10) return null;
		const peak = s.hours.indexOf(Math.max(...s.hours));
		if (s.nightPct >= 20) return ['🦉', 'Night owl', `${s.nightPct}% of your commits land between 22:00 and 05:00.`];
		if (s.morningPct >= 25) return ['🌅', 'Early bird', `${s.morningPct}% of your commits land before 09:00.`];
		if (s.weekendPct >= 20) return ['🏕️', 'Weekend warrior', `${s.weekendPct}% of your commits happen on Saturday or Sunday.`];
		if (peak >= 9 && peak <= 17) return ['☕', 'Office-hours coder', `You keep it civilized — your peak hour is ${hourLabel(peak)}.`];
		return ['🌙', 'Evening coder', `Your peak hour is ${hourLabel(peak)}.`];
	}

	// ------------------------------------------------------------ sharing

	const SITE_URL = 'radufarcas10.github.io/ghis';

	const letters = (i) => {
		let s = '';
		for (let n = i + 1; n > 0; n = Math.floor((n - 1) / 26)) s = String.fromCharCode(65 + ((n - 1) % 26)) + s;
		return s;
	};

	// A copy of a history.json that is safe to post: commit messages and titles removed,
	// and/or repositories, teammates, emails, links and the instance renamed or dropped.
	// The numbers (commits, lines, days, times) are untouched.
	function anonymize(data, { messages = true, names = true } = {}) {
		const copy = JSON.parse(JSON.stringify(data));
		const items = [...(copy.requests || []), ...(copy.reviews || [])];
		if (messages) {
			for (const c of copy.commits || []) c.s = '';
			for (const r of items) r.title = '';
		}
		if (names) {
			const counts = {};
			for (const c of copy.commits || []) counts[c.r] = (counts[c.r] || 0) + 1;
			const order = (copy.projects || []).map((_, i) => i).sort((a, b) => (counts[b] || 0) - (counts[a] || 0) || a - b);
			const renamed = new Map();
			order.forEach((i, rank) => renamed.set(i, `Project ${letters(rank)}`));
			const byPath = new Map((copy.projects || []).map((p, i) => [p.path.toLowerCase(), renamed.get(i)]));
			copy.projects = (copy.projects || []).map((p, i) => ({ path: renamed.get(i), name: renamed.get(i), webUrl: null, source: p.source }));
			const own = copy.user?.username;
			const people = new Map();
			const person = (name) => {
				if (!name || name === own) return name;
				if (!people.has(name)) people.set(name, `teammate-${people.size + 1}`);
				return people.get(name);
			};
			for (const r of items) {
				r.project = byPath.get(String(r.project).toLowerCase()) || 'Project ?';
				r.url = null;
				r.author = person(r.author);
				if (r.reviewers) r.reviewers = r.reviewers.map(person);
			}
			for (const c of copy.commits || []) c.e = '';
			if (copy.user) copy.user.webUrl = null;
			copy.source = { ...copy.source, host: null, emails: [] };
			copy.warnings = [];
		}
		copy.source = { ...copy.source, shareSafe: { messages, names } };
		return copy;
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
		offsetOf, localizeActivity, delta, anonymize, SITE_URL,
	};
});
