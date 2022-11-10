from typing import Mapping, Any
import logging

log = logging.getLogger(__name__)


class JujuRunResult:
    """Utility for handling juju run/run-action results from 2.9 or 3.0 controllers."""

    def __init__(self, action):
        """Wrap an action action's results."""
        self._action = action

    @property
    def status(self) -> str:
        """Pass through to the action's status."""
        return self._action.status

    @property
    def results(self) -> Mapping[str, Any]:
        """Pass through to the action's results."""
        return self._action.results

    @property
    def code(self) -> str:
        """Return code from the process."""
        code = self.results.get("Code", self.results.get("return-code"))
        if code is None:
            log.error(f"Failed to find the return code in {self.results}")
            return -1
        return int(code)

    @property
    def stdout(self) -> str:
        """Return stdout from the process."""
        stdout = self.results.get("Stdout", self.results.get("stdout")) or ""
        return stdout.strip()

    @property
    def stderr(self) -> str:
        """Return stderr from the process."""
        stderr = self.results.get("Stderr", self.results.get("stderr")) or ""
        return stderr.strip()

    @property
    def output(self) -> str:
        """Return output from the process."""
        return self.stderr or self.stdout

    @property
    def success(self) -> bool:
        """Return True if completed successfully."""
        return self.status == "completed" and self.code == 0

    def __repr__(self) -> str:
        """Return a string repr."""
        return f"JujuRunResult({self._action})"
