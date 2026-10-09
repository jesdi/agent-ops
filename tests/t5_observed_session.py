"""Run the public supervisor entry point in this process for branch coverage.

The listener and independent native provider remain external processes connected
through their real Unix sockets. Only the supervisor's process boundary moves.
"""
from concurrent.futures import ThreadPoolExecutor

from dispatcher.codex_supervisor import main
from t5_executable_support import ExecutableSession


class ObservedSession(ExecutableSession):
    def __init__(self, monkeypatch, **kwargs):
        super().__init__(**kwargs)
        self.patch = monkeypatch.context()
        self.environment = self.patch.__enter__()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.future = None

    def spawn(self, args, name):
        if name != "supervisor":
            return super().spawn(args, name)
        for key, value in self.env.items():
            self.environment.setenv(key, value)
        arguments = args[args.index("dispatcher.codex_supervisor") + 1:]
        self.future = self.executor.submit(main, arguments)
        return None

    def __exit__(self, *error):
        try:
            if self.future is not None:
                if self.backend_control.exists():
                    self.backend("shutdown")
                self.future.result(timeout=10)
        finally:
            super().__exit__(*error)
            self.executor.shutdown(wait=True)
            self.patch.__exit__(*error)
