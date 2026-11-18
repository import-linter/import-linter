import abc

from ..user_options import ConfigSource, UserOptions


class UserOptionReader(abc.ABC):
    @abc.abstractmethod
    def read_options(self, config_filename: str | None = None) -> UserOptions | None:
        raise NotImplementedError

    def read_source(
        self, config_filename: str | None = None, *, fragment: bool = False
    ) -> ConfigSource | None:
        """Read options with their origin; legacy readers may override for discovery."""
        options = self.read_options(config_filename)
        return ConfigSource(config_filename, options) if options else None
