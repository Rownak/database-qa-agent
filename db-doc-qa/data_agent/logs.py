"""Logging setup shared by the agent package.

Cloud Run collects anything written to stderr into Cloud Logging, but ``adk
api_server`` only configures its own ``google_adk`` logger, so INFO records from this
package would otherwise be dropped. :func:`get_logger` gives each module a child of the
``data_agent`` logger, which has its own stderr handler at INFO level.

Nothing logged through it may contain file contents or the API key (design section 7).
"""

import logging

_ROOT_NAME = "data_agent"


def get_logger(name: str) -> logging.Logger:
    """Returns a logger under the ``data_agent`` namespace, configuring it once.

    Args:
        name: Usually the calling module's ``__name__``.

    Returns:
        A logger whose records reach stderr at INFO level and above.

    Side Effects:
        On first use, attaches a stderr handler to the ``data_agent`` logger and
        stops its records from propagating, so they are not printed twice if the
        host (e.g. pytest, Streamlit) configures the root logger.
    """
    root = logging.getLogger(_ROOT_NAME)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        root.propagate = False
    return logging.getLogger(name)
