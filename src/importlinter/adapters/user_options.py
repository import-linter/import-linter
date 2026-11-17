import abc
import configparser
import tomllib
from typing import Any

from importlinter.application import file_finding
from importlinter.application.app_config import settings
from importlinter.application.ports import user_options as ports
from importlinter.application.user_options import ConfigSource, InvalidUserOptions, UserOptions


class AbstractUserOptionReader(ports.UserOptionReader):
    potential_config_filenames: list[str]

    def read_options(self, config_filename: str | None = None) -> UserOptions | None:
        source = self.read_source(config_filename)
        return source.user_options if source else None

    def read_source(
        self, config_filename: str | None = None, *, fragment: bool = False
    ) -> ConfigSource | None:
        if config_filename:
            config_filenames = file_finding.find_any(config_filename)
            if not config_filenames:
                # If we specify a filename, raise an exception.
                raise FileNotFoundError(f"Could not find {config_filename}.")
        else:
            config_filenames = file_finding.find_any(*self.potential_config_filenames)
            if not config_filenames:
                return None

        for config_filename in config_filenames:
            options = self._read_config_filename(config_filename, fragment=fragment)
            if options:
                return ConfigSource(config_filename, options)

        return None

    @abc.abstractmethod
    def _read_config_filename(
        self, config_filename: str, *, fragment: bool = False
    ) -> UserOptions | None:
        raise NotImplementedError


class IniFileUserOptionReader(AbstractUserOptionReader):
    """
    Reader that looks for and parses the contents of INI files.
    """

    potential_config_filenames = ["setup.cfg", ".importlinter"]
    section_name = "importlinter"

    def _read_config_filename(
        self, config_filename: str, *, fragment: bool = False
    ) -> UserOptions | None:
        config = configparser.ConfigParser()
        file_contents = settings.FILE_SYSTEM.read(config_filename)
        config.read_string(file_contents)
        if fragment and self.section_name not in config.sections():
            config.add_section(self.section_name)
        if self.section_name in config.sections():
            return self._build_from_config(config)
        return None

    def _build_from_config(self, config: configparser.ConfigParser) -> UserOptions:
        session_options = self._clean_section_config(dict(config[self.section_name]))
        contract_options = []
        for section_name in config.sections():
            if section_name.startswith(f"{self.section_name}:"):
                contract_option = {"id": section_name.split(":")[-1]}
                contract_option.update(self._clean_section_config(dict(config[section_name])))
                contract_options.append(contract_option)
        return UserOptions(session_options=session_options, contracts_options=contract_options)

    @staticmethod
    def _clean_section_config(section_config: dict[str, Any]) -> dict[str, Any]:
        section_dict: dict[str, Any] = {}
        for key, value in section_config.items():
            if "\n" not in value:
                section_dict[key] = value
            else:
                section_dict[key] = value.strip().split("\n")
        return section_dict


class TomlFileUserOptionReader(AbstractUserOptionReader):
    """
    Reader that looks for and parses the contents of TOML files.
    """

    section_name = "importlinter"
    potential_config_filenames = ["pyproject.toml"]

    def _read_config_filename(
        self, config_filename: str, *, fragment: bool = False
    ) -> UserOptions | None:
        file_contents = settings.FILE_SYSTEM.read(config_filename, encoding="utf-8")
        data = tomllib.loads(file_contents)

        tool_data = data.get("tool", {})
        session_options = tool_data.get("importlinter", {})
        if not session_options:
            return None

        if not isinstance(session_options, dict):
            raise InvalidUserOptions(f"Invalid import-linter table in {config_filename}.")
        contracts = session_options.pop("contracts", [])
        if not isinstance(contracts, list) or any(not isinstance(c, dict) for c in contracts):
            raise InvalidUserOptions(f"Invalid contracts in {config_filename}: expected tables.")

        self._normalize_booleans(session_options)
        for contract in contracts:
            self._normalize_booleans(contract)

        return UserOptions(session_options=session_options, contracts_options=contracts)

    def _normalize_booleans(self, data: dict) -> None:
        for key, value in data.items():
            if isinstance(value, bool):
                data[key] = str(value)
