"""Test Escea discovery service module functionality """

from asyncio import sleep
import socket
from unittest.mock import MagicMock, patch

import pytest
from pytest import mark

from pescea.controller import Controller
from pescea.discovery import DiscoveryService

from .conftest import fireplaces, patched_open_datagram_endpoint


@mark.asyncio
async def test_service_basics(mocker):

    mocker.patch(
        "pescea.udp_endpoints.open_datagram_endpoint", patched_open_datagram_endpoint
    )

    mocker.patch("pescea.discovery.DISCOVERY_SLEEP", 0.3)
    mocker.patch("pescea.discovery.DISCOVERY_RESCAN", 0.1)
    mocker.patch("pescea.controller.ON_OFF_BUSY_WAIT_TIME", 0.2)
    mocker.patch("pescea.controller.REFRESH_INTERVAL", 0.1)
    mocker.patch("pescea.controller.RETRY_INTERVAL", 0.1)
    mocker.patch("pescea.controller.RETRY_TIMEOUT", 0.3)
    mocker.patch("pescea.controller.DISCONNECTED_INTERVAL", 0.5)
    mocker.patch("pescea.datagram.REQUEST_TIMEOUT", 0.3)

    for f in fireplaces:
        fireplaces[f]["Responsive"] = True

    # Test steps:
    discovery = DiscoveryService()
    await discovery.start_discovery()

    await sleep(1.0)

    # check has fould all controlers
    assert len(discovery.controllers) == len(fireplaces)

    for c in discovery.controllers:
        ctrl = discovery.controllers[c]  # Type: Controller
        assert ctrl.state == Controller.State.READY
        assert ctrl.device_ip == fireplaces[ctrl.device_uid]["IPAddress"]
        assert ctrl.is_on == fireplaces[ctrl.device_uid]["FireIsOn"]
        if fireplaces[ctrl.device_uid]["FanBoost"]:
            assert ctrl.fan == Controller.Fan.FAN_BOOST
        elif fireplaces[ctrl.device_uid]["FlameEffect"]:
            assert ctrl.fan == Controller.Fan.FLAME_EFFECT
        else:
            assert ctrl.fan == Controller.Fan.AUTO
        assert ctrl.desired_temp == fireplaces[ctrl.device_uid]["DesiredTemp"]
        assert ctrl.current_temp == fireplaces[ctrl.device_uid]["CurrentTemp"]

    await sleep(0.5)
    # change values in background and check the polling picks it up
    for f in fireplaces:
        fireplaces[f]["CurrentTemp"] = 10.0

    await sleep(0.5)
    for ctrl in discovery.controllers:
        ctrl = discovery.controllers[c]  # Type: Controller
        assert ctrl.current_temp == fireplaces[ctrl.device_uid]["CurrentTemp"]

    await discovery.close()


