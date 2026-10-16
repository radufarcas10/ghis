// Unit tests for the story page's logic. Node's built-in runner, no dependencies:
//   node --test tests/js/
const test = require('node:test');
const assert = require('node:assert/strict');
const lib = require('../../src/ghis/web/lib.js');

// A commit as history.json stores it; `t` keeps the author's own UTC offset.
const commit = (t, extra = {}) => ({ h: 'x', t, a: 10, d: 2, f: 1, s: 'Work', m: 0, l: { Python: 12 }, r: 0, ...extra });

test('esc escapes everything that could open markup', () => {
	assert.equal(lib.esc(`<img src=x onerror="a('b')">&`), '&lt;img src=x onerror=&quot;a(&#39;b&#39;)&quot;&gt;&amp;');
	assert.equal(lib.esc(null), '');
});

test('safeUrl only lets http(s) links through', () => {
	assert.equal(lib.safeUrl('https://example.com/a'), 'https://example.com/a');
	assert.equal(lib.safeUrl('HTTP://example.com'), 'HTTP://example.com');
	assert.equal(lib.safeUrl('javascript:alert(1)'), null);
	assert.equal(lib.safeUrl(' javascript:alert(1)'), null);
	assert.equal(lib.safeUrl('data:text/html,hi'), null);
	assert.equal(lib.safeUrl(undefined), null);
});

test('numbers, plurals and durations read naturally', () => {
	assert.equal(lib.num(1284), '1,284');
	assert.equal(lib.num(12900), '12.9K');
	assert.equal(lib.plural(1, 'commit'), '1 commit');
	assert.equal(lib.plural(3, 'repository', 'repositories'), '3 repositories');
	assert.equal(lib.duration(30 * 60e3), '30 min');
	assert.equal(lib.duration(5 * 3600e3), '5.0 h');
	assert.equal(lib.duration(3 * 86400e3), '3.0 days');
});

test('build times read naturally, and the stopwatch ticks like one', () => {
	assert.equal(lib.buildTime(0.4), 'under a second'); // never "0 s"
	assert.equal(lib.buildTime(1), '1 s');
	assert.equal(lib.buildTime(45.4), '45 s');
	assert.equal(lib.buildTime(72), '1 min 12 s');
	assert.equal(lib.buildTime(120), '2 min');
	assert.equal(lib.buildTime(14 * 60 + 20), '14 min'); // seconds are noise on a long wait
	assert.equal(lib.buildTime(3600 + 3 * 60), '1 h 3 min');
	assert.equal(lib.buildTime(7200), '2 h');
	assert.equal(lib.clock(0), '0:00');
	assert.equal(lib.clock(7.9), '0:07'); // a stopwatch never rounds up
	assert.equal(lib.clock(12 * 60 + 45), '12:45');
	assert.equal(lib.clock(3723), '1:02:03');
	assert.equal(lib.median([5, 1, 3]), 3);
	assert.equal(lib.median([4, 1, 3, 2]), 2.5);
	assert.equal(lib.median([]), null);
	assert.equal(lib.initials('Ada  Lovelace'), 'AL');
	assert.equal(lib.initials('radu.farcas'), 'RF');
});

test('dates: weekdays start on Monday and ignore the local timezone', () => {
	assert.equal(lib.weekdayOf('2024-03-04'), 0); // a Monday
	assert.equal(lib.weekdayOf('2024-03-10'), 6); // a Sunday
	assert.equal(lib.addDays('2024-02-28', 2), '2024-03-01'); // leap year
	assert.equal(lib.daysBetween('2024-01-01', '2025-01-01'), 366);
});

test('niceTicks gives whole-number axes that cover the maximum', () => {
	for (const max of [1, 3, 7, 10, 23, 99, 187, 1000, 4321]) {
		const ticks = lib.niceTicks(max);
		assert.equal(ticks[0], 0);
		assert.ok(ticks[ticks.length - 1] >= max, `top tick covers ${max}`);
		assert.ok(ticks.every(Number.isInteger), `integers for ${max}: ${ticks}`);
		assert.ok(ticks.length >= 2 && ticks.length <= 7, `tick count for ${max}: ${ticks}`);
	}
});

