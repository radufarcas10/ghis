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
	// Colours go by all-time lines changed: Vue (600) before Python (6 × 12).
	assert.equal(model.langColors.get('Vue'), 'var(--s1)');
	assert.equal(model.langColors.get('Python'), 'var(--s2)');
});

test('compute: tickets and vocabulary skip noise words and stray punctuation', () => {
	const model = lib.createModel({
		commits: [
			commit('2024-01-01T10:00:00Z', { s: "APP-12 Fix the checkout' flow" }),
			commit('2024-01-02T10:00:00Z', { s: 'APP-12 checkout tweaks for CORE-7' }),
		],
		requests: [{ title: 'OPS-3 Deploy', createdAt: '2024-01-03T00:00:00Z', state: 'opened' }],
	});
	const s = model.compute('all');
	assert.deepEqual([...s.tickets].sort(), ['APP-12', 'CORE-7', 'OPS-3']);
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
