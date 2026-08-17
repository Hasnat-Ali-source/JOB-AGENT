"""
Fit Scoring Service (Phase 3).

Evaluates how well a job matches the user's search profile.
Placeholder implementation for Phase 3; LLM integration in Phase 4.

Scoring dimensions:
- Title match (title vs target_titles + alt_titles)
- Seniority match
- Location match
- Keywords match
- Exclusions match (negative score)

Score: 0-1 (1.0 = perfect fit)
"""

import logging
from typing import Optional

from job_agent.models.database import SearchProfile, Job

logger = logging.getLogger(__name__)


class FitScorer:
    """
    Placeholder fit scorer for Phase 3.
    
    Phase 4 will replace with LLM-based scoring.
    """
    
    @staticmethod
    def score_job(job: Job, search_profile: SearchProfile) -> float:
        """
        Score a job against a search profile.
        
        Phase 3 approach: Simple heuristic scoring
        Phase 4: Replace with LLM-based scoring
        
        Args:
            job: Job posting to score
            search_profile: Search profile to match against
        
        Returns:
            Fit score from 0.0 to 1.0
        """
        # Each dimension contributes (score, weight); the result is their
        # weighted average over whichever dimensions the profile actually
        # specifies. Dimensions the profile leaves blank are simply absent
        # rather than counted as neutral.
        components: list[tuple[float, float]] = []

        # Title match (weight: 0.3)
        title_score = FitScorer._score_title(job.title, search_profile)
        if title_score is not None:
            components.append((title_score, 0.3))

        # Seniority match (weight: 0.2)
        if search_profile.seniority:
            seniority_score = FitScorer._score_seniority(
                job.description,
                search_profile.seniority
            )
            if seniority_score is not None:
                components.append((seniority_score, 0.2))

        # Location match (weight: 0.2)
        if search_profile.country or search_profile.region or search_profile.remote_pref:
            location_score = FitScorer._score_location(
                job.location,
                search_profile.country,
                search_profile.region,
                search_profile.remote_pref
            )
            if location_score is not None:
                components.append((location_score, 0.2))

        # Keywords match (weight: 0.15)
        if search_profile.keywords:
            keywords_score = FitScorer._score_keywords(
                job.description,
                search_profile.keywords
            )
            if keywords_score is not None:
                components.append((keywords_score, 0.15))

        # Exclusions penalty (weight: 0.15)
        if search_profile.exclusions:
            exclusions_score = FitScorer._score_exclusions(
                job.description,
                search_profile.exclusions
            )
            if exclusions_score is not None:
                components.append((exclusions_score, 0.15))

        if not components:
            # Profile specifies nothing to match on — no signal either way
            return 0.5

        total_weight = sum(weight for _, weight in components)
        score = sum(value * weight for value, weight in components) / total_weight

        # Clamp to 0-1
        score = max(0.0, min(1.0, score))

        logger.debug(f"Scored job '{job.title}' at {score:.2f} against profile '{search_profile.name}'")

        return score
    
    @staticmethod
    def _score_title(title: str, search_profile: SearchProfile) -> Optional[float]:
        """
        Score title match against target_titles and alt_titles.
        
        Returns:
            0.0-1.0 or None if no target titles
        """
        if not search_profile.target_titles:
            return None
        
        title_lower = title.lower()
        
        # Check for exact matches in target_titles
        for target in search_profile.target_titles:
            if target.lower() in title_lower:
                return 1.0
        
        # Check for partial matches in alt_titles
        if search_profile.alt_titles:
            for alt in search_profile.alt_titles:
                if alt.lower() in title_lower:
                    return 0.8
        
        # Check for key words from target titles
        for target in search_profile.target_titles:
            words = target.lower().split()
            if any(word in title_lower for word in words):
                return 0.6
        
        return 0.3
    
    @staticmethod
    def _score_seniority(description: str, target_seniority: str) -> Optional[float]:
        """
        Score seniority level match.
        
        Returns:
            0.0-1.0
        """
        description_lower = description.lower()
        
        seniority_keywords = {
            "entry": ["junior", "entry", "graduate", "internship", "0-2 years", "early career"],
            "mid": ["mid-level", "3-5 years", "intermediate"],
            "senior": ["senior", "5+ years", "lead", "principal"],
            "staff": ["staff", "principal", "architect", "10+ years"],
            "executive": ["director", "vp", "c-level", "executive"],
        }
        
        target_keywords = seniority_keywords.get(target_seniority, [])
        
        if not target_keywords:
            return 0.5  # Unknown seniority level
        
        # Count keyword matches
        matches = sum(1 for kw in target_keywords if kw in description_lower)
        
        if matches > 0:
            return 1.0  # Strong seniority match
        
        # Check for opposite seniority levels (negative)
        opposite_keywords = [
            kw for level, kws in seniority_keywords.items()
            if level != target_seniority
            for kw in kws
        ]
        
        opposite_matches = sum(1 for kw in opposite_keywords if kw in description_lower)
        
        if opposite_matches > 0:
            return 0.3  # Wrong seniority level
        
        return 0.5  # Neutral
    
    @staticmethod
    def _score_location(
        job_location: str,
        target_country: Optional[str],
        target_region: Optional[str],
        target_remote: Optional[str]
    ) -> Optional[float]:
        """
        Score location match.
        
        Returns:
            0.0-1.0 or None if no location requirements
        """
        if not (target_country or target_region or target_remote):
            return None
        
        location_lower = job_location.lower()
        
        # Remote preference
        if target_remote == "remote":
            if "remote" in location_lower:
                return 1.0
            else:
                return 0.3  # Want remote, but this job isn't
        
        elif target_remote == "hybrid":
            if "hybrid" in location_lower:
                return 1.0
            elif "remote" in location_lower:
                return 0.7  # Remote is similar to hybrid
            else:
                return 0.4
        
        # Region match
        if target_region and target_region.lower() in location_lower:
            return 0.9
        
        # Country match (looser)
        if target_country and target_country.lower() in location_lower:
            return 0.7
        
        return 0.3  # Location doesn't match
    
    @staticmethod
    def _score_keywords(
        description: str,
        keywords: list
    ) -> Optional[float]:
        """
        Score keyword match (AND - all keywords should be present).
        
        Returns:
            0.0-1.0 or None if no keywords
        """
        if not keywords:
            return None
        
        description_lower = description.lower()
        
        # Count how many keywords are present
        matches = sum(1 for kw in keywords if kw.lower() in description_lower)
        
        # Score based on percentage of keywords matched
        return matches / len(keywords)
    
    @staticmethod
    def _score_exclusions(
        description: str,
        exclusions: list
    ) -> Optional[float]:
        """
        Score exclusions (negative - no exclusions should be present).
        
        Returns:
            0.0-1.0 or None if no exclusions
        """
        if not exclusions:
            return None
        
        description_lower = description.lower()
        
        # Count how many exclusions are present
        matches = sum(1 for excl in exclusions if excl.lower() in description_lower)
        
        # If any exclusions are present, score is low
        if matches > 0:
            return 0.1  # Some exclusions matched
        
        return 1.0  # No exclusions matched (good)


# Singleton instance
_fit_scorer: Optional[FitScorer] = None


def get_fit_scorer() -> FitScorer:
    """Get the global fit scorer instance."""
    global _fit_scorer
    
    if _fit_scorer is None:
        _fit_scorer = FitScorer()
    
    return _fit_scorer
