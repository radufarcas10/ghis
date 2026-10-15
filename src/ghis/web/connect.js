// ghis connect page: pick a provider, start a collection job, stream its progress,
// then hand over to the story page. Also accepts a history.json made by the CLI.
(() => {
	'use strict';

	const $ = (selector) => document.querySelector(selector);
	const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({
		'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
	}[c]));

	const ICONS = {
		github: '<svg width="1em" height="1em" viewBox="0 0 26 26" aria-hidden="true"><circle cx="13" cy="13" r="12" fill="currentColor"/><path d="m10 9-4 4 4 4m6-8 4 4-4 4" fill="none" stroke="var(--surface)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
		gitlab: '<svg width="1em" height="1em" viewBox="0 0 26 26" aria-hidden="true"><path d="M13 23 2.6 15.2 5.4 4.4l3 8.2h9.2l3-8.2 2.8 10.8Z" fill="currentColor"/></svg>',
		gitea: '<svg width="1em" height="1em" viewBox="0 0 26 26" aria-hidden="true"><rect x="2" y="2" width="22" height="22" rx="6" fill="currentColor"/><path d="M9 19V10a3 3 0 0 1 3-3h5M9 13.5h5" fill="none" stroke="var(--surface)" stroke-width="2" stroke-linecap="round"/></svg>',
		git: '<svg width="1em" height="1em" viewBox="0 0 26 26" aria-hidden="true"><rect x="3.5" y="3.5" width="19" height="19" rx="3" transform="rotate(45 13 13)" fill="currentColor"/><g fill="none" stroke="var(--surface)" stroke-width="1.8" stroke-linecap="round"><path d="M10 9.5 13 12.5v5M13 12.5l3-1.5"/></g><circle cx="13" cy="18" r="1.6" fill="var(--surface)"/><circle cx="16.4" cy="10.8" r="1.6" fill="var(--surface)"/></svg>',
	};
	const SHORT = { github: 'GitHub', gitlab: 'GitLab', gitea: 'Codeberg', git: 'Any git' };
	const SUB = { github: 'and Enterprise', gitlab: 'and self-managed', gitea: 'Forgejo · Gitea', git: 'Bitbucket, sr.ht…' };

	const TOKEN_HELP = {
		github: (url) => ({
			href: `${hostRoot(url, 'github.com')}/settings/tokens/new?description=ghis&scopes=repo,read:user,user:email`,
			text: 'Create a classic token with <b>repo</b>, <b>read:user</b> and <b>user:email</b>',
			after: '. Or a fine-grained one with read-only Contents and Pull requests.',
		}),
		gitlab: (url) => ({
			href: `${url}/-/user_settings/personal_access_tokens?name=ghis&scopes=read_api,read_user,read_repository`,
			text: 'Create a token with <b>read_api</b>, <b>read_user</b> and <b>read_repository</b>',
		}),
		gitea: (url) => ({
			href: `${url}/user/settings/applications`,
			text: 'Create a token with read access to <b>user</b>, <b>repository</b> and <b>issue</b>',
		}),
	};

	const hostRoot = (url, fallback) => (url ? url.replace(/\/+$/, '') : `https://${fallback}`);

	// Step-by-step help under the token field; `link` is the provider's token page.
	const TOKEN_STEPS = {
		github: (link) => [
			`Open <a href="${link}" target="_blank" rel="noopener noreferrer">your token settings ↗</a>. The scopes are already ticked.`,
			'Name it "ghis" and pick a short expiry, 7 days is plenty.',
			'Keep <b>repo</b>, <b>read:user</b> and <b>user:email</b> ticked, then <b>Generate token</b>.',
			'Copy the token and paste it above. If your organization uses SSO, choose <b>Configure SSO → Authorize</b> next to the token first.',
		],
		gitlab: (link) => [
			`Open <a href="${link}" target="_blank" rel="noopener noreferrer">your access tokens ↗</a>. The scopes are already ticked.`,
			'Pick a short expiry date.',
			'Keep <b>read_api</b>, <b>read_user</b> and <b>read_repository</b> ticked, then <b>Create token</b>.',
			'Copy the token and paste it above.',
		],
		gitea: (link) => [
			`Open <a href="${link}" target="_blank" rel="noopener noreferrer">Settings → Applications ↗</a>.`,
			'Under <b>Generate new token</b>, name it "ghis".',
			'Set <b>user</b>, <b>repository</b> and <b>issue</b> to <b>Read</b>, then <b>Generate token</b>.',
			'Copy the token and paste it above.',
		],
		git: () => [
			'You only need one for <b>private</b> repositories cloned over HTTPS.',
			'Bitbucket: create an API token with read access to repositories, and put your Bitbucket username in <b>HTTPS username</b>.',
			'Azure DevOps, Sourcehut and others: any personal access token that can read code.',
			'Public repositories and SSH URLs need no token.',
		],
	};

	let config = { providers: [], allowedHosts: [] };
	let provider = 'github';
	let detectRun = 0;
	// Detected emails the user unticked: kept across provider switches, sent with the form.
	const excludedEmails = new Set();

	// ------------------------------------------------------------ hero preview

	(function preview() {
		const root = $('#preview');
		const columns = Math.min(26, Math.floor((innerWidth - 32) / 14));
		let html = '';
		for (let c = 0; c < columns; c++) {
			html += '<span class="c">';
			for (let r = 0; r < 7; r++) {
				const delay = Math.round((c * 90 + r * 45 + (Math.sin(c * 12.9898 + r * 78.233) * 43758.5453 % 1) * 1600 + 1600) % 3200);
				const level = delay % 5 === 0 ? 0 : delay % 4; // a static pattern when motion is off
				html += `<i style="--d:${delay};--b:var(--q${level})"></i>`;
			}
			html += '</span>';
		}
		root.innerHTML = html;
	})();

	// ------------------------------------------------------------ provider picker

	function renderProviders() {
		$('#providers').innerHTML = config.providers.map((p) => `
			<button type="button" class="provider" role="radio" data-provider="${p.id}" aria-checked="${p.id === provider}">
				${ICONS[p.id] || ''}<span>${esc(SHORT[p.id] || p.label)}<br><small>${esc(SUB[p.id] || '')}</small></span>
			</button>`).join('');
	}

	function selectProvider(id) {
		provider = id;
		renderProviders();
		const spec = config.providers.find((p) => p.id === id) || {};
		const isGit = id === 'git';
		$('#f-url').hidden = isGit;
		$('#f-repos').hidden = !isGit;
		$('#f-git-extra').hidden = !isGit;
		$('#f-folders').hidden = !config.personal;
		$('#repos-hint').textContent = config.personal
			? 'Optional. One clone URL per line, HTTPS or SSH, on any host. Your local repositories are always included.'
			: 'One HTTPS clone URL per line, on any host git can clone from.';
		// A team's server can start everyone on its own instance (GHIS_DEFAULT_URL).
		$('#url').value = (id === config.defaults?.provider && config.defaults.url) || spec.url || '';
		$('#url-hint').textContent = id === 'github'
			? 'Change it for GitHub Enterprise Server.'
			: id === 'gitlab' ? 'Change it for a self-managed GitLab.' : 'Codeberg by default. Any Forgejo or Gitea instance works.';
		$('#token-label').textContent = isGit
			? 'Access token (only for private repositories)'
			: config.personal ? 'Access token (optional)' : 'Access token';
		$('#emails-label').textContent = isGit && !config.personal ? 'Emails you commit with' : 'Other emails you commit with';
		$('#emails-hint').textContent = config.personal
			? 'Optional. The emails found above are used automatically; add any others here.'
			: isGit
				? 'Required: commits are matched by author email.'
				: "Commits are matched by author email. Your account's emails are added automatically.";
		updateTokenHint();
		hideError();
		if (config.personal) detect();
	}

	// ------------------------------------------------------------ personal mode: what this computer knows

	const DETECT_ICON = { ok: '✓', no: '○', busy: '…' };
	const item = (state, html) => `<li class="d-${state}"><span class="d-mark" aria-hidden="true">${DETECT_ICON[state]}</span><span>${html}</span></li>`;

	async function detect() {
		const run = ++detectRun;
		const box = $('#detect');
		box.hidden = false;
		box.innerHTML = item('busy', 'Looking around this computer for your repositories, emails and tokens…');
		let found;
		try {
			const response = await fetch('api/detect', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ provider, url: provider === 'git' ? '' : $('#url').value.trim() }),
			});
			found = await response.json();
			if (!response.ok) throw new Error(found.error);
		} catch {
			if (run === detectRun) box.hidden = true;
			return;
		}
		if (run !== detectRun) return; // the provider changed while this was running
		const spec = config.providers.find((p) => p.id === provider) || {};
		if (found.url && found.url !== $('#url').value.trim()) {
			$('#url').value = found.url;
			updateTokenHint();
		}
		const lines = [];
		const from = found.host ? ` from <b>${esc(found.host)}</b>` : '';
		lines.push(found.repos
			? item('ok', `<b>${found.repos} ${found.repos === 1 ? 'repository' : 'repositories'}</b>${from} in your home folder will be read in place`)
			: item('no', `No repositories${from} in your home folder${found.host ? '' : '. Add a folder below if they live elsewhere'}`));
		const emails = found.identity.emails;
		if (emails.length) {
			// Matched by your git name, so a teammate who shares it would be caught too: every
			// address is listed, with its commits, and can be unticked.
			const picks = emails.map((e) => `<label class="email-pick"><input type="checkbox" value="${esc(e.email)}"${excludedEmails.has(e.email) ? '' : ' checked'}>
				<span class="addr">${esc(e.email)}</span><span class="n">${e.commits ? `${Number(e.commits).toLocaleString('en')} commits` : 'no commits yet'}</span></label>`).join('');
			lines.push(item('ok', `You commit as <span class="d-hint">(untick any that aren't yours)</span><span class="email-picks">${picks}</span>`));
		}
		if (provider !== 'git' && found.url) {
			const host = esc(new URL(found.url).hostname);
			const what = provider === 'gitlab' ? 'merge requests' : 'pull requests';
			lines.push(found.token
				? item('ok', `Signed in to <b>${host}</b> as <b>@${esc(found.token.username)}</b>, using your token from ${esc(found.token.source)}`)
				: item('no', `No ${esc(spec.label || '')} token found for <b>${host}</b>. That's fine: you'll get a story from your local repositories. Add a token to include ${what}, reviews and repositories you never cloned.`));
		}
		box.innerHTML = lines.join('');
		for (const input of box.querySelectorAll('.email-pick input')) {
			input.addEventListener('change', () => {
				if (input.checked) excludedEmails.delete(input.value); else excludedEmails.add(input.value);
			});
		}
		$('#token').placeholder = found.token ? `Leave empty to use @${found.token.username}'s token` : '';
	}

	let urlTimer;
	$('#url').addEventListener('change', () => {
		if (!config.personal) return;
		clearTimeout(urlTimer);
		urlTimer = setTimeout(detect, 300);
	});

	function updateTokenHint() {
		const hint = $('#token-hint');
		if (provider === 'git') {
			hint.textContent = 'Sent as HTTPS basic auth with the username below. Leave empty for public repositories.';
			$('#token-steps').innerHTML = TOKEN_STEPS.git().map((step) => `<li>${step}</li>`).join('');
			return;
		}
		const url = $('#url').value.trim().replace(/\/+$/, '');
		const help = TOKEN_HELP[provider]?.(url);
		$('#token-steps').innerHTML = (TOKEN_STEPS[provider]?.(esc(help?.href || '')) || []).map((step) => `<li>${step}</li>`).join('');
		hint.innerHTML = help ? `<a href="${esc(help.href)}" target="_blank" rel="noopener noreferrer">${help.text} ↗</a>${help.after || '. Read-only scopes are enough.'}` : '';
	}

	$('#providers').addEventListener('click', (e) => {
		const button = e.target.closest('[data-provider]');
		if (button) selectProvider(button.dataset.provider);
	});
	$('#providers').addEventListener('keydown', (e) => {
		if (!['ArrowRight', 'ArrowLeft', 'ArrowDown', 'ArrowUp'].includes(e.key)) return;
		e.preventDefault();
		const ids = config.providers.map((p) => p.id);
		const step = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : -1;
		selectProvider(ids[(ids.indexOf(provider) + step + ids.length) % ids.length]);
		$(`[data-provider="${provider}"]`).focus();
	});
	$('#url').addEventListener('input', updateTokenHint);

	// ------------------------------------------------------------ errors

	function showError(message) {
		const box = $('#error');
		box.textContent = message;
		box.hidden = false;
	}
	function hideError() { $('#error').hidden = true; }

	function hostAllowed(url) {
		const allowed = config.allowedHosts || [];
		if (allowed.includes('*')) return true;
		try {
			const host = new URL(url).hostname;
			return allowed.some((a) => host === a || host.endsWith(`.${a}`));
		} catch { return false; }
	}

	// ------------------------------------------------------------ submit

	$('#form').addEventListener('submit', async (e) => {
		e.preventDefault();
		hideError();
		const payload = {
			provider,
			url: $('#url').value.trim(),
			token: $('#token').value.trim(),
			emails: $('#emails').value,
			repos: provider === 'git' ? $('#repos').value : '',
			folders: config.personal ? $('#folders').value : '',
			excludeEmails: config.personal ? [...excludedEmails] : [],
			name: $('#name').value.trim(),
			cloneUser: $('#clone-user').value.trim(),
		};
		if (provider !== 'git' && !payload.token && !config.personal) return showError('Paste an access token: ghis uses it to read your history.');
		if (provider !== 'git' && !hostAllowed(payload.url)) {
			return showError(`This server can only reach ${config.allowedHosts.join(', ')}. Run ghis yourself to use another instance.`);
		}
		if (provider === 'git') {
			const urls = payload.repos.split(/\s+/).filter(Boolean);
			if (!urls.length && !config.personal) return showError('Add at least one repository URL.');
			const bad = config.personal ? null : urls.find((u) => !u.startsWith('https://') || !hostAllowed(u));
			if (bad) return showError(`This server only clones HTTPS URLs on ${config.allowedHosts.join(', ')}: ${bad}`);
			if (!payload.emails.trim() && !config.personal) return showError('Add the email you commit with, so ghis can find your commits.');
		}

		const button = $('#submit');
		button.disabled = true;
		button.textContent = 'Starting…';
		try {
			const response = await fetch('api/jobs', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(payload),
			});
			const body = await response.json();
			if (!response.ok) throw new Error(body.error || `Server answered ${response.status}`);
			$('#token').value = '';
			follow(body.id);
		} catch (err) {
			showError(err.message);
		} finally {
			button.disabled = false;
			button.textContent = 'Create my story';
		}
	});

	// ------------------------------------------------------------ progress

	function follow(id) {
		const panel = $('#panel');
		panel.innerHTML = `<div class="progress" aria-live="polite">
			<div class="orb" aria-hidden="true"><i></i><i></i><i></i></div>
			<h2>Reading your history</h2>
			<p class="status" id="status">Connecting…</p>
			<ol class="log" id="log"></ol>
		</div>`;
		panel.scrollIntoView({ behavior: 'smooth', block: 'center' });
		let since = 0;
		const started = Date.now();

		const poll = async () => {
			let state;
			try {
				const response = await fetch(`api/jobs/${encodeURIComponent(id)}?since=${since}`, { cache: 'no-store' });
				state = await response.json();
				if (!response.ok) throw new Error(state.error || `Server answered ${response.status}`);
			} catch (err) {
				return fail(err.message);
			}
			const log = $('#log');
			for (const line of state.lines) {
				const li = document.createElement('li');
				li.textContent = line;
				log.append(li);
			}
			since = state.next;
			log.scrollTop = log.scrollHeight;
			const last = log.lastElementChild?.textContent?.trim();
			if (last) $('#status').textContent = last;
			if (state.status === 'done') {
				$('#status').textContent = `Done in ${Math.round((Date.now() - started) / 1000)} s. Opening your story…`;
				setTimeout(() => { location.href = `story?job=${encodeURIComponent(id)}`; }, 600);
			} else if (state.status === 'failed') {
				fail(state.error);
			} else {
				setTimeout(poll, 900);
			}
		};

		const fail = (message) => {
			panel.querySelector('.orb')?.remove();
			panel.querySelector('h2').textContent = 'That did not work';
			$('#status').innerHTML = `${esc(message)}<br><br><button class="btn" type="button" id="retry">Try again</button>`;
			$('#retry').addEventListener('click', () => { location.reload(); });
		};

		poll();
	}

	// ------------------------------------------------------------ upload

	async function upload(file) {
		hideError();
		if (!file) return;
		try {
			const response = await fetch('api/upload', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: file });
			const body = await response.json();
			if (!response.ok) throw new Error(body.error || `Server answered ${response.status}`);
			location.href = `story?job=${encodeURIComponent(body.id)}`;
		} catch (err) {
			showError(err.message);
		}
	}

	const drop = $('#drop');
	$('#file').addEventListener('change', (e) => upload(e.target.files[0]));
	drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('over'); });
	drop.addEventListener('dragleave', () => drop.classList.remove('over'));
	drop.addEventListener('drop', (e) => {
		e.preventDefault();
		drop.classList.remove('over');
		upload(e.dataTransfer.files[0]);
	});

	// ------------------------------------------------------------ boot

	if (location.protocol === 'file:') {
		showError('This page needs the ghis server: run `python3 ghis.py serve` and open http://ghis.localhost:8080.');
		$('#submit').disabled = true;
		return;
	}
	fetch('api/config').then((r) => r.json()).then((c) => {
		config = c;
		if (c.personal) {
			const mode = $('#mode');
			mode.hidden = false;
			mode.innerHTML = '<span class="dot" aria-hidden="true"></span>Running on this computer';
			$('#privacy-text').textContent = `Everything stays on this computer: ghis only talks to the host you pick, keeps your token in memory and saves each story to ${c.outDir}.`;
			$('#folders').placeholder = '~  (your whole home folder)';
			SUB.git = 'or just this computer';
		} else {
			const ttl = c.ttlMinutes ? ` Stories are kept for ${c.ttlMinutes} minutes, then forgotten.` : '';
			$('#privacy-text').textContent += ttl;
		}
		selectProvider(c.providers.some((p) => p.id === c.defaults?.provider) ? c.defaults.provider : 'github');
	}).catch(() => showError('Could not reach the ghis server.'));
})();