@mark.asyncio
async def test_controller_updates(mocker):

    mocker.patch(
        "pescea.udp_endpoints.open_datagram_endpoint", patched_open_datagram_endpoint
    )

    mocker.patch("pescea.discovery.DISCOVERY_SLEEP", 0.4)
    mocker.patch("pescea.discovery.DISCOVERY_RESCAN", 0.2)
    mocker.patch("pescea.controller.ON_OFF_BUSY_WAIT_TIME", 0.2)
    mocker.patch("pescea.controller.REFRESH_INTERVAL", 0.1)
    mocker.patch("pescea.controller.RETRY_INTERVAL", 0.1)
    mocker.patch("pescea.controller.RETRY_TIMEOUT", 0.3)
    mocker.patch("pescea.controller.DISCONNECTED_INTERVAL", 0.5)
    mocker.patch("pescea.datagram.REQUEST_TIMEOUT", 0.3)

    # Test steps:
    discovery = DiscoveryService()
    await discovery.start_discovery()

    await sleep(0.5)

    # check has fould all controlers
    assert len(discovery.controllers) == len(fireplaces)

    for c in discovery.controllers:
        ctrl = discovery.controllers[c]  # Type: Controller
        assert ctrl.state == Controller.State.READY

        assert ctrl.is_on == fireplaces[ctrl.device_uid]["FireIsOn"]
        await ctrl.set_on(not ctrl.is_on)
        assert ctrl.state == Controller.State.BUSY

        if ctrl.fan == Controller.Fan.FLAME_EFFECT:
            await ctrl.set_fan(Controller.Fan.AUTO)
        elif ctrl.fan == Controller.Fan.AUTO:
            await ctrl.set_fan(Controller.Fan.FAN_BOOST)
        else:
            await ctrl.set_fan(Controller.Fan.FLAME_EFFECT)

        await ctrl.set_desired_temp(ctrl.min_temp)

    await sleep(0.5)

    # change values in background and check the polling picks it up
    for f in fireplaces:
        fireplaces[f]["CurrentTemp"] = 10.0

    await sleep(0.5)

    for c in discovery.controllers:
        ctrl = discovery.controllers[c]  # Type: Controller
        assert ctrl.state == Controller.State.READY
        assert ctrl.device_ip == fireplaces[ctrl.device_uid]["IPAddress"]
        assert ctrl.is_on == fireplaces[ctrl.device_uid]["FireIsOn"]
        if fireplaces[ctrl.device_uid]["FanBoost"]:
            assert ctrl.fan == Controller.Fan.FAN_BOOST
        elif fireplaces[ctrl.device_uid]["FlameEffect"]:
            assert ctrl.fan == Controller.Fan.FLAME_EFFECT
        else:
            assert ctrl.fan == Controller.Fan.AUTO
        assert ctrl.desired_temp == fireplaces[ctrl.device_uid]["DesiredTemp"]
        assert ctrl.current_temp == fireplaces[ctrl.device_uid]["CurrentTemp"]

    await discovery.close()


@mark.asyncio
async def test_no_controllers_found(mocker):

    mocker.patch(
        "pescea.udp_endpoints.open_datagram_endpoint", patched_open_datagram_endpoint
    )

    mocker.patch("pescea.controller.ON_OFF_BUSY_WAIT_TIME", 0.2)
    mocker.patch("pescea.controller.REFRESH_INTERVAL", 0.1)
    mocker.patch("pescea.controller.RETRY_INTERVAL", 0.1)
    mocker.patch("pescea.controller.RETRY_TIMEOUT", 0.3)
    mocker.patch("pescea.controller.DISCONNECTED_INTERVAL", 0.6)

    mocker.patch("pescea.discovery.DISCOVERY_SLEEP", 0.3)
    mocker.patch("pescea.discovery.DISCOVERY_RESCAN", 0.1)

    mocker.patch("pescea.datagram.REQUEST_TIMEOUT", 0.1)

    for f in fireplaces:
        fireplaces[f]["Responsive"] = False

    # Test steps:
    discovery = DiscoveryService()
    await discovery.start_discovery()

    await sleep(0.5)

    # check no controllers found
    assert len(discovery.controllers) == 0

    c_count = 0
    for f in fireplaces:
        fireplaces[f]["Responsive"] = True
        c_count += 1
        await sleep(0.5)
        # check controllers found again after a rescan
        assert len(discovery.controllers) == c_count

    fireplaces[next(iter(fireplaces))]["Responsive"] = False
    fireplaces[next(iter(fireplaces))]["IPAddress"] = "11.11.11.11"

    # controllers remain in the list, even after disconnected
    await sleep(0.3)
    assert len(discovery.controllers) == c_count

    await discovery.close()


