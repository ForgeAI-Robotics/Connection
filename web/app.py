"""Compatibility entry for the independent operations panel."""
from connection.ops.app import create_app
from connection.ops.control import *
from connection.ops import dream_remote, vla_remote, tmuxctl
app = create_app()

if __name__ == '__main__':
    from connection.ops.__main__ import main
    main()