test('columnPath rounds the data end and keeps the baseline square', () => {
	assert.equal(lib.columnPath(0, 10, 20, 30, 4), 'M0,40V14Q0,10 4,10H16Q20,10 20,14V40Z');
	// A short column cannot be rounded by more than its own height.
	assert.match(lib.columnPath(0, 10, 20, 2, 4), /^M0,12V12Q0,10 2,10/);
});

test('vocabulary follows the provider', () => {
	assert.equal(lib.vocabulary('gitlab').REQS, 'MRs');
	assert.equal(lib.vocabulary('github').req, 'pull request');
	assert.equal(lib.vocabulary('gitea', 'codeberg.org').host, 'codeberg.org');
	assert.deepEqual(lib.actionLabels(lib.vocabulary('gitlab')).merged, ['merge request merged', 'merge requests merged']);
	assert.equal(lib.contributionCount({ commented: 2, approved: 1, pushed: 9, joined: 1 }), 3);
});

test('compute: streaks, busiest day, repositories and languages', () => {
	const model = lib.createModel({
		generatedAt: '2024-12-31T00:00:00Z',
		commits: [
			commit('2024-03-04T10:00:00+02:00', { r: 0 }),
			commit('2024-03-05T23:30:00+02:00', { r: 0 }),
			commit('2024-03-05T11:00:00+02:00', { r: 1, a: 500, d: 100, l: { Vue: 600 } }),
			commit('2024-03-06T09:00:00+02:00', { r: 1 }),
			commit('2024-03-09T10:00:00+02:00', { r: 0, m: 1 }), // a Saturday merge commit
			commit('2023-06-01T10:00:00+00:00', { r: 0 }),
		],
		activity: { '2024-03-07': { commented: 2, pushed: 5 } },
	});
	assert.deepEqual(model.years, ['2024', '2023']);
	const s = model.compute('2024');
	assert.equal(s.cs.length, 5);
	// Mar 4, 5, 6 (commits) and 7 (a comment) are consecutive; the push does not count.
	assert.deepEqual([s.streak.start, s.streak.end, s.streak.len], ['2024-03-04', '2024-03-07', 4]);
	assert.equal(s.busiest.day, '2024-03-05');
	assert.equal(s.biggest.a, 500);
	assert.deepEqual(s.repos.map((r) => [r.r, r.n]), [[0, 3], [1, 2]]);
	assert.equal(s.langs.Vue, 600);
	assert.equal(s.weekendPct, 20);
	assert.equal(s.mergesPct, 20);
	assert.equal(s.nightPct, 20); // 23:30 in the author's own timezone
	assert.equal(model.compute('all').cs.length, 6);
	// Colors go by all-time lines changed: Vue (600) before Python (6 × 12).
	assert.equal(model.langColors.get('Vue'), 'var(--s1)');
	assert.equal(model.langColors.get('Python'), 'var(--s2)');
});

test('compute: tickets are real tracker keys, not things shaped like them', () => {
	const model = lib.createModel({
		commits: [
			commit('2024-01-01T10:00:00Z', { s: "APP-12 Fix the checkout' flow" }),
			commit('2024-01-02T10:00:00Z', { s: 'APP-14 checkout tweaks, see CORE-7' }),
			commit('2024-01-03T10:00:00Z', { s: 'CORE-9 Read files as UTF-8, hash with SHA-256' }),
			commit('2024-01-04T10:00:00Z', { s: 'Dates in ISO-8601, patch CVE-2021-44228, bump TOOL-3' }),
		],
		requests: [{ title: 'OPS-3 Deploy', createdAt: '2024-01-05T00:00:00Z', state: 'opened' }],
	});
	const s = model.compute('all');
	// APP and CORE recur with different numbers; UTF, SHA, ISO and CVE are never tickets;
	// TOOL and OPS appear once each, too little to call them a tracker.
	assert.deepEqual([...s.tickets].sort(), ['APP-12', 'APP-14', 'CORE-7', 'CORE-9']);
	assert.equal(s.words.checkout, 2);
	assert.equal(s.words.fix, undefined); // a stop word
	assert.equal(s.words['checkout\''], undefined);
});

