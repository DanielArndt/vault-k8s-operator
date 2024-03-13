#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
"""Library for interacting with a Vault cluster.

This library shares operations that interact with Vault through its API. It is
intended to be used by charms that need to manage a Vault cluster.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple, Union

import hvac
import requests
from hvac.exceptions import InvalidPath, InvalidRequest, VaultError
from requests.exceptions import RequestException

# The unique Charmhub library identifier, never change it
LIBID = "674754a3268d4507b749ec34214706fd"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 6


logger = logging.getLogger(__name__)
RAFT_STATE_ENDPOINT = "v1/sys/storage/raft/autopilot/state"


@dataclass
class Token:
    """Class that represents token authentication for vault.

    This method is the most basic and always available method to access vault.
    """

    token: str

    def login(self, client: hvac.Client):
        """Authenticate a vault client with a token."""
        client.token = self.token


@dataclass
class AppRole:
    """Class that represents approle authentication for vault.

    This method is primarily used to authenticate automation programs for vault.
    """

    role_id: str
    secret_id: str

    def login(self, client: hvac.Client):
        """Authenticate a vault client with approle details."""
        client.auth.approle.login(role_id=self.role_id, secret_id=self.secret_id, use_token=True)


@dataclass
class Certificate:
    """Class that represents a certificate generated by the PKI secrets engine."""

    certificate: str
    ca: str
    chain: List[str]


class AuditDeviceType(Enum):
    """Class that represents the devices that vault supports as device types for audit."""

    FILE = "file"
    SYSLOG = "syslog"
    SOCKET = "socket"


class SecretsBackend(Enum):
    """Class that represents the supported secrets backends by Vault."""

    KV_V2 = "kv-v2"
    PKI = "pki"


class Vault:
    """Class to interact with Vault through its API."""

    def __init__(self, url: str, ca_cert_path: str):
        self._client = hvac.Client(url=url, verify=ca_cert_path)

    def authenticate(self, auth_details: Union[Token, AppRole]) -> None:
        """Find and use the token related with the given auth method."""
        auth_details.login(self._client)

    def is_api_available(self) -> bool:
        """Return whether Vault is available."""
        try:
            self._client.sys.read_health_status(standby_ok=True)
            return True
        except (VaultError, RequestException) as e:
            logger.error("Error while checking Vault health status: %s", e)
            return False

    def initialize(
        self, secret_shares: int = 1, secret_threshold: int = 1
    ) -> Tuple[str, List[str]]:
        """Initialize Vault.

        Returns:
            A tuple containing the root token and the unseal keys.
        """
        initialize_response = self._client.sys.initialize(
            secret_shares=secret_shares, secret_threshold=secret_threshold
        )
        logger.info("Vault is initialized")
        return initialize_response["root_token"], initialize_response["keys"]

    def is_initialized(self) -> bool:
        """Return whether Vault is initialized."""
        return self._client.sys.is_initialized()

    def unseal(self, unseal_keys: List[str]) -> None:
        """Unseal Vault."""
        try:
            for unseal_key in unseal_keys:
                self._client.sys.submit_unseal_key(unseal_key)
            logger.info("Vault is unsealed")
        except InvalidRequest as e:
            if self._client.sys.is_sealed():
                raise e

    def is_active(self) -> bool:
        """Return the health status of Vault.

        Returns:
            True if initialized, unsealed and active, False otherwise.
                Will return True if Vault is in standby mode too (standby_ok=True).
        """
        try:
            health_status = self._client.sys.read_health_status(standby_ok=True)
            return health_status.status_code == 200
        except (VaultError, RequestException) as e:
            logger.error("Error while checking Vault health status: %s", e)
            return False

    def enable_audit_device(self, device_type: AuditDeviceType, path: str) -> None:
        """Enable a new audit device at the supplied path.

        Args:
            device_type: One of three available device types
            path: The path that will receive audit logs
        """
        try:
            self._client.sys.enable_audit_device(
                device_type=device_type.value,
                options={"file_path": path},
            )
            logger.info("Enabled audit device %s for path %s", device_type.value, path)
        except InvalidRequest:
            logger.info("Audit device already enabled.")

    def enable_approle_auth_method(self) -> None:
        """Enable approle auth method."""
        try:
            self._client.sys.enable_auth_method(method_type="approle")
            logger.info("Enabled approle auth method.")
        except InvalidRequest:
            logger.info("Approle already enabled.")

    def configure_policy(
        self, policy_name: str, policy_path: str, mount: Optional[str] = None
    ) -> None:
        """Create/update a policy within vault.

        Args:
            policy_name: Name of the policy to create
            policy_path: The path of the file where the policy is defined, ending with .hcl
            mount: Formats the policy string with the given mount path to create dynamic policies
        """
        with open(policy_path, "r") as f:
            policy = f.read()
        self._client.sys.create_or_update_policy(
            name=policy_name,
            policy=policy if not mount else policy.format(mount=mount),
        )
        logger.debug("Created or updated charm policy: %s", policy_name)

    def configure_approle(self, role_name: str, cidrs: List[str], policies: List[str]) -> str:
        """Create/update a role within vault associating the supplied policies.

        Args:
            role_name: Name of the role to be created or updated
            cidrs: The list of IP networks that are allowed to authenticate
            policies: The attached list of policy names this approle will have access to
        """
        self._client.auth.approle.create_or_update_approle(
            role_name,
            token_ttl="60s",
            token_max_ttl="60s",
            token_policies=policies,
            bind_secret_id="true",
            token_bound_cidrs=cidrs,
        )
        response = self._client.auth.approle.read_role_id(role_name)
        return response["data"]["role_id"]

    def generate_role_secret_id(self, name: str, cidrs: List[str]) -> str:
        """Generate a new secret tied to an AppRole."""
        response = self._client.auth.approle.generate_secret_id(name, cidr_list=cidrs)
        return response["data"]["secret_id"]

    def read_role_secret(self, name: str, id: str) -> dict:
        """Get definition of a secret tied to an AppRole."""
        response = self._client.auth.approle.read_secret_id(name, id)
        return response["data"]

    def enable_secrets_engine(self, backend_type: SecretsBackend, path: str) -> None:
        """Enable given secret engine on the given path."""
        try:
            self._client.sys.enable_secrets_engine(
                backend_type=backend_type.value,
                description=f"Charm created '{backend_type.value}' backend",
                path=path,
            )
            logger.info("Enabled %s backend", backend_type.value)
        except InvalidRequest:
            logger.info("%s backend already enabled", backend_type.value)

    def is_intermediate_ca_set(self, mount: str, certificate: str) -> bool:
        """Check if the intermediate CA is set for the PKI backend."""
        intermediate_ca = self._client.secrets.pki.read_ca_certificate(mount_point=mount)
        return intermediate_ca == certificate

    def get_intermediate_ca(self, mount: str) -> str:
        """Get the intermediate CA for the PKI backend."""
        return self._client.secrets.pki.read_ca_certificate(mount_point=mount)

    def generate_pki_intermediate_ca_csr(self, mount: str, common_name: str) -> str:
        """Generate an intermediate CA CSR for the PKI backend.

        Returns:
            str: The Certificate Signing Request.
        """
        response = self._client.secrets.pki.generate_intermediate(
            mount_point=mount,
            common_name=common_name,
            type="internal",
        )
        logger.info("Generated a CSR for the intermediate CA for the PKI backend")
        return response["data"]["csr"]

    def set_pki_intermediate_ca_certificate(self, certificate: str, mount: str) -> None:
        """Set the intermediate CA certificate for the PKI backend."""
        self._client.secrets.pki.set_signed_intermediate(
            certificate=certificate, mount_point=mount
        )
        logger.info("Set the intermediate CA certificate for the PKI backend")

    def sign_pki_certificate_signing_request(
        self,
        mount: str,
        role: str,
        csr: str,
        common_name: str,
    ) -> Optional[Certificate]:
        """Sign a certificate signing request for the PKI backend.

        Args:
            mount: The PKI mount point.
            role: The role to use for signing the certificate.
            csr: The certificate signing request.
            common_name: The common name for the certificate.

        Returns:
            Certificate: The signed certificate object
        """
        try:
            response = self._client.secrets.pki.sign_certificate(
                csr=csr,
                mount_point=mount,
                common_name=common_name,
                name=role,
            )
            logger.info("Signed a PKI certificate for %s", common_name)
            return Certificate(
                certificate=response["data"]["certificate"],
                ca=response["data"]["issuing_ca"],
                chain=response["data"]["ca_chain"],
            )
        except InvalidRequest as e:
            logger.warning("Error while signing PKI certificate: %s", e)
            return None

    def is_pki_ca_certificate_set(self, mount: str, certificate: str) -> bool:
        """Check if the CA certificate is set for the PKI backend."""
        existing_certificate = self._client.secrets.pki.read_ca_certificate(mount_point=mount)
        return existing_certificate == certificate

    def create_pki_charm_role(self, role: str, allowed_domains: str, mount: str) -> None:
        """Create a role for the PKI backend."""
        self._client.secrets.pki.create_or_update_role(
            name=role,
            mount_point=mount,
            extra_params={
                "allowed_domains": allowed_domains,
                "allow_subdomains": True,
            },
        )
        logger.info("Created a role for the PKI backend")

    def is_pki_role_created(self, role: str, mount: str) -> bool:
        """Check if the role is created for the PKI backend."""
        try:
            existing_roles = self._client.secrets.pki.list_roles(mount_point=mount)
            return role in existing_roles["data"]["keys"]
        except InvalidPath:
            return False

    def create_snapshot(self) -> requests.Response:
        """Create a snapshot of the Vault data."""
        return self._client.sys.take_raft_snapshot()

    def restore_snapshot(self, snapshot: bytes) -> requests.Response:
        """Restore a snapshot of the Vault data.

        Uses force_restore_raft_snapshot to restore the snapshot
        even if the unseal key used at backup time is different from the current one.
        """
        return self._client.sys.force_restore_raft_snapshot(snapshot)

    def get_raft_cluster_state(self) -> dict:
        """Get raft cluster state."""
        response = self._client.adapter.get(RAFT_STATE_ENDPOINT)
        return response["data"]

    def is_raft_cluster_healthy(self) -> bool:
        """Check if raft cluster is healthy."""
        return self.get_raft_cluster_state()["healthy"]

    def remove_raft_node(self, node_id: str) -> None:
        """Remove raft peer."""
        self._client.sys.remove_raft_node(server_id=node_id)
        logger.info("Removed raft node %s", node_id)

    def is_node_in_raft_peers(self, node_id: str) -> bool:
        """Check if node is in raft peers."""
        raft_config = self._client.sys.read_raft_config()
        for peer in raft_config["data"]["config"]["servers"]:
            if peer["node_id"] == node_id:
                return True
        return False

    def get_num_raft_peers(self) -> int:
        """Return the number of raft peers."""
        raft_config = self._client.sys.read_raft_config()
        return len(raft_config["data"]["config"]["servers"])
