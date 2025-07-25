#!/usr/bin/env python3
#
# rat_config_parser.py
#
# Author: jeFF0Falltrades
#
# Provides the primary functionality for parsing configurations from RATs
#
# MIT License
#
# Copyright (c) 2024 Jeff Archer
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
from collections import OrderedDict
from logging import getLogger
from os.path import isfile
from re import DOTALL, compile, search
from typing import Any, Tuple

from yara import Rules

from .config_parser_exception import ConfigParserException
from .utils import config_item
from .utils.config_normalization import check_key_n_value
from .utils.decryptors import (
    SUPPORTED_DECRYPTORS,
    ConfigDecryptor,
    IncompatibleDecryptorException,
)
from .utils.decryptors.config_decryptor_plaintext import KNOWN_CONFIG_FIELD_NAMES
from .utils.dotnetpe_payload import DotNetPEPayload

logger = getLogger(__name__)


class RATConfigParser:
    # Min and max number of items in a potential config section
    _MIN_CONFIG_LEN_FLOOR = 5
    _MIN_CONFIG_LEN_CEILING = 9

    # Pattern to find the VerifyHash() method
    _PATTERN_VERIFY_HASH = compile(
        rb"\x7e.{3}\x04(?:\x6f.{3}\x0a){2}\x74.{3}\x01", DOTALL
    )

    def __init__(
        self,
        file_path: str = "",
        yara_rule: Rules = None,
        data: bytes = None,
        remap_config: bool = False,
        preserve_obfuscated_keys: bool = False,
    ) -> None:
        self.report = {
            "file_path": file_path,
            "sha256": "",
            "yara_possible_family": "",
            "key": "",
            "salt": "",
            "config": {},
        }
        self.remap_config = remap_config
        self.preserve_obfuscated_keys = preserve_obfuscated_keys
        try:
            if data is None and not isfile(file_path):
                raise ConfigParserException("File not found")
            # Filled in _decrypt_and_decode_config()
            self._incompatible_decryptors: list[int] = []
            try:
                self._dnpp = DotNetPEPayload(file_path, yara_rule, data)
            except Exception as e:
                raise e
            self.report["sha256"] = self._dnpp.sha256
            self.report["yara_possible_family"] = self._dnpp.yara_match

            # Assigned in _decrypt_and_decode_config()
            self._decryptor: ConfigDecryptor = None
            self.report["config"] = self._get_config()
            self.report["key"] = (
                self._decryptor.key.hex()
                if self._decryptor is not None and self._decryptor.key is not None
                else "None"
            )
            self.report["salt"] = (
                self._decryptor.salt.hex()
                if self._decryptor is not None and self._decryptor.salt is not None
                else "None"
            )
        except Exception as e:
            self.report["config"] = f"Exception encountered for {file_path}: {e}"

    # Decrypts/decodes values from an encrypted config and returns the
    # decrypted/decoded config
    def _decrypt_and_decode_config(
        self, encrypted_config: bytes, min_config_len: int
    ) -> dict[str, Any]:
        decoded_config = {}
        config_fields_map = {}

        for item_class in config_item.SUPPORTED_CONFIG_ITEMS:
            item = item_class()
            item_data = {}
            # Translate config Field RVAs to Field names
            for k, v in item.parse_from(encrypted_config).items():
                field_name = self._dnpp.field_name_from_rva(k)
                config_fields_map[k] = field_name
                item_data[field_name] = v
            if len(item_data) > 0:
                if type(item) is config_item.EncryptedStringConfigItem:
                    # Translate config value RVAs to string values
                    for k in item_data:
                        item_data[k] = self._dnpp.user_string_from_rva(item_data[k])

                    # Attempt to decrypt encrypted values
                    for decryptor in SUPPORTED_DECRYPTORS:
                        if decryptor in self._incompatible_decryptors:
                            continue

                        if self._decryptor is None:
                            # Try to instantiate the selected decryptor
                            # Add to incompatible list and move on upon failure
                            try:
                                self._decryptor = decryptor(self._dnpp)
                            except IncompatibleDecryptorException as ide:
                                logger.debug(
                                    f"Decryptor incompatible {decryptor} : {ide}"
                                )
                                self._incompatible_decryptors.append(decryptor)
                                continue
                        try:
                            # Try to decrypt the encrypted strings
                            # Continue to next compatible decryptor on failure
                            item_data = self._decryptor.decrypt_encrypted_strings(
                                item_data
                            )
                            break
                        except Exception as e:
                            logger.debug(
                                f"Decryption failed with decryptor {decryptor} : {e}"
                            )
                            self._decryptor = None

                    if self._decryptor is None:
                        raise ConfigParserException("All decryptors failed")

                elif type(item) is config_item.ByteArrayConfigItem:
                    for k in item_data:
                        arr_size, arr_rva = item_data[k]
                        item_data[k] = self._dnpp.byte_array_from_size_and_rva(
                            arr_size, arr_rva
                        ).hex()

                decoded_config.update(item_data)
        # UrlHost is a marker of a special case until this can be standardized
        if len(decoded_config) < min_config_len and "UrlHost" not in item_data:
            raise ConfigParserException(
                f"Minimum threshold of config items not met: {len(decoded_config)}/{min_config_len}"
            )
        if self.remap_config:
            sorted_decoded_config = OrderedDict()
            normalized_fields = []
            for k in sorted(config_fields_map.keys()):
                key_name = config_fields_map[k]
                value = decoded_config[key_name]
                key_normalized, value = check_key_n_value(key_name, value)
                if key_normalized != key_name:
                    normalized_fields.append(key_name)
                sorted_decoded_config[key_normalized] = value
            # Ensure config items added by decryptors dynamically are preserved
            sorted_decoded_config.update(
                {
                    key: decoded_config[key]
                    for key in decoded_config
                    if key not in sorted_decoded_config and key not in normalized_fields
                }
            )
            return sorted_decoded_config
        return decoded_config

    # Searches for the RAT configuration section, using the VerifyHash() marker
    # or brute-force, returning the decrypted config on success
    def _get_config(self) -> dict[str, Any]:
        logger.debug("Extracting config...")
        try:
            config_start, decrypted_config = self._get_config_verify_hash_method()
        except Exception:
            logger.debug("VerifyHash() method failed; Attempting .cctor brute force...")
            # If the VerifyHash() method does not work, move to brute-forcing
            # static constructors
            try:
                config_start, decrypted_config = self._get_config_cctor_brute_force()
            except Exception as e:
                raise ConfigParserException(f"Could not identify config: {e}")
        logger.debug(f"Config found at RVA {hex(config_start)}...")
        if not self.preserve_obfuscated_keys and self._is_likely_obfuscated_config(
            decrypted_config
        ):
            logger.debug(
                "Preserve keys set to False and possible obfuscation detected; Translating keys..."
            )
            decrypted_config = {
                f"obfuscated_key_{idx}": v
                for idx, (_, v) in enumerate(decrypted_config.items(), 1)
            }

        return decrypted_config

    # Attempts to retrieve the config via brute-force, looking through every
    # static constructor (.cctor) and attempting to decode/decrypt a valid
    # config from that constructor, returning the config RVA and decrypted
    # config on success
    def _get_config_cctor_brute_force(self) -> Tuple[int, dict[str, Any]]:
        candidates = self._dnpp.methods_from_name(".cctor")
        if len(candidates) == 0:
            raise ConfigParserException("No .cctor method could be found")

        # For each .cctor method, map its RVA and body (in raw bytes)
        candidate_cctor_data = {
            method.rva: self._dnpp.method_body_from_method(method)
            for method in candidates
        }

        config_start, decrypted_config = None, None
        # Start at our ceiling value for number of config items
        min_config_len = self._MIN_CONFIG_LEN_CEILING

        while decrypted_config is None and min_config_len >= self._MIN_CONFIG_LEN_FLOOR:
            for method_rva, method_body in candidate_cctor_data.items():
                logger.debug(
                    f"Attempting brute force at .cctor method at {hex(method_rva)}"
                )
                try:
                    config_start, decrypted_config = (
                        method_rva,
                        self._decrypt_and_decode_config(method_body, min_config_len),
                    )
                    break
                except Exception as e:
                    logger.debug(
                        f"Brute force failed for method at {hex(method_rva)}: {e}"
                    )
                    continue
            # Reduce the minimum config length until we reach our floor
            min_config_len -= 1

        if decrypted_config is None:
            raise ConfigParserException(
                "No valid configuration could be parsed from any .cctor methods"
            )
        return config_start, decrypted_config

    # Attempts to retrieve the config via looking for a config section preceded
    # by the VerifyHash() method typically found in a Settings module,
    # returning the config RVA and decrypted config on success
    def _get_config_verify_hash_method(self) -> Tuple[int, dict[str, Any]]:
        # Identify the VerifyHash() Method code
        verify_hash_hit = search(self._PATTERN_VERIFY_HASH, self._dnpp.data)
        if verify_hash_hit is None:
            raise ConfigParserException("Could not identify VerifyHash() marker")

        # Reverse the hit to find the VerifyHash() method, then grab the
        # subsequent function
        config_method = self._dnpp.method_from_instruction_offset(
            verify_hash_hit.start(), 1
        )
        encrypted_config = self._dnpp.method_body_from_method(config_method)
        min_config_len = self._MIN_CONFIG_LEN_CEILING
        while True:
            try:
                decrypted_config = self._decrypt_and_decode_config(
                    encrypted_config, min_config_len
                )
                return config_method.rva, decrypted_config
            except Exception as e:
                # Reduce the minimum config length until we reach our floor
                if min_config_len < self._MIN_CONFIG_LEN_FLOOR:
                    raise e
                min_config_len -= 1

    def _is_likely_obfuscated_config(self, config: dict[str, Any]) -> bool:
        for key in config.keys():
            if not key.isascii():
                return True
            if key in KNOWN_CONFIG_FIELD_NAMES:
                return False
        return True
