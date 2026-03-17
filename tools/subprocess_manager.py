"""
Reusable Subprocess Manager
Extracted from MiroFish-Offline — standalone, stdlib-only module.

Handles the full lifecycle of spawning and managing long-running subprocesses:
  - Cross-platform process group spawning (Unix: start_new_session, Windows: CREATE_NEW_PROCESS_GROUP)
  - Graceful termination with escalation (SIGTERM → wait → SIGKILL)
  - atexit + signal handler registration for clean shutdown
  - Stdout/stderr capture to log files
  - Process registry for managing multiple concurrent processes

Usage:
    from tools.subprocess_manager import SubprocessManager

    mgr = SubprocessManager()
    pid = mgr.spawn("sim_001", ["python", "run_simulation.py", "--config", "sim.json"],
                     cwd="/path/to/sim", log_dir="/path/to/logs")
    print(mgr.is_running("sim_001"))  # True
    mgr.stop("sim_001", timeout=10)
    mgr.stop_all()
"""

import os
import sys
import signal
import atexit
import logging
import subprocess
from typing import Dict, Optional, List
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock

logger = logging.getLogger(__name__)


@dataclass
class ManagedProcess:
    """Tracks a managed subprocess."""
    name: str
    process: subprocess.Popen
    pid: int
    started_at: datetime
    cwd: Optional[str] = None
    stdout_path: Optional[str] = None
    stderr_path: Optional[str] = None
    _stdout_fh: Optional[object] = field(default=None, repr=False)
    _stderr_fh: Optional[object] = field(default=None, repr=False)


class SubprocessManager:
    """
    Manages long-running subprocesses with graceful lifecycle control.

    Features:
      - Cross-platform process group spawning for clean tree termination
      - Graceful shutdown: SIGTERM → timeout → SIGKILL (Unix) / taskkill (Windows)
      - atexit handler to kill all processes on parent exit
      - Signal handler registration (SIGINT, SIGTERM)
      - File handle management for stdout/stderr logs
      - Thread-safe process registry
    """

    def __init__(self, register_cleanup: bool = True):
        self._processes: Dict[str, ManagedProcess] = {}
        self._lock = Lock()
        self._cleanup_registered = False

        if register_cleanup:
            self._register_cleanup()

    def _register_cleanup(self):
        """Register atexit and signal handlers for clean shutdown."""
        if self._cleanup_registered:
            return

        atexit.register(self.stop_all)

        if sys.platform != "win32":
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    prev = signal.getsignal(sig)

                    def handler(signum, frame, prev_handler=prev):
                        self.stop_all()
                        if callable(prev_handler) and prev_handler not in (signal.SIG_DFL, signal.SIG_IGN):
                            prev_handler(signum, frame)

                    signal.signal(sig, handler)
                except (OSError, ValueError):
                    pass

        self._cleanup_registered = True

    def spawn(
        self,
        name: str,
        cmd: List[str],
        cwd: Optional[str] = None,
        log_dir: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> int:
        """
        Spawn a subprocess with process group isolation.

        Args:
            name:    Unique name for this process (used as key for stop/status).
            cmd:     Command + args list.
            cwd:     Working directory for the subprocess.
            log_dir: Directory for stdout/stderr log files. If None, pipes are discarded.
            env:     Optional environment variables (merged with os.environ).

        Returns:
            Process PID.

        Raises:
            ValueError: If a process with this name is already running.
        """
        with self._lock:
            if name in self._processes and self._processes[name].process.poll() is None:
                raise ValueError(f"Process '{name}' is already running (PID {self._processes[name].pid})")

        # Build environment
        proc_env = os.environ.copy()
        if env:
            proc_env.update(env)

        # Set up log files
        stdout_fh = None
        stderr_fh = None
        stdout_path = None
        stderr_path = None

        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
            stdout_path = os.path.join(log_dir, f"{name}_stdout.log")
            stderr_path = os.path.join(log_dir, f"{name}_stderr.log")
            stdout_fh = open(stdout_path, "w", encoding="utf-8")
            stderr_fh = open(stderr_path, "w", encoding="utf-8")

        # Spawn with process group isolation
        kwargs = {
            "stdout": stdout_fh or subprocess.DEVNULL,
            "stderr": stderr_fh or subprocess.DEVNULL,
            "cwd": cwd,
            "env": proc_env,
        }

        if sys.platform != "win32":
            kwargs["start_new_session"] = True
        else:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        process = subprocess.Popen(cmd, **kwargs)

        managed = ManagedProcess(
            name=name,
            process=process,
            pid=process.pid,
            started_at=datetime.now(),
            cwd=cwd,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            _stdout_fh=stdout_fh,
            _stderr_fh=stderr_fh,
        )

        with self._lock:
            self._processes[name] = managed

        logger.info("Spawned process '%s' (PID %d): %s", name, process.pid, " ".join(cmd))
        return process.pid

    def is_running(self, name: str) -> bool:
        """Check if a named process is still running."""
        with self._lock:
            proc = self._processes.get(name)
            if not proc:
                return False
            return proc.process.poll() is None

    def get_pid(self, name: str) -> Optional[int]:
        """Get the PID of a named process, or None if not found."""
        with self._lock:
            proc = self._processes.get(name)
            return proc.pid if proc else None

    def stop(self, name: str, timeout: float = 10.0) -> bool:
        """
        Gracefully stop a named process.

        Unix: SIGTERM → wait(timeout) → SIGKILL
        Windows: taskkill /F /T /PID

        Returns True if the process was stopped, False if not found.
        """
        with self._lock:
            proc = self._processes.get(name)
            if not proc:
                return False

        if proc.process.poll() is not None:
            self._cleanup_handles(proc)
            return True

        logger.info("Stopping process '%s' (PID %d)...", name, proc.pid)

        try:
            if sys.platform != "win32":
                # Send SIGTERM to process group
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    proc.process.terminate()

                try:
                    proc.process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    logger.warning("Process '%s' didn't stop gracefully, sending SIGKILL", name)
                    try:
                        pgid = os.getpgid(proc.pid)
                        os.killpg(pgid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        proc.process.kill()
                    proc.process.wait(timeout=5)
            else:
                # Windows: taskkill with /T for tree kill
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, timeout=timeout,
                )
        except Exception as e:
            logger.error("Error stopping process '%s': %s", name, e)

        self._cleanup_handles(proc)
        logger.info("Process '%s' stopped.", name)
        return True

    def stop_all(self):
        """Stop all managed processes."""
        with self._lock:
            names = list(self._processes.keys())

        for name in names:
            try:
                self.stop(name, timeout=5)
            except Exception as e:
                logger.error("Error stopping '%s' during cleanup: %s", name, e)

    def _cleanup_handles(self, proc: ManagedProcess):
        """Close stdout/stderr file handles."""
        for fh in (proc._stdout_fh, proc._stderr_fh):
            if fh:
                try:
                    fh.close()
                except Exception:
                    pass

    def list_processes(self) -> Dict[str, dict]:
        """Return a dict of all managed processes with their status."""
        with self._lock:
            result = {}
            for name, proc in self._processes.items():
                result[name] = {
                    "pid": proc.pid,
                    "running": proc.process.poll() is None,
                    "started_at": proc.started_at.isoformat(),
                    "stdout_log": proc.stdout_path,
                    "stderr_log": proc.stderr_path,
                }
            return result
