import logging
import tempfile

import paramiko
import xmltodict

from app.core.config import settings
from .config.schema import PfSenseConfig
from .config.settings import (
    BACKUP_CONFIG_DIR,
    CONFIG_DIR,
    PFSENSE_HOST,
    PFSENSE_PASSWORD,
    PFSENSE_USERNAME,
)


class PfSenseError(Exception):
    """Custom exception for pfSense operations."""


def connect() -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        if settings.PFSENSE_KEY_FILE:
            key = paramiko.RSAKey.from_private_key_file(settings.PFSENSE_KEY_FILE)
            client.connect(
                settings.PFSENSE_HOST,
                port=settings.PFSENSE_PORT,
                username=settings.PFSENSE_USERNAME,
                pkey=key,
            )
        client.connect(
            settings.PFSENSE_HOST,
            port=settings.PFSENSE_PORT,
            username=settings.PFSENSE_USERNAME,
            password=settings.PFSENSE_PASSWORD,
        )
    except paramiko.AuthenticationException as e:
        logging.error("Authentication failed when connecting to pfSense: %s", e)
        raise PfSenseError("Authentication failed") from e
    except paramiko.SSHException as e:
        logging.error("SSH connection failed: %s", e)
        raise PfSenseError("SSH connection failed") from e
    return client


def fetch_pfsense_config() -> PfSenseConfig:

    client = connect()

    _, stdout, stderr = client.exec_command(f"cat {CONFIG_DIR}")

    if stderr.channel.recv_exit_status() != 0:
        stderr.read().decode()
        client.close()
        msg = "Failed to fetch pfSense configuration."
        raise PfSenseError(msg)

    config_xml = stdout.read().decode()
    client.close()

    # Convert XML to dict
    config_dict = xmltodict.parse(config_xml, force_list=("user", "group", "staticmap"))
    # Parse with Pydantic
    return PfSenseConfig(**config_dict["pfsense"])


def push_pfsense_config(config: PfSenseConfig) -> int:
    """Push a new configuration to the pfSense device.

    Args:
        config (PfSense): The pfSense configuration to push.

    Returns:
        int: 1 if successful, 0 otherwise.
    """
    remote_config = CONFIG_DIR
    backup_config = BACKUP_CONFIG_DIR

    client = connect()

    client.exec_command(f"cp {remote_config} {backup_config}")

    sftp = client.open_sftp()
    # Convert config (Pydantic model) to XML string
    config_dict = {"pfsense": config.model_dump(by_alias=True)}
    config_xml = xmltodict.unparse(config_dict, pretty=True)

    # Write XML to a temporary local file
    with tempfile.NamedTemporaryFile("w+", delete=False) as tmpfile:
        tmpfile.write(config_xml)
        tmpfile.flush()
        local_path = tmpfile.name

    # Upload the temporary file to the remote device
    sftp.put(local_path, "/tmp/config.xml")
    sftp.close()

    client.exec_command(f"mv /tmp/config.xml {remote_config}")
    client.exec_command("rm /tmp/config.cache")
    client.exec_command("/etc/rc.reload_all")

    client.close()
    return 1


def load_pfsense_config_from_file(file_path: str) -> PfSenseConfig:
    """Load a pfSense configuration from a file.

    Args:
        file_path (str): Path to the configuration file.

    Returns:
        PfSense: Parsed pfSense configuration.
    """
    with open(file_path) as file:
        config_xml = file.read()

    # Convert XML to dict
    config_dict = xmltodict.parse(config_xml, force_list=("user", "group", "staticmap"))

    # Parse with Pydantic
    return PfSenseConfig(**config_dict["pfsense"])


def _validate_dhcp_range_consistency(config: PfSenseConfig) -> bool:
    """Validate DHCP range consistency."""
    if not (config.dhcpd and config.dhcpd.lan and config.dhcpd.lan.range):
        return True

    dhcp_range = config.dhcpd.lan.range
    if not (dhcp_range.from_ and dhcp_range.to):
        return True

    try:
        import ipaddress

        from_ip = ipaddress.IPv4Address(dhcp_range.from_)
        to_ip = ipaddress.IPv4Address(dhcp_range.to)
        if from_ip >= to_ip:
            logging.error(
                "[!] DHCP range 'from' address must be less than 'to' address."
            )
            return False
    except ValueError as e:
        logging.exception("[!] Invalid DHCP range addresses: %r", e)
        return False

    return True


def _validate_dhcp_subnet_consistency(config: PfSenseConfig) -> bool:
    """Validate that DHCP range is within the LAN subnet."""
    try:
        import ipaddress

        lan_subnet = f"{config.interfaces.lan.ipaddr}/{config.interfaces.lan.subnet}"
        lan_network = ipaddress.IPv4Network(lan_subnet, strict=False)

        if not (config.dhcpd and config.dhcpd.lan and config.dhcpd.lan.range):
            return True

        dhcp_range = config.dhcpd.lan.range
        if not (dhcp_range.from_ and dhcp_range.to):
            return True

        try:
            from_ip = ipaddress.IPv4Address(dhcp_range.from_)
            to_ip = ipaddress.IPv4Address(dhcp_range.to)

            if from_ip not in lan_network or to_ip not in lan_network:
                logging.error("[!] DHCP range must be within the LAN subnet.")
                return False
        except ValueError:
            # Skip if addresses are special values (dhcp, etc.)
            pass

    except ValueError:
        # Skip validation if addresses are special values (dhcp, static, etc.)
        pass

    return True


def validate_pfsense_config(config: PfSenseConfig) -> bool:
    """Validate the pfSense configuration.

    Args:
        config (PfSense): The pfSense configuration to validate.

    Returns:
        bool: True if valid, False otherwise.
    """
    try:
        # Basic system validation
        if not config.system.hostname:
            logging.error("[!] Hostname is required.")
            return False

        # Interface validation
        if not config.interfaces.lan.ipaddr:
            logging.error("[!] LAN interface IP address is required.")
            return False

        # Additional IP address validations are now handled automatically
        # by Pydantic validators in the schema models

        # Validate DHCP configuration
        if not _validate_dhcp_range_consistency(config):
            return False

        if not _validate_dhcp_subnet_consistency(config):
            return False

        logging.info("[+] pfSense configuration validation passed.")
        return True

    except Exception as e:
        logging.exception(f"[!] Configuration validation failed: {e}")
        return False
