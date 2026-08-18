"""
Per-requirement fit report.

Reads what a posting actually asks for, line by line, and says which of those
the candidate's real resume can answer — with the sentence from the resume that
answers it. Where nothing answers it, it says so.

**Why this exists.** A single fit score tells you a resume is a 0.58 and
nothing about what to do. This says "they ask for five things, you can evidence
three, and here is the wording in your own resume that proves them" — which is
what makes a truthful reframing possible, and what makes it obvious when a
posting is not worth an application at all.

**What it will not do.** Close a gap. A requirement with no evidence stays
missing; the honest response to that is a different posting, not a different
resume. Everything downstream of this report may reorder and reword what the
resume says, never add to it.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)

# Words that carry no signal when matching a requirement to experience.
STOPWORDS = {
    "the", "and", "for", "with", "you", "your", "our", "are", "will", "have",
    "has", "this", "that", "from", "they", "their", "them", "who", "why",
    "how", "what", "when", "where", "able", "ability", "work", "working",
    "experience", "experienced", "years", "year", "strong", "excellent",
    "good", "great", "team", "role", "job", "position", "must", "should",
    "would", "can", "plus", "nice", "bonus", "preferred", "required", "skills",
    "knowledge", "understanding", "familiarity", "proven", "track", "record",
    "including", "such", "etc", "other", "others", "all", "any", "some",
    "well", "highly", "very", "self", "high", "using", "use", "used", "new",
    "across", "within", "into", "over", "more", "than", "also", "both",
}

# Terms that mean the same thing on a resume and in a posting. Without this a
# candidate who "handled escalations in Zendesk" reads as having nothing to do
# with a posting asking for "ticketing systems".
SYNONYMS = {
    "ticketing": {"zendesk", "freshdesk", "jira", "ticket", "tickets", "crm"},
    "crm": {"salesforce", "hubspot", "zendesk", "ticketing"},
    "escalation": {"escalations", "escalated", "complaints", "resolution"},
    "stakeholder": {"client", "clients", "customer", "customers", "employee"},
    "onboarding": {"induction", "training", "orientation", "onboard"},
    "training": {"mentored", "mentoring", "coaching", "trained", "onboarding"},
    "documentation": {"sop", "sops", "procedures", "documented", "knowledge"},
    "communication": {"communications", "correspondence", "liaison"},
    "operations": {"operational", "ops", "process", "processes"},
    "payroll": {"compensation", "benefits", "hris"},
    "compliance": {"regulatory", "policy", "policies", "gdpr"},
    "analysis": {"analytics", "reporting", "reports", "data"},
    "remote": {"distributed", "asynchronous", "async"},
}

# A requirement line shorter than this is a heading, not a requirement.
MIN_REQUIREMENT_WORDS = 4
MAX_REQUIREMENTS = 20

# Evidence longer than this is truncated before it is compared. Length is what
# makes a statement match everything: the longer the text, the closer its
# embedding sits to the centre of all professional prose.
MAX_EVIDENCE_WORDS = 45

# Lines every posting carries that ask nothing of the candidate. Scored as
# requirements they drag the fit down and tell the user nothing.
BOILERPLATE = re.compile(
    r"privacy polic|recruitment privacy|equal opportunit|we encourage every"
    r"|apply now|talent acquisition|please review|cookie|about (remote|us)\b"
    r"|our mission|we.re always looking|by expressing your interest"
    r"|this is not an active job|future opening|evergreen pipeline"
    r"|report to our|you will report"
    # What the employer offers, not what it asks for. These were being scored
    # as requirements the candidate had failed to evidence: a resume cannot
    # "match" an Employee Stock Purchase Plan, and five such lines pulled a
    # genuine 40% fit down to single figures.
    r"|paid time off|parental leave|stock purchase|equity compensation"
    r"|resource group|development fund|benefits to support|health insurance"
    r"|dental|vision (care|insurance)|401\(?k|pension|life insurance"
    r"|home office (budget|stipend)|wellbeing|well-being|discount"
    r"|we offer|you.ll (get|receive)|perks|compensation range|salary range"
    r"|base (pay|salary) range|sabbatical|volunteer (day|time)"
    r"|how .{0,30} supports (full|part).time|how we (hire|support)",
    re.IGNORECASE,
)


@dataclass
class RequirementMatch:
    """One thing the posting asks for, and what the resume says about it."""

    requirement: str
    verdict: str  # "matched" | "partial" | "missing"
    evidence: Optional[str] = None
    matched_terms: List[str] = field(default_factory=list)
    # Set when the requirement names a specific technology the resume never
    # mentions — Ruby, Kubernetes, Salesforce. These are what a screen filters
    # on first, so an unmet one costs more than a soft requirement.
    missing_technologies: List[str] = field(default_factory=list)

    @property
    def weight(self) -> float:
        """
        How much this requirement counts towards the score.

        A named technology the candidate does not have is the requirement a
        recruiter screens on, and treating it as one line among twenty let a
        resume with no Ruby score 86% against a Ruby role.
        """
        return 2.0 if self.missing_technologies and self.verdict == "missing" else 1.0

    def to_dict(self) -> dict:
        """Serialize for the dashboard."""
        return {
            "requirement": self.requirement,
            "verdict": self.verdict,
            "evidence": self.evidence,
            "matched_terms": self.matched_terms,
            "missing_technologies": self.missing_technologies,
        }


@dataclass
class FitReport:
    """What a posting asks for, against what the candidate can evidence."""

    matches: List[RequirementMatch] = field(default_factory=list)

    @property
    def matched(self) -> List[RequirementMatch]:
        """Requirements the resume can evidence outright."""
        return [m for m in self.matches if m.verdict == "matched"]

    @property
    def partial(self) -> List[RequirementMatch]:
        """Requirements with adjacent, arguable evidence."""
        return [m for m in self.matches if m.verdict == "partial"]

    @property
    def missing(self) -> List[RequirementMatch]:
        """Requirements nothing in the resume speaks to."""
        return [m for m in self.matches if m.verdict == "missing"]

    @property
    def score(self) -> int:
        """
        Share of requirements evidenced, counting partials at half.

        Weighted, so that a named technology the resume does not have costs
        more than a soft requirement — that is the order a screen applies
        them in.
        """
        if not self.matches:
            return 0

        total = sum(match.weight for match in self.matches)
        earned = len(self.matched) + 0.5 * len(self.partial)

        return round(100 * earned / total) if total else 0

    @property
    def blocking(self) -> List[RequirementMatch]:
        """Requirements naming a technology the resume never mentions."""
        return [
            match for match in self.matches
            if match.missing_technologies and match.verdict == "missing"
        ]

    @property
    def worth_applying(self) -> bool:
        """
        Whether an application here is worth the candidate's name.

        Not a rule about quality — a judgement about whether a truthful
        application can compete. Below this, the honest move is a different
        posting rather than a more imaginative resume.
        """
        return self.score >= 40

    def to_dict(self) -> dict:
        """Serialize for the dashboard."""
        return {
            "score": self.score,
            "worth_applying": self.worth_applying,
            "counts": {
                "matched": len(self.matched),
                "partial": len(self.partial),
                "missing": len(self.missing),
            },
            "matched": [m.to_dict() for m in self.matched],
            "partial": [m.to_dict() for m in self.partial],
            "missing": [m.to_dict() for m in self.missing],
            # Named technologies the resume never mentions, gathered so the
            # dashboard can say "this role wants Ruby and Kubernetes" rather
            # than only showing a number.
            "blocking_technologies": sorted(
                {
                    technology
                    for match in self.blocking
                    for technology in match.missing_technologies
                }
            ),
        }


class FitAnalyser:
    """Compares a posting's requirements against a resume."""

    @staticmethod
    def run(job_text: str, resume_text: str) -> FitReport:
        """
        Build a per-requirement fit report by shared vocabulary.

        The synchronous path, kept because a great deal of the codebase and
        every existing test calls it. `run_async` is better wherever the
        caller can await: it matches by meaning, which is what a recruiter
        does, and this cannot.

        Args:
            job_text: The posting's description and requirements
            resume_text: The candidate's real resume

        Returns:
            A FitReport
        """
        requirements = FitAnalyser._requirements(job_text or "")
        sentences = FitAnalyser._sentences(resume_text or "")

        report = FitReport(
            matches=[
                FitAnalyser._assess(requirement, sentences)
                for requirement in requirements
            ]
        )

        logger.info(
            f"Fit report (lexical): {report.score}% — {len(report.matched)} matched, "
            f"{len(report.partial)} partial, {len(report.missing)} missing"
        )

        return report

    @staticmethod
    async def run_async(job_text: str, resume_text: str) -> FitReport:
        """
        Build a per-requirement fit report by meaning, not by shared words.

        "Built responsive interfaces in React" answers "Proficiency with
        modern frontend frameworks" and shares not one content word with it.
        Counting words scores that at zero and reports a resume as evidencing
        none of a posting it plainly speaks to — which is what made the fit
        analyser look broken.

        Falls back to the lexical path whenever no local embedding model is
        reachable, so a report is always produced.

        Args:
            job_text: The posting's description and requirements
            resume_text: The candidate's real resume

        Returns:
            A FitReport
        """
        from job_agent.services.semantic_match import (
            get_semantic_matcher,
            verdict_for,
        )

        requirements = FitAnalyser._requirements(job_text or "")
        sentences = FitAnalyser._sentences(resume_text or "")

        if not requirements or not sentences:
            return FitReport(
                matches=[
                    RequirementMatch(requirement=requirement, verdict="missing")
                    for requirement in requirements
                ]
            )

        similarities = await get_semantic_matcher().best_matches(
            requirements, sentences
        )

        resume_terms = _named_technologies(resume_text or "", anywhere=True)

        if similarities is None:
            return FitAnalyser.run(job_text, resume_text)

        matches: List[RequirementMatch] = []

        for requirement, similarity in zip(requirements, similarities):
            verdict = verdict_for(similarity.score, similarity.margin)

            # An embedding cannot tell you whether someone has used Ruby. It
            # matched "Deep experience with Ruby and Rails, comfortable
            # working within a large monolith" to "Comfortable working
            # independently on end-to-end product builds" — on the shared
            # phrasing, at a similarity and margin that both passed. A named
            # technology is not a matter of meaning: it is on the resume or it
            # is not, and where it is not, no amount of similar-sounding
            # experience evidences it.
            named = _named_technologies(requirement)
            unmet = named - resume_terms
            blocked = bool(named) and unmet == named

            if blocked:
                verdict = "missing"

            # A lexical hit is still worth having: an exact technology name
            # ("Kubernetes", "Ruby") is a harder signal than paraphrase
            # similarity, and embeddings smooth over exactly those. Whichever
            # method is more confident wins, so the two only ever add
            # evidence.
            lexical = FitAnalyser._assess(requirement, sentences)

            if _rank(lexical.verdict) > _rank(verdict):
                matches.append(lexical)
                continue

            matches.append(
                RequirementMatch(
                    requirement=requirement,
                    verdict=verdict,
                    evidence=similarity.text if verdict != "missing" else None,
                    matched_terms=lexical.matched_terms,
                    missing_technologies=sorted(unmet) if blocked else [],
                )
            )

        report = FitReport(matches=matches)

        logger.info(
            f"Fit report (semantic): {report.score}% — {len(report.matched)} matched, "
            f"{len(report.partial)} partial, {len(report.missing)} missing"
        )

        return report

    # ------------------------------------------------------------------
    # Reading the posting
    # ------------------------------------------------------------------

    @staticmethod
    def _requirements(job_text: str) -> List[str]:
        """
        Pull the lines that state what the job asks for.

        Postings put their requirements under a heading — "What you bring",
        "Requirements", "Qualifications" — and everything else is company
        boilerplate. Searching the whole text for the word "requirements"
        matched "we have built AI capabilities into the requirements for every
        role" and returned the marketing copy instead.

        Args:
            job_text: The posting text

        Returns:
            Requirement lines, deduplicated and capped
        """
        lines = [line.strip(" \t-•*·") for line in job_text.splitlines()]
        lines = [line for line in lines if line]

        # Anchored to the whole line: a heading is the only thing on its own
        # line. Matching a prefix meant "Benefits plans enrolment and
        # unenrollment" — an actual requirement — read as the start of the
        # compensation section and closed the list after one item.
        opens = re.compile(
            r"^(what you.ll bring|what you bring|what we.re looking for"
            r"|requirements|qualifications|minimum qualifications"
            r"|basic qualifications|preferred qualifications|who you are"
            r"|about you|what you.ll need|what you need to succeed"
            r"|skills and experience|you.ll bring|you have|your experience"
            r"|what makes you a good fit|we.re looking for)\s*:?$",
            re.IGNORECASE,
        )

        # Sections that follow the requirements and are not requirements. Left
        # open, a posting's benefits list becomes fifteen things the candidate
        # has failed to evidence.
        closes = re.compile(
            r"^(practicals|benefits|benefits and perks|what.s next"
            r"|how you.ll[^:]{0,40}|application process|about remote"
            r"|about the team|about gitlab|about the company|compensation"
            r"|perks|our values|our culture|life at [\w\s]{0,20}"
            r"|why join[\w\s]{0,20}|hiring process|interview process"
            r"|the interview|next steps|remote.first|equal opportunity"
            r"|country hiring guidelines)\s*:?$",
            re.IGNORECASE,
        )

        collected: List[str] = []
        inside = False

        for line in lines:
            if opens.match(line):
                inside = True
                continue

            if inside and closes.match(line):
                break

            if inside and FitAnalyser._is_requirement(line):
                collected.append(line)

        # No labelled section: fall back to every substantive line that is not
        # obviously company boilerplate.
        if len(collected) < 3:
            collected = [
                line for line in lines
                if FitAnalyser._is_requirement(line)
                and not BOILERPLATE.search(line)
            ]

        return list(dict.fromkeys(collected))[:MAX_REQUIREMENTS]

    @staticmethod
    def _is_requirement(line: str) -> bool:
        """
        Whether a line states something the candidate must be or have.

        Args:
            line: A line from the posting

        Returns:
            True if it reads as a requirement rather than a heading or notice
        """
        words = line.split()

        if not (MIN_REQUIREMENT_WORDS <= len(words) <= 45):
            return False

        if line.endswith(":"):
            return False

        return not BOILERPLATE.search(line)

    @staticmethod
    def _sentences(resume_text: str) -> List[str]:
        """
        Whole statements from the resume, as candidate evidence.

        A resume exported from a PDF hard-wraps its prose, so reading it line
        by line yields fragments: "web applications, complemented by prior
        front-end and desktop applicat". Matching against those is worse than
        useless — a fragment carries half a thought, and half a thought is
        close to *everything*, so the matcher paired "Experience managing
        technical support engineering" with a sentence about web development
        and called it evidence. Lines are joined back into sentences first.

        Args:
            resume_text: The candidate's resume

        Returns:
            Sentences and bullets, each a complete statement
        """
        joined: List[str] = []
        buffer = ""

        def flush() -> None:
            nonlocal buffer
            if buffer:
                joined.append(buffer)
                buffer = ""

        for raw in (resume_text or "").splitlines():
            line = raw.strip()

            if not line:
                flush()
                continue

            # A heading ends the statement above it and never joins the one
            # below. Merging them produced "PROJECTS BotCraft Ai — In
            # development Full-stack AI solutions platform…", a long generic
            # blob that an embedding model scores as moderately similar to
            # *everything* — which is how a Director of Support posting came
            # back 62% against a front-end resume.
            if _is_section_heading(line):
                flush()
                continue

            stripped = line.lstrip("-•*·– ").strip()

            starts_new = line[:1] in "-•*·" or not buffer or buffer.rstrip().endswith(
                (".", "!", "?", ":", ";")
            )

            if starts_new:
                flush()
                buffer = stripped
            else:
                buffer = f"{buffer} {stripped}"

        flush()

        parts: List[str] = []

        for statement in joined:
            for piece in re.split(r"(?<=[.;])\s+", statement):
                words = piece.strip().split()

                if len(words) < 4:
                    continue

                # A very long statement embeds toward the average of all
                # professional English and starts matching everything. One
                # claim per comparison keeps the signal.
                parts.append(" ".join(words[:MAX_EVIDENCE_WORDS]))

        return parts

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    @staticmethod
    def _terms(text: str) -> set:
        """Meaningful lowercase terms in a line."""
        # Split on slashes and dots so "Zendesk/e-mail" and "HR/Payroll" yield
        # the terms a resume would actually use, rather than one token that
        # matches nothing.
        cleaned = re.sub(r"[/\\]", " ", text.lower())
        words = re.findall(r"[A-Za-z][A-Za-z+#-]{2,}", cleaned)

        return {word.strip(".-") for word in words if word not in STOPWORDS}

    @staticmethod
    def _expand(terms: set) -> set:
        """Add the words that mean the same thing on a resume."""
        expanded = set(terms)

        for term in terms:
            expanded |= SYNONYMS.get(term, set())

            for key, group in SYNONYMS.items():
                if term in group:
                    expanded.add(key)
                    expanded |= group

        return expanded

    @staticmethod
    def _assess(requirement: str, sentences: List[str]) -> RequirementMatch:
        """
        Decide whether the resume evidences one requirement.

        Args:
            requirement: The line from the posting
            sentences: Candidate evidence from the resume

        Returns:
            A RequirementMatch carrying the evidence, when there is any
        """
        wanted = FitAnalyser._terms(requirement)

        if not wanted:
            return RequirementMatch(requirement=requirement, verdict="missing")

        best_sentence, best_overlap = None, set()

        for sentence in sentences:
            overlap = wanted & FitAnalyser._expand(FitAnalyser._terms(sentence))

            if len(overlap) > len(best_overlap):
                best_sentence, best_overlap = sentence, overlap

        share = len(best_overlap) / len(wanted)

        if share >= 0.5:
            verdict = "matched"
        elif share >= 0.25:
            verdict = "partial"
        else:
            return RequirementMatch(requirement=requirement, verdict="missing")

        return RequirementMatch(
            requirement=requirement,
            verdict=verdict,
            evidence=best_sentence,
            matched_terms=sorted(best_overlap),
        )


