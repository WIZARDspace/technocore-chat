# The `python -m technocore_exporter` shim. No cover: two lines that only run as a module
# entrypoint, and main() itself is the serve loop whose parts are tested in test_server.py.
from .server import main  # pragma: no cover

main()  # pragma: no cover
