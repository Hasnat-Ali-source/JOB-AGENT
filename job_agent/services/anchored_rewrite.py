"""
Anchored resume rewriting.

**The failure this replaces.** Asked to tailor a whole resume in one turn, a
small local model does not edit — it writes. Given a Full-Stack Developer's
master resume and a posting for a VP of Data, llama3.2 returned a resume whose
entire experience section was the *posting's own responsibilities*, rewritten
in the past tense: "Oversaw the architecture and evolution of GitLab's data
engineering foundations". Every real employer, every real bullet, the degree
and the languages were gone. It looked like a finished resume and it was a
forgery, and because the posting's text is legitimately quotable the
fabrication check waved it through — only a dropped credential caught it.

Whole-document generation cannot be made safe by asking more firmly. So this
module removes the model's opportunity to do it:

- The master is split into **blocks** — a bullet, a paragraph, a heading, a
  contact line, an employment line.
- Headings, contact details, employment and education lines, skills lists and
  languages are **copied verbatim**. The model never sees a chance to touch a
  name, a date, an employer or a degree.
- Only prose blocks are sent for rewriting, **one at a time, anchored to their
  own source text**. The model is asked to say that same thing in the
  posting's vocabulary — not to write a resume.
- Every returned block is checked **against the block it came from**. A
  rewrite that keeps less than half of its source's meaning, invents a number,
  or names the hiring company is discarded and the master's own words are used.

The consequences are structural rather than advisory. Nothing can be dropped,
because unrewritten blocks fall back to the master. Nothing can be replaced
wholesale, because a block that shares nothing with its source fails its check.
The output is always at least the master, and at best the master reworded for
the posting — which is what tailoring was supposed to be.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional, Set

logger = logging.getLogger(__name__)

# How many blocks to rewrite at once. Sequential requests to a local model are
# slow and a resume has ~15 prose blocks; asking for a handful per turn keeps
# each response short enough that a 3B model stays on task.
BATCH_SIZE = 4

# A rewrite may be terser or more expansive than its source, but a block that
# comes back at a fifth of the length has been summarised away, and one at
# three times the length has had claims added to it.
MIN_LENGTH_RATIO = 0.5
MAX_LENGTH_RATIO = 2.2

# The share of a source block's meaning-bearing words a rewrite must still
# carry. Half is loose enough for a genuine reword into the posting's
# vocabulary ("managed escalations" -> "owned customer escalations end to
# end") and tight enough that a block replaced by the posting's own copy —
# which shares almost nothing with the candidate's real work — is rejected.
MIN_OVERLAP = 0.5

# The same floor applied to a long block lets a third of it disappear. A
# paragraph listing a project's features has no legitimate rewording that
# drops the client portal, the admin system and the email integration — but
# it keeps enough of its opening to clear a 50% bar. Long blocks are where
# clipping hides, so they are held to a higher one.
LONG_BLOCK_WORDS = 25
MIN_OVERLAP_LONG = 0.75

# How much of a rewrite may be words found neither in the block it came from,
# nor anywhere else in the master, nor in the posting. Some slack is necessary
# — rewording is choosing different words, and "delivered" for "built" is the
# whole point. Past about a third, the model has stopped rewording the
# passage and started writing over it: "providing strategic access to
# resources and expertise" bolted onto a line about running computer labs.
MAX_INVENTED_SHARE = 0.3

# A block shorter than this is a label, not prose worth rewriting.
MIN_WORDS_TO_REWRITE = 6

# Words that carry no identity. Counting them would let a rewrite pass on
# filler alone. Mirrors the list the tailoring service uses for the same
# reason.
_FILLER = frozenset({
    "and", "the", "for", "with", "from", "that", "this", "their", "them",
    "have", "has", "was", "were", "are", "into", "over", "under", "across",
    "through", "including", "various", "while", "also", "such", "other",
    "within", "based", "using", "used", "well", "more", "than", "then",
})

BULLET_MARKERS = ("-", "•", "*", "·", "–", "—")

# Qualifications a reworded block must carry through. Screening filters on
# these, and a summary is the easiest place for one to go missing unnoticed.
CREDENTIAL_TERMS = (
    "bachelor", "master's", "masters", "mba", "phd", "doctorate",
    "diploma", "certified", "certification",
)

# A line recording a fact rather than describing work: an employment or
# education entry, a language line, a certification. These are copied
# verbatim — there is no rewrite of "University of Lahore | 2017 – 2021" that
# is an improvement, and every way it can change is a way it can go wrong.
_FACT_LINE = re.compile(
    r"(19|20)\d{2}"
    r"|\|"
    r"|\b(certification|certificate|diploma|degree|bachelor|master|licen[cs]e"
    r"|fluent|native|proficient|present|current)\b",
    re.IGNORECASE,
)

# A line carrying a way to reach the candidate, or a link. Never rewritten:
# this is the header the model most often drops.
_CONTACT_LINE = re.compile(
    r"@|https?://|www\.|\.com|\.net|\.org|\.io|\+\d|\(\d{3}\)|\d{3}[-.\s]\d{3}",
    re.IGNORECASE,
)


@dataclass
class Block:
    """One unit of the master resume, and what may be done to it."""

    text: str
    rewritable: bool
    # Set when a rewrite was accepted, so the caller can report how much
    # actually changed rather than claiming a rewrite that never happened.
    rewritten: Optional[str] = None

    @property
    def output(self) -> str:
        """What this block contributes to the finished document."""
        return self.rewritten if self.rewritten is not None else self.text


@dataclass
class RewriteOutcome:
    """The finished document and an honest account of how it was made."""

    text: str
    rewritten_blocks: int = 0
    rewritable_blocks: int = 0
    rejected: List[str] = field(default_factory=list)

    @property
    def changed_anything(self) -> bool:
        """True when at least one block came back in the posting's language."""
        return self.rewritten_blocks > 0


