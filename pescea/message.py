"""Escea Fireplace Message module"""

import logging

from enum import Enum

_LOG = logging.getLogger(__name__)

# The same message structure is used for commands and responses:
MESSAGE_LENGTH = 15
MSG_OFFSET_START_BYTE = 0  # Byte 1: Start Byte code
MSG_OFFSET_ID = 1  # Byte 2: Command / Response ID
MSG_OFFSET_DATA_LENGTH = 2  # Byte 3: Data Length

# Byte 4..13: Data (0 filled / don'tcare after Data Length)
MSG_OFFSET_DATA_START = 3
MSG_OFFSET_DATA_END = 12

# Byte 14: CRC (Sum Bytes 2 to 13, overflowing on 256)
MSG_OFFSET_CRC = 13
MSG_OFFSET_END_BYTE = 14  # Byte 15: End Byte code

# Data structure for STATUS response:
# Data Byte 1: (boolean) Fireplace has new timers (not used)
DATA_OFFSET_TIMERS = 0
DATA_OFFSET_FIRE_ON = 1  # Data Byte 2: (boolean) Fire is On
DATA_OFFSET_BOOST_ON = 2  # Data Byte 3: (boolean) Fan Boost is on
DATA_OFFSET_EFFECT_ON = 3  # Data Byte 4: (boolean) Flame Effect is on

# Data Byte 5: (unsigned int) Desired Temperature
DATA_OFFSET_DESIRED_TEMP = 4

# Data Byte 6: (unsigned int) Room Temperature
DATA_OFFSET_CURRENT_TEMP = 5

# Data structure for I_AM_A_FIRE response:
# Data Bytes 1..4: (Unsigned Long, big Endian) Serial Number (use for UID)
DATA_OFFSET_SERIAL = 0
# Data Byte 5..6: (Unsigned Long, big Endian) PIN (not used)
DATA_OFFSET_PIN = 4

# Preconfigured start/end characters:
MESSAGE_START_BYTE = 0x47
MESSAGE_END_BYTE = 0x46

# Valid command identifiers:
class CommandID(Enum):
    STATUS_PLEASE = 0x31
    POWER_ON = 0x39
    POWER_OFF = 0x3A
    SEARCH_FOR_FIRES = 0x50
    FAN_BOOST_ON = 0x37
    FAN_BOOST_OFF = 0x38
    FLAME_EFFECT_ON = 0x56
    FLAME_EFFECT_OFF = 0x55
    NEW_SET_TEMP = 0x57


class ResponseID(Enum):
    STATUS = 0x80
    POWER_ON_ACK = 0x8D
    POWER_OFF_ACK = 0x8F
    FAN_BOOST_ON_ACK = 0x89
    FAN_BOOST_OFF_ACK = 0x8B
    FLAME_EFFECT_ON_ACK = 0x61
    FLAME_EFFECT_OFF_ACK = 0x60
    NEW_SET_TEMP_ACK = 0x66
    I_AM_A_FIRE = 0x90


# Acceptable limits when commanding NEW_SET_TEMP:
MIN_SET_TEMP = 4
MAX_SET_TEMP = 30


def expected_response(command: CommandID) -> ResponseID:
    """Utility function to check correct response
    Raises ValueError (if unexpected CommandID)
    """
    if command == CommandID.STATUS_PLEASE:
        return ResponseID.STATUS
    elif command == CommandID.POWER_ON:
        return ResponseID.POWER_ON_ACK
    elif command == CommandID.POWER_OFF:
        return ResponseID.POWER_OFF_ACK
    elif command == CommandID.SEARCH_FOR_FIRES:
        return ResponseID.I_AM_A_FIRE
    elif command == CommandID.FAN_BOOST_ON:
        return ResponseID.FAN_BOOST_ON_ACK
    elif command == CommandID.FAN_BOOST_OFF:
        return ResponseID.FAN_BOOST_OFF_ACK
    elif command == CommandID.FLAME_EFFECT_ON:
        return ResponseID.FLAME_EFFECT_ON_ACK
    elif command == CommandID.FLAME_EFFECT_OFF:
        return ResponseID.FLAME_EFFECT_OFF_ACK
    elif command == CommandID.NEW_SET_TEMP:
        return ResponseID.NEW_SET_TEMP_ACK
    else:
        raise (ValueError)


