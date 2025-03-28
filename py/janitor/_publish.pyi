from datetime import datetime

def calculate_next_try_time(finish_time: datetime, attempt_count: int) -> datetime: ...
def get_merged_by_user_url(url: str, user: str) -> str | None: ...
def branches_match(url_a: str | None, url_b: str | None) -> bool: ...
def role_branch_url(url: str, remote_branch_name: str | None) -> str: ...

class RateLimiter:
    def set_mps_per_bucket(self, mps_per_bucket: dict[str, dict[str, int]]) -> None: ...
    def check_allowed(self, bucket: str) -> None: ...
    def inc(self, bucket: str) -> None: ...
    def get_stats(self) -> dict[str, tuple[int, int | None]]: ...

class SlowStartRateLimiter(RateLimiter):
    def __init__(self, mps_per_bucket: int | None) -> None: ...

class NonRateLimiter(RateLimiter):
    def __init__(self) -> None: ...

class FixedRateLimiter(RateLimiter):
    def __init__(self, mps_per_bucket: int) -> None: ...

class RateLimited(Exception):
    def __init__(self, message: str) -> None: ...

class BucketRateLimited(RateLimited):
    def __init__(self, bucket: str, open_mps: int, max_open_mps: int) -> None: ...

    bucket: str
    open_mps: int
    max_open_mps: int
