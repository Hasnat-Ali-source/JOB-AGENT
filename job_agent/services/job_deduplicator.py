"""
Job Deduplicator Service (Phase 3).

Detects duplicate job postings from multiple sources using fuzzy matching.

Strategy:
- Primary: Exact hash match on (company + title + location)
- Secondary: Fuzzy match using rapidfuzz for near-duplicates
- Threshold: 85% similarity or higher = same job

Normalizes inputs before hashing:
- Lowercase all strings
- Remove extra whitespace
- Remove common job title prefixes/suffixes
- Remove company suffixes (Inc, LLC, Ltd, etc.)
"""

import hashlib
import logging
import re
from typing import Any, Optional, Sequence

from rapidfuzz import fuzz

logger = logging.getLogger(__name__)


class JobDeduplicator:
    """
    Detects and handles duplicate job postings.
    """
    
    # Company name suffixes to normalize
    COMPANY_SUFFIXES = [
        r'\s+(Inc|Inc\.|Incorporated)$',
        r'\s+(LLC|LLC\.)$',
        r'\s+(Ltd|Ltd\.|Limited)$',
        r'\s+(Corp|Corp\.|Corporation)$',
        r'\s+(\.\s*)?com$',
        r'\s+(GmbH)$',
        r'\s+(AG)$',
        r'\s+(S\.?A\.?)$',
        r'\s+(Pvt|Pvt\.|Private)$',
    ]
    
    # Job title prefixes to normalize
    TITLE_PREFIXES = [
        r'^(Senior|Lead|Principal|Staff|Junior|Entry-Level)\s+',
        r'^(Sr|Jr)\.?\s+',
    ]

    # Employers must match at least this closely before two postings can be
    # considered the same job (see is_duplicate).
    COMPANY_MATCH_THRESHOLD = 95
    
    @staticmethod
    def generate_dedup_hash(company: str, title: str, location: str) -> str:
        """
        Generate a hash for deduplication.
        
        Normalizes inputs and creates hash from (company + title + location).
        Same job from multiple sources should generate same hash.
        
        Args:
            company: Company name
            title: Job title
            location: Job location
        
        Returns:
            SHA256 hash hex string
        """
        # Normalize inputs
        company = JobDeduplicator._normalize_company(company)
        title = JobDeduplicator._normalize_title(title)
        location = JobDeduplicator._normalize_location(location)
        
        # Create composite key
        composite = f"{company}|{title}|{location}"
        
        # Hash it
        hash_obj = hashlib.sha256(composite.encode('utf-8'))
        hash_hex = hash_obj.hexdigest()
        
        logger.debug(f"Generated dedup hash for {company}/{title}/{location}: {hash_hex[:8]}...")
        
        return hash_hex
    
    @staticmethod
    def _normalize_company(company: str) -> str:
        """Normalize company name for deduplication."""
        if not company:
            return ""
        
        # Lowercase
        normalized = company.lower().strip()
        
        # Remove common suffixes
        for suffix_pattern in JobDeduplicator.COMPANY_SUFFIXES:
            normalized = re.sub(suffix_pattern, '', normalized, flags=re.IGNORECASE)
        
        # Remove extra whitespace
        normalized = ' '.join(normalized.split())
        
        return normalized
    
    @staticmethod
    def _normalize_title(title: str) -> str:
        """Normalize job title for deduplication."""
        if not title:
            return ""
        
        # Lowercase
        normalized = title.lower().strip()
        
        # Remove common prefixes (Senior, Lead, etc.)
        for prefix_pattern in JobDeduplicator.TITLE_PREFIXES:
            normalized = re.sub(prefix_pattern, '', normalized, flags=re.IGNORECASE)
        
        # Remove extra whitespace
        normalized = ' '.join(normalized.split())
        
        return normalized
    
    @staticmethod
    def _normalize_location(location: str) -> str:
        """Normalize location for deduplication."""
        if not location:
            return ""
        
        # Lowercase and strip
        normalized = location.lower().strip()
        
        # Handle "Remote" / "WFH" variants
        if "remote" in normalized or "wfh" in normalized or "work from home" in normalized:
            normalized = "remote"
        
        # Remove extra whitespace
        normalized = ' '.join(normalized.split())
        
        return normalized
    
    @staticmethod
    def is_duplicate(job1_company: str, job1_title: str, job1_location: str,
                     job2_company: str, job2_title: str, job2_location: str,
                     threshold: int = 85) -> bool:
        """
        Check if two jobs are duplicates using fuzzy matching.
        
        Uses rapidfuzz to compare normalized job attributes.
        
        Args:
            job1_company: First job company
            job1_title: First job title
            job1_location: First job location
            job2_company: Second job company
            job2_title: Second job title
            job2_location: Second job location
            threshold: Similarity threshold (0-100)
        
        Returns:
            True if jobs are likely the same, False otherwise
        """
        # Normalize
        comp1 = JobDeduplicator._normalize_company(job1_company)
        comp2 = JobDeduplicator._normalize_company(job2_company)
        
        title1 = JobDeduplicator._normalize_title(job1_title)
        title2 = JobDeduplicator._normalize_title(job2_title)
        
        loc1 = JobDeduplicator._normalize_location(job1_location)
        loc2 = JobDeduplicator._normalize_location(job2_location)
        
        # Compare each field independently. A blended average is wrong here:
        # "Company1" and "Company2" are 87% similar, and with an identical
        # title and location the average clears any sane threshold — merging
        # two unrelated employers' postings into one.
        company_similarity = fuzz.ratio(comp1, comp2)
        title_similarity = fuzz.ratio(title1, title2)
        location_similarity = fuzz.ratio(loc1, loc2)

        # The employer must be essentially identical (this tolerates typos and
        # punctuation, not a different company with a similar name).
        if company_similarity < JobDeduplicator.COMPANY_MATCH_THRESHOLD:
            return False

        # Title and location then each have to clear the threshold on their own.
        if title_similarity < threshold or location_similarity < threshold:
            return False

        logger.debug(f"Duplicate detected: "
                    f"{job1_company}/{job1_title} vs {job2_company}/{job2_title} "
                    f"(company: {company_similarity:.1f}%, title: {title_similarity:.1f}%, "
                    f"location: {location_similarity:.1f}%)")

        return True


    @staticmethod
    def find_duplicate(
        company: str,
        title: str,
        location: str,
        candidates: Sequence[Any],
        threshold: int = 85,
    ) -> Optional[Any]:
        """
        Find the first near-duplicate of a job among existing records.

        Used as the secondary pass after the exact dedup_hash lookup misses:
        the same posting seen on two platforms often differs by a word
        ("Sr. Backend Engineer" vs "Senior Backend Engineer, Platform").

        Args:
            company: Candidate job's company
            title: Candidate job's title
            location: Candidate job's location
            candidates: Existing job records (objects with .company/.title/.location)
            threshold: Similarity threshold (0-100)

        Returns:
            The matching record, or None if no candidate is similar enough
        """
        for existing in candidates:
            if JobDeduplicator.is_duplicate(
                company, title, location,
                existing.company, existing.title, existing.location,
                threshold,
            ):
                return existing

        return None


