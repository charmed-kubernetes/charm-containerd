from typing import Mapping, Any


class JujuRunResult:
    def __init__(self, action):
        self._action = action

    @property
    def status(self) -> str:
        return self._action.status

    @property
    def results(self) -> Mapping[str, Any]:
        return self._action.results

    @property
    def code(self) -> str:
        code = self.results.get("Code", self.results.get("return-code"))
        if code is None:
            log.error(f"Failed to find the return code in {self.results}")
            return -1
        return int(code)

    @property
    def stdout(self) -> str:
        stdout = self.results.get("Stdout", self.results.get("stdout")) or ""
        return stdout.strip()

    @property
    def stderr(self) -> str:
        stderr = self.results.get("Stderr", self.results.get("stderr")) or ""
        return stderr.strip()

    @property
    def output(self) -> str:
        return self.stderr or self.stdout

    @property
    def success(self) -> bool:
        return self.status == "completed" and self.code == 0

    def __repr__(self) -> str:
        return f"JujuRunResult({self._action})"