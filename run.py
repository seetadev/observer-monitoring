import sys

__author__ = 'sdpatro'

USAGE = ("usage: python run.py {listener|dash|live|compute} PORT\n"
         "       python run.py agent [--config FILE] [--check]\n"
         "       python run.py monitor\n"
         "       python run.py hash-password | agent-add NAME | agent-list | agent-revoke NAME")


def admin_command(argv):
    import admin_cli
    commands = {"hash-password": (0, admin_cli.hash_password_command), "agent-add": (1, admin_cli.agent_add_command),
                "agent-list": (0, admin_cli.agent_list_command), "agent-revoke": (1, admin_cli.agent_revoke_command)}
    arity, command = commands[argv[1]]
    if len(argv) - 2 != arity:
        print("ERROR: Arguments missing\n" + USAGE)
        return 2
    return command(*argv[2:])


def main(argv):
    if len(argv) >= 2 and argv[1] in ("agent", "remote"):
        import agent
        return agent.main(argv[2:] if argv[1] == "agent" else [])
    if len(argv) >= 2 and argv[1] == "monitor":
        import monitor
        return monitor.main()
    if len(argv) >= 2 and argv[1] in ("hash-password", "agent-add", "agent-list", "agent-revoke"):
        return admin_command(argv)
    if len(argv) < 3:
        print("ERROR: Arguments missing\n" + USAGE)
        return 2

    role, port = argv[1], argv[2]

    if role in ("listener", "dash", "live", "compute"):
        import root
        starter = {"listener": root.start_listener, "dash": root.start_dash_server,
                   "live": root.start_live_server, "compute": root.start_compute_server}[role]
        import auth
        try:
            starter(port)
        except KeyboardInterrupt:
            pass
        except auth.ConfigError as exc:
            print("Refusing to start: " + str(exc), file=sys.stderr)
            return 1
    else:
        print("Invalid args\n" + USAGE)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