class Message:

    """Implements messages to and from the fireplace.
    Refer to Escea Fireplace LAN Comms Spec for details.
    """

    def _initialise_data(self) -> None:
        """Default all attributes"""
        self._is_command = None
        self._id = None
        self._has_new_timers = None
        self._fan_boost_on = None
        self._effect_on = None
        self._desired_temp = None
        self._current_temp = None
        self._serial = None
        self._pin = None
        self._bytearray = None
        self._crc_sum = None

    def __init__(
        self,
        command: CommandID = None,
        set_temp: int = None,
        incoming: bytearray = None,
    ) -> None:
        """Creates either a Command or a Response message

        Args: Either command or incoming must be set
        """

        if command is not None:
            self._create_command(command, set_temp)

        elif incoming is not None:
            self._parse_incoming(incoming)

        else:
            raise (ValueError, "Invalid constructor")

    def _create_command(self, command: CommandID, set_temp: float = None):
        """Create a command (outgoing) message.
        Args:
        - command: valid command code to fireplace
        - set_temp: desired temperature (only applies to that command)

        Use the property bytearray_of to get the bytes to send.
        """
        self._initialise_data()

        self._is_command = True
        self._id = command
        self._desired_temp = set_temp  # Only used for NEW_SET_TEMP

        # Build outgoing message:

        self._bytearray = bytearray(MESSAGE_LENGTH)

        self._bytearray[MSG_OFFSET_START_BYTE] = MESSAGE_START_BYTE
        self._bytearray[MSG_OFFSET_END_BYTE] = MESSAGE_END_BYTE

        self._bytearray[MSG_OFFSET_ID] = (self._id).value

        if self._id == CommandID.NEW_SET_TEMP:
            if set_temp is None or set_temp < MIN_SET_TEMP or set_temp > MAX_SET_TEMP:
                raise ValueError
            self._bytearray[MSG_OFFSET_DATA_LENGTH] = 1
            self._bytearray[MSG_OFFSET_DATA_START] = set_temp

        # Calculate CRC
        self._crc_sum = 0
        for i in range(MSG_OFFSET_ID, MSG_OFFSET_DATA_END):
            self._crc_sum += self._bytearray[i]
        self._crc_sum = self._crc_sum % 256
        self._bytearray[MSG_OFFSET_CRC] = self._crc_sum

    def _parse_incoming(self, incoming: bytearray):
        """Create a response Message from incoming buffer

        Note:
            Can also be used with a Command bytearray for test purposes

        Raises:
            ValueError if message content does not match specification
        """
        self._initialise_data()

        self._bytearray = incoming

        # Check message integrity
        if len(incoming) != MESSAGE_LENGTH:
            raise ValueError(
                "Message: '{}' has incorrect message length: {} (expecting {})".format(
                    incoming.hex(), len(incoming), MESSAGE_LENGTH
                )
            )

        if incoming[MSG_OFFSET_START_BYTE] != MESSAGE_START_BYTE:
            raise ValueError(
                "Message: '{}' has invalid start byte: {} (expecting {})".format(
                    incoming.hex(), incoming[MSG_OFFSET_START_BYTE], MESSAGE_START_BYTE
                )
            )

        if incoming[MSG_OFFSET_END_BYTE] != MESSAGE_END_BYTE:
            raise ValueError(
                "Message: '{}' has invalid end byte: {} (expecting {})".format(
                    incoming.hex(), incoming[MSG_OFFSET_END_BYTE], MESSAGE_END_BYTE
                )
            )

        # Check CRC
        self._crc_sum = 0
        for i in range(MSG_OFFSET_ID, MSG_OFFSET_DATA_END):
            self._crc_sum += incoming[i]

        self._crc_sum = self._crc_sum % 256
        if self._crc_sum != incoming[MSG_OFFSET_CRC]:
            raise ValueError(
                "Message: '{}' has invalid CRC: {} (expecting {})".format(
                    incoming.hex(), incoming[MSG_OFFSET_CRC], self._crc_sum
                )
            )

        id_value = incoming[MSG_OFFSET_ID]
        if id_value in ResponseID._value2member_map_:

            # Normal use: Decode incoming responses from fireplace
            self._parse_response(incoming)

        elif incoming[MSG_OFFSET_ID] in CommandID._value2member_map_:
            # For Test use only: Decode incoming command bytearray
            self._parse_command(incoming)

        else:
            raise ValueError("Invalid message id: {}".format(id_value))

    def _parse_response(self, incoming: bytearray):
        """Normal use case - decode incoming response from fireplace"""

        self._is_command = False
        self._id = ResponseID(incoming[MSG_OFFSET_ID])

        # Extract data
        if (self._id) == ResponseID.STATUS:
            if incoming[MSG_OFFSET_DATA_LENGTH] != 6:
                raise ValueError(
                    "Message: '{}' has invalid data length: {} (expecting 6)".format(
                        incoming.hex(), incoming[MSG_OFFSET_DATA_LENGTH]
                    )
                )

            self._has_new_timers = bool(
                incoming[MSG_OFFSET_DATA_START + DATA_OFFSET_TIMERS]
            )
            self._fire_on = bool(incoming[MSG_OFFSET_DATA_START + DATA_OFFSET_FIRE_ON])
            self._fan_boost_on = bool(
                incoming[MSG_OFFSET_DATA_START + DATA_OFFSET_BOOST_ON]
            )
            self._effect_on = bool(
                incoming[MSG_OFFSET_DATA_START + DATA_OFFSET_EFFECT_ON]
            )
            self._desired_temp = int(
                incoming[MSG_OFFSET_DATA_START + DATA_OFFSET_DESIRED_TEMP]
            )
            self._current_temp = int(
                incoming[MSG_OFFSET_DATA_START + DATA_OFFSET_CURRENT_TEMP]
            )

        elif (self._id) == ResponseID.I_AM_A_FIRE:
            if incoming[MSG_OFFSET_DATA_LENGTH] != 6:
                # Just log this... there is an error on the fireplace side here
                _LOG.debug(
                    "Message: %s Has Invalid Data Length: %s (expecting 6)",
                    incoming.hex(),
                    str(incoming[MSG_OFFSET_DATA_LENGTH]),
                )
            self._serial = int.from_bytes(
                incoming[
                    MSG_OFFSET_DATA_START
                    + DATA_OFFSET_SERIAL : MSG_OFFSET_DATA_START
                    + DATA_OFFSET_SERIAL
                    + 4
                ],
                byteorder="big",
                signed=False,
            )
            self._pin = int.from_bytes(
                incoming[
                    MSG_OFFSET_DATA_START
                    + DATA_OFFSET_PIN : MSG_OFFSET_DATA_START
                    + DATA_OFFSET_PIN
                    + 2
                ],
                byteorder="big",
                signed=False,
            )

        else:
            if int(incoming[MSG_OFFSET_DATA_LENGTH]) != 0:
                raise ValueError(
                    "Message: '{}' has invalid data length: {} (expecting 0)".format(
                        incoming.hex(), int(incoming[MSG_OFFSET_DATA_LENGTH])
                    )
                )

    def _parse_command(self, incoming: bytearray):
        """For Test use only: Decode incoming command bytearray"""
        self._is_command = True
        self._id = CommandID(incoming[MSG_OFFSET_ID])

        if self._id == CommandID.NEW_SET_TEMP:
            self._desired_temp = incoming[MSG_OFFSET_DATA_START]

        if incoming[MSG_OFFSET_DATA_LENGTH] != (
            1 if self._id == CommandID.NEW_SET_TEMP else 0
        ):
            raise ValueError(
                "Message: '{}' has invalid data length: {} (expecting 1)".format(
                    incoming.hex(), incoming[MSG_OFFSET_DATA_LENGTH]
                )
            )

    @property
    def is_command(self) -> bool:
        return self._is_command

    @property
    def is_response(self) -> bool:
        return not self._is_command

    @property
    def command_id(self) -> CommandID:
        if self._is_command:
            return self._id
        else:
            raise (ValueError)

    @property
    def response_id(self) -> ResponseID:
        if not self._is_command:
            return self._id
        else:
            raise (ValueError)

    @property
    def has_new_timers(self) -> bool:
        return self._has_new_timers

    @property
    def fire_is_on(self) -> bool:
        return self._fire_on

    @property
    def fan_boost_is_on(self) -> bool:
        return self._fan_boost_on

    @property
    def flame_effect(self) -> bool:
        return self._effect_on

    @property
    def desired_temp(self) -> int:
        return self._desired_temp

    @property
    def current_temp(self) -> int:
        return self._current_temp

    @property
    def serial_number(self) -> int:
        return self._serial

    @property
    def pin(self) -> int:
        return self._pin

    @property
    def crc(self) -> int:
        return self._crc_sum

    @property
    def bytearray_(self) -> bytearray:
        return self._bytearray

    # Internal use test methods follow:

    def mock_response(
        response_id: ResponseID,
        uid: int = 0,
        has_new_timers: bool = False,
        fire_on: bool = False,
        fan_boost_on: bool = False,
        effect_on: bool = False,
        desired_temp: int = MAX_SET_TEMP,
        current_temp: int = MIN_SET_TEMP,
        force_crc_error: bool = False,
        force_id_error: bool = False,
        force_data_len_error: bool = False,
        force_start_byte_error: bool = False,
        force_end_byte_error: bool = False,
    ) -> bytearray:
        """Create a dummy message for testing purposes."""

        message = bytearray(MESSAGE_LENGTH)

        if force_start_byte_error:
            message[MSG_OFFSET_START_BYTE] = 0
        else:
            message[MSG_OFFSET_START_BYTE] = MESSAGE_START_BYTE

        if force_end_byte_error:
            message[MSG_OFFSET_END_BYTE] = 0
        else:
            message[MSG_OFFSET_END_BYTE] = MESSAGE_END_BYTE

        if force_id_error:
            message[MSG_OFFSET_ID] = 0
        else:
            message[MSG_OFFSET_ID] = response_id.value

        if response_id == ResponseID.STATUS:
            message[MSG_OFFSET_DATA_LENGTH] = 6
            message[MSG_OFFSET_DATA_START] = desired_temp
            message[MSG_OFFSET_DATA_START + DATA_OFFSET_TIMERS] = has_new_timers
            message[MSG_OFFSET_DATA_START + DATA_OFFSET_FIRE_ON] = fire_on
            message[MSG_OFFSET_DATA_START + DATA_OFFSET_BOOST_ON] = fan_boost_on
            message[MSG_OFFSET_DATA_START + DATA_OFFSET_EFFECT_ON] = effect_on
            message[MSG_OFFSET_DATA_START + DATA_OFFSET_DESIRED_TEMP] = desired_temp
            message[MSG_OFFSET_DATA_START + DATA_OFFSET_CURRENT_TEMP] = current_temp

        elif response_id == ResponseID.I_AM_A_FIRE:
            message[MSG_OFFSET_DATA_LENGTH] = 6
            serial_segment = uid.to_bytes(length=4, byteorder="big", signed=False)
            pin_segment = int(9999).to_bytes(length=2, byteorder="big", signed=False)
            for i in range(len(serial_segment)):
                message[
                    MSG_OFFSET_DATA_START + DATA_OFFSET_SERIAL + i
                ] = serial_segment[i]
            for i in range(len(pin_segment)):
                message[MSG_OFFSET_DATA_START + DATA_OFFSET_PIN + i] = pin_segment[i]

        else:
            message[MSG_OFFSET_DATA_LENGTH] = 0

        if force_data_len_error:
            message[MSG_OFFSET_DATA_LENGTH] += 1

        if force_crc_error:
            message[MSG_OFFSET_CRC] = 0
        else:
            # Calculate CRC
            crc_sum = 0
            for i in range(MSG_OFFSET_ID, MSG_OFFSET_DATA_END):
                crc_sum += message[i]
            crc_sum = crc_sum % 256
            message[MSG_OFFSET_CRC] = crc_sum

        return message
