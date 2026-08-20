"""
Finding the page that actually lists the jobs.

**The failure this closes.** Adding a station asked for "the page that lists
the jobs" and then took whatever was pasted at face value. What people
actually have to hand is the company's address — `https://acme.com` — and a
station pointed at a homepage searches a homepage: it finds no postings and
reports that the site had none, which is indistinguishable from the company
not hiring. The user is left guessing which of a dozen URLs the agent wanted,
with no feedback except an empty run.

So the URL is now treated as a starting point rather than an answer. Given a
company's front door, this walks to the listings the way a person would:
follow the "Careers" link, and if that lands on a page that hands off to a
Greenhouse, Lever, Ashby or Workday board, follow that too — those are where
the postings and the application forms really live.

**What it will not do.** Guess. Every step is a link that was actually on the
page, or one of a handful of conventional paths tried and confirmed to
respond. When nothing is found the station is still created with the URL as
given, and the user is told plainly that no listings page was found and what
was tried — an honest dead end beats a confident wrong turn.
"""

import asyncio
import html
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10.0

# Sent so a careers page does not serve the "unsupported browser" variant,
# which carries none of the links being looked for.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# The hosted boards. A link to one of these is the strongest signal there is:
# it is where the postings are, and it is a board the agent can also fill an
# application on.
ATS_HOSTS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "bamboohr.com",
    "recruitee.com",
    "workable.com",
    "teamtailor.com",
    "jobvite.com",
    "icims.com",
    "successfactors.com",
    "taleo.net",
    "personio.de",
    "join.com",
    "breezy.hr",
    "pinpointhq.com",
)

# Paths that mean "this is already the listings page". Checked against the URL
# the user gave, so a correct URL costs nothing.
LISTINGS_PATH = re.compile(
    r"/(careers?|jobs?|vacanc(y|ies)|openings?|opportunit(y|ies)"
    r"|positions?|join-?us|work-?with-?us|hiring|recruitment)\b",
    re.IGNORECASE,
)

# What a link to the careers section looks like, in its text or its href.
CAREERS_LINK = re.compile(
    r"careers?|jobs?|vacanc|opening|opportunit|join[\s\-_]?us"
    r"|work[\s\-_]?(with|for|at)[\s\-_]?us|we.re hiring|current openings",
    re.IGNORECASE,
)

# Tried in order when a page offers no careers link of its own. Ordinary
# convention, not guesswork: each one is fetched and has to respond.
CONVENTIONAL_PATHS = (
    "/careers",
    "/jobs",
    "/careers/jobs",
    "/company/careers",
    "/about/careers",
    "/en/careers",
    "/join-us",
    "/work-with-us",
)

# How many postings-shaped links a page needs before it is called a listings
# page. One is a link to a blog post about hiring; several is a board.
MIN_POSTING_LINKS = 2

# Links on a page that look like individual postings.
POSTING_HREF = re.compile(
    r"/(jobs?|careers?|opening|position|vacanc)[/\-][\w\-/%.]+", re.IGNORECASE
)


@dataclass
class Discovery:
    """Where the listings were found, and how."""

    url: str
    # How the page was reached, in the user's terms. Shown when the station is
    # created so the user can see whether it went somewhere sensible.
    how: str = ""
    # True when this is the URL the user gave, unchanged.
    as_given: bool = True
    # Every page that was looked at, so a failure can say what was tried.
    tried: List[str] = field(default_factory=list)
    ats: Optional[str] = None

    def describe(self) -> str:
        """One sentence about what was found."""
        if self.as_given:
            return "Used the URL as given."

        return f"Found the listings at {self.url} — {self.how}."


async def find_listings_page(url: str) -> Discovery:
    """
    Walk from whatever the user pasted to the page that lists the jobs.

    Args:
        url: Anything from a bare company domain to an exact board URL

    Returns:
        A Discovery. Its `url` is always usable — it falls back to the URL
        given rather than failing, so adding a station never depends on this
        succeeding.
    """
    url = _normalise(url)
    discovery = Discovery(url=url, tried=[url])

    # A URL with search placeholders in it was written deliberately; the user
    # knows exactly which page they mean.
    if "{" in url:
        return discovery

    host = (urlparse(url).netloc or "").lower()

    # Already a hosted board — nothing to find.
    if any(host.endswith(ats) for ats in ATS_HOSTS):
        discovery.ats = next(ats for ats in ATS_HOSTS if host.endswith(ats))
        return discovery

    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is a hard dependency
        logger.info("httpx is unavailable — using the URL as given")
        return discovery

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        landing = await _get(client, url)

        # An exact board URL, or a careers page that already lists postings.
        if landing and LISTINGS_PATH.search(urlparse(url).path or ""):
            board = _first_ats_link(landing, url)

            if board:
                return _found(
                    discovery, board,
                    "this careers page hands off to a hosted job board",
                )

            if _counts_as_listings(landing):
                return discovery

        # Follow the site's own careers link, then look again from there.
        for candidate in _careers_links(landing or "", url):
            discovery.tried.append(candidate)
            page = await _get(client, candidate)

            if not page:
                continue

            board = _first_ats_link(page, candidate)

            if board:
                return _found(
                    discovery, board,
                    "followed this site's careers link to its job board",
                )

            if _counts_as_listings(page):
                return _found(
                    discovery, candidate, "followed this site's own careers link"
                )

        # Nothing linked. Try the conventional paths.
        for path in CONVENTIONAL_PATHS:
            candidate = urljoin(url, path)

            if candidate in discovery.tried:
                continue

            discovery.tried.append(candidate)
            page = await _get(client, candidate)

            if not page:
                continue

            board = _first_ats_link(page, candidate)

            if board:
                return _found(
                    discovery, board, f"{path} leads to this company's job board"
                )

            if _counts_as_listings(page):
                return _found(discovery, candidate, f"{path} lists the postings")

    return discovery