def plan_blocks(master_text: str) -> List[Block]:
    """
    Split a master resume into blocks, marking which may be rewritten.

    A resume exported from a PDF hard-wraps its paragraphs, so a block has to
    span several lines — but only the ones that are genuinely one sentence
    carried over. Joining every run of consecutive lines instead was wrong in
    a way worth recording: it swallowed the five-line skills list into one
    block, and both project entries into another, and the model duly returned
    each as a single reworded sentence. The skills list stopped being a list
    and one of the two projects disappeared, inside an output that had passed
    every check.

    So a line joins the block above it only when that line is a **wrap** — the
    line above ended mid-sentence. Anything else starts its own block, and a
    line too short to be prose is copied verbatim as a label.

    Args:
        master_text: The master document

    Returns:
        Blocks in document order, together covering the whole master
    """
    from job_agent.services.document_parser import DocumentParser

    blocks: List[Block] = []
    prose: List[str] = []

    def flush() -> None:
        if not prose:
            return

        text = "\n".join(prose)
        blocks.append(Block(text=text, rewritable=True))
        prose.clear()

    for raw in master_text.split("\n"):
        stripped = raw.strip()

        # Never absorbed into a block, whatever precedes them: a heading is
        # structure, and a line carrying an email, a phone number or a link is
        # the one thing a rewrite must never be able to touch.
        untouchable = (
            not stripped
            or bool(DocumentParser.is_heading(stripped))
            or bool(_CONTACT_LINE.search(stripped))
        )

        continues_a_wrap = (
            not untouchable
            and bool(prose)
            and _is_wrapped(prose[-1])
            and stripped[:1] not in BULLET_MARKERS
        )

        # A mid-paragraph line is part of its paragraph even when it mentions a
        # degree or a year. Treating those as standalone facts cut the profile
        # summary into three pieces and asked the model to reword a sentence
        # fragment. What the fact rule was protecting — the degree, the
        # figures — `accept_rewrite` protects per block instead.
        if continues_a_wrap:
            prose.append(raw)
            continue

        if untouchable or _FACT_LINE.search(stripped):
            flush()
            blocks.append(Block(text=raw, rewritable=False))
            continue

        flush()

        # A project name, a skills category, a university. These read as prose
        # and are facts; rewriting "MavaEvents" can only damage it.
        if len(_strip_marker(stripped).split()) < MIN_WORDS_TO_REWRITE:
            blocks.append(Block(text=raw, rewritable=False))
            continue

        prose.append(raw)

    flush()

    return blocks


