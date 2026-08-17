"""
Document Tailoring (Phase 4).

Produces a job-specific variant of a master resume or cover letter.

Three generators, tried in order of preference:

1. Ollama  — local LLM, the project default (nothing leaves the machine)
2. Anthropic — Claude API, used only when a key is configured
3. Deterministic — no LLM at all: reorders and selects existing content

The deterministic generator is not merely a stub. It is the guaranteed-safe
path: because it only reorders sentences that already exist in the master, it
cannot invent anything. It runs whenever no LLM is reachable, so document
generation never silently fails just because `ollama serve` isn't running.

Every generated variant — including LLM output — is passed through
FabricationCheck before it is returned. Flags are attached to the result, not
raised, so the user can see exactly what the model added and decide.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

from job_agent.config import settings
from job_agent.models.database import DocumentType
from job_agent.services.fabrication_check import verify_no_fabrication

logger = logging.getLogger(__name__)

# The instruction that keeps a model from inventing experience. Repeated in
# both the system prompt and the user turn, because a single mention is easy
# for a model to drift away from over a long document.
NO_FABRICATION_RULE = (
    "You must not invent anything. Every company, job title, date, degree, "
    "certification, technology, metric and number in your output must already "
    "appear in the master document. You may reorder content and rewrite wording "
    "for emphasis — nothing else. If the candidate lacks something the job asks "
    "for, leave it out. Never add a skill or achievement to close a gap."
)

# The rule the model breaks most often, and the one the user notices: asked to
# tailor a resume, a small model returns a shorter one. It keeps what looked
# relevant and silently drops the skills list, the second job, the languages —
# leaving a half-page document that reads as a thin candidate rather than a
# focused one. Reordering and rewording are the job; deleting is not.
NOTHING_IS_LOST_RULE = (
    "You must not delete anything. Every section, every employer, every job "
    "title, every date range, every bullet point, every skill, every "
    "qualification and every language in the master document must still appear "
    "in your output. Reword them and reorder them so the most relevant come "
    "first — but the finished resume must contain the same facts as the master, "
    "not a subset of them. A shorter resume is a failed one."
)

MAX_DESCRIPTION_CHARS = 6000  # Keep prompts within a small local model's context


@dataclass
class TailoringResult:
    """Outcome of a tailoring run."""

    content_text: str
    generator: str  # "llm:ollama:<model>", "llm:anthropic:<model>", "deterministic"
    notes: List[str] = field(default_factory=list)
    fabrication_flags: List[str] = field(default_factory=list)

    @property
    def is_verified(self) -> bool:
        """True when nothing unsupported by the master was detected."""
        return not self.fabrication_flags


class TailoringService:
    """Generates job-specific document variants."""

    def __init__(self) -> None:
        self.ollama_url = settings.ollama_url
        self.ollama_model = settings.ollama_model
        self.use_ollama = settings.use_ollama
        self.allow_cloud_models = getattr(settings, "ollama_allow_cloud_models", False)
        self.use_anthropic = settings.use_anthropic
        self.anthropic_api_key = settings.anthropic_api_key
        self.anthropic_model = getattr(settings, "anthropic_model", "claude-sonnet-5")

    async def tailor(
        self,
        master_text: str,
        job,
        doc_type: DocumentType,
        profile: Optional[Any] = None,
    ) -> TailoringResult:
        """
        Produce a tailored variant of a master document for one job.

        Args:
            master_text: The user's master document text
            job: Job record (title, company, description, requirements)
            doc_type: Resume or cover letter
            profile: Optional active candidate profile to allow contact info fields

        Returns:
            TailoringResult, always with content — never raises for a missing LLM
        """
        result = await self._generate(master_text, job, doc_type)

        profile_terms = []
        if profile:
            for field in ("full_name", "email", "phone", "location", "linkedin_url", "github_url", "portfolio_url", "website_url"):
                val = getattr(profile, field, None)
                if val:
                    profile_terms.append(str(val))

        # Verify whatever came back, including our own deterministic output
        result.fabrication_flags = verify_no_fabrication(
            master_text,
            result.content_text,
            allowed_terms=self._allowed_terms(job) + profile_terms,
        )

        if result.fabrication_flags:
            logger.warning(
                f"{result.generator} produced {len(result.fabrication_flags)} "
                f"unsupported claim(s) for '{getattr(job, 'title', '?')}' — "
                f"variant flagged for review"
            )

        return result

    async def _generate(self, master_text: str, job, doc_type: DocumentType) -> TailoringResult:
        """Try each generator in order of preference."""
        if self.use_anthropic and self.anthropic_api_key:
            result = await self._tailor_with_anthropic(master_text, job, doc_type)
            if result:
                return result

        if self.use_ollama:
            result = await self._tailor_with_ollama(master_text, job, doc_type)
            if result:
                return result

        logger.info("No LLM available — using the deterministic tailoring path")

        return self._tailor_deterministically(master_text, job, doc_type)

    # ------------------------------------------------------------------
    # Prompting
    # ------------------------------------------------------------------

    @staticmethod
    def _allowed_terms(job) -> List[str]:
        """Job-derived text a variant may legitimately mention."""
        return [
            str(getattr(job, "title", "") or ""),
            str(getattr(job, "company", "") or ""),
            str(getattr(job, "location", "") or ""),
            str(getattr(job, "description", "") or ""),
            str(getattr(job, "requirements", "") or ""),
        ]

    def _build_prompt(self, master_text: str, job, doc_type: DocumentType) -> str:
        """
        Build the user turn for the LLM.

        Args:
            master_text: Master document
            job: Job record
            doc_type: Resume or cover letter

        Returns:
            Prompt text
        """
        description = (getattr(job, "description", "") or "")[:MAX_DESCRIPTION_CHARS]
        requirements = (getattr(job, "requirements", "") or "")[:MAX_DESCRIPTION_CHARS]

        if doc_type == DocumentType.RESUME:
            task = (
                "Rewrite the master resume so the experience most relevant to this "
                "job appears first and is described in the job's own vocabulary. "
                "Keep the same overall resume structure and section headings. "
                "Your output must be the same length as the master or longer — "
                "every bullet in the master becomes a bullet in your output, "
                "reworded, in a new order. Return only the resume text."
            )
        else:
            task = (
                "Write a cover letter for this job using only the candidate's real "
                "background from the master document. Three or four short "
                "paragraphs. No placeholders such as [Your Name] — use the details "
                "in the master document. Return only the letter text."
            )

        # The instruction is repeated after the inputs: small models given a
        # long prompt tend to continue it rather than act on it, and echo the
        # whole thing back. Ending on the instruction makes that much rarer.
        rules = NO_FABRICATION_RULE + (
            f"\n\n{NOTHING_IS_LOST_RULE}" if doc_type == DocumentType.RESUME else ""
        )

        return f"""{task}