def _found(discovery: Discovery, url: str, how: str) -> Discovery:
    """Record a successful walk."""
    discovery.url = url
    discovery.how = how
    discovery.as_given = False

    host = (urlparse(url).netloc or "").lower()
    discovery.ats = next((ats for ats in ATS_HOSTS if host.endswith(ats)), None)

    logger.info(f"Careers discovery: {url} ({how})")

    return discovery


async def _get(client, url: str) -> Optional[str]:
    """
    Fetch a page, returning None for anything that isn't readable HTML.

    Args:
        client: An httpx.AsyncClient
        url: The page to fetch

    Returns:
        The HTML, or None
    """
    try:
        response = await client.get(url)
    except Exception as e:
        logger.debug(f"Could not read {url}: {type(e).__name__}: {e}")
        return None

    if response.status_code >= 400:
        return None

    if "html" not in response.headers.get("content-type", "").lower():
        return None

    return response.text


def _normalise(url: str) -> str:
    """
    Make a pasted address into something fetchable.

    Args:
        url: What the user typed, possibly without a scheme

    Returns:
        An absolute URL
    """
    url = (url or "").strip()

    if url and not re.match(r"^https?://", url, re.IGNORECASE):
        url = f"https://{url}"

    return url


def _links(source: str, base: str) -> List[Tuple[str, str]]:
    """
    Every link on a page, as (absolute href, link text).

    Parsed with a regex rather than a DOM: this runs inside an API request on
    pages that are often megabytes of marketing markup, and only the anchors
    matter.

    Args:
        source: The page source
        base: The URL it was fetched from, for resolving relative hrefs

    Returns:
        (url, text) pairs
    """
    found: List[Tuple[str, str]] = []

    for match in re.finditer(
        r"<a\b[^>]*?href\s*=\s*[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>",
        source,
        re.IGNORECASE | re.DOTALL,
    ):
        # An href in HTML is entity-encoded: a search URL is written
        # `?q=remote&amp;l=london`, and storing it that way turns the second
        # parameter into one called "amp;l". The station then searches with
        # half its query silently dropped.
        href = html.unescape(match.group(1).strip())

        if href.lower().startswith(("javascript:", "mailto:", "tel:")):
            continue

        text = html.unescape(re.sub(r"<[^>]+>", " ", match.group(2)))
        text = re.sub(r"\s+", " ", text).strip()

        found.append((urljoin(base, href), text))

    return found


def _first_ats_link(source: str, base: str) -> Optional[str]:
    """
    The first link to a hosted job board on this page.

    Args:
        source: The page source
        base: The URL it was fetched from

    Returns:
        The board URL, or None
    """
    for url, _ in _links(source, base):
        host = (urlparse(url).netloc or "").lower()

        if any(host.endswith(ats) for ats in ATS_HOSTS):
            return url

    # Boards are often embedded rather than linked.
    for match in re.finditer(
        r"<iframe\b[^>]*?src\s*=\s*[\"']([^\"']+)[\"']", source, re.IGNORECASE
    ):
        url = urljoin(base, match.group(1))
        host = (urlparse(url).netloc or "").lower()

        if any(host.endswith(ats) for ats in ATS_HOSTS):
            return url

    return None


def _careers_links(source: str, base: str) -> List[str]:
    """
    Links on a homepage that lead towards the jobs, best first.

    Args:
        source: The page source
        base: The URL it was fetched from

    Returns:
        Candidate URLs, deduplicated
    """
    base_host = (urlparse(base).netloc or "").lower()
    scored: List[Tuple[int, str]] = []

    for url, text in _links(source, base):
        parsed = urlparse(url)

        # Off-site links are only worth following when they go to a board.
        host = (parsed.netloc or "").lower()

        if host != base_host and not any(host.endswith(a) for a in ATS_HOSTS):
            continue

        matches_text = bool(text and CAREERS_LINK.search(text))
        matches_path = bool(LISTINGS_PATH.search(parsed.path or ""))

        if not (matches_text or matches_path):
            continue

        # A link saying "Careers" and pointing at /careers is the one the site
        # itself considers the way in.
        scored.append((int(matches_text) + int(matches_path), url))

    ordered = [url for _, url in sorted(scored, key=lambda pair: -pair[0])]

    return list(dict.fromkeys(ordered))[:4]


def _counts_as_listings(source: str) -> bool:
    """
    Whether a page is showing postings rather than talking about working here.

    A careers landing page full of culture copy and one "see our openings"
    button is not a listings page, and a station pointed at one finds nothing.

    Args:
        source: The page source

    Returns:
        True when the page carries several posting-shaped links
    """
    hrefs = {
        match.group(0).lower()
        for match in POSTING_HREF.finditer(source)
    }

    return len(hrefs) >= MIN_POSTING_LINKS