@mark.asyncio
async def test_search_specific_ip(mocker):

    mocker.patch(
        "pescea.udp_endpoints.open_datagram_endpoint", patched_open_datagram_endpoint
    )

    mocker.patch("pescea.controller.ON_OFF_BUSY_WAIT_TIME", 0.2)
    mocker.patch("pescea.controller.REFRESH_INTERVAL", 0.1)
    mocker.patch("pescea.controller.RETRY_INTERVAL", 0.1)
    mocker.patch("pescea.controller.RETRY_TIMEOUT", 0.3)
    mocker.patch("pescea.controller.DISCONNECTED_INTERVAL", 0.5)

    mocker.patch("pescea.discovery.DISCOVERY_SLEEP", 0.3)
    mocker.patch("pescea.discovery.DISCOVERY_RESCAN", 0.1)

    mocker.patch("pescea.datagram.REQUEST_TIMEOUT", 0.2)

    ip_address = fireplaces[next(iter(fireplaces))]["IPAddress"]
    fireplaces[next(iter(fireplaces))]["Responsive"] = True

    # Test steps:
    discovery = DiscoveryService(ip_addr=ip_address)
    await discovery.start_discovery()

    await sleep(0.5)

    # check only one matching controller found
    assert len(discovery.controllers) == 1

    await discovery.close()


@mark.asyncio
async def test_get_broadcast_addresses():
    """Test the get_broadcast_addresses static method"""

    # Mock psutil.net_if_addrs to return test network interfaces
    mock_interfaces = {
        "eth0": [
            MagicMock(family=socket.AF_INET, broadcast="192.168.1.255"),
            MagicMock(
                family=socket.AF_INET6, broadcast=None
            ),  # IPv6, should be ignored
        ],
        "wlan0": [
            MagicMock(family=socket.AF_INET, broadcast="10.0.0.255"),
        ],
        "lo": [
            MagicMock(
                family=socket.AF_INET, broadcast=None
            ),  # No broadcast, should be ignored
        ],
    }

    with patch("psutil.net_if_addrs", return_value=mock_interfaces):
        addresses = DiscoveryService.get_broadcast_addresses()

        # Should return both valid broadcast addresses
        assert len(addresses) == 2
        assert "192.168.1.255" in addresses
        assert "10.0.0.255" in addresses


@mark.asyncio
async def test_get_broadcast_addresses_no_interfaces():
    """Test get_broadcast_addresses when no valid interfaces exist"""

    mock_interfaces = {
        "lo": [
            MagicMock(family=socket.AF_INET, broadcast=None),  # No broadcast
        ],
    }

    with patch("psutil.net_if_addrs", return_value=mock_interfaces):
        addresses = DiscoveryService.get_broadcast_addresses()
        assert len(addresses) == 0


def test_get_broadcast_addresses_single_interface():
    """Test broadcast address detection with single network interface"""

    mock_interfaces = {
        "eth0": [
            MagicMock(family=socket.AF_INET, broadcast="192.168.1.255"),
        ],
    }

    with patch("psutil.net_if_addrs", return_value=mock_interfaces):
        addresses = DiscoveryService.get_broadcast_addresses()

        assert len(addresses) == 1
        assert "192.168.1.255" in addresses


def test_get_broadcast_addresses_multiple_interfaces():
    """Test broadcast address detection with multiple network interfaces"""

    mock_interfaces = {
        "eth0": [
            MagicMock(family=socket.AF_INET, broadcast="192.168.1.255"),
        ],
        "wlan0": [
            MagicMock(family=socket.AF_INET, broadcast="10.0.0.255"),
        ],
        "eth1": [
            MagicMock(family=socket.AF_INET, broadcast="172.16.255.255"),
        ],
    }

    with patch("psutil.net_if_addrs", return_value=mock_interfaces):
        addresses = DiscoveryService.get_broadcast_addresses()

        assert len(addresses) == 3
        assert "192.168.1.255" in addresses
        assert "10.0.0.255" in addresses
        assert "172.16.255.255" in addresses


def test_get_broadcast_addresses_mixed_address_families():
    """Test that only IPv4 addresses with broadcast are included"""

    mock_interfaces = {
        "eth0": [
            MagicMock(family=socket.AF_INET, broadcast="192.168.1.255"),  # Valid IPv4
            MagicMock(
                family=socket.AF_INET6, broadcast="fe80::1"
            ),  # IPv6, should be ignored
        ],
        "wlan0": [
            MagicMock(family=socket.AF_INET, broadcast="10.0.0.255"),  # Valid IPv4
            MagicMock(
                family=socket.AF_INET6, broadcast=None
            ),  # IPv6, should be ignored
        ],
    }

    with patch("psutil.net_if_addrs", return_value=mock_interfaces):
        addresses = DiscoveryService.get_broadcast_addresses()

        assert len(addresses) == 2
        assert "192.168.1.255" in addresses
        assert "10.0.0.255" in addresses
        # IPv6 and MAC addresses should not be included
        assert "fe80::1" not in addresses


