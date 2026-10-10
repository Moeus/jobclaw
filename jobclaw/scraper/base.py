"""Abstract base class for job scrapers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Self

from jobclaw.models import Job, JobSource


class BaseScraper(ABC):
    """Common scraper interface for the Boss search workflow."""

    source: JobSource

    @abstractmethod
    def scrape_jobs(
        self,
        query: str,
        location: str | None = None,
        limit: int = 20,
        job_type: str = "all",
    ) -> list[Job]:
        """Scrape job listings matching the query.

        Args:
            query: Search keywords (e.g. "Python Engineer").
            location: Optional location filter.
            limit: Maximum number of jobs to return.
            job_type: Boss job type (all, full-time, internship, part-time).

        Returns:
            List of normalized Job objects.
        """
        ...

    def __enter__(self) -> Self:
        """Set up browser/session resources."""
        return self

    def __exit__(self, *args: object) -> None:
        """Tear down browser/session resources."""
