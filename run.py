import sys

__author__ = 'sdpatro'

USAGE = "usage: python run.py {remote|listener|dash|sim|live|compute} PORT"


def main(argv):
    if len(argv) < 3:
        print("ERROR: Arguments missing\n" + USAGE)
        return 2

    role, port = argv[1], argv[2]

    if role == "remote":
        import remote
        remote.start_remote_server(port)
    elif role in ("listener", "dash", "sim", "live", "compute"):
        import root
        starter = {"listener": root.start_listener, "dash": root.start_dash_server, "sim": root.start_sim_server,
                   "live": root.start_live_server, "compute": root.start_compute_server}[role]
        try:
            starter(port)
        except KeyboardInterrupt:
            pass
    else:
        print("Invalid args\n" + USAGE)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
