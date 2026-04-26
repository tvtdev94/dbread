"""Allow `python -m dbread` to invoke the same entry point as the `dbread` script."""

from dbread.server import main

if __name__ == "__main__":
    main()
