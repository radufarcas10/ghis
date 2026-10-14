// ghis story page. Reads window.GHIS_DATA (offline, from data.js) or fetches a hosted
// job's result, then renders every section for the selected year. Charts are plain SVG
// sized to their container and redrawn on resize; no libraries.
(() => {
	'use strict';

	const $ = (selector, root = document) => root.querySelector(selector);
	const app = $('#app');
	const tip = $('#tip');
	const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;

	const {
		esc, safeUrl, num, pct, plural, utc, fmtDay, monthFmt, monthShort, addDays, daysBetween,
		weekdayOf, WEEKDAYS, WEEKDAYS_LONG, hourLabel, clamp, duration, median, initials,
		vocabulary, actionLabels, createModel, persona, niceTicks, columnPath,
	} = window.GhisLib;

	// ------------------------------------------------------------ data loading

	const hosted = !window.GHIS_DATA && location.protocol.startsWith('http');
	const query = new URLSearchParams(location.search);

	async function loadData() {
		if (window.GHIS_DATA) return window.GHIS_DATA;
		let url = null;
		if (query.get('job')) url = `api/jobs/${encodeURIComponent(query.get('job'))}/result`;
		else if (query.has('demo')) url = `api/demo?provider=${encodeURIComponent(query.get('demo') || 'github')}`;
		if (!url) return null;
		const response = await fetch(url);
		if (!response.ok) {
			const body = await response.json().catch(() => ({}));
			throw new Error(body.error || `The server answered ${response.status}.`);
		}
		return response.json();
	}

	function showMessage(title, html) {
		app.innerHTML = `<section class="hero"><h1>${esc(title)}</h1><p class="sub">${html}</p>
			${hosted ? '<p style="margin-top:28px"><a class="btn primary" href="./">Make a story</a></p>' : ''}</section>`;
	}

	loadData().then((data) => {
		if (!data) {
			showMessage('No story yet', hosted
				? 'Start one from the home page.'
				: 'Run <code>python3 ghis.py collect …</code> or <code>python3 ghis.py demo</code>, then reopen this page.');
			return;
		}
		init(data);
	}).catch((err) => showMessage('Story unavailable', esc(err.message)));

	// ------------------------------------------------------------ model

	let D, V, has, model, commits, projects, activity, langColors, years, generatedDay, ACTIONS;

	function init(data) {
		D = data;
		V = vocabulary(D.source?.provider, D.source?.host);
		ACTIONS = actionLabels(V);
		has = new Set(D.source?.has || []);
		projects = D.projects || [];
		model = createModel(D);
		({ commits, activity, langColors, years, generatedDay } = model);

		const name = D.user?.name || D.user?.username || 'Your';
		document.title = `${name} · ghis`;
		if (hosted) $('#home').href = './';

		const initial = location.hash.slice(1);
		setRange(years.includes(initial) ? initial : 'all', false);
		window.addEventListener('hashchange', () => {
			const want = location.hash.slice(1);
			setRange(years.includes(want) ? want : 'all', false);
		});
	}

	// ------------------------------------------------------------ range & render

	let state = null;
	const revealed = new Set();
	const lastCounts = new Map();
	let chartDrawers = [];

	function setRange(range, pushHash = true) {
		state = model.compute(range);
		if (pushHash) history.replaceState(null, '', range === 'all' ? location.pathname + location.search : `#${range}`);
		renderYears();
		render();
	}

	function renderYears() {
		const nav = $('#years');
		const options = ['all', ...years];
		nav.innerHTML = years.length > 1
			? options.map((y) => `<button class="pill" type="button" data-range="${y}" aria-pressed="${state.range === y}">${y === 'all' ? 'All time' : y}</button>`).join('')
			: '';
		nav.onclick = (e) => {
			const button = e.target.closest('[data-range]');
			if (button && button.dataset.range !== state.range) setRange(button.dataset.range);
		};
	}

	function render() {
		chartDrawers = [];
		const s = state;
		app.innerHTML = [
			heroSection(s),
			calendarSection(s),
			monthsSection(s),
			rhythmSection(s),
			reposSection(s),
			languagesSection(s),
			requestsSection(s),
			highlightsSection(s),
			wordsSection(s),
			footer(),
		].join('');
		drawCharts(true);
		observe();
	}

	// ------------------------------------------------------------ sections

	function section(id, kicker, title, lede, body) {
		return `<section class="section reveal" id="${id}">
			<div class="section-head"><div><p class="kicker">${kicker}</p><h2>${title}</h2>${lede ? `<p class="lede">${lede}</p>` : ''}</div></div>
			${body}</section>`;
	}

	function counter(key, value, format = 'num') {
		return `<span class="count" data-key="${key}" data-count="${value}" data-format="${format}">${format === 'num' ? num(value) : value}</span>`;
	}

	function heroSection(s) {
		const user = D.user || {};
		const first = commits[0]?.day;
		const since = user.createdAt ? user.createdAt.slice(0, 10) : first;
		const sinceText = since ? `${D.source?.provider && user.createdAt ? `on ${esc(V.host)} since` : 'committing since'} ${fmtDay(since)} · ${plural(daysBetween(since, generatedDay), 'day')}` : '';
		const handle = user.username ? `@${esc(user.username)}` : '';
		const eyebrow = s.range === 'all' ? `Your ${esc(V.host)} story · all time` : `Your ${s.range} in code`;
		const net = s.adds - s.dels;
		const tiles = [
			['Lines added', counter('adds', s.adds), `${net >= 0 ? '+' : '−'}${num(Math.abs(net))} net`],
			['Lines removed', counter('dels', s.dels), s.adds ? `${pct(s.dels, s.adds)}% of what you added` : ''],
			['Active days', counter('days', s.activeDays.length), s.streak.len ? `best streak ${plural(s.streak.len, 'day')}` : ''],
			['Repositories', counter('repos', s.repos.length), `${num(projects.length)} touched in total`],
		];
		if (has.has('requests')) tiles.push([`${V.REQS} merged`, counter('merged', s.merged.length), `${num(s.reqs.length)} opened`]);
		if (has.has('reviews')) tiles.push(['Reviews', counter('reviews', s.revs.length), has.has('approvals') ? `${num(s.events.approved || 0)} approvals` : '']);
		return `<section class="hero">
			<div class="monogram" aria-hidden="true">${esc(initials(user.name || user.username))}</div>
			<p class="eyebrow">${eyebrow}</p>
			<h1>${esc(user.name || user.username || 'Your history')}</h1>
			<p class="sub">${[handle, sinceText].filter(Boolean).join(' · ')}</p>
			<div class="hero-figure"><span class="value">${counter('commits', s.cs.length)}</span><span class="label">${s.cs.length === 1 ? 'commit' : 'commits'}${s.range === 'all' ? '' : ` in ${s.range}`}</span></div>
			<div class="tiles">${tiles.map(([label, value, sub], i) => `<div class="tile" style="--i:${i}"><span class="t-label">${label}</span><span class="t-value">${value}</span>${sub ? `<span class="t-sub">${sub}</span>` : ''}</div>`).join('')}</div>
			<p class="scroll-cue">Scroll for the story <span aria-hidden="true">↓</span></p>
		</section>`;
	}

	function calendarSection(s) {
		const calendarYears = s.range === 'all' ? years : [s.range];
		const values = [...s.contrib.values()].filter((v) => v > 0).sort((a, b) => a - b);
		const q = (p) => values[Math.min(values.length - 1, Math.floor(p * values.length))] || 1;
		const thresholds = [q(0.25), q(0.5), q(0.75)];
		const extra = Object.keys(ACTIONS).some((a) => s.events[a]);
		calendarYears.forEach((year) => chartDrawers.push({ id: `cal-${year}`, draw: (w) => heatmapSVG(year, s, thresholds, w) }));
		const body = s.contrib.size
			? `${calendarYears.map((year) => `<div class="cal-year">${calendarYears.length > 1 ? `<h3>${year}</h3>` : ''}<div class="chart chart-scroll" data-chart="cal-${year}"></div></div>`).join('')}
				<div class="scale-legend" aria-hidden="true">Less <i class="l0"></i><i class="l1"></i><i class="l2"></i><i class="l3"></i><i class="l4"></i> More</div>
				<p class="note">${s.streak.len > 1 ? `Longest streak: <b>${plural(s.streak.len, 'day')}</b>, ${fmtDay(s.streak.start)} – ${fmtDay(s.streak.end)}. ` : ''}${s.busiest ? `Busiest day: <b>${fmtDay(s.busiest.day)}</b> with ${plural(s.busiest.n, 'commit')}.` : ''}</p>`
			: '<p class="empty-note">No activity in this range.</p>';
		return section('calendar', 'Every day', 'The days you showed up',
			extra ? `Each square is a day: commits plus ${V.reqs}, reviews and comments.` : 'Each square is a day, shaded by how many commits you made.', body);
	}

	function monthsSection(s) {
		const months = model.monthList(s);
		const stats = new Map(months.map((m) => [m, { n: 0, a: 0, d: 0 }]));
		for (const c of s.cs) {
			const m = stats.get(c.day.slice(0, 7));
			if (m) { m.n++; m.a += c.a; m.d += c.d; }
		}
		const items = months.map((m) => {
			const v = stats.get(m);
			const label = monthFmt.format(utc(`${m}-01`));
			return { key: m, value: v.n, short: months.length > 14 ? monthShort.format(utc(`${m}-01`)).slice(0, 3) + (m.endsWith('-01') ? ` ’${m.slice(2, 4)}` : '') : monthShort.format(utc(`${m}-01`)), tip: `${label}\n${plural(v.n, 'commit')}\n+${num(v.a)} / −${num(v.d)} lines` };
		});
		const best = items.reduce((a, b) => (b.value > (a?.value ?? -1) ? b : a), null);
		chartDrawers.push({ id: 'months', draw: (w) => columnChart(items, w, { height: 240, labelMax: true }) });
		const table = dataTable(['Month', 'Commits', 'Added', 'Removed'], months.map((m) => {
			const v = stats.get(m);
			return [monthFmt.format(utc(`${m}-01`)), num(v.n), num(v.a), num(v.d)];
		}), [1, 2, 3]);
		return section('months', 'Momentum', 'Month by month',
			best && best.value ? `Your biggest month was <b>${esc(monthFmt.format(utc(`${best.key}-01`)))}</b> with ${plural(best.value, 'commit')}.` : '',
			s.cs.length ? `<div class="chart" data-chart="months"></div>${table}` : '<p class="empty-note">No commits in this range.</p>');
	}

	function rhythmSection(s) {
		if (!s.cs.length) return '';
		const p = persona(s);
		const peakHour = s.hours.indexOf(Math.max(...s.hours));
		const peakDay = s.weekdays.indexOf(Math.max(...s.weekdays));
		chartDrawers.push({ id: 'punch', draw: (w) => punchSVG(s.punch, w) });
		const tiles = [
			['Peak hour', hourLabel(peakHour), `${plural(s.hours[peakHour], 'commit')}`],
			['Favourite day', WEEKDAYS_LONG[peakDay], `${pct(s.weekdays[peakDay], s.cs.length)}% of commits`],
			['After dark', `${s.nightPct}%`, '22:00 – 05:00'],
			['Weekends', `${s.weekendPct}%`, 'Saturday and Sunday'],
		];
		const table = dataTable(['Day', ...Array.from({ length: 24 }, (_, h) => String(h).padStart(2, '0'))],
			WEEKDAYS.map((d, i) => [d, ...s.punch[i].map(String)]), Array.from({ length: 24 }, (_, i) => i + 1));
		return section('rhythm', 'Rhythm', 'When you write code',
			p ? `<span class="persona"><span class="badge" aria-hidden="true">${p[0]}</span>${esc(p[1])}</span> <span style="display:block;margin-top:10px">${esc(p[2])}</span>` : 'Commit times are in the timezone you committed from.',
			`<div class="chart" data-chart="punch"></div>
			<div class="tiles" style="margin-top:24px">${tiles.map(([l, v, sub]) => `<div class="tile"><span class="t-label">${l}</span><span class="t-value">${v}</span><span class="t-sub">${sub}</span></div>`).join('')}</div>
			${table}`);
	}

	function reposSection(s) {
		if (!s.repos.length) return '';
		const top = s.repos.slice(0, 10);
		const max = top[0].n;
		const reqCount = {};
		for (const r of s.reqs) reqCount[r.project.toLowerCase()] = (reqCount[r.project.toLowerCase()] || 0) + 1;
		const rows = top.map((repo, i) => {
			const p = projects[repo.r] || { path: '?' };
			const parts = p.path.split('/');
			const name = `${parts.length > 1 ? `${esc(parts.slice(0, -1).join('/'))}/` : ''}<b>${esc(parts[parts.length - 1])}</b>`;
			const reqs = reqCount[p.path.toLowerCase()] || 0;
			const tipText = `${p.path}\n${plural(repo.n, 'commit')} · +${num(repo.a)} / −${num(repo.d)}\n${fmtDay(repo.first)} → ${fmtDay(repo.last)}${reqs ? `\n${plural(reqs, V.REQ)}` : ''}`;
			const label = safeUrl(p.webUrl) ? `<a href="${esc(p.webUrl)}" target="_blank" rel="noopener noreferrer" class="name">${name}</a>` : `<span class="name">${name}</span>`;
			return `<li class="hbar" data-tip="${esc(tipText)}">${label}<span class="track"><span class="fill" style="--v:${repo.n / max};--i:${i}"></span></span><span class="val">${num(repo.n)}</span></li>`;
		}).join('');
		const share = pct(top[0].n, s.cs.length);
		const table = dataTable(['Repository', 'Commits', 'Added', 'Removed', 'First', 'Last'],
			s.repos.map((repo) => [projects[repo.r]?.path || '?', num(repo.n), num(repo.a), num(repo.d), fmtDay(repo.first), fmtDay(repo.last)]), [1, 2, 3]);
		return section('repos', 'Where', 'Where you spent your time',
			`<b>${esc(projects[top[0].r]?.name || '')}</b> took ${share}% of your commits${s.repos.length > 1 ? `, across ${plural(s.repos.length, 'repository', 'repositories')} in all` : ''}.`,
			`<div class="chart"><ol class="hbars">${rows}</ol></div>${table}`);
	}

	function languagesSection(s) {
		const entries = Object.entries(s.langs).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]);
		if (!entries.length) return '';
		const total = entries.reduce((sum, [, n]) => sum + n, 0);
		const named = entries.filter(([lang]) => langColors.has(lang));
		const rest = entries.filter(([lang]) => !langColors.has(lang));
		const restTotal = rest.reduce((sum, [, n]) => sum + n, 0);
		const parts = [...named.map(([lang, n]) => ({ lang, n, c: langColors.get(lang) }))];
		if (restTotal) parts.push({ lang: 'Other', n: restTotal, c: 'var(--other)', detail: rest.slice(0, 6).map(([l]) => l).join(', ') });
		const bar = parts.map((p, i) => `<span style="--v:${p.n / total};--c:${p.c};--i:${i}" data-tip="${esc(`${p.lang}\n${pct(p.n, total)}% · ${num(p.n)} lines changed${p.detail ? `\n${p.detail}` : ''}`)}"></span>`).join('');
		const legend = parts.map((p) => `<li><i class="sw" style="--c:${p.c}"></i><span>${esc(p.lang)}</span><span class="pct">${pct(p.n, total) || '<1'}%</span><span class="sub">${num(p.n)} lines${p.detail ? ` · ${esc(p.detail)}` : ''}</span></li>`).join('');
		const table = dataTable(['Language', 'Lines changed', 'Share'], entries.map(([l, n]) => [l, num(n), `${(n / total * 100).toFixed(1)}%`]), [1, 2]);
		return section('languages', 'What', 'What you wrote in',
			`Measured in lines added and removed. Mostly <b>${esc(entries[0][0])}</b>${entries[1] ? `, then ${esc(entries[1][0])}` : ''}.`,
			`<div class="chart"><div class="stack" role="img" aria-label="Share of lines changed by language">${bar}</div></div><ul class="legend">${legend}</ul>${table}`);
	}

	function requestsSection(s) {
		if (!has.has('requests')) {
			return section('requests', 'Collaboration', `${V.REQS} and reviews`, '',
				`<p class="empty-note">Connect a GitHub, GitLab or Forgejo account to add ${V.reqs}, reviews and comments.</p>`);
		}
		const medianTtm = median(s.ttm.map((x) => x.ms));
		const tiles = [
			[`${V.REQS} opened`, counter('req-open', s.reqs.length), `${num(s.reqs.filter((r) => r.state === 'opened').length)} still open`],
			['Merged', counter('req-merged', s.merged.length), s.reqs.length ? `${pct(s.merged.length, s.reqs.length)}% merge rate` : ''],
			['Median time to merge', medianTtm == null ? '—' : duration(medianTtm), s.ttm.length ? `fastest ${duration(Math.min(...s.ttm.map((x) => x.ms)))}` : ''],
			[`${V.REQS} reviewed`, counter('req-reviews', s.revs.length), 'opened by others'],
		];
		if (has.has('approvals')) tiles.push(['Approvals given', counter('approvals', s.events.approved || 0), '']);
		if (has.has('comments')) tiles.push(['Comments', counter('comments', s.events.commented || 0), 'on issues, code and reviews']);

		const buckets = [['< 1 h', 3600e3], ['1–24 h', 86400e3], ['1–3 days', 3 * 86400e3], ['3–7 days', 7 * 86400e3], ['1–4 weeks', 28 * 86400e3], ['> 4 weeks', Infinity]];
		const counts = buckets.map(() => 0);
		for (const x of s.ttm) counts[buckets.findIndex(([, limit]) => x.ms < limit)]++;
		const items = buckets.map(([label], i) => ({ key: label, value: counts[i], short: label, tip: `${label}\n${plural(counts[i], V.REQ)} (${pct(counts[i], s.ttm.length)}%)` }));
		if (s.ttm.length) chartDrawers.push({ id: 'ttm', draw: (w) => columnChart(items, w, { height: 200, labelAll: true }) });

		const discussed = [...s.reqs].sort((a, b) => (b.notes || 0) - (a.notes || 0))[0];
		const longest = [...s.ttm].sort((a, b) => b.ms - a.ms)[0];
		const link = (r) => (safeUrl(r.url) ? `<a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">${esc(r.title)}</a>` : esc(r.title));
		const callouts = [];
		if (discussed?.notes) callouts.push(['💬', 'Most discussed', plural(discussed.notes, 'comment'), `${link(discussed)} · ${esc(discussed.project)}`]);
		if (longest) callouts.push(['🐢', 'Longest road to merge', duration(longest.ms), `${link(longest.r)} · ${esc(longest.r.project)}`]);

		return section('requests', 'Collaboration', `${V.REQS} and reviews`, '',
			`<div class="tiles" style="margin-top:0">${tiles.map(([l, v, sub]) => `<div class="tile"><span class="t-label">${l}</span><span class="t-value">${v}</span>${sub ? `<span class="t-sub">${sub}</span>` : ''}</div>`).join('')}</div>
			${s.ttm.length ? `<div style="margin-top:32px"><p class="kicker">How long ${V.reqs} took to merge</p><div class="chart" data-chart="ttm"></div></div>` : ''}
			${callouts.length ? `<div class="cards" style="margin-top:28px">${callouts.map(card).join('')}</div>` : ''}`);
	}

	function card([icon, label, value, detail], i = 0) {
		return `<article class="card" style="--i:${i}"><div class="c-icon" aria-hidden="true">${icon}</div><p class="c-label">${label}</p><p class="c-value">${value}</p>${detail ? `<p class="c-detail">${detail}</p>` : ''}</article>`;
	}

	function highlightsSection(s) {
		if (!s.cs.length) return '';
		const project = (c) => esc(projects[c.r]?.path || '');
		const first = s.cs[0];
		const last = s.cs[s.cs.length - 1];
		const cards = [];
		if (s.streak.len) cards.push(['🔥', 'Longest streak', plural(s.streak.len, 'day'), `${fmtDay(s.streak.start)} → ${fmtDay(s.streak.end)}`]);
		if (s.busiest) cards.push(['⚡', 'Busiest day', plural(s.busiest.n, 'commit'), `${WEEKDAYS_LONG[weekdayOf(s.busiest.day)]}, ${fmtDay(s.busiest.day)}`]);
		if (s.biggest) cards.push(['🧱', 'Biggest commit', `+${num(s.biggest.a)} / −${num(s.biggest.d)}`, `“${esc(s.biggest.s)}” · ${project(s.biggest)}`]);
		cards.push(['🌱', s.range === 'all' ? 'Where it started' : `First commit of ${s.range}`, fmtDay(first.day), `“${esc(first.s)}” · ${project(first)}`]);
		if (last !== first) cards.push(['📍', 'Most recent', fmtDay(last.day), `“${esc(last.s)}” · ${project(last)}`]);
		if (s.tickets.size) cards.push(['🎫', 'Tickets referenced', num(s.tickets.size), 'distinct ticket keys in commits and titles']);
		const avg = s.cs.length ? Math.round((s.adds + s.dels) / s.cs.length) : 0;
		cards.push(['📏', 'Typical commit', `${num(avg)} lines`, `${num(Math.round(s.cs.reduce((n, c) => n + c.f, 0) / s.cs.length))} files on average`]);
		if (s.mergesPct) cards.push(['🔀', 'Merge commits', `${s.mergesPct}%`, 'of your commits joined two branches']);
		return section('highlights', 'Highlights', 'Moments worth remembering', '',
			`<div class="cards">${cards.map((c, i) => card(c, i)).join('')}</div>`);
	}

	function wordsSection(s) {
		const top = Object.entries(s.words).sort((a, b) => b[1] - a[1]).slice(0, 28);
		if (top.length < 5) return '';
		const max = top[0][1], min = top[top.length - 1][1];
		const order = [...top].sort((a, b) => a[0].localeCompare(b[0]));
		const words = order.map(([word, n], i) => {
			const weight = max === min ? 0.5 : (n - min) / (max - min);
			return `<span class="word" style="--w:${weight.toFixed(3)};--i:${i}" data-tip="${esc(`${word}\nin ${plural(n, 'commit message')}`)}">${esc(word)}</span>`;
		}).join('');
		return section('words', 'Vocabulary', 'In your own words',
			`The words you reach for most in commit messages. Yours is <b>“${esc(top[0][0])}”</b>.`,
			`<div class="words">${words}</div>`);
	}

	function footer() {
		const src = D.source || {};
		const parts = [];
		if (src.demo) parts.push('sample data');
		else {
			if (src.host) parts.push(`${esc(src.label || V.host)} (${esc(src.host)})`);
			if (src.clonedRepos) parts.push(plural(src.clonedRepos, 'cloned repository', 'cloned repositories'));
			if (src.localRepos) parts.push(plural(src.localRepos, 'local checkout'));
		}
		const warnings = D.warnings || [];
		const actions = [];
		if (hosted && query.get('job')) actions.push(`<a class="btn small" href="api/jobs/${encodeURIComponent(query.get('job'))}/result?download=1">Download history.json</a>`);
		if (hosted) actions.push('<a class="btn small" href="./">Make another story</a>');
		actions.push('<button class="btn small" type="button" data-print>Print / save as PDF</button>');
		return `<footer class="foot">
			<p>Built by ghis on ${fmtDay(generatedDay)} from ${parts.join(', ') || 'git'}.
			Commits matched by ${esc((src.emails || []).join(', ') || 'your email')}.</p>
			${warnings.length ? `<details><summary>${plural(warnings.length, 'note')} about this data</summary><ul>${warnings.map((w) => `<li>${esc(w)}</li>`).join('')}</ul></details>` : ''}
			<div class="actions">${actions.join('')}</div>
		</footer>`;
	}

	function dataTable(headers, rows, numericCols = []) {
		const isNum = (i) => numericCols.includes(i);
		return `<details class="table"><summary>View as table</summary><div class="table-wrap"><table>
			<thead><tr>${headers.map((h, i) => `<th class="${isNum(i) ? 'n' : ''}" scope="col">${esc(h)}</th>`).join('')}</tr></thead>
			<tbody>${rows.map((row) => `<tr>${row.map((cell, i) => `<td class="${isNum(i) ? 'n' : ''}">${esc(cell)}</td>`).join('')}</tr>`).join('')}</tbody>
		</table></div></details>`;
	}

	// ------------------------------------------------------------ charts

	function columnChart(items, width, { height = 220, labelMax = false, labelAll = false } = {}) {
		const padL = 40, padR = 6, padT = 22, padB = 28;
		const iw = Math.max(10, width - padL - padR), ih = height - padT - padB;
		const max = Math.max(1, ...items.map((d) => d.value));
		const ticks = niceTicks(max, 4);
		const top = ticks[ticks.length - 1];
		const band = iw / items.length;
		const bw = Math.max(2, Math.min(24, band * 0.68));
		const y = (v) => padT + ih - (v / top) * ih;
		const labelStep = labelAll ? 1 : Math.max(1, Math.ceil(items.length / Math.max(1, Math.floor(iw / 52))));
		const maxIndex = items.findIndex((d) => d.value === max);
		const focusable = items.length <= 40;
		let out = '';
		for (const t of ticks) out += `<line class="${t === 0 ? 'baseline' : 'gridline'}" x1="${padL}" x2="${width - padR}" y1="${y(t)}" y2="${y(t)}"/><text class="ax" x="${padL - 8}" y="${y(t) + 4}" text-anchor="end">${num(t)}</text>`;
		items.forEach((d, i) => {
			const cx = padL + band * i + band / 2;
			const x = cx - bw / 2;
			const h = (d.value / top) * ih;
			out += `<rect class="hit" x="${padL + band * i}" y="${padT}" width="${band}" height="${ih}" data-tip="${esc(d.tip)}"${focusable ? ` tabindex="0" aria-label="${esc(d.tip.replace(/\n/g, ', '))}"` : ''}/>`;
			out += d.value ? `<path class="col" style="--i:${i}" d="${columnPath(x, y(d.value), bw, h, 4)}"/>` : '<g></g>';
			if (i % labelStep === 0) out += `<text class="ax" x="${cx}" y="${height - 8}" text-anchor="middle">${esc(d.short)}</text>`;
			if (labelMax && i === maxIndex && d.value) out += `<text class="ax-strong" x="${cx}" y="${y(d.value) - 7}" text-anchor="middle">${num(d.value)}</text>`;
		});
		return `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-label="Column chart">${out}</svg>`;
	}

	function heatmapSVG(year, s, thresholds, width) {
		const jan1 = `${year}-01-01`;
		const start = addDays(jan1, -weekdayOf(jan1));
		const end = `${year}-12-31`;
		const weeks = Math.ceil((daysBetween(start, end) + 1) / 7);
		const labelW = 30, top = 18;
		const cell = clamp(Math.floor((width - labelW) / weeks), 9, 18);
		const size = cell - 2; // the 2px gap between squares is the surface showing through
		const w = labelW + weeks * cell, h = top + 7 * cell;
		const level = (v) => (!v ? 0 : v <= thresholds[0] ? 1 : v <= thresholds[1] ? 2 : v <= thresholds[2] ? 3 : 4);
		let cells = '', labels = '';
		for (let i = 0; ; i++) {
			const day = addDays(start, i);
			if (day > end) break;
			if (!day.startsWith(year)) continue;
			const week = Math.floor(i / 7), wd = i % 7;
			const x = labelW + week * cell, y = top + wd * cell;
			const v = s.contrib.get(day) || 0;
			cells += `<rect class="hm l${level(v)}" style="--w:${week}" x="${x}" y="${y}" width="${size}" height="${size}" rx="2" data-tip="${esc(dayTip(day, s))}"/>`;
			if (day.endsWith('-01')) labels += `<text class="ax" x="${x}" y="11">${monthShort.format(utc(day))}</text>`;
		}
		[0, 2, 4].forEach((wd) => { labels += `<text class="ax" x="0" y="${top + wd * cell + size - 1}">${WEEKDAYS[wd]}</text>`; });
		return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="Activity calendar for ${year}">${labels}${cells}</svg>`;
	}

	function dayTip(day, s) {
		const lines = [`${WEEKDAYS_LONG[weekdayOf(day)]}, ${fmtDay(day)}`];
		const c = s.byDay.get(day);
		if (c) lines.push(`${plural(c.n, 'commit')} · +${num(c.a)} / −${num(c.d)}`);
		for (const [action, n] of Object.entries(activity[day] || {})) {
			if (ACTIONS[action]) { const [one, many] = ACTIONS[action]; lines.push(`${num(n)} ${n === 1 ? one : many}`); }
		}
		if (lines.length === 1) lines.push('No activity');
		return lines.join('\n');
	}

	function punchSVG(punch, width) {
		const labelW = 36, top = 6, bottom = 24;
		const col = Math.max(10, (width - labelW) / 24);
		const row = clamp(col * 1.05, 20, 34);
		const w = labelW + col * 24, h = top + row * 7 + bottom;
		const max = Math.max(1, ...punch.flat());
		const rMax = Math.min(col, row) / 2 - 1.5;
		const step = col < 22 ? 6 : 3;
		let out = '';
		punch.forEach((hours, wd) => {
			const cy = top + row * wd + row / 2;
			out += `<text class="ax" x="0" y="${cy + 4}">${WEEKDAYS[wd]}</text>`;
			hours.forEach((v, hr) => {
				const cx = labelW + col * hr + col / 2;
				const r = v ? Math.max(2.5, Math.sqrt(v / max) * rMax) : 1.5;
				out += `<rect class="pc-hit" x="${cx - col / 2}" y="${cy - row / 2}" width="${col}" height="${row}" data-tip="${esc(`${WEEKDAYS_LONG[wd]} ${hourLabel(hr)}–${hourLabel((hr + 1) % 24)}\n${plural(v, 'commit')}`)}"/>`;
				out += `<circle class="${v ? 'pc' : 'pc0'}" style="--i:${hr}" cx="${cx}" cy="${cy}" r="${r}"/>`;
			});
		});
		for (let hr = 0; hr < 24; hr += step) out += `<text class="ax" x="${labelW + col * hr + col / 2}" y="${h - 6}" text-anchor="middle">${hourLabel(hr)}</text>`;
		return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="Commits by weekday and hour">${out}</svg>`;
	}

	function drawCharts(animate) {
		for (const { id, draw } of chartDrawers) {
			const el = app.querySelector(`[data-chart="${id}"]`);
			if (!el) continue;
			el.innerHTML = draw(Math.floor(el.clientWidth || 600));
		}
		// Re-rendered charts in an already-visible section grow in again from zero.
		if (animate && !reduceMotion) {
			const charts = app.querySelectorAll('.reveal.in .chart');
			charts.forEach((el) => el.classList.add('pending'));
			requestAnimationFrame(() => requestAnimationFrame(() => charts.forEach((el) => el.classList.remove('pending'))));
		}
		runCounters(app.querySelectorAll('.hero .count'));
		for (const el of app.querySelectorAll('.reveal.in')) runCounters(el.querySelectorAll('.count'));
	}

	let resizeTimer;
	let lastWidth = innerWidth;
	addEventListener('resize', () => {
		if (Math.abs(innerWidth - lastWidth) < 8) return;
		lastWidth = innerWidth;
		clearTimeout(resizeTimer);
		resizeTimer = setTimeout(() => drawCharts(false), 150);
	});

	// ------------------------------------------------------------ motion

	let observer;
	function observe() {
		observer?.disconnect();
		const sections = app.querySelectorAll('.reveal');
		if (reduceMotion || !('IntersectionObserver' in window)) {
			sections.forEach((el) => el.classList.add('in'));
			return;
		}
		observer = new IntersectionObserver((entries) => {
			for (const entry of entries) {
				if (!entry.isIntersecting) continue;
				entry.target.classList.add('in');
				revealed.add(entry.target.id);
				runCounters(entry.target.querySelectorAll('.count'));
				observer.unobserve(entry.target);
			}
		}, { threshold: 0.12, rootMargin: '0px 0px -40px 0px' });
		sections.forEach((el) => {
			if (revealed.has(el.id)) el.classList.add('in');
			else observer.observe(el);
		});
	}

	function runCounters(nodes) {
		for (const el of nodes) {
			if (el.dataset.done) continue;
			el.dataset.done = '1';
			const to = Number(el.dataset.count);
			const from = lastCounts.get(el.dataset.key) ?? 0;
			lastCounts.set(el.dataset.key, to);
			if (reduceMotion || from === to) { el.textContent = num(to); continue; }
			const startedAt = performance.now();
			const length = 1300;
			const tick = (now) => {
				const t = Math.min(1, (now - startedAt) / length);
				const eased = 1 - (1 - t) ** 4;
				el.textContent = num(from + (to - from) * eased);
				if (t < 1) requestAnimationFrame(tick);
			};
			el.textContent = num(from);
			requestAnimationFrame(tick);
		}
	}

	// ------------------------------------------------------------ tooltip

	function showTip(target, x, y) {
		const lines = target.dataset.tip.split('\n');
		tip.innerHTML = `<strong>${esc(lines[0])}</strong>${lines.slice(1).map((l) => `<span>${esc(l)}</span>`).join('')}`;
		tip.hidden = false;
		placeTip(x, y);
	}
	function placeTip(x, y) {
		const w = tip.offsetWidth;
		tip.style.left = `${clamp(x, w / 2 + 8, innerWidth - w / 2 - 8)}px`;
		tip.style.top = `${Math.max(tip.offsetHeight + 22, y)}px`;
	}
	const hideTip = () => { tip.hidden = true; };

	document.addEventListener('pointerover', (e) => {
		const target = e.target.closest?.('[data-tip]');
		if (target) showTip(target, e.clientX, e.clientY); else hideTip();
	});
	document.addEventListener('pointermove', (e) => { if (!tip.hidden) placeTip(e.clientX, e.clientY); });
	document.addEventListener('focusin', (e) => {
		const target = e.target.closest?.('[data-tip]');
		if (!target) return;
		const r = target.getBoundingClientRect();
		showTip(target, r.left + r.width / 2, r.top);
	});
	document.addEventListener('focusout', hideTip);
	document.addEventListener('click', (e) => { if (e.target.closest('[data-print]')) window.print(); });
	addEventListener('scroll', hideTip, { passive: true });
})();