def test_get_broadcast_addresses_no_broadcast_addresses():
    """Test behavior when network interfaces have no broadcast addresses"""

    mock_interfaces = {
        "lo": [
            MagicMock(family=socket.AF_INET, broadcast=None),  # Loopback, no broadcast
        ],
        "tun0": [
            MagicMock(family=socket.AF_INET, broadcast=None),  # Tunnel, no broadcast
        ],
    }

    with patch("psutil.net_if_addrs", return_value=mock_interfaces):
        addresses = DiscoveryService.get_broadcast_addresses()

        assert len(addresses) == 0


def test_get_broadcast_addresses_duplicate_addresses():
    """Test that duplicate broadcast addresses are deduplicated"""

    mock_interfaces = {
        "eth0": [
            MagicMock(family=socket.AF_INET, broadcast="192.168.1.255"),
        ],
        "eth0:1": [  # Virtual interface with same broadcast
            MagicMock(family=socket.AF_INET, broadcast="192.168.1.255"),
        ],
        "wlan0": [
            MagicMock(
                family=socket.AF_INET, broadcast="192.168.1.255"
            ),  # Same broadcast again
        ],
    }

    with patch("psutil.net_if_addrs", return_value=mock_interfaces):
        addresses = DiscoveryService.get_broadcast_addresses()

        # Should only have one instance of the duplicate address
        assert len(addresses) == 1
        assert "192.168.1.255" in addresses


def test_get_broadcast_addresses_empty_interfaces():
    """Test behavior with empty network interfaces"""

    mock_interfaces = {}

    with patch("psutil.net_if_addrs", return_value=mock_interfaces):
        addresses = DiscoveryService.get_broadcast_addresses()

        assert len(addresses) == 0


def test_get_broadcast_addresses_interface_with_no_addresses():
    """Test behavior with network interface that has no addresses"""

    mock_interfaces = {
        "eth0": [],  # Interface exists but has no addresses
        "wlan0": [
            MagicMock(family=socket.AF_INET, broadcast="192.168.1.255"),
        ],
    }

    with patch("psutil.net_if_addrs", return_value=mock_interfaces):
        addresses = DiscoveryService.get_broadcast_addresses()

        assert len(addresses) == 1
        assert "192.168.1.255" in addresses


def test_get_broadcast_addresses_common_network_types():
    """Test broadcast detection for common network types"""

    mock_interfaces = {
        "eth0": [  # Ethernet
            MagicMock(family=socket.AF_INET, broadcast="192.168.1.255"),
        ],
        "wlan0": [  # WiFi
            MagicMock(family=socket.AF_INET, broadcast="192.168.0.255"),
        ],
        "br0": [  # Bridge
            MagicMock(family=socket.AF_INET, broadcast="10.0.0.255"),
        ],
        "docker0": [  # Docker bridge
            MagicMock(family=socket.AF_INET, broadcast="172.17.255.255"),
        ],
        "lo": [  # Loopback (no broadcast)
            MagicMock(family=socket.AF_INET, broadcast=None),
        ],
    }

    with patch("psutil.net_if_addrs", return_value=mock_interfaces):
        addresses = DiscoveryService.get_broadcast_addresses()

        assert len(addresses) == 4
        assert "192.168.1.255" in addresses
        assert "192.168.0.255" in addresses
        assert "10.0.0.255" in addresses
        assert "172.17.255.255" in addresses