# Capitalised words that are ordinary English rather than the name of a
# technology, a product or a platform. Without this every sentence-initial
# word and every "Experience"/"Strong" would read as a named requirement.
_NOT_A_TECHNOLOGY = frozenset({
    "a", "an", "the", "and", "or", "but", "we", "you", "your", "our", "their",
    "this", "that", "these", "those", "it", "its", "as", "at", "by", "for",
    "from", "in", "into", "of", "on", "to", "with", "within", "across",
    "experience", "experienced", "strong", "deep", "proven", "demonstrated",
    "hands", "familiarity", "knowledge", "understanding", "ability", "track",
    "record", "excellent", "good", "great", "solid", "extensive", "working",
    "work", "team", "teams", "role", "candidate", "years", "year", "plus",
    "bonus", "nice", "preferred", "required", "must", "should", "will",
    "senior", "junior", "lead", "staff", "principal", "manager", "engineer",
    "developer", "designer", "director", "vice", "president", "head",
    "support", "customer", "customers", "product", "products", "business",
    "company", "companies", "global", "remote", "hybrid", "onsite", "full",
    "part", "time", "may", "can", "one", "two", "three", "four", "five",
    "center", "centre", "operations", "engineering", "development",
    "management", "leadership", "communication", "collaboration",
    # Qualities, not technologies. A posting writes "Strong Judgment" or
    # "Ownership: you spot what needs doing" with a capital, and that was read
    # as a named tool the candidate lacked — so a soft requirement became a
    # double-weighted blocker and the score understated the fit.
    "judgment", "judgement", "ownership", "autonomy", "initiative",
    "curiosity", "empathy", "integrity", "accountability", "adaptability",
    "creativity", "resilience", "pragmatism", "rigor", "rigour", "craft",
    "impact", "execution", "delivery", "quality", "mentorship", "coaching",
    "problem", "solving", "thinking", "mindset", "attitude", "passion",
    "written", "verbal", "english", "fluency", "attention", "detail",
    "responsibilities", "requirements", "qualifications", "benefits",
    "about", "what", "who", "why", "how", "where", "when",
})