def _is_wrapped(line: str) -> bool:
    """
    Whether a line was cut off by the page rather than finished by its author.

    Args:
        line: A line of the master

    Returns:
        True when the sentence carries on to the next line
    """
    return not line.rstrip().endswith((".", "!", "?", ":", ";"))


def _strip_marker(text: str) -> str:
    """The block's text without its bullet marker or indentation."""
    return re.sub(r"^\s*[-•*·–—]\s*", "", text.strip())


def _marker_of(text: str) -> str:
    """The bullet marker and indentation a block must be given back."""
    match = re.match(r"^(\s*[-•*·–—]\s*)", text)
    return match.group(1) if match else ""


def content_words(text: str) -> Set[str]:
    """
    The words and numbers carrying a block's meaning.

    Words are reduced to a rough stem, and trailing punctuation is stripped.
    Both matter more than they look: without them "resolution." and
    "resolution" are different words, and so are "issue" and "issues", so a
    faithful rewording reads as having replaced half the line. Every check in
    this module rests on comparing these sets, and every one of them was
    quietly too strict.

    Args:
        text: Any text

    Returns:
        Stems of four characters or more, minus filler, plus every number — a
        metric is exactly what a rewrite must not lose or invent
    """
    lowered = (text or "").lower()

    words = set()

    for raw in re.findall(r"[a-z][a-z0-9+#./]{3,}", lowered):
        word = raw.strip("./")

        if len(word) < 4 or word in _FILLER:
            continue

        words.add(_stem(word))

    return words | set(re.findall(r"\d+", lowered))


def _stem(word: str) -> str:
    """
    A crude common stem, enough to see through a change of tense or number.

    Not linguistics — the only job here is that "deliver", "delivered" and
    "delivers" compare equal, so that rewording a line does not read as
    replacing it.

    Args:
        word: A lowercased word

    Returns:
        The word with a common inflectional ending removed, when what is left
        is still long enough to mean something
    """
    # "s" rather than "es": stripping "es" from "issues" leaves "issu", which
    # no longer equals the stem of "issue". Consistency between the two sides
    # of a comparison matters more here than linguistic accuracy.
    for suffix in ("ing", "ed", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]

    return word


def numbers_in(text: str) -> Set[str]:
    """Every number in a block — the detail a rewrite may neither add nor drop."""
    return set(re.findall(r"\d+", text or ""))


