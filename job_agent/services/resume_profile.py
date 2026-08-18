"""
Reading a resume for what it says the candidate should be searching for.

**The problem this solves.** The search profile and the master resume were
edited independently, so nothing kept them honest with each other. A
Full-Stack Developer's resume sat next to a profile still searching for
support and data-leadership roles, and the wire filled with VP of Data,
Director of Support and Ruby backend postings — every one of which the resume
answers at single-digit fit. The user then sees a fit analyser that "does not
work", when what is actually broken is upstream: the agent was never looking
for jobs this person could do.

Fit is decided long before any document is tailored. Tailoring reorders and
rewords; it cannot make a React developer into a VP of Data, and it is built
not to try. So the only honest way to a high fit score is to search for the
right jobs, which means deriving the search from the resume rather than from
whatever was typed into a form months ago.

**What this reads.** Titles the candidate has actually held, the technologies
their skills section names, and the seniority their history supports. Nothing
is inferred beyond the document: a resume that never mentions Kubernetes will
not produce a search for Kubernetes roles.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional, Set

logger = logging.getLogger(__name__)

# How many derived titles and keywords are worth searching on. Past a handful
# a search stops discriminating and returns the whole board.
MAX_TITLES = 8
MAX_KEYWORDS = 20

# Words that appear in a job title without saying what the job is.
TITLE_NOISE = re.compile(
    r"\b(intern|internship|contract|contractor|freelance|part.time|full.time"
    r"|remote|hybrid|onsite|on.site|temporary|permanent|volunteer)\b",
    re.IGNORECASE,
)

# The shape of an employment line: "React Developer | SimpleX Technology Sep
# 2024 – Feb 2026" or "Front-End Developer, ADMAXIM, 2023-2024". The title is
# everything before the first separator.
#
# A bare hyphen is deliberately not a separator. Allowing one made the
# non-greedy title stop inside "Front-End Developer" and return "Front", which
# is one word and was then discarded — so the whole front-end role, the most
# relevant one on the resume, was invisible to the search.
EMPLOYMENT_LINE = re.compile(
    r"^(?P<title>[A-Z][^|,\n]{2,60}?)\s*(?:\||,|\s[–—]\s)\s*\S",
)

# "Actively seeking remote and contract-based Full-Stack Developer
# engagements" — the candidate saying, in their own words, what they want
# next. It outranks anything inferred from history: a lecturer retraining into
# development should be searched for development roles.
STATED_TARGET = re.compile(
    r"\b(?:seeking|looking for|open to|available for|pursuing|targeting"
    r"|interested in)\b[^.]{0,80}?"
    r"(?P<title>[A-Z][\w+#.]*(?:[\- ][A-Za-z][\w+#.]*){0,3}?\s"
    r"(?:developer|engineer|designer|manager|analyst|architect|consultant"
    r"|specialist|administrator|scientist|lead|director))",
    re.IGNORECASE,
)

# Section headings whose content is job titles rather than prose.
EXPERIENCE_HEADINGS = re.compile(
    r"^(professional\s+)?(experience|employment|work history|career)\b",
    re.IGNORECASE,
)

SKILLS_HEADINGS = re.compile(
    r"^(technical\s+)?(skills|technologies|tech stack|competenc)",
    re.IGNORECASE,
)

# Seniority as a resume states it, most senior first so the first match wins.
SENIORITY_MARKERS = (
    ("executive", r"\b(chief|c[tef]o|vice president|\bvp\b|head of)\b"),
    ("staff", r"\b(staff|principal|distinguished|architect)\b"),
    ("senior", r"\b(senior|sr\.?|lead|manager|supervisor)\b"),
    ("mid", r"\b(mid.level|intermediate|associate)\b"),
    ("entry", r"\b(junior|jr\.?|graduate|trainee|entry.level)\b"),
)

# A skills line is "Frontend: React, Vite, Tailwind CSS" — the label is a
# category, the values are the searchable terms.
SKILL_SPLIT = re.compile(r"[,;/|]|\band\b")

# Terms too generic to search on even when a skills section lists them.
GENERIC_SKILLS = frozenset({
    "communication", "teamwork", "leadership", "problem solving", "agile",
    "scrum", "management", "collaboration", "documentation", "testing",
    "development", "design", "software", "web", "data", "cloud", "tools",
    "frameworks", "languages", "databases", "other", "various", "etc",
})


@dataclass
class ResumeProfile:
    """What a resume says it should be searched against."""

    titles: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    seniority: Optional[str] = None
    # Every title found, including ones that did not make the search list.
    # Kept so the desk can show its working.
    all_titles: List[str] = field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        """Whether enough was found to search on."""
        return bool(self.titles)

    def describe(self) -> str:
        """One sentence for the dashboard."""
        if not self.is_usable:
            return "Could not read any job titles from this resume."

        return (
            f"Searching for {', '.join(self.titles[:3])}"
            + (f" and {len(self.titles) - 3} more" if len(self.titles) > 3 else "")
            + (f", {self.seniority} level" if self.seniority else "")
            + "."
        )

    def to_dict(self) -> dict:
        """Serialize for the dashboard."""
        return {
            "titles": self.titles,
            "keywords": self.keywords,
            "seniority": self.seniority,
            "all_titles": self.all_titles,
            "usable": self.is_usable,
            "message": self.describe(),
        }


def read_resume(master_text: str) -> ResumeProfile:
    """
    Derive what to search for from the resume itself.

    Args:
        master_text: The master resume's text

    Returns:
        A ResumeProfile. Empty rather than guessed when the resume has no
        recognisable experience section — searching on a guess is how the wire
        filled with jobs the candidate could not do.
    """
    lines = [line.strip() for line in (master_text or "").split("\n")]

    stated = _stated_targets(master_text or "")
    held = _titles(lines)
    keywords = _keywords(lines)

    # What they say they want comes first, then what they have done. A resume
    # whose summary asks for development work and whose history includes a
    # lecturing post should be searched for development.
    titles = _rank_titles(stated + held)
    seniority = _seniority(titles, master_text or "")

    profile = ResumeProfile(
        titles=titles[:MAX_TITLES],
        keywords=keywords[:MAX_KEYWORDS],
        seniority=seniority,
        all_titles=_rank_titles(held),
    )

    logger.info(
        f"Read resume: {len(profile.titles)} title(s) "
        f"({', '.join(profile.titles[:3])}), {len(profile.keywords)} keyword(s), "
        f"seniority {profile.seniority or 'unstated'}"
    )

    return profile


def _stated_targets(master_text: str) -> List[str]:
    """
    The role the candidate says they are looking for.

    Args:
        master_text: The whole resume

    Returns:
        Stated target titles, in the order they appear
    """
    found: List[str] = []

    for match in STATED_TARGET.finditer(master_text or ""):
        title = TITLE_NOISE.sub("", match.group("title")).strip(" ,-–—|")

        # "contract-based Full-Stack Developer" starts matching inside the
        # hyphenated word and yields "based Full-Stack Developer". A title
        # begins at a capitalised word; drop whatever precedes one.
        title = re.sub(r"^(?:[a-z][\w+#.]*[\s\-]+)+", "", title).strip()

        # A summary hard-wraps, so a stated target can straddle a line break
        # and arrive as "Full-Stack\nDeveloper". A search term with a newline
        # in it matches nothing.
        title = " ".join(title.split())

        if 2 <= len(title.split()) <= 6 and title not in found:
            found.append(title)

    return found


def _titles(lines: List[str]) -> List[str]:
    """
    The job titles this person has actually held.

    Only the experience section is read. A resume's summary says "seeking a
    Full-Stack Developer role" and its projects say "BotCraft Ai" — neither is
    a title held, and treating them as one produced searches for a product
    name.

    Args:
        lines: The resume's lines

    Returns:
        Titles in the order the resume gives them, deduplicated
    """
    found: List[str] = []
    inside = False

    for line in lines:
        if not line:
            continue

        if EXPERIENCE_HEADINGS.match(line):
            inside = True
            continue

        # Any other heading ends the section. A heading here is a short line
        # in title case with no sentence punctuation.
        if inside and _looks_like_heading(line):
            if not EXPERIENCE_HEADINGS.match(line):
                inside = False
            continue

        if not inside:
            continue

        match = EMPLOYMENT_LINE.match(line)

        if not match:
            continue

        title = " ".join(
            TITLE_NOISE.sub("", match.group("title")).strip(" ,-–—|").split()
        )

        if 2 <= len(title.split()) <= 6 and title not in found:
            found.append(title)

    return found


def _looks_like_heading(line: str) -> bool:
    """
    Whether a line is a section heading rather than content.

    Args:
        line: A resume line

    Returns:
        True for a short, unpunctuated, upper-or-title-case line
    """
    if len(line.split()) > 4 or line.endswith((".", ",", ";")):
        return False

    if line.startswith(("-", "•", "*", "·")):
        return False

    letters = [c for c in line if c.isalpha()]

    if not letters:
        return False

    # ALL CAPS, or Title Case With No Lower-case Openers.
    return all(c.isupper() for c in letters) or line[0].isupper() and "|" not in line


def _keywords(lines: List[str]) -> List[str]:
    """
    The technologies the resume's skills section names.

    Args:
        lines: The resume's lines

    Returns:
        Searchable skill terms, most specific first
    """
    found: List[str] = []
    inside = False

    for line in lines:
        if not line:
            continue

        if SKILLS_HEADINGS.match(line):
            inside = True
            continue

        # A labelled skills line — "Backend: Node.js, Express" — is short,
        # capitalised and unpunctuated, so it reads as a heading. Treating it
        # as one ended the section after the first line and lost every skill
        # but the first category's.
        if inside and ":" not in line and _looks_like_heading(line):
            break

        if not inside:
            continue

        # "Frontend: React, Vite, Tailwind CSS" — drop the category label.
        values = line.split(":", 1)[-1] if ":" in line else line

        for piece in SKILL_SPLIT.split(values):
            term = piece.strip(" .•*·-")

            if not term or len(term) < 2 or len(term.split()) > 3:
                continue

            if term.lower() in GENERIC_SKILLS:
                continue

            if term not in found:
                found.append(term)

    return found


def _seniority(titles: List[str], master_text: str) -> Optional[str]:
    """
    The seniority the resume's own titles claim.

    Read from titles rather than from years of experience: a resume that says
    "Senior Engineer" is applying at that level, and counting years would
    promote someone the market would not.

    Args:
        titles: Titles held
        master_text: The whole resume, as a fallback

    Returns:
        A seniority band, or None when nothing states one
    """
    haystack = " ".join(titles) or master_text[:2000]

    for band, pattern in SENIORITY_MARKERS:
        if re.search(pattern, haystack, re.IGNORECASE):
            return band

    return None


def _rank_titles(titles: List[str]) -> List[str]:
    """
    Order titles by how well they represent the candidate now.

    The most recent role leads a resume, and it is the one the next job should
    look like. Later entries are earlier history and matter less.

    Args:
        titles: Titles in resume order

    Returns:
        The same titles, most representative first
    """
    return list(dict.fromkeys(titles))


def matches_titles(job_title: str, profile: ResumeProfile) -> bool:
    """
    Whether a posting's title is one this resume is looking for.

    Args:
        job_title: The posting's title
        profile: What the resume asks for

    Returns:
        True when the posting shares a meaningful word with a derived title
    """
    if not profile.titles:
        return True  # Nothing derived, so nothing to filter on

    wanted: Set[str] = set()

    for title in profile.titles:
        wanted |= {
            word.lower() for word in re.findall(r"[A-Za-z][A-Za-z+#.-]{2,}", title)
        }

    wanted -= {"the", "and", "for"}

    present = {
        word.lower() for word in re.findall(r"[A-Za-z][A-Za-z+#.-]{2,}", job_title or "")
    }

    return bool(wanted & present)