{rules}

--- The job being applied to ---
Title: {getattr(job, 'title', '')}
Company: {getattr(job, 'company', '')}
Location: {getattr(job, 'location', '')}

Description:
{description}

Requirements:
{requirements}

--- The candidate's master document (the only source of facts) ---
{master_text}

--- End of inputs ---

Now write the {'resume' if doc_type == DocumentType.RESUME else 'cover letter'}.
Output only the finished document, beginning with the candidate's name.
Do not repeat these instructions, the job description, or any of the headings
above. Do not add any commentary before or after the document.
"""

    # ------------------------------------------------------------------
    # LLM backends
    # ------------------------------------------------------------------

    async def resolve_ollama_model(self, client) -> Optional[str]:
        """
        Pick which installed Ollama model to use.

        The configured model is preferred, but a fresh Ollama install rarely
        has exactly that one pulled — so rather than failing, fall back to any
        installed model that can do completion.

        Models tagged ':cloud' are skipped unless explicitly allowed: they run
        on Ollama's servers, which would send the user's resume and the job
        posting off the machine. Local execution is the entire reason this
        project prefers Ollama, so that can't be an accident.

        Args:
            client: An ollama AsyncClient

        Returns:
            A model name, or None if nothing usable is installed
        """
        listing = await client.list()
        models = getattr(listing, "models", None) or listing.get("models", [])

        available = []
        for entry in models:
            name = getattr(entry, "model", None) or (
                entry.get("model") or entry.get("name") if isinstance(entry, dict) else None
            )
            if name:
                available.append(name)

        if not available:
            logger.info("Ollama is running but has no models installed")
            return None

        # Exact match, then prefix match ("llama3.2" -> "llama3.2:latest")
        for candidate in available:
            if candidate == self.ollama_model or candidate.startswith(f"{self.ollama_model}:"):
                return candidate

        usable = [
            name for name in available
            if not self._is_embedding_model(name)
            and (self.allow_cloud_models or not name.endswith(":cloud"))
        ]

        if not usable:
            logger.info(
                f"No usable local Ollama model (configured '{self.ollama_model}' is not "
                f"installed; available: {', '.join(available)})"
            )
            return None

        chosen = usable[0]
        logger.info(
            f"Ollama model '{self.ollama_model}' is not installed — using '{chosen}' instead"
        )

        return chosen

    async def draft_answer(
        self,
        question: str,
        master_text: str,
        job: Any,
    ) -> Optional[str]:
        """
        Draft an answer to a free-text application question, for review.

        A blank box beside "Which countries did you manage Benefits in?" makes
        the user compose from scratch what the agent has already read in their
        resume. Drafting it is not answering for them: the draft arrives in
        the tray as an editable answer they must look at before anything is
        released, and it is held to the same rule as the documents — nothing
        that the master resume does not already say.

        Args:
            question: The question the form asks
            master_text: The user's master resume, the only source of facts
            job: The job being applied to

        Returns:
            A drafted answer, or None if no model is available or its output
            failed the no-invention check
        """
        description = (getattr(job, "description", "") or "")[:2000]

        prompt = f"""Answer this application question as the candidate, in the