test('compute: merge request timing', () => {
	const model = lib.createModel({
		requests: [
			{ createdAt: '2024-01-01T10:00:00Z', mergedAt: '2024-01-01T12:00:00Z', state: 'merged', title: 'a' },
			{ createdAt: '2024-01-02T10:00:00Z', mergedAt: '2024-01-04T10:00:00Z', state: 'merged', title: 'b' },
			{ createdAt: '2024-01-03T10:00:00Z', state: 'opened', title: 'c' },
		],
	});
	const s = model.compute('all');
	assert.equal(s.reqs.length, 3);
	assert.deepEqual(s.ttm.map((x) => x.ms / 3600e3), [2, 48]);
});

test('monthList covers a range without gaps', () => {
	const model = lib.createModel({
		generatedAt: '2024-04-15T00:00:00Z',
		commits: [commit('2023-11-20T10:00:00Z'), commit('2024-02-01T10:00:00Z')],
	});
	assert.deepEqual(model.monthList(model.compute('all')), ['2023-11', '2023-12', '2024-01', '2024-02']);
	// The current year stops at the month the story was made.
	assert.deepEqual(model.monthList(model.compute('2024')), ['2024-01', '2024-02', '2024-03', '2024-04']);
	assert.equal(model.monthList(model.compute('2023')).length, 12);
});

test('persona picks the strongest habit', () => {
	const at = (hour, n, day = '2024-03-04') => Array.from({ length: n }, () => commit(`${day}T${String(hour).padStart(2, '0')}:00:00Z`));
	const of = (commits) => lib.persona(lib.createModel({ commits }).compute('all'));
	assert.equal(of(at(23, 5).concat(at(11, 10)))[1], 'Night owl');
	assert.equal(of(at(11, 12))[1], 'Office-hours coder');
	assert.equal(of(at(11, 12, '2024-03-09'))[1], 'Weekend warrior');
	assert.equal(of(at(11, 3)), null); // too little to say
});

// ------------------------------------------------------------ accuracy

test('bulk commits count as commits but stay out of lines, languages and "biggest"', () => {
	const model = lib.createModel({
		commits: [
			...Array.from({ length: 20 }, (_, i) => commit(`2024-02-${String(i + 1).padStart(2, '0')}T10:00:00Z`)), // 12 lines each
			commit('2024-03-01T10:00:00Z', { a: 300, d: 20, s: 'Real feature' }),
			commit('2024-03-02T10:00:00Z', { a: 48000, d: 0, s: 'Import legacy module', l: { PHP: 48000 } }),
		],
	});
	assert.equal(model.bulkThreshold, 2000); // the floor: 40 × a 12-line median is only 480
	const s = model.compute('all');
	assert.equal(s.cs.length, 22);
	assert.deepEqual([s.bulk.n, s.bulk.a], [1, 48000]);
	assert.equal(s.adds, 20 * 10 + 300);
	assert.equal(s.langs.PHP, undefined);
	assert.equal(s.biggest.s, 'Real feature');
});

test('co-authored commits are counted and flagged', () => {
	const model = lib.createModel({ commits: [commit('2024-01-01T10:00:00Z'), commit('2024-01-02T10:00:00Z', { co: 1 })] });
	const s = model.compute('all');
	assert.equal(s.cs.length, 2);
	assert.equal(s.coAuthored, 1);
});

