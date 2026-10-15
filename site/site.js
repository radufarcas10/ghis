// ghis website: points every repository link at wherever the site is published, copies
// code samples, and reveals sections on scroll.
(() => {
	'use strict';

	// Links in the HTML point at the main repository. A fork published on its own
	// <owner>.github.io/<repo>/ points them at itself instead, with no editing.
	const pages = location.hostname.match(/^([\w-]+)\.github\.io$/i);
	const repoName = pages && location.pathname.split('/').filter(Boolean)[0];
	if (pages && !(pages[1].toLowerCase() === 'radufarcas10' && repoName === 'ghis')) {
		const repo = `https://github.com/${pages[1]}/${repoName || `${pages[1]}.github.io`}`;
		for (const a of document.querySelectorAll('[data-repo-link]')) a.href = repo;
		for (const a of document.querySelectorAll('[data-repo-file]')) a.href = `${repo}/blob/main/${a.dataset.repoFile}`;
		for (const el of document.querySelectorAll('[data-repo-clone]')) el.textContent = `${repo}.git`;
		for (const el of document.querySelectorAll('[data-repo-pip]')) el.textContent = `git+${repo}`;
	}

	for (const button of document.querySelectorAll('[data-copy]')) {
		button.addEventListener('click', async () => {
			const text = document.getElementById(button.dataset.copy).textContent.replace(/^\$ /gm, '');
			try {
				await navigator.clipboard.writeText(text);
				button.textContent = 'Copied';
			} catch {
				button.textContent = 'Select and copy';
			}
			setTimeout(() => { button.textContent = 'Copy'; }, 1600);
		});
	}

	// The embedded demo: one story per provider, each its own route (demo/, demo/gitlab/, ...).
	const frame = document.getElementById('demo-frame');
	// On phones the frame is a non-interactive preview: no scrollbar of its own.
	if (matchMedia('(max-width: 640px)').matches) {
		frame.setAttribute('scrolling', 'no');
		frame.src = frame.getAttribute('src');
	}
	for (const button of document.querySelectorAll('[data-demo]')) {
		button.addEventListener('click', () => {
			frame.src = button.dataset.demo;
			document.getElementById('demo-open').href = button.dataset.demo;
			document.getElementById('demo-tap').href = button.dataset.demo;
			for (const other of document.querySelectorAll('[data-demo]')) {
				other.setAttribute('aria-pressed', String(other === button));
			}
		});
	}

	// FAQ: an accordion whose answers slide open and closed, one open at a time. <details>
	// itself can only snap, so the click is taken over: open first and grow, or shrink first
	// and then close. (Not the native `name` grouping: it would snap the others shut.)
	const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
	const timing = { duration: 340, easing: 'cubic-bezier(0.2, 0.8, 0.2, 1)' };
	const faqs = [...document.querySelectorAll('.faq details')].map((details) => ({
		details, answer: details.querySelector('.answer'), closing: false,
	}));
	const animated = !reduceMotion && faqs.length && faqs[0].answer.animate;

	function expand(item) {
		// Mid-close, grow back from wherever it got to.
		const from = item.details.open ? item.answer.getBoundingClientRect().height : 0;
		item.answer.getAnimations().forEach((running) => running.cancel());
		item.closing = false;
		item.details.open = true;
		if (!animated) return;
		item.answer.animate([
			{ height: `${from}px`, opacity: from ? 1 : 0 },
			{ height: `${item.answer.scrollHeight}px`, opacity: 1 },
		], timing);
	}

	function collapse(item) {
		if (!animated) {
			item.details.open = false;
			return;
		}
		const from = item.answer.getBoundingClientRect().height;
		item.answer.getAnimations().forEach((running) => running.cancel());
		item.closing = true;
		const shrink = item.answer.animate([{ height: `${from}px`, opacity: 1 }, { height: '0px', opacity: 0 }], timing);
		shrink.onfinish = () => { item.closing = false; item.details.open = false; };
	}

	for (const item of faqs) {
		item.details.querySelector('summary').addEventListener('click', (event) => {
			event.preventDefault();
			if (item.details.open && !item.closing) {
				collapse(item);
				return;
			}
			for (const other of faqs) if (other !== item && other.details.open && !other.closing) collapse(other);
			expand(item);
		});
	}

	// Links to a question (#match) open its answer.
	const openFromHash = () => {
		const target = location.hash && document.getElementById(location.hash.slice(1));
		const item = faqs.find((f) => f.details === target);
		if (item && !item.details.open) {
			for (const other of faqs) if (other !== item && other.details.open) collapse(other);
			expand(item);
		}
	};
	addEventListener('hashchange', openFromHash);
	openFromHash();

	const sections = document.querySelectorAll('.reveal');
	if (matchMedia('(prefers-reduced-motion: reduce)').matches || !('IntersectionObserver' in window)) {
		sections.forEach((el) => el.classList.add('in'));
	} else {
		const observer = new IntersectionObserver((entries) => {
			for (const entry of entries) {
				if (!entry.isIntersecting) continue;
				entry.target.classList.add('in');
				observer.unobserve(entry.target);
			}
		}, { threshold: 0.12 });
		sections.forEach((el) => observer.observe(el));
	}
})();
