"""
Hard Filter Evaluator Service (Phase 3).

Evaluates if a job passes hard filters (salary, location, date posted, etc.).
Hard filters are binary (pass/fail) unlike fit scoring which is continuous.

Hard filters are strict exclusions:
- If salary_min is set and job salary < salary_min → FAIL
- If exclusions keyword found in description → FAIL
- If location doesn't match preference → FAIL
- If posted too long ago → FAIL

Jobs failing hard filters are marked hard_filter_pass=False and not shown to user.
"""

import logging
from datetime import timedelta
from typing import Optional
import re

from job_agent.models.database import SearchProfile, Job
from job_agent.utils.dates import utcnow

logger = logging.getLogger(__name__)


# Places people write several ways. A location filter that only does substring
# matching turns "United States" vs "US" — or a dropped plural — into zero
# results, and the run reports that as though no such jobs exist.
_REGION_ALIASES = [
    {"united states", "united state", "usa", "u.s.", "u.s.a.", "us", "america"},
    {"united kingdom", "uk", "u.k.", "great britain", "britain", "england"},
    {"united arab emirates", "uae", "u.a.e."},
    {"netherlands", "the netherlands", "holland"},
    {"singapore", "sg"},
    {"australia", "au"},
    {"canada", "ca"},
    {"germany", "deutschland", "de"},
    {"india", "in"},
]


def _region_terms(region: str) -> set:
    """
    Every spelling of a region worth accepting.

    Args:
        region: What the user typed, in any casing

    Returns:
        The alias group containing it, or just the term itself
    """
    term = region.strip().lower()

    for group in _REGION_ALIASES:
        if term in group:
            return group

    return {term}


def _location_names_region(job_location: str, region: str) -> bool:
    """
    Whether a job's location text names the region the user asked for.

    Args:
        job_location: The posting's location, lowercased
        region: The region from the search profile

    Returns:
        True if any spelling of the region appears
    """
    for term in _region_terms(region):
        # Short forms like "us" and "uk" are substrings of ordinary words
        # ("Austin", "Ukraine"), so they have to match as whole words.
        if len(term) <= 3:
            if re.search(rf"\b{re.escape(term)}\b", job_location):
                return True
        elif term in job_location:
            return True

    return False


