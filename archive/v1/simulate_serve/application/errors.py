class RepositoryPortError(RuntimeError):
    """A persistence integrity failure that must stop the batch."""


class ExecutorPortError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stage: str,
        retryable: bool = False,
        ambiguous_submit: bool = False,
        remote_task_id: str = "",
        remote_session_id: str = "",
        agent_id: str = "",
    ):
        self.stage = stage
        self.retryable = retryable
        self.ambiguous_submit = ambiguous_submit
        self.remote_task_id = remote_task_id
        self.remote_session_id = remote_session_id
        self.agent_id = agent_id
        super().__init__(message)