# What a named technology looks like written down: a capitalised token that is
# not the first word of its sentence, an all-caps acronym, or a token carrying
# the punctuation software names use.
_TECHNOLOGY_TOKEN = re.compile(r"[A-Za-z][\w.+#/-]*")


def _named_technologies(text: str, anywhere: bool = False) -> set:
    """
    The specific technologies, products and platforms a text names.

    Args:
        text: A requirement line, or a whole resume
        anywhere: Treat every capitalised token as a candidate, including
            sentence-initial ones. Used when reading the resume, where the
            point is to collect everything it mentions rather than to decide
            what a sentence is asking for.

    Returns:
        Lowercased names
    """
    found = set()

    for sentence in re.split(r"(?<=[.;:])\s+|\n", text or ""):
        tokens = _TECHNOLOGY_TOKEN.findall(sentence)

        for index, token in enumerate(tokens):
            if index == 0 and not anywhere:
                continue  # Sentence-initial capitals say nothing

            bare = token.strip(".,")

            if len(bare) < 2 or bare.lower() in _NOT_A_TECHNOLOGY:
                continue

            # "Ruby", "Rails", "Kubernetes", "PostgreSQL", "AI", "SaaS".
            looks_named = bare[0].isupper() or bare.isupper()

            if looks_named and any(c.isalpha() for c in bare):
                found.add(bare.lower())

    return found