test('activity moves into the author\'s timezone, daylight saving included', () => {
	const model = lib.createModel({
		commits: [
			commit('2024-01-10T22:00:00+02:00'), // winter: +02:00
			commit('2024-07-10T22:00:00+03:00'), // summer: +03:00
		],
		activity: {
			'2024-01-10T21': { commented: 1 }, // 23:00 at +02:00, still Jan 10
			'2024-01-10T22': { approved: 1 }, // 00:00 at +02:00, already Jan 11
			'2024-07-10T21': { commented: 1 }, // 00:00 at +03:00, already Jul 11
			'2024-02-02': { reviewed: 1 }, // an older day key passes through
		},
	});
	assert.deepEqual(model.activity['2024-01-10'], { commented: 1 });
	assert.deepEqual(model.activity['2024-01-11'], { approved: 1 });
	assert.deepEqual(model.activity['2024-07-11'], { commented: 1 });
	assert.deepEqual(model.activity['2024-02-02'], { reviewed: 1 });
	assert.equal(lib.offsetOf('2024-01-10T22:00:00-05:00'), -300);
	assert.equal(lib.offsetOf('2024-01-10T22:00:00Z'), 0);
});

// ------------------------------------------------------------ exploring

const projectsData = {
	generatedAt: '2025-06-15T12:00:00Z',
	user: { username: 'ada', createdAt: '2023-06-01T00:00:00Z' },
	projects: [{ path: 'acme/web', name: 'web' }, { path: 'acme/api', name: 'api' }],
	commits: [
		commit('2024-02-01T10:00:00Z', { r: 0 }),
		commit('2024-05-01T10:00:00Z', { r: 1 }),
		commit('2024-09-01T10:00:00Z', { r: 0 }),
		commit('2025-02-01T10:00:00Z', { r: 0 }),
		commit('2025-06-13T10:00:00Z', { r: 0 }),
		commit('2025-06-14T10:00:00Z', { r: 1 }),
		commit('2025-06-15T09:00:00Z', { r: 0 }),
	],
	requests: [
		{ project: 'acme/web', title: 'a', createdAt: '2024-03-01T10:00:00Z', mergedAt: '2024-03-02T10:00:00Z', state: 'merged', reviewers: ['bob', 'cleo'] },
		{ project: 'acme/api', title: 'b', createdAt: '2025-03-01T10:00:00Z', mergedAt: '2025-03-01T12:00:00Z', state: 'merged', reviewers: ['bob'] },
	],
	reviews: [
		{ project: 'acme/web', title: 'c', author: 'bob', createdAt: '2025-01-05T10:00:00Z' },
		{ project: 'acme/web', title: 'd', author: 'bob', createdAt: '2025-01-06T10:00:00Z' },
		{ project: 'acme/api', title: 'e', author: 'dina', createdAt: '2025-01-07T10:00:00Z' },
	],
};

test('the repository filter narrows commits and requests', () => {
	const model = lib.createModel(projectsData);
	assert.deepEqual(model.repoOptions.map((o) => [o.path, o.n]), [['acme/web', 5], ['acme/api', 2]]);
	const web = model.compute('all', { repo: 0 });
	assert.equal(web.cs.length, 5);
	assert.equal(web.reqs.length, 1);
	assert.equal(web.revs.length, 2);
	assert.deepEqual(web.events, {}); // events carry no repository
});

test('a year in progress is compared with the same days of the year before', () => {
	const model = lib.createModel(projectsData);
	const now = model.compute('2025');
	const before = model.previous(now);
	assert.equal(before.range, '2024');
	assert.equal(before.until, '06-15');
	assert.equal(before.cs.length, 2); // Feb 1 and May 1 2024, not Sep 1
	assert.equal(lib.delta(now.cs.length, before.cs.length), 100);
	assert.equal(lib.delta(5, 0), null);
	assert.equal(model.previous(model.compute('all')), null);
	assert.equal(model.previous(model.compute('2024')), null); // nothing in 2023
});

