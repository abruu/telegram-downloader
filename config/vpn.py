"""
config/vpn.py — OpenVPN connection manager.

Usage:
    from config.vpn import VpnManager
    vpn = VpnManager()
    await vpn.start()     # connect
    await vpn.stop()      # disconnect
    vpn.is_connected()    # status check

The manager is a no-op when settings.VPN_ENABLED is False.
"""

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Optional

from config.settings import settings

log = logging.getLogger("dlbot.vpn")

# Minimum seconds between automatic reconnect attempts
_RECONNECT_COOLDOWN = 30
# Seconds to wait for tun interface to come up after openvpn starts
_CONNECT_TIMEOUT = 60
# Interval (seconds) for the background health-check loop
_MONITOR_INTERVAL = 15


def _openvpn_binary() -> Optional[str]:
    """Return the path to the openvpn binary, or None if not found."""
    return shutil.which("openvpn")


def _tun_is_up() -> bool:
    """
    Return True when at least one tun/tap interface exists, which
    indicates an active OpenVPN tunnel.
    """
    try:
        import socket
        import fcntl
        import struct
        # /proc/net/dev is available on every Linux system
        with open("/proc/net/dev") as f:
            for line in f:
                iface = line.split(":")[0].strip()
                if iface.startswith(("tun", "tap")):
                    return True
    except Exception:
        pass
    return False


