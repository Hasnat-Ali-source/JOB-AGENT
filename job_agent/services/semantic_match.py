"""
Matching a requirement to evidence by meaning rather than by shared words.

**Why the old matcher scored honest resumes at zero.** Fit was decided by
counting words a requirement and a resume line had in common, rescued by a
hand-written synonym table of about a dozen entries. That misses almost every
real match. "Built responsive interfaces in React" and "Proficiency with
modern frontend frameworks" share *no* content words at all, so a resume that
plainly answers the requirement scored nothing against it — and a user reading
"you evidence 0% of this posting" concludes the analyser is broken, because
from where they are sitting it is.

A synonym table cannot be extended to fix this. The space of ways to say the
same thing about work is not enumerable, and every posting invents more.

**What this does instead.** Embeds the requirement and each candidate line
with a local embedding model through Ollama — `nomic-embed-text` by default,
which is small, fast, and already the model most Ollama installs have — and
compares them by cosine similarity. Meaning survives paraphrase, so the match
above is found.

**It stays a matcher, not a judge.** A high similarity means the resume
*talks about the same thing* as the requirement. It does not mean the
candidate meets it, and nothing here can invent evidence: every match points
at a real line of the user's own resume, which is what the fit report shows
them. Where nothing in the resume comes close, the requirement stays missing,
and that remains the honest answer — a different posting, not a different
resume.

**It degrades rather than fails.** No Ollama, no embedding model, or any
error, and the caller falls back to the lexical matcher. A fit report is
always produced.
"""

import logging
import math
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from job_agent.config import settings

logger = logging.getLogger(__name__)

# Small, fast, and the embedding model most Ollama installs already have.
DEFAULT_EMBED_MODEL = "nomic-embed-text"

# Cosine similarity above which a requirement counts as evidenced outright,
# and above which it counts as partial.
#
# Measured rather than guessed. Against nomic-embed-text, eleven hand-labelled
# requirement/evidence pairs from this project's own resume and postings
# separated cleanly: genuine matches ("modern frontend frameworks" against
# "component-based web interfaces using React") scored 0.56-0.74, and
# unrelated pairs ("managing technical support engineering" against the same
# React line) scored 0.39-0.50. Nothing crossed. The thresholds sit in that
# gap, with `matched` reserved for the upper half — an embedding model puts
# any two pieces of professional English above zero, so the floor matters more
# than the ceiling.
#
# `tests/test_semantic_match.py` holds those pairs; if a change to the model
# or the prompt moves the distribution, that test fails rather than the scores
# quietly becoming flattery.
MATCHED_AT = 0.62
PARTIAL_AT = 0.55

# How far above the resume's own baseline the best evidence must stand.
# Measured on this project's data: against a posting the resume genuinely
# answers, margins ran 0.145-0.190; against a Director of Support posting the
# same resume cannot answer, its role-specific requirements ran 0.092-0.119
# while its incidental ones ("works in a distributed team", "uses AI tooling")
# ran 0.167+ — which is correct, because the candidate does do those.
MATCHED_MARGIN = 0.14
PARTIAL_MARGIN = 0.10

# Embedding every line of every posting on every page load would be slow and
# pointless — the same resume is compared against the same postings again and
# again. Vectors are cached by their text.
_CACHE: Dict[str, List[float]] = {}
_CACHE_LIMIT = 4096


@dataclass
class Similarity:
    """How close one piece of evidence came to a requirement."""

    text: str
    score: float
    # How far the best evidence stands above this resume's average similarity
    # to the same requirement. The absolute score alone cannot be trusted: an
    # embedding model puts any two pieces of professional English around
    # 0.45-0.55, so a whole resume of unrelated work still clears an absolute
    # floor. What distinguishes real evidence is that it stands *out* from the
    # rest of the same resume.
    margin: float = 0.0


