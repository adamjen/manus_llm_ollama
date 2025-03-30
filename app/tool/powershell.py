import asyncio
import os
from typing import Optional

from app.exceptions import ToolError
from app.tool.base import BaseTool, CLIResult, ToolResult


_POWERSHELL_DESCRIPTION = """Execute a PowerShell command in the terminal.
* Long running commands: For commands that may run indefinitely, it should be run in the background and the output should be redirected to a file, e.g. command = `Start-Process -FilePath python -ArgumentList "app.py" -RedirectStandardOutput "server.log" -RedirectStandardError "error.log"`.
* Interactive: If a PowerShell command returns exit code `-1`, this means the process is not yet finished. The assistant must then send a second call to terminal with an empty `command` (which will retrieve any additional logs), or it can send additional text (set `command` to the text) to STDIN of the running process, or it can send command=`ctrl+c` to interrupt the process.  Note: Ctrl+C might not gracefully terminate all PowerShell processes; consider using `Stop-Process` if needed.
* Timeout: If a command execution result says "Command timed out. Sending SIGINT to the process", the assistant should retry running the command in the background.
"""


class _PowerShellSession:
    """A session of a PowerShell shell."""

    _started: bool
    _process: asyncio.subprocess.Process

    command: str = "powershell.exe"
    _output_delay: float = 0.2  # seconds
    _timeout: float = 120.0  # seconds
    _sentinel: str = "<<exit>>"

    def __init__(self):
        self._started = False
        self._timed_out = False

    async def start(self):
        if self._started:
            return

        self._process = await asyncio.create_subprocess_shell(
            self.command,
            shell=True,
            bufsize=0,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        self._started = True

    def stop(self):
        """Terminate the PowerShell shell."""
        if not self._started:
            raise ToolError("Session has not started.")
        if self._process.returncode is not None:
            return
        self._process.terminate()  # Or consider .kill() for forceful termination

    async def run(self, command: str):
        """Execute a command in the PowerShell shell."""
        if not self._started:
            raise ToolError("Session has not started.")
        if self._process.returncode is not None:
            return ToolResult(
                system="tool must be restarted",
                error=f"PowerShell has exited with returncode {self._process.returncode}",
            )
        if self._timed_out:
            raise ToolError(
                f"timed out: PowerShell has not returned in {self._timeout} seconds and must be restarted",
            )

        # we know these are not None because we created the process with PIPEs
        assert self._process.stdin
        assert self._process.stdout
        assert self._process.stderr

        # send command to the process
        # PowerShell uses UTF-16LE encoding by default
        self._process.stdin.write(
            command.encode('utf-16le') + f"; Write-Output '{self._sentinel}'\n".encode('utf-16le')
        )
        await self._process.stdin.drain()

        output = ""
        error = ""

        # read output from the process, until the sentinel is found
        try:
            async with asyncio.timeout(self._timeout):
                while True:
                    await asyncio.sleep(self._output_delay)
                    # Read stdout
                    stdout_buffer = await self._process.stdout.read(65536) # Read in chunks to avoid blocking
                    if stdout_buffer:
                        decoded_output_chunk = stdout_buffer.decode('utf-16le', errors='ignore')
                        output += decoded_output_chunk
                        if self._sentinel in output:
                            output = output[: output.index(self._sentinel)] # Strip sentinel
                            break

                    # Read stderr
                    stderr_buffer = await self._process.stderr.read(65536) # Read in chunks
                    if stderr_buffer:
                        decoded_error_chunk = stderr_buffer.decode('utf-16le', errors='ignore')
                        error += decoded_error_chunk


        except asyncio.TimeoutError:
            self._timed_out = True
            # Attempt to send Ctrl+C to the PowerShell process (might not always work perfectly)
            if os.name == 'nt': # Windows
                self._process.send_signal(signal.CTRL_BREAK_EVENT) # Or CTRL_C_EVENT, test which works better
            else: # Posix (Linux, macOS) - original code's behavior, might not be ideal for Windows
                self._process.send_signal(signal.SIGINT)

            raise ToolError(
                f"timed out: PowerShell has not returned in {self._timeout} seconds and must be restarted",
            ) from None


        if output.endswith("\n"):
            output = output[:-1]
        if error.endswith("\n"):
            error = error[:-1]


        # clear the buffers - might not be strictly necessary with chunked reads, but good practice
        # self._process.stdout._buffer.clear() # No _buffer with StreamReader in asyncio >= 3.8
        # self._process.stderr._buffer.clear() # No _buffer with StreamReader in asyncio >= 3.8

        return CLIResult(output=output, error=error)


import signal

class PowerShell(BaseTool):
    """A tool for executing PowerShell commands"""

    name: str = "powershell"
    description: str = _POWERSHELL_DESCRIPTION
    parameters: dict = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The PowerShell command to execute. Can be empty to view additional logs when previous exit code is `-1`. Can be `ctrl+c` to attempt to interrupt the currently running process.", # Note on Ctrl+C behavior
            },
        },
        "required": ["command"],
    }

    _session: Optional[_PowerShellSession] = None

    async def execute(
        self, command: str | None = None, restart: bool = False, **kwargs
    ) -> CLIResult:
        if restart:
            if self._session:
                self._session.stop()
            self._session = _PowerShellSession()
            await self._session.start()
            return ToolResult(system="tool has been restarted.")

        if self._session is None:
            self._session = _PowerShellSession()
            await self._session.start()

        if command is not None:
            if command.strip().lower() == 'ctrl+c':
                if self._session._process and self._session._process.returncode is None:
                    if os.name == 'nt':
                        self._session._process.send_signal(signal.CTRL_BREAK_EVENT) # Or CTRL_C_EVENT
                    else:
                        self._session._process.send_signal(signal.SIGINT)
                    return ToolResult(system="Interrupt signal sent to process.") # Informative message
                else:
                    return ToolResult(system="No active process to interrupt.") # Informative message
            return await self._session.run(command)

        raise ToolError("no command provided.")


if __name__ == "__main__":
    powershell = PowerShell()
    async def test_powershell():
        rst = await powershell.execute("Get-ChildItem -Attributes !Directory") # PowerShell command to list files
        print("List files command result:\n", rst)

        rst_pwd = await powershell.execute("Get-Location") # PowerShell command for current directory
        print("\nCurrent directory command result:\n", rst_pwd)

        rst_error = await powershell.execute("Get-ProcessDoesNotExist") # Example command that will produce an error
        print("\nError command result:\n", rst_error)

    asyncio.run(test_powershell())