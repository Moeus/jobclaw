"""Re-export domain models for existing imports."""

from jobclaw.domain import Application, ApplicationStatus, Job, JobSource, Match, SalaryRange

__all__ = ["Application", "ApplicationStatus", "Job", "JobSource", "Match", "SalaryRange"]
