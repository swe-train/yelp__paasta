import getpass
import logging
import os
from typing import Any
from typing import Dict
from typing import List
from typing import Mapping
from typing import Optional

from paasta_tools.secret_providers import CryptoKey

try:
    from vault_tools.client.jsonsecret import get_plaintext
    from vault_tools.paasta_secret import get_vault_client
    from vault_tools.gpg import TempGpgKeyring
    from vault_tools.paasta_secret import encrypt_secret
    import hvac
except ImportError:

    def get_plaintext(*args: Any, **kwargs: Any) -> bytes:
        return b"No plain text available without vault_tools"

    def get_vault_client(*args: Any, **kwargs: Any) -> None:
        return None

    TempGpgKeyring = None

    def encrypt_secret(*args: Any, **kwargs: Any) -> None:
        return None


from paasta_tools.secret_providers import BaseSecretProvider
from paasta_tools.secret_tools import get_secret_name_from_ref


log = logging.getLogger(__name__)


class SecretProvider(BaseSecretProvider):
    def __init__(
        self,
        soa_dir: Optional[str],
        service_name: Optional[str],
        cluster_names: List[str],
        vault_cluster_config: Dict[str, str] = {},
        vault_auth_method: str = "ldap",
        vault_token_file: str = "/root/.vault-token",
        vault_num_uses: int = 1,
        **kwargs: Any,
    ) -> None:
        super().__init__(soa_dir, service_name, cluster_names)
        self.vault_cluster_config = vault_cluster_config
        self.vault_auth_method = vault_auth_method
        self.vault_token_file = vault_token_file
        self.ecosystems = self.get_vault_ecosystems_for_clusters()
        self.clients: Mapping[str, hvac.Client] = {}
        if vault_auth_method == "ldap":
            username = getpass.getuser()
            password = getpass.getpass(
                "Please enter your LDAP password to auth with Vault\n"
            )
        else:
            username = None
            password = None
        for ecosystem in self.ecosystems:
            self.clients[ecosystem] = get_vault_client(
                ecosystem=ecosystem,
                num_uses=vault_num_uses,
                vault_auth_method=self.vault_auth_method,
                vault_token_file=self.vault_token_file,
                username=username,
                password=password,
            )

    def decrypt_environment(
        self, environment: Dict[str, str], **kwargs: Any
    ) -> Dict[str, str]:
        client = self.clients[self.ecosystems[0]]
        secret_environment = {}
        for k, v in environment.items():
            secret_name = get_secret_name_from_ref(v)
            secret_path = os.path.join(self.secret_dir, f"{secret_name}.json")
            secret = get_plaintext(
                client=client,
                env=self.ecosystems[0],
                path=secret_path,
                cache_enabled=False,
                cache_dir=None,
                cache_key=None,
                context=self.service_name,
                rescue_failures=False,
            ).decode("utf-8")
            secret_environment[k] = secret
        return secret_environment

    def get_vault_ecosystems_for_clusters(self) -> List[str]:
        try:
            return list(
                {
                    self.vault_cluster_config[cluster_name]
                    for cluster_name in self.cluster_names
                }
            )
        except KeyError as e:
            print(
                "Cannot find a vault cluster for the %s paasta cluster. A mapping must exist "
                "in /etc/paasta so we contact the correct vault cluster to get/set secrets"
                % e
            )
            raise

    def write_secret(
        self,
        action: str,
        secret_name: str,
        plaintext: bytes,
        cross_environment_motivation: Optional[str] = None,
    ) -> None:
        with TempGpgKeyring(overwrite=True):
            for ecosystem in self.ecosystems:
                client = self.clients[ecosystem]
                encrypt_secret(
                    client=client,
                    action=action,
                    ecosystem=ecosystem,
                    secret_name=secret_name,
                    soa_dir=self.soa_dir,
                    plaintext=plaintext,
                    service_name=self.service_name,
                    transit_key=self.encryption_key,
                    cross_environment_motivation=cross_environment_motivation,
                )

    def decrypt_secret(self, secret_name: str) -> str:
        return self.decrypt_secret_raw(secret_name).decode("utf-8")

    def decrypt_secret_raw(self, secret_name: str) -> bytes:
        client = self.clients[self.ecosystems[0]]
        secret_path = os.path.join(self.secret_dir, f"{secret_name}.json")
        return get_plaintext(
            client=client,
            path=secret_path,
            env=self.ecosystems[0],
            cache_enabled=False,
            cache_key=None,
            cache_dir=None,
            context=self.service_name,
            rescue_failures=False,
        )

    def get_secret_signature_from_data(self, data: Mapping[str, Any]) -> Optional[str]:
        ecosystem = self.ecosystems[0]
        if data["environments"].get(ecosystem):
            return data["environments"][ecosystem]["signature"]
        else:
            return None

    def get_data_from_vault_path(self, path: str) -> Optional[Dict[str, str]]:
        # clients.read returns None if not set
        # if it is set, it returns an object with { **metadata, data: {} }
        # eg lease_id, request_id, etc. we only care about 'data' here
        entry = self.clients[self.ecosystems[0]].read(path)

        # returns one of 3 things:
        #   entry -> could be None
        #   entry["data"] -> could be an object with content
        #   entry["data"] -> could be empty (ie no secrets)
        # all are plausible and valid scenarios that give different information about the path

        if entry is not None:
            return entry.get("data", {})
        return None

    def get_key_versions(
        self,
        key_name: str,
    ) -> List[CryptoKey]:
        """
        Retrieve all versions of Vault key based on its metadata
        """
        client = self.clients[self.ecosystems[0]]
        crypto_keys: List[CryptoKey] = []
        try:
            meta_response = client.secrets.kv.read_secret_metadata(
                path=key_name, mount_point="keystore"
            )

            for key_version in meta_response["data"]["versions"].keys():
                key_response = client.secrets.kv.read_secret_version(
                    path=key_name, version=key_version, mount_point="keystore"
                )
                crypto_keys.append(
                    {
                        "key_name": key_name,
                        "key_version": key_response["data"]["metadata"]["version"],
                        "key": key_response["data"]["data"]["key"],
                    }
                )
        except hvac.exceptions.VaultError:
            log.warning(
                f"Could not fetch key versions for {key_name} on {self.ecosystems[0]}"
            )
            pass

        return crypto_keys
