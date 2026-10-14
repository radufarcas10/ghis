// Applies the saved theme before first paint; any [data-theme-toggle] flips and remembers it.
// The switch is a soft crossfade: a View Transition where the browser has them (timed in
// CSS), a colour transition elsewhere, and instant for reduced-motion users.
(() => {
	const root = document.documentElement;
	try {
		const saved = localStorage.getItem('ghis:theme');
		if (saved === 'light' || saved === 'dark') root.dataset.theme = saved;
	} catch { /* storage blocked: follow the OS */ }

	const isDark = () => (root.dataset.theme
		? root.dataset.theme === 'dark'
		: matchMedia('(prefers-color-scheme: dark)').matches);

	function apply(next) {
		root.dataset.theme = next;
		try { localStorage.setItem('ghis:theme', next); } catch { /* ignore */ }
	}

	function toggle() {
		const next = isDark() ? 'light' : 'dark';
		if (matchMedia('(prefers-reduced-motion: reduce)').matches) {
			apply(next);
			return;
		}
		if (document.startViewTransition) {
			document.startViewTransition(() => apply(next));
			return;
		}
		root.classList.add('theme-fade');
		apply(next);
		setTimeout(() => root.classList.remove('theme-fade'), 700);
	}

	addEventListener('DOMContentLoaded', () => {
		for (const button of document.querySelectorAll('[data-theme-toggle]')) {
			button.addEventListener('click', toggle);
		}
	});
})();