def accept_rewrite(
    source: str,
    candidate: str,
    company: str = "",
    supported: Optional[Set[str]] = None,
) -> Optional[str]:
    """
    Decide whether one rewritten block may replace its source.

    This is where "reword it for the posting" is separated from "write me a
    better candidate". The rewrite is read only against the block it came
    from, never against the resume as a whole, so a block that quietly becomes
    a different job has nowhere to hide.

    Args:
        source: The master's own text for this block
        candidate: What the model returned for it
        company: The hiring company's name, which a rewrite may not claim
        supported: Every word the rest of the master and the posting use.
            A rewording may reach for the employer's vocabulary or the
            candidate's own words; a word from neither is the model writing
            rather than editing — "live access provided **to veterans** on
            request" is what that looks like, and it is invisible to the
            document-level fabrication check because it is neither a number
            nor a proper noun.

    Returns:
        The reason it was refused, or None if it may be used
    """
    text = (candidate or "").strip()

    if not text:
        return "empty"

    bare_source = _strip_marker(source)
    bare = _strip_marker(text)

    if not bare:
        return "empty"

    ratio = len(bare) / max(len(bare_source), 1)

    if ratio < MIN_LENGTH_RATIO:
        return f"summarised away ({int(ratio * 100)}% of the original length)"

    if ratio > MAX_LENGTH_RATIO:
        return f"padded out ({int(ratio * 100)}% of the original length)"

    # A number that was not in the source is a claim the candidate never made.
    invented = numbers_in(bare) - numbers_in(bare_source)

    if invented:
        return f"invented a figure ({', '.join(sorted(invented)[:3])})"

    if numbers_in(bare_source) - numbers_in(bare):
        return "dropped a figure the master states"

    # Naming the employer being applied to turns a description of the
    # candidate's own work into a claim to have done it for them. This is
    # precisely how whole-document rewriting failed.
    if company and len(company) > 2 and company.lower() in bare.lower():
        if company.lower() not in bare_source.lower():
            return f"claims work at {company}"

    # A qualification is checked here, per block, rather than over the
    # finished document. A rewrite that trims "completing a research-based
    # Master's degree" out of a summary costs the candidate a degree, and
    # catching it at the document level would throw away every other good
    # rewording with it.
    lost = [
        term for term in CREDENTIAL_TERMS
        if term in bare_source.lower() and term not in bare.lower()
    ]

    if lost:
        return f"dropped the qualification the master states ({lost[0]})"

    wanted = content_words(bare_source)
    produced = content_words(bare)

    if wanted:
        kept = len(wanted & produced) / len(wanted)
        floor = MIN_OVERLAP_LONG if len(wanted) >= LONG_BLOCK_WORDS else MIN_OVERLAP

        if kept < floor:
            return f"says something else ({int(kept * 100)}% of the original kept)"

    if produced and supported is not None:
        invented_words = produced - wanted - supported

        if len(invented_words) / len(produced) > MAX_INVENTED_SHARE:
            named = ", ".join(sorted(invented_words)[:3])
            return f"added material the master does not support ({named})"

    return None


class AnchoredRewriter:
    """Rewords a master resume block by block, in the posting's vocabulary."""

    def __init__(self, chat) -> None:
        """
        Args:
            chat: An async callable taking (system, user) and returning the
                model's text. Injected so this module has no opinion about
                which backend is in use, and so tests can drive it directly.
        """
        self._chat = chat

    async def rewrite(self, master_text: str, job) -> RewriteOutcome:
        """
        Produce a tailored resume anchored to the master, block by block.

        Args:
            master_text: The master resume
            job: The posting being applied to

        Returns:
            A RewriteOutcome. Its text is the master itself when nothing could
            be improved — never less than the master.
        """
        blocks = plan_blocks(master_text)
        targets = [block for block in blocks if block.rewritable]
        outcome = RewriteOutcome(text=master_text, rewritable_blocks=len(targets))

        if not targets:
            return outcome

        vocabulary = posting_vocabulary(job)
        company = str(getattr(job, "company", "") or "")

        # What a rewording is allowed to reach for: any word the candidate
        # already uses somewhere in their resume, and the terms that actually
        # characterise this job. Deliberately the ranked vocabulary rather
        # than every word in the posting — a word appearing once in an equal
        # opportunities footer is not this employer's language for the work,
        # and admitting the whole posting would licence anything in it.
        supported = content_words(master_text) | set(
            posting_vocabulary(job, limit=120)
        )

        for start in range(0, len(targets), BATCH_SIZE):
            batch = targets[start:start + BATCH_SIZE]

            try:
                returned = await self._rewrite_batch(batch, job, vocabulary)
            except Exception as e:
                logger.info(
                    f"Anchored rewrite gave up on a batch ({type(e).__name__}: {e}) — "
                    f"those blocks keep the master's wording"
                )
                continue

            for block, candidate in zip(batch, returned):
                if candidate is None:
                    continue

                refusal = accept_rewrite(block.text, candidate, company, supported)

                if refusal:
                    outcome.rejected.append(refusal)
                    logger.debug(
                        f"Refused a rewritten block ({refusal}): {candidate[:80]!r}"
                    )
                    continue

                block.rewritten = _marker_of(block.text) + _strip_marker(candidate)
                outcome.rewritten_blocks += 1

        outcome.text = "\n".join(block.output for block in blocks).strip()

        return outcome

    async def _rewrite_batch(self, batch: List[Block], job, vocabulary: List[str]) -> List[Optional[str]]:
        """
        Ask the model to reword one batch of blocks.

        Args:
            batch: The blocks to reword
            job: The posting
            vocabulary: The posting's distinctive terms

        Returns:
            One candidate per block, None where the model returned nothing for it
        """
        numbered = "\n\n".join(
            f"[{index + 1}] {_strip_marker(block.text)}"
            for index, block in enumerate(batch)
        )

        prompt = f"""Reword each numbered passage below so it speaks to this job,
using the job's own vocabulary where it honestly fits what the passage already
says.

--- The job ---
Title: {getattr(job, 'title', '')}
Company: {getattr(job, 'company', '')}
Words this employer uses: {', '.join(vocabulary[:25])}

--- The passages, from the candidate's own resume ---
{numbered}

--- Rules ---
Rewrite only the wording. Each passage describes something the candidate
actually did; your version must describe the same thing.
- Keep every number, date, tool, employer and product name exactly as written.
- Do not add a number, a tool, a client or an employer that is not already in
  the passage.
- Never say the candidate worked at {getattr(job, 'company', 'the company')} or
  did this job. These passages are their past work, not this role.
- Do not merge, split, summarise or explain the passages.
- Keep each version within a line or two of the length it started at.

Return exactly {len(batch)} line(s), each starting with its number in square
brackets, in the same order, and nothing else:
[1] your rewording of passage 1
"""

        raw = await self._chat(
            "You reword a candidate's own resume lines into an employer's "
            "vocabulary. You never invent experience and never write new "
            "achievements. Output only the numbered rewordings.",
            prompt,
        )

        return _parse_numbered(raw or "", len(batch))