first person, in at most three sentences.

{NO_FABRICATION_RULE}

If the master document does not support an answer, say so plainly in the
candidate's voice — for example "I have not worked in this area" — rather
than inventing experience. Never claim a country, employer, tool or number
that is not in the master document.

--- The question ---
{question}

--- The job ---
{getattr(job, 'title', '')} at {getattr(job, 'company', '')}
{description}

--- The candidate's master document (the only source of facts) ---
{master_text}

--- End of inputs ---

Write only the answer itself. No preamble, no quotation marks, no commentary.
"""

        try:
            from ollama import AsyncClient

            client = AsyncClient(host=self.ollama_url)
            model = await self.resolve_ollama_model(client)

            if not model:
                return None

            response = await client.chat(
                model=model,
                messages=[
                    {"role": "system", "content": NO_FABRICATION_RULE},
                    {"role": "user", "content": prompt},
                ],
                options={"temperature": 0.2},
            )

            answer = self._clean((response.get("message") or {}).get("content", ""))
        except Exception as e:
            logger.info(f"Could not draft an answer for {question[:40]!r}: {e}")
            return None

        if not answer or len(answer) < 3:
            return None

        # Held to the same standard as a tailored document: a drafted answer
        # that names an employer or a metric the resume never mentions is the
        # exact failure the fabrication check exists to catch.
        from job_agent.services.fabrication_check import verify_no_fabrication

        flags = verify_no_fabrication(
            master_text,
            answer,
            allowed_terms=[
                getattr(job, "title", "") or "",
                getattr(job, "company", "") or "",
                question,
            ],
        )

        if flags:
            logger.info(
                f"Discarded a drafted answer for {question[:40]!r}: {flags[0]}"
            )
            return None

        return answer[:1200]

    @staticmethod
    def _is_embedding_model(name: str) -> bool:
        """Embedding models can't generate text; never pick one for tailoring."""
        lowered = name.lower()
        return "embed" in lowered or "bge" in lowered

    async def _tailor_with_ollama(
        self, master_text: str, job, doc_type: DocumentType
    ) -> Optional[TailoringResult]:
        """
        Tailor via a local Ollama model.

        Returns:
            TailoringResult, or None if Ollama is unreachable or has no model
        """
        try:
            from ollama import AsyncClient

            client = AsyncClient(host=self.ollama_url)

            model = await self.resolve_ollama_model(client)

            if not model:
                return None

            response = await client.chat(
                model=model,
                messages=[
                    {"role": "system", "content": NO_FABRICATION_RULE},
                    {"role": "user", "content": self._build_prompt(master_text, job, doc_type)},
                ],
                options={"temperature": 0.2},  # Tailoring is editing, not invention
            )

            message = getattr(response, "message", None) or response.get("message") or {}
            content = (getattr(message, "content", None) or message.get("content") or "").strip()

            if not content:
                logger.warning(f"Ollama model '{model}' returned an empty response")
                return None

            cleaned = self._clean(content)
            problem = self._is_usable(cleaned, master_text, job, doc_type)

            if problem:
                logger.warning(
                    f"Discarding output from '{model}': {problem}. "
                    f"Falling back to deterministic tailoring — a larger model "
                    f"(e.g. llama3.1:8b) follows document instructions more reliably."
                )
                return None

            return TailoringResult(
                content_text=cleaned,
                generator=f"llm:ollama:{model}",
                notes=[f"Tailored by local Ollama model '{model}'"],
            )

        except Exception as e:
            # Ollama not running is the normal case, not an error worth raising
            logger.info(f"Ollama unavailable ({type(e).__name__}: {e}) — falling through")
            return None

    async def _tailor_with_anthropic(
        self, master_text: str, job, doc_type: DocumentType
    ) -> Optional[TailoringResult]:
        """
        Tailor via the Claude API.

        Returns:
            TailoringResult, or None if the call fails
        """
        try:
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(api_key=self.anthropic_api_key)

            message = await client.messages.create(
                model=self.anthropic_model,
                max_tokens=4096,
                system=NO_FABRICATION_RULE,
                messages=[
                    {"role": "user", "content": self._build_prompt(master_text, job, doc_type)}
                ],
            )

            content = "".join(
                block.text for block in message.content if getattr(block, "type", "") == "text"
            ).strip()

            if not content:
                logger.warning("Claude returned an empty response")
                return None

            cleaned = self._clean(content)
            problem = self._is_usable(cleaned, master_text, job, doc_type)

            if problem:
                logger.warning(f"Discarding Claude output: {problem}")
                return None

            return TailoringResult(
                content_text=cleaned,
                generator=f"llm:anthropic:{self.anthropic_model}",
                notes=[f"Tailored by Claude ({self.anthropic_model})"],
            )

        except Exception as e:
            logger.warning(f"Claude API call failed ({type(e).__name__}: {e}) — falling through")
            return None

    # ------------------------------------------------------------------
    # Deterministic path
    # ------------------------------------------------------------------

    def _tailor_deterministically(
        self, master_text: str, job, doc_type: DocumentType
    ) -> TailoringResult:
        """
        Tailor without an LLM by selecting and reordering existing content.

        Cannot fabricate: every line of output is a line from the master.

        Args:
            master_text: Master document
            job: Job record
            doc_type: Resume or cover letter

        Returns:
            TailoringResult
        """
        keywords = self._job_keywords(job)

        if doc_type == DocumentType.COVER_LETTER:
            return self._deterministic_cover_letter(master_text, job, keywords)

        return self._deterministic_resume(master_text, keywords)

    def _deterministic_resume(self, master_text: str, keywords: List[str]) -> TailoringResult:
        """
        Reorder bullet points within each section so relevant ones lead.

        Section order and headings are preserved — only the ordering of bullets
        inside a section changes, and nothing is dropped.
        """
        from job_agent.services.document_parser import DocumentParser

        lines = master_text.split("\n")
        output: List[str] = []
        bullet_run: List[str] = []
        moved = 0

        def flush() -> None:
            nonlocal moved
            if not bullet_run:
                return

            scored = sorted(
                bullet_run,
                key=lambda line: self._relevance(line, keywords),
                reverse=True,
            )

            if scored != bullet_run:
                moved += 1

            output.extend(scored)
            bullet_run.clear()

        for line in lines:
            stripped = line.strip()

            if stripped[:1] in ("-", "•", "*", "·"):
                bullet_run.append(line)
                continue

            flush()
            output.append(line)

            if DocumentParser.is_heading(stripped):
                continue

        flush()

        notes = ["No LLM available — reordered existing content only, no rewriting."]
        if moved:
            notes.append(
                f"Moved job-relevant bullet points to the top of {moved} section(s)."
            )
        if keywords:
            notes.append(f"Matched on: {', '.join(keywords[:8])}")

        return TailoringResult(
            content_text="\n".join(output).strip(),
            generator="deterministic",
            notes=notes,
        )

    def _deterministic_cover_letter(
        self, master_text: str, job, keywords: List[str]
    ) -> TailoringResult:
        """
        Assemble a cover letter from sentences already in the master.

        The scaffolding (greeting, closing) is fixed boilerplate; every claim
        about the candidate is a sentence lifted verbatim from the master.
        """
        from job_agent.services.document_parser import DocumentParser

        sections = DocumentParser.split_sections(master_text)
        header = sections.get("header", "")
        name = header.split("\n")[0].strip() if header else ""

        # Pick the master's most job-relevant sentences, in original order
        sentences = [
            s.strip(" -•*·")
            for s in re.split(r"(?<=[.!?])\s+|\n", master_text)
            if len(s.strip()) > 40
        ]
        ranked = sorted(sentences, key=lambda s: self._relevance(s, keywords), reverse=True)
        highlights = [s for s in ranked[:3] if self._relevance(s, keywords) > 0] or ranked[:2]
        ordered = [s for s in sentences if s in highlights]

        title = getattr(job, "title", "the role")
        company = getattr(job, "company", "your company")

        body = "\n\n".join(
            [
                "Dear Hiring Manager,",
                f"I am writing to apply for the {title} position at {company}.",
                " ".join(ordered) if ordered else "",
                "I would welcome the opportunity to discuss how my background fits "
                "this role. Thank you for your consideration.",
                "Sincerely,",
                name,
            ]
        )

        return TailoringResult(
            content_text=re.sub(r"\n{3,}", "\n\n", body).strip(),
            generator="deterministic",
            notes=[
                "No LLM available — letter assembled from sentences in the master document.",
                "Every statement about the candidate is copied verbatim; review before sending.",
            ],
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _job_keywords(job) -> List[str]:
        """
        Extract distinctive terms from a job posting for relevance ranking.

        Returns:
            Lowercased keywords, most frequent first
        """
        text = " ".join(
            str(getattr(job, attr, "") or "")
            for attr in ("title", "description", "requirements")
        ).lower()

        stopwords = {
            "the", "and", "for", "with", "you", "our", "will", "are", "have", "this",
            "that", "from", "your", "who", "all", "can", "has", "was", "were", "not",
            "but", "they", "their", "them", "its", "into", "more", "than", "then",
            "work", "team", "role", "job", "position", "company", "candidate", "we",
            "a", "an", "of", "to", "in", "on", "at", "as", "is", "be", "or", "by", "it",
            "experience", "years", "ability", "strong", "excellent", "good", "great",
            "help", "join", "looking", "seeking", "including", "well", "also",
        }

        words = re.findall(r"[a-z][a-z0-9+#.]{2,}", text)
        counts: dict = {}

        for word in words:
            if word in stopwords:
                continue
            counts[word] = counts.get(word, 0) + 1

        return [w for w, _ in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)][:25]

    @staticmethod
    def _relevance(line: str, keywords: List[str]) -> int:
        """Count how many job keywords appear in a line."""
        lowered = line.lower()
        return sum(1 for kw in keywords if kw in lowered)

    @staticmethod
    def _clean(text: str) -> str:
        """
        Strip conversational wrappers and echoed prompt scaffolding.

        Models prefix "Here is the tailored resume:" or wrap output in a code
        fence; either would be rendered straight into the PDF. Small models go
        further and replay the prompt itself — observed with llama3.2:3b, which
        returned the job description and the section headings verbatim.

        Args:
            text: Raw model output

        Returns:
            Cleaned document text
        """
        text = text.strip()

        # Remove a leading/trailing code fence
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text)

        # If the model replayed the prompt, keep only what follows the last
        # input marker — that's where the actual document starts.
        for marker in ("--- End of inputs ---", "--- The candidate's master document"):
            if marker in text:
                tail = text.rsplit(marker, 1)[-1]
                # Drop a leftover marker heading line
                tail = re.sub(r"^[^\n]*---[^\n]*\n", "", tail).lstrip()
                if tail.strip():
                    text = tail
                break

        lines = text.split("\n")

        # Drop a single leading "Here is ...:" line
        if lines and re.match(r"^(here'?s?|sure|certainly|below)\b.*:\s*$", lines[0].strip(), re.I):
            lines = lines[1:]

        # Drop any remaining scaffolding lines the model repeated
        cleaned = [
            line for line in lines
            if not re.match(r"^\s*(-{3,}|={3,})", line)
            and not re.match(r"^\s*(Title|Company|Location|Description|Requirements):\s*$",
                             line.strip())
        ]

        return "\n".join(cleaned).strip()

    def _is_usable(
        self,
        content: str,
        master_text: str,
        job,
        doc_type: DocumentType = DocumentType.RESUME,
    ) -> Optional[str]:
        """
        Decide whether model output is fit to become the user's document.

        A model that echoes the prompt, truncates after two lines, or returns
        the job description instead of a resume must not have its output
        rendered to PDF and attached to an application. Rejecting here falls
        through to the deterministic path, which always produces something
        sane.

        Args:
            content: Cleaned model output
            master_text: The master document
            job: The job being applied to
            doc_type: What is being written. A letter is held to the checks
                about echoing and truncation, but not to the ones about
                carrying a resume's header and credentials — a letter that
                recited the candidate's degrees would be a worse letter.

        Returns:
            A reason string if unusable, or None if it passes
        """
        if len(content.strip()) < 200:
            return f"output was only {len(content.strip())} characters"

        # Prompt scaffolding that survived cleaning
        for marker in ("--- The job being applied to", "--- The candidate's master",
                       "=== JOB ===", "MASTER DOCUMENT"):
            if marker.lower() in content.lower():
                return "output still contains the prompt scaffolding"

        # The job description reproduced verbatim means it echoed the input
        description = (getattr(job, "description", "") or "").strip()
        if len(description) > 60 and description[:60].lower() in content.lower():
            return "output reproduced the job description verbatim"

        # A resume that lost the candidate's name isn't a resume
        first_master_line = next(
            (line.strip() for line in master_text.split("\n") if line.strip()), ""
        )
        if first_master_line and first_master_line.lower() not in content.lower():
            return f"output does not contain the candidate's name ('{first_master_line}')"

        if doc_type != DocumentType.RESUME:
            return None

        # A rewrite that loses the header loses the point of the document. The
        # model keeps the name — it is told to start with it — and drops the
        # line under it carrying the email and phone, which nothing else in the
        # pipeline notices because the result still parses as a resume.
        dropped_contact = self._dropped_contact(content, master_text)

        if dropped_contact:
            return f"output dropped the candidate's {dropped_contact}"

        missing = self._dropped_credentials(content, master_text)

        if missing:
            return f"output dropped {missing[0]} from the master"

        dropped = self._dropped_content(content, master_text)

        if dropped:
            return (
                f"output dropped {len(dropped)} line(s) from the master, "
                f"starting with {dropped[0][:60]!r}"
            )

        return None

    @staticmethod
    def _dropped_content(content: str, master_text: str) -> List[str]:
        """
        Lines of the master with no counterpart anywhere in the rewrite.

        This is the check the user asked for after reading a tailored resume
        that had lost its skills list, its second role, its languages and half
        its bullets: what came back was not a focused version of their career,
        it was a shorter one, and it read as a thinner candidate. Tailoring is
        allowed to change how something is said and where it appears. It is not
        allowed to decide the candidate did less than they did.

        Matching is by content words rather than by exact text, so rewording
        passes and deletion does not. Two bars, because they carry different
        risk:

        - A line stating a **fact** — an employer and dates, a degree, a
          certification, a language — must survive nearly intact. There is no
          legitimate rewrite of "University of Lahore | 2017 – 2021" that loses
          the university or the years.
        - A line of **prose** — a bullet, a summary sentence — must keep half
          its content words. That is loose enough for a genuine rewrite in the
          job's vocabulary and tight enough that a deleted bullet has nowhere
          to hide.

        Args:
            content: The rewritten document
            master_text: The master document

        Returns:
            The master lines that went missing, in order
        """
        available = TailoringService._content_words(content)
        missing: List[str] = []

        for raw in master_text.split("\n"):
            line = raw.strip().lstrip("-•*·–— ").strip()

            if not line:
                continue

            wanted = TailoringService._content_words(line)

            # A one-word line is a section heading. Headings may legitimately
            # be renamed ("Professional Experience" -> "Experience"), and the
            # content under them is checked on its own.
            if len(wanted) < 2:
                continue

            kept = len(wanted & available) / len(wanted)
            floor = 0.8 if TailoringService._states_a_fact(line) else 0.5

            if kept < floor:
                missing.append(line)

        return missing

    @staticmethod
    def _states_a_fact(line: str) -> bool:
        """
        Whether a line records a fact rather than describes work.

        Args:
            line: A line from the master

        Returns:
            True for employment, education and qualification lines — the ones
            a recruiter reads for dates and names rather than for phrasing
        """
        return bool(
            re.search(r"(19|20)\d{2}", line)  # any year
            or "|" in line  # the separator resumes use for role | employer | dates
            or re.search(
                r"\b(certification|certificate|diploma|degree|bachelor|master|"
                r"licen[cs]e|fluent|native|proficient)\b",
                line,
                re.IGNORECASE,
            )
        )

    # Words that carry no identity: a line keeps its meaning without them, so
    # counting them would let a rewrite pass on filler alone.
    _FILLER = frozenset({
        "and", "the", "for", "with", "from", "that", "this", "their", "them",
        "have", "has", "was", "were", "are", "into", "over", "under", "across",
        "through", "including", "various", "while", "also", "such", "other",
        "within", "based", "using", "used", "well", "more", "than", "then",
        "ensuring", "provided", "delivered", "managed", "worked", "working",
    })

    @staticmethod
    def _content_words(text: str) -> set:
        """
        The words and numbers that carry a line's meaning.

        Args:
            text: Any text

        Returns:
            Lowercased words of four characters or more, plus every number —
            a metric is exactly the kind of detail a rewrite must not lose
        """
        lowered = (text or "").lower()

        words = {
            word for word in re.findall(r"[a-z][a-z0-9+#./]{3,}", lowered)
            if word not in TailoringService._FILLER
        }

        return words | set(re.findall(r"\d+", lowered))

    @staticmethod
    def _dropped_contact(content: str, master_text: str) -> Optional[str]:
        """
        Whether a rewrite lost a way for the employer to make contact.

        Args:
            content: The rewritten document
            master_text: The master document

        Returns:
            "email address" or "phone number" if one went missing, else None
        """
        from job_agent.services.document_parser import (
            has_email_address,
            has_phone_number,
        )

        checks = (
            ("email address", has_email_address),
            ("phone number", has_phone_number),
        )

        for label, present in checks:
            if present(master_text) and not present(content):
                return label

        return None

    @staticmethod
    def _dropped_credentials(content: str, master_text: str) -> List[str]:
        """
        Qualifications present in the master but missing from the rewrite.

        Tailoring may reorder and reword; it may not quietly cost the
        candidate a degree. A model trimming a long summary is the usual way
        this happens, and it is invisible in a finished-looking document.

        Args:
            content: The rewritten document
            master_text: The master document

        Returns:
            Descriptions of what went missing, empty if nothing did
        """
        haystack = content.lower()

        credentials = (
            "bachelor", "master's", "masters", "mba", "phd", "doctorate",
            "diploma", "certified", "certification",
        )

        return [
            f"a credential the master mentions ('{term}')"
            for term in credentials
            if term in master_text.lower() and term not in haystack
        ]


# Singleton
_tailoring_service: Optional[TailoringService] = None


def get_tailoring_service() -> TailoringService:
    """Get the global tailoring service instance."""
    global _tailoring_service

    if _tailoring_service is None:
        _tailoring_service = TailoringService()

    return _tailoring_service
