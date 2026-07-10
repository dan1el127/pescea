"""Escea Network Controller module"""

import logging
import asyncio

from enum import Enum
from typing import Dict, Union
from time import time
from async_timeout import timeout
from copy import deepcopy

# Pescea imports:
from .message import (
    Message,
    CommandID,
    ResponseID,
    MIN_SET_TEMP,
    MAX_SET_TEMP,
    expected_response,
)
from .datagram import Datagram

_LOG = logging.getLogger(__name__)


# ===========================================================================
# TEMPORARY live-test tracing — REMOVE before release.
# WARNING level so lines show in the standard HA log with no extra logger
# config. Fixed prefix so the operator can `grep ESCEA-TRACE` the HA log.
# ===========================================================================
def _trace(msg, *args):
    _LOG.warning("[ESCEA-TRACE] " + msg, *args)


# Seconds between (internal) updates under normal conditions
# TEMPORARY: dropped from 30.0 to 4.0 for live-testing so the operator gets
# fine-grained samples of fire_is_on through the post-off cool-down window.
# REVERT to 30.0 before release.
REFRESH_INTERVAL = 4.0

# Seconds between publishing changes to listeners
# - Updates due to changes happen immediately
NOTIFY_REFRESH_INTERVAL = 5 * 60.0

# Retry rate when first stop getting responses
# - this is needed as UDP is not guaranteed and expect missed messages
RETRY_INTERVAL = 10.0

# Timeout to stop retrying and reduce poll rate
# - plus notify listeners of disconnections
RETRY_TIMEOUT = 60.0

# Time to retry updates once we declare disconnection
DISCONNECTED_INTERVAL = 5 * 60.0

# Time to wait for fireplace to turn fire on / off
# - While busy, commands are stored locally, but not sent on
# - This period was measured from an Escea OEM remote control
ON_OFF_BUSY_WAIT_TIME = 66.0