def generate_dedup_hash(company: str, title: str, location: str) -> str:
    """
    Convenience function to generate dedup hash.
    
    Args:
        company: Company name
        title: Job title
        location: Job location
    
    Returns:
        SHA256 hash hex string
    """
    return JobDeduplicator.generate_dedup_hash(company, title, location)


def is_duplicate_job(job1_company: str, job1_title: str, job1_location: str,
                     job2_company: str, job2_title: str, job2_location: str,
                     threshold: int = 85) -> bool:
    """
    Convenience function to check if two jobs are duplicates.
    
    Args:
        job1_company: First job company
        job1_title: First job title
        job1_location: First job location
        job2_company: Second job company
        job2_title: Second job title
        job2_location: Second job location
        threshold: Similarity threshold
    
    Returns:
        True if duplicate, False otherwise
    """
    return JobDeduplicator.is_duplicate(
        job1_company, job1_title, job1_location,
        job2_company, job2_title, job2_location,
        threshold
    )


def find_duplicate_job(
    company: str,
    title: str,
    location: str,
    candidates: Sequence[Any],
    threshold: int = 85,
) -> Optional[Any]:
    """
    Convenience function to find a near-duplicate among existing jobs.

    Args:
        company: Candidate job's company
        title: Candidate job's title
        location: Candidate job's location
        candidates: Existing job records
        threshold: Similarity threshold

    Returns:
        Matching record, or None
    """
    return JobDeduplicator.find_duplicate(company, title, location, candidates, threshold)
