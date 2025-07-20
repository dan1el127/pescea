"""Escea fireplace device discovery."""

import asyncio
import logging
import socket

from abc import abstractmethod, ABC
from asyncio import (
    AbstractEventLoop,
    Condition,
    Future,
    Task,
    Lock,
)
from async_timeout import timeout
from logging import Logger
from typing import Dict, List, Set, Optional
import psutil

# Pescea imports:
from .controller import Controller
from .datagram import Datagram
from .message import Message, CommandID

# Interface for fireplace search (under normal circumstances)
DISCOVERY_SLEEP = 5 * 60.0

# Shorter interval (if we've lost comms to a controller)
DISCOVERY_RESCAN = 5.0

_LOG = logging.getLogger(__name__)  # type: Logger


class LogExceptions:
    """Utility context manager to log and discard exceptions"""

    def __init__(self, func: str) -> None:
        self.func = func

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type:
            _LOG.exception("Exception ignored when calling listener %s", self.func)
        return True


class Listener:
    """Base class for listeners for Escea updates"""

    def controller_discovered(self, ctrl: Controller) -> None:
        """
        New controller discovered. This will also be called for all
        existing controllers if a new listener is registered
        """

    def controller_disconnected(self, ctrl: Controller, ex: Exception) -> None:
        """
        Connection lost to controller. Exception argument will show reason why.
        """

    def controller_reconnected(self, ctrl: Controller) -> None:
        """
        Reconnected to controller.
        """

    def controller_update(self, ctrl: Controller) -> None:
        """Called when a system update message is received from the controller.
        Controller data will be set to new value.
        """


class AbstractDiscoveryService(ABC):
    """Interface for discovery.

    This service is both a context manager, and an asynchronous context
    manager. When used in the context manager version, the start
    discovery and close will be called automatically when opening
    and closing the context respectively.
    """

    @abstractmethod
    def add_listener(self, listener: Listener) -> None:
        """Add a listener.

        All existing controllers will be passed to the listener."""

    @abstractmethod
    def remove_listener(self, listener: Listener) -> None:
        """Remove a listener"""

    @abstractmethod
    async def start_discovery(self) -> None:
        """Async version to start discovery.
        Will return once discovery is started, but before any controllers
        are found.
        """

    @abstractmethod
    async def rescan(self) -> None:
        """Trigger rescan for new controllers / update IP addresses of
        existing controllers.

        Returns immediately, listener will be called with any new
        controllers or if reconnected.
        """

    @abstractmethod
    async def close(self) -> None:
        """Stop discovery.

        As these are all UDP comms, there are no open connections to close.

        Returns immediately, but closing off controllers may take time
        """

    @property
    def is_closed(self) -> bool:
        """Return true if closed"""

    @property
    def controllers(self) -> Dict[str, Controller]:
        """Dictionary of all the currently discovered controllers"""