class Controller:
    """Interface to Escea controller"""

    class Fan(Enum):
        """Supported fan modes"""

        FLAME_EFFECT = "FlameEffect"
        AUTO = "Auto"
        FAN_BOOST = "FanBoost"

    class State(Enum):
        """Controller states:

        Under normal operations:
            The Controller is READY:
                - The Controller sends commands directly to the Fireplace
                - The Controller polls at REFRESH_INTERVAL
        When toggling the fire power:
            The Controller remains BUSY for ON_OFF_BUSY_WAIT_TIME:
                - The Controller buffers requests but does not send to the Fireplace
        When responses are missed (expected as we are using UDP datagrams):
            The Controller is NON_RESPONSIVE
                - The Controller will poll at a (quicker) retry rate
        When there are no comms for a prolonged period:
            The Controller enters DISCONNECTED state
                - The Controller will continue to poll at a reduced rate
                - The Controller buffers requests but cannot send to the Fireplace
        """

        BUSY = "BusyWaiting"
        READY = "Ready"
        NON_RESPONSIVE = "NonResponsive"
        DISCONNECTED = "Disconnected"

    class Settings(Enum):
        """Available controller system settings - Internal Use Only"""

        IP_ADDRESS = "IPAddress"
        DEVICE_UID = "DeviceUId"
        CONTROLLER_STATE = "State"
        HAS_NEW_TIMERS = "HasNewTimers"
        FIRE_IS_ON = "FireIsOn"
        FAN_MODE = "FanMode"
        DESIRED_TEMP = "DesiredTemp"
        CURRENT_TEMP = "CurrentTemp"

    Value = Union[str, int, float, bool, Fan]
    ControllerData = Dict[Settings, Value]

    def __init__(self, discovery, device_uid: str, device_ip: str) -> None:
        """Create a controller interface.

        Args:
            discovery: DiscoveryService() object implementing (at least):
                        - loop
                        - sending_lock
                        - controller_update (callback)
                        - controller_disconnected (callback)
                        - controller_reconnected (callback)
            device_uid: Controller UId as a string (Serial Number of unit)
            device_addr: Device network address. Usually specified as IP
                address
        """

        self._discovery = discovery
        self._system_settings = {}  # type: Controller.ControllerData
        self._prior_settings = {}  # type: Controller.ControllerData
        self._system_settings[Controller.Settings.IP_ADDRESS] = device_ip
        self._system_settings[Controller.Settings.DEVICE_UID] = device_uid

        self._datagram = Datagram(
            self._discovery.loop, device_ip, self._discovery.sending_lock
        )

        self._interrupt_poll_loop_sleep = asyncio.Condition()

        self._initialised = False

    async def initialize(self) -> None:
        """Initialize the controller, does not complete until the firplace has
        been contacted and current settings read.
        """

        self._state = Controller.State.READY
        self._last_response = 0.0  # Tracks last valid message received
        self._busy_end_time = 0.0  # Tracks when exit BUSY state
        self._last_update = 0.0  # To 'rate limit' the notifications to discovery
        self._closed = False  # To exit poll_loop when done

        # Read current state of fireplace
        await self._refresh_system(notify=False)

        self._initialised = True

        # Start regular polling for status updates
        self._poll_loop_task = self._discovery.loop.create_task(self._poll_loop())

    async def close(self):
        """Signal loop to exit, then wait till done"""
        if self._closed:
            return

        self._closed = True
        async with self._interrupt_poll_loop_sleep:
            self._interrupt_poll_loop_sleep.notify()
        await self._poll_loop_task

    async def _poll_loop(self) -> None:
        """Regularly poll for status update from fireplace.
        If Disconnected, retry based on how long ago we last had an update.
        """
        while not self._closed:

            try:
                await self._refresh_system()
            except Exception as exc:
                _LOG.exception("Unexpected exception (EXITING): %s", repr(exc))
                self._closed = True
                return

            _LOG.debug(
                "Polling unit %s at address %s (current state is %s)",
                self._system_settings[Controller.Settings.DEVICE_UID],
                self._system_settings[Controller.Settings.IP_ADDRESS],
                self._state,
            )

            if self._state == Controller.State.READY:
                sleep_time = REFRESH_INTERVAL
            elif self._state == Controller.State.NON_RESPONSIVE:
                sleep_time = RETRY_INTERVAL
            elif self._state == Controller.State.DISCONNECTED:
                sleep_time = DISCONNECTED_INTERVAL
            elif self._state == Controller.State.BUSY:
                sleep_time = max(self._busy_end_time - time(), 0.0)

            _trace(
                "_poll_loop: state=%s sleep=%.1fs busy_remaining=%.1fs",
                self._state,
                sleep_time,
                (self._busy_end_time - time())
                if self._state == Controller.State.BUSY
                else -1.0,
            )

            try:
                # Sleep for poll time, allow early wakeup
                async with timeout(sleep_time):
                    async with self._interrupt_poll_loop_sleep:
                        await self._interrupt_poll_loop_sleep.wait()
            except asyncio.TimeoutError:
                pass

    @property
    def device_ip(self) -> str:
        """IP Address of the unit"""
        return self._system_settings[Controller.Settings.IP_ADDRESS]

    @property
    def device_uid(self) -> str:
        """UId of the unit (serial number)"""
        return self._system_settings[Controller.Settings.DEVICE_UID]

    @property
    def discovery(self):
        """Handle to the discovery service"""
        return self._discovery

    @property
    def state(self) -> State:
        """Controller state"""
        return self._state

    @property
    def is_on(self) -> bool:
        """True if the fire is turned on"""
        return self._get_system_state(Controller.Settings.FIRE_IS_ON)

    async def set_on(self, value: bool) -> None:
        """Turn the fire on or off.
        Does not return until command has been sent to the fireplace.
        Note: After systems receives on or off command, must wait ON_OFF_BUSY_TIMEOUT
        before sending further commands (they will be buffered internally)
        """
        await self._set_system_state(Controller.Settings.FIRE_IS_ON, value)

    @property
    def fan(self) -> Fan:
        """The current fan level."""
        return self._get_system_state(Controller.Settings.FAN_MODE)

    async def set_fan(self, value: Fan) -> None:
        """The fan level.
        Does not return until command has been sent to the fireplace.

        Note: A peculiarity of this fireplace is that if we change the fan
        setting while off, it actually turns the fire on.
        """
        await self._set_system_state(Controller.Settings.FAN_MODE, value)

    @property
    def desired_temp(self) -> float:
        """fireplace DesiredTemp temperature."""
        return float(self._get_system_state(Controller.Settings.DESIRED_TEMP))

    async def set_desired_temp(self, value: float):
        """Fireplace DesiredTemp temperature.
        Does not return until command has been sent to the fireplace.

        This is the target temp for the fire
        Args:
            value: Valid settings are in range MIN_TEMP..MAX_TEMP
            at 1 degree increments (will be rounded)
        """
        degrees = round(value)
        if degrees < MIN_SET_TEMP or degrees > MAX_SET_TEMP:
            _LOG.error(
                "Desired Temp %s is out of range (%s-%s)",
                degrees,
                MIN_SET_TEMP,
                MAX_SET_TEMP,
            )
            return

        await self._set_system_state(Controller.Settings.DESIRED_TEMP, degrees)

    @property
    def current_temp(self) -> float:
        """The room air temperature"""
        return float(self._get_system_state(Controller.Settings.CURRENT_TEMP))

    @property
    def min_temp(self) -> float:
        """The minimum valid target (desired) temperature"""
        return float(MIN_SET_TEMP)

    @property
    def max_temp(self) -> float:
        """The maximum valid target (desired) temperature"""
        return float(MAX_SET_TEMP)

    async def _refresh_system(self, notify: bool = True) -> None:
        """Request fresh status from the fireplace.
        This is also where state changes are handled.

        Approach:

            if current state BUSY (and not timed out) -> return

            request status

            New status received:
                if prior state READY
                    update local system settings from received message
                else (prior state DISCONNECTED / NON_RESPONSIVE / BUSY (timeout))
                    sync buffered commands to fireplace
                    new state READY
                    if prior state DISCONNECTED:
                        notify discovery reconnected

            No status received:
                prior state *ANY*
                    if time since last response < RETRY_TIMEOUT
                        new state NON_RESPONSIVE
                    else
                        new state DISCONNECTED
                        notify discovery disconnected
        """
        if self._state != Controller.State.BUSY or time() >= self._busy_end_time:
            # Ok to fetch new status

            _trace(
                "_refresh_system: PROCEEDING with status read (prior_state=%s)",
                self._state,
            )
            prior_state = self._state
            response = await self._request_status()
            if (response is not None) and (response.response_id == ResponseID.STATUS):
                # We have a valid response - the controller is communicating

                _trace(
                    "_refresh_system: valid STATUS  prior_state=%s  "
                    "response.fire_is_on=%s  buffered FIRE_IS_ON=%s  buffered FAN_MODE=%s",
                    prior_state,
                    response.fire_is_on,
                    self._system_settings.get(Controller.Settings.FIRE_IS_ON),
                    self._system_settings.get(Controller.Settings.FAN_MODE),
                )

                self._state = Controller.State.READY

                # These values are readonly, so copy them in any case
                self._system_settings[
                    Controller.Settings.HAS_NEW_TIMERS
                ] = response.has_new_timers
                self._system_settings[
                    Controller.Settings.CURRENT_TEMP
                ] = response.current_temp

                # FIX (RCA update): NON_RESPONSIVE just means we missed some UDP
                # replies - nothing was buffered as a user command, so on recovery
                # we must TRUST the fresh status, not re-sync a stale buffer. Only
                # BUSY (a real buffered power toggle) and DISCONNECTED take the sync
                # path. This is what stops the remote-off relight: after a remote
                # off the unit goes NON_RESPONSIVE then reports fire_on=False, and
                # we now accept that instead of re-sending POWER_ON.
                if prior_state in (
                    Controller.State.READY,
                    Controller.State.NON_RESPONSIVE,
                ):

                    _trace(
                        "_refresh_system: BRANCH = NORMAL-UPDATE (prior=%s)",
                        prior_state,
                    )
                    # Normal operation, update our internal values
                    self._system_settings[
                        Controller.Settings.DESIRED_TEMP
                    ] = response.desired_temp
                    if response.fan_boost_is_on:
                        self._system_settings[
                            Controller.Settings.FAN_MODE
                        ] = Controller.Fan.FAN_BOOST
                    elif response.flame_effect:
                        self._system_settings[
                            Controller.Settings.FAN_MODE
                        ] = Controller.Fan.FLAME_EFFECT
                    else:
                        self._system_settings[
                            Controller.Settings.FAN_MODE
                        ] = Controller.Fan.AUTO
                    _trace(
                        "_refresh_system: NORMAL-UPDATE CLOBBER "
                        "local FIRE_IS_ON %s <- status %s",
                        self._system_settings.get(Controller.Settings.FIRE_IS_ON),
                        response.fire_is_on,
                    )
                    self._system_settings[
                        Controller.Settings.FIRE_IS_ON
                    ] = response.fire_is_on

                else:

                    _trace(
                        "_refresh_system: BRANCH = SYNC-BUFFERED (prior=%s)",
                        prior_state,
                    )
                    # We have come back to READY state.
                    # We need to try to sync buffered settings to fireplace

                    if (
                        response.desired_temp
                        != self._system_settings[Controller.Settings.DESIRED_TEMP]
                    ):
                        await self._set_system_state(
                            Controller.Settings.DESIRED_TEMP,
                            self._system_settings[Controller.Settings.DESIRED_TEMP],
                            sync=True,
                        )

                    if response.fan_boost_is_on:
                        response_fan = Controller.Fan.FAN_BOOST
                    elif response.flame_effect:
                        response_fan = Controller.Fan.FLAME_EFFECT
                    else:
                        response_fan = Controller.Fan.AUTO
                    if not self._system_settings[Controller.Settings.FIRE_IS_ON]:
                        # Escea controller has a quirk that turning on
                        # FAN_BOOST or FLAME_EFFECT actually turns on the fire
                        # ... so must avoid setting fan when 'synching' if want it to stay off
                        self._system_settings[
                            Controller.Settings.FAN_MODE
                        ] = response_fan
                    else:
                        if (
                            response_fan
                            != self._system_settings[Controller.Settings.FAN_MODE]
                        ):
                            await self._set_system_state(
                                Controller.Settings.FAN_MODE,
                                self._system_settings[Controller.Settings.FAN_MODE],
                                sync=True,
                            )

                    # Do power last, as then we go to BUSY state
                    buffered_on = self._system_settings[
                        Controller.Settings.FIRE_IS_ON
                    ]
                    if response.fire_is_on != buffered_on:
                        if not buffered_on:
                            # Buffer says OFF, unit reports ON -> re-send POWER_OFF.
                            # This direction is safe (never lights a gas fire).
                            _trace(
                                "_refresh_system: SYNC-BUFFERED power mismatch "
                                "buffered=False response=True -> RE-SEND POWER_OFF",
                            )
                            await self._set_system_state(
                                Controller.Settings.FIRE_IS_ON,
                                False,
                                sync=True,
                            )
                        else:
                            # Buffer says ON, unit reports OFF. FAIL-SAFE: never
                            # auto-relight a gas fire from a possibly-stale buffer.
                            # Accept the unit's OFF state instead of POWER_ON.
                            _trace(
                                "_refresh_system: FAIL-SAFE suppressing POWER_ON "
                                "re-send (buffered=True response=False); accepting "
                                "unit OFF",
                            )
                            self._system_settings[
                                Controller.Settings.FIRE_IS_ON
                            ] = response.fire_is_on
                    else:
                        _trace(
                            "_refresh_system: SYNC-BUFFERED power OK "
                            "(buffered=%s == response=%s, no re-send)",
                            buffered_on,
                            response.fire_is_on,
                        )

                    if prior_state == Controller.State.DISCONNECTED:
                        self._discovery.controller_reconnected(self)

            else:
                # No / invalid response, need to check if we need to change state
                #
                # FIX (availability): don't count the BUSY window against the
                # disconnect timer. While BUSY (66s) we intentionally skip polls,
                # so _last_response is frozen. Since ON_OFF_BUSY_WAIT_TIME (66s) >
                # RETRY_TIMEOUT (60s), the FIRST missed poll right after a toggle
                # would otherwise always be >RETRY_TIMEOUT old and jump straight to
                # DISCONNECTED (5-min backoff + entity goes unavailable), skipping
                # NON_RESPONSIVE entirely. The unit commonly goes briefly silent
                # after ignition, so this hit on nearly every HA on/off. Measuring
                # from max(_last_response, _busy_end_time) means a miss just after
                # BUSY is treated as NON_RESPONSIVE (10s retry) and recovers fast.
                last_ok = max(self._last_response, self._busy_end_time)
                if time() - last_ok < RETRY_TIMEOUT:
                    self._state = Controller.State.NON_RESPONSIVE
                else:
                    self._state = Controller.State.DISCONNECTED
                    if prior_state != Controller.State.DISCONNECTED:
                        self._discovery.controller_disconnected(self, TimeoutError)
                _trace(
                    "_refresh_system: BRANCH = NO/INVALID RESPONSE -> %s "
                    "(since last_ok=%.1fs)",
                    self._state,
                    time() - last_ok,
                )

        else:
            _trace(
                "_refresh_system: SKIPPING status read (BUSY, %.1fs left)",
                self._busy_end_time - time(),
            )

        if notify and self._state != Controller.State.DISCONNECTED:
            # send an update to discovery if there have been any changes
            # or if haven't sent one in last NOTIFY_REFRESH_INTERVAL
            changes_found = False
            for entry in self._system_settings:
                if not entry in self._prior_settings or (
                    self._prior_settings[entry] != self._system_settings[entry]
                ):
                    changes_found = True
                    break
            if changes_found or (time() - self._last_update > NOTIFY_REFRESH_INTERVAL):
                _trace(
                    "_refresh_system: NOTIFY controller_update (changes_found=%s) "
                    "FIRE_IS_ON=%s FAN_MODE=%s",
                    changes_found,
                    self._system_settings.get(Controller.Settings.FIRE_IS_ON),
                    self._system_settings.get(Controller.Settings.FAN_MODE),
                )
                self._last_update = time()
                self._prior_settings = deepcopy(self._system_settings)
                self._discovery.controller_update(self)

    async def _request_status(self) -> Message:
        """Send command to fireplace requesting current status"""
        _trace("_request_status: sent STATUS_PLEASE to %s", str(self.device_uid))
        try:
            responses = await self._datagram.send_command(CommandID.STATUS_PLEASE)
            if len(responses) > 0:
                this_response = next(iter(responses))  # only expecting one
                if responses[this_response].response_id == expected_response(
                    CommandID.STATUS_PLEASE
                ):
                    _LOG.debug(
                        "_request_status - send_command(success): %s",
                        str(self.device_uid),
                    )
                    self._last_response = time()
                    reply = responses[this_response]
                    _trace(
                        "_request_status: STATUS reply  fire_is_on=%s "
                        "fan_boost_is_on=%s flame_effect=%s desired_temp=%s "
                        "current_temp=%s",
                        reply.fire_is_on,
                        reply.fan_boost_is_on,
                        reply.flame_effect,
                        reply.desired_temp,
                        reply.current_temp,
                    )
                    return reply
        except ConnectionError:
            pass
        # If we get here... did not receive a response or not valid
        if self._state != Controller.State.DISCONNECTED:
            self._state = Controller.State.NON_RESPONSIVE
        _LOG.debug(
            "_request_status - send_command(failed): %s (now: %s)",
            str(self.device_uid),
            self._state,
        )
        _trace(
            "_request_status: NO/INVALID reply from %s (state now %s)",
            str(self.device_uid),
            self._state,
        )
        return None

    def refresh_address(self, address):
        """Called from discovery to point controller to the IP address"""
        if self._system_settings[Controller.Settings.IP_ADDRESS] == address:
            return

        self._datagram.set_ip(address)
        self._system_settings[Controller.Settings.IP_ADDRESS] = address

        # signal the poll loop to wake up and ask for new status
        async def signal_loop(self):
            async with self._interrupt_poll_loop_sleep:
                self._interrupt_poll_loop_sleep.notify()

        self._discovery.loop.create_task(signal_loop(self))

    def _get_system_state(self, state: Settings):
        """Locally stored (buffered) value, or received from fireplace"""
        return self._system_settings[state]

    async def _set_system_state(self, state: Settings, value, sync: bool = False):
        """Send command to fireplace to change given state.
        Args:
            sync: state/value have been buffered -> send to fireplace
        """

        _trace(
            "_set_system_state: ENTER state=%s from=%s to=%s sync=%s (ctrl_state=%s)",
            state,
            self._system_settings.get(state),
            value,
            sync,
            self._state,
        )

        # nothing to do if not synching our state, and already have right state
        if (not sync) and (self._system_settings[state] == value):
            _trace(
                "_set_system_state: EARLY-RETURN (already %s, not syncing)", value
            )
            return

        _LOG.debug(
            "_set_system_state - uid: %s | %s from:%s to:%s  (sync:%s)",
            str(self.device_uid),
            str(state),
            str(self._system_settings[state]),
            str(value),
            str(sync),
        )

        # save the new value internally
        self._system_settings[state] = value

        # send it to the fireplace if asked to sync, or the controller is not
        # mid-transition / offline.
        # FIX (responsiveness): also transmit while NON_RESPONSIVE. That state
        # only means we missed some UDP polls; the unit is very likely still
        # reachable, so a command pressed during a blip should go out NOW rather
        # than be silently buffered and only reconciled ~66s later at BUSY expiry
        # (which read as "I pressed off and nothing happened"). For a power toggle
        # a failed send still falls through to BUSY below, so the buffered intent
        # stays protected and the sync branch re-sends if this attempt missed.
        if sync or self._state in (
            Controller.State.READY,
            Controller.State.NON_RESPONSIVE,
        ):

            command = None

            if state == Controller.Settings.FIRE_IS_ON:
                if value:
                    command = CommandID.POWER_ON
                else:
                    command = CommandID.POWER_OFF

            elif state == Controller.Settings.DESIRED_TEMP:
                command = CommandID.NEW_SET_TEMP

            elif state == Controller.Settings.FAN_MODE:

                # Fan is implemented via separate FLAME_EFFECT and FAN_BOOST commands
                # Any change to Fan will take one or two separate commands:
                #
                # To AUTO:
                # 1. turn off FAN_BOOST
                if value == Controller.Fan.AUTO:
                    command = CommandID.FAN_BOOST_OFF

                # To FAN_BOOST:
                # 1. Turn off FLAME_EFFECT
                elif value == Controller.Fan.FAN_BOOST:
                    command = CommandID.FLAME_EFFECT_OFF

                # To FLAME_EFFECT:
                # 1. Turn off FAN_BOOST
                elif value == Controller.Fan.FLAME_EFFECT:
                    command = CommandID.FAN_BOOST_OFF

            else:
                raise (AttributeError, "Unexpected state: {0}".format(state))

            if command is not None:
                _trace("_set_system_state: sending command %s", command)
                valid_response = False
                try:
                    responses = await self._datagram.send_command(command, value)
                    if (len(responses) > 0) and (
                        responses[next(iter(responses))].response_id
                        == expected_response(command)
                    ):
                        valid_response = True
                        _LOG.debug(
                            "_set_system_state - send_command(success): %s -> %s",
                            str(self.device_uid),
                            str(command),
                        )

                except ConnectionError:
                    pass
                _trace(
                    "_set_system_state: command %s ack_valid=%s",
                    command,
                    valid_response,
                )
                if valid_response:
                    self._last_response = time()
                elif state == Controller.Settings.FIRE_IS_ON:
                    # Power toggle: even if this send missed, fall through to the
                    # BUSY entry below so the buffered power state is protected and
                    # the sync branch re-sends at BUSY expiry (never orphaned).
                    _trace(
                        "_set_system_state: power send missed, "
                        "falling through to BUSY (buffer protected)"
                    )
                else:
                    # temp/fan: a failed send aborts (nothing to protect via BUSY)
                    return

            if state == Controller.Settings.FAN_MODE:
                # Fan is implemented via separate FLAME_EFFECT and FAN_BOOST commands
                # Any change will take one or two separate commands:
                #
                # To AUTO:
                # 2. turn off FLAME_EFFECT
                if value == Controller.Fan.AUTO:
                    command = CommandID.FLAME_EFFECT_OFF

                # To FAN_BOOST:
                # 2. Turn on FAN_BOOST
                elif value == Controller.Fan.FAN_BOOST:
                    command = CommandID.FAN_BOOST_ON

                # To FLAME_EFFECT:
                # 2. Turn on FLAME_EFFECT
                else:
                    command = CommandID.FLAME_EFFECT_ON

                valid_response = False
                try:
                    responses = await self._datagram.send_command(command, value)
                    if (len(responses) > 0) and (
                        responses[next(iter(responses))].response_id
                        == expected_response(command)
                    ):
                        valid_response = True
                        _LOG.debug(
                            "_set_system_state - send_command(success): %s -> %s",
                            str(self.device_uid),
                            str(command),
                        )
                except ConnectionError:
                    pass
                if valid_response:
                    self._last_response = time()
                else:
                    return

        # FIX (RCA §4): for a power toggle, enter BUSY *before* the immediate
        # refresh. The unit keeps reporting fire_is_on=True during its post-off
        # cool-down, so an immediate status read here would clobber the just-
        # buffered power state (and bounce the HA entity back to "on"). Setting
        # BUSY first makes _refresh_system() skip the status read (BUSY guard).
        if state == Controller.Settings.FIRE_IS_ON:
            _trace(
                "_set_system_state: entering BUSY for %.0fs BEFORE refresh "
                "(buffered FIRE_IS_ON=%s)",
                ON_OFF_BUSY_WAIT_TIME,
                self._system_settings.get(Controller.Settings.FIRE_IS_ON),
            )
            self._state = Controller.State.BUSY
            self._busy_end_time = time() + ON_OFF_BUSY_WAIT_TIME

        # Need to refresh immediately after setting
        # (unless synching, in which case the poll loop will update)
        if not sync:
            _trace(
                "_set_system_state: POST-COMMAND immediate refresh (ctrl_state=%s)",
                self._state,
            )
            await self._refresh_system()
