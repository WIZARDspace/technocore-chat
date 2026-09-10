# The `python -m technocore_exporter` shim. No cover: two lines that only run as a module
# entrypoint. main() itself is driven by test_server.py with the serve call stubbed.
from .server import main  # pragma: no cover

main()  # pragma: no cover
