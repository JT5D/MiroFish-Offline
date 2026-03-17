"""
Reusable File-Based IPC (Inter-Process Communication)
Extracted from MiroFish-Offline — standalone, zero-dependency module.

Implements a simple command/response pattern via the filesystem:
  1. Client writes command JSON to commands/ directory
  2. Server polls commands/, executes, writes response to responses/
  3. Client polls responses/ for the matching command_id

Works across any two processes on the same machine — no sockets, no
message brokers, no dependencies beyond the stdlib.

Usage (client side):
    from tools.ipc import IPCClient, CommandType

    client = IPCClient("/tmp/my_simulation")
    response = client.send_command(CommandType.INTERVIEW, {"agent_id": 1, "prompt": "Hello"})

Usage (server side):
    from tools.ipc import IPCServer, CommandStatus

    server = IPCServer("/tmp/my_simulation")
    server.start()
    cmd = server.poll_commands()
    if cmd:
        server.send_success(cmd.command_id, {"answer": "Hi there"})
    server.stop()
"""

import os
import json
import time
import uuid
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class CommandType(str, Enum):
    """Extensible command types. Add your own as needed."""
    INTERVIEW = "interview"
    BATCH_INTERVIEW = "batch_interview"
    CLOSE_ENV = "close_env"


class CommandStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class IPCCommand:
    command_id: str
    command_type: CommandType
    args: Dict[str, Any]
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": self.command_type.value,
            "args": self.args,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IPCCommand":
        return cls(
            command_id=data["command_id"],
            command_type=CommandType(data["command_type"]),
            args=data.get("args", {}),
            timestamp=data.get("timestamp", datetime.now().isoformat()),
        )


@dataclass
class IPCResponse:
    command_id: str
    status: CommandStatus
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command_id": self.command_id,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IPCResponse":
        return cls(
            command_id=data["command_id"],
            status=CommandStatus(data["status"]),
            result=data.get("result"),
            error=data.get("error"),
            timestamp=data.get("timestamp", datetime.now().isoformat()),
        )


class IPCClient:
    """
    Client side — sends commands and waits for responses.
    """

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.commands_dir = os.path.join(base_dir, "ipc_commands")
        self.responses_dir = os.path.join(base_dir, "ipc_responses")
        os.makedirs(self.commands_dir, exist_ok=True)
        os.makedirs(self.responses_dir, exist_ok=True)

    def send_command(
        self,
        command_type: CommandType,
        args: Dict[str, Any],
        timeout: float = 60.0,
        poll_interval: float = 0.5,
    ) -> IPCResponse:
        """Send a command and block until response or timeout."""
        command_id = str(uuid.uuid4())
        command = IPCCommand(command_id=command_id, command_type=command_type, args=args)

        command_file = os.path.join(self.commands_dir, f"{command_id}.json")
        with open(command_file, "w", encoding="utf-8") as f:
            json.dump(command.to_dict(), f, ensure_ascii=False, indent=2)

        logger.info("Sent IPC command: %s id=%s", command_type.value, command_id)

        response_file = os.path.join(self.responses_dir, f"{command_id}.json")
        start = time.time()

        while time.time() - start < timeout:
            if os.path.exists(response_file):
                try:
                    with open(response_file, "r", encoding="utf-8") as f:
                        response = IPCResponse.from_dict(json.load(f))
                    for f_path in (command_file, response_file):
                        try:
                            os.remove(f_path)
                        except OSError:
                            pass
                    logger.info("IPC response received: id=%s status=%s", command_id, response.status.value)
                    return response
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning("Bad response file: %s", e)
            time.sleep(poll_interval)

        try:
            os.remove(command_file)
        except OSError:
            pass
        raise TimeoutError(f"IPC timeout after {timeout}s for command {command_id}")

    def check_alive(self) -> bool:
        """Check if the server has written an 'alive' status."""
        status_file = os.path.join(self.base_dir, "env_status.json")
        if not os.path.exists(status_file):
            return False
        try:
            with open(status_file, "r", encoding="utf-8") as f:
                return json.load(f).get("status") == "alive"
        except (json.JSONDecodeError, OSError):
            return False


class IPCServer:
    """
    Server side — polls for commands and writes responses.
    """

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.commands_dir = os.path.join(base_dir, "ipc_commands")
        self.responses_dir = os.path.join(base_dir, "ipc_responses")
        os.makedirs(self.commands_dir, exist_ok=True)
        os.makedirs(self.responses_dir, exist_ok=True)
        self._running = False

    def start(self):
        self._running = True
        self._write_status("alive")

    def stop(self):
        self._running = False
        self._write_status("stopped")

    def _write_status(self, status: str):
        with open(os.path.join(self.base_dir, "env_status.json"), "w", encoding="utf-8") as f:
            json.dump({"status": status, "timestamp": datetime.now().isoformat()}, f, indent=2)

    def poll_commands(self) -> Optional[IPCCommand]:
        """Return the oldest pending command, or None."""
        if not os.path.exists(self.commands_dir):
            return None

        entries = []
        for name in os.listdir(self.commands_dir):
            if name.endswith(".json"):
                path = os.path.join(self.commands_dir, name)
                entries.append((path, os.path.getmtime(path)))
        entries.sort(key=lambda x: x[1])

        for path, _ in entries:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return IPCCommand.from_dict(json.load(f))
            except (json.JSONDecodeError, KeyError, OSError) as e:
                logger.warning("Skipping bad command file %s: %s", path, e)
        return None

    def send_response(self, response: IPCResponse):
        with open(os.path.join(self.responses_dir, f"{response.command_id}.json"), "w", encoding="utf-8") as f:
            json.dump(response.to_dict(), f, ensure_ascii=False, indent=2)
        try:
            os.remove(os.path.join(self.commands_dir, f"{response.command_id}.json"))
        except OSError:
            pass

    def send_success(self, command_id: str, result: Dict[str, Any]):
        self.send_response(IPCResponse(command_id=command_id, status=CommandStatus.COMPLETED, result=result))

    def send_error(self, command_id: str, error: str):
        self.send_response(IPCResponse(command_id=command_id, status=CommandStatus.FAILED, error=error))
