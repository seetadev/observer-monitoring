"""Command-line administration: python run.py {hash-password|agent-add|agent-list|agent-revoke} ..."""
import getpass
import sys

import auth


def hash_password_command():
    first = getpass.getpass("New admin password (min %d characters): " % auth.MIN_PASSWORD_LENGTH)
    if first != getpass.getpass("Repeat: "):
        print("Passwords do not match.", file=sys.stderr)
        return 1
    try:
        print(auth.hash_password(first))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def agent_add_command(name):
    try:
        token = auth.run(auth.create_agent(name))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Agent %s created. Token (shown once, store it safely):\n%s" % (name, token))
    return 0


def agent_list_command():
    for name in auth.run(auth.list_agents()):
        print(name)
    return 0


def agent_revoke_command(name):
    if auth.run(auth.revoke_agent(name)):
        print("Agent %s revoked." % name)
        return 0
    print("No such agent: %s" % name, file=sys.stderr)
    return 1