@patch("pescea.discovery._LOG")
def test_get_broadcast_addresses_logging(mock_log):
    """Test that broadcast address discovery is properly logged"""

    mock_interfaces = {
        "eth0": [
            MagicMock(family=socket.AF_INET, broadcast="192.168.1.255"),
        ],
        "wlan0": [
            MagicMock(family=socket.AF_INET, broadcast="10.0.0.255"),
        ],
    }

    with patch("psutil.net_if_addrs", return_value=mock_interfaces):
        DiscoveryService.get_broadcast_addresses()

        # Should log each found broadcast address
        assert mock_log.info.call_count >= 2

        # Check that the final summary log includes all addresses
        final_log_call = mock_log.info.call_args_list[-1]
        final_message = final_log_call[0][0]
        assert "Found broadcast addresses:" in final_message


def test_get_broadcast_addresses_psutil_exception():
    """Test behavior when psutil.net_if_addrs raises an exception"""

    def mock_net_if_addrs():
        raise OSError("Network interfaces not available")

    with patch("psutil.net_if_addrs", side_effect=mock_net_if_addrs):
        # Should handle the exception gracefully
        with pytest.raises(OSError):
            DiscoveryService.get_broadcast_addresses()


def test_get_broadcast_addresses_interface_name_types():
    """Test with various interface name types and patterns"""

    mock_interfaces = {
        "eth0": [MagicMock(family=socket.AF_INET, broadcast="192.168.1.255")],
        "enp0s3": [
            MagicMock(family=socket.AF_INET, broadcast="10.0.2.255")
        ],  # systemd naming
        "wlp2s0": [
            MagicMock(family=socket.AF_INET, broadcast="192.168.0.255")
        ],  # systemd wifi
        "ens33": [
            MagicMock(family=socket.AF_INET, broadcast="172.16.255.255")
        ],  # VMware naming
        "em1": [
            MagicMock(family=socket.AF_INET, broadcast="10.1.1.255")
        ],  # Enterprise naming
        "p1p1": [
            MagicMock(family=socket.AF_INET, broadcast="192.168.10.255")
        ],  # Physical port naming
    }

    with patch("psutil.net_if_addrs", return_value=mock_interfaces):
        addresses = DiscoveryService.get_broadcast_addresses()

        assert len(addresses) == 6
        expected_addresses = {
            "192.168.1.255",
            "10.0.2.255",
            "192.168.0.255",
            "172.16.255.255",
            "10.1.1.255",
            "192.168.10.255",
        }
        assert set(addresses) == expected_addresses


def test_get_broadcast_addresses_return_type():
    """Test that get_broadcast_addresses returns the correct type"""

    mock_interfaces = {
        "eth0": [
            MagicMock(family=socket.AF_INET, broadcast="192.168.1.255"),
        ],
    }

    with patch("psutil.net_if_addrs", return_value=mock_interfaces):
        addresses = DiscoveryService.get_broadcast_addresses()

        # Should return a list
        assert isinstance(addresses, list)

        # All items should be strings
        for addr in addresses:
            assert isinstance(addr, str)


def test_get_broadcast_addresses_subnet_variations():
    """Test broadcast addresses for different subnet sizes"""

    mock_interfaces = {
        "eth0": [MagicMock(family=socket.AF_INET, broadcast="192.168.1.255")],  # /24
        "eth1": [MagicMock(family=socket.AF_INET, broadcast="10.0.255.255")],  # /16
        "eth2": [MagicMock(family=socket.AF_INET, broadcast="172.16.15.255")],  # /20
        "eth3": [MagicMock(family=socket.AF_INET, broadcast="192.168.0.127")],  # /25
        "eth4": [MagicMock(family=socket.AF_INET, broadcast="10.255.255.255")],  # /8
    }

    with patch("psutil.net_if_addrs", return_value=mock_interfaces):
        addresses = DiscoveryService.get_broadcast_addresses()

        assert len(addresses) == 5
        expected_addresses = {
            "192.168.1.255",
            "10.0.255.255",
            "172.16.15.255",
            "192.168.0.127",
            "10.255.255.255",
        }
        assert set(addresses) == expected_addresses
