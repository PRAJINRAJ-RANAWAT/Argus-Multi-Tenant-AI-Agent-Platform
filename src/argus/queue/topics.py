"""Stream names. One stream per job type, each with a dead-letter stream."""

JOBS_REVIEW = "jobs:review"
JOBS_QA = "jobs:qa"
JOBS_INDEX = "jobs:index"
JOBS_EVAL = "jobs:eval"

ALL_STREAMS = (JOBS_REVIEW, JOBS_QA, JOBS_INDEX, JOBS_EVAL)


def dlq(stream: str) -> str:
    return f"{stream}:dlq"
