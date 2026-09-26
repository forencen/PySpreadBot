"""Application entry point."""

import logging

from pyspreadbot.config import load_settings


def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.getLogger(__name__).info("%s started", settings.app_name)


if __name__ == "__main__":
    main()