class FilterEvaluator:
    """
    Evaluates if a job passes hard filters from a search profile.
    """
    
    @staticmethod
    def evaluate(job: Job, search_profile: SearchProfile) -> bool:
        """
        Evaluate if a job passes all hard filters.
        
        Args:
            job: Job posting to evaluate
            search_profile: Search profile with filter criteria
        
        Returns:
            True if job passes all filters, False if any filter fails
        """
        # Salary filter
        if not FilterEvaluator._check_salary(job, search_profile):
            logger.debug(f"Job '{job.title}' failed salary filter")
            return False
        
        # Exclusions filter
        if not FilterEvaluator._check_exclusions(job, search_profile):
            logger.debug(f"Job '{job.title}' failed exclusions filter")
            return False
        
        # Location filter
        if not FilterEvaluator._check_location(job, search_profile):
            logger.debug(f"Job '{job.title}' failed location filter")
            return False
        
        # Date posted filter
        if not FilterEvaluator._check_date_posted(job, search_profile):
            logger.debug(f"Job '{job.title}' failed date posted filter")
            return False
        
        # Job type filter
        if not FilterEvaluator._check_job_type(job, search_profile):
            logger.debug(f"Job '{job.title}' failed job type filter")
            return False
        
        logger.debug(f"Job '{job.title}' passed all hard filters")
        return True
    
    @staticmethod
    def _check_salary(job: Job, search_profile: SearchProfile) -> bool:
        """
        Check if job salary meets minimum requirement.
        
        Returns:
            True if salary passes (or not specified), False if fails
        """
        if search_profile.salary_min is None:
            return True  # No salary requirement
        
        if job.salary is None:
            # No salary info - assume it might be OK (don't reject)
            return True
        
        # Try to extract salary from job.salary string
        # Common formats: "$100,000", "100k-150k", "$100,000 - $150,000", etc.
        salary_min = FilterEvaluator._extract_salary_min(job.salary)
        
        if salary_min is None:
            # Couldn't parse salary - assume it passes
            return True
        
        # Check if salary meets requirement
        return salary_min >= search_profile.salary_min
    
    @staticmethod
    def _extract_salary_min(salary_string: str) -> Optional[int]:
        """
        Extract minimum salary from common salary formats.
        
        Handles: "$100,000", "100k", "$100,000 - $150,000", etc.
        
        Returns:
            Salary in integer form, or None if couldn't parse
        """
        if not salary_string:
            return None
        
        # Remove common currency symbols and text
        cleaned = salary_string.replace("$", "").replace("€", "").replace("£", "").lower()
        
        # Look for numbers
        numbers = re.findall(r'\d+(?:,\d{3})*(?:\.\d+)?', cleaned.replace(",", ""))
        
        if not numbers:
            return None
        
        try:
            # First number is usually the minimum
            first_num = numbers[0].replace(",", "")
            
            # If it ends with 'k', multiply by 1000
            if "k" in cleaned:
                return int(float(first_num) * 1000)
            
            return int(float(first_num))
        except (ValueError, IndexError):
            return None
    
    @staticmethod
    def _check_exclusions(job: Job, search_profile: SearchProfile) -> bool:
        """
        Check if job description contains any excluded keywords.
        
        Returns:
            True if no exclusions found, False if any exclusion found
        """
        if not search_profile.exclusions:
            return True  # No exclusions specified
        
        description = (job.description + " " + (job.requirements or "")).lower()
        
        # Check if any exclusion keyword appears in description
        for exclusion in search_profile.exclusions:
            if exclusion.lower() in description:
                return False  # Excluded keyword found
        
        return True  # No exclusions found
    
    @staticmethod
    def _check_location(job: Job, search_profile: SearchProfile) -> bool:
        """
        Check if job location matches profile requirements.
        
        Returns:
            True if location matches (or not specified), False if doesn't match
        """
        # If no location requirement, pass
        if not (search_profile.country or search_profile.region or search_profile.remote_pref):
            return True
        
        job_location = job.location.lower()

        # Remote preference (if specified, must be met)
        if search_profile.remote_pref == "remote":
            if not _is_remote(job):
                return False  # Want remote, but job isn't remote

        elif search_profile.remote_pref == "hybrid":
            if not _is_remote(job) and "hybrid" not in job_location:
                return False  # Want hybrid/remote, but job is on-site
        
        # A posting listed only as "Remote" names no place at all. Requiring a
        # region to appear in it as well rejects most remote listings — the
        # exact combination someone asking for remote work in a country will
        # choose. Where the location says nothing about geography, the region
        # filter has nothing to test, so it stays out of the way and the
        # posting is surfaced for review rather than silently discarded.
        location_is_only_remote = job_location.strip(" ,-()") in {
            "remote", "fully remote", "remote work", "anywhere",
        }

        # Region requirement (if specified, must be present)
        if search_profile.region and not location_is_only_remote:
            if not _location_names_region(job_location, search_profile.region):
                return False  # Region doesn't match

        # Country requirement (if specified, must be present)
        if search_profile.country and not location_is_only_remote:
            if not _location_names_region(job_location, search_profile.country):
                return False  # Country doesn't match

        return True
    
    @staticmethod
    def _check_date_posted(job: Job, search_profile: SearchProfile) -> bool:
        """
        Check if job was posted within acceptable timeframe.
        
        Returns:
            True if within timeframe (or not specified), False if too old
        """
        if search_profile.date_posted_within_days is None:
            return True  # No date requirement
        
        if job.posted_at is None:
            # No posted date - assume it's recent enough
            return True
        
        # Calculate cutoff date
        cutoff = utcnow() - timedelta(days=search_profile.date_posted_within_days)
        
        # Check if job was posted after cutoff
        return job.posted_at >= cutoff
    
    @staticmethod
    def _check_job_type(job: Job, search_profile: SearchProfile) -> bool:
        """
        Check if job type matches profile requirement.
        
        Returns:
            True if job type matches (or not specified), False if doesn't match
        """
        if search_profile.job_type is None:
            return True  # No job type requirement
        
        if job.job_type is None:
            # No job type specified in job - assume it's OK
            return True
        
        # Check if job type matches
        return job.job_type.lower() == search_profile.job_type.lower()


# How a posting says it is remote when its location field does not.
#
# Aggregators fill `location` with the employer's office and put the
# remoteness in the title: "Remote Customer Service Representative" at
# "Mountain View, CA, US" is a remote job. Reading only the location rejected
# every one of them — a search for remote customer support returned twenty
# postings and filtered out all twenty, which reads as a broken agent.
_REMOTE_IN_TITLE = re.compile(
    r"\bremote\b|\bwork from home\b|\bwfh\b|\btelecommut|\bvirtual\b"
    r"|\bhome.based\b|\banywhere\b|\bdistributed\b",
    re.IGNORECASE,
)

# Weaker evidence, so it has to be explicit. The word "remote" somewhere in a
# long description is often about a remote *team* the role supports, or a
# benefit paragraph; these phrasings are about the role itself.
_REMOTE_IN_DESCRIPTION = re.compile(
    r"(100%|fully|entirely|completely)\s+remote"
    r"|remote(\s+first|-first)"
    r"|this (is a|role is) .{0,20}remote"
    r"|work from home|telecommut|work remotely|remote position"
    r"|remote (role|job|opportunity|work arrangement)",
    re.IGNORECASE,
)


def _is_remote(job: Job) -> bool:
    """
    Whether a posting is remote, wherever it happens to say so.

    Args:
        job: The posting

    Returns:
        True when the location, the title, the job type or an explicit
        statement in the description says the role is remote
    """
    location = (job.location or "").lower()

    if "remote" in location or "anywhere" in location:
        return True

    if _REMOTE_IN_TITLE.search(job.title or ""):
        return True

    if "remote" in (job.job_type or "").lower():
        return True

    return bool(_REMOTE_IN_DESCRIPTION.search(job.description or ""))


def evaluate_hard_filters(job: Job, search_profile: SearchProfile) -> bool:
    """
    Convenience function to evaluate hard filters.
    
    Args:
        job: Job posting to evaluate
        search_profile: Search profile with filter criteria
    
    Returns:
        True if job passes all filters, False otherwise
    """
    return FilterEvaluator.evaluate(job, search_profile)