test('the current streak runs up to the day the story was made', () => {
	const s = lib.createModel(projectsData).compute('all');
	assert.deepEqual([s.currentStreak.start, s.currentStreak.end, s.currentStreak.len], ['2025-06-13', '2025-06-15', 3]);
	assert.equal(lib.createModel(projectsData).compute('2024').currentStreak.len, 0);
});

test('people: who you review and who reviews you', () => {
	const s = lib.createModel(projectsData).compute('all');
	assert.deepEqual(s.people.reviewed, [['bob', 2], ['dina', 1]]);
	assert.deepEqual(s.people.reviewers, [['bob', 2], ['cleo', 1]]);
});

test('milestones are all-time moments shown in the range they fall in', () => {
	const model = lib.createModel(projectsData);
	const all = model.milestones(model.compute('all'));
	const kinds = all.map((m) => `${m.day} ${m.kind}${m.n ? ` ${m.n}` : ''}`);
	assert.deepEqual(kinds, [
		'2024-02-01 first 1',
		'2024-03-01 request',
		'2024-05-01 repo',
		'2024-06-01 anniversary 1',
		'2025-06-01 anniversary 2',
	]);
	assert.deepEqual(model.milestones(model.compute('2025')).map((m) => m.kind), ['anniversary']);
});

// ------------------------------------------------------------ sharing

test('anonymize keeps the numbers and drops what was asked', () => {
	const data = {
		...projectsData,
		source: { provider: 'gitlab', host: 'gitlab.corp.example', emails: ['ada@corp.example'] },
		warnings: ['Could not clone acme/secret'],
		commits: projectsData.commits.map((c) => ({ ...c, s: 'APP-1 secret plan', e: 'ada@corp.example' })),
		requests: projectsData.requests.map((r) => ({ ...r, url: 'https://gitlab.corp.example/x' })),
	};
	const safe = lib.anonymize(data);
	const text = JSON.stringify(safe);
	for (const secret of ['secret', 'corp.example', 'acme/', 'bob', 'cleo', 'dina']) assert.ok(!text.includes(secret), secret);
	assert.deepEqual(safe.projects.map((p) => p.path), ['Project A', 'Project B']); // by commits
	assert.equal(safe.requests[1].project, 'Project B');
	assert.equal(safe.reviews[0].author, safe.reviews[1].author); // one person, one alias
	assert.equal(safe.commits.length, data.commits.length);
	assert.equal(safe.commits[0].a, data.commits[0].a);
	assert.deepEqual(safe.source.shareSafe, { messages: true, names: true });
	assert.equal(data.projects[0].path, 'acme/web'); // the original is untouched

	const covered = lib.anonymize({
		...data,
		coverage: { repos: [{ path: 'acme/web', via: 'local', commits: 5 }], skipped: [{ path: 'acme/api', reason: 'could not be cloned: auth failed for gitlab.corp.example' }], excludedEmails: ['bob@corp.example'], limits: [] },
	});
	assert.deepEqual(covered.coverage.repos.map((r) => r.path), ['Project A']);
	assert.ok(!JSON.stringify(covered.coverage).includes('corp.example'));

	const namesOnly = lib.anonymize(data, { messages: false, names: true });
	assert.equal(namesOnly.commits[0].s, 'APP-1 secret plan');
	const messagesOnly = lib.anonymize(data, { messages: true, names: false });
	assert.equal(messagesOnly.projects[0].path, 'acme/web');
	assert.equal(messagesOnly.commits[0].s, '');
});

test('year-in-review season runs from December 1 to January 15', () => {
	assert.equal(lib.reviewSeason('2026-12-01'), '2026');
	assert.equal(lib.reviewSeason('2026-12-31'), '2026');
	assert.equal(lib.reviewSeason('2027-01-15'), '2026');
	assert.equal(lib.reviewSeason('2027-01-16'), null);
	assert.equal(lib.reviewSeason('2026-11-30'), null);
	assert.equal(lib.reviewSeason('2026-06-10'), null);
});