class VpnManager:
    """
    Manages an OpenVPN subprocess.

    Configuration is read from ``config.settings``:
        VPN_ENABLED       – master switch (bool)
        VPN_CONFIG_FILE   – path to .ovpn file (required when enabled)
        VPN_AUTH_FILE     – path to credentials file (username\\npassword),
                            optional when the .ovpn embeds credentials
        VPN_RECONNECT     – auto-reconnect on failure (bool, default True)
        VPN_MAX_RETRIES   – max consecutive reconnect attempts (int, default 5)
    """

    def __init__(self) -> None:
        self._process: Optional[asyncio.subprocess.Process] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._retry_count: int = 0
        self._last_reconnect: float = 0.0
        self._stopping: bool = False

    # ── Public API ────────────────────────────────────────────────

    def is_enabled(self) -> bool:
        return bool(settings.VPN_ENABLED)

    def is_connected(self) -> bool:
        """True when the OpenVPN process is alive AND the tun interface is up."""
        if not self.is_enabled():
            return False
        proc_alive = self._process is not None and self._process.returncode is None
        return proc_alive and _tun_is_up()

    async def start(self) -> bool:
        """
        Connect to VPN.  Returns True on success, False if disabled or failed.
        Raises RuntimeError on configuration errors.
        """
        if not self.is_enabled():
            log.info("[VPN] Disabled — skipping.")
            return False

        self._validate_config()
        self._stopping = False
        self._retry_count = 0

        connected = await self._connect()
        if connected:
            self._monitor_task = asyncio.create_task(self._monitor_loop())
        return connected

    async def stop(self) -> None:
        """Disconnect from VPN and cancel the monitor task."""
        if not self.is_enabled():
            return

        self._stopping = True
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None

        await self._kill_process()
        log.info("[VPN] Disconnected.")

    def status_line(self) -> str:
        """One-line human-readable status for startup messages."""
        if not self.is_enabled():
            return "disabled"
        if self.is_connected():
            return f"connected (config={settings.VPN_CONFIG_FILE})"
        return "not connected"

    # ── Internal ──────────────────────────────────────────────────

    def _validate_config(self) -> None:
        binary = _openvpn_binary()
        if binary is None:
            raise RuntimeError(
                "[VPN] 'openvpn' binary not found. "
                "Install it with: sudo apt install openvpn"
            )

        cfg = settings.VPN_CONFIG_FILE
        if not cfg:
            raise RuntimeError(
                "[VPN] VPN_CONFIG_FILE is not set. "
                "Add it to your .env file."
            )
        if not Path(cfg).is_file():
            raise RuntimeError(
                f"[VPN] Config file not found: {cfg}"
            )

        auth = settings.VPN_AUTH_FILE
        if auth and not Path(auth).is_file():
            raise RuntimeError(
                f"[VPN] Auth file not found: {auth}"
            )

    async def _connect(self) -> bool:
        """Launch openvpn subprocess and wait for tunnel to come up."""
        await self._kill_process()

        binary = _openvpn_binary()
        cfg    = settings.VPN_CONFIG_FILE
        auth   = settings.VPN_AUTH_FILE

        # Split tunneling: only route Telegram IPs through VPN
        # Prevents VPN from becoming the default gateway for ALL traffic.
        # Run via sudo (needed to create tun device) WITHOUT --daemon so
        # we hold the process handle and can monitor/kill it directly.
        vpn_log = "/tmp/openvpn-bot.log"
        cmd = [
            "sudo", binary,
            "--config", cfg,
            "--log", vpn_log,
            "--route-nopull",  # Don't accept server's routing config
            "--route", "149.154.160.0", "255.255.240.0",  # Telegram DC1-DC5
            "--route", "91.108.4.0", "255.255.252.0",     # Telegram additional
            "--route", "91.108.56.0", "255.255.252.0",    # Telegram additional
            "--script-security", "2",
            "--pull-filter", "ignore", "redirect-gateway",
        ]
        if auth:
            cmd += ["--auth-user-pass", auth]

        log.info(f"[VPN] Starting OpenVPN (split tunnel - Telegram only): {' '.join(cmd)}")
        log.info(f"[VPN] OpenVPN log: {vpn_log}")
        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception as exc:
            log.error(f"[VPN] Failed to launch openvpn: {exc}")
            return False

        # Wait up to _CONNECT_TIMEOUT seconds for tun interface to appear
        deadline = time.monotonic() + _CONNECT_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            if _tun_is_up():
                log.info("[VPN] Tunnel is UP ✅")
                self._retry_count = 0
                return True
            # Process died before tunnel came up — real failure
            if self._process.returncode is not None:
                log.error(
                    f"[VPN] openvpn exited unexpectedly "
                    f"(rc={self._process.returncode}). "
                    f"Check {vpn_log} for details."
                )
                return False

        log.error(
            f"[VPN] Timed out waiting for tunnel after {_CONNECT_TIMEOUT}s."
        )
        await self._kill_process()
        return False

    async def _kill_process(self) -> None:
        """Terminate the openvpn daemon process.

        With --daemon, the subprocess we launched is just the parent that
        forked into the background (rc=0). The actual running daemon must
        be killed via pkill targeting the config file path.
        """
        cfg = settings.VPN_CONFIG_FILE or ""
        try:
            # Kill the real openvpn daemon by config file path
            kill_proc = await asyncio.create_subprocess_exec(
                "sudo", "pkill", "-f", f"openvpn.*{cfg}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(kill_proc.wait(), timeout=5)
        except Exception:
            pass

        # Also clean up the tracked subprocess if still alive
        if self._process is not None and self._process.returncode is None:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
        self._process = None

    async def _monitor_loop(self) -> None:
        """Background task — checks connectivity and reconnects as needed."""
        log.info("[VPN] Monitor started.")
        while not self._stopping:
            await asyncio.sleep(_MONITOR_INTERVAL)
            if self._stopping:
                break

            if not self.is_connected():
                log.warning("[VPN] Tunnel is DOWN.")

                if not settings.VPN_RECONNECT:
                    log.warning("[VPN] Auto-reconnect disabled — stopping monitor.")
                    break

                max_retries = settings.VPN_MAX_RETRIES
                if max_retries > 0 and self._retry_count >= max_retries:
                    log.error(
                        f"[VPN] Reached max reconnect attempts ({max_retries}). "
                        f"Giving up."
                    )
                    break

                now = time.monotonic()
                if now - self._last_reconnect < _RECONNECT_COOLDOWN:
                    wait = _RECONNECT_COOLDOWN - (now - self._last_reconnect)
                    log.info(f"[VPN] Cooldown — waiting {wait:.0f}s before retry.")
                    await asyncio.sleep(wait)

                self._retry_count += 1
                self._last_reconnect = time.monotonic()
                log.info(
                    f"[VPN] Reconnect attempt {self._retry_count}"
                    + (f"/{max_retries}" if max_retries > 0 else "")
                )
                await self._connect()

        log.info("[VPN] Monitor stopped.")