def _is_section_heading(line: str) -> bool:
    """
    Whether a resume line is a section heading rather than a statement.

    Args:
        line: A line from the resume

    Returns:
        True for "PROFESSIONAL EXPERIENCE", "PROJECTS", "EDUCATION" and the like
    """
    stripped = line.strip().rstrip(":")

    if not stripped or len(stripped.split()) > 4:
        return False

    if stripped[:1] in "-•*·" or stripped.endswith((".", ",", ";")):
        return False

    letters = [character for character in stripped if character.isalpha()]

    return bool(letters) and all(character.isupper() for character in letters)


def _rank(verdict: str) -> int:
    """Order verdicts so the more confident of two can be picked."""
    return {"matched": 2, "partial": 1}.get(verdict, 0)


async def analyse_fit_async(job_text: str, resume_text: str) -> FitReport:
    """
    Convenience wrapper around FitAnalyser.run_async.

    Args:
        job_text: Posting description and requirements
        resume_text: The candidate's resume

    Returns:
        A FitReport
    """
    return await FitAnalyser.run_async(job_text, resume_text)


def analyse_fit(job_text: str, resume_text: str) -> FitReport:
    """
    Convenience wrapper around FitAnalyser.run.

    Args:
        job_text: Posting description and requirements
        resume_text: The candidate's resume

    Returns:
        A FitReport
    """
    return FitAnalyser.run(job_text, resume_text)