def _parse_numbered(raw: str, expected: int) -> List[Optional[str]]:
    """
    Read the model's numbered reply back into one candidate per block.

    Args:
        raw: The model's reply
        expected: How many blocks were sent

    Returns:
        A list of length `expected`, None where nothing came back for a block
    """
    results: List[Optional[str]] = [None] * expected
    current: Optional[int] = None
    buffer: List[str] = []

    def commit() -> None:
        if current is not None and 0 <= current < expected and buffer:
            joined = " ".join(part.strip() for part in buffer).strip()
            if joined:
                results[current] = joined
        buffer.clear()

    for line in (raw or "").split("\n"):
        match = re.match(r"^\s*[\[(]?(\d{1,2})[\]).:]\s*(.*)$", line)

        if match:
            commit()
            current = int(match.group(1)) - 1
            remainder = match.group(2).strip()

            if remainder:
                buffer.append(remainder)

            continue

        if current is not None and line.strip():
            buffer.append(line.strip())

    commit()

    return results


def posting_vocabulary(job, limit: int = 40) -> List[str]:
    """
    The terms this employer uses for the work, most frequent first.

    This is what the rewrite reaches for. It is deliberately drawn from the
    posting's own text rather than from a fixed synonym table, because the
    words that get a resume past a screen are the ones in front of the person
    reading it.

    Args:
        job: The posting
        limit: How many terms to return

    Returns:
        Lowercased distinctive terms
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
        "experience", "years", "ability", "strong", "excellent", "good", "great",
        "help", "join", "looking", "seeking", "including", "well", "also",
        "about", "what", "how", "why", "when", "where", "would", "should",
        "across", "within", "every", "other", "must", "need", "want", "make",
    }

    counts: dict = {}

    for word in re.findall(r"[a-z][a-z0-9+#.]{3,}", text):
        if word in stopwords:
            continue
        counts[word] = counts.get(word, 0) + 1

    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)

    return [word for word, _ in ranked[:limit]]