class SemanticMatcher:
    """Compares requirements to evidence using local embeddings."""

    def __init__(self, model: Optional[str] = None, host: Optional[str] = None) -> None:
        self.model = model or getattr(
            settings, "ollama_embed_model", DEFAULT_EMBED_MODEL
        )
        self.host = host or settings.ollama_url
        self._unavailable = False

    async def available(self) -> bool:
        """
        Whether an embedding model can actually be reached.

        Returns:
            True when embedding will work
        """
        if self._unavailable:
            return False

        return await self._resolve_model() is not None

    async def _resolve_model(self) -> Optional[str]:
        """
        Pick an installed embedding model.

        The configured one is preferred, but an install that has *an*
        embedding model and not that one should still get semantic matching
        rather than silently dropping to word counting.

        Returns:
            A model name, or None
        """
        try:
            from ollama import AsyncClient

            client = AsyncClient(host=self.host)
            listing = await client.list()
        except Exception as e:
            logger.info(f"No embedding model reachable ({type(e).__name__}: {e})")
            self._unavailable = True
            return None

        models = getattr(listing, "models", None) or listing.get("models", [])
        names: List[str] = []

        for entry in models:
            name = getattr(entry, "model", None) or (
                entry.get("model") or entry.get("name") if isinstance(entry, dict) else None
            )
            if name:
                names.append(name)

        for candidate in names:
            if candidate == self.model or candidate.startswith(f"{self.model}:"):
                return candidate

        # Any embedding model beats none. Cloud-hosted ones are skipped for
        # the same reason tailoring skips them: the resume would leave the
        # machine, which is the thing this project exists to avoid.
        for candidate in names:
            lowered = candidate.lower()

            if ("embed" in lowered or "bge" in lowered) and not lowered.endswith(":cloud"):
                logger.info(
                    f"Embedding model '{self.model}' is not installed — using "
                    f"'{candidate}'"
                )
                return candidate

        logger.info(
            f"Ollama has no embedding model installed — semantic matching is off. "
            f"`ollama pull {DEFAULT_EMBED_MODEL}` turns it on."
        )
        self._unavailable = True

        return None

    async def embed(self, texts: Sequence[str]) -> Optional[List[List[float]]]:
        """
        Embed a batch of texts, using the cache where possible.

        Args:
            texts: The texts to embed

        Returns:
            One vector per input, or None if embedding is unavailable
        """
        cleaned = [_normalise(text) for text in texts]
        missing = [text for text in dict.fromkeys(cleaned) if text and text not in _CACHE]

        if missing:
            model = await self._resolve_model()

            if not model:
                return None

            try:
                from ollama import AsyncClient

                client = AsyncClient(host=self.host)
                response = await client.embed(model=model, input=missing)
                vectors = (
                    getattr(response, "embeddings", None)
                    or response.get("embeddings")
                    or []
                )
            except Exception as e:
                logger.info(f"Embedding failed ({type(e).__name__}: {e})")
                self._unavailable = True
                return None

            if len(vectors) != len(missing):
                logger.info(
                    f"Embedding returned {len(vectors)} vector(s) for "
                    f"{len(missing)} input(s) — ignoring"
                )
                return None

            for text, vector in zip(missing, vectors):
                _remember(text, [float(value) for value in vector])

        result: List[List[float]] = []

        for text in cleaned:
            vector = _CACHE.get(text)

            if vector is None:
                return None

            result.append(vector)

        return result

    async def best_matches(
        self, requirements: Sequence[str], evidence: Sequence[str]
    ) -> Optional[List[Similarity]]:
        """
        For each requirement, the closest line of evidence and how close it is.

        Args:
            requirements: What the posting asks for
            evidence: Lines from the candidate's resume

        Returns:
            One Similarity per requirement, or None if embedding is
            unavailable and the caller should fall back
        """
        if not requirements or not evidence:
            return None

        vectors = await self.embed(list(requirements) + list(evidence))

        if not vectors:
            return None

        split = len(requirements)
        requirement_vectors = vectors[:split]
        evidence_vectors = vectors[split:]

        matches: List[Similarity] = []

        for requirement_vector in requirement_vectors:
            scored = [
                (text, cosine(requirement_vector, evidence_vector))
                for text, evidence_vector in zip(evidence, evidence_vectors)
            ]

            if not scored:
                matches.append(Similarity(text="", score=0.0))
                continue

            best_text, best_score = max(scored, key=lambda pair: pair[1])
            baseline = sum(score for _, score in scored) / len(scored)

            matches.append(
                Similarity(
                    text=best_text,
                    score=best_score,
                    margin=best_score - baseline,
                )
            )

        return matches


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """
    Cosine similarity of two vectors.

    Args:
        left: A vector
        right: Another vector of the same length

    Returns:
        Similarity in [-1, 1], or 0 when either vector has no magnitude
    """
    if len(left) != len(right):
        return 0.0

    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))

    if not left_norm or not right_norm:
        return 0.0

    return dot / (left_norm * right_norm)


def verdict_for(score: float, margin: float = 1.0) -> str:
    """
    Turn a similarity into the fit report's three-way verdict.

    Both an absolute floor and a margin over the resume's own baseline must be
    cleared. Either alone is wrong: the absolute score admits any professional
    resume, and the margin alone would credit the best of a uniformly poor
    set.

    Args:
        score: Cosine similarity of the best evidence
        margin: How far that stands above the resume's average for this
            requirement. Defaults to a passing value so callers that have no
            baseline (a single pair, as in the calibration test) still work.

    Returns:
        "matched", "partial" or "missing"
    """
    if score >= MATCHED_AT and margin >= MATCHED_MARGIN:
        return "matched"

    if score >= PARTIAL_AT and margin >= PARTIAL_MARGIN:
        return "partial"

    return "missing"


def _normalise(text: str) -> str:
    """Whitespace-collapsed text, so trivially different strings share a vector."""
    return re.sub(r"\s+", " ", (text or "").strip())[:1000]


def _remember(text: str, vector: List[float]) -> None:
    """
    Cache a vector, evicting oldest-first when the cache is full.

    Args:
        text: The text embedded
        vector: Its embedding
    """
    if len(_CACHE) >= _CACHE_LIMIT:
        for key in list(_CACHE)[: _CACHE_LIMIT // 4]:
            _CACHE.pop(key, None)

    _CACHE[text] = vector


_matcher: Optional[SemanticMatcher] = None


def get_semantic_matcher() -> SemanticMatcher:
    """Get the shared matcher, so its model lookup and cache are reused."""
    global _matcher

    if _matcher is None:
        _matcher = SemanticMatcher()

    return _matcher