class DiscoveryService(AbstractDiscoveryService, Listener):
    """Discovery protocol class. Not for external use."""

    def __init__(self, ip_addr: str | None = None) -> None:
        """Start the discovery protocol

        Args:
            ip_addr (str | None): Address of controller. Defaults to None which will broadcast to all interfaces
        raises:
            RuntimeError: If attempted to start the protocol when it is
                          already running.
        """
        self._controllers = {}  # type: Dict[str, Controller]
        self._disconnected_uids = set()  # type: Set[str]
        self._listeners = []  # type: List[Listener]
        self._close_task = None  # type: Optional[Task]

        _LOG.info("Starting discovery protocol")
        self.loop = asyncio.get_running_loop()
        self.sending_lock = Lock()

        self._ip_addr = ip_addr
        self._datagram = Datagram(self.loop, ip_addr, self.sending_lock)

        self._discovery_started = False
        self._scan_condition = Condition()  # type: Condition

        self._tasks = []  # type: List[Future]

    # Async context manager interface
    async def __aenter__(self) -> AbstractDiscoveryService:
        await self.start_discovery()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.close()

    def _task_done_callback(self, task: Task):
        if task.exception():
            _LOG.exception("Uncaught exception", exc_info=task.exception())
        self._tasks.remove(task)

    # managing the task list (also called by controller for poll_loop)
    def create_task(self, coro) -> Task:
        """Create a task in the event loop. Keeps track of created tasks."""
        task = self.loop.create_task(coro)  # type: Task
        self._tasks.append(task)

        task.add_done_callback(self._task_done_callback)
        return task

    # Listeners.
    def add_listener(self, listener: Listener) -> None:
        """Add a discovered listener.

        All existing controllers will be passed to the listener."""
        self._listeners.append(listener)

        def callback():
            for controller in self._controllers.values():
                listener.controller_discovered(controller)

        self.loop.call_soon(callback)

    def remove_listener(self, listener: Listener) -> None:
        """Remove a listener"""
        self._listeners.remove(listener)

    def controller_discovered(self, ctrl: Controller) -> None:
        _LOG.info("New controller found: id=%s ip=%s", ctrl.device_uid, ctrl.device_ip)
        for listener in self._listeners:
            with LogExceptions("controller_discovered"):
                listener.controller_discovered(ctrl)

    def controller_disconnected(self, ctrl: Controller, ex: Exception) -> None:
        _LOG.debug(
            "Connection to controller lost: id=%s ip=%s",
            ctrl.device_uid,
            ctrl.device_ip,
        )
        self._disconnected_uids.add(ctrl.device_uid)
        self.create_task(self._rescan())
        for listener in self._listeners:
            with LogExceptions("controller_disconnected"):
                listener.controller_disconnected(ctrl, ex)

    def controller_reconnected(self, ctrl: Controller) -> None:
        _LOG.debug(
            "Controller reconnected: id=%s ip=%s", ctrl.device_uid, ctrl.device_ip
        )
        self._disconnected_uids.remove(ctrl.device_uid)
        for listener in self._listeners:
            with LogExceptions("controller_reconnected"):
                listener.controller_reconnected(ctrl)

    def controller_update(self, ctrl: Controller) -> None:
        for listener in self._listeners:
            with LogExceptions("controller_update"):
                listener.controller_update(ctrl)

    @property
    def controllers(self) -> Dict[str, Controller]:
        """Dictionary of all the currently discovered controllers"""
        return self._controllers

    @staticmethod
    def get_broadcast_addresses() -> list[str]:
        """Get a list of broadcast addresses by iterating over interfaces"""
        broadcast_addrs = set()

        for interface_name, interface_addrs in psutil.net_if_addrs().items():
            for addr in interface_addrs:
                if addr.family == socket.AF_INET and addr.broadcast:
                    _LOG.debug(
                        "Found broadcast address: %s for %s",
                        addr.broadcast,
                        interface_name,
                    )
                    broadcast_addrs.add(addr.broadcast)

        _LOG.debug("Found broadcast addresses: %s", broadcast_addrs)

        return list(broadcast_addrs)

    async def start_discovery(self) -> None:
        """Non-context manager version for starting discovery"""
        if not self._discovery_started:
            self._discovery_started = True
            self.create_task(self._scan_loop())

    async def _scan_loop(self) -> None:
        """Scan loop to search for fireplaces"""
        while not self._close_task:
            await self._send_broadcast()

            try:
                async with timeout(
                    DISCOVERY_RESCAN
                    if len(self._disconnected_uids) > 0
                    else DISCOVERY_SLEEP
                ):
                    # Allows interrupt when need to rescan
                    async with self._scan_condition:
                        await self._scan_condition.wait()
            except asyncio.TimeoutError:
                pass

    async def _send_broadcast(self):
        """Send UDP commands to IPs to search for fires"""
        # If self._ip_addr is not None, we are not targeting a specific IP, so get list of broadcast IPs
        ip_addresses = (
            self.get_broadcast_addresses() if not self._ip_addr else [self._ip_addr]
        )
        all_responses = {}

        # Loop over each IP address and send a broadcast listening for reply from a Fire
        for ip_addr in ip_addresses:
            _LOG.debug(
                "Sending discovery message to addr %s",
                ip_addr,
            )
            # Temporarily set the datagram IP to the IP address we want to broadcast
            original_ip = self._datagram.ip
            self._datagram.set_ip(ip_addr)
            try:
                responses = await self._datagram.send_command(
                    CommandID.SEARCH_FOR_FIRES
                )
                all_responses.update(responses)
            except ConnectionError:
                _LOG.debug(
                    "No controllers responded at %s",
                    ip_addr,
                )
            finally:
                # Restore original IP, even if an exception occurred
                self._datagram.set_ip(original_ip)

        if all_responses:
            for ip_addr, resp in all_responses.items():
                self._discovery_received(resp, ip_addr)
        else:
            _LOG.warning("No controllers responded.")

    async def rescan(self) -> None:
        """Request a rescan of fireplaces (eg after losing comms)"""
        if self.is_closed:
            raise ConnectionError("Already closed")
        _LOG.debug("Manual rescan of controllers triggered.")
        await self._rescan()

    async def _rescan(self) -> None:
        """Interrupt the scan loop so does immediate search for fireplaces"""
        async with self._scan_condition:
            self._scan_condition.notify()

    async def close(self) -> None:
        """Request local scan loop and controllers to exit gracefully"""
        if self._close_task:
            # Already called, so wait completion
            await self._close_task
            return
        _LOG.info("Close called on discovery service.")
        # Request controllers to exit
        self._close_task = asyncio.current_task(loop=self.loop)
        for _, ctrl in self._controllers.items():
            await ctrl.close()
        # Request local scan loop to exit
        await self._rescan()
        if len(self._tasks) > 0:
            await asyncio.wait(self._tasks)

    @property
    def is_closed(self) -> bool:
        return self._close_task is not None

    def _discovery_received(self, data: Message, addr):
        """Process the received discovery response from a fireplace.
        Start up a controller if don't already have one
        """
        device_ip = addr
        device_uid = data.serial_number  # Used as the unique id

        if device_uid not in self._controllers:
            controller = Controller(self, device_uid=device_uid, device_ip=device_ip)

            async def initialize_controller():
                try:
                    await controller.initialize()
                except ConnectionError as ex:
                    _LOG.warning(
                        "Can't connect to discovered server at IP '%s' exception: %s",
                        device_ip,
                        repr(ex),
                    )
                    return

                self._controllers[device_uid] = controller
                self.controller_discovered(controller)

            self.create_task(initialize_controller())
        else:
            controller = self._controllers[device_uid]
            controller.refresh_address(device_ip)


def discovery_service(
    *listeners: Listener, ip_addr: str | None = None
) -> AbstractDiscoveryService:
    """Create discovery service. Returned object is an asynchronous
    context manager so can be used with 'async with' statement.
    Alternately call start_discovery or start_discovery_async to commence
    the discovery process."""
    service = DiscoveryService(ip_addr=ip_addr)
    for listener in listeners:
        service.add_listener(listener)
    return service
