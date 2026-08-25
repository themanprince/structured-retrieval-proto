import logging
import sys

# 1. Create a named logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG) # Catch all logs down to DEBUG level

# 2. Create a console handler pointing to stdout (or stderr)
cli_handler = logging.StreamHandler(sys.stdout)
cli_handler.setLevel(logging.DEBUG)

# 3. Create a clean message format
formatter = logging.Formatter("[%(levelname)s] %(message)s")
cli_handler.setFormatter(formatter)

# 4. Attach the handler to your logger
logger.addHandler(cli_handler)