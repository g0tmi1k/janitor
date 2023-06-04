from typing import Any

async def is_gce_instance() -> bool: ...
async def gce_external_ip() -> str: ...

class EmptyQueue(Exception): ...
class AssignmentFailure(Exception): ...
class ResultUploadFailure(Exception): ...

def abort_run(client: Client, run_id: str, metadata: Any, description: str) -> None: ...

class Client(object):
    def __new__(cls, base_url: str, username: str | None, password: str | None, user_agent: str) -> Client: ...

    def get_assignment_raw(self, my_url: str | None, node_name: str,
                           jenkins_build_url: str | None, codebase: str |
                           None, campaign: str | None) -> Any: ...

    def upload_results(self, run_id: str, metadata: Any, output_directory: str | None = None) -> Any: ...
