"""Generated sample data, for trying ghis without an account."""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .common import SCHEMA_VERSION, bump
from .git import DEFAULT_IGNORE
from .providers import PROVIDERS

DEMO_HOSTS = {"github": "github.com", "gitlab": "gitlab.com", "gitea": "codeberg.org"}


def make_demo(seed: int, provider: str) -> dict[str, Any]:
    rng = random.Random(seed)
    today = date.today()
    start = today - timedelta(days=640)
    repos = [
        ("acme/storefront-ui", 0.34, {"Vue": 5, "TypeScript": 4, "SCSS": 2, "JSON": 1}),
        ("acme/orders-api", 0.2, {"PHP": 6, "YAML": 1, "SQL": 1}),
        ("acme/design-tokens", 0.1, {"TypeScript": 3, "CSS": 3, "JSON": 2}),
        ("acme/search-service", 0.12, {"Python": 6, "YAML": 1, "Shell": 1}),
        ("acme/dev-env", 0.09, {"Python": 4, "Shell": 3, "Docker": 2, "YAML": 2}),
        ("acme/docs", 0.06, {"Markdown": 6}),
        ("platform/ci-templates", 0.05, {"YAML": 5, "Shell": 2}),
        ("platform/terraform-modules", 0.04, {"Terraform": 5, "Markdown": 1}),
    ]
    weights = [w for _, w, _ in repos]
    verbs = ["Fix", "Add", "Refactor", "Update", "Remove", "Migrate", "Rename", "Extract", "Test"]
    things = [
        "order list pagination",
        "login redirect",
        "basket totals",
        "search facets",
        "ESLint config",
        "checkout modal",
        "price formatting",
        "feature flag",
        "SDK client",
        "error handling",
        "date picker",
        "CI cache",
        "visual tests",
        "locale switcher",
    ]
    teammates = ["maya", "tom", "lena", "ion", "sara", "kenji"]
    hour_weights = [1, 0, 0, 0, 0, 0, 1, 2, 5, 9, 12, 12, 6, 7, 11, 12, 10, 8, 4, 3, 3, 4, 3, 2]
    commits: list[dict[str, Any]] = []
    activity: dict[str, dict[str, int]] = {}
    requests: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    day = start
    while day <= today:
        weekend = day.weekday() >= 5
        rate = 0.0
        if rng.random() > 0.04 and (not weekend or rng.random() < 0.15):
            rate = 0.6 if weekend else 4.2
        for _ in range(int(rng.expovariate(1 / rate)) if rate else 0):
            r = rng.choices(range(len(repos)), weights)[0]
            hour = rng.choices(range(24), hour_weights)[0]
            offset = timezone(timedelta(hours=3 if 3 < day.month < 11 else 2))
            when = datetime(day.year, day.month, day.day, hour, rng.randrange(60), tzinfo=offset)
            size = int(rng.lognormvariate(3.2, 1.2))
            adds = max(1, int(size * rng.uniform(0.4, 0.9)))
            dels = max(0, size - adds)
            langs = repos[r][2]
            lang = rng.choices(list(langs), list(langs.values()))[0]
            ticket = f"APP-{rng.randrange(100, 900)} " if rng.random() < 0.6 else ""
            # About one commit in twelve was pair-programmed and authored by a teammate.
            co_authored = rng.random() < 0.08
            commit = {
                "h": f"{rng.getrandbits(48):012x}",
                "t": when.isoformat(),
                "e": f"{rng.choice(teammates)}@example.com" if co_authored else "ada@example.com",
                "a": adds,
                "d": dels,
                "f": rng.randint(1, 9),
                "s": f"{ticket}{rng.choice(verbs)} {rng.choice(things)}",
                "m": 0,
                "l": {lang: adds + dels},
                "r": r,
            }
            if co_authored:
                commit["co"] = 1
            commits.append(commit)
        if rate:
            for action, lam in (("commented", 2.2), ("approved", 0.7)):
                for _ in range(int(rng.expovariate(1 / lam))):
                    hour = rng.choices(range(24), hour_weights)[0]
                    bump(activity, f"{day.isoformat()}T{hour:02d}:30:00+03:00", action)
            if rng.random() < 0.35:
                created = datetime(
                    day.year, day.month, day.day, rng.randint(9, 17), tzinfo=timezone.utc
                )
                end = created + timedelta(hours=rng.lognormvariate(2.8, 1.3))
                state = rng.choices(["merged", "closed", "opened"], [0.86, 0.09, 0.05])[0]
                if end > now:
                    state = "opened"
                requests.append(
                    {
                        "project": rng.choices(repos, weights)[0][0],
                        "number": len(requests) + 100,
                        "title": f"APP-{rng.randrange(100, 900)} {rng.choice(verbs)} "
                        f"{rng.choice(things)}",
                        "state": state,
                        "createdAt": created.isoformat(),
                        "mergedAt": end.isoformat() if state == "merged" else None,
                        "closedAt": end.isoformat() if state == "closed" else None,
                        "notes": int(rng.expovariate(1 / 5)),
                        "author": "ada",
                        "reviewers": rng.sample(teammates, rng.choice([1, 1, 2])),
                        "url": None,
                    }
                )
                bump(activity, created.isoformat(), "request_opened")
            if rng.random() < 0.5:
                reviews.append(
                    {
                        "project": rng.choice(repos)[0],
                        "number": len(reviews) + 500,
                        "title": f"{rng.choice(verbs)} {rng.choice(things)}",
                        "state": "merged",
                        "createdAt": f"{day.isoformat()}T10:00:00Z",
                        "mergedAt": None,
                        "closedAt": None,
                        "notes": 0,
                        "author": rng.choices(teammates, [6, 4, 3, 2, 1, 1])[0],
                        "url": None,
                    }
                )
        day += timedelta(days=1)

    # A few bulk changes, the kind that dwarf real work in line counts: an import, a vendored
    # library and a formatter run.
    for days_ago, repo, subject, lang, size in (
        (600, 1, "Import legacy orders module", "PHP", 48_000),
        (380, 0, "Vendor charting library", "JavaScript", 31_000),
        (150, 0, "Run prettier on the whole codebase", "Vue", 22_000),
    ):
        moment = datetime.combine(today - timedelta(days=days_ago), datetime.min.time())
        when = moment.replace(hour=15, tzinfo=timezone(timedelta(hours=3)))
        commits.append(
            {
                "h": f"{rng.getrandbits(48):012x}",
                "t": when.isoformat(),
                "e": "ada@example.com",
                "a": size,
                "d": size // 3 if "prettier" in subject else 0,
                "f": 400,
                "s": subject,
                "m": 0,
                "l": {lang: size},
                "r": repo,
            }
        )

    commits.sort(key=lambda c: datetime.fromisoformat(c["t"]))
    return {
        "schema": SCHEMA_VERSION,
        "generatedAt": now.isoformat(timespec="seconds"),
        "source": {
            "provider": provider,
            "label": PROVIDERS[provider]["label"],
            "host": DEMO_HOSTS[provider],
            "has": ["requests", "reviews", "approvals", "comments"],
            "localRepos": 0,
            "clonedRepos": len(repos),
            "emails": ["ada@example.com"],
            "linesIgnored": list(DEFAULT_IGNORE),
            "demo": True,
        },
        "user": {
            "name": "Ada Example",
            "username": "ada",
            "createdAt": f"{start.isoformat()}T09:00:00Z",
            "webUrl": None,
        },
        "projects": [
            {"path": p, "name": p.split("/")[-1], "webUrl": None, "source": "api"}
            for p, _, _ in repos
        ],
        "commits": commits,
        "requests": requests,
        "reviews": reviews,
        "activity": activity,
        "coverage": {
            "repos": [
                {
                    "path": p,
                    "via": "clone",
                    "commits": sum(1 for c in commits if c["r"] == i),
                    "fetchedAt": now.isoformat(timespec="seconds"),
                }
                for i, (p, _, _) in enumerate(repos)
            ],
            "scanned": len(repos),
            "skipped": [],
            "limits": [],
            "excludedEmails": [],
        },
        "warnings": ["This is generated sample data."],
    }
