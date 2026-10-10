"""Abstract base class for job application adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Self

from jobclaw.models import Application, Job


class BaseApplier(ABC):
    """Common applier interface for the Boss chat workflow."""

    @abstractmethod
    async def apply(self, job: Job, resume_text: str) -> Application:
        """Submit an application for the given job.

        Args:
            job: The job to apply to.
            resume_text: The unstructured candidate resume.

        Returns:
            Application object with status.
        """
        ...

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass
