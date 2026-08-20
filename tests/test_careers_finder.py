"""
Walking from a company's front door to the page that lists its jobs.

The failure being fixed: adding a station took the pasted URL at face value,
so `https://acme.com` produced a station that searched a homepage, found
nothing, and reported that the company had no openings.
"""

import pytest

from job_agent.services.careers_finder import (
    Discovery,
    _careers_links,
    _counts_as_listings,
    _first_ats_link,
    _normalise,
    find_listings_page,
)


HOMEPAGE = """
<html><body>
  <nav>
    <a href="/product">Product</a>
    <a href="/pricing">Pricing</a>
    <a href="/careers">Careers</a>
    <a href="https://twitter.com/acme">Follow us</a>
  </nav>
</body></html>
"""

CULTURE_PAGE = """
<html><body>
  <h1>Life at Acme</h1>
  <p>We are a remote-first team.</p>
  <a href="https://job-boards.greenhouse.io/acme">See our open roles</a>
</body></html>
"""

LISTINGS_PAGE = """
<html><body>
  <a href="/careers/senior-backend-engineer">Senior Backend Engineer</a>
  <a href="/careers/support-engineer">Support Engineer</a>
  <a href="/careers/product-designer">Product Designer</a>
</body></html>
"""


class TestNormalise:
    """A bare domain is the most natural thing to paste."""

    @pytest.mark.parametrize(
        "given,expected",
        [
            ("acme.com", "https://acme.com"),
            ("https://acme.com", "https://acme.com"),
            ("http://acme.com", "http://acme.com"),
            ("  acme.com  ", "https://acme.com"),
        ],
    )
    def test_a_scheme_is_supplied(self, given, expected):
        assert _normalise(given) == expected


class TestReadingAPage:
    """What the walk looks for, without any network."""

    def test_a_hosted_board_link_is_found(self):
        assert (
            _first_ats_link(CULTURE_PAGE, "https://acme.com/careers")
            == "https://job-boards.greenhouse.io/acme"
        )

    def test_an_embedded_board_is_found(self):
        """Boards are often iframed rather than linked."""
        html = '<iframe src="https://acme.teamtailor.com/jobs"></iframe>'

        assert (
            _first_ats_link(html, "https://acme.com/careers")
            == "https://acme.teamtailor.com/jobs"
        )

    def test_the_careers_link_is_picked_out_of_a_nav(self):
        links = _careers_links(HOMEPAGE, "https://acme.com")

        assert "https://acme.com/careers" in links

    def test_unrelated_offsite_links_are_ignored(self):
        """Following every outbound link would walk off the company's site."""
        links = _careers_links(HOMEPAGE, "https://acme.com")

        assert not any("twitter.com" in link for link in links)

    def test_a_page_of_postings_is_recognised(self):
        assert _counts_as_listings(LISTINGS_PAGE)

    def test_a_culture_page_is_not_mistaken_for_listings(self):
        """
        One "see our openings" button is not a listings page, and a station
        pointed at one finds nothing.
        """
        assert not _counts_as_listings(CULTURE_PAGE)


class TestShortCircuits:
    """Cases that must not touch the network at all."""

    async def test_a_hosted_board_url_is_used_as_given(self):
        result = await find_listings_page("https://job-boards.greenhouse.io/gitlab")

        assert result.as_given
        assert result.ats == "greenhouse.io"
        assert result.url == "https://job-boards.greenhouse.io/gitlab"

    async def test_a_templated_url_is_left_alone(self):
        """Placeholders mean the user knows exactly which page they meant."""
        url = "https://acme.com/careers?q={query}&l={location}"
        result = await find_listings_page(url)

        assert result.as_given
        assert result.url == url

    async def test_a_lever_url_is_recognised(self):
        result = await find_listings_page("https://jobs.lever.co/acme")

        assert result.ats == "lever.co"


class TestDescribe:
    """What the user is told about where their station points."""

    def test_an_unchanged_url_says_so(self):
        assert Discovery(url="https://acme.com/jobs").describe() == (
            "Used the URL as given."
        )

    def test_a_walk_names_the_page_it_landed_on(self):
        message = Discovery(
            url="https://job-boards.greenhouse.io/acme",
            how="followed this site's careers link to its job board",
            as_given=False,
        ).describe()

        assert "job-boards.greenhouse.io/acme" in message
        assert "careers link" in message


TestShortCircuits.pytestmark = pytest.mark.asyncio
